"""Agent 注册表：按名字建 Agent。

工作流里没有 ``if kind == "reviewer"`` 这种硬编码，只有一次查表。加一个新角色
（测试 Agent、文档 Agent、项目 1 的 CodeAgent 适配器）= 注册一行。

```python
from workflow_mvp.agents import register_agent
register_agent("tester", lambda **kw: MyTesterAgent(**kw))
```
"""

from __future__ import annotations

from typing import Callable

from ..llm.base import LLMProvider
from ..workspace import Workspace
from .base import BaseAgent
from .coder import CoderAgent
from .fixer import FixerAgent
from .reviewer import ReviewerAgent

AgentFactory = Callable[..., BaseAgent]

_REGISTRY: dict[str, AgentFactory] = {
    "coder": CoderAgent,
    "reviewer": ReviewerAgent,
    "fixer": FixerAgent,
}

ROLE_DESCRIPTIONS = {
    "coder": "按需求规格产出初稿",
    "reviewer": "审查代码并输出结构化问题清单",
    "fixer": "按问题清单修复并复验",
}


def register_agent(kind: str, factory: AgentFactory) -> None:
    """注册/覆盖一种 Agent 类型。"""
    _REGISTRY[kind] = factory


def agent_kinds() -> list[str]:
    return sorted(_REGISTRY)


def create_agent(
    kind: str,
    *,
    name: str | None = None,
    provider: LLMProvider | None = None,
    workspace: Workspace | None = None,
    temperature: float = 0.2,
) -> BaseAgent:
    try:
        factory = _REGISTRY[kind]
    except KeyError as exc:
        raise KeyError(f"未注册的 Agent 类型 {kind!r}，可用：{', '.join(agent_kinds())}") from exc
    return factory(
        name=name or kind,
        provider=provider,
        workspace=workspace,
        temperature=temperature,
    )


def build_standard_agents(
    provider: LLMProvider | None,
    workspace: Workspace,
    *,
    temperature: float = 0.2,
) -> dict[str, BaseAgent]:
    """主流程用到的三个 Agent。"""
    return {
        kind: create_agent(kind, provider=provider, workspace=workspace, temperature=temperature)
        for kind in ("coder", "reviewer", "fixer")
    }


__all__ = [
    "BaseAgent",
    "CoderAgent",
    "ReviewerAgent",
    "FixerAgent",
    "register_agent",
    "create_agent",
    "agent_kinds",
    "build_standard_agents",
    "ROLE_DESCRIPTIONS",
]
