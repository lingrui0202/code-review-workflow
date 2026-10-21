"""Prompt 模板：只放"说什么"，不放"怎么跑"。

三个 Agent 的 Prompt 有一个共同点：**要求输出可机器校验的结构**。
审查要 JSON（能逐条落成 Finding），修复要整份文件（能 ast.parse 后再采纳）。
把"模型说的"变成"程序能查的"，是这条流水线能闭环的前提。
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------- 编码

CODER_SYSTEM = """你是一名编码工程师，按需求规格写一份可直接运行的 Python 模块。

要求：
1. 只输出一个 ```python 代码块，不要解释；
2. 代码必须能通过 ast.parse，不依赖第三方库；
3. 公开函数写 docstring；
4. 异常处理要具体，不要用裸 except，不要把异常静默吞掉；
5. 不要写死任何密钥。"""

CODER_USER = """需求规格：

{spec}

请给出完整实现（单文件）。"""

# --------------------------------------------------------------------- 审查

REVIEWER_SYSTEM = """你是一名严格的代码审查工程师，只关注规则引擎看不出来的**语义层面**问题：
逻辑错误、边界条件缺失、命名与职责不符、可维护性隐患、明显的性能问题。

不要重复下面"已经由静态规则发现的问题"。
不要提出纯风格偏好（缩进、引号、行宽）。

只输出 JSON 数组，每个元素形如：
{{"rule": "语义问题短标签(英文小写)", "severity": "high|medium|low",
  "line": 行号(整数), "symbol": "相关函数或变量名",
  "message": "一句话说明问题", "suggestion": "一句话给出改法"}}

没有额外问题时输出 []。"""

REVIEWER_USER = """被审查文件：{filename}

```python
{code}
```

静态规则已发现的问题（不要重复）：
{known}

请补充语义层面的问题。"""

# --------------------------------------------------------------------- 修复

FIXER_SYSTEM = """你是一名修复工程师。给你一份代码和一份问题清单，请输出**修改后的完整文件**。

硬性要求：
1. 只输出一个 ```python 代码块；
2. 保持原有公开行为与函数签名（除非问题本身要求改签名）；
3. 修完的代码必须能通过 ast.parse；
4. 不要顺手做无关重构；问题清单之外的代码尽量原样保留；
5. 无法安全修复的条目，保持原样，不要发明新逻辑。"""

FIXER_USER = """文件：{filename}

待修复问题：
{issues}

当前代码：

```python
{code}
```

请输出修复后的完整文件。"""


def render_known(findings: list[dict]) -> str:
    if not findings:
        return "（无）"
    lines = []
    for item in findings[:40]:
        where = f"L{item.get('line', '?')}"
        lines.append(f"- {where} [{item.get('severity')}] {item.get('rule')}：{item.get('message')}")
    return "\n".join(lines)


def render_issues(findings: list[dict]) -> str:
    if not findings:
        return "（无）"
    lines = []
    for item in findings[:40]:
        fixable = "可自动修复" if item.get("fixable") else "需人工判断"
        lines.append(
            f"- L{item.get('line', '?')} [{item.get('severity')}] {item.get('rule')}："
            f"{item.get('message')}（{fixable}；建议：{item.get('suggestion', '')})"
        )
    return "\n".join(lines)


_CODE_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str | None:
    """从模型回复里取第一个代码块；没有代码块时整段当代码（再交给 ast 校验）。"""
    matches = _CODE_FENCE.findall(text or "")
    if matches:
        return matches[0].strip("\n")
    stripped = (text or "").strip()
    if stripped.startswith(("def ", "import ", "from ", "class ", '"""', "#")):
        return stripped
    return None
