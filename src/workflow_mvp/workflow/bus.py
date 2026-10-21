"""消息总线：Agent 之间唯一的通信通道。

引擎在节点跑完之后统一调用 ``publish()``，由总线按 ``recipient`` 投递到收件箱。
节点开工时从自己的收件箱取信，读到的就是"上一棒交给我的东西"。

总线只做三件事：投递、留档、统计。不做重试、不做顺序保证——工作流本身就是
顺序执行的，这里保持简单，反而让"消息去哪儿了"一目了然。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .message import Message


class MessageBus:
    """进程内消息总线（可序列化，因此 checkpoint 能连消息一起存）。"""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self._history: list[Message] = []
        self._inboxes: dict[str, list[Message]] = {}

    # ------------------------------------------------------------------ 投递
    def send(self, message: Message) -> Message:
        message.run_id = message.run_id or self.run_id
        self._history.append(message)
        self._inboxes.setdefault(message.recipient, []).append(message)
        return message

    def publish(self, messages: Iterable[Message]) -> list[Message]:
        return [self.send(message) for message in messages]

    def broadcast(self, sender: str, content: str, kind: str = "verdict", **kw: Any) -> Message:
        return self.send(Message(sender=sender, recipient="*", kind=kind, content=content, **kw))

    # ------------------------------------------------------------------ 收件箱
    def inbox(self, agent: str) -> list[Message]:
        return list(self._inboxes.get(agent, []))

    def unread(self, agent: str) -> list[Message]:
        """最近一次投递、且还没被该 Agent 读过的消息。"""
        messages = self.inbox(agent)
        return [m for m in messages if not getattr(m, "_read", False)]

    def latest(self, agent: str, kind: str | None = None) -> Message | None:
        for message in reversed(self.inbox(agent)):
            if kind is None or message.kind == kind:
                return message
        return None

    def drain(self, agent: str) -> list[Message]:
        """取走收件箱（读操作，表示"这轮我已经处理了"）。"""
        return self._inboxes.pop(agent, [])

    def clear(self) -> None:
        self._history.clear()
        self._inboxes.clear()

    # ------------------------------------------------------------------ 观测
    @property
    def history(self) -> list[Message]:
        return list(self._history)

    def transcript(self) -> list[dict[str, Any]]:
        return [message.to_dict() for message in self._history]

    def stats(self) -> dict[str, Any]:
        kinds = Counter(message.kind for message in self._history)
        pairs = Counter(f"{message.sender}→{message.recipient}" for message in self._history)
        return {
            "total": len(self._history),
            "by_kind": dict(kinds),
            "by_pair": dict(pairs),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "history": self.transcript(),
            "inboxes": {
                who: [message.to_dict() for message in messages]
                for who, messages in self._inboxes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MessageBus":
        bus = cls(run_id=data.get("run_id", ""))
        for raw in data.get("history", []):
            bus._history.append(Message.from_dict(raw))
        for who, messages in data.get("inboxes", {}).items():
            bus._inboxes[who] = [Message.from_dict(raw) for raw in messages]
        return bus
