"""执行引擎：把图跑起来。

负责的只有五件事，但每件都关系到"工作流能不能真的跑通"：

1. **顺序推进**：按 ``Graph.resolve()`` 算下一棒，条件边即分支；
2. **循环保护**：``max_steps`` 总步数 + 单节点 ``max_visits`` 访问次数，双保险；
3. **失败回滚与重试**：节点抛异常时先把黑板恢复到该步之前再重试，避免半截状态；
4. **每步快照**：step 记录 + checkpoint 落盘，支持从任意一步续跑；
5. **事件流**：``emit`` 回调把每一步推给 CLI/Web，界面与引擎解耦。
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import checkpoint
from .bus import MessageBus
from .graph import Graph
from .message import Message
from .node import Node, NodeContext, NodeResult
from .state import WorkflowState

Emit = Callable[[str, str], None]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class EngineConfig:
    max_steps: int = 40
    max_visits: int = 8
    checkpoint: bool = True
    run_root: Path = Path("runs")

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps 至少为 1")
        if self.max_visits < 1:
            raise ValueError("max_visits 至少为 1")


@dataclass
class StepRecord:
    """一步的执行记录。报告里的时间线和 JSON trace 都来自它。"""

    index: int
    node: str
    kind: str
    status: str
    iteration: int
    started_at: str
    duration_ms: int
    note: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    state_digest: str = ""
    changed: dict[str, str] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "node": self.node,
            "kind": self.kind,
            "status": self.status,
            "iteration": self.iteration,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "note": self.note,
            "messages": self.messages,
            "state_digest": self.state_digest,
            "changed": self.changed,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepRecord":
        return cls(
            index=int(data["index"]),
            node=data["node"],
            kind=data.get("kind", "node"),
            status=data.get("status", "ok"),
            iteration=int(data.get("iteration", 1)),
            started_at=data.get("started_at", ""),
            duration_ms=int(data.get("duration_ms", 0)),
            note=data.get("note", ""),
            messages=list(data.get("messages", [])),
            state_digest=data.get("state_digest", ""),
            changed=dict(data.get("changed", {})),
            error=data.get("error", ""),
        )


@dataclass
class RunResult:
    run_id: str
    status: str
    stop_reason: str
    steps: list[StepRecord]
    state: WorkflowState
    bus: MessageBus
    run_dir: Path | None
    started_at: str
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def node_visits(self) -> dict[str, int]:
        return dict(Counter(step.node for step in self.steps))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": [step.to_dict() for step in self.steps],
            "state": self.state.to_dict(),
            "bus": self.bus.to_dict(),
        }


class WorkflowEngine:
    """顺序执行引擎（支持条件分支与回边）。"""

    def __init__(
        self,
        graph: Graph,
        state: WorkflowState | None = None,
        bus: MessageBus | None = None,
        config: EngineConfig | None = None,
        emit: Emit | None = None,
        run_id: str | None = None,
        services: dict[str, Any] | None = None,
    ) -> None:
        self.graph = graph.validate()
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.state = state or WorkflowState()
        self.bus = bus or MessageBus(self.run_id)
        self.bus.run_id = self.run_id
        self.config = config or EngineConfig()
        self.services: dict[str, Any] = services or {}
        self._emit = emit or (lambda level, text: None)
        self._steps: list[StepRecord] = []
        self._run_dir: Path | None = None
        self._started_at = _now()

    # ------------------------------------------------------------------ 事件
    def emit(self, level: str, text: str) -> None:
        try:
            self._emit(level, text)
        except Exception:  # 展示层的问题不能影响流程
            pass

    # ------------------------------------------------------------------ 主循环
    def run(self, start: str | None = None) -> RunResult:
        current = start or self.graph.start
        if current is None:
            raise ValueError("图没有起点")

        self._run_dir = self.config.run_root / self.run_id if self.config.checkpoint else None
        if self._run_dir is not None:
            self._run_dir.mkdir(parents=True, exist_ok=True)

        visits: Counter[str] = Counter(step.node for step in self._steps)
        status, stop_reason = "running", ""

        self.emit("start", f"run {self.run_id} 启动，起点 {current}")
        while current:
            if len(self._steps) >= self.config.max_steps:
                status, stop_reason = "aborted", f"达到最大步数 {self.config.max_steps}"
                break
            if visits[current] >= self.config.max_visits:
                status, stop_reason = "aborted", f"节点 {current} 访问超过 {self.config.max_visits} 次，疑似死循环"
                break

            node = self.graph.node(current)
            visits[current] += 1
            step = self._run_node(node, visits[current])
            self._steps.append(step)

            if step.status != "ok":
                status, stop_reason = "failed", f"节点 {current} 执行失败：{step.error}"
                # next_node 停在这一个节点上：续跑时应该重试它，而不是跳过
                self._save(next_node=current, status=status)
                break

            targets = self.graph.resolve(self.state, current)
            if len(targets) > 1:
                status, stop_reason = "aborted", f"节点 {current} 同时满足多条出边：{targets}"
                self._save(next_node=None, status=status)
                break

            nxt = targets[0] if targets else None
            self._save(next_node=nxt, status="running")
            if nxt is None:
                status, stop_reason = "completed", "没有后续节点，流程正常结束"
            current = nxt

        finished = _now()
        self.emit("done", f"run {self.run_id} 结束：{status}（{stop_reason}）")
        return RunResult(
            run_id=self.run_id,
            status=status,
            stop_reason=stop_reason,
            steps=list(self._steps),
            state=self.state,
            bus=self.bus,
            run_dir=self._run_dir,
            started_at=self._started_at,
            finished_at=finished,
        )

    # ------------------------------------------------------------------ 单步
    def _run_node(self, node: Node, visit: int) -> StepRecord:
        before = self.state.snapshot()
        self.state.enter_node(node.name)
        iteration = int(self.state.get("iterations", 1) or 1)
        started_at = _now()
        attempts = max(1, node.retries + 1)
        error_text = ""
        result: NodeResult | None = None
        started = time.perf_counter()

        for attempt in range(1, attempts + 1):
            ctx = NodeContext(
                run_id=self.run_id,
                node=node.name,
                iteration=iteration,
                state=self.state,
                bus=self.bus,
                workspace=Path(self.state.get("workspace", ".")),
                services=self.services,
                emit=self.emit,
            )
            try:
                result = node.run(ctx)
                break
            except Exception as exc:  # noqa: BLE001 - 节点失败要兜住
                error_text = f"{type(exc).__name__}: {exc}"
                self.state.restore(before)  # 半截状态回滚
                self.state.enter_node(node.name)
                if attempt < attempts:
                    self.emit("warn", f"{node.name} 第 {attempt} 次失败（{error_text}），重试…")
                    time.sleep(min(0.5 * attempt, 2.0))
                else:
                    self.emit("error", f"{node.name} 失败：{error_text}")

        duration_ms = int((time.perf_counter() - started) * 1000)

        if result is None:
            return StepRecord(
                index=len(self._steps) + 1,
                node=node.name,
                kind=node.kind,
                status="failed",
                iteration=iteration,
                started_at=started_at,
                duration_ms=duration_ms,
                error=error_text,
                state_digest=self.state.digest(),
            )

        if result.patch:
            self.state.update(result.patch)
        for message in result.messages:
            message.run_id = message.run_id or self.run_id
            message.iteration = message.iteration or iteration
            self.bus.send(message)

        step = StepRecord(
            index=len(self._steps) + 1,
            node=node.name,
            kind=node.kind,
            status=result.status,
            iteration=iteration,
            started_at=started_at,
            duration_ms=duration_ms,
            note=result.note,
            messages=[message.to_dict() for message in result.messages],
            state_digest=self.state.digest(),
            changed=self.state.diff(before),
        )
        self._log_step(step)
        return step

    def _log_step(self, step: StepRecord) -> None:
        self.emit("step", f"{step.node}（{step.duration_ms}ms）{step.note}")
        for message in step.messages:
            self.emit("message", f"  {message['kind']}: {message['sender']} → {message['recipient']}")
        for key, change in step.changed.items():
            self.emit("state", f"  state.{key}: {change}")

    # ------------------------------------------------------------------ 存档
    def _save(self, next_node: str | None, status: str) -> None:
        if not self.config.checkpoint or self._run_dir is None:
            return
        checkpoint.save(
            self._run_dir / checkpoint.FILENAME,
            run_id=self.run_id,
            graph_name=self.graph.name,
            state=self.state,
            bus=self.bus,
            steps=[step.to_dict() for step in self._steps],
            next_node=next_node,
            status=status,
        )

    # ------------------------------------------------------------------ 续跑
    @classmethod
    def from_checkpoint(
        cls,
        data: dict[str, Any],
        graph: Graph,
        config: EngineConfig | None = None,
        emit: Emit | None = None,
        services: dict[str, Any] | None = None,
    ) -> tuple["WorkflowEngine", str | None]:
        """从 checkpoint 还原引擎，返回 (引擎, 下一步节点)。"""
        engine = cls(
            graph=graph,
            state=checkpoint.restore_state(data),
            bus=checkpoint.restore_bus(data),
            config=config,
            emit=emit,
            run_id=data.get("run_id"),
            services=services,
        )
        engine._steps = [StepRecord.from_dict(raw) for raw in data.get("steps", [])]
        # 失败的那一步不算"已完成"，续跑时会重跑它，所以要从历史里去掉
        while engine._steps and engine._steps[-1].status != "ok":
            engine._steps.pop()
        return engine, data.get("next_node")

    @property
    def steps(self) -> list[StepRecord]:
        return list(self._steps)

    def send(self, message: Message) -> Message:
        """允许外部（例如人工介入）往总线上补一条消息。"""
        message.run_id = message.run_id or self.run_id
        return self.bus.send(message)
