"""审查 Agent：静态规则 + （可选）模型语义审查。

它是工作流里"判断"的那一棒，输出结构化的 ``findings``，以及一份给人看的
Markdown 审查意见。规则的部分是确定性的，模型的部分是可选的——所以离线也
能得到完整、可信的结论，在线只是多发现一些规则抓不到的语义问题。
"""

from __future__ import annotations

from pathlib import Path

from ..llm.base import LLMError, extract_json
from ..review.finding import (
    SEVERITY_LABEL,
    Finding,
    count_by_severity,
    fingerprint,
    rule_histogram,
    sort_findings,
    to_dicts,
    worst_severity,
)
from ..review.rules import RULES, analyze
from ..workflow.message import Message
from .base import AgentResult, BaseAgent
from .prompts import REVIEWER_SYSTEM, REVIEWER_USER, render_known


class ReviewerAgent(BaseAgent):
    role = "reviewer"

    def __init__(self, name: str = "reviewer", **kw) -> None:
        super().__init__(name, description="静态规则 + 语义审查，产出问题清单", **kw)

    def act(self, ctx) -> AgentResult:
        code = ctx.state.get("code")
        if not code:
            raise ValueError("审查 Agent 没有拿到代码（state.code 为空）")
        filename = Path(ctx.state.get("code_path") or "code.py").name

        static_findings = analyze(code, filename)
        self.log(ctx, f"静态规则发现 {len(static_findings)} 处问题")

        semantic_findings: list[Finding] = []
        if not self.offline:
            semantic_findings = self._semantic_review(code, filename, static_findings, ctx)
            self.log(ctx, f"模型补充 {len(semantic_findings)} 处语义问题")

        findings = sort_findings(static_findings + semantic_findings)
        summary = count_by_severity(findings)
        blocking = summary["high"] + summary["medium"]
        verdict = "needs_fix" if blocking else "pass"
        round_no = int(ctx.state.get("iterations", 1) or 1)
        digest = fingerprint(findings)

        markdown = self._render_markdown(filename, findings, summary, round_no)
        history = list(ctx.state.get("findings_history") or [])
        history.append(
            {
                "round": round_no,
                "total": len(findings),
                "high": summary["high"],
                "medium": summary["medium"],
                "low": summary["low"],
                "fingerprint": digest,
                "verdict": verdict,
            }
        )

        self.log(ctx, f"第 {round_no} 轮结论：{verdict}（高 {summary['high']} / 中 {summary['medium']} / 低 {summary['low']}）")

        result = AgentResult(
            patch={
                "findings": to_dicts(findings),
                "review_summary": {
                    "total": len(findings),
                    **summary,
                    "worst": worst_severity(findings),
                    "by_rule": rule_histogram(findings),
                    "static": len(static_findings),
                    "semantic": len(semantic_findings),
                },
                "review_markdown": markdown,
                "verdict": verdict,
                "review_round": round_no,
                "findings_fingerprint": digest,
                "findings_history": history,
            },
            note=f"第 {round_no} 轮审查：{len(findings)} 处问题（{verdict}）",
        )

        if verdict == "needs_fix":
            result.say(
                Message.result(
                    self.name,
                    "fixer",
                    f"第 {round_no} 轮审查完成：高 {summary['high']} / 中 {summary['medium']} / "
                    f"低 {summary['low']}，请修复可自动处理的部分。",
                    artifacts={"findings": to_dicts(findings), "round": round_no},
                )
            )
        else:
            result.say(
                Message.verdict(
                    self.name,
                    "*",
                    f"第 {round_no} 轮审查通过：无高/中风险问题（低风险 {summary['low']} 处）。",
                    artifacts={"summary": summary, "round": round_no},
                )
            )
        return result

    # ------------------------------------------------------------------ 语义
    def _semantic_review(
        self,
        code: str,
        filename: str,
        known: list[Finding],
        ctx,
    ) -> list[Finding]:
        prompt = REVIEWER_USER.format(
            filename=filename,
            code=code,
            known=render_known(to_dicts(known)),
        )
        try:
            text = self.ask_model(
                [{"role": "system", "content": REVIEWER_SYSTEM}, {"role": "user", "content": prompt}],
                max_tokens=1500,
            )
            payload = extract_json(text)
        except (LLMError, ValueError) as exc:
            # 模型这一路失败不该拖垮整个审查：规则结论照样有效
            ctx.emit("warn", f"审查模型调用失败，仅使用静态规则：{exc}")
            return []

        if isinstance(payload, dict):
            payload = payload.get("findings", [])
        if not isinstance(payload, list):
            return []

        existing = {(finding.rule, finding.line) for finding in known}
        merged: list[Finding] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            rule = str(raw.get("rule") or "semantic").strip().lower().replace(" ", "-")[:40]
            severity = str(raw.get("severity") or "medium").lower()
            if severity not in ("high", "medium", "low"):
                severity = "medium"
            try:
                line = int(raw.get("line") or 1)
            except (TypeError, ValueError):
                line = 1
            if (rule, line) in existing:
                continue
            known_rule = RULES.get(rule)
            merged.append(
                Finding(
                    rule=rule,
                    severity=severity,
                    line=max(1, line),
                    message=str(raw.get("message") or "").strip()[:300],
                    symbol=str(raw.get("symbol") or "")[:60],
                    suggestion=str(raw.get("suggestion") or "").strip()[:300],
                    fixable=False,
                    source="llm",
                    extra={"rule_title": known_rule.title if known_rule else "语义问题"},
                )
            )
        return merged[:20]

    # ------------------------------------------------------------------ 渲染
    def _render_markdown(
        self,
        filename: str,
        findings: list[Finding],
        summary: dict[str, int],
        round_no: int,
    ) -> str:
        lines = [
            f"## 第 {round_no} 轮审查意见 · `{filename}`",
            "",
            f"- 问题总数：**{len(findings)}**（高 {summary['high']} / 中 {summary['medium']} / 低 {summary['low']}）",
            f"- 数据来源：静态规则 {sum(1 for f in findings if f.source == 'static')} 条"
            + ("，模型补充 " + str(sum(1 for f in findings if f.source == "llm")) + " 条" if any(f.source == "llm" for f in findings) else ""),
            "",
        ]
        if not findings:
            lines.append("未发现问题。")
            return "\n".join(lines)

        lines.extend(["| # | 严重度 | 规则 | 位置 | 问题 | 建议 |", "|---|--------|------|------|------|------|"])
        for index, finding in enumerate(findings, start=1):
            where = f"L{finding.line}" + (f" `{finding.symbol}`" if finding.symbol else "")
            message = finding.message.replace("|", "\\|")
            suggestion = (finding.suggestion or "").replace("|", "\\|")
            lines.append(
                f"| {index} | {SEVERITY_LABEL[finding.severity]} | `{finding.rule}` | {where} | {message} | {suggestion} |"
            )
        return "\n".join(lines)
