"""规则直出渲染（设计 §10.1：本票的最低质量版本；T6 换受约束的 LLM 解读）。

本模块只提供三件事，组装交给 `assemble.py`：

- `fact_body(no, facts, header)`：**事实段**正文，只由「原始记录 + 规则统计」生成；
- `interpretation_text(no, facts)`：**解读段**正文，规则模板填空，只引用事实段里
  已经出现过的数字（需求 §6.9 第 1 条），由组装层统一加「解读，非事实」标注；
- `sources_for(no, facts)`：该段引用的原始记录（文件 + 行范围 + SHA256）。

缺段不得生成（`build_segments` 把关），空段同理。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Callable

from danmu_intel.common.sources import SourceRef, refs_for_lines
from danmu_intel.report.facts import GameFacts, MatchFacts
from danmu_intel.report.forms import ReportHeader
from danmu_intel.stats.basic import RawLine

INTERPRETATION_MARK = "（解读，非事实）"

BOUNDARY_LABELS = {
    "official": "官方时间",
    "danmu_signal": "弹幕信号复核",
    "report_window": "已发布报告窗口",
    "manual": "人工指定",
}

# 段 → 引用「全部原始记录」还是「小局的记录」
ALL_LINE_SEGMENTS = (0, 4, 8, 10)


def format_ts(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "未登记"
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def refs_for(
    lines: list[RawLine] | tuple[RawLine, ...], facts: MatchFacts
) -> tuple[SourceRef, ...]:
    by_file: dict[str, list[int]] = defaultdict(list)
    for line in lines:
        by_file[line.rel_path].append(line.line_no)
    refs: list[SourceRef] = []
    for rel_path in sorted(by_file):
        refs.extend(refs_for_lines(rel_path, by_file[rel_path], data_root=facts.data_root))
    return tuple(refs)


def sources_for(no: int, facts: MatchFacts) -> tuple[SourceRef, ...]:
    """该段引用的原始记录。事实层的每一项都带来源（FR-C4-7）。"""
    if no in ALL_LINE_SEGMENTS:
        return refs_for(facts.all_lines, facts)
    return refs_for([line for game in facts.games for line in game.lines], facts)


def _peak_phrase(game: GameFacts) -> str:
    top = game.metrics.get("peak") or {}
    if not top:
        return "无显著峰值（所有窗口均未超过「均值+3σ」与绝对阈值）"
    method = "均值+3σ" if top.get("method") == "mean+3sigma" else "绝对阈值"
    return (
        f"峰值窗口 {format_ts(int(top['t_start']))}（窗口内 {top['count']} 条，"
        f"判定依据：{method}，阈值 {top['threshold']}）"
    )


def _game_line(game: GameFacts) -> str:
    window = game.window
    return (
        f"G{window.game_no}：{format_ts(window.start_ms)} – {format_ts(window.end_ms)}"
        f"（边界来源：{BOUNDARY_LABELS.get(window.boundary_source, window.boundary_source)}）"
        f"｜弹幕 {game.metrics['danmu_total']['count']} 条"
        f"｜独立发言者 {game.metrics['distinct_users']['count']} 人"
        f"｜{_peak_phrase(game)}"
    )


def _coverage_line(facts: MatchFacts) -> str:
    """取材范围（FR-C4-4）+ 覆盖了哪些节点。"""
    match = facts.match
    parts: list[str] = []
    if facts.segments:
        first = min(s.first_ts for s in facts.segments if s.first_ts is not None)
        last = max(s.last_ts for s in facts.segments if s.last_ts is not None)
        parts.append(
            f"取材范围：平台 {'、'.join(facts.platforms)}；直播间 {'、'.join(facts.room_ids)}；"
            f"覆盖 {format_ts(first)} – {format_ts(last)}；共 {len(facts.all_lines)} 条弹幕；"
            f"比赛状态 {match.state}"
        )
    else:
        parts.append("取材范围：无原始记录")
    covered = "、".join(f"G{no}" for no in facts.game_nos) or "（无切片）"
    parts.append(f"本报告覆盖的节点（小局）：{covered}")
    if facts.excluded_games:
        excluded = "、".join(f"G{no}" for no in facts.excluded_games)
        parts.append(f"未纳入本报告的节点：{excluded}（进行中，不发布其段落）")
    return "｜".join(parts)


def _match_info(facts: MatchFacts, header: ReportHeader) -> str:
    match = facts.match
    result = match.official_result or {}
    return "\n".join(
        [
            f"比赛 #{match.id}｜{match.league}｜{match.team_a} vs {match.team_b}｜状态 {match.state}",
            f"阶段：{match.stage or '未登记'}｜计划开始：{format_ts(match.scheduled_at)}",
            f"官方结果：{result.get('score', '未回填')}",
            _coverage_line(facts),
        ]
    )


def _result_overview(facts: MatchFacts, header: ReportHeader) -> str:
    match = facts.match
    result = match.official_result or {}
    scope = f"本报告覆盖 {len(facts.games)} 局"
    if facts.excluded_games:
        scope += f"（另有 {len(facts.excluded_games)} 局进行中，未纳入）"
    lines = [
        f"官方结果：{result.get('score', '未回填')}（来源：人工登记；官方数据源接入见设计 §20 O8）",
        f"小局切片：{scope}（边界来源：人工指定）",
    ]
    lines.extend(_game_line(game) for game in facts.games)
    return "\n".join(lines)


def _game_review_facts(facts: MatchFacts, header: ReportHeader) -> str:
    if not facts.games:
        return "本场未登记任何小局切片，逐局复盘无可复核的边界事实。"
    return "\n".join(["逐局事实：", *(_game_line(game) for game in facts.games)])


def _prediction_facts(facts: MatchFacts, header: ReportHeader) -> str:
    return "本场没有公开发布过的预测记录（预测台账与验证闭环属 T7），因此无可对照项。"


def _gray_signals(facts: MatchFacts, header: ReportHeader) -> str:
    return "\n".join(
        [
            f"本报告覆盖的 {len(facts.games)} 个小局未产出灰信号（灰信号识别属 T4，本票未接入）。",
            "纪律（需求 §6.5）：灰信号只作风险提示，不出现指控性结论、不指名个人或队伍、"
            "必须附样本（时间 + 原文片段）、必须满足多人多时段的证据门槛；"
            "不达门槛即作废并留原因。本报告不含任何指控。",
        ]
    )


def _sources(facts: MatchFacts, header: ReportHeader) -> str:
    lines = [
        f"报告形态：{header.kind}｜版本：v{header.version}｜解读层："
        f"{'LLM' if header.llm_state == 'llm' else '规则直出（解读能力降级，如实标注）'}",
        f"事实层哈希：{header.fact_layer_hash}（解读层的输入指纹，可回溯当时的事实层）",
        f"算法版本：{facts.algo_version}",
        f"原始记录文件：{len(facts.segments)} 个；本报告覆盖弹幕：{len(facts.all_lines)} 条",
    ]
    lines.extend(
        f"- {segment.rel_path}｜{segment.msg_count} 条｜"
        f"{format_ts(segment.first_ts)} – {format_ts(segment.last_ts)}｜SHA256 {segment.sha256}"
        for segment in facts.segments
    )
    lines.append("每个来源都可用「文件 + 行范围 + SHA256」独立复核（页面上逐项展开）。")
    return "\n".join(lines)


def _game_review_reading(facts: MatchFacts) -> str:
    if not facts.games:
        return "不做没有边界依据的复盘。"
    busiest = max(facts.games, key=lambda game: int(game.metrics["danmu_total"]["count"]))
    return (
        f"按弹幕总量看，讨论最集中的是 G{busiest.window.game_no}；"
        "这只说明观众注意力所在，不等于局势判断。"
    )


def _team_profile(facts: MatchFacts) -> str:
    match = facts.match
    if not facts.games:
        detail = "本场没有可用的小局切片，因此没有任何可归属到队伍的事实。"
    else:
        busiest = max(facts.games, key=lambda game: int(game.metrics["danmu_total"]["count"]))
        detail = (
            f"弹幕总量最高的小局是 G{busiest.window.game_no}"
            f"（{busiest.metrics['danmu_total']['count']} 条），说明该局的讨论热度最高。"
        )
    return (
        f"本报告不含任何可归属到 {match.team_a} / {match.team_b} 的结构化数据，"
        f"因此不对两队的实力与风格下结论。{detail}"
        "队伍画像需要跨场累积（设计 §19 M6），单场弹幕样本不足以支撑。"
    )


def _player_profile(facts: MatchFacts) -> str:
    return (
        "本系统的原始记录不落明文身份（设计 §5.2：只存加盐用户哈希），"
        "因此无法产出人员级画像，也不做任何点名。"
        f"本报告覆盖的 {len(facts.all_lines)} 条弹幕只用于热度与去重计数，不用于评价个人。"
    )


def _league_patterns(facts: MatchFacts) -> str:
    return (
        f"单场样本不足以形成联赛规律。本报告只有 {len(facts.games)} 个小局的弹幕证据，"
        "跨场规律需要历史库累积（设计 §19 M6）。本段不引入事实段之外的任何数字。"
    )


def _prediction_reading(facts: MatchFacts) -> str:
    return "不做事后追认：没有留痕的预测不参与对错统计（需求 NFR-L-4）。"


def _market_talk(facts: MatchFacts) -> str:
    return (
        "本票未接入盘口数据源，弹幕中的盘口讨论也尚未做结构化抽取（关键词与灰信号统计属 T4）。"
        f"本报告覆盖的 {len(facts.all_lines)} 条弹幕里是否提及盘口，本报告不作判断——"
        "没有抽取过程就没有可信结论。"
    )


def _outlook(facts: MatchFacts) -> str:
    if not facts.games:
        points = "本报告没有可用的小局切片，无法给出观察点。"
    else:
        busiest = max(facts.games, key=lambda game: int(game.metrics["danmu_total"]["count"]))
        points = (
            f"① 回看 G{busiest.window.game_no} 的弹幕密度变化：{_peak_phrase(busiest)}；"
            "密度最高的时刻通常对应比赛的关键事件。"
        )
    return (
        f"观察点由事实段推出，不新增事实：{points}"
        "② 本报告的数据缺口请对照第 10 段的取材范围与文件清单，缺口即证据边界。"
    )


FactBody = Callable[[MatchFacts, ReportHeader], str]
InterpretationBody = Callable[[MatchFacts], str]

FACT_BODIES: dict[int, FactBody] = {
    0: _match_info,
    1: _result_overview,
    2: _game_review_facts,
    5: _gray_signals,
    7: _prediction_facts,
    10: _sources,
}

INTERPRETATION_BODIES: dict[int, InterpretationBody] = {
    2: _game_review_reading,
    3: _team_profile,
    4: _player_profile,
    6: _league_patterns,
    7: _prediction_reading,
    8: _market_talk,
    9: _outlook,
}


def fact_body(no: int, facts: MatchFacts, header: ReportHeader) -> str:
    try:
        return FACT_BODIES[no](facts, header)
    except KeyError:
        raise KeyError(f"第 {no} 段没有事实正文（该段是纯解读段）") from None


def interpretation_text(no: int, facts: MatchFacts) -> str:
    try:
        return INTERPRETATION_BODIES[no](facts)
    except KeyError:
        raise KeyError(f"第 {no} 段没有解读正文（该段是纯事实段）") from None
