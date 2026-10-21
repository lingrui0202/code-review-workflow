"""Agent 之间的消息信封。

没有用"节点函数互相传参"，也没有用全局变量。所有交接都落成一条显式的
``Message``：谁发给谁、什么类型、对应哪次迭代、引用了哪条上游消息。

这样做有三个直接好处：

1. **可审计**：一次运行的全部协作过程就是一条消息时间线，报告里能原样贴出来；
2. **可回放**：``causation_id`` 让"修复建议是因为哪条审查结论产生的"可以追到根上；
3. **可替换**：跨进程/跨机器的 Agent 只要按同样的信封收发，就能接进同一个工作流。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

# 消息类型：交接(task) / 结果(result) / 反馈(feedback) / 结论(verdict) / 系统(sys)
KINDS = ("task", "result", "feedback", "verdict", "sys")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Message:
    """一条 Agent 间消息。

    ``recipient="*"`` 表示广播（例如最终结论给整个流程看）。
    """

    sender: str
    recipient: str
    kind: str
    content: str
    refs: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    iteration: int = 0  # 0 表示"未指定"，由引擎按当前轮次补上
    causation_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"未知消息类型 {self.kind!r}，可选 {KINDS}")

    # ------------------------------------------------------------------ 构造
    @classmethod
    def task(cls, sender: str, recipient: str, content: str, **kw: Any) -> "Message":
        return cls(sender=sender, recipient=recipient, kind="task", content=content, **kw)

    @classmethod
    def result(cls, sender: str, recipient: str, content: str, **kw: Any) -> "Message":
        return cls(sender=sender, recipient=recipient, kind="result", content=content, **kw)

    @classmethod
    def feedback(cls, sender: str, recipient: str, content: str, **kw: Any) -> "Message":
        return cls(sender=sender, recipient=recipient, kind="feedback", content=content, **kw)

    @classmethod
    def verdict(cls, sender: str, recipient: str, content: str, **kw: Any) -> "Message":
        return cls(sender=sender, recipient=recipient, kind="verdict", content=content, **kw)

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "iteration": self.iteration,
            "at": self.at,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "content": self.content,
            "refs": list(self.refs),
            "artifacts": self.artifacts,
            "causation_id": self.causation_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            sender=data["sender"],
            recipient=data["recipient"],
            kind=data["kind"],
            content=data.get("content", ""),
            refs=list(data.get("refs", [])),
            artifacts=dict(data.get("artifacts", {})),
            run_id=data.get("run_id", ""),
            iteration=int(data.get("iteration", 0) or 0),
            causation_id=data.get("causation_id"),
            id=data.get("id", uuid.uuid4().hex[:12]),
            at=data.get("at", _now()),
        )

    def brief(self, limit: int = 60) -> str:
        text = self.content.replace("\n", " ").strip()
        if len(text) > limit:
            text = text[:limit] + "…"
        return f"[{self.kind}] {self.sender} → {self.recipient}：{text}"


def thread_of(messages: Iterable[Message], root: Message) -> list[Message]:
    """按 causation_id 把一条消息的因果链收集出来（含自身）。"""
    pool = list(messages)
    chain = [root]
    seen = {root.id}
    cursor = root
    while cursor.causation_id:
        parent = next((m for m in pool if m.id == cursor.causation_id), None)
        if parent is None or parent.id in seen:
            break
        chain.append(parent)
        seen.add(parent.id)
        cursor = parent
    return list(reversed(chain))
