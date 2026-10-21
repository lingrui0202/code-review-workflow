"""确定性修复器：改得对、改不坏、改一次就够。"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.review.finding import count_by_severity
from workflow_mvp.review.fixes import apply_fixes, apply_model_patch, fixable
from workflow_mvp.review.rules import analyze

SOURCE = '''import os


def total(items=[]):
    value = 0
    for item in items:
        if item == None:
            continue
        value += item
    return value
'''


def parsed(source: str) -> bool:
    try:
        ast.parse(source)
        return True
    except SyntaxError:
        return False


class FixerTest(unittest.TestCase):
    def test_bare_except(self) -> None:
        source = "def f():\n    try:\n        pass\n    except:\n        pass\n"
        outcome = apply_fixes(source, analyze(source))
        self.assertIn("except Exception:", outcome.source)
        self.assertTrue(parsed(outcome.source))
        self.assertEqual([r.rule for r in outcome.applied], ["bare-except"])

    def test_eq_none(self) -> None:
        source = "def f(x):\n    return x == None\n"
        outcome = apply_fixes(source, analyze(source))
        self.assertIn("x is None", outcome.source)
        self.assertNotIn("== None", outcome.source)

    def test_unused_import_removed(self) -> None:
        outcome = apply_fixes(SOURCE, analyze(SOURCE))
        self.assertNotIn("import os", outcome.source.splitlines()[0])
        self.assertTrue(parsed(outcome.source))

    def test_unused_import_partial(self) -> None:
        source = "import os, json\n\nprint(json.dumps({}))\n"
        outcome = apply_fixes(source, analyze(source))
        self.assertIn("import json", outcome.source)
        self.assertNotIn("os", outcome.source)

    def test_mutable_default(self) -> None:
        outcome = apply_fixes(SOURCE, analyze(SOURCE))
        self.assertIn("def total(items=None):", outcome.source)
        self.assertIn("if items is None:", outcome.source)
        self.assertIn("items = []", outcome.source)
        self.assertTrue(parsed(outcome.source))

    def test_mutable_default_keeps_docstring_first(self) -> None:
        source = 'def f(items=[]):\n    """说明。"""\n    return len(items)\n'
        outcome = apply_fixes(source, analyze(source))
        tree = ast.parse(outcome.source)
        fn = tree.body[0]
        self.assertIsInstance(fn.body[0], ast.Expr)  # docstring 仍在第一句
        self.assertTrue(parsed(outcome.source))

    def test_composite_source_is_improved(self) -> None:
        before = analyze(SOURCE)
        outcome = apply_fixes(SOURCE, before)
        after = analyze(outcome.source)
        self.assertGreater(len(before), len(after))
        self.assertEqual(len(outcome.applied), 3)
        self.assertEqual(count_by_severity(after)["high"], 0)

    def test_idempotent(self) -> None:
        first = apply_fixes(SOURCE, analyze(SOURCE))
        second = apply_fixes(first.source, analyze(first.source))
        self.assertFalse(second.changed)
        self.assertEqual(second.applied, [])

    def test_skips_rule_without_fixer(self) -> None:
        source = "def f():\n    return 1\n"
        outcome = apply_fixes(source, analyze(source))
        self.assertFalse(outcome.changed)

    def test_fixable_filter(self) -> None:
        findings = analyze(SOURCE)
        self.assertTrue(all(f.rule in {"bare-except", "eq-none", "unused-import", "mutable-default"} for f in fixable(findings)))

    def test_model_patch_rejects_empty(self) -> None:
        source, records = apply_model_patch(SOURCE, "   ")
        self.assertEqual(source, SOURCE)
        self.assertEqual(records[0].status, "rejected")

    def test_model_patch_rejects_syntax_error(self) -> None:
        source, records = apply_model_patch(SOURCE, "def broken(:\n")
        self.assertEqual(source, SOURCE)
        self.assertEqual(records[0].status, "rejected")

    def test_model_patch_rejects_more_high_risk(self) -> None:
        worse = SOURCE + "\ndef g(x=[]):\n    return x\n"
        source, records = apply_model_patch(SOURCE, worse)
        self.assertEqual(source, SOURCE)
        self.assertEqual(records[0].status, "rejected")

    def test_model_patch_accepts_improvement(self) -> None:
        better = '''"""改进版。"""


def total(items=None):
    """合计。"""
    items = items or []
    return sum(item for item in items if item is not None)
'''
        source, records = apply_model_patch(SOURCE, better)
        self.assertIn("改进版", source)
        self.assertEqual(records[0].status, "applied")

    def test_records_are_serializable(self) -> None:
        outcome = apply_fixes(SOURCE, analyze(SOURCE))
        payload = outcome.to_dict()
        self.assertEqual(payload["applied"], 3)
        self.assertTrue(payload["records"])
        for record in payload["records"]:
            self.assertIn("status", record)


if __name__ == "__main__":
    unittest.main()
