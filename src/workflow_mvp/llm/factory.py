"""Provider 工厂：把 provider 名字映射成一个可用的客户端。

所有厂商都走 OpenAI 兼容协议，所以差异只在于 base_url、Key 的环境变量名和默认模型。
加一家新厂商 = 在 ``PRESETS`` 里加一行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .base import LLMError, LLMProvider
from .offline import OfflineProvider
from .openai_compat import OpenAICompatProvider


@dataclass(frozen=True)
class Preset:
    name: str
    base_url: str
    key_env: str
    model: str


PRESETS: dict[str, Preset] = {
    "deepseek": Preset("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "qwen": Preset("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "qwen-plus"),
    "openai": Preset("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o-mini"),
    "moonshot": Preset("moonshot", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "moonshot-v1-8k"),
    "ollama": Preset("ollama", "http://127.0.0.1:11434/v1", "OLLAMA_API_KEY", "qwen2.5-coder:7b"),
}

PROVIDER_NAMES = (*PRESETS, "offline", "custom")


def resolve_key(preset: Preset, api_key: str | None) -> str:
    if api_key:
        return api_key
    return os.environ.get(preset.key_env, "").strip()


def create_provider(
    name: str | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    offline: bool = False,
) -> LLMProvider:
    """按名字建 provider。``offline=True`` 或名字为 offline 时返回离线实现。

    ``custom`` 用于任意 OpenAI 兼容接口：只要给 base_url，就能接自建服务、
    中转站或本地网关。
    """
    if offline:
        return OfflineProvider()

    chosen = (name or os.environ.get("PROVIDER") or "deepseek").strip().lower()
    if chosen == "offline":
        return OfflineProvider()

    if chosen == "custom":
        if not base_url:
            raise LLMError("自定义接口需要 base_url（例如 https://api.example.com/v1）")
        return OpenAICompatProvider(
            name="custom",
            base_url=base_url,
            api_key=(api_key or os.environ.get("CUSTOM_API_KEY", "") or "EMPTY").strip(),
            model=model or "gpt-4o-mini",
        )

    if chosen not in PRESETS:
        raise LLMError(f"未知 provider {chosen!r}，可选：{', '.join(PROVIDER_NAMES)}")

    preset = PRESETS[chosen]
    key = resolve_key(preset, api_key)
    if not key and "localhost" not in (base_url or preset.base_url) and "127.0.0.1" not in (base_url or preset.base_url):
        raise LLMError(
            f"未找到 {preset.key_env}。三种解决办法：\n"
            f"  1) 设置环境变量 {preset.key_env}；\n"
            f"  2) 在 .env 里填写 {preset.key_env}；\n"
            f"  3) 加 --offline 使用离线规则模式。",
            retryable=False,
        )

    return OpenAICompatProvider(
        name=preset.name,
        base_url=base_url or preset.base_url,
        api_key=key,
        model=model or preset.model,
    )
