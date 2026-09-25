"""极简 HTML 渲染工具：转义、布局、表格、表单（服务端渲染，零前端构建链）。

与站点产物（`publish/site.py`、`report/html.py`）同一路子：HTML 由 Python 生成，
样式内联在页面里，**没有前端构建、没有第三方脚本**（NFR-A-2 / NFR-P-3）。

两条纪律：

1. **默认转义**：函数收到的普通 `str` 一律 `html.escape` 后再拼进页面；要插自己的标记时
   显式用 `raw()`（名字就是提醒）。这样「往页面里塞用户数据」不会变成 XSS。
2. **一个组件只做一件事**：`card` 管卡片、`table` 管表格、`form` 管表单。
   页面只负责把事实排成这些组件，不自己拼 `<div>`（改样式只改这一处）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape as _escape
from typing import Iterable, Sequence

#: 已经转义好的 HTML 片段（`table` 里放链接、标签这类要自己拼的单元格时用它）。
class Html(str):
    """一段**可信**的 HTML（已经转义过，或是本工具箱生成的标记）。"""


def raw(markup: str) -> Html:
    """标记一段已经安全的 HTML（只在本模块与页面里拼标记时用）。"""
    return Html(markup)


def esc(value: object) -> Html:
    """转义任意值 → 可安全插进页面的片段。"""
    return Html(_escape("" if value is None else str(value)))


def _safe(value: object) -> Html:
    return value if isinstance(value, Html) else esc(value)


def _cell(value: object) -> Html:
    return raw(f"<td>{_safe(value)}</td>")


# —— 布局 ——

CSS = """
:root { --line: #e2e6ea; --ink: #24292f; --muted: #667085; --accent: #1f6feb; }
* { box-sizing: border-box; }
body { margin: 0; color: var(--ink); background: #f6f7f9; font: 15px/1.6 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif; }
a { color: var(--accent); }
header.admin { background: #101828; color: #fff; padding: 12px 20px; }
header.admin h1 { margin: 0; font-size: 17px; display: inline-block; }
header.admin span.who { float: right; font-size: 13px; color: #cbd5e1; }
nav.admin { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 10px 20px; background: #fff; border-bottom: 1px solid var(--line); font-size: 14px; }
nav.admin a { padding: 3px 8px; border-radius: 6px; text-decoration: none; }
nav.admin a.active { background: var(--accent); color: #fff; }
main { padding: 16px 20px 40px; max-width: 1200px; }
section.card { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; margin: 0 0 16px; }
section.card > h2 { margin: 0 0 10px; font-size: 15px; }
p.note, p.empty { color: var(--muted); font-size: 13px; margin: 6px 0; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; white-space: nowrap; }
td code, p code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
form { margin: 8px 0 0; display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: flex-end; }
form p.field { margin: 0; display: flex; flex-direction: column; gap: 3px; }
form p.field > label { font-size: 12px; color: var(--muted); }
input[type=text], input[type=number], input[type=password], select, textarea {
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; font: inherit; font-size: 13px; min-width: 120px; }
textarea { min-width: 420px; min-height: 60px; }
button { border: 0; border-radius: 6px; background: var(--accent); color: #fff; padding: 6px 12px; font: inherit; font-size: 13px; cursor: pointer; }
button.ghost { background: #475467; }
.notice { border-radius: 6px; padding: 8px 12px; margin: 0 0 14px; font-size: 13px; }
.notice.ok { background: #ecfdf3; border: 1px solid #abefc6; }
.notice.err { background: #fef3f2; border: 1px solid #fecdca; }
.tag { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 999px; background: #eef2f6; }
.tag.critical { background: #fee4e2; color: #912018; }
.tag.warning { background: #fef0c7; color: #93370d; }
.tag.ok { background: #d1fadf; color: #054f31; }
.stat { display: flex; flex-wrap: wrap; gap: 10px; }
.stat div { border: 1px solid var(--line); border-radius: 8px; padding: 8px 14px; background: #fff; min-width: 150px; }
.stat b { display: block; font-size: 20px; }
.stat span { color: var(--muted); font-size: 12px; }
"""


def tag(label: str, kind: str = "") -> Html:
    return raw(f'<span class="tag {esc(kind)}">{esc(label)}</span>')


def severity_tag(severity: str) -> Html:
    css = {"critical": "critical", "warning": "warning", "info": "ok"}.get(severity, "")
    return tag(severity, css)


def link(href: str, label: object) -> Html:
    return raw(f'<a href="{esc(href)}">{_safe(label)}</a>')


def layout(
    title: str,
    body: Html | str,
    *,
    nav: Sequence[tuple[str, str]],
    active: str = "",
    notice: tuple[str, str] | None = None,
) -> Html:
    """整页 HTML：页头 + 固定导航 + 内容（`notice` = `(ok|err, 消息)`）。"""
    nav_html = "".join(
        f'<a href="{esc(self_path)}"{" class=\"active\"" if self_path == active else ""}>{esc(label)}</a>'
        for self_path, label in nav
    )
    banner = ""
    if notice is not None:
        kind, message = notice
        banner = f'<div class="notice {esc(kind)}">{_safe(message)}</div>'
    return raw(
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{esc(title)}｜弹幕情报库后台</title><style>{CSS}</style></head><body>"
        f'<header class="admin"><h1>弹幕情报库后台</h1><span class="who">{esc(title)}</span></header>'
        f'<nav class="admin">{nav_html}<a href="/admin/logout">退出登录</a></nav>'
        f'<main>{banner}{_safe(body)}</main></body></html>'
    )


def card(title: str, body: Html | str, *, note: str | None = None) -> Html:
    footnote = f'<p class="note">{esc(note)}</p>' if note else ""
    return raw(f'<section class="card"><h2>{esc(title)}</h2>{_safe(body)}{footnote}</section>')


def table(headers: Sequence[str], rows: Iterable[Sequence[object]], *, empty: str = "暂无数据") -> Html:
    """一张表：表头 + 每行（单元格里放 `Html` 就当作已转义的标记，放 `str` 则转义）。"""
    materialised = list(rows)
    if not materialised:
        return raw(f'<p class="empty">{esc(empty)}</p>')
    head = "".join(f"<th>{esc(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(_cell(cell) for cell in row) + "</tr>" for row in materialised
    )
    return raw(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


def stats(pairs: Sequence[tuple[str, object]]) -> Html:
    """一排数字卡片（概览与成本页用）。"""
    cells = "".join(f"<div><b>{_safe(value)}</b><span>{esc(label)}</span></div>" for label, value in pairs)
    return raw(f'<div class="stat">{cells}</div>')


def paragraphs(items: Sequence[object]) -> Html:
    if not items:
        return raw('<p class="empty">无</p>')
    return raw("".join(f"<p>{_safe(item)}</p>" for item in items))


@dataclass(frozen=True, slots=True)
class Field:
    """一个表单字段（`kind`：text | number | password | select | textarea | hidden | checkbox）。"""

    name: str
    label: str = ""
    value: object = ""
    kind: str = "text"
    options: tuple[tuple[str, str], ...] = ()
    step: str = "any"
    placeholder: str = ""
    rows: int = 4
    required: bool = False

    def render(self) -> Html:
        required = " required" if self.required else ""
        if self.kind == "hidden":
            return raw(f'<input type="hidden" name="{esc(self.name)}" value="{esc(self.value)}">')
        if self.kind == "textarea":
            return raw(
                f'<p class="field"><label for="{esc(self.name)}">{esc(self.label)}</label>'
                f'<textarea id="{esc(self.name)}" name="{esc(self.name)}" rows="{self.rows}"'
                f' placeholder="{esc(self.placeholder)}"></textarea></p>'
            )
        if self.kind == "checkbox":
            checked = " checked" if self.value else ""
            return raw(
                f'<p class="field"><label for="{esc(self.name)}">{esc(self.label)}</label>'
                f'<input type="checkbox" id="{esc(self.name)}" name="{esc(self.name)}" value="1"{checked}></p>'
            )
        if self.kind == "select":
            options = "".join(
                f'<option value="{esc(value)}"{" selected" if str(value) == str(self.value) else ""}>'
                f"{esc(label)}</option>"
                for value, label in self.options
            )
            control = f'<select id="{esc(self.name)}" name="{esc(self.name)}"{required}>{options}</select>'
        else:
            control = (
                f'<input type="{esc(self.kind)}" id="{esc(self.name)}" name="{esc(self.name)}"'
                f' value="{esc(self.value)}" step="{esc(self.step)}"'
                f' placeholder="{esc(self.placeholder)}"{required}>'
            )
        return raw(
            f'<p class="field"><label for="{esc(self.name)}">{esc(self.label)}</label>{control}</p>'
        )


def form(
    action: str,
    fields_: Sequence[Field],
    *,
    submit: str = "保存",
    danger: bool = False,
) -> Html:
    """一个 POST 表单（写操作一律 POST：GET 只读，不被爬虫/预取误触发）。"""
    body = "".join(field_.render() for field_ in fields_)
    css = " ghost" if danger else ""
    return raw(
        f'<form method="post" action="{esc(action)}">{body}'
        f'<button class="{css.strip()}" type="submit">{esc(submit)}</button></form>'
    )


def inline_form(action: str, fields_: Sequence[Field], *, submit: str) -> Html:
    """行内表单（表格里的「升级/作废/撤权」这类单行动作）。"""
    return form(action, fields_, submit=submit, danger=True)


def json_block(value: object, *, limit: int = 600) -> Html:
    """一段 JSON（审计详情、通知 payload 这类结构体在页面上按原文看）。"""
    import json

    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return raw(f"<code>{esc(text)}</code>")


def stamp(ts_ms: int | None) -> str:
    """毫秒时间戳 → 本机时间的可读串（没有就如实写 `-`）。"""
    if not ts_ms:
        return "-"
    from datetime import datetime

    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def duration(ms: int | None) -> str:
    if ms is None:
        return "-"
    seconds = ms / 1000
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.0f} 天"


@dataclass(frozen=True, slots=True)
class Table:
    """表格的行收集器（页面里 `rows = Table(); rows.add(...)` 比拼字符串清楚）。"""

    headers: tuple[str, ...]
    items: list[tuple[object, ...]] = field(default_factory=list)

    def add(self, *cells: object) -> None:
        self.items.append(cells)

    def render(self, *, empty: str = "暂无数据") -> Html:
        return table(self.headers, self.items, empty=empty)
