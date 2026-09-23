"""静态报告页渲染（设计 §10.1 / §11）—— 输入是 `ReportContent`，不再碰统计。

一条硬规则：**每一项事实都带可展开的来源**（文件 + 行范围 + SHA256），
读者可自己复核。事实段与解读段在样式与标注上可区分（需求 §6.9 第 2 条）。
无外部脚本、无外部字体、无第三方请求（NFR-A-2 / NFR-P-3）。
"""

from __future__ import annotations

import re
from html import escape

from danmu_intel.common.sources import SourceRef
from danmu_intel.report.assemble import ReportContent
from danmu_intel.report.forms import LLM_STATE_LLM, form_of
from danmu_intel.report.rule_render import format_ts
from danmu_intel.report.segments import Segment

# 段性质（需求 §6.6「内容性质」原文）→ 页面样式类
NATURE_CLASSES = {
    "事实": "fact",
    "事实 + 解读": "fact-interpretation",
    "解读": "interpretation",
    "事实（风险提示）": "fact-gray",
}

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
.degraded { margin: 12px 0 0; padding: 10px 12px; border-radius: 6px;
  background: #fffaf0; border: 1px solid #f6ad55; color: #7b341e; font-weight: 600; }
"""


SOURCE_ITEM_RE = re.compile(
    r"<li><code>(?P<rel_path>[^<]+)</code>\s*第\s*(?P<line_start>\d+)\u2013(?P<line_end>\d+)\s*行\s*·\s*"
    r"SHA256\s*<code>(?P<sha256>[0-9a-f]{64})</code></li>"
)


def parse_sources(html: str) -> list[SourceRef]:
    """从**已生成的页面**里取回冻结的来源引用。

    校验必须对着产物里记下的哈希来比对当前文件，而不是现算一遍——
    现算等于把「证据有没有被改过」这个问题问成了「文件现在长什么样」。
    """
    return [
        SourceRef(
            rel_path=match.group("rel_path"),
            line_start=int(match.group("line_start")),
            line_end=int(match.group("line_end")),
            sha256=match.group("sha256"),
        )
        for match in SOURCE_ITEM_RE.finditer(html)
    ]


def nature_class(nature: str) -> str:
    """段性质 → CSS 类名（样式表按性质区分事实段与解读段）。"""
    return NATURE_CLASSES.get(nature, "fact")


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
    kind_class = nature_class(segment.nature)
    return (
        f'<section class="seg seg--{escape(kind_class)}" id="seg-{segment.no}">'
        f'<h2><span>{segment.no}</span> {escape(segment.title)} '
        f'<span class="kind kind--{escape(kind_class)}">{escape(segment.nature)}</span></h2>'
        f'<div class="body">{_render_body(segment.body)}</div>'
        f"{_render_sources(segment.sources)}"
        "</section>"
    )


def render_report_html(content: ReportContent) -> str:
    """渲染完整页面。段落由 `assemble.build_content` 给出（已保证形态段集齐备）。"""
    form = form_of(content.kind)
    meta = content.meta
    title = f"{meta['league']} {meta['match_title']} 情报（{form.label}）"
    toc = "\n".join(
        f'<li><a href="#seg-{segment.no}">{segment.no} {escape(segment.title)}</a></li>'
        for segment in content.segments
    )
    body = "\n".join(_segment_html(segment) for segment in content.segments)
    excluded = meta.get("excluded_games") or []
    excluded_note = (
        f"｜未纳入（进行中）：{'、'.join(f'G{no}' for no in excluded)}" if excluded else ""
    )
    meta_line = (
        f"{meta['league']}｜{meta['match_title']}｜状态 {meta['state']}｜"
        f"覆盖节点 {'、'.join(f'G{no}' for no in meta['covered_games']) or '无'}{excluded_note}｜"
        f"弹幕 {meta['danmu_count']} 条｜算法版本 {meta['algo_version']}"
    )
    interpretation = [segment for segment in content.segments if segment.has_interpretation]
    degraded = content.llm_state != LLM_STATE_LLM
    note = str(meta.get("llm_note") or "")
    banner = (
        '<p class="degraded">⚠ 解读能力降级：本次报告的解读段由规则直出'
        + (f"（{escape(note)}）" if note else "")
        + "；事实段不受影响。</p>"
        if degraded
        else ""
    )
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
<p class="meta">{escape(meta_line)}</p>
<p class="meta">报告形态 {escape(form.kind)}｜版本 v{content.version}｜
生成时间 {escape(format_ts(content.generated_at))}｜事实层哈希 {escape(content.fact_layer_hash)}</p>
<p class="meta">本页标注「解读」的 {len(interpretation)} 段是分析而非事实；标注「事实」的段落逐项附来源。</p>
{banner}
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
