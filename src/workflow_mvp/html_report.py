"""把运行报告转成一份自包含的 HTML。

为什么不直接让人看 Markdown：很多人机器上的 ``.md`` 没有关联程序，双击只会弹
"选择打开方式"。而报告是要给人看的——表格、代码 diff、流程图，浏览器里直接
打开最省事。

这里只实现报告真正用到的语法子集（标题 / 表格 / 围栏代码 / 列表 / 引用 / 分隔线 /
行内 ``code``、粗体、链接），因此没有引入任何 Markdown 库，也不会因为语法差异
渲染歪掉。
"""

from __future__ import annotations

import html
import re
from typing import Iterable

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 40px 20px 80px;
  font: 15px/1.75 -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  color: #1f2328; background: #f6f8fa;
}
main { max-width: 980px; margin: 0 auto; background: #fff; padding: 40px 48px 56px;
       border: 1px solid #d0d7de; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }
h1 { font-size: 26px; margin: 0 0 8px; padding-bottom: 12px; border-bottom: 1px solid #d8dee4; }
h2 { font-size: 20px; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 1px solid #eaeef2; }
h3 { font-size: 16px; margin: 24px 0 8px; }
p, li { margin: 8px 0; }
blockquote { margin: 12px 0; padding: 10px 16px; color: #57606a; background: #f6f8fa;
             border-left: 4px solid #d0d7de; border-radius: 0 6px 6px 0; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
th, td { border: 1px solid #d0d7de; padding: 7px 10px; text-align: left; vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; white-space: nowrap; }
tr:nth-child(even) td { background: #fbfcfd; }
code { font-family: "Cascadia Code", Consolas, "Courier New", monospace; font-size: 13px;
       background: #eff2f5; padding: 1px 5px; border-radius: 4px; }
pre { background: #0d1117; color: #e6edf3; padding: 14px 18px; border-radius: 8px;
      overflow-x: auto; margin: 14px 0; }
pre code { background: none; padding: 0; color: inherit; font-size: 12.5px; line-height: 1.6; }
pre.diff code { color: #e6edf3; }
hr { border: 0; border-top: 1px solid #d8dee4; margin: 32px 0; }
a { color: #0969da; }
.stat { display: inline-block; }
footer { max-width: 980px; margin: 16px auto 0; color: #8b949e; font-size: 12px; text-align: center; }
"""

# 报告里会出现 → ｜ 这类符号，等宽字体下更整齐
CODE_LANGS = {"text", "diff", "bash", "python", "json", "mermaid"}


def _inline(text: str) -> str:
    """行内元素：先转义，再还原 **粗体** / `code` / [链接](url)。"""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def _split_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    cells: list[str] = []
    current = ""
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body) and body[index + 1] == "|":
            current += "|"
            index += 2
            continue
        if char == "|":
            cells.append(current.strip())
            current = ""
        else:
            current += char
        index += 1
    cells.append(current.strip())
    return cells


def _is_table_separator(line: str) -> bool:
    return bool(re.match(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$", line.strip()))


def _table(header: list[str], rows: Iterable[list[str]]) -> str:
    parts = ["<table>", "<thead><tr>"]
    parts.extend(f"<th>{_inline(cell)}</th>" for cell in header)
    parts.append("</tr></thead><tbody>")
    for row in rows:
        cells = row + [""] * (len(header) - len(row))
        parts.append("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in cells[: len(header)]) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def render(markdown: str, *, title: str = "运行报告") -> str:
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]

        # 围栏代码块
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip() or "text"
            index += 1
            buffer: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                buffer.append(lines[index])
                index += 1
            index += 1
            css = lang if lang in CODE_LANGS else "text"
            out.append(f'<pre class="{css}"><code>{html.escape(chr(10).join(buffer))}</code></pre>')
            continue

        # 表格
        if line.strip().startswith("|") and index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
            header = _split_row(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_split_row(lines[index]))
                index += 1
            out.append(_table(header, rows))
            continue

        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped == "---":
            out.append("<hr>")
            index += 1
            continue
        if stripped.startswith("### "):
            out.append(f"<h3>{_inline(stripped[4:])}</h3>")
            index += 1
            continue
        if stripped.startswith("## "):
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
            index += 1
            continue
        if stripped.startswith("# "):
            out.append(f"<h1>{_inline(stripped[2:])}</h1>")
            index += 1
            continue
        if stripped.startswith("> "):
            quotes: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quotes.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append(f"<blockquote>{_inline(' '.join(quotes))}</blockquote>")
            continue
        if re.match(r"^[-*]\s+", stripped):
            items: list[str] = []
            while index < len(lines) and re.match(r"^[-*]\s+", lines[index].strip()):
                items.append(re.sub(r"^[-*]\s+", "", lines[index].strip()))
                index += 1
            out.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + "</ul>")
            continue
        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while index < len(lines) and re.match(r"^\d+\.\s+", lines[index].strip()):
                items.append(re.sub(r"^\d+\.\s+", "", lines[index].strip()))
                index += 1
            out.append("<ol>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + "</ol>")
            continue

        out.append(f"<p>{_inline(stripped)}</p>")
        index += 1

    body = "\n".join(out)
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>{html.escape(title)}</title>\n"
        f"<style>{CSS}</style>\n</head>\n<body>\n<main>\n{body}\n</main>\n"
        "<footer>由 workflow-mvp 自动生成 · 同一目录下的 trace.json / messages.json 是机器可读版本</footer>\n"
        "</body>\n</html>\n"
    )


def write(markdown: str, path, *, title: str = "运行报告"):
    from pathlib import Path

    target = Path(path)
    target.write_text(render(markdown, title=title), encoding="utf-8")
    return target
