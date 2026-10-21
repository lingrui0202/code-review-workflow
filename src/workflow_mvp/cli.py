"""命令行入口。

设计目标：第一次用的人只看 ``--help`` 就能跑起来；跑完一定能拿到
``runs/<run_id>/report.md``。三个子命令对应三种使用场景：

- ``run``：跑一次完整工作流（默认离线，不需要任何 Key）；
- ``graph``：只看流程结构，不执行；
- ``rules``：看审查规则清单。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .agents import ROLE_DESCRIPTIONS, agent_kinds
from .config import DEFAULT_MAX_ROUNDS, Settings, load_dotenv
from .llm.base import LLMError
from .llm.factory import PROVIDER_NAMES, create_provider
from .pipelines import build_review_workflow, initial_state
from .report import ReportContext, write_artifacts
from .review.finding import SEVERITY_LABEL, from_dicts
from .review.rules import RULE_TABLE
from .workflow import checkpoint as checkpoint_io
from .workflow.engine import EngineConfig, RunResult, WorkflowEngine
from .workspace import Workspace, WorkspaceError

BANNER = "代码审查与修复工作流  ·  Code Review Workflow MVP"


def _supports_unicode() -> bool:
    encoding = (sys.stdout.encoding or "").lower()
    return "utf" in encoding


def _sym(fallback: str, unicode_char: str) -> str:
    return unicode_char if _supports_unicode() else fallback


class Printer:
    """控制台输出。``quiet`` 只保留关键节点。"""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.ok = _sym("[ok]", "✔")
        self.fail = _sym("[x]", "✖")
        self.arrow = _sym("->", "→")

    def emit(self, level: str, text: str) -> None:
        if self.quiet and level in ("state", "message", "agent", "start"):
            return
        prefix = {
            "step": f"{self.ok} ",
            "message": "    ",
            "state": "    ",
            "agent": "  ",
            "warn": f"{_sym('[!]', '⚠')} ",
            "error": f"{self.fail} ",
            "write": "  ",
            "start": "",
            "done": "",
        }.get(level, "  ")
        print(f"{prefix}{text}")

    def section(self, title: str) -> None:
        print(f"\n{title}")
        print("-" * min(72, len(title) * 2 + 4))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflow-mvp",
        description="代码审查与修复工作流：编码 Agent → 审查 Agent → 修复 Agent（带复检回边）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py run --offline                       # 用内置模板完整跑一遍\n"
            "  python main.py run --code examples/bad_module.py --offline\n"
            "  python main.py run --task examples/task_order.md --provider deepseek\n"
            "  python main.py graph                               # 只看流程图\n"
            "  python main.py rules                               # 看审查规则\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="执行工作流")
    run.add_argument("--task", help="需求规格文件（Markdown/文本）")
    run.add_argument("--code", help="直接审查一个已有文件（跳过编码节点）")
    run.add_argument("--offline", action="store_true", help="离线模式：只用规则引擎，不调用模型")
    run.add_argument("--provider", choices=PROVIDER_NAMES, help="模型服务商，默认读 PROVIDER 或 deepseek")
    run.add_argument("--model", help="覆盖模型名")
    run.add_argument("--api-key", help="覆盖 API Key（优先于环境变量）")
    run.add_argument("--base-url", help="覆盖接口地址（自建/代理时用）")
    run.add_argument("--workspace", default=".", help="工作区根目录，默认当前目录")
    run.add_argument("--output", help="运行产物目录，默认 <workspace>/runs")
    run.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, help=f"最多修复轮次，默认 {DEFAULT_MAX_ROUNDS}")
    run.add_argument("--max-steps", type=int, default=40, help="引擎最大步数保护，默认 40")
    run.add_argument("--temperature", type=float, default=0.2, help="采样温度，默认 0.2")
    run.add_argument("--no-write", action="store_true", help="不写回原文件，只在 runs 目录留修复结果")
    run.add_argument("--quiet", action="store_true", help="只输出关键步骤")
    run.add_argument("--no-checkpoint", action="store_true", help="不写 checkpoint（无法续跑）")

    graph = sub.add_parser("graph", help="打印工作流结构，不执行")
    graph.add_argument("--offline", action="store_true", help="按离线模式构建（不影响结构）")
    graph.add_argument("--workspace", default=".", help="工作区根目录，默认当前目录")

    sub.add_parser("rules", help="打印审查规则清单")

    resume = sub.add_parser("resume", help="从 checkpoint 继续执行")
    resume.add_argument("--checkpoint", required=True, help="checkpoint.json 路径")
    resume.add_argument("--offline", action="store_true")
    resume.add_argument("--provider", choices=PROVIDER_NAMES)
    resume.add_argument("--model")
    resume.add_argument("--api-key")
    resume.add_argument("--base-url")
    resume.add_argument("--workspace", default=".")
    resume.add_argument("--max-rounds", type=int, default=None)
    resume.add_argument("--quiet", action="store_true")
    return parser


# ---------------------------------------------------------------------- 执行


def _make_settings(args: argparse.Namespace) -> Settings:
    load_dotenv()
    workspace = Path(getattr(args, "workspace", ".") or ".").expanduser()
    settings = Settings(
        provider=getattr(args, "provider", None) or "deepseek",
        model=getattr(args, "model", None),
        api_key=getattr(args, "api_key", None),
        base_url=getattr(args, "base_url", None),
        offline=bool(getattr(args, "offline", False)),
        workspace=workspace,
        run_root=Path(args.output) if getattr(args, "output", None) else Path("runs"),
        max_rounds=getattr(args, "max_rounds", None) or DEFAULT_MAX_ROUNDS,
        temperature=getattr(args, "temperature", 0.2),
        write_back=not getattr(args, "no_write", False),
    )
    if not settings.workspace.exists():
        raise WorkspaceError(f"工作区不存在：{settings.workspace}")
    return settings


def _make_provider(settings: Settings):
    try:
        return create_provider(
            settings.provider,
            api_key=settings.api_key,
            model=settings.model,
            base_url=settings.base_url,
            offline=settings.offline,
        )
    except LLMError as exc:
        print(f"{_sym('[!]', '⚠')} {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _resolve_inputs(args: argparse.Namespace, workspace: Workspace) -> tuple[str, str | None, str | None]:
    """返回 (任务规格文本, 代码全文, 代码相对路径)。

    什么都不传时：离线模式用内置的需求规格 + 内置模板跑一遍演示；
    在线模式则要求显式给出 --task 或 --code，避免悄悄烧 token。
    """
    task = ""
    code: str | None = None
    code_path: str | None = None
    if args.task:
        task_file = workspace.resolve(args.task, must_exist=True)
        task = task_file.read_text(encoding="utf-8")
    if args.code:
        code_file = workspace.resolve(args.code, must_exist=True)
        code = code_file.read_text(encoding="utf-8")
        code_path = workspace.rel(code_file)
    if not task and not code:
        if not getattr(args, "offline", False):
            raise SystemExit(
                "需要 --task（需求规格）或 --code（待审查文件）其中之一；\n"
                "想先看看效果：python main.py run --offline"
            )
        task = _default_task()
    return task, code, code_path


def _default_task() -> str:
    import importlib.resources

    resource = importlib.resources.files("workflow_mvp").joinpath("data/demo_task.md")
    return resource.read_text(encoding="utf-8")


def _summary(result: RunResult, settings: Settings, printer: Printer) -> None:
    state = result.state
    history = state.get("findings_history") or []
    findings = from_dicts(state.get("findings") or [])
    printer.section("运行结果")
    status = {"completed": "完成", "failed": "失败", "aborted": "中止"}.get(result.status, result.status)
    print(f"  状态：{status}（{result.stop_reason}）")
    if history:
        first, last = history[0], history[-1]
        print(
            f"  问题数：{first.get('total')} → {last.get('total')} 处"
            f"（高 {first.get('high')}→{last.get('high')}，中 {first.get('medium')}→{last.get('medium')}，"
            f"低 {first.get('low')}→{last.get('low')}）"
        )
        print(f"  审查轮次：{len(history)}，修复记录：{len(state.get('fix_log') or [])} 条")
    blocking = [f for f in findings if f.severity in ("high", "medium")]
    print(f"  裁决：{'通过（无高/中风险）' if not blocking else f'需人工复核（{len(blocking)} 处高/中风险）'}")

    remaining = [f for f in findings if f.severity in ("high", "medium")][:8]
    if remaining:
        printer.section("遗留问题（需人工）")
        for finding in remaining:
            where = f"L{finding.line}" + (f" {finding.symbol}" if finding.symbol else "")
            print(f"  [{SEVERITY_LABEL[finding.severity]}] {finding.rule:20s} {where:16s} {finding.message}")

    if result.run_dir:
        print(f"\n  报告：{result.run_dir / 'report.md'}")
        print(f"  trace：{result.run_dir / 'trace.json'}")


def _cmd_run(args: argparse.Namespace) -> int:
    printer = Printer(quiet=args.quiet)
    settings = _make_settings(args)
    workspace = Workspace(settings.workspace)
    task, code, code_path = _resolve_inputs(args, workspace)
    provider = _make_provider(settings)

    print(BANNER)
    print(f"模式：{settings.model_mode}    工作区：{settings.workspace}")

    graph = build_review_workflow(provider, workspace, temperature=settings.temperature)
    print()
    print(graph.to_ascii())
    print()

    state = initial_state(
        task=task,
        code=code,
        code_path=code_path,
        workspace=settings.workspace,
        max_rounds=settings.max_rounds,
    )
    config = EngineConfig(
        max_steps=args.max_steps,
        run_root=settings.run_root,
        checkpoint=not args.no_checkpoint,
    )
    engine = WorkflowEngine(
        graph,
        state,
        config=config,
        emit=printer.emit,
        services={"settings": settings},
    )
    result = engine.run()
    if result.run_dir:
        written = write_artifacts(
            ReportContext(result=result, graph=graph, settings=settings, workspace=workspace),
            result.run_dir,
        )
        result.state.set("artifacts", {key: str(path) for key, path in written.items()})

    _summary(result, settings, printer)
    return 0 if result.status == "completed" else 1


def _cmd_graph(args: argparse.Namespace) -> int:
    settings = _make_settings(args)
    workspace = Workspace(settings.workspace)
    # 看结构不需要模型：固定用离线 provider，避免没配 Key 就看不了图
    from .llm.offline import OfflineProvider

    graph = build_review_workflow(OfflineProvider(), workspace)
    print(BANNER)
    print()
    print(graph.to_ascii())
    print()
    print("节点说明：")
    for node in graph.nodes():
        print(f"  {node.name:10s} [{node.kind}] {node.description}  角色：{ROLE_DESCRIPTIONS.get(node.name, '—')}")
    print()
    print("可用 Agent 类型：" + "、".join(agent_kinds()))
    print()
    print("mermaid：")
    print(graph.to_mermaid())
    return 0


def _cmd_rules(_: argparse.Namespace) -> int:
    print(BANNER)
    print()
    print(f"{'规则':22s}{'严重度':8s}{'可自动修复':10s}说明")
    print("-" * 96)
    for rule in RULE_TABLE:
        fixable = "是" if rule.fixable else "否"
        print(f"{rule.id:22s}{SEVERITY_LABEL[rule.severity]:8s}{fixable:10s}{rule.title} —— {rule.rationale[:46]}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    printer = Printer(quiet=args.quiet)
    settings = _make_settings(args)
    workspace = Workspace(settings.workspace)
    provider = _make_provider(settings)

    path = Path(args.checkpoint).expanduser().resolve()
    data = checkpoint_io.load(path)
    graph = build_review_workflow(provider, workspace, temperature=settings.temperature)

    config = EngineConfig(
        max_steps=40,
        run_root=path.parent.parent if path.parent.name.startswith("20") else settings.run_root,
    )
    engine, next_node = WorkflowEngine.from_checkpoint(
        data,
        graph,
        config=config,
        emit=printer.emit,
        services={"settings": settings},
    )
    print(BANNER)
    print(f"从 checkpoint 续跑：{path}")
    print(f"下一步：{next_node or '（流程已结束）'}")
    if not next_node:
        print("该 checkpoint 已经是终态，无需续跑。")
        return 0

    result = engine.run(start=next_node)
    if result.run_dir:
        write_artifacts(
            ReportContext(result=result, graph=graph, settings=settings, workspace=workspace),
            result.run_dir,
        )
    _summary(result, settings, printer)
    return 0 if result.status == "completed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handlers = {
        "run": _cmd_run,
        "graph": _cmd_graph,
        "rules": _cmd_rules,
        "resume": _cmd_resume,
    }
    try:
        return handlers[args.command](args)
    except (WorkspaceError, FileNotFoundError, ValueError) as exc:
        print(f"\n{_sym('[x]', '✖')} {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
