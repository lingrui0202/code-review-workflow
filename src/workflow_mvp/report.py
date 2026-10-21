"""报告生成：把一次运行变成一份能直接交给老师/同事看的 Markdown。

报告不是"日志堆砌"，它回答四个问题：
1. 这次跑成了什么（结论）；
2. 流程是怎么走的（结构 + 时间线）；
3. Agent 之间到底说了什么（协作记录）；
4. 代码到底哪里变了、还剩什么（差异 + 遗留）。

同时落一份机器可读的 ``trace.json``，方便以后做统计或接可视化。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .review.finding import SEVERITY_LABEL, from_dicts
from .workflow.engine import RunResult
from .workflow.graph import Graph
from .workspace import Workspace

STATUS_LABEL = {
    "completed": "完成",
    "failed": "失败",
    "aborted": "中止",
    "running": "运行中",
}


@dataclass
class ReportContext:
    result: RunResult
    graph: Graph
    settings: Any = None
    workspace: Workspace | None = None


def _md_cell(text: str, limit: int = 80) -> str:
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    return flat.replace("|", "\\|")


def _fmt_duration(result: RunResult) -> str:
    steps = result.steps
    total = sum(step.duration_ms for step in steps)
    pretty = f"{total}ms" if total < 1000 else f"{total / 1000:.1f}s"
    return f"{pretty}（{len(steps)} 步）"


def render_report(ctx: ReportContext) -> str:
    result = ctx.result
    state = result.state
    workspace = ctx.workspace or Workspace(state.get("workspace") or ".")
    history = state.get("findings_history") or []
    first = history[0] if history else {}
    last = history[-1] if history else {}
    final_findings = from_dicts(state.get("findings") or [])
    manual = state.get("manual_todo") or []
    fix_log = state.get("fix_log") or []
    applied = [item for item in fix_log if item.get("status") == "applied"]
    skipped = [item for item in fix_log if item.get("status") != "applied"]
    blocking_now = sum(1 for f in final_findings if f.severity in ("high", "medium"))

    lines: list[str] = []
    lines.append("# 代码审查与修复工作流 · 运行报告")
    lines.append("")
    lines.append(f"> run_id：`{result.run_id}` ｜ 状态：**{STATUS_LABEL.get(result.status, result.status)}** ｜ "
                 f"耗时：{_fmt_duration(result)} ｜ 结束原因：{result.stop_reason}")
    lines.append("")

    # ---------------------------------------------------------------- 结论
    lines.append("## 1. 结论")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 初始问题 | {first.get('total', '—')} 处（高 {first.get('high', '—')} / 中 {first.get('medium', '—')} / 低 {first.get('low', '—')}） |")
    lines.append(f"| 最终问题 | {len(final_findings)} 处（高 {last.get('high', 0)} / 中 {last.get('medium', 0)} / 低 {last.get('low', 0)}） |")
    lines.append(f"| 审查轮次 | {len(history)} |")
    lines.append(f"| 自动修复 | {len(applied)} 处 |")
    lines.append(f"| 跳过/需人工 | {len(skipped)} 处 |")
    lines.append(f"| 最终裁决 | **{'通过（无高/中风险）' if blocking_now == 0 else '需人工复核'}** |")
    lines.append("")
    if first.get("total") is not None and last:
        delta = int(first.get("total", 0)) - int(last.get("total", 0))
        if delta > 0:
            lines.append(f"审查发现的 {delta} 处问题已被自动修复消除，剩余部分按严重度列在下方。")
        elif delta == 0 and len(applied):
            lines.append("问题总数没有下降：修复改动了代码，但引入了等量的新问题，详见修复记录。")
        else:
            lines.append("本轮没有产生可自动修复的改动，剩余问题需要人工判断。")
        lines.append("")

    # ---------------------------------------------------------------- 结构
    lines.append("## 2. 工作流结构")
    lines.append("")
    lines.append("```text")
    lines.append(ctx.graph.to_ascii())
    lines.append("```")
    lines.append("")
    lines.append("```mermaid")
    lines.append(ctx.graph.to_mermaid())
    lines.append("```")
    lines.append("")

    # ---------------------------------------------------------------- 时间线
    lines.append("## 3. 执行时间线")
    lines.append("")
    lines.append("| # | 节点 | 状态 | 轮次 | 耗时 | 说明 | 状态变更 |")
    lines.append("|---|------|------|------|------|------|----------|")
    for step in result.steps:
        changed = "、".join(f"`{key}`" for key in step.changed) or "—"
        lines.append(
            f"| {step.index} | `{step.node}` | {step.status} | {step.iteration} | {step.duration_ms}ms | "
            f"{_md_cell(step.note or '—', 40)} | {_md_cell(changed, 60)} |"
        )
    lines.append("")

    # ---------------------------------------------------------------- 轮次
    if history:
        lines.append("## 4. 每轮审查结果")
        lines.append("")
        lines.append("| 轮次 | 问题总数 | 高 | 中 | 低 | 裁决 |")
        lines.append("|------|----------|----|----|----|------|")
        for item in history:
            lines.append(
                f"| 第 {item.get('round')} 轮 | {item.get('total')} | {item.get('high')} | "
                f"{item.get('medium')} | {item.get('low')} | {item.get('verdict')} |"
            )
        lines.append("")

    # ---------------------------------------------------------------- 协作
    lines.append("## 5. Agent 协作记录")
    lines.append("")
    messages = result.bus.history
    if messages:
        lines.append("| 轮次 | 类型 | 发送方 → 接收方 | 内容摘要 |")
        lines.append("|------|------|------------------|----------|")
        for message in messages:
            lines.append(
                f"| {message.iteration} | `{message.kind}` | {message.sender} → {message.recipient} | "
                f"{_md_cell(message.content)} |"
            )
        stats = result.bus.stats()
        lines.append("")
        lines.append(
            f"共 {stats['total']} 条消息：" + "，".join(f"{kind} {count}" for kind, count in stats["by_kind"].items())
        )
    else:
        lines.append("（本次运行没有产生消息）")
    lines.append("")

    # ---------------------------------------------------------------- 修复
    lines.append("## 6. 自动修复明细")
    lines.append("")
    if fix_log:
        lines.append("| 轮次 | 规则 | 位置 | 结果 | 说明 |")
        lines.append("|------|------|------|------|------|")
        for item in fix_log:
            location = f"L{item.get('line')}" + (f" `{item.get('symbol')}`" if item.get("symbol") else "")
            icon = {"applied": "✅ 已改", "skipped": "⏭ 跳过", "rejected": "⛔ 回滚"}.get(str(item.get("status")), str(item.get("status")))
            lines.append(
                f"| {item.get('round', '—')} | `{item.get('rule')}` | {location} | {icon} | "
                f"{_md_cell(item.get('detail') or '', 60)} |"
            )
    else:
        lines.append("（没有产生修复记录）")
    lines.append("")

    # ---------------------------------------------------------------- 遗留
    lines.append("## 7. 遗留问题（需人工处理）")
    lines.append("")
    remaining = [f for f in final_findings if f.severity in ("high", "medium")]
    if remaining:
        lines.append("| 严重度 | 规则 | 位置 | 问题 | 建议 |")
        lines.append("|--------|------|------|------|------|")
        for finding in remaining:
            where = f"L{finding.line}" + (f" `{finding.symbol}`" if finding.symbol else "")
            lines.append(
                f"| {SEVERITY_LABEL[finding.severity]} | `{finding.rule}` | {where} | "
                f"{_md_cell(finding.message, 70)} | {_md_cell(finding.suggestion, 60)} |"
            )
        lines.append("")
        lines.append("这些问题的改法依赖业务语义（比如异常该重抛还是该降级、函数该拆成哪几块），")
        lines.append("工具不做猜测，留给人工判断。")
    else:
        lines.append("没有高/中风险遗留问题。")
    if manual:
        lines.append("")
        lines.append(f"（修复阶段标记为需人工的问题共 {len(manual)} 条，已包含在上表中。）")
    lines.append("")

    # ---------------------------------------------------------------- 差异
    lines.append("## 8. 代码差异")
    lines.append("")
    code_history = state.get("code_history") or []
    diff = ""
    if len(code_history) >= 2:
        diff = workspace.diff(code_history[0]["code"], code_history[-1]["code"], state.get("code_path") or "code.py")
    if diff:
        lines.append("```diff")
        lines.append(diff)
        lines.append("```")
    else:
        lines.append("（本次运行没有产生代码改动）")
    lines.append("")

    # ---------------------------------------------------------------- 状态
    lines.append("## 9. 状态快照（关键键）")
    lines.append("")
    lines.append("| key | 值 |")
    lines.append("|-----|-----|")
    for key in ("code_path", "code_origin", "iterations", "verdict", "findings_fingerprint", "max_rounds"):
        if key in state:
            lines.append(f"| `{key}` | {_md_cell(str(state.get(key)), 60)} |")
    if ctx.settings is not None:
        lines.append("")
        lines.append("运行配置：")
        lines.append("")
        lines.append("| 项 | 值 |")
        lines.append("|----|----|")
        for key, value in ctx.settings.masked().items():
            lines.append(f"| `{key}` | {_md_cell(str(value), 50)} |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("报告由 `python main.py run` 自动生成；同一目录下的 `trace.json` / `messages.json` / "
                 "`diff.patch` 是同一份数据的机器可读版本。")
    return "\n".join(lines)


# ---------------------------------------------------------------------- 落盘


def write_artifacts(
    ctx: ReportContext,
    run_dir: Path,
) -> dict[str, Path]:
    """把报告与全部可回溯产物写到 run 目录。"""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    state = ctx.result.state
    written: dict[str, Path] = {}

    report = render_report(ctx)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    written["report"] = run_dir / "report.md"

    # 浏览器直接打开的版本（.md 在多数机器上没关联程序）
    from .html_report import render as render_html

    (run_dir / "report.html").write_text(render_html(report, title="代码审查工作流 · 运行报告"), encoding="utf-8")
    written["html"] = run_dir / "report.html"

    (run_dir / "trace.json").write_text(
        json.dumps(ctx.result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written["trace"] = run_dir / "trace.json"

    (run_dir / "state.json").write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written["state"] = run_dir / "state.json"

    (run_dir / "messages.json").write_text(
        json.dumps(ctx.result.bus.transcript(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written["messages"] = run_dir / "messages.json"

    review = state.get("review_markdown")
    if review:
        (run_dir / "review.md").write_text(review + "\n", encoding="utf-8")
        written["review"] = run_dir / "review.md"

    history = state.get("code_history") or []
    if len(history) >= 2:
        workspace = ctx.workspace or Workspace(state.get("workspace") or ".")
        diff = workspace.diff(history[0]["code"], history[-1]["code"], state.get("code_path") or "code.py")
        if diff:
            (run_dir / "diff.patch").write_text(diff + "\n", encoding="utf-8")
            written["diff"] = run_dir / "diff.patch"

    final_code = state.get("code")
    if final_code:
        (run_dir / "final_code.py").write_text(final_code, encoding="utf-8")
        written["final_code"] = run_dir / "final_code.py"

    return written
