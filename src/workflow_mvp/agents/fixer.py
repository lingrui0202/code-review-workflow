"""修复 Agent：把审查意见变成真实的代码改动。

两种修复手段，优先级明确：

1. **模型重写**（在线）：让模型给出整份新文件。采纳前必须过 ``ast.parse`` +
   问题数不增加 + 高危不增加三道闸，任何一道不过就整份丢弃；
2. **机械修复**（离线和兜底都用）：只改规则明确、改法唯一的地方，每改一处都
   重新校验，改坏就回滚。

修不动的（架构、语义、需要业务判断的）不会硬改，而是进 ``manual_todo``——
报告里会明确写"这些要人来做"，而不是假装修好了。
"""

from __future__ import annotations

from pathlib import Path

from ..llm.base import LLMError
from ..review.finding import Finding, count_by_severity, from_dicts, to_dicts
from ..review.fixes import FixRecord, apply_fixes, apply_model_patch
from ..review.rules import analyze
from ..workflow.message import Message
from ..workspace import Workspace
from .base import AgentResult, BaseAgent
from .prompts import FIXER_SYSTEM, FIXER_USER, extract_code, render_issues


class FixerAgent(BaseAgent):
    role = "fixer"

    def __init__(self, name: str = "fixer", **kw) -> None:
        super().__init__(name, description="按审查意见修复代码并复验", **kw)

    def act(self, ctx) -> AgentResult:
        code = ctx.state.get("code")
        raw_findings = ctx.state.get("findings") or []
        findings = from_dicts(raw_findings)
        if not code:
            raise ValueError("修复 Agent 没有拿到代码（state.code 为空）")
        if not findings:
            return AgentResult(note="无事可做：没有待修复问题")

        round_no = int(ctx.state.get("iterations", 1) or 1)
        current = code
        records: list[FixRecord] = []
        origin = "mechanical"

        if not self.offline:
            current, model_records = self._model_rewrite(current, findings, ctx)
            records.extend(model_records)
            if any(record.status == "applied" for record in model_records):
                origin = "model+mechanical"

        # 模型改完（或离线）之后，再让机械修复器收一遍尾；
        # 用重新扫描的结果做输入，避免拿着"改动前"的行号去改"改动后"的文件。
        remaining = analyze(current, Path(ctx.state.get("code_path") or "code.py").name)
        outcome = apply_fixes(current, remaining)
        records.extend(outcome.records)
        new_code = outcome.source

        changed = new_code != code
        if changed:
            self._persist(ctx, new_code, origin)
            self.log(
                ctx,
                f"第 {round_no} 轮修复：{len([r for r in records if r.status == 'applied'])} 处已改，"
                f"{len([r for r in records if r.status != 'applied'])} 处跳过",
            )
        else:
            self.log(ctx, "没有可自动修复的问题，保留现状")

        workspace = self.workspace or Workspace(ctx.workspace)
        filename = Path(ctx.state.get("code_path") or "code.py").name
        diff = workspace.diff(code, new_code, filename) if changed else ""
        stats = workspace.diff_stats(code, new_code) if changed else {"added": 0, "removed": 0}
        summary = count_by_severity(analyze(new_code, filename))

        patch = {
            "code": new_code,
            "last_progress": changed,
            "fix_origin": origin,
            "fix_log": list(ctx.state.get("fix_log") or [])
            + [dict(record.to_dict(), round=round_no) for record in records],
            "fix_summary": {
                "round": round_no,
                "applied": len([r for r in records if r.status == "applied"]),
                "skipped": len([r for r in records if r.status != "applied"]),
                "changed": changed,
                "diff": stats,
                "after": summary,
            },
            "manual_todo": [
                dict(f.to_dict())
                for f in findings
                if not f.fixable and f.severity in ("high", "medium")
            ],
            "code_history": list(ctx.state.get("code_history") or [])
            + [{"round": round_no, "code": new_code, "origin": origin}],
        }
        if changed:
            patch["iterations"] = round_no + 1
        if diff:
            patch["patches"] = list(ctx.state.get("patches") or []) + [
                {"round": round_no, "diff": diff, "stats": stats}
            ]

        result = AgentResult(patch=patch, note=f"第 {round_no} 轮修复完成（{'有改动' if changed else '无改动'}）")

        if changed:
            result.say(
                Message.feedback(
                    self.name,
                    "reviewer",
                    f"第 {round_no} 轮修复完成：改 {len([r for r in records if r.status == 'applied'])} 处，"
                    f"新增 {stats['added']} 行 / 删除 {stats['removed']} 行，请复检。",
                    artifacts={"summary": patch["fix_summary"], "diff": diff},
                )
            )
        else:
            result.say(
                Message.verdict(
                    self.name,
                    "*",
                    f"第 {round_no} 轮无可自动修复的问题，剩余 {len(findings)} 处需要人工判断。",
                    artifacts={"remaining": len(findings)},
                )
            )
        return result

    # ------------------------------------------------------------------ 模型
    def _model_rewrite(self, code: str, findings: list[Finding], ctx) -> tuple[str, list[FixRecord]]:
        filename = Path(ctx.state.get("code_path") or "code.py").name
        prompt = FIXER_USER.format(
            filename=filename,
            issues=render_issues(to_dicts(findings)),
            code=code,
        )
        try:
            text = self.ask_model(
                [{"role": "system", "content": FIXER_SYSTEM}, {"role": "user", "content": prompt}],
                max_tokens=4000,
            )
        except LLMError as exc:
            ctx.emit("warn", f"修复模型调用失败，退回机械修复：{exc}")
            return code, [FixRecord("model-rewrite", 0, "", "skipped", f"模型调用失败：{exc}")]

        candidate = extract_code(text)
        if not candidate:
            return code, [FixRecord("model-rewrite", 0, "", "skipped", "模型未返回代码块")]
        return apply_model_patch(code, candidate)

    # ------------------------------------------------------------------ 落盘
    def _persist(self, ctx, new_code: str, origin: str) -> None:
        workspace = self.workspace or Workspace(ctx.workspace)
        target = ctx.state.get("code_path") or "output/draft.py"
        settings = ctx.service("settings")
        write_back = bool(settings.write_back) if settings is not None else True

        if write_back:
            path = workspace.write_text(target, new_code, backup=True)
            ctx.emit("write", f"  已写回 {workspace.rel(path)}（原件备份为 .bak）")
            return

        original = workspace.resolve(target)
        fixed = original.with_name(f"{original.stem}.fixed{original.suffix}")
        workspace.write_text(fixed, new_code, backup=False)
        ctx.emit("write", f"  未写回原件，修复结果另存为 {workspace.rel(fixed)}")
