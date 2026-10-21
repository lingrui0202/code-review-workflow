"""执行引擎：顺序推进、分支、循环保护、失败回滚、续跑。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.workflow import checkpoint as checkpoint_io
from workflow_mvp.workflow.engine import EngineConfig, WorkflowEngine
from workflow_mvp.workflow.graph import Graph
from workflow_mvp.workflow.message import Message
from workflow_mvp.workflow.node import FunctionNode, NodeResult
from workflow_mvp.workflow.state import WorkflowState


def make_graph(*names: str) -> Graph:
    graph = Graph("test")
    for index, name in enumerate(names):
        graph.add_node(FunctionNode(name, lambda ctx: NodeResult(patch={name: True}), description=name), start=index == 0)
    graph.chain(*names)
    return graph


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------ 基本
    def test_linear_run_completes(self) -> None:
        graph = make_graph("a", "b", "c")
        engine = WorkflowEngine(graph, WorkflowState(), config=EngineConfig(run_root=self.root))
        result = engine.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual([step.node for step in result.steps], ["a", "b", "c"])
        self.assertTrue(result.state.get("c"))

    def test_step_records_state_changes(self) -> None:
        graph = make_graph("a")
        engine = WorkflowEngine(graph, WorkflowState(), config=EngineConfig(run_root=self.root))
        result = engine.run()
        step = result.steps[0]
        self.assertEqual(step.status, "ok")
        self.assertIn("a", step.changed)
        self.assertTrue(step.duration_ms >= 0)

    def test_conditional_branch(self) -> None:
        graph = Graph("branch")
        graph.add_node(FunctionNode("start", lambda ctx: NodeResult(patch={"flag": True})), start=True)
        graph.add_node(FunctionNode("left", lambda ctx: NodeResult(patch={"picked": "left"})))
        graph.add_node(FunctionNode("right", lambda ctx: NodeResult(patch={"picked": "right"})))
        graph.add_edge("start", "left", when=lambda s: s.get("flag") is True)
        graph.add_edge("start", "right", when=lambda s: s.get("flag") is not True)
        graph.validate()

        engine = WorkflowEngine(graph, WorkflowState(), config=EngineConfig(run_root=self.root))
        result = engine.run()
        self.assertEqual(result.state.get("picked"), "left")
        self.assertEqual(len(result.steps), 2)

    def test_messages_are_published(self) -> None:
        graph = Graph("msg")
        graph.add_node(
            FunctionNode(
                "sender",
                lambda ctx: NodeResult(
                    patch={"sent": True},
                    messages=[Message.result("sender", "receiver", "交接")],
                ),
            ),
            start=True,
        )
        graph.add_node(FunctionNode("receiver", lambda ctx: NodeResult(patch={"got": True})))
        graph.chain("sender", "receiver")
        graph.validate()

        engine = WorkflowEngine(graph, WorkflowState(), config=EngineConfig(run_root=self.root))
        result = engine.run()
        self.assertEqual(len(result.bus.history), 1)
        self.assertEqual(result.bus.history[0].recipient, "receiver")

    # ------------------------------------------------------------------ 失败
    def test_failure_rolls_back_and_stops(self) -> None:
        def boom(ctx) -> NodeResult:
            ctx.state.set("half_written", True)
            raise RuntimeError("接口挂了")

        graph = Graph("fail")
        graph.add_node(FunctionNode("ok", lambda ctx: NodeResult(patch={"ok": True})), start=True)
        graph.add_node(FunctionNode("boom", boom, retries=0))
        graph.chain("ok", "boom")
        graph.validate()

        engine = WorkflowEngine(graph, WorkflowState(), config=EngineConfig(run_root=self.root))
        result = engine.run()
        self.assertEqual(result.status, "failed")
        self.assertIn("接口挂了", result.steps[-1].error)
        self.assertNotIn("half_written", result.state)  # 半截状态被回滚

    def test_retry_then_success(self) -> None:
        attempts = {"n": 0}

        def flaky(ctx) -> NodeResult:
            attempts["n"] += 1
            ctx.state.set("dirty", attempts["n"])
            if attempts["n"] == 1:
                raise RuntimeError("第一次失败")
            return NodeResult(patch={"done": True})

        graph = Graph("retry")
        graph.add_node(FunctionNode("flaky", flaky, retries=1), start=True)
        graph.validate()

        engine = WorkflowEngine(graph, WorkflowState(), config=EngineConfig(run_root=self.root))
        result = engine.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(result.steps[0].status, "ok")

    # ------------------------------------------------------------------ 保护
    def test_loop_guard(self) -> None:
        graph = Graph("loop")
        graph.add_node(FunctionNode("a", lambda ctx: NodeResult()), start=True)
        graph.add_node(FunctionNode("b", lambda ctx: NodeResult()))
        graph.add_edge("a", "b").add_edge("b", "a")
        graph.validate()

        config = EngineConfig(max_visits=2, run_root=self.root)
        engine = WorkflowEngine(graph, WorkflowState(), config=config)
        result = engine.run()
        self.assertEqual(result.status, "aborted")
        self.assertIn("死循环", result.stop_reason)

    def test_max_steps_guard(self) -> None:
        graph = Graph("loop")
        graph.add_node(FunctionNode("a", lambda ctx: NodeResult()), start=True)
        graph.add_node(FunctionNode("b", lambda ctx: NodeResult()))
        graph.add_edge("a", "b").add_edge("b", "a")
        graph.validate()

        engine = WorkflowEngine(graph, WorkflowState(), config=EngineConfig(max_steps=3, run_root=self.root))
        result = engine.run()
        self.assertEqual(result.status, "aborted")
        self.assertEqual(len(result.steps), 3)

    # ------------------------------------------------------------------ 续跑
    def test_checkpoint_and_resume(self) -> None:
        def boom(ctx) -> NodeResult:
            raise RuntimeError("暂时不可用")

        graph = Graph("resume")
        graph.add_node(FunctionNode("first", lambda ctx: NodeResult(patch={"first": True})), start=True)
        graph.add_node(FunctionNode("second", boom, retries=0))
        graph.chain("first", "second")
        graph.validate()

        config = EngineConfig(run_root=self.root)
        engine = WorkflowEngine(graph, WorkflowState(), config=config)
        result = engine.run()
        self.assertEqual(result.status, "failed")
        checkpoint_path = result.run_dir / "checkpoint.json"
        self.assertTrue(checkpoint_path.exists())

        data = checkpoint_io.load(checkpoint_path)
        self.assertEqual(data["next_node"], "second")

        # 用同一张图但修好 second，从 checkpoint 接着跑
        graph2 = Graph("resume")
        graph2.add_node(FunctionNode("first", lambda ctx: NodeResult(patch={"first": True})), start=True)
        graph2.add_node(FunctionNode("second", lambda ctx: NodeResult(patch={"second": True})))
        graph2.chain("first", "second")
        graph2.validate()

        engine2, next_node = WorkflowEngine.from_checkpoint(
            data, graph2, config=EngineConfig(run_root=self.root, checkpoint=False)
        )
        resumed = engine2.run(start=next_node)
        self.assertEqual(resumed.status, "completed")
        self.assertEqual([step.node for step in resumed.steps], ["first", "second"])
        self.assertTrue(resumed.state.get("second"))
        self.assertTrue(resumed.state.get("first"))  # 之前步骤的状态被保留

    def test_no_checkpoint_when_disabled(self) -> None:
        graph = make_graph("only")
        engine = WorkflowEngine(
            graph, WorkflowState(), config=EngineConfig(run_root=self.root, checkpoint=False)
        )
        result = engine.run()
        self.assertIsNone(result.run_dir)


if __name__ == "__main__":
    unittest.main()
