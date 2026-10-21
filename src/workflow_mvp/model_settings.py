"""模型设置：让网页里能换厂商、换模型、换接口地址，而不是只能改 .env。

设计上有三条硬规矩：

1. **Key 只在服务端内存里**，接口只返回 ``sk-1***23`` 这种掩码，永不回显明文；
2. **不落盘除非你勾了"保存"**，保存时只改 ``.env`` 里对应的那几行，注释原样保留；
3. **任何一次连接失败都要说清楚是哪一步**（缺 Key / 地址不对 / 401 / 超时），
   而不是只回一句"失败了"。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm.base import LLMError
from .llm.factory import PRESETS, create_provider
from .llm.openai_compat import OpenAICompatProvider

PROVIDER_CHOICES = [*PRESETS, "custom"]
CUSTOM_KEY_ENV = "CUSTOM_API_KEY"

PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "qwen": "通义千问（DashScope）",
    "openai": "OpenAI",
    "moonshot": "Moonshot",
    "ollama": "本地 Ollama",
    "custom": "自定义（OpenAI 兼容）",
}


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}***{key[-2:]}"


def key_env_name(provider: str) -> str:
    preset = PRESETS.get(provider)
    return preset.key_env if preset else CUSTOM_KEY_ENV


@dataclass
class ModelSettings:
    """当前生效的模型配置（服务端单例，进程内共享）。"""

    provider: str = "deepseek"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    # ------------------------------------------------------------------ 初始
    @classmethod
    def from_env(cls) -> "ModelSettings":
        name = (os.environ.get("PROVIDER") or "deepseek").strip().lower()
        if name not in PROVIDER_CHOICES:
            name = "deepseek"
        key = os.environ.get(key_env_name(name), "").strip()
        return cls(
            provider=name,
            model=(os.environ.get("MODEL") or "").strip(),
            base_url=(os.environ.get("BASE_URL") or "").strip(),
            api_key=key,
        )

    # ------------------------------------------------------------------ 读写
    def update(self, payload: dict[str, Any]) -> None:
        with self._lock:
            provider = str(payload.get("provider") or self.provider).strip().lower()
            if provider not in PROVIDER_CHOICES:
                raise ValueError(f"不支持的 provider：{provider}")

            base_url = str(payload.get("base_url") or "").strip()
            if provider == "custom" and not base_url:
                raise ValueError("自定义接口必须填 base_url（例如 https://api.example.com/v1）")

            self.provider = provider
            self.base_url = base_url
            self.model = str(payload.get("model") or "").strip()
            if payload.get("api_key") is not None:
                # 空字符串表示"清空"，None 表示"这次不改"
                self.api_key = str(payload["api_key"]).strip()

            # 换了厂商但没给 Key 时，尝试从环境变量里找新厂商自己的 Key
            if not self.api_key and provider != "custom":
                self.api_key = os.environ.get(key_env_name(provider), "").strip()

    def clear_key(self) -> None:
        with self._lock:
            self.api_key = ""

    # ------------------------------------------------------------------ 展示
    def public(self) -> dict[str, Any]:
        with self._lock:
            preset = PRESETS.get(self.provider)
            return {
                "provider": self.provider,
                "provider_label": PROVIDER_LABELS.get(self.provider, self.provider),
                "model": self.model or (preset.model if preset else ""),
                "base_url": self.base_url or (preset.base_url if preset else ""),
                "key_configured": bool(self.api_key),
                "key_hint": mask_key(self.api_key),
                "key_env": key_env_name(self.provider),
                "providers": [{"id": key, "label": PROVIDER_LABELS.get(key, key)} for key in PROVIDER_CHOICES],
                "presets": {key: {"model": value.model, "base_url": value.base_url} for key, value in PRESETS.items()},
                "editable": True,
            }

    # ------------------------------------------------------------------ 构建
    def build(self):
        """按当前设置构造 provider。配置不全时抛 ``LLMError``（带可读原因）。"""
        with self._lock:
            provider, model, base_url, api_key = self.provider, self.model, self.base_url, self.api_key

        if provider == "custom":
            if not base_url:
                raise LLMError("自定义接口缺少 base_url", retryable=False)
            return OpenAICompatProvider(
                name="custom",
                base_url=base_url,
                api_key=api_key or "EMPTY",
                model=model or "gpt-4o-mini",
            )
        return create_provider(
            provider,
            api_key=api_key or None,
            model=model or None,
            base_url=base_url or None,
        )

    def test_connection(self) -> dict[str, Any]:
        """发一个极小的请求验证连通性，返回结构化结果（不抛异常）。"""
        started = time.perf_counter()
        try:
            provider = self.build()
        except LLMError as exc:
            return {"ok": False, "stage": "config", "error": str(exc), "latency_ms": 0}

        try:
            response = provider.chat(
                [{"role": "user", "content": "只回复两个字：在线"}],
                temperature=0.0,
                max_tokens=16,
            )
        except LLMError as exc:
            stage = "auth" if exc.status in (401, 403) else ("network" if exc.retryable else "request")
            return {
                "ok": False,
                "stage": stage,
                "status": exc.status,
                "error": str(exc),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 - 兜底，页面上要看得到原因
            return {"ok": False, "stage": "unknown", "error": f"{type(exc).__name__}: {exc}", "latency_ms": 0}

        return {
            "ok": True,
            "stage": "done",
            "provider": provider.describe(),
            "reply": (response.text or "").strip()[:80],
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    # ------------------------------------------------------------------ 持久化
    def persist(self, path: str | Path) -> Path:
        """把当前设置写进 .env：只动相关的几行，其它内容原样保留。"""
        target = Path(path)
        with self._lock:
            provider, model, base_url, api_key = self.provider, self.model, self.base_url, self.api_key

        updates = {"PROVIDER": provider}
        if model:
            updates["MODEL"] = model
        if base_url:
            updates["BASE_URL"] = base_url
        if api_key:
            updates[key_env_name(provider)] = api_key

        existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        seen: set[str] = set()
        output: list[str] = []
        for line in existing:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    output.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            output.append(line)
        for key, value in updates.items():
            if key not in seen:
                output.append(f"{key}={value}")

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")
        tmp.replace(target)
        return target
