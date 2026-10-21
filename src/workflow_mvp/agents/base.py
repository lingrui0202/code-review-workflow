"""Agent 基类，以及"Agent → 工作流节点"的适配。

项目 1 里的 Agent 是一个自己跑 ReAct 循环的个体；到了工作流里，Agent 变成
**一个有输入、有输出、不知道前后是谁的算子**。这个适配层只做一件事：
把 ``act()`` 的返回值翻译成引擎认识的 ``NodeResult``（patch + messages）。

因此接入任何已有的 Agent（包括项目 1 的 CodeAgent）都只需要：
实现一个 ``act()``，把结果放进 patch，把要说的话放进 messages。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import LLMProvider
from ..workflow.message import Message
from ..workflow.node import Node, NodeContext, NodeResult
from ..workspace import Workspace


@dataclass
class AgentResult:
    """一个 Agent 一次"出手"的结果。"""

    patch: dict[str, Any] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    note: str = ""
    status: str = "ok"

    def say(self, message: Message) -> "AgentResult":
        self.messages.append(message)
        return self


class BaseAgent(ABC):
    """所有 Agent 的父类。"""

    kind = "agent"
    role = "agent"

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        provider: LLMProvider | None = None,
        workspace: Workspace | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> None:
        self.name = name
        self.description = description
        self.provider = provider
        self.workspace = workspace
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------ 工具
    @property
    def offline(self) -> bool:
        return self.provider is None or getattr(self.provider, "offline", False)

    def ask_model(self, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> str:
        if self.provider is None or self.offline:
            raise RuntimeError(f"{self.name} 运行在离线模式，不应调用模型")
        response = self.provider.chat(
            messages,
            temperature=self.temperature,
            max_tokens=max_tokens or self.max_tokens,
        )
        return response.text

    def log(self, ctx: NodeContext, text: str) -> None:
        ctx.emit("agent", f"  · {self.name}: {text}")

    def inbox_latest(self, ctx: NodeContext, kind: str | None = None) -> Message | None:
        return ctx.bus.latest(self.name, kind)

    def require(self, ctx: NodeContext, key: str) -> Any:
        value = ctx.state.get(key)
        if value in (None, "", [], {}):
            raise ValueError(f"节点 {self.name} 缺少上游输入 state.{key}")
        return value

    # ------------------------------------------------------------------ 主入口
    @abstractmethod
    def act(self, ctx: NodeContext) -> AgentResult: ...


class AgentNode(Node):
    """把 ``BaseAgent`` 包成工作流节点。"""

    kind = "agent"

    def __init__(self, agent: BaseAgent, *, retries: int = 1) -> None:
        super().__init__(agent.name, description=agent.description, retries=retries, tags=(agent.role,))
        self.agent = agent

    def run(self, ctx: NodeContext) -> NodeResult:
        result = self.agent.act(ctx)
        return NodeResult(
            patch=result.patch,
            messages=result.messages,
            note=result.note,
            status=result.status,
        )
