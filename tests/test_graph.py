"""图结构（节点、条件边、校验）的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.workflow.graph import Graph, GraphError
from workflow_mvp.workflow.node import FunctionNode
from workflow_mvp.workflow.state import WorkflowState


def node(name: str) -> FunctionNode:
    return FunctionNode(name, lambda ctx: {})


class GraphTest(unittest.TestCase):
    def test_linear_chain(self) -> None:
        graph = Graph("linear")
        graph.add_node(node("a"), start=True).add_node(node("b")).add_node(node("c"))
        graph.chain("a", "b", "c").validate()
        self.assertEqual(graph.start, "a")
        self.assertTrue(graph.is_terminal("c"))
        self.assertEqual([t for t in graph.resolve(WorkflowState(), "a")], ["b"])

    def test_duplicate_node_rejected(self) -> None:
        graph = Graph()
        graph.add_node(node("a"))
        with self.assertRaises(GraphError):
            graph.add_node(node("a"))

    def test_dangling_edge_rejected(self) -> None:
        graph = Graph()
        graph.add_node(node("a"), start=True)
        graph.add_edge("a", "ghost")
        with self.assertRaises(GraphError):
            graph.validate()

    def test_unreachable_node_rejected(self) -> None:
        graph = Graph()
        graph.add_node(node("a"), start=True)
        graph.add_node(node("b"))
        with self.assertRaises(GraphError):
            graph.validate()

    def test_multiple_unconditional_edges_rejected(self) -> None:
        graph = Graph()
        for name in ("a", "b", "c"):
            graph.add_node(node(name), start=(name == "a"))
        graph.add_edge("a", "b").add_edge("a", "c")
        with self.assertRaises(GraphError):
            graph.validate()

    def test_conditional_edges_are_exclusive(self) -> None:
        graph = Graph()
        for name in ("a", "b", "c"):
            graph.add_node(node(name), start=(name == "a"))
        graph.add_edge("a", "b", when=lambda s: s.get("ok") is True, label="成功")
        graph.add_edge("a", "c", when=lambda s: not s.get("ok"), label="失败")
        graph.validate()
        self.assertEqual(graph.resolve(WorkflowState({"ok": True}), "a"), ["b"])
        self.assertEqual(graph.resolve(WorkflowState({"ok": False}), "a"), ["c"])

    def test_condition_exception_is_treated_as_false(self) -> None:
        graph = Graph()
        for name in ("a", "b"):
            graph.add_node(node(name), start=(name == "a"))

        def boom(state):
            raise RuntimeError("条件写错了")

        graph.add_edge("a", "b", when=boom)
        self.assertEqual(graph.resolve(WorkflowState(), "a"), [])

    def test_cycle_detection(self) -> None:
        graph = Graph()
        for name in ("a", "b"):
            graph.add_node(node(name), start=(name == "a"))
        graph.add_edge("a", "b").add_edge("b", "a")
        self.assertTrue(graph.has_cycle())

    def test_ascii_and_mermaid(self) -> None:
        graph = Graph("demo")
        graph.add_node(node("coder"), start=True).add_node(node("reviewer"))
        graph.add_edge("coder", "reviewer", label="交付")
        text = graph.to_ascii()
        self.assertIn("coder", text)
        self.assertIn("交付", text)
        mermaid = graph.to_mermaid()
        self.assertTrue(mermaid.startswith("graph TD"))
        self.assertIn("-->|交付|", mermaid)

    def test_describe(self) -> None:
        graph = Graph("demo", "说明")
        graph.add_node(node("a"), start=True)
        info = graph.describe()
        self.assertEqual(info["start"], "a")
        self.assertEqual(info["nodes"][0]["name"], "a")
        self.assertFalse(info["has_cycle"])


if __name__ == "__main__":
    unittest.main()
