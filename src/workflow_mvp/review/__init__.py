"""审查子系统：规则扫描 + 确定性修复。"""

from .finding import (
    SEVERITY_LABEL,
    SEVERITY_ORDER,
    Finding,
    Rule,
    count_by_severity,
    fingerprint,
    from_dicts,
    rule_histogram,
    sort_findings,
    to_dicts,
    worst_severity,
)
from .fixes import FIXERS, FixOutcome, FixRecord, apply_fixes, apply_model_patch, fixable
from .rules import RULES, RULE_TABLE, analyze, analyze_file

__all__ = [
    "SEVERITY_LABEL",
    "SEVERITY_ORDER",
    "Finding",
    "Rule",
    "count_by_severity",
    "fingerprint",
    "from_dicts",
    "rule_histogram",
    "sort_findings",
    "to_dicts",
    "worst_severity",
    "FIXERS",
    "FixOutcome",
    "FixRecord",
    "apply_fixes",
    "apply_model_patch",
    "fixable",
    "RULES",
    "RULE_TABLE",
    "analyze",
    "analyze_file",
]
