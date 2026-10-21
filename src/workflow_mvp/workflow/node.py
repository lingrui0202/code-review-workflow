"""节点抽象。

一个节点 = 工作流里的一次"交棒"。它拿到 ``NodeContext``（黑板 + 总线 + 迭代号），
做完事返回 ``NodeResult``（要写回黑板的 patch + 要发出去的消息）。

节点不直接调用别的节点，也不关心自己在图里的位置——顺序由 ``Graph`` 决定。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .bus import MessageBus
from .message import Message
from .state import WorkflowState


@dataclass
class NodeContext:
    """节点运行时能拿到的一切。"""

    run_id: str
    node: str
    iteration: int
    state: WorkflowState
    bus: MessageBus
    workspace: Path
    services: dict[str, Any] = field(default_factory=dict)
    emit: Callable[[str, str], None] = lambda level, text: None

    def service(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    @property
    def inbox(self) -> list[Message]:
        return self.bus.inbox(self.node)


@dataclass
class NodeResult:
    """节点产出：要合并进黑板的键值 + 要投递给下游的消息。"""

    patch: dict[str, Any] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    note: str = ""
    status: str = "ok"

    def with_message(self, message: Message) -> "NodeResult":
        self.messages.append(message)
        return self


class Node(ABC):
    """节点基类。子类只需要实现 ``run()``。"""

    kind: str = "node"

    def __init__(
        self,
        name: str,
        description: str = "",
        retries: int = 1,
        tags: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.description = description
        self.retries = retries
        self.tags = tags

    @abstractmethod
    def run(self, ctx: NodeContext) -> NodeResult:
        """执行节点逻辑。抛异常视为失败，引擎会按 ``retries`` 重试。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} {self.name}>"


class FunctionNode(Node):
    """用普通函数拼出来的节点，适合做 glue / 校验 / 自定义扩展。

    ``func`` 可以返回 ``NodeResult``，也可以只返回一个 patch dict。
    """

    kind = "function"

    def __init__(
        self,
        name: str,
        func: Callable[[NodeContext], NodeResult | dict[str, Any]],
        description: str = "",
        retries: int = 1,
        tags: tuple[str, ...] = (),
    ) -> None:
        super().__init__(name, description, retries, tags)
        self.func = func

    def run(self, ctx: NodeContext) -> NodeResult:
        out = self.func(ctx)
        if isinstance(out, NodeResult):
            return out
        return NodeResult(patch=dict(out or {}))


def timed(step: Callable[[], NodeResult]) -> tuple[NodeResult, int]:
    """跑一次节点并返回 (结果, 耗时毫秒)。"""
    started = time.perf_counter()
    result = step()
    return result, int((time.perf_counter() - started) * 1000)
