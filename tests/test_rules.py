"""审查规则：每条规则都要有正例，同时不能对干净代码误报。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.review.finding import count_by_severity, rule_histogram, sort_findings, worst_severity
from workflow_mvp.review.rules import RULES, analyze

CLEAN = '''"""一个干净的小模块。"""

import json


def load(path: str) -> dict:
    """读取 JSON 文件。"""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Store:
    """内存存储。"""

    def __init__(self) -> None:
        self.items: dict = {}

    def put(self, key: str, value: object) -> None:
        """写入一项。"""
        self.items[key] = value
'''


def rules_of(source: str) -> list[str]:
    return [finding.rule for finding in analyze(source)]


class RuleTest(unittest.TestCase):
    def test_clean_code_has_no_findings(self) -> None:
        self.assertEqual(rules_of(CLEAN), [])

    def test_syntax_error(self) -> None:
        findings = analyze("def broken(:\n    pass\n")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "syntax-error")
        self.assertEqual(findings[0].severity, "high")

    def test_bare_except(self) -> None:
        self.assertIn("bare-except", rules_of("def f():\n    try:\n        pass\n    except:\n        pass\n"))

    def test_swallowed_exception(self) -> None:
        source = "def f():\n    try:\n        pass\n    except Exception:\n        pass\n"
        self.assertIn("swallowed-exception", rules_of(source))

    def test_mutable_default(self) -> None:
        self.assertIn("mutable-default", rules_of("def f(items=[]):\n    return items\n"))
        self.assertIn("mutable-default", rules_of("def g(cache={}):\n    return cache\n"))
        self.assertNotIn("mutable-default", rules_of("def h(items=None):\n    return items or []\n"))

    def test_dangerous_call(self) -> None:
        for source in ('eval("1+1")', 'exec("x=1")', 'os.system("ls")', 'subprocess.run(["ls"], shell=True)'):
            with self.subTest(source=source):
                self.assertIn("dangerous-call", rules_of(source))

    def test_hardcoded_secret(self) -> None:
        self.assertIn("hardcoded-secret", rules_of('API_KEY = "sk-1234567890"\n'))
        self.assertNotIn("hardcoded-secret", rules_of("key_name = 'x'\n"))

    def test_eq_none(self) -> None:
        self.assertIn("eq-none", rules_of("def f(x):\n    return x == None\n"))
        self.assertIn("eq-none", rules_of("def f(x):\n    return x != None\n"))
        self.assertNotIn("eq-none", rules_of("def f(x):\n    return x is None\n"))

    def test_unused_import(self) -> None:
        self.assertIn("unused-import", rules_of("import os\n"))
        self.assertNotIn("unused-import", rules_of("import os\n\nprint(os.sep)\n"))
        # 部分使用：只有 json 被用到，os 应该被点名
        findings = [f for f in analyze("import os\nimport json\n\nprint(json.dumps({}))\n") if f.rule == "unused-import"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].symbol, "os")

    def test_missing_docstring(self) -> None:
        self.assertIn("missing-docstring", rules_of("def public():\n    return 1\n"))
        self.assertNotIn("missing-docstring", rules_of('def public():\n    """说明。"""\n    return 1\n'))
        self.assertNotIn("missing-docstring", rules_of("def _private():\n    return 1\n"))

    def test_too_many_args(self) -> None:
        self.assertIn("too-many-args", rules_of("def f(a, b, c, d, e, g):\n    return 1\n"))
        self.assertNotIn("too-many-args", rules_of("def f(a, b, c):\n    return 1\n"))

    def test_deep_nesting(self) -> None:
        source = (
            "def f(x):\n"
            "    if x:\n"
            "        if x > 1:\n"
            "            if x > 2:\n"
            "                if x > 3:\n"
            "                    if x > 4:\n"
            "                        return 1\n"
            "    return 0\n"
        )
        self.assertIn("deep-nesting", rules_of(source))

    def test_long_function(self) -> None:
        body = "\n".join(f"    total += {index}" for index in range(62))
        source = "def f():\n    total = 0\n" + body + "\n    return total\n"
        self.assertIn("long-function", rules_of(source))

    def test_line_numbers_are_positive(self) -> None:
        for finding in analyze("import os\ndef f(x=[]):\n    return x == None\n"):
            self.assertGreaterEqual(finding.line, 1)

    def test_finding_helpers(self) -> None:
        findings = analyze("import os\ndef f(x=[]):\n    return x == None\n")
        counts = count_by_severity(findings)
        self.assertGreaterEqual(counts["high"], 1)
        self.assertEqual(worst_severity(findings), "high")
        self.assertEqual(sum(rule_histogram(findings).values()), len(findings))
        ordered = sort_findings(findings)
        self.assertEqual(ordered[0].severity, "high")

    def test_rule_metadata_is_complete(self) -> None:
        for rule in RULES.values():
            self.assertTrue(rule.title)
            self.assertIn(rule.severity, ("high", "medium", "low"))
            self.assertTrue(rule.rationale)


if __name__ == "__main__":
    unittest.main()
