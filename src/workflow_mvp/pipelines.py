"""主流程：编码 → 审查 → 修复（→ 复检回边）。

这张图只有三个节点、四条边，但它已经覆盖了 Workflow 的四个核心概念：

- **编排**：节点顺序由图决定，谁先谁后不写在 Agent 里；
- **通信**：每条边上的交接都是显式消息（result / feedback / verdict）；
- **状态**：所有中间产物（代码、问题清单、修复记录、迭代号）都在共享黑板上；
- **分支与循环**：``needs_fix`` 决定要不要修，``made_progress`` 决定要不要复检。

轮次上限来自 ``state.max_rounds``，因此"修几轮"是运行期参数，不是写死的。
"""

from __future__ import annotations

from .llm.base import LLMProvider
from .workflow.graph import Graph
from .workflow.node import Node
from .workflow.state import WorkflowState
from .workspace import Workspace
from .agents import build_standard_agents
from .agents.base import AgentNode

BLOCKING = ("high", "medium")


def needs_fix(state: WorkflowState) -> bool:
    """还有高/中风险问题，并且还没超过修复轮次上限。"""
    findings = state.get("findings") or []
    blocking = any(str(item.get("severity")) in BLOCKING for item in findings)
    if not blocking:
        return False
    return int(state.get("iterations", 1) or 1) <= int(state.get("max_rounds", 3) or 3)


def made_progress(state: WorkflowState) -> bool:
    """上一轮修复确实改了代码，才值得再审查一遍。"""
    return bool(state.get("last_progress"))


def build_review_workflow(
    provider: LLMProvider | None,
    workspace: Workspace,
    *,
    temperature: float = 0.2,
    agents: dict[str, object] | None = None,
) -> Graph:
    """组装主工作流。"""
    built = agents or build_standard_agents(provider, workspace, temperature=temperature)

    coder = AgentNode(built["coder"])
    reviewer = AgentNode(built["reviewer"])
    fixer = AgentNode(built["fixer"])

    graph = Graph(
        name="code-review-workflow",
        description="编码 → 审查 → 修复，带复检回边",
    )
    graph.add_node(coder, start=True)
    graph.add_node(reviewer)
    graph.add_node(fixer)

    graph.add_edge("coder", "reviewer", label="交付初稿")
    graph.add_edge(
        "reviewer",
        "fixer",
        when=needs_fix,
        label="有高/中风险且未超轮次",
    )
    graph.add_edge("fixer", "reviewer", when=made_progress, label="本轮有改动 → 复检")
    return graph.validate()


def initial_state(
    *,
    task: str = "",
    code_path: str | None = None,
    code: str | None = None,
    workspace: str | Workspace = ".",
    max_rounds: int = 3,
) -> WorkflowState:
    """准备黑板上的起始数据。"""
    state = WorkflowState()
    state.set("workspace", str(workspace))
    state.set("iterations", 1)
    state.set("max_rounds", max(1, int(max_rounds)))
    state.set("task", task or "")
    state.set("verdict", "pending")
    state.set("fix_log", [])
    state.set("patches", [])
    state.set("findings_history", [])
    state.set("manual_todo", [])
    if code_path:
        state.set("code_path", code_path)
    if code is not None:
        state.set("code", code)
        state.set("code_origin", "外部提供")
        state.set("code_history", [{"round": 0, "code": code, "origin": "外部提供"}])
    return state


def node_by_name(graph: Graph, name: str) -> Node:
    return graph.node(name)


def workflow_summary(graph: Graph) -> str:
    return graph.to_ascii()
