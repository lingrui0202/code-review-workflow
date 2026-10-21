"""确定性审查规则：基于 AST，不依赖模型。

为什么要有这一层：审查任务里最贵的是"猜"。模型负责判断"这段逻辑对不对"，
而"有没有裸 ``except:``""默认参数是不是可变对象""这个 import 到底用没用"
这类问题**不需要模型**——AST 一扫就是确定答案，可复现、可测试、零 token。

所以工作流里审查 Agent 的做法是：先跑规则拿到确定的问题，再（可选地）让模型
补充语义层面的问题。离线模式下只有前者，流程照样跑得通，报告照样有内容。
"""

from __future__ import annotations

import ast
import re
from typing import Iterator

from .finding import Finding, Rule

RULES: dict[str, Rule] = {
    "syntax-error": Rule(
        id="syntax-error",
        title="语法错误",
        severity="high",
        fixable=False,
        rationale="文件无法解析，后续所有检查与修复都失去意义。",
        how="先修语法，再进入审查流程。",
    ),
    "bare-except": Rule(
        id="bare-except",
        title="裸 except",
        severity="high",
        fixable=True,
        rationale="裸 except 会连 KeyboardInterrupt/SystemExit 一起吞掉，导致程序无法正常退出。",
        how="改成 except Exception: 或更具体的异常类型。",
    ),
    "swallowed-exception": Rule(
        id="swallowed-exception",
        title="异常被静默吞掉",
        severity="high",
        fixable=False,
        rationale="把异常捕获后什么都不做，故障会变成「数据看起来是对的」，排障成本极高。",
        how="至少记录日志，或把异常重新抛出；确实要忽略时写一句注释说明原因。",
    ),
    "mutable-default": Rule(
        id="mutable-default",
        title="可变对象做默认参数",
        severity="high",
        fixable=True,
        rationale="默认值只在函数定义时求值一次，多次调用会共享同一个 list/dict，产生跨调用的脏数据。",
        how="默认值改成 None，函数体内再兜底成新对象。",
    ),
    "dangerous-call": Rule(
        id="dangerous-call",
        title="危险调用",
        severity="high",
        fixable=False,
        rationale="eval/exec/os.system/shell=True 会把外部输入直接变成代码执行或命令注入。",
        how="改用 ast.literal_eval、subprocess 参数列表，并对输入做白名单校验。",
    ),
    "hardcoded-secret": Rule(
        id="hardcoded-secret",
        title="硬编码密钥",
        severity="high",
        fixable=False,
        rationale="密钥进了代码就等于进了仓库历史，撤不回来。",
        how="移到环境变量或 .env，并在 .gitignore 里排除。",
    ),
    "long-function": Rule(
        id="long-function",
        title="函数过长",
        severity="medium",
        fixable=False,
        rationale="超过 60 行的函数通常同时承担了多个职责，测试和复用都很困难。",
        how="按职责拆成若干私有函数。",
    ),
    "deep-nesting": Rule(
        id="deep-nesting",
        title="嵌套过深",
        severity="medium",
        fixable=False,
        rationale="四层以上的嵌套意味着圈复杂度高，分支组合爆炸，回归风险大。",
        how="用提前返回（卫语句）或抽取子函数削减层级。",
    ),
    "too-many-args": Rule(
        id="too-many-args",
        title="参数过多",
        severity="medium",
        fixable=False,
        rationale="参数超过 5 个时，调用方极易传错位置，且难以扩展。",
        how="把一组相关参数收成 dataclass 或 dict。",
    ),
    "unused-import": Rule(
        id="unused-import",
        title="未使用的导入",
        severity="low",
        fixable=True,
        rationale="无用导入会增加模块加载时间，也掩盖真实的依赖关系。",
        how="删掉即可。",
    ),
    "missing-docstring": Rule(
        id="missing-docstring",
        title="缺少文档字符串",
        severity="low",
        fixable=False,
        rationale="公开函数没有 docstring 时，调用方只能读实现来理解契约。",
        how="补一段说明用途、参数与返回值的 docstring。",
    ),
    "eq-none": Rule(
        id="eq-none",
        title="用 == 比较 None",
        severity="low",
        fixable=True,
        rationale="自定义 __eq__ 的对象可能让 == None 返回真；PEP 8 要求用 is None。",
        how="改成 is None / is not None。",
    ),
}

# 供 --list-rules 与文档使用
RULE_TABLE = list(RULES.values())

_MUTABLE_BUILTINS = {"list", "dict", "set", "bytearray"}
_DANGEROUS_NAMES = {"eval", "exec", "compile", "__import__"}
_SECRET_HINTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "access_key")
_LONG_FUNCTION_LINES = 60
_MAX_DEPTH = 4
_MAX_ARGS = 5


def _is_mutable_default(node: ast.expr) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in _MUTABLE_BUILTINS
    return False


def _default_source(source: str, node: ast.AST) -> str:
    try:
        segment = ast.get_source_segment(source, node)
    except Exception:  # pragma: no cover - 极端情况下取不到源码
        segment = None
    return segment or "[]"


def _nesting_depth(node: ast.AST) -> int:
    """函数体内部的最大控制流嵌套层数。"""
    nesting = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler, ast.Match)

    def walk(current: ast.AST, depth: int) -> int:
        best = depth
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            step = depth + 1 if isinstance(child, nesting) else depth
            best = max(best, walk(child, step))
        return best

    return max((walk(child, 1 if isinstance(child, nesting) else 0) for child in ast.iter_child_nodes(node)), default=0)


def _arg_count(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    args = fn.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    names = [name for name in names if name not in ("self", "cls")]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return len(names)


class _Scanner(ast.NodeVisitor):
    def __init__(self, source: str, filename: str) -> None:
        self.source = source
        self.filename = filename
        self.lines = source.splitlines()
        self.findings: list[Finding] = []

    # ------------------------------------------------------------------ 工具
    def add(self, rule_id: str, line: int, message: str, symbol: str = "", suggestion: str = "") -> None:
        rule = RULES[rule_id]
        self.findings.append(
            Finding(
                rule=rule.id,
                severity=rule.severity,
                line=max(1, int(line)),
                message=message,
                symbol=symbol,
                suggestion=suggestion or rule.how,
                fixable=rule.fixable,
            )
        )

    def _docstring(self, node: ast.AST) -> str:
        for child in getattr(node, "body", [])[:1]:
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant) and isinstance(child.value.value, str):
                return child.value.value
        return ""

    # ------------------------------------------------------------------ 函数
    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = node.name
        if not name.startswith("_") and not self._docstring(node):
            self.add("missing-docstring", node.lineno, f"公开函数 {name}() 没有 docstring", name)
        lines = (node.end_lineno or node.lineno) - node.lineno + 1
        if lines > _LONG_FUNCTION_LINES:
            self.add(
                "long-function",
                node.lineno,
                f"{name}() 共 {lines} 行，超过 {_LONG_FUNCTION_LINES} 行阈值",
                name,
            )
        depth = _nesting_depth(node)
        if depth > _MAX_DEPTH:
            self.add("deep-nesting", node.lineno, f"{name}() 控制流嵌套 {depth} 层", name)
        arg_count = _arg_count(node)
        if arg_count > _MAX_ARGS:
            self.add("too-many-args", node.lineno, f"{name}() 有 {arg_count} 个参数", name)

        defaults = list(zip(node.args.args[-len(node.args.defaults) :], node.args.defaults)) if node.args.defaults else []
        for arg, default in defaults:
            if _is_mutable_default(default):
                self.add(
                    "mutable-default",
                    default.lineno,
                    f"参数 {arg.arg} 的默认值是可变对象，会被多次调用共享",
                    name,
                    suggestion=f"默认值改为 None，并在函数体内 if {arg.arg} is None: {arg.arg} = ...",
                )
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is not None and _is_mutable_default(default):
                self.add(
                    "mutable-default",
                    default.lineno,
                    f"关键字参数 {arg.arg} 的默认值是可变对象",
                    name,
                )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._check_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        if not node.name.startswith("_") and not self._docstring(node):
            self.add("missing-docstring", node.lineno, f"公开类 {node.name} 没有 docstring", node.name)
        self.generic_visit(node)

    # ------------------------------------------------------------------ 异常
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.type is None:
            self.add("bare-except", node.lineno, "使用裸 except，会吞掉 KeyboardInterrupt 等系统异常")
        else:
            body = [stmt for stmt in node.body if not (isinstance(stmt, ast.Pass))]
            silent = not body
            if silent and isinstance(node.type, (ast.Name, ast.Tuple)):
                type_name = ast.unparse(node.type)
                self.add(
                    "swallowed-exception",
                    node.lineno,
                    f"except {type_name} 之后只有 pass，异常被静默吞掉",
                )
        self.generic_visit(node)

    # ------------------------------------------------------------------ 比较
    def visit_Compare(self, node: ast.Compare) -> None:  # noqa: N802
        for op, comparator in zip(node.ops, node.comparators):
            is_none = isinstance(comparator, ast.Constant) and comparator.value is None
            is_none = is_none or (isinstance(comparator, ast.Name) and comparator.id in ("None", "none"))
            if is_none and isinstance(op, (ast.Eq, ast.NotEq)):
                self.add("eq-none", getattr(node, "lineno", 1), "与 None 比较应使用 is / is not")
        self.generic_visit(node)

    # ------------------------------------------------------------------ 调用
    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in _DANGEROUS_NAMES:
                self.add("dangerous-call", node.lineno, f"直接调用 {func_name}()，存在代码注入风险")
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
            if func_name in ("system", "popen"):
                self.add("dangerous-call", node.lineno, f"调用 .{func_name}()，存在命令注入风险")
        for keyword in node.keywords:
            if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                self.add("dangerous-call", node.lineno, "subprocess 使用 shell=True")
        self.generic_visit(node)

    # ------------------------------------------------------------------ 赋值
    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            lowered = target.id.lower()
            if not any(hint in lowered for hint in _SECRET_HINTS):
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) >= 6:
                self.add(
                    "hardcoded-secret",
                    node.lineno,
                    f"{target.id} 直接写入了字符串常量，疑似硬编码密钥",
                    target.id,
                )
        self.generic_visit(node)

    # ------------------------------------------------------------------ 导入
    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        self._check_import(node, [(alias, alias.name.split(".")[0]) for alias in node.names])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module == "__future__":
            return
        names = []
        for alias in node.names:
            if alias.name == "*":
                continue
            names.append((alias, alias.asname or alias.name))
        self._check_import(node, names)
        self.generic_visit(node)

    def _check_import(self, node: ast.stmt, aliases: list[tuple[ast.alias, str]]) -> None:
        if not aliases:
            return
        body = "\n".join(
            line for index, line in enumerate(self.lines, start=1) if not (node.lineno <= index <= (node.end_lineno or node.lineno))
        )
        unused = [
            (alias, binding)
            for alias, binding in aliases
            if not re.search(rf"\b{re.escape(binding)}\b", body)
        ]
        for alias, binding in unused:
            self.add(
                "unused-import",
                node.lineno,
                f"导入的 {binding} 在文件里没有被使用",
                symbol=binding,
                suggestion="删除该导入",
            )


def analyze(source: str, filename: str = "<string>") -> list[Finding]:
    """对源码做一次全量规则扫描。语法错误时返回单条 syntax-error。"""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [
            Finding(
                rule="syntax-error",
                severity=RULES["syntax-error"].severity,
                line=exc.lineno or 1,
                message=f"第 {exc.lineno} 行语法错误：{exc.msg}",
                symbol=filename,
                suggestion="先修好语法，再重新审查。",
            )
        ]
    scanner = _Scanner(source, filename)
    scanner.visit(tree)
    return sorted(scanner.findings, key=lambda f: (f.line, f.rule))


def analyze_file(path: str) -> list[Finding]:
    from pathlib import Path

    file = Path(path)
    return analyze(file.read_text(encoding="utf-8"), filename=str(file))


def iter_rule_descriptions() -> Iterator[Rule]:
    yield from RULE_TABLE
