"""状态黑板的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.workflow.state import StateError, WorkflowState


class StateTest(unittest.TestCase):
    def test_dotted_path_roundtrip(self) -> None:
        state = WorkflowState()
        state.set("artifacts.code", "print(1)")
        state.set("artifacts.meta.lang", "python")
        self.assertEqual(state.get("artifacts.code"), "print(1)")
        self.assertEqual(state.get("artifacts.meta.lang"), "python")
        self.assertEqual(state.get("missing.key", "默认"), "默认")

    def test_append_and_bump(self) -> None:
        state = WorkflowState()
        state.append("findings", {"rule": "bare-except"})
        state.append("findings", {"rule": "eq-none"})
        self.assertEqual(len(state.get("findings")), 2)
        self.assertEqual(state.bump("iterations"), 1)
        self.assertEqual(state.bump("iterations"), 2)

    def test_invalid_path_rejected(self) -> None:
        state = WorkflowState()
        with self.assertRaises(StateError):
            state.set("a..b", 1)

    def test_append_to_scalar_rejected(self) -> None:
        state = WorkflowState()
        state.set("iterations", 3)
        with self.assertRaises(StateError):
            state.append("iterations", 1)

    def test_snapshot_is_isolated(self) -> None:
        state = WorkflowState({"items": [1, 2]})
        snapshot = state.snapshot()
        state.append("items", 3)
        self.assertEqual(snapshot["items"], [1, 2])
        self.assertEqual(state.get("items"), [1, 2, 3])

    def test_restore_rolls_back(self) -> None:
        state = WorkflowState({"code": "v1"})
        snapshot = state.snapshot()
        state.set("code", "v2")
        state.restore(snapshot)
        self.assertEqual(state.get("code"), "v1")

    def test_digest_depends_on_content(self) -> None:
        one, two = WorkflowState({"a": 1}), WorkflowState({"a": 2})
        self.assertNotEqual(one.digest(), two.digest())
        self.assertEqual(one.digest(), WorkflowState({"a": 1}).digest())

    def test_diff_reports_changes(self) -> None:
        state = WorkflowState({"a": 1, "b": "old"})
        before = state.snapshot()
        state.set("b", "new")
        state.set("c", True)
        diff = state.diff(before)
        self.assertIn("b", diff)
        self.assertIn("c", diff)
        self.assertNotIn("a", diff)

    def test_events_are_attributed_to_node(self) -> None:
        state = WorkflowState()
        state.enter_node("reviewer")
        state.set("verdict", "needs_fix")
        events = state.events
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].node, "reviewer")
        self.assertEqual(events[0].key, "verdict")

    def test_serialization_roundtrip(self) -> None:
        state = WorkflowState({"code": "x = 1", "findings": [{"rule": "a"}]})
        restored = WorkflowState.from_dict(state.to_dict())
        self.assertEqual(restored.get("code"), "x = 1")
        self.assertEqual(restored.get("findings"), [{"rule": "a"}])

    def test_contains(self) -> None:
        state = WorkflowState({"code": ""})
        self.assertIn("code", state)
        self.assertNotIn("missing", state)


if __name__ == "__main__":
    unittest.main()
