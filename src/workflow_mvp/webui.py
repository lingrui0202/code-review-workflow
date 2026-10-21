"""本地 Web 界面：浏览器里贴代码 → 跑工作流 → 实时看三个 Agent 在干什么。

只用标准库（http.server），不起前端构建、不连 CDN、不装 Streamlit：

- 页面是内嵌的一份 HTML（``data/webui.html``），离线可用；
- 服务只监听 127.0.0.1，跑完不常驻；
- 每次审查在**临时目录**里进行，不会动你机器上的任何文件；
- 前端用轮询取状态，因为工作流按秒级推进，SSE 的复杂度在这里不值当。
"""

from __future__ import annotations

import importlib.resources
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm.base import LLMError
from .llm.factory import create_provider
from .llm.offline import OfflineProvider
from .pipelines import build_review_workflow, initial_state
from .review.finding import from_dicts
from .workflow.engine import EngineConfig, RunResult, WorkflowEngine
from .workspace import Workspace

MAX_CODE_BYTES = 400_000


@dataclass
class ReviewRun:
    """一次网页审查的全部可见状态。"""

    id: str
    filename: str
    started_at: float = field(default_factory=time.time)
    status: str = "running"  # running / completed / failed
    events: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    finished_at: float = 0.0

    def add_event(self, level: str, text: str) -> None:
        self.events.append(
            {
                "t": round((time.time() - self.started_at) * 1000),
                "level": level,
                "text": text,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "elapsed_ms": int(elapsed * 1000),
            "events": self.events[-80:],
            "payload": self.payload,
            "error": self.error,
        }


class ReviewService:
    """跑审查的线程池（每个请求一个线程，够 MVP 用）。"""

    def __init__(self) -> None:
        self._runs: dict[str, ReviewRun] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 接口
    def start(
        self,
        code: str,
        filename: str = "review_target.py",
        *,
        max_rounds: int = 2,
        use_model: bool = False,
    ) -> ReviewRun:
        if not code.strip():
            raise ValueError("代码是空的")
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise ValueError("代码太大（上限 400KB），请只贴要审查的模块")
        run = ReviewRun(id=uuid.uuid4().hex[:10], filename=filename or "review_target.py")
        with self._lock:
            self._runs[run.id] = run
        thread = threading.Thread(target=self._execute, args=(run, code, max_rounds, use_model), daemon=True)
        thread.start()
        return run

    def get(self, run_id: str) -> ReviewRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def prune(self, keep: int = 30) -> None:
        with self._lock:
            if len(self._runs) <= keep:
                return
            for old in sorted(self._runs.values(), key=lambda r: r.started_at)[: len(self._runs) - keep]:
                self._runs.pop(old.id, None)

    # ------------------------------------------------------------------ 执行
    def _execute(self, run: ReviewRun, code: str, max_rounds: int, use_model: bool) -> None:
        try:
            with _temp_workspace() as workspace:
                target = run.filename
                workspace.write_text(target, code, backup=False)

                provider = OfflineProvider()
                if use_model:
                    try:
                        provider = create_provider()
                        run.add_event("info", f"已连接模型：{provider.describe()}")
                    except LLMError as exc:
                        run.add_event("warn", f"没有可用的 API Key，改用离线规则引擎（{exc}）")

                graph = build_review_workflow(provider, workspace)
                state = initial_state(
                    task="",
                    code=code,
                    code_path=target,
                    workspace=workspace.root,
                    max_rounds=max(1, min(5, max_rounds)),
                )
                engine = WorkflowEngine(
                    graph,
                    state,
                    config=EngineConfig(run_root=workspace.root / "runs", checkpoint=False),
                    emit=run.add_event,
                    services={},
                )
                result = engine.run()
                run.payload = _build_payload(result, workspace, provider)
                run.status = "completed" if result.ok else "failed"
                if not result.ok:
                    run.error = result.stop_reason
                run.add_event("done", f"流程结束：{result.status}（{result.stop_reason}）")
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回到页面上
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.add_event("error", run.error)
        finally:
            run.finished_at = time.time()


class _temp_workspace:
    """临时工作区：审查过程只写临时目录，退出即删。"""

    def __enter__(self) -> Workspace:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="workflow-web-")
        return Workspace(self._tmp.name)

    def __exit__(self, *exc_info: object) -> None:
        self._tmp.cleanup()


# ---------------------------------------------------------------------- 载荷


def _build_payload(result: RunResult, workspace: Workspace, provider: Any) -> dict[str, Any]:
    state = result.state
    findings = from_dicts(state.get("findings") or [])
    history = state.get("findings_history") or []
    code_history = state.get("code_history") or []
    filename = state.get("code_path") or "review_target.py"

    diff = ""
    if len(code_history) >= 2:
        diff = workspace.diff(code_history[0]["code"], code_history[-1]["code"], filename)

    fix_log = state.get("fix_log") or []
    return {
        "status": result.status,
        "stop_reason": result.stop_reason,
        "provider": provider.describe(),
        "mode": "离线（规则引擎）" if getattr(provider, "offline", False) else "在线（规则 + 模型）",
        "filename": filename,
        "verdict": state.get("verdict") or "pending",
        "summary": state.get("review_summary") or {},
        "history": history,
        "steps": [
            {
                "index": step.index,
                "node": step.node,
                "status": step.status,
                "iteration": step.iteration,
                "duration_ms": step.duration_ms,
                "note": step.note,
                "changed": list(step.changed),
            }
            for step in result.steps
        ],
        "findings": [
            {
                "rule": f.rule,
                "severity": f.severity,
                "line": f.line,
                "symbol": f.symbol,
                "message": f.message,
                "suggestion": f.suggestion,
                "fixable": f.fixable,
                "source": f.source,
            }
            for f in findings
        ],
        "fix_log": fix_log,
        "manual_todo": state.get("manual_todo") or [],
        "diff": diff,
        "code_before": code_history[0]["code"] if code_history else state.get("code", ""),
        "code_after": state.get("code", ""),
        "messages": [
            {
                "iteration": m.iteration,
                "kind": m.kind,
                "sender": m.sender,
                "recipient": m.recipient,
                "content": m.content,
            }
            for m in result.bus.history
        ],
        "message_stats": result.bus.stats(),
    }


# ---------------------------------------------------------------------- HTML


def load_page() -> str:
    resource = importlib.resources.files("workflow_mvp").joinpath("data/webui.html")
    return resource.read_text(encoding="utf-8")


def example_code() -> str:
    resource = importlib.resources.files("workflow_mvp").joinpath("data/webui_example.py")
    return resource.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- HTTP


def build_server(host: str = "127.0.0.1", port: int = 8765):
    """构建 HTTP 服务（不启动）。port=0 时由系统分配，测试用。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    service = ReviewService()
    page_cache = {"html": load_page(), "example": example_code()}

    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkflowMVP/1.0"

        # ---------------------------------------------------------- 路由
        def do_GET(self) -> None:  # noqa: N802
            from urllib.parse import urlparse

            path = urlparse(self.path).path  # 忽略 ?demo=1 这类查询参数
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page_cache["html"])
                return
            if path.startswith("/api/example"):
                self._send_json({"code": page_cache["example"]})
                return
            if path.startswith("/api/status"):
                run_id = self._query("id")
                run = service.get(run_id or "")
                if run is None:
                    self._send_json({"error": "run 不存在"}, status=404)
                    return
                self._send_json(run.to_dict())
                return
            if path.startswith("/api/health"):
                self._send_json({"ok": True})
                return
            self._send(404, "text/plain; charset=utf-8", "not found")

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/review"):
                self._send_json({"error": "not found"}, status=404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 2_000_000:
                self._send_json({"error": "请求体为空或过大"}, status=400)
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                run = service.start(
                    str(body.get("code") or ""),
                    str(body.get("filename") or "review_target.py"),
                    max_rounds=int(body.get("max_rounds") or 2),
                    use_model=bool(body.get("use_model")),
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            service.prune()
            self._send_json({"run_id": run.id})

        # ---------------------------------------------------------- 工具
        def _query(self, key: str) -> str | None:
            from urllib.parse import parse_qs, urlparse

            values = parse_qs(urlparse(self.path).query).get(key)
            return values[0] if values else None

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            self._send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False))

        def _send(self, status: int, content_type: str, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:  # 别刷屏
            return

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.service = service  # 方便测试拿 service
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True) -> None:
    """起一个只监听本机的 HTTP 服务。Ctrl+C 结束。"""
    httpd = build_server(host, port)
    bound_port = httpd.server_address[1]
    url = f"http://{host}:{bound_port}/"
    print("\n  代码审查工作流 · Web 界面")
    print("  " + "-" * 40)
    print(f"  地址：{url}")
    print("  停止：Ctrl+C")
    print()
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # pragma: no cover - 打不开浏览器不影响服务
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        httpd.server_close()
