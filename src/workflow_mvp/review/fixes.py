"""确定性修复器：把"能机械改的问题"真的改掉。

设计原则是**改坏不如不改**。每一次修改都要过三道闸：

1. 改完还能被 ``ast.parse`` 解析（不能把代码改到语法错误）；
2. 目标规则的问题条数确实变少了（不能假装修了）；
3. 没有引入新的高危问题（不能修一个坏三个）。

任何一道不过就回滚这一处修改，并在修复记录里写清楚原因。离线模式下这是唯一
的修复手段；在线模式下模型改完的整份文件也要过同一套校验才会被采纳。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Callable

from .finding import Finding, count_by_severity, sort_findings
from .rules import analyze


@dataclass
class FixRecord:
    """一处修复的结果。``status``：applied / skipped / rejected。"""

    rule: str
    line: int
    symbol: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, str | int]:
        return {
            "rule": self.rule,
            "line": self.line,
            "symbol": self.symbol,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class FixOutcome:
    original: str
    source: str
    records: list[FixRecord] = field(default_factory=list)

    @property
    def applied(self) -> list[FixRecord]:
        return [record for record in self.records if record.status == "applied"]

    @property
    def skipped(self) -> list[FixRecord]:
        return [record for record in self.records if record.status != "applied"]

    @property
    def changed(self) -> bool:
        return self.source != self.original

    def summary(self) -> str:
        return f"修复 {len(self.applied)} 处，跳过 {len(self.skipped)} 处"

    def to_dict(self) -> dict[str, object]:
        return {
            "applied": len(self.applied),
            "skipped": len(self.skipped),
            "records": [record.to_dict() for record in self.records],
        }


# ---------------------------------------------------------------------- 工具


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _char_col(line_text: str, byte_col: int) -> int:
    """ast 的 col_offset 是 UTF-8 字节偏移，这里换算成字符下标。"""
    return len(line_text.encode("utf-8")[:byte_col].decode("utf-8", "replace"))


def _span(source: str, node: ast.AST) -> tuple[int, int] | None:
    if not hasattr(node, "end_lineno") or node.end_lineno is None:
        return None
    starts = _line_starts(source)
    lines = source.splitlines(keepends=True)
    try:
        start = starts[node.lineno - 1] + _char_col(lines[node.lineno - 1], node.col_offset)
        end = starts[node.end_lineno - 1] + _char_col(lines[node.end_lineno - 1], node.end_col_offset or 0)
    except IndexError:
        return None
    return start, end


def _node_at(tree: ast.AST, line: int, kinds: tuple[type, ...]) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, kinds) and getattr(node, "lineno", -1) == line:
            return node
    return None


def _function_at(tree: ast.AST, line: int, kinds: tuple[type, ...]) -> ast.AST | None:
    candidates = [n for n in ast.walk(tree) if isinstance(n, kinds) and n.lineno <= line <= (n.end_lineno or n.lineno)]
    return min(candidates, key=lambda n: (n.end_lineno or n.lineno) - n.lineno) if candidates else None


# ---------------------------------------------------------------------- 单条修复


def _fix_bare_except(source: str, finding: Finding) -> str | None:
    lines = source.splitlines(keepends=True)
    index = finding.line - 1
    if index >= len(lines):
        return None
    raw = lines[index]
    match = re.match(r"^(\s*)except\s*:\s*(#.*?)?(\r?\n)?$", raw)
    if not match:
        return None
    indent, comment, newline = match.group(1), match.group(2) or "", match.group(3) or "\n"
    spacer = " " if comment else ""
    lines[index] = f"{indent}except Exception:{spacer}{comment}{newline}"
    return "".join(lines)


def _fix_eq_none(source: str, finding: Finding) -> str | None:
    lines = source.splitlines(keepends=True)
    index = finding.line - 1
    if index >= len(lines):
        return None
    line = lines[index]
    new = re.sub(r"==\s*None\b", "is None", line)
    new = re.sub(r"!=\s*None\b", "is not None", new)
    new = re.sub(r"\bNone\s*==", "None is", new)
    new = re.sub(r"\bNone\s*!=", "None is not", new)
    new = re.sub(r"\bis not is\b", "is not", new)
    if new == line:
        return None
    lines[index] = new
    return "".join(lines)


def _fix_unused_import(source: str, finding: Finding) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    node = _node_at(tree, finding.line, (ast.Import, ast.ImportFrom))
    if node is None:
        return None
    if not isinstance(node, (ast.Import, ast.ImportFrom)):
        return None

    lines = source.splitlines(keepends=True)
    body = "\n".join(line for i, line in enumerate(source.splitlines(), start=1) if not (node.lineno <= i <= (node.end_lineno or node.lineno)))

    aliases = list(node.names)
    bindings = [(alias, alias.asname or alias.name.split(".")[0]) for alias in aliases]
    unused = [(alias, binding) for alias, binding in bindings if not re.search(rf"\b{re.escape(binding)}\b", body)]
    if not unused:
        return None

    start = node.lineno - 1
    end = node.end_lineno or node.lineno
    if len(unused) == len(bindings):
        del lines[start:end]
        return "".join(lines)

    if node.lineno != (node.end_lineno or node.lineno):
        return None  # 多行 import 只删不重建，避免把格式搞乱
    indent = " " * node.col_offset
    kept = [alias for alias, _ in bindings if (alias, _) not in unused]
    rendered = ", ".join(f"{alias.name} as {alias.asname}" if alias.asname else alias.name for alias in kept)
    if isinstance(node, ast.Import):
        statement = f"{indent}import {rendered}"
    else:
        dots = "." * node.level
        statement = f"{indent}from {dots}{node.module or ''} import {rendered}"
    lines[start] = statement + "\n"
    return "".join(lines)


def _fix_mutable_default(source: str, finding: Finding) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    target: tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, ast.expr] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        default_pairs = list(zip(node.args.args[-len(node.args.defaults) :], node.args.defaults)) if node.args.defaults else []
        for arg, default in default_pairs:
            if default.lineno == finding.line and node.name == finding.symbol:
                target = (node, arg.arg, default)
                break
        if target:
            break
    if target is None:
        return None
    fn, arg_name, default = target
    if (default.end_lineno or default.lineno) != default.lineno:
        return None
    span = _span(source, default)
    if span is None:
        return None
    default_text = source[span[0] : span[1]]

    patched = source[: span[0]] + "None" + source[span[1] :]
    # 重新定位函数体第一行（替换长度变了，但行号不变）
    body_line = fn.body[0].lineno
    if (
        isinstance(fn.body[0], ast.Expr)
        and isinstance(fn.body[0].value, ast.Constant)
        and isinstance(fn.body[0].value.value, str)
        and len(fn.body) > 1
    ):
        body_line = fn.body[1].lineno

    lines = patched.splitlines(keepends=True)
    index = body_line - 1
    if index > len(lines):
        return None
    indent = " " * (fn.col_offset + 4)
    guard = [
        f"{indent}if {arg_name} is None:\n",
        f"{indent}    {arg_name} = {default_text}\n",
    ]
    lines[index:index] = guard
    return "".join(lines)


FIXERS: dict[str, Callable[[str, Finding], str | None]] = {
    "bare-except": _fix_bare_except,
    "eq-none": _fix_eq_none,
    "unused-import": _fix_unused_import,
    "mutable-default": _fix_mutable_default,
}


# ---------------------------------------------------------------------- 批量


def fixable(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.fixable and f.rule in FIXERS]


def apply_fixes(source: str, findings: list[Finding], max_rounds: int = 40) -> FixOutcome:
    """按行号从下往上修，每改一次就重新校验。"""
    outcome = FixOutcome(original=source, source=source)
    current = source
    candidates = sort_findings(fixable(findings))
    candidates.sort(key=lambda f: -f.line)  # 从下往上，避免行号错位

    seen: set[str] = set()
    for finding in candidates[:max_rounds]:
        if finding.key in seen:
            continue
        seen.add(finding.key)
        fixer = FIXERS.get(finding.rule)
        if fixer is None:
            outcome.records.append(FixRecord(finding.rule, finding.line, finding.symbol, "skipped", "没有对应的自动修复器"))
            continue

        before = analyze(current)
        try:
            candidate = fixer(current, finding)
        except Exception as exc:  # noqa: BLE001 - 修复器出错只跳过这一处
            outcome.records.append(FixRecord(finding.rule, finding.line, finding.symbol, "rejected", f"修复器异常：{exc}"))
            continue
        if candidate is None or candidate == current:
            outcome.records.append(FixRecord(finding.rule, finding.line, finding.symbol, "skipped", "该写法超出自动修复范围"))
            continue

        ok, reason = _validate(before, candidate, finding)
        if not ok:
            outcome.records.append(FixRecord(finding.rule, finding.line, finding.symbol, "rejected", reason))
            continue
        current = candidate
        outcome.records.append(FixRecord(finding.rule, finding.line, finding.symbol, "applied", "已自动修改"))

    outcome.source = current
    return outcome


def _validate(before: list[Finding], candidate: str, finding: Finding) -> tuple[bool, str]:
    try:
        ast.parse(candidate)
    except SyntaxError as exc:
        return False, f"修改后语法错误：{exc.msg}"
    after = analyze(candidate)
    if _count(after, finding.rule) >= _count(before, finding.rule):
        return False, "修改后该规则的问题数没有减少"
    before_high = count_by_severity(before)["high"]
    after_high = count_by_severity(after)["high"]
    if after_high > before_high:
        return False, f"修改引入了新的高危问题（{before_high} → {after_high}）"
    return True, ""


def _count(findings: list[Finding], rule: str) -> int:
    return sum(1 for finding in findings if finding.rule == rule)


def apply_model_patch(source: str, candidate: str) -> tuple[str, list[FixRecord]]:
    """采纳模型给出的整份新文件，但要过同一套校验。"""
    record = FixRecord("model-rewrite", 0, "", "applied", "采纳模型重写")
    if not candidate.strip():
        record.status = "rejected"
        record.detail = "模型返回空内容"
        return source, [record]
    try:
        ast.parse(candidate)
    except SyntaxError as exc:
        record.status = "rejected"
        record.detail = f"模型返回的代码无法解析：{exc.msg}"
        return source, [record]
    before = analyze(source)
    after = analyze(candidate)
    if count_by_severity(after)["high"] > count_by_severity(before)["high"]:
        record.status = "rejected"
        record.detail = "模型重写后高危问题反而变多"
        return source, [record]
    if len(after) > len(before):
        record.status = "rejected"
        record.detail = f"模型重写后问题总数变多（{len(before)} → {len(after)}）"
        return source, [record]
    record.detail = f"采纳模型重写（问题 {len(before)} → {len(after)}）"
    return candidate, [record]
