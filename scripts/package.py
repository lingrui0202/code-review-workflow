"""打包脚本：先跑测试，再打成 zip（提交课程平台用）。

用法：
    python scripts/package.py --name 学号          # 例如 --name 2023123456
    python scripts/package.py --skip-tests
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    "node_modules", "dist", "build", ".pytest_cache", ".mypy_cache",
    "runs", "demo-work", "output", ".ruff_cache",
}
EXCLUDE_FILES = {".env", ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".zip", ".rar", ".7z", ".bak"}

MAX_BYTES = 200 * 1024 * 1024


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if any(part in EXCLUDE_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        if path.name in EXCLUDE_FILES or path.suffix.lower() in EXCLUDE_SUFFIX:
            continue
        yield path


def run_tests() -> bool:
    print(">> 运行单元测试…")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
    print("\n".join(tail))
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="将项目打包为 zip")
    parser.add_argument("--name", default="code-review-workflow", help="输出文件名（不含扩展名）")
    parser.add_argument("--output", default=None, help="输出目录，默认项目根目录")
    parser.add_argument("--skip-tests", action="store_true", help="跳过测试")
    args = parser.parse_args()

    if not args.skip_tests and not run_tests():
        print("!! 单元测试未通过，仍要打包请加 --skip-tests")
        return 1

    out_dir = Path(args.output) if args.output else ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{args.name}.zip"

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in iter_files(ROOT):
            if path == zip_path:
                continue
            zf.write(path, path.relative_to(ROOT).as_posix())
            count += 1

    size = zip_path.stat().st_size
    print(f"\n>> 已打包 {count} 个文件 -> {zip_path}")
    print(f">> 体积 {size / 1024:.1f} KB" + ("" if size < MAX_BYTES else "（!! 超过 200MB）"))
    print(">> 已排除 .env / 运行产物 / 缓存；使用者无需任何第三方依赖即可跑 --offline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
