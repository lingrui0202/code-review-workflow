"""审查结论的数据结构。

``Finding`` 尽量小、且完全可序列化：它既要在 Agent 之间传递（消息体），
也要在报告的"问题清单"表格里逐行展示，还要能进 checkpoint。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}


@dataclass(frozen=True)
class Rule:
    """一条审查规则的元信息。"""

    id: str
    title: str
    severity: str
    fixable: bool
    rationale: str
    how: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "fixable": self.fixable,
            "rationale": self.rationale,
            "how": self.how,
        }


@dataclass
class Finding:
    """一条问题。``line`` 是 1-based 行号，``symbol`` 是所属函数/变量名。"""

    rule: str
    severity: str
    line: int
    message: str
    symbol: str = ""
    suggestion: str = ""
    fixable: bool = False
    source: str = "static"  # static=规则扫描，llm=模型补充
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"未知严重度：{self.severity}")

    @property
    def key(self) -> str:
        return f"{self.rule}@{self.line}:{self.symbol}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "message": self.message,
            "symbol": self.symbol,
            "suggestion": self.suggestion,
            "fixable": self.fixable,
            "source": self.source,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        return cls(
            rule=data["rule"],
            severity=data["severity"],
            line=int(data.get("line", 1)),
            message=data.get("message", ""),
            symbol=data.get("symbol", ""),
            suggestion=data.get("suggestion", ""),
            fixable=bool(data.get("fixable", False)),
            source=data.get("source", "static"),
            extra=dict(data.get("extra", {})),
        )

    def one_line(self) -> str:
        where = f"L{self.line}" + (f" {self.symbol}()" if self.symbol else "")
        return f"[{SEVERITY_LABEL[self.severity]}] {self.rule} {where}：{self.message}"


def from_dicts(items: Iterable[dict[str, Any]]) -> list[Finding]:
    return [Finding.from_dict(item) for item in items]


def to_dicts(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    return [finding.to_dict() for finding in findings]


def count_by_severity(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def worst_severity(findings: Iterable[Finding]) -> str:
    """最严重的那一档；没有问题返回 ``none``。"""
    worst = "none"
    for finding in findings:
        if SEVERITY_ORDER[finding.severity] > SEVERITY_ORDER.get(worst, 0):
            worst = finding.severity
    return worst


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda f: (-SEVERITY_ORDER[f.severity], f.line, f.rule, f.symbol),
    )


def rule_histogram(findings: Iterable[Finding]) -> dict[str, int]:
    histogram: dict[str, int] = {}
    for finding in findings:
        histogram[finding.rule] = histogram.get(finding.rule, 0) + 1
    return dict(sorted(histogram.items(), key=lambda pair: (-pair[1], pair[0])))


def fingerprint(findings: Iterable[Finding]) -> str:
    """一组问题的指纹，用于判断"这一轮有没有实质变化"。"""
    import hashlib

    payload = "|".join(sorted(f"{f.rule}:{f.line}:{f.symbol}" for f in findings))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
