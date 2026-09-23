"""站点产物：一次发布写出的整棵站点树（设计 §11.1，ADR-0015 决策 1 / 决策 9）。

页面清单与职责：

| 页面 | 路径 | 内容 |
|---|---|---|
| 索引 | `index.html` | 最新报告、按联赛与比赛入口、栏目、发布纪律 |
| 历史情报库 | `history/index.html` | 按日期 / 联赛 / 队伍 / 比赛四个入口分面浏览全部报告 |
| 联赛页 | `leagues/<league>.html` | 该联赛的比赛列表 |
| 比赛页 | `matches/<id>/index.html` | 对阵、状态、可见性、该场已发布形态入口、原始记录规模 |
| 报告页 | `matches/<id>/<kind>.html` | T5 的报告页；付费时不写任何段正文 |
| 画像库 | `profile/index.html` + `profile/teams/<slug>.html` + `profile/players/<slug>.html` | 队伍与人员的长期表现摘要（只由官方数据汇总） |
| 灰信号页 | `gray/index.html` | 达门槛灰信号（必附样本、零身份、不指控） |
| 验证闭环页 | `verification/index.html` | 已发布报告 + 事实层哈希 + 来源复核结果 + 官方结果 |
| 订阅页 | `subscribe.html` | 免费 / 付费边界（需求 §6.10 原文）与访问方式 |

三条纪律：

1. **页面只由库里的事实汇总**，没有数据就不造页（选手页只在官方阵容存在时生成）；
2. **导航是同一组固定栏目**（`NAV_ITEMS`，值是**相对站点根**的目标路径），
   页面渲染时换算成相对本页的链接（`relative_href`）；
3. 无脚本、无外部字体、无第三方请求（NFR-A-2 / NFR-P-3）：页面上只有 HTML + 内联样式。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path

from danmu_intel.common import official, paywall
from danmu_intel.common.matches import Match, list_matches
from danmu_intel.common.sources import SourceRef, verify
from danmu_intel.report.assemble import ReportContent
from danmu_intel.report.forms import form_of
from danmu_intel.report.html import CSS as REPORT_CSS
from danmu_intel.report.html import render_report_html
from danmu_intel.report.rule_render import format_ts
from danmu_intel.stats.gray import GraySample, GraySignal, STATUS_CANDIDATE

#: 取报告的形态顺序（页面上的固定排列）。
FORM_ORDER: tuple[str, ...] = ("live_brief", "full", "review")

#: 全站固定导航（栏目）：`(标签, 相对站点根的目标路径)`。
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("首页", "index.html"),
    ("历史情报库", "history/index.html"),
    ("画像库", "profile/index.html"),
    ("灰信号", "gray/index.html"),
    ("验证闭环", "verification/index.html"),
    ("订阅", "subscribe.html"),
)

VISIBILITY_LABELS = {
    paywall.VISIBILITY_PUBLIC: "公开",
    paywall.VISIBILITY_PAID: "会员（付费）",
}

SITE_CSS = (
    REPORT_CSS
    + """
header.site { border-bottom: 1px solid #e5e5e5; margin-bottom: 16px; }
nav.site { display: flex; flex-wrap: wrap; gap: 4px 14px; padding: 8px 0 12px; font-size: 14px; }
section.card { background: #fff; border: 1px solid #e5e5e5; border-radius: 8px;
  padding: 16px; margin: 0 0 16px; }
ul.rows { list-style: none; margin: 0; padding: 0; }
ul.rows li { padding: 8px 0; border-bottom: 1px dashed #e5e5e5; }
ul.rows li:last-child { border-bottom: none; }
.tag { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  background: #edf2f7; color: #2d3748; }
.tag--paid { background: #fefcbf; color: #744210; }
table.boundary { width: 100%; border-collapse: collapse; font-size: 14px; }
table.boundary th, table.boundary td { border: 1px solid #e5e5e5; padding: 6px 8px; text-align: left; }
article { margin-bottom: 24px; }
"""
)

#: 页面里的站内链接（`<a href="...">`）。站外链接不应存在（NFR-A-2），解析时被忽略。
LINK_RE = re.compile(r'<a\s[^>]*href="(?P<href>[^"]*)"')


# —— 路径、标识与链接 ——


def slug(text: str) -> str:
    """URL 与文件名安全、且**不撞车**的标识（非纯 ASCII 名补 8 位内容哈希）。

    中文队名 / 选手名按内容哈希区分：不同的人不会共用页面，同一个人也不会被静默合并。
    """
    ascii_part = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    if ascii_part and ascii_part == text:
        return ascii_part
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{ascii_part}-{digest}" if ascii_part else f"x-{digest}"


def league_page_path(league: str) -> str:
    return f"leagues/{slug(league)}.html"


def match_page_path(match_id: int) -> str:
    return f"matches/{match_id}/index.html"


def report_page_path(match_id: int, kind: str) -> str:
    return f"matches/{match_id}/{kind}.html"


def team_page_path(team: str) -> str:
    return f"profile/teams/{slug(team)}.html"


def player_page_path(player: str) -> str:
    return f"profile/players/{slug(player)}.html"


IDENTIFIER_RESOLVERS = {
    "league": league_page_path,
    "match": lambda value: match_page_path(int(value)),
    "team": team_page_path,
    "player": player_page_path,
}


def identifier(kind: str, value: str) -> str:
    """标识字符串：`league:LPL` / `match:1` / `team:iG` / `player:Faker`。"""
    if kind not in IDENTIFIER_RESOLVERS:
        raise ValueError(f"未知的标识类型：{kind}（允许：{','.join(IDENTIFIER_RESOLVERS)}）")
    return f"{kind}:{value}"


def parse_identifier(value: str) -> tuple[str, str]:
    parts = value.split(":", 1)
    if len(parts) != 2 or parts[0] not in IDENTIFIER_RESOLVERS:
        raise ValueError(f"非法标识：{value}（形如 league:LPL / match:1 / team:iG / player:Faker）")
    return parts[0], parts[1]


def resolve_identifier(value: str) -> str:
    """标识 → 承载它的页面路径（检查 ⑤ 用它对「页面 × 联赛 × 标识」）。"""
    kind, raw = parse_identifier(value)
    try:
        return IDENTIFIER_RESOLVERS[kind](raw)
    except ValueError as exc:  # int(raw) 失败
        raise ValueError(f"非法标识：{value}（{exc}）") from None


def relative_href(page_path: str, target: str) -> str:
    """从 `page_path` 指向站内 `target` 的相对链接。"""
    return posixpath.relpath(target, posixpath.dirname(page_path) or ".")


def resolve_href(page_path: str, href: str) -> str | None:
    """把页面里的 `href` 解析成站内绝对路径；站外链接与页内锚点返回 `None`。"""
    if not href or href.startswith("#"):
        return None
    if "://" in href or href.startswith(("mailto:", "tel:")):
        return None
    base = posixpath.dirname(page_path)
    return posixpath.normpath(posixpath.join(base, href) if base else href)


def page_links(page_path: str, html: str) -> tuple[str, ...]:
    """页面里的站内链接目标（去重、保序）。"""
    targets: list[str] = []
    for match in LINK_RE.finditer(html):
        target = resolve_href(page_path, match.group("href"))
        if target is not None and target not in targets:
            targets.append(target)
    return tuple(targets)


# —— 页面与产物的模型 ——


@dataclass(frozen=True, slots=True)
class SitePage:
    path: str
    title: str
    html: str
    nav: tuple[tuple[str, str], ...] = ()
    league: str | None = None
    match_id: int | None = None
    identifiers: tuple[str, ...] = ()
    report_kind: str | None = None
    visibility: str = paywall.VISIBILITY_PUBLIC

    @property
    def is_report(self) -> bool:
        return self.report_kind is not None

    @property
    def links(self) -> tuple[str, ...]:
        """页面正文里的站内链接目标（检查 ① 的孤儿链接判定用）。"""
        return page_links(self.path, self.html)


@dataclass(frozen=True, slots=True)
class ReportMaterial:
    """进入本次发布的一份报告：账本里的内容 + 页面 + 构建时的来源复核结果。"""

    match_id: int
    kind: str
    version: int
    generated_at: int
    content: ReportContent
    visibility: str
    unresolved: tuple[SourceRef, ...] = ()

    @property
    def page_path(self) -> str:
        return report_page_path(self.match_id, self.kind)

    @property
    def label(self) -> str:
        return form_of(self.kind).label


@dataclass(frozen=True, slots=True)
class TeamView:
    """一支队伍的长期表现摘要（只由比赛登记与官方比分汇总）。"""

    name: str
    leagues: tuple[str, ...]
    matches: tuple[int, ...]
    wins: int
    losses: int
    undecided: int

    @property
    def page_path(self) -> str:
        return team_page_path(self.name)

    @property
    def record(self) -> str:
        text = f"{self.wins} 胜 {self.losses} 负"
        return f"{text}（另有 {self.undecided} 场官方比分未回填）" if self.undecided else text


@dataclass(frozen=True, slots=True)
class PlayerView:
    """一名选手的出场摘要（**只**由官方阵容数据生成，不从弹幕推断）。"""

    name: str
    teams: tuple[str, ...]
    matches: tuple[int, ...]

    @property
    def page_path(self) -> str:
        return player_page_path(self.name)


@dataclass(frozen=True, slots=True)
class GrayView:
    """灰信号页的一行：达门槛的灰信号 + 它属于哪场比赛（链回比赛页用）。"""

    match_id: int
    signal: GraySignal

    @property
    def category_label(self) -> str:
        return self.signal.category_label

    @property
    def keyword(self) -> str:
        return self.signal.keyword

    @property
    def hit_count(self) -> int:
        return self.signal.hit_count

    @property
    def distinct_users(self) -> int:
        return self.signal.distinct_users

    @property
    def window_count(self) -> int:
        return self.signal.window_count

    @property
    def samples(self) -> tuple[GraySample, ...]:
        return self.signal.samples

    @property
    def status(self) -> str:
        return self.signal.status


@dataclass(frozen=True, slots=True)
class SiteFacts:
    """页面要用到的全部事实（渲染函数的唯一输入）。"""

    generated_at: int
    matches: tuple[Match, ...]
    reports: tuple[ReportMaterial, ...]
    teams: tuple[TeamView, ...] = ()
    players: tuple[PlayerView, ...] = ()
    gray_signals: tuple[GrayView, ...] = ()
    segment_stats: dict[int, str] = field(default_factory=dict)

    def match(self, match_id: int) -> Match:
        for match in self.matches:
            if match.id == match_id:
                return match
        raise KeyError(f"站点树里没有比赛 #{match_id}")

    def visibility_of(self, match_id: int) -> str:
        return paywall.visibility(self.match(match_id).state)

    def reports_of(self, match_id: int) -> tuple[ReportMaterial, ...]:
        return tuple(item for item in self.reports if item.match_id == match_id)

    @property
    def leagues(self) -> tuple[str, ...]:
        return tuple(sorted({match.league for match in self.matches}))


@dataclass(frozen=True, slots=True)
class SiteTree:
    """一次发布的完整产物（检查、指纹与原子替换的对象）。"""

    pages: tuple[SitePage, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(page.path for page in self.pages)

    def page(self, path: str) -> SitePage:
        for page in self.pages:
            if page.path == path:
                return page
        raise KeyError(f"站点树里没有页面：{path}")

    def has(self, path: str) -> bool:
        return any(page.path == path for page in self.pages)

    def files(self) -> dict[str, str]:
        return {page.path: page.html for page in self.pages}

    def digest(self) -> str:
        """站点树指纹（幂等判定与版本标识用）：路径 + 内容，与顺序无关。"""
        payload = "\n".join(
            f"{page.path}\t{hashlib.sha256(page.html.encode('utf-8')).hexdigest()}"
            for page in sorted(self.pages, key=lambda item: item.path)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SiteBuild:
    """构建结果：站点树 + 事实（检查与账本都要用到事实）。"""

    tree: SiteTree
    facts: SiteFacts


# —— 页面渲染 ——


def _layout(page_path: str, title: str, body: str, *, subtitle: str = "") -> str:
    nav = " ".join(
        f'<a href="{escape(href)}">{escape(label)}</a>' for label, href in nav_links(page_path)
    )
    meta = f'<p class="meta">{escape(subtitle)}</p>' if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{SITE_CSS}</style>
</head>
<body>
<header class="site">
<h1>{escape(title)}</h1>
{meta}
<nav class="site">{nav}</nav>
</header>
<main>
{body}
</main>
<footer>本站不加载任何第三方脚本、字体或统计；页面内容由公开弹幕与官方数据汇总而成。</footer>
</body>
</html>
"""


def nav_links(page_path: str) -> tuple[tuple[str, str], ...]:
    """本页的站点导航（标签 + 相对本页的链接）：报告页也带上它，读者不会走进死胡同。"""
    return tuple((label, relative_href(page_path, target)) for label, target in NAV_ITEMS)


def _link(page_path: str, target: str, label: str) -> str:
    return f'<a href="{escape(relative_href(page_path, target))}">{escape(label)}</a>'


def _visibility_tag(value: str) -> str:
    css = "tag tag--paid" if value == paywall.VISIBILITY_PAID else "tag"
    return f'<span class="{css}">{escape(VISIBILITY_LABELS[value])}</span>'


def _match_label(match: Match) -> str:
    return f"{match.league}｜{match.team_a} vs {match.team_b}"


def _stamp(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


def _report_row(facts: SiteFacts, page_path: str, material: ReportMaterial) -> str:
    return (
        f"<li>{escape(_stamp(material.generated_at))}｜"
        f"{escape(_match_label(facts.match(material.match_id)))}｜"
        f"{escape(material.label)} v{material.version}｜{_visibility_tag(material.visibility)}｜"
        f"{_link(page_path, material.page_path, '报告页')}｜"
        f"{_link(page_path, match_page_path(material.match_id), '比赛页')}</li>"
    )


def _render_index(facts: SiteFacts) -> str:
    page_path = "index.html"
    latest = sorted(facts.reports, key=lambda item: (item.generated_at, item.match_id), reverse=True)[:10]
    rows = "".join(_report_row(facts, page_path, item) for item in latest) or "<li>还没有已发布的报告。</li>"
    league_links = "、".join(
        _link(page_path, league_page_path(name), name) for name in facts.leagues
    ) or "还没有登记任何联赛"
    recent = sorted(facts.matches, key=lambda item: item.id, reverse=True)[:10]
    match_links = "、".join(
        _link(page_path, match_page_path(match.id), f"#{match.id} {match.team_a} vs {match.team_b}")
        for match in recent
    ) or "还没有登记任何比赛"
    body = f"""<section class="card">
<h2>最新报告</h2>
<ul class="rows">{rows}</ul>
</section>
<section class="card">
<h2>按联赛浏览</h2>
<p>{league_links}</p>
<h2>最近的比赛</h2>
<p>{match_links}</p>
</section>
<section class="card">
<h2>栏目</h2>
<ul class="rows">
<li>{_link(page_path, "history/index.html", "历史情报库")}：按日期 / 联赛 / 队伍 / 比赛四个入口分面浏览过往报告。</li>
<li>{_link(page_path, "profile/index.html", "画像库")}：队伍与人员的长期表现摘要（只汇总官方数据，不用弹幕评价谁）。</li>
<li>{_link(page_path, "gray/index.html", "灰信号")}：弹幕里的讨论聚集现象，只作风险提示，不指控、不点名。</li>
<li>{_link(page_path, "verification/index.html", "验证闭环")}：已发布报告的事实层哈希与来源复核结果，公开可查。</li>
<li>{_link(page_path, "subscribe.html", "订阅")}：免费 / 付费边界与访问方式。</li>
</ul>
</section>
<section class="card">
<h2>纪律</h2>
<p>事实与解读分层标注；每项事实附「文件 + 行范围 + SHA256」，读者可独立复核。
比赛结束前，该场的报告正文只向会员提供；比赛一结束，该场全部页面自动转为公开。</p>
</section>
"""
    return _layout(page_path, "弹幕情报库", body, subtitle="公开弹幕 → 可复核的情报报告")


def _render_history(facts: SiteFacts) -> str:
    page_path = "history/index.html"
    ordered = sorted(facts.reports, key=lambda item: (item.generated_at, item.match_id), reverse=True)
    by_date: dict[str, list[str]] = {}
    for material in ordered:
        by_date.setdefault(_stamp(material.generated_at)[:10], []).append(
            _report_row(facts, page_path, material)
        )
    date_sections = "".join(
        f'<h2>{escape(day)}</h2><ul class="rows">{"".join(rows)}</ul>'
        for day, rows in sorted(by_date.items(), reverse=True)
    ) or "<p>还没有已发布的报告。</p>"
    league_links = "、".join(
        _link(page_path, league_page_path(name), name) for name in facts.leagues
    ) or "无"
    team_links = "、".join(_link(page_path, team.page_path, team.name) for team in facts.teams) or "无"
    match_links = "、".join(
        _link(page_path, match_page_path(match.id), f"#{match.id} {match.team_a} vs {match.team_b}")
        for match in sorted(facts.matches, key=lambda item: item.id, reverse=True)
    ) or "无"
    body = f"""<section class="card">
<h2>按日期</h2>
{date_sections}
</section>
<section class="card">
<h2>按联赛</h2>
<p>{league_links}</p>
<h2>按队伍</h2>
<p>{team_links}</p>
<h2>按比赛</h2>
<p>{match_links}</p>
</section>
"""
    return _layout(
        page_path,
        "历史情报库",
        body,
        subtitle="静态站不加载脚本：检索即「按日期 / 联赛 / 队伍 / 比赛」四个入口分面浏览",
    )


def _render_league(facts: SiteFacts, league: str) -> str:
    page_path = league_page_path(league)
    rows: list[str] = []
    for match in sorted(facts.matches, key=lambda item: item.id, reverse=True):
        if match.league != league:
            continue
        materials = facts.reports_of(match.id)
        entries = "、".join(
            _link(page_path, item.page_path, f"{item.label} v{item.version}") for item in materials
        ) or "尚无已发布报告"
        rows.append(
            f"<li>#{match.id}｜{escape(match.team_a)} vs {escape(match.team_b)}｜"
            f"状态 {escape(match.state)}｜{_visibility_tag(paywall.visibility(match.state))}｜"
            f"{_link(page_path, match_page_path(match.id), '比赛页')}｜{entries}</li>"
        )
    count = sum(1 for match in facts.matches if match.league == league)
    body = f"""<section class="card">
<h2>比赛</h2>
<ul class="rows">{''.join(rows) or '<li>该联赛还没有登记比赛。</li>'}</ul>
</section>
"""
    return _layout(page_path, f"{league} 联赛页", body, subtitle=f"共 {count} 场比赛")


def _render_match(facts: SiteFacts, match: Match) -> str:
    page_path = match_page_path(match.id)
    visibility = paywall.visibility(match.state)
    materials = facts.reports_of(match.id)
    rows = "".join(_report_row(facts, page_path, item) for item in materials) or "<li>尚无已发布报告。</li>"
    if visibility == paywall.VISIBILITY_PAID:
        note = (
            f'<p class="locked">{escape(paywall.PAYWALL_MARK)}：本场比赛状态为 {escape(match.state)}，'
            "报告正文只向会员提供，静态页面上没有正文与来源；比赛结束后本站自动再发布公开版。</p>"
        )
    else:
        note = '<p class="meta">本场比赛已结束，报告正文与来源对所有人公开。</p>'
    teams = "、".join(
        _link(page_path, team_page_path(name), name) for name in (match.team_a, match.team_b)
    )
    body = f"""<section class="card">
<h2>比赛信息</h2>
<p class="meta">#{match.id}｜{escape(match.league)}｜{escape(match.team_a)} vs {escape(match.team_b)}｜
阶段 {escape(match.stage or '未登记')}｜状态 {escape(match.state)}｜{_visibility_tag(visibility)}</p>
<p class="meta">计划开始 {escape(format_ts(match.scheduled_at))}｜开始 {escape(format_ts(match.started_at))}｜
结束 {escape(format_ts(match.ended_at))}｜官方结果 {escape(official.match_score(match.official_result) or '未回填')}</p>
<p class="meta">联赛页：{_link(page_path, league_page_path(match.league), match.league)}｜队伍：{teams}</p>
{note}
</section>
<section class="card">
<h2>报告</h2>
<ul class="rows">{rows}</ul>
</section>
<section class="card">
<h2>原始记录规模</h2>
<p class="meta">{escape(facts.segment_stats.get(match.id, '无原始记录'))}</p>
</section>
"""
    return _layout(
        page_path,
        f"{match.league} {match.team_a} vs {match.team_b}",
        body,
        subtitle=f"比赛页 #{match.id}",
    )


def _render_profile_index(facts: SiteFacts) -> str:
    page_path = "profile/index.html"
    team_rows = "".join(
        f"<li>{_link(page_path, team.page_path, team.name)}｜{escape('、'.join(team.leagues))}｜"
        f"{len(team.matches)} 场｜{escape(team.record)}</li>"
        for team in facts.teams
    ) or "<li>还没有登记任何队伍。</li>"
    player_rows = "".join(
        f"<li>{_link(page_path, player.page_path, player.name)}｜"
        f"{escape('、'.join(player.teams))}｜出场 {len(player.matches)} 场</li>"
        for player in facts.players
    )
    player_section = (
        f'<ul class="rows">{player_rows}</ul>'
        if player_rows
        else '<p class="meta">官方阵容数据尚未接入，因此本站不生成选手页 —— 原始弹幕只存加盐用户哈希，'
        "不从弹幕里推断「选手」是谁。</p>"
    )
    body = f"""<section class="card">
<h2>队伍</h2>
<ul class="rows">{team_rows}</ul>
</section>
<section class="card">
<h2>人员</h2>
{player_section}
</section>
<section class="card">
<h2>纪律</h2>
<p>画像只汇总官方数据与比赛登记事实（场次、官方比分、联赛），不用弹幕评价队伍或个人；
解读与事实在报告页分层标注。</p>
</section>
"""
    return _layout(page_path, "画像库", body, subtitle="队伍与人员的长期表现摘要（只由官方数据汇总）")


def _render_team(facts: SiteFacts, team: TeamView) -> str:
    page_path = team.page_path
    rows = "".join(
        f"<li>#{match_id}｜{escape(_match_label(facts.match(match_id)))}｜"
        f"状态 {escape(facts.match(match_id).state)}｜"
        f"{_link(page_path, match_page_path(match_id), '比赛页')}</li>"
        for match_id in team.matches
    )
    body = f"""<section class="card">
<h2>战绩</h2>
<p class="meta">联赛：{escape('、'.join(team.leagues))}｜场次：{len(team.matches)}｜{escape(team.record)}</p>
</section>
<section class="card">
<h2>比赛</h2>
<ul class="rows">{rows or '<li>该队伍还没有登记比赛。</li>'}</ul>
</section>
<section class="card">
<h2>说明</h2>
<p>胜负只取官方登记的比分（未回填的比赛不计入胜负）；本页不含任何来自弹幕的评价。</p>
</section>
"""
    return _layout(page_path, f"{team.name} 队伍页", body, subtitle="队伍长期表现摘要")


def _render_player(facts: SiteFacts, player: PlayerView) -> str:
    page_path = player.page_path
    rows = "".join(
        f"<li>#{match_id}｜{escape(_match_label(facts.match(match_id)))}｜"
        f"{_link(page_path, match_page_path(match_id), '比赛页')}</li>"
        for match_id in player.matches
    )
    body = f"""<section class="card">
<h2>出场</h2>
<p class="meta">队伍：{escape('、'.join(player.teams))}｜出场：{len(player.matches)} 场</p>
<ul class="rows">{rows or '<li>暂无出场记录。</li>'}</ul>
</section>
<section class="card">
<h2>说明</h2>
<p>本页只由官方阵容数据生成（官方数据源属设计 §20 O8 的开放项）；不结合弹幕做任何个人评价。</p>
</section>
"""
    return _layout(page_path, f"{player.name} 选手页", body, subtitle="选手出场摘要（官方阵容数据）")


def _render_gray(facts: SiteFacts) -> str:
    page_path = "gray/index.html"
    blocks: list[str] = []
    for item in facts.gray_signals:
        samples = "".join(
            f"<li>{escape(format_ts(sample.ts))}｜原文「{escape(sample.text)}」"
            f"（{escape(sample.rel_path)} 第 {sample.line_no} 行）</li>"
            for sample in item.samples
        )
        link = _link(page_path, match_page_path(item.match_id), f"比赛 #{item.match_id}")
        blocks.append(
            f'<section class="card"><h2>{escape(item.category_label)}｜'
            f"关键词「{escape(item.keyword)}」</h2>"
            f'<p class="meta">命中 {item.hit_count} 条｜独立发言者 {item.distinct_users} 人｜'
            f"覆盖 {item.window_count} 个时段｜状态 {escape(item.status)}｜{link}</p>"
            f'<ul class="rows">{samples}</ul></section>'
        )
    body = f"""<section class="card">
<h2>纪律</h2>
<p>下列内容是弹幕里出现的讨论聚集现象，<strong>只作风险提示</strong>，不构成对任何个人或队伍的任何指控，
也不代表比赛存在任何问题。样本只有时间、原文与取证坐标，不含任何身份字段；
不达证据门槛的命中已作废并留原因，本站不提供任何对外导出。</p>
</section>
{''.join(blocks) or '<section class="card"><p>目前没有任何达到门槛的灰信号。</p></section>'}
"""
    return _layout(page_path, "灰信号", body, subtitle="只作风险提示：不指控、不点名、必附样本")


def _render_verification(facts: SiteFacts) -> str:
    page_path = "verification/index.html"
    rows: list[str] = []
    for material in sorted(facts.reports, key=lambda item: (item.match_id, FORM_ORDER.index(item.kind))):
        match = facts.match(material.match_id)
        verdict = (
            '<span class="tag">来源全部复核通过</span>'
            if not material.unresolved
            else f'<span class="tag tag--paid">{len(material.unresolved)} 项来源复核失败</span>'
        )
        rows.append(
            f"<li>#{match.id}｜{escape(_match_label(match))}｜{escape(material.label)} v{material.version}｜"
            f"{verdict}｜官方结果 {escape(official.match_score(match.official_result) or '未回填')}｜"
            f"事实层哈希 <code>{escape(material.content.fact_layer_hash)}</code>｜"
            f"{_link(page_path, material.page_path, '报告页')}</li>"
        )
    body = f"""<section class="card">
<h2>可复核的东西</h2>
<p>每一份已发布报告都冻结了事实层哈希，并逐项记录来源（文件 + 行范围 + SHA256）。
本页对每个来源<strong>重新复核一遍</strong>：通过的标「来源全部复核通过」，失败的写在这里。</p>
</section>
<section class="card">
<h2>已发布报告</h2>
<ul class="rows">{''.join(rows) or '<li>还没有已发布的报告。</li>'}</ul>
</section>
<section class="card">
<h2>预测对错</h2>
<p>系统目前没有「预测」的产出方，因此<strong>不做预测，也不做事后追认</strong>：
没有留痕的预测不参与对错统计。报告第 7 段（预测验证）如实标注这一点。</p>
</section>
"""
    return _layout(page_path, "验证闭环", body, subtitle="公开可复核项：事实层哈希 + 来源逐项复核")


def _render_subscribe(facts: SiteFacts) -> str:
    page_path = "subscribe.html"
    body = """<section class="card">
<h2>免费与付费的边界</h2>
<table class="boundary">
<tr><th>内容</th><th>免费读者</th><th>会员</th></tr>
<tr><td>该场比赛结束前的赛中快报与完整版（含全部解读段落）</td><td>✗</td><td>✓</td></tr>
<tr><td>该场比赛结束后的复盘版报告</td><td>✓</td><td>✓</td></tr>
<tr><td>该场比赛结束后，其全部节点页</td><td>✓（自动转公开）</td><td>✓</td></tr>
<tr><td>订阅介绍页、历史情报库列表</td><td>✓</td><td>✓</td></tr>
</table>
<p class="meta">比赛一结束，其全部内容自动转为免费公开，不存在「已结束但仍被锁」的比赛。</p>
</section>
<section class="card">
<h2>怎么拿到会员</h2>
<p>不需要注册账号：凭既有的第三方通讯账号标识即可校验。档位与具体价格由后台配置
（数值属开放项，配置生效前本页不写任何数字）。</p>
</section>
<section class="card">
<h2>付费内容怎么发</h2>
<p>付费正文<strong>不写进静态页面</strong>：页面上只有标题、段目与付费说明，正文由后端凭凭据返回。
因此直接抓取页面地址也拿不到付费内容。</p>
</section>
"""
    return _layout(page_path, "订阅", body, subtitle="免费 / 付费边界与访问方式")


# —— 事实汇总（页面内容的事实来源）——


def _parsed_score(official_result: dict | None) -> tuple[int, int] | None:
    score = official.match_score(official_result)
    if not score or ":" not in score:
        return None
    left, _, right = score.partition(":")
    if not (left.strip().isdigit() and right.strip().isdigit()):
        return None
    return int(left), int(right)


def _team_views(matches: tuple[Match, ...]) -> tuple[TeamView, ...]:
    names: dict[str, dict[str, object]] = {}
    for match in matches:
        score = _parsed_score(match.official_result)
        for side, name in (("team_a", match.team_a), ("team_b", match.team_b)):
            entry = names.setdefault(
                name, {"leagues": set(), "matches": [], "wins": 0, "losses": 0, "undecided": 0}
            )
            entry["leagues"].add(match.league)  # type: ignore[union-attr]
            entry["matches"].append(match.id)  # type: ignore[union-attr]
            if score is None:
                entry["undecided"] += 1  # type: ignore[operator]
                continue
            left, right = score
            mine, theirs = (left, right) if side == "team_a" else (right, left)
            entry["wins" if mine > theirs else "losses"] += 1  # type: ignore[operator]
    return tuple(
        TeamView(
            name=name,
            leagues=tuple(sorted(entry["leagues"])),  # type: ignore[arg-type]
            matches=tuple(entry["matches"]),  # type: ignore[arg-type]
            wins=int(entry["wins"]),  # type: ignore[call-overload]
            losses=int(entry["losses"]),  # type: ignore[call-overload]
            undecided=int(entry["undecided"]),  # type: ignore[call-overload]
        )
        for name, entry in sorted(names.items())
    )


def _player_views(matches: tuple[Match, ...]) -> tuple[PlayerView, ...]:
    names: dict[str, dict[str, object]] = {}
    for match in matches:
        lineups = official.lineups(match.official_result)
        for side, team in (("team_a", match.team_a), ("team_b", match.team_b)):
            for player in lineups.get(side, ()):
                entry = names.setdefault(player, {"teams": set(), "matches": []})
                entry["teams"].add(team)  # type: ignore[union-attr]
                entry["matches"].append(match.id)  # type: ignore[union-attr]
    return tuple(
        PlayerView(
            name=name,
            teams=tuple(sorted(entry["teams"])),  # type: ignore[arg-type]
            matches=tuple(entry["matches"]),  # type: ignore[arg-type]
        )
        for name, entry in sorted(names.items())
    )


def _gray_views(conn: sqlite3.Connection) -> tuple[GrayView, ...]:
    """页面用的灰信号行：只有达到证据门槛的（`candidate`），且必附样本。"""
    rows = conn.execute(
        "SELECT * FROM gray_signals WHERE status=? ORDER BY match_id, category, keyword",
        (STATUS_CANDIDATE,),
    ).fetchall()
    return tuple(
        GrayView(
            match_id=int(row["match_id"]),
            signal=GraySignal(
                category=row["category"],
                keyword=row["keyword"],
                hit_count=int(row["hit_count"]),
                distinct_users=int(row["distinct_users"]),
                window_count=int(row["window_count"]),
                samples=tuple(GraySample(**item) for item in json.loads(row["samples_json"])),
                status=row["status"],
                reason=row["reason"],
            ),
        )
        for row in rows
    )


def _segment_stats(conn: sqlite3.Connection) -> dict[int, str]:
    """每场比赛的原始记录规模（文件数 + 弹幕条数）：让读者知道结论建立在多少证据上。"""
    rows = conn.execute(
        """
        SELECT s.match_id AS match_id, COUNT(*) AS files, COALESCE(SUM(seg.msg_count), 0) AS total
        FROM danmu_segments seg
        JOIN room_sessions s ON s.id = seg.room_session_id
        WHERE s.match_id IS NOT NULL
        GROUP BY s.match_id
        """
    ).fetchall()
    return {
        int(row["match_id"]): f"{row['files']} 个落盘文件｜共 {row['total']} 条弹幕"
        "（逐项 SHA256 见报告第 10 段）"
        for row in rows
    }


# —— 构建 ——


def build_site(
    conn: sqlite3.Connection,
    *,
    data_root: Path | None = None,
    generated_at: int | None = None,
) -> SiteBuild:
    """从库里的比赛、报告账本、灰信号与官方数据生成整棵站点树（纯汇总，不写盘）。"""
    from danmu_intel.pipeline import now_ms
    from danmu_intel.publish.access import published_versions

    matches = tuple(list_matches(conn))
    by_id = {match.id: match for match in matches}
    reports = tuple(
        _material(
            conn,
            by_id[match_id],
            kind,
            version,
            report_generated_at,
            data_root=data_root,
        )
        for match_id, kind, version, report_generated_at in published_versions(conn)
    )
    facts = SiteFacts(
        generated_at=generated_at if generated_at is not None else now_ms(),
        matches=matches,
        reports=reports,
        teams=_team_views(matches),
        players=_player_views(matches),
        gray_signals=_gray_views(conn),
        segment_stats=_segment_stats(conn),
    )
    return SiteBuild(tree=_build_tree(facts), facts=facts)


def _material(
    conn: sqlite3.Connection,
    match: Match,
    kind: str,
    version: int,
    generated_at: int,
    *,
    data_root: Path | None,
) -> ReportMaterial:
    """把账本里的一行报告变成产物材料：内容 + 可见性 + 来源复核结果。"""
    from danmu_intel.report.publish import load_content

    content = load_content(conn, match.id, kind, version)
    seen: dict[tuple[str, int, int, str], SourceRef] = {}
    for segment in content.segments:
        for ref in segment.sources:
            seen.setdefault((ref.rel_path, ref.line_start, ref.line_end, ref.sha256), ref)
    unresolved = tuple(
        ref for ref in seen.values() if not verify(ref, data_root=data_root)
    )
    return ReportMaterial(
        match_id=match.id,
        kind=kind,
        version=version,
        generated_at=generated_at,
        content=content,
        visibility=paywall.visibility(match.state),
        unresolved=unresolved,
    )


def _build_tree(facts: SiteFacts) -> SiteTree:
    pages: list[SitePage] = []
    league_ids = tuple(identifier("league", name) for name in facts.leagues)
    match_ids = tuple(identifier("match", str(match.id)) for match in facts.matches)
    team_ids = tuple(identifier("team", team.name) for team in facts.teams)
    player_ids = tuple(identifier("player", player.name) for player in facts.players)

    pages.append(
        SitePage(
            path="index.html",
            title="弹幕情报库",
            html=_render_index(facts),
            nav=NAV_ITEMS,
            identifiers=league_ids + match_ids + team_ids,
        )
    )
    pages.append(
        SitePage(
            path="history/index.html",
            title="历史情报库",
            html=_render_history(facts),
            nav=NAV_ITEMS,
            identifiers=league_ids + match_ids + team_ids,
        )
    )
    for league in facts.leagues:
        pages.append(
            SitePage(
                path=league_page_path(league),
                title=f"{league} 联赛页",
                html=_render_league(facts, league),
                nav=NAV_ITEMS,
                league=league,
                identifiers=(identifier("league", league),)
                + tuple(
                    identifier("match", str(match.id))
                    for match in facts.matches
                    if match.league == league
                ),
            )
        )
    for match in facts.matches:
        pages.append(
            SitePage(
                path=match_page_path(match.id),
                title=f"{match.league} {match.team_a} vs {match.team_b}",
                html=_render_match(facts, match),
                nav=NAV_ITEMS,
                league=match.league,
                match_id=match.id,
                visibility=paywall.visibility(match.state),
                identifiers=(identifier("match", str(match.id)), identifier("league", match.league))
                + tuple(identifier("team", name) for name in (match.team_a, match.team_b)),
            )
        )
    for material in facts.reports:
        match = facts.match(material.match_id)
        pages.append(
            SitePage(
                path=material.page_path,
                title=f"{match.league} {match.team_a} vs {match.team_b} 情报（{material.label}）",
                html=render_report_html(
                    material.content,
                    visibility=material.visibility,
                    nav=nav_links(material.page_path),
                ),
                nav=NAV_ITEMS,
                league=match.league,
                match_id=match.id,
                report_kind=material.kind,
                visibility=material.visibility,
                identifiers=(identifier("match", str(match.id)), identifier("league", match.league)),
            )
        )
    pages.append(
        SitePage(
            path="profile/index.html",
            title="画像库",
            html=_render_profile_index(facts),
            nav=NAV_ITEMS,
            identifiers=team_ids + player_ids,
        )
    )
    for team in facts.teams:
        pages.append(
            SitePage(
                path=team.page_path,
                title=f"{team.name} 队伍页",
                html=_render_team(facts, team),
                nav=NAV_ITEMS,
                league=team.leagues[0] if len(team.leagues) == 1 else None,
                identifiers=(identifier("team", team.name),)
                + tuple(identifier("league", name) for name in team.leagues)
                + tuple(identifier("match", str(match_id)) for match_id in team.matches),
            )
        )
    for player in facts.players:
        pages.append(
            SitePage(
                path=player.page_path,
                title=f"{player.name} 选手页",
                html=_render_player(facts, player),
                nav=NAV_ITEMS,
                identifiers=(identifier("player", player.name),)
                + tuple(identifier("team", name) for name in player.teams)
                + tuple(identifier("match", str(match_id)) for match_id in player.matches),
            )
        )
    pages.append(
        SitePage(
            path="gray/index.html",
            title="灰信号",
            html=_render_gray(facts),
            nav=NAV_ITEMS,
            identifiers=match_ids,
        )
    )
    pages.append(
        SitePage(
            path="verification/index.html",
            title="验证闭环",
            html=_render_verification(facts),
            nav=NAV_ITEMS,
            identifiers=match_ids,
        )
    )
    pages.append(
        SitePage(
            path="subscribe.html",
            title="订阅",
            html=_render_subscribe(facts),
            nav=NAV_ITEMS,
        )
    )
    return SiteTree(tuple(pages))
