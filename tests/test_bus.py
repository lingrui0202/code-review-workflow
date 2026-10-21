"""消息与总线的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.workflow.bus import MessageBus
from workflow_mvp.workflow.message import Message, thread_of


class MessageTest(unittest.TestCase):
    def test_kind_validation(self) -> None:
        with self.assertRaises(ValueError):
            Message(sender="a", recipient="b", kind="unknown", content="")

    def test_serialization_roundtrip(self) -> None:
        message = Message.result("coder", "reviewer", "初稿完成", refs=["m1"], artifacts={"n": 3})
        restored = Message.from_dict(message.to_dict())
        self.assertEqual(restored.sender, "coder")
        self.assertEqual(restored.content, "初稿完成")
        self.assertEqual(restored.artifacts, {"n": 3})

    def test_thread_follows_causation(self) -> None:
        root = Message.task("user", "coder", "写个模块")
        child = Message.result("coder", "reviewer", "初稿好了", causation_id=root.id)
        grand = Message.feedback("reviewer", "fixer", "改这里", causation_id=child.id)
        chain = thread_of([root, child, grand], grand)
        self.assertEqual([m.id for m in chain], [root.id, child.id, grand.id])


class BusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = MessageBus(run_id="run-1")

    def test_send_delivers_to_inbox(self) -> None:
        message = self.bus.send(Message.result("coder", "reviewer", "done"))
        self.assertEqual(message.run_id, "run-1")
        self.assertEqual([m.id for m in self.bus.inbox("reviewer")], [message.id])
        self.assertEqual(self.bus.inbox("fixer"), [])

    def test_latest_returns_last(self) -> None:
        self.bus.send(Message.result("coder", "reviewer", "第一版"))
        second = self.bus.send(Message.result("coder", "reviewer", "第二版"))
        self.assertEqual(self.bus.latest("reviewer").id, second.id)

    def test_broadcast_goes_to_star(self) -> None:
        message = self.bus.broadcast("reviewer", "通过")
        self.assertEqual(message.recipient, "*")
        self.assertEqual(len(self.bus.inbox("*")), 1)

    def test_stats(self) -> None:
        self.bus.send(Message.result("coder", "reviewer", "a"))
        self.bus.send(Message.feedback("reviewer", "fixer", "b"))
        stats = self.bus.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_kind"]["result"], 1)
        self.assertEqual(stats["by_pair"]["coder→reviewer"], 1)

    def test_drain_clears_inbox(self) -> None:
        self.bus.send(Message.result("coder", "reviewer", "a"))
        self.assertEqual(len(self.bus.drain("reviewer")), 1)
        self.assertEqual(self.bus.inbox("reviewer"), [])

    def test_serialization_roundtrip(self) -> None:
        self.bus.send(Message.result("coder", "reviewer", "a"))
        restored = MessageBus.from_dict(self.bus.to_dict())
        self.assertEqual(len(restored.history), 1)
        self.assertEqual(restored.run_id, "run-1")

    def test_history_is_copy(self) -> None:
        self.bus.send(Message.result("coder", "reviewer", "a"))
        history = self.bus.history
        history.clear()
        self.assertEqual(len(self.bus.history), 1)


if __name__ == "__main__":
    unittest.main()
