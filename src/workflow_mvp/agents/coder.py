"""编码 Agent：把需求规格变成一份可运行的初稿。

在工作流里它通常是第一棒。它有两条路径：

- **在线**：把需求交给模型写，写完用 ``ast.parse`` 校验，不合格就重试；
- **离线**：从内置模板生成一份"新手风格"的初稿（明确带缺陷，供后面两个 Agent
  演示发现问题 → 修复问题）。这一步是刻意保留的演示入口，不是能力替代品。

如果上游已经塞了 ``state.code``（例如 ``--code existing.py``），它什么都不做——
节点对"已有初稿"是幂等的。
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..review.rules import analyze
from ..workflow.message import Message
from .base import AgentResult, BaseAgent
from .prompts import CODER_SYSTEM, CODER_USER, extract_code

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_module.py"


class CoderAgent(BaseAgent):
    role = "coder"

    def __init__(self, name: str = "coder", **kw) -> None:
        super().__init__(name, description="按需求规格生成初稿", **kw)

    # ------------------------------------------------------------------ 主逻辑
    def act(self, ctx) -> AgentResult:
        existing = ctx.state.get("code")
        if existing:
            self.log(ctx, "已有初稿，跳过生成")
            return AgentResult(
                note="跳过：已有初稿",
                messages=[
                    Message.result(
                        self.name,
                        "reviewer",
                        f"上游已提供初稿（{ctx.state.get('code_path', 'code.py')}），编码步骤跳过。",
                    )
                ],
            )

        spec = ctx.state.get("task") or ""
        if not spec.strip():
            raise ValueError("没有需求规格（state.task 为空），请用 --task 指定任务文件")

        code, origin = self._produce(spec, ctx)
        if not code or not code.strip():
            raise ValueError("编码 Agent 没有产出代码")
        try:
            ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"生成的代码无法解析：{exc}") from exc

        target = ctx.state.get("code_path") or "output/draft.py"
        workspace = self.workspace
        if workspace is None:
            raise ValueError("编码 Agent 缺少 workspace")
        path = workspace.write_text(target, code, backup=False)
        rel = workspace.rel(path)
        self.log(ctx, f"初稿写入 {rel}（{len(code.splitlines())} 行，来源：{origin}）")

        profile = analyze(code, Path(rel).name)
        result = AgentResult(
            patch={
                "code": code,
                "code_path": rel,
                "code_origin": origin,
                "code_history": [{"round": 0, "code": code, "origin": origin}],
                "task_spec": spec,
                "iterations": ctx.state.get("iterations", 1) or 1,
            },
            note=f"生成初稿 {len(code.splitlines())} 行",
        )
        result.say(
            Message.result(
                self.name,
                "reviewer",
                f"初稿已生成：{rel}，共 {len(code.splitlines())} 行；"
                f"自检发现 {len(profile)} 处规则问题，请审查。",
                artifacts={"code_path": rel, "self_check": len(profile)},
            )
        )
        return result

    # ------------------------------------------------------------------ 产出
    def _produce(self, spec: str, ctx) -> tuple[str, str]:
        if self.offline:
            return self._seed(), "内置模板（离线演示）"
        text = self.ask_model(
            [
                {"role": "system", "content": CODER_SYSTEM},
                {"role": "user", "content": CODER_USER.format(spec=spec)},
            ]
        )
        code = extract_code(text)
        if not code:
            raise ValueError("模型没有返回代码块")
        return code, "模型生成"

    @staticmethod
    def _seed() -> str:
        if not SEED_PATH.exists():
            raise FileNotFoundError(f"缺少内置模板：{SEED_PATH}")
        header = "# 由离线模式模板生成：这是一份故意留有缺陷的初稿，用于演示审查与修复流程。\n"
        return header + SEED_PATH.read_text(encoding="utf-8")
