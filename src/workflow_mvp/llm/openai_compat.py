"""OpenAI 兼容协议的 HTTP 客户端（只用标准库 urllib，不引第三方 SDK）。

之所以不用 ``openai`` 包：这个项目的运行时依赖为零，评测方 ``pip install`` 都不需要
就能跑离线模式；在线模式只多一个可选的 API Key。SDK 能做的事（拼请求、解析 JSON）
在这里不到一百行，换来的是"没有依赖就不会有依赖冲突"。
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any

from .base import LLMError, LLMProvider, LLMResponse


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        retries: int = 3,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = max(1, retries)

    # ------------------------------------------------------------------ 请求
    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        if not self.api_key and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            raise LLMError("缺少 API Key（可用 --offline 走离线模式）", retryable=False)

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        started = time.perf_counter()

        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key or 'EMPTY'}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                text = _first_message(data)
                usage = data.get("usage", {}) if isinstance(data, dict) else {}
                return LLMResponse(
                    text=text,
                    model=data.get("model", self.model) if isinstance(data, dict) else self.model,
                    usage=usage if isinstance(usage, dict) else {},
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except urllib.error.HTTPError as exc:
                detail = _read_error(exc)
                # 4xx（除 429）是参数/鉴权问题，重试没意义
                retryable = exc.code == 429 or exc.code >= 500
                last_error = LLMError(f"HTTP {exc.code}：{detail}", retryable=retryable, status=exc.code)
                if not retryable or attempt == self.retries:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = LLMError(f"网络/解析错误：{exc}", retryable=True)
                if attempt == self.retries:
                    raise last_error from exc

            backoff = 2 ** (attempt - 1) + random.uniform(0, 0.5)
            time.sleep(min(backoff, 8.0))

        raise last_error or LLMError("未知的模型调用失败", retryable=True)


def _first_message(data: Any) -> str:
    if not isinstance(data, dict):
        raise LLMError("响应不是 JSON 对象", retryable=True)
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"响应里没有 choices：{str(data)[:200]}", retryable=True)
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # 少数厂商返回分段结构
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise LLMError("响应里没有文本内容", retryable=True)
    return content


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:300]
    except Exception:  # pragma: no cover
        return str(exc)
