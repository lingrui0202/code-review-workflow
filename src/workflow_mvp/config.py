"""配置层：环境变量 / .env / 命令行参数，优先级从低到高。

不做配置文件解析库，只认 ``KEY=VALUE`` 这一种形状——评测方复制一个
``.env.example`` 就能用，不需要理解任何约定。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_ROUNDS = 3
DEFAULT_TEMPERATURE = 0.2


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """极简 .env 读取：忽略注释与空行，不覆盖已存在的环境变量。"""
    file = Path(path)
    loaded: dict[str, str] = {}
    if not file.exists():
        return loaded
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


@dataclass
class Settings:
    """一次运行的配置。"""

    provider: str = "deepseek"
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    offline: bool = False
    workspace: Path = field(default_factory=lambda: Path.cwd())
    run_root: Path = field(default_factory=lambda: Path("runs"))
    max_rounds: int = DEFAULT_MAX_ROUNDS
    temperature: float = DEFAULT_TEMPERATURE
    write_back: bool = True
    use_llm: bool = True

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()
        self.run_root = Path(self.run_root)
        if not self.run_root.is_absolute():
            self.run_root = self.workspace / self.run_root
        self.max_rounds = max(1, int(self.max_rounds))

    @property
    def model_mode(self) -> str:
        return "离线（规则引擎）" if self.offline else f"{self.provider}（模型辅助）"

    def masked(self) -> dict[str, object]:
        """给报告用的脱敏配置。"""
        key = self.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        return {
            "provider": self.provider,
            "model": self.model or "(默认)",
            "api_key": f"{key[:4]}***{key[-2:]}" if len(key) > 8 else ("未设置" if not key else "***"),
            "base_url": self.base_url or "(默认)",
            "offline": self.offline,
            "max_rounds": self.max_rounds,
            "write_back": self.write_back,
            "workspace": str(self.workspace),
        }
