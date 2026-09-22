"""静态报告页渲染（设计 §10.1 / §11）。

一条硬规则：**每一项事实都带可展开的来源**（文件 + 行范围 + SHA256），
读者可自己复核。事实段与解读段在样式与标注上可区分（需求 §6.9 第 2 条）。
无外部脚本、无外部字体、无第三方请求（NFR-A-2 / NFR-P-3）。
"""

from __future__ import annotations

from html import escape

from danmu_intel.common.sources import SourceRef
from danmu_intel.report.facts import MatchFacts
from danmu_intel.report.segments import KIND_LABELS, KIND_INTERPRETATION, KIND_FACT_INTERPRETATION, Segment

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; max-width: 880px; margin-inline: auto;
  font: 16px/1.7 -apple-system, "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  color: #1a1a1a; background: #fafafa; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 0 0 8px; display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
p { margin: 0 0 8px; }
.meta { color: #666; font-size: 14px; }
.toc { background: #fff; border: 1px solid #e5e5e5; border-radius: 8px; padding: 12px 16px; margin: 16px 0; }
.toc ol { margin: 0; padding-left: 20px; }
.toc a { color: inherit; }
.seg { background: #fff; border: 1px solid #e5e5e5; border-radius: 8px; padding: 16px; margin: 0 0 16px; }
.seg--interpretation, .seg--fact-interpretation { border-left: 4px solid #b7791f; }
.seg--fact, .seg--fact-gray { border-left: 4px solid #2c7a7b; }
.kind { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px; }
.kind--fact { background: #e6fffa; color: #234e52; }
.kind--fact-gray { background: #fffaf0; color: #7b341e; }
.kind--interpretation, .kind--fact-interpretation { background: #fefcbf; color: #744210; }
.body p { white-space: pre-wrap; }
details { margin-top: 12px; font-size: 13px; }
summary { cursor: pointer; color: #2b6cb0; }
details ul { margin: 8px 0 0; padding-left: 20px; }
code { word-break: break-all; font-size: 12px; }
footer { color: #666; font-size: 13px; padding: 16px 0 32px; }
"""


def _render_sources(refs: tuple[SourceRef, ...]) -> str:
    if not refs:
        return '<p class="meta">本段没有需要引用的原始记录。</p>'
    items = "\n".join(
        f"<li><code>{escape(ref.rel_path)}</code> 第 {ref.line_start}–{ref.line_end} 行 · "
        f"SHA256 <code>{escape(ref.sha256)}</code></li>"
        for ref in refs
    )
    return (
        f'<details><summary>来源（{len(refs)} 项：文件 + 行范围 + SHA256）</summary>'
        f"<ul>{items}</ul></details>"
    )


def _render_body(body: str) -> str:
    paragraphs = [escape(part) for part in body.split("\n")]
    return "\n".join(f"<p>{part}</p>" for part in paragraphs if part)


def _segment_html(segment: Segment) -> str:
    kind_class = segment.kind.replace("+", "-").replace("(", "-").replace(")", "")
    label = KIND_LABELS.get(segment.kind, segment.kind)
    return (
        f'<section class="seg seg--{escape(kind_class)}" id="seg-{segment.no}">'
        f'<h2><span>{segment.no}</span> {escape(segment.title)} '
        f'<span class="kind kind--{escape(kind_class)}">{escape(label)}</span></h2>'
        f'<div class="body">{_render_body(segment.body)}</div>'
        f"{_render_sources(segment.sources)}"
        "</section>"
    )


def render_html(facts: MatchFacts) -> str:
    """渲染完整页面。段落由 `build_report` 给出（已保证十一段齐备）。"""
    from danmu_intel.report.rule_render import build_report, format_ts

    segments = build_report(facts)
    match = facts.match
    title = f"{match.league} {match.title} 情报"
    toc = "\n".join(
        f'<li><a href="#seg-{segment.no}">{segment.no} {escape(segment.title)}</a></li>'
        for segment in segments
    )
    body = "\n".join(_segment_html(segment) for segment in segments)
    meta = (
        f"{match.league}｜{match.title}｜状态 {match.state}｜"
        f"弹幕 {len(facts.all_lines)} 条｜算法版本 {facts.algo_version}"
    )
    interpretation = [
        segment for segment in segments if segment.kind in (KIND_INTERPRETATION, KIND_FACT_INTERPRETATION)
    ]
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<header>
<h1>{escape(title)}</h1>
<p class="meta">{escape(meta)}</p>
<p class="meta">生成时间 {escape(format_ts(facts.generated_at))}｜
本页标注「解读」的 {len(interpretation)} 段是分析而非事实；标注「事实」的段落逐项附来源。</p>
</header>
<nav class="toc"><ol>
{toc}
</ol></nav>
<main>
{body}
</main>
<footer>数据来源为公开弹幕；原始记录存于仓库外数据目录，逐项可用 SHA256 复核。</footer>
</body>
</html>
"""
