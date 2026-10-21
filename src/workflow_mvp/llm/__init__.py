"""LLM 层：统一抽象 + OpenAI 兼容实现 + 离线兜底。"""

from .base import LLMError, LLMProvider, LLMResponse, extract_json
from .factory import PRESETS, PROVIDER_NAMES, create_provider
from .offline import OfflineProvider

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "extract_json",
    "PRESETS",
    "PROVIDER_NAMES",
    "create_provider",
    "OfflineProvider",
]
