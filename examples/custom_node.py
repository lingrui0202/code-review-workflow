"""扩展示例：给工作流加一个"测试 Agent"节点。

这个脚本演示两件事：

1. **节点可以随便加**：`graph.add_node(...)` + 两条边即可，不用改引擎；
2. **Agent 可以随便换**：`register_agent("tester", ...)` 之后，主流程照常构建。

新节点做的是"跑得起来吗"这件事——它把改完的模块真的执行一遍，
这和审查 Agent 的"看得对不对"是两个维度。

运行：
    python examples/custom_node.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_mvp.agents import build_standard_agents, register_agent  # noqa: E402
from workflow_mvp.agents.base import AgentNode, AgentResult, BaseAgent  # noqa: E402
from workflow_mvp.config import Settings  # noqa: E402
from workflow_mvp.llm.offline import OfflineProvider  # noqa: E402
from workflow_mvp.pipelines import initial_state  # noqa: E402
from workflow_mvp.report import ReportContext, write_artifacts  # noqa: E402
from workflow_mvp.workflow.engine import EngineConfig, WorkflowEngine  # noqa: E402
from workflow_mvp.workflow.graph import Graph  # noqa: E402
from workflow_mvp.workflow.message import Message  # noqa: E402
from workflow_mvp.workspace import Workspace  # noqa: E402


class TesterAgent(BaseAgent):
    """把模块真的跑起来，看它会不会崩。"""

    role = "tester"

    def __init__(self, name: str = "tester", **kw) -> None:
        super().__init__(name, description="编译并执行模块，验证可运行", **kw)

    def act(self, ctx):
        workspace = self.workspace or Workspace(ctx.workspace)
        code_path = ctx.state.get("code_path") or "output/draft.py"
        target = workspace.resolve(code_path)
        if not target.exists():
            raise ValueError(f"待测文件不存在：{target}")

        compiled = subprocess.run(
            [sys.executable, "-m", "py_compile", str(target)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ran = {"returncode": None, "output": "", "error": ""}
        if compiled.returncode == 0:
            result = subprocess.run(
                [sys.executable, str(target)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(workspace.root),
            )
            ran = {
                "returncode": result.returncode,
                "output": (result.stdout or "").strip()[:500],
                "error": (result.stderr or "").strip()[:500],
            }

        ok = compiled.returncode == 0 and ran["returncode"] == 0
        self.log(ctx, f"编译 {'通过' if compiled.returncode == 0 else '失败'}，运行 {'通过' if ran['returncode'] == 0 else '失败'}")
        return AgentResult(
            patch={
                "tests": {
                    "compiled": compiled.returncode == 0,
                    "ran": ran["returncode"] == 0,
                    "stdout": ran["output"],
                    "stderr": ran["error"],
                }
            },
            messages=[
                Message.verdict(
                    self.name,
                    "*",
                    f"可运行性检查：编译 {'OK' if compiled.returncode == 0 else 'FAIL'} / "
                    f"执行 {'OK' if ran['returncode'] == 0 else 'FAIL'}",
                )
            ],
            note="运行检查完成",
        )


def build_graph(provider, workspace: Workspace) -> Graph:
    agents = build_standard_agents(provider, workspace)
    register_agent("tester", TesterAgent)
    tester = TesterAgent(provider=provider, workspace=workspace)

    graph = Graph("code-review-with-tests", "编码 → 审查 → 修复 → 运行验证")
    graph.add_node(AgentNode(agents["coder"]), start=True)
    graph.add_node(AgentNode(agents["reviewer"]))
    graph.add_node(AgentNode(agents["fixer"]))
    graph.add_node(AgentNode(tester))
    graph.chain("coder", "reviewer", "fixer", "tester")
    return graph.validate()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Workspace(tmp)
        settings = Settings(offline=True, workspace=Path(tmp), run_root=Path(tmp) / "runs")
        provider = OfflineProvider()
        graph = build_graph(provider, workspace)
        print(graph.to_ascii())
        print()

        engine = WorkflowEngine(
            graph,
            initial_state(task="写一个订单计价模块", workspace=Path(tmp), max_rounds=2),
            config=EngineConfig(run_root=Path(tmp) / "runs"),
            emit=lambda level, text: print(f"  {text}") if level in ("step", "agent", "warn", "error", "write") else None,
            services={"settings": settings},
        )
        result = engine.run()
        write_artifacts(ReportContext(result=result, graph=graph, settings=settings, workspace=workspace), result.run_dir)

        print()
        print(f"状态：{result.status}")
        print(f"运行检查：{result.state.get('tests')}")
        print(f"报告：{result.run_dir / 'report.md'}")
        return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
