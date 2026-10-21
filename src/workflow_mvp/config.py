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


def project_root() -> Path:
    """仓库/安装根目录（用来找随项目带的 .env）。"""
    return Path(__file__).resolve().parents[2]


def load_project_env() -> dict[str, str]:
    """先找当前目录的 .env，再找项目根目录的 .env。

    双击 ``start-web.bat`` 时当前目录就是项目根；但如果从别的目录带路径启动，
    只按 CWD 找会漏掉，所以这里两个位置都试一次。
    """
    loaded: dict[str, str] = {}
    seen: set[Path] = set()
    for candidate in (Path.cwd() / ".env", project_root() / ".env"):
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        loaded.update(load_dotenv(resolved))
    return loaded


def env_file_candidates() -> list[Path]:
    """给界面提示用：告诉用户应该把 .env 放在哪儿。"""
    paths = [Path.cwd() / ".env", project_root() / ".env"]
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


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
