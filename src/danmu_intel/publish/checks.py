"""发布前检查：需求 §6.8 的 6 项 + 1 项加固（逐项纯函数，设计 §11.2，ADR-0015 决策 2）。

输入是**产物 + 比赛状态机**（`SiteBuild`），输出是结论（`CheckResult`）：
没有 I/O、没有网络、没有时钟，所以「注入一个缺陷 → 检查必须拦下」可以逐项写成红绿用例。

| # | 检查 | 判定 |
|---|---|---|
| 1 | 全站导航唯一 | 每页导航无重复条目；所有站内链接都能落地；没有孤儿页面 |
| 2 | 无旧模板残留 | 模板指纹（旧版占位符 / 蓝图文件名 / 未渲染占位）计数为 0 |
| 3 | 付费墙正确 | 逐页对照**比赛状态机**结论：该锁的锁、该公开的公开（不看文件名/路径） |
| 4 | 报告分段完整 | 对照需求 §6.6 的十一段：缺段、空段、多段、缺解读段都不行 |
| 5 | 页面 × 联赛 × 标识一致 | 页面声明的联赛/比赛/队伍/选手标识都能解析，且与库里的事实一致 |
| 6 | 无「速览卡」类残留物 | 废弃组件指纹计数为 0 |
| 7 | 来源引用可达（加固，非需求 6 项） | 报告每个来源文件存在 + 行范围可读 + SHA256 与冻结值一致 |

**任一项不通过即不得发布**（`blocking=True`）；第 4 项复用报告层的同一断言
（`report/publish.py` 的段集与解读段检查），不把同一件事写两遍。
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Mapping

from danmu_intel.common import paywall
from danmu_intel.publish.site import (
    NAV_ITEMS,
    SiteBuild,
    SitePage,
    league_page_path,
    parse_identifier,
    resolve_identifier,
)
from danmu_intel.report.forms import form_of
from danmu_intel.report.publish import (
    CheckResult,
    check_interpretation_present,
    check_segments_complete,
    check_sources_resolvable,
)

#: 旧模板残留指纹（需求 §6.8 第 2 项）：蓝图时代的文件名 / 模板名 / 未渲染占位。
LEGACY_TEMPLATE_MARKERS: tuple[str, ...] = (
    "intel_danmu",
    "INTEL_HTML_TEMPLATE",
    "INTEL_TEMPLATE_OLD",
    "TEMPLATE_OLD",
    "旧模板",
    "<!-- legacy",
    "PLACEHOLDER",
    "Lorem ipsum",
    "{{",
)

#: 「速览卡」类残留指纹（需求 §6.8 第 6 项 / CONTEXT.md：该组件已废弃，不得出现在新产物中）。
QUICK_CARD_MARKERS: tuple[str, ...] = (
    "速览卡",
    "quick_card",
    "quick-card",
    "QuickCard",
    "quickcard",
)


def _body_markers(page: SitePage, build: SiteBuild) -> tuple[str, ...]:
    """报告页正文的判定片段：每段正文首行的转义形式（渲染层就是这么写的）。"""
    markers: list[str] = []
    material = next(
        (
            item
            for item in build.facts.reports
            if item.page_path == page.path
        ),
        None,
    )
    if material is None:
        return ()
    for segment in material.content.segments:
        for line in segment.body.splitlines():
            if line.strip():
                escaped = escape(line)
                if escaped not in markers:
                    markers.append(escaped)
                break
    return tuple(markers)


# —— 1. 全站导航唯一 ——


def check_navigation_unique(build: SiteBuild) -> CheckResult:
    tree = build.tree
    paths = set(tree.paths)
    problems: list[str] = []
    if len(paths) != len(tree.pages):
        problems.append("存在重复的页面路径")
    for page in tree.pages:
        if page.nav != NAV_ITEMS:
            problems.append(f"{page.path} 的导航与全站导航不一致（{len(page.nav)} 项）")
        labels = [label for label, _ in page.nav]
        targets = [target for _, target in page.nav]
        if len(set(labels)) != len(labels):
            problems.append(f"{page.path} 导航标签重复")
        if len(set(targets)) != len(targets):
            problems.append(f"{page.path} 导航项重复")
        for target in targets:
            if target not in paths:
                problems.append(f"{page.path} 的导航项指向不存在的页面 {target}")
        links = set(page.links)
        missing_nav = [target for target in targets if target not in links]
        if missing_nav:
            problems.append(
                f"{page.path} 的导航项没有渲染到页面上（少 {len(missing_nav)} 项："
                f"{'、'.join(missing_nav[:2])}）"
            )
        for target in page.links:
            if target not in paths:
                problems.append(f"{page.path} 链到了不存在的页面 {target}")
    reachable = {"index.html"}
    frontier = ["index.html"]
    while frontier:
        current = frontier.pop()
        if not tree.has(current):  # 指向不存在页面的链接已经在上面报过了
            continue
        for target in tree.page(current).links:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    orphans = sorted(paths - reachable)
    if orphans:
        problems.append(f"孤儿页面（从首页不可达）：{'、'.join(orphans)}")
    if problems:
        return CheckResult("navigation_unique", "全站导航唯一", False, "；".join(problems[:4]))
    return CheckResult(
        "navigation_unique",
        "全站导航唯一",
        True,
        f"{len(tree.pages)} 个页面导航无重复、链接全部落地、无孤儿页面",
    )


# —— 2. 无旧模板残留 ——


def _fingerprints(build: SiteBuild, markers: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for page in build.tree.pages:
        for marker in markers:
            if marker in page.html:
                found.append(f"{page.path} 含「{marker}」")
            if marker.lower() in page.path.lower():
                found.append(f"路径 {page.path} 含「{marker}」")
    return found


def check_no_legacy_template(build: SiteBuild) -> CheckResult:
    found = _fingerprints(build, LEGACY_TEMPLATE_MARKERS)
    if found:
        return CheckResult(
            "no_legacy_template", "无旧模板残留", False, "；".join(found[:4])
        )
    return CheckResult(
        "no_legacy_template",
        "无旧模板残留",
        True,
        f"{len(build.tree.pages)} 个页面零命中旧模板指纹（{len(LEGACY_TEMPLATE_MARKERS)} 类）",
    )


# —— 3. 付费墙正确 ——


def check_paywall_correct(build: SiteBuild) -> CheckResult:
    problems: list[str] = []
    for page in build.tree.pages:
        if page.match_id is None:
            continue
        expected = build.facts.visibility_of(page.match_id)
        state = build.facts.match(page.match_id).state
        if page.visibility != expected:
            problems.append(
                f"{page.path} 的可见性 {page.visibility} 与比赛状态 {state} 的判定 {expected} 不符"
            )
            continue
        if not page.is_report:
            continue
        markers = _body_markers(page, build)
        if expected == paywall.VISIBILITY_PAID:
            if paywall.PAYWALL_MARK not in page.html:
                problems.append(f"{page.path} 该受付费墙保护，但页面上没有付费墙")
            leaked = [marker for marker in markers if marker in page.html]
            if leaked:
                problems.append(f"{page.path} 是付费页却写了 {len(leaked)} 段正文")
            continue
        if paywall.PAYWALL_MARK in page.html:
            problems.append(f"{page.path} 的比赛已结束，但页面上仍有付费墙")
        missing = [marker for marker in markers if marker not in page.html]
        if missing:
            problems.append(f"{page.path} 是公开页，但缺 {len(missing)} 段正文")
    if problems:
        return CheckResult("paywall_correct", "付费墙正确", False, "；".join(problems[:4]))
    paid = sum(1 for page in build.tree.pages if page.visibility == paywall.VISIBILITY_PAID)
    return CheckResult(
        "paywall_correct",
        "付费墙正确",
        True,
        f"逐页对照比赛状态机：{paid} 个付费页只含付费墙、其余页面公开且正文齐全",
    )


# —— 4. 报告分段完整 ——


def check_report_segments(build: SiteBuild) -> CheckResult:
    problems: list[str] = []
    for material in build.facts.reports:
        form = form_of(material.kind)
        for result in (
            check_segments_complete(material.content, form),
            check_interpretation_present(material.content, form),
        ):
            if not result.passed:
                problems.append(f"比赛 #{material.match_id} {material.kind}：{result.detail}")
    if problems:
        return CheckResult(
            "report_segments_complete", "报告分段完整", False, "；".join(problems[:3])
        )
    return CheckResult(
        "report_segments_complete",
        "报告分段完整",
        True,
        f"{len(build.facts.reports)} 份报告对照需求 §6.6 十一段齐全、解读段齐备",
    )


# —— 5. 页面 × 联赛 × 标识一致 ——


def check_cross_references(build: SiteBuild) -> CheckResult:
    problems: list[str] = []
    tree = build.tree
    leagues = set(build.facts.leagues)
    team_names = {team.name for team in build.facts.teams}
    player_names = {player.name for player in build.facts.players}
    for page in tree.pages:
        if page.league is not None and page.league not in leagues:
            problems.append(f"{page.path} 声明了库里没有的联赛 {page.league}")
        if page.match_id is not None:
            try:
                match = build.facts.match(page.match_id)
            except KeyError:
                problems.append(f"{page.path} 指向库里没有的比赛 #{page.match_id}")
                continue
            if page.league != match.league:
                problems.append(
                    f"{page.path} 的联赛 {page.league} 与比赛 #{match.id} 的联赛 {match.league} 不一致"
                )
        for value in page.identifiers:
            try:
                kind, raw = parse_identifier(value)
                target = resolve_identifier(value)
            except ValueError as exc:
                problems.append(f"{page.path} 的标识有问题：{exc}")
                continue
            if not tree.has(target):
                problems.append(f"{page.path} 的标识 {value} 指向不存在的页面 {target}")
                continue
            resolved = tree.page(target)
            if kind == "league" and resolved.league != raw:
                problems.append(f"{page.path} 的标识 {value} 指向的页面不是该联赛页")
            if kind == "match" and resolved.match_id != int(raw):
                problems.append(f"{page.path} 的标识 {value} 指向的比赛页对不上")
            if kind == "team" and raw not in team_names:
                problems.append(f"{page.path} 引用了没有数据支撑的队伍 {raw}")
            if kind == "player" and raw not in player_names:
                problems.append(f"{page.path} 引用了没有数据支撑的选手 {raw}")
    for match in build.facts.matches:
        path = league_page_path(match.league)
        if not tree.has(path):
            problems.append(f"比赛 #{match.id} 的联赛页 {path} 不存在")
            continue
        if f"match:{match.id}" not in set(tree.page(path).identifiers):
            problems.append(f"{match.league} 联赛页没有列出比赛 #{match.id}")
    for material in build.facts.reports:
        if not tree.has(material.page_path):
            problems.append(f"账本里的报告 {material.page_path} 没有进产物")
    if problems:
        return CheckResult("cross_references", "页面×联赛×标识一致", False, "；".join(problems[:4]))
    return CheckResult(
        "cross_references",
        "页面×联赛×标识一致",
        True,
        f"{len(tree.pages)} 个页面的联赛、比赛、队伍、选手标识全部解析且与库里事实一致",
    )


# —— 6. 无「速览卡」类残留物 ——


def check_no_quick_card(build: SiteBuild) -> CheckResult:
    found = _fingerprints(build, QUICK_CARD_MARKERS)
    if found:
        return CheckResult(
            "no_quick_card",
            "无速览卡残留",
            False,
            "速览卡组件已废弃（CONTEXT.md）：" + "；".join(found[:4]),
        )
    return CheckResult(
        "no_quick_card",
        "无速览卡残留",
        True,
        f"{len(build.tree.pages)} 个页面零命中废弃组件指纹（{len(QUICK_CARD_MARKERS)} 类）",
    )


# —— 7. 来源引用可达（加固）——


def check_report_sources(
    build: SiteBuild, *, data_root: Path | None = None, seals: Mapping[str, str] | None = None
) -> CheckResult:
    problems: list[str] = []
    checked = 0
    for material in build.facts.reports:
        result = check_sources_resolvable(material.content, data_root=data_root, seals=seals)
        checked += 1
        if not result.passed:
            problems.append(f"比赛 #{material.match_id} {material.kind}：{result.detail}")
    if problems:
        return CheckResult("report_sources_resolvable", "来源引用可达", False, "；".join(problems[:3]))
    return CheckResult(
        "report_sources_resolvable",
        "来源引用可达",
        True,
        f"{checked} 份报告的来源全部可复核（文件 + 行范围 + 封存 SHA256）",
    )


# —— 汇总 ——


def run_checks(
    build: SiteBuild,
    *,
    data_root: Path | None = None,
    seals: Mapping[str, str] | None = None,
) -> tuple[CheckResult, ...]:
    """7 项检查（需求 6 项 + 来源可达加固）。任一项不通过即不得发布。"""
    return (
        check_navigation_unique(build),
        check_no_legacy_template(build),
        check_paywall_correct(build),
        check_report_segments(build),
        check_cross_references(build),
        check_no_quick_card(build),
        check_report_sources(build, data_root=data_root, seals=seals),
    )


def failures(checks: tuple[CheckResult, ...]) -> tuple[CheckResult, ...]:
    return tuple(item for item in checks if item.blocking and not item.passed)
