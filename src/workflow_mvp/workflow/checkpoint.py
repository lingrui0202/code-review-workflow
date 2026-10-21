"""Checkpoint：把一次运行冻结到磁盘，之后能接着跑。

为什么值得单独做：LLM 调用失败、人中途关窗口、修复把文件改坏了——这三种情况
在真实使用里都会发生。没有 checkpoint 就只能从头再烧一遍 token / 再跑一遍工具。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bus import MessageBus
from .state import WorkflowState

CHECKPOINT_VERSION = 1
FILENAME = "checkpoint.json"


def save(
    path: Path,
    *,
    run_id: str,
    graph_name: str,
    state: WorkflowState,
    bus: MessageBus,
    steps: list[dict[str, Any]],
    next_node: str | None,
    status: str = "running",
) -> Path:
    """写一份 checkpoint。每一步之后都会调用，因此文件始终是"最新可续跑点"。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CHECKPOINT_VERSION,
        "run_id": run_id,
        "graph": graph_name,
        "status": status,
        "next_node": next_node,
        "state": state.to_dict(),
        "bus": bus.to_dict(),
        "steps": steps,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 原子替换，避免写到一半断电留下半个文件
    return path


def load(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = data.get("version")
    if version != CHECKPOINT_VERSION:
        raise ValueError(f"checkpoint 版本不支持：{version}（期望 {CHECKPOINT_VERSION}）")
    for key in ("state", "bus"):
        if key not in data:
            raise ValueError(f"checkpoint 缺少字段 {key!r}")
    return data


def restore_state(data: dict[str, Any]) -> WorkflowState:
    return WorkflowState.from_dict(data.get("state", {}))


def restore_bus(data: dict[str, Any]) -> MessageBus:
    return MessageBus.from_dict(data.get("bus", {}))
