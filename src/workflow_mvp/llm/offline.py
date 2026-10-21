"""离线模式：不调用任何模型。

它不是"假实现"——工作流里三条 Agent 都保留了真正的决策逻辑，离线时只是把
"语义判断"这一层换成规则和模板：

- 编码 Agent：从任务模板生成一份**明确带缺陷**的初稿（模拟"新手写的代码"）；
- 审查 Agent：用 AST 规则做全量检查，结论客观可复现；
- 修复 Agent：只做机械修复，改不动的如实标成"需人工处理"。

因此 ``--offline`` 是评测/演示的可靠路径：没有 Key、没有网络也能跑出完整报告。
"""

from __future__ import annotations

from .base import LLMError, LLMProvider, LLMResponse


class OfflineProvider(LLMProvider):
    name = "offline"
    model = "rules-only"
    offline = True

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        raise LLMError(
            "离线模式不会调用模型；请用 --provider deepseek 等方式配置 API Key，"
            "或继续使用规则引擎的确定性结论。",
            retryable=False,
        )
