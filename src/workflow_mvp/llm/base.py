"""LLM 抽象层。

只有两件事需要稳定：``chat(messages) -> text`` 的调用形状，以及"这次失败到底
该不该重试"的判断。其余（换厂商、换模型、换协议）都应该只改工厂里的一行配置。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """模型调用失败。``retryable`` 决定引擎要不要重试。"""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens", 0) or 0)


class LLMProvider(ABC):
    """模型提供方抽象。"""

    name: str = "unknown"
    model: str = ""
    offline: bool = False

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """一次纯文本对话。失败抛 ``LLMError``。"""

    def describe(self) -> str:
        if self.offline:
            return "离线模式（规则引擎，无模型调用）"
        return f"{self.name} / {self.model}"


def extract_json(text: str) -> Any:
    """从模型回复里抠出 JSON。

    模型很喜欢在 JSON 外面裹一层解释文字或 ```json 代码块，所以既处理代码块，
    也允许从任意 ``{`` / ``[`` 起点做 ``raw_decode``（天然支持嵌套）。
    """
    if not text or not text.strip():
        raise ValueError("模型返回为空，无法解析 JSON")

    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    candidates = [block.strip() for block in fenced] + [text.strip()]

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for index, char in enumerate(candidate):
            if char not in "{[":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("回复里没有可解析的 JSON")
