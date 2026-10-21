"""项目统一入口：``python main.py run --offline``。

保持根目录只有这一个薄壳，源码都在 ``src/workflow_mvp`` 里，这样既可以直接
``git clone`` 后运行，也能 ``pip install -e .`` 后当库用。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from workflow_mvp.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
