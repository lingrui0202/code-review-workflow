"""工作区：所有文件读写的唯一入口。

工作流会真的改磁盘上的文件，所以这里做三件事：

- **越权防护**：写入目标必须落在 workspace 之内，防止路径穿越；
- **写前备份**：改文件前先留一份 ``.bak``，跑砸了还能退回去；
- **原子写入**：先写临时文件再 ``replace``，避免断电留下半个文件。
"""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from pathlib import Path


class WorkspaceError(RuntimeError):
    """路径越界、文件不存在等。"""


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    # ------------------------------------------------------------------ 路径
    def resolve(self, path: str | Path, *, must_exist: bool = False) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.expanduser().resolve()
        if must_exist and not candidate.exists():
            raise WorkspaceError(f"文件不存在：{candidate}")
        return candidate

    def ensure_inside(self, path: Path) -> Path:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"拒绝访问工作区之外的路径：{path}") from exc
        return path

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    # ------------------------------------------------------------------ 读写
    def read_text(self, path: str | Path) -> str:
        target = self.resolve(path, must_exist=True)
        return target.read_text(encoding="utf-8")

    def write_text(self, path: str | Path, text: str, *, backup: bool = True) -> Path:
        target = self.ensure_inside(self.resolve(path))
        if backup and target.exists():
            self.backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=target.suffix)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return target

    def backup(self, path: Path) -> Path | None:
        """同目录 ``<name>.bak``（每次覆盖上一份，够用于一次会话的回滚）。"""
        if not path.exists():
            return None
        target = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, target)
        return target

    # ------------------------------------------------------------------ 差异
    @staticmethod
    def diff(before: str, after: str, filename: str = "code.py") -> str:
        lines = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
            n=3,
        )
        return "\n".join(lines)

    def diff_stats(self, before: str, after: str) -> dict[str, int]:
        added = removed = 0
        for line in self.diff(before, after).splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return {"added": added, "removed": removed}
