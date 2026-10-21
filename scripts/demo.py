"""一条命令看完整效果：离线跑两个场景，产出两份可读报告。

场景一：只有需求 → 编码 Agent 生成初稿 → 审查 → 修复 → 复检。
场景二：已有代码（``examples/bad_module.py`` 的副本）→ 跳过编码 → 审查 → 修复。

产物落在 ``demo-work/runs/<run_id>/``，跑完直接把报告路径打印出来。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_mvp.cli import main  # noqa: E402


def run_demo() -> int:
    work = ROOT / "demo-work"
    (work / "runs").mkdir(parents=True, exist_ok=True)

    print("\n=== 场景一：从需求规格开始 ===")
    code = main(
        [
            "run",
            "--offline",
            "--workspace",
            str(work),
            "--task",
            str(ROOT / "examples" / "task_order.md"),
        ]
    )
    if code != 0:
        return code

    source = ROOT / "examples" / "bad_module.py"
    target = work / "bad_module.py"
    shutil.copy2(source, target)

    print("\n=== 场景二：审查已有代码（examples/bad_module.py） ===")
    code = main(["run", "--offline", "--workspace", str(work), "--code", "bad_module.py"])
    if code != 0:
        return code

    reports = sorted((work / "runs").glob("*/report.md"), key=lambda path: path.stat().st_mtime)
    print("\n=== 报告 ===")
    for report in reports:
        print(f"  {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_demo())
