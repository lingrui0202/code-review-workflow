"""端到端：离线跑完整工作流，验证"发现 → 修复 → 复检"闭环。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.cli import main
from workflow_mvp.config import Settings
from workflow_mvp.llm.offline import OfflineProvider
from workflow_mvp.pipelines import build_review_workflow, initial_state
from workflow_mvp.report import ReportContext, write_artifacts
from workflow_mvp.workflow.engine import EngineConfig, WorkflowEngine
from workflow_mvp.workspace import Workspace

BAD_CODE = '''import os


def total(items=[]):
    value = 0
    for item in items:
        if item == None:
            continue
        value += item
    return value


def risky(name):
    return eval(name)
'''


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = Workspace(self.root)
        self.settings = Settings(offline=True, workspace=self.root, write_back=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_workflow(self, code: str | None = None, code_path: str | None = None):
        graph = build_review_workflow(OfflineProvider(), self.workspace)
        state = initial_state(
            task="写一个订单计价模块",
            code=code,
            code_path=code_path,
            workspace=self.root,
            max_rounds=3,
        )
        engine = WorkflowEngine(
            graph,
            state,
            config=EngineConfig(run_root=self.root / "runs"),
            services={"settings": self.settings},
        )
        return engine.run(), graph

    def test_generated_draft_flow(self) -> None:
        result, graph = self.run_workflow()
        self.assertEqual(result.status, "completed")
        self.assertEqual([step.node for step in result.steps], ["coder", "reviewer", "fixer", "reviewer", "fixer"])
        history = result.state.get("findings_history")
        self.assertGreater(len(history), 1)
        self.assertLess(history[-1]["total"], history[0]["total"])
        self.assertGreaterEqual(history[0]["low"], history[-1]["low"])
        # 初稿真的落盘了
        draft = self.root / "output" / "draft.py"
        self.assertTrue(draft.exists())
        self.assertEqual(draft.read_text(encoding="utf-8"), result.state.get("code"))

    def test_existing_code_skips_coder(self) -> None:
        target = self.root / "bad_module.py"
        target.write_text(BAD_CODE, encoding="utf-8")
        result, _ = self.run_workflow(code=BAD_CODE, code_path="bad_module.py")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps[0].node, "coder")
        self.assertIn("跳过", result.steps[0].note)
        self.assertEqual(result.state.get("code_origin"), "外部提供")
        # 写回原文件，并留下备份
        fixed = target.read_text(encoding="utf-8")
        self.assertIn("def total(items=None):", fixed)
        self.assertNotEqual(fixed, BAD_CODE)
        self.assertTrue((self.root / "bad_module.py.bak").exists())

    def test_report_and_artifacts(self) -> None:
        result, graph = self.run_workflow()
        written = write_artifacts(
            ReportContext(result=result, graph=graph, settings=self.settings, workspace=self.workspace),
            result.run_dir,
        )
        for key in ("report", "trace", "state", "messages", "review", "diff", "final_code"):
            self.assertIn(key, written)
            self.assertTrue(written[key].exists())
        report = written["report"].read_text(encoding="utf-8")
        self.assertIn("代码审查与修复工作流", report)
        self.assertIn("Agent 协作记录", report)
        self.assertIn("自动修复明细", report)
        self.assertIn("mermaid", report)

        trace = json.loads(written["trace"].read_text(encoding="utf-8"))
        self.assertEqual(trace["status"], "completed")
        self.assertTrue(trace["steps"])
        messages = json.loads(written["messages"].read_text(encoding="utf-8"))
        self.assertTrue(any(m["kind"] == "feedback" for m in messages))

    def test_checkpoint_is_resumable(self) -> None:
        result, graph = self.run_workflow()
        checkpoint = result.run_dir / "checkpoint.json"
        self.assertTrue(checkpoint.exists())
        data = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertIsNone(data["next_node"])  # 正常结束
        self.assertIn("bus", data)
        self.assertEqual(data["version"], 1)

    def test_no_write_keeps_original(self) -> None:
        target = self.root / "keep.py"
        target.write_text(BAD_CODE, encoding="utf-8")
        self.settings.write_back = False
        self.run_workflow(code=BAD_CODE, code_path="keep.py")
        self.assertEqual(target.read_text(encoding="utf-8"), BAD_CODE)
        self.assertTrue((self.root / "keep.fixed.py").exists())

    def test_cli_run_offline(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["run", "--offline", "--workspace", str(self.root), "--quiet"])
        output = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("运行结果", output)
        reports = list((self.root / "runs").glob("*/report.md"))
        self.assertEqual(len(reports), 1)

    def test_cli_requires_input_when_online(self) -> None:
        with self.assertRaises(SystemExit):
            main(["run", "--workspace", str(self.root)])


if __name__ == "__main__":
    unittest.main()
