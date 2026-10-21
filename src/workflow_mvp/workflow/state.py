"""工作流共享状态（Blackboard）。

节点之间不直接互相调用，而是读写同一块黑板：上游把结果写进来，下游按 key 读。
这样做的好处是每个节点只依赖"数据契约"，不依赖"谁在我前面跑"，因此调整
节点顺序、加节点、把某个节点换成项目 1 的 Agent，都不需要改其它节点的代码。

状态必须是可 JSON 序列化的：引擎每一步都会打快照（checkpoint），失败时可以
回滚到该步之前，也能从任意一步恢复继续跑。
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

# 状态里那些"一眼就能看出内容"的 key，报告里会摘出来展示。
HEADLINE_KEYS = ("code_path", "code", "findings", "iterations", "verdict")


class StateError(RuntimeError):
    """路径不合法、状态不可序列化等。"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class StateEvent:
    """一次状态写入的审计记录。谁在什么时候改了哪个 key，改成了什么形状。"""

    seq: int
    node: str
    action: str
    key: str
    summary: str
    at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "node": self.node,
            "action": self.action,
            "key": self.key,
            "summary": self.summary,
            "at": self.at,
        }


def _summarize(value: Any, limit: int = 80) -> str:
    """把任意值压成一行短摘要，用于事件日志和 diff。"""
    if isinstance(value, str):
        text = value.replace("\n", "\\n")
        return text if len(text) <= limit else f"{text[:limit]}…({len(value)} 字符)"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}({len(value)} 项)"
    if isinstance(value, dict):
        return f"dict({len(value)} 个 key)"
    return repr(value)


class WorkflowState:
    """可序列化、带审计日志的黑板。

    用点号路径读写嵌套结构：``state.set("artifacts.code", "...")``。
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = copy.deepcopy(data) if data else {}
        self._events: list[StateEvent] = []
        self._node = "<init>"

    # ------------------------------------------------------------------ 上下文
    def enter_node(self, node: str) -> None:
        """告诉状态"接下来这些写入都来自哪个节点"，只影响审计日志。"""
        self._node = node

    @property
    def current_node(self) -> str:
        return self._node

    # ------------------------------------------------------------------ 读写
    def get(self, path: str, default: Any = None) -> Any:
        cursor: Any = self._data
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        if not all(parts):
            raise StateError(f"非法状态路径：{path!r}")
        cursor = self._data
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value
        self._record("set", path, value)

    def update(self, patch: dict[str, Any]) -> None:
        """批量写入。patch 是扁平 key（可含点号）到值的映射。"""
        for key, value in patch.items():
            self.set(key, value)

    def append(self, path: str, item: Any) -> None:
        bucket = self.get(path)
        if bucket is None:
            bucket = []
            self.set(path, bucket)
        if not isinstance(bucket, list):
            raise StateError(f"{path} 不是列表，无法 append")
        bucket = list(bucket)
        bucket.append(item)
        self.set(path, bucket)
        self._record("append", path, item)

    def bump(self, path: str, delta: int = 1) -> int:
        value = int(self.get(path, 0) or 0) + delta
        self.set(path, value)
        return value

    def __contains__(self, path: str) -> bool:
        marker = object()
        return self.get(path, marker) is not marker

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> dict[str, Any]:
        """深拷贝，用于回滚与 checkpoint。"""
        return copy.deepcopy(self._data)

    def restore(self, snapshot: dict[str, Any]) -> None:
        self._data = copy.deepcopy(snapshot)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WorkflowState":
        return cls(data or {})

    def digest(self) -> str:
        """状态内容指纹，用来判断"这一步到底改没改东西"。"""
        payload = json.dumps(self._data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    # ------------------------------------------------------------------ 审计
    def _record(self, action: str, key: str, value: Any) -> None:
        self._events.append(
            StateEvent(
                seq=len(self._events) + 1,
                node=self._node,
                action=action,
                key=key,
                summary=_summarize(value),
            )
        )

    @property
    def events(self) -> list[StateEvent]:
        return list(self._events)

    def events_since(self, seq: int) -> list[StateEvent]:
        return [event for event in self._events if event.seq > seq]

    def clear_events(self) -> None:
        self._events.clear()

    def iter_events(self) -> Iterator[StateEvent]:
        return iter(self._events)

    def diff(self, before: dict[str, Any]) -> dict[str, str]:
        """对比一份旧快照，返回 {key: "old → new"} 形式的可读差异。"""
        changed: dict[str, str] = {}
        old_keys = set(before)
        new_keys = set(self._data)
        for key in sorted(new_keys):
            old = before.get(key, "<缺失>")
            new = self._data[key]
            if key not in old_keys or json.dumps(old, sort_keys=True, default=str) != json.dumps(
                new, sort_keys=True, default=str
            ):
                changed[key] = f"{_summarize(old, 40)} → {_summarize(new, 40)}"
        for key in sorted(old_keys - new_keys):
            changed[key] = f"{_summarize(before[key], 40)} → <已删除>"
        return changed

    def headline(self) -> dict[str, str]:
        """报告摘要区用的一行式状态。"""
        out: dict[str, str] = {}
        for key in HEADLINE_KEYS:
            if key in self._data:
                out[key] = _summarize(self._data[key], 120)
        return out
