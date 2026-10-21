"""代码审查与修复工作流（Simple Workflow MVP）。

入口有三种：``python main.py run`` 跑完整流程，``build_review_workflow()``
拿到图直接嵌进别的程序，或者只用 ``review.rules.analyze()`` 做静态检查。
"""

from .config import Settings
from .pipelines import build_review_workflow, initial_state
from .workspace import Workspace

__version__ = "1.0.0"

__all__ = ["Settings", "build_review_workflow", "initial_state", "Workspace", "__version__"]
