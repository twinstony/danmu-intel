"""规则直出渲染（设计 §10.1，T1 的最低质量版本；T6 换 LLM 受约束解读）。

两种段落：
- **事实段**（0/1/2/5/7/10 的事实部分）：只由「原始记录 + 规则统计」生成，
  每项都带来源引用（文件 + 行范围 + SHA256）。
- **解读段**（3/4/6/8/9 与 2 的解读部分）：规则模板填空，**只引用事实段里已经
  出现过的数字**，并且显式标注「解读」。缺段不得生成（`build_segments` 把关）。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from danmu_intel.common.sources import SourceRef, refs_for_lines
from danmu_intel.report.facts import GameFacts, MatchFacts
from danmu_intel.report.segments import Segment, build_segments
from danmu_intel.stats import final as final_signals
from danmu_intel.stats.basic import RawLine
from danmu_intel.stats.gray import contains_identity

BOUNDARY_LABELS = {
    "official": "官方时间",
    "danmu_signal": "弹幕信号复核",
    "report_window": "已发布报告窗口",
    "manual": "人工指定",
}
INTERPRETATION_MARK = "（解读，非事实）"


def format_ts(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "未登记"
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def refs_for(lines: list[RawLine] | tuple[RawLine, ...], facts: MatchFacts) -> tuple[SourceRef, ...]:
    by_file: dict[str, list[int]] = defaultdict(list)
    for line in lines:
        by_file[line.rel_path].append(line.line_no)
    refs: list[SourceRef] = []
    for rel_path in sorted(by_file):
        refs.extend(refs_for_lines(rel_path, by_file[rel_path], data_root=facts.data_root))
    return tuple(refs)


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


def _collect_scope(facts: MatchFacts) -> str:
    match = facts.match
    if not facts.segments:
        return "取材范围：无原始记录"
    first = min(s.first_ts for s in facts.segments if s.first_ts is not None)
    last = max(s.last_ts for s in facts.segments if s.last_ts is not None)
    return (
        f"取材范围：平台 {'、'.join(facts.platforms)}；直播间 {'、'.join(facts.room_ids)}；"
        f"覆盖 {format_ts(first)} – {format_ts(last)}；共 {len(facts.all_lines)} 条弹幕；"
        f"比赛状态 {match.state}"
    )


def body_match_info(facts: MatchFacts) -> str:
    match = facts.match
    result = match.official_result or {}
    lines = [
        f"比赛 #{match.id}｜{match.league}｜{match.team_a} vs {match.team_b}｜状态 {match.state}",
        f"阶段：{match.stage or '未登记'}｜计划开始：{format_ts(match.scheduled_at)}",
        f"官方结果：{result.get('score', '未回填')}",
        _collect_scope(facts),
    ]
    return "\n".join(lines)


def _final_phrase(facts: MatchFacts) -> str:
    judgement = facts.final_judgement
    if judgement.verdict == final_signals.VERDICT_FINAL:
        return (
            f"终局判定：已终局（{format_ts(judgement.satisfied_at_ms)} 起满足 "
            f"{len(judgement.kinds)} 类独立信号，{format_ts(judgement.decided_at_ms)} 确认无反转）"
        )
    if judgement.verdict == final_signals.VERDICT_REVOKED:
        return f"终局判定：曾判定、已撤销（{judgement.reason}）"
    return f"终局判定：未判定终局（{judgement.reason}）"


def _score_phrase(facts: MatchFacts) -> str:
    if not facts.games:
        return "比分：无小局切片，无法归属比分"
    score = facts.games[-1].metrics.get("score") or {}
    if not score.get("official"):
        return "比分：官方未回填（设计 §20 O8 官方数据源）"
    tail = f"；{score['discrepancy']}" if score.get("discrepancy") else ""
    return f"比分：官方 {score['official']}（口径 {score['official_scope']}，弹幕提及 {score['mentions']} 条）{tail}"


def body_result_overview(facts: MatchFacts) -> str:
    match = facts.match
    result = match.official_result or {}
    lines = [
        f"官方结果：{result.get('score', '未回填')}（来源：人工登记；官方数据源接入见设计 §20 O8）",
        _score_phrase(facts),
        f"小局切片：{len(facts.games)} 局（边界来源：{_boundary_summary(facts)}）",
        _final_phrase(facts),
    ]
    lines.extend(_game_line(game) for game in facts.games)
    return "\n".join(lines)


def _boundary_summary(facts: MatchFacts) -> str:
    counts: dict[str, int] = {}
    for game in facts.games:
        counts[game.window.boundary_source] = counts.get(game.window.boundary_source, 0) + 1
    return "、".join(f"{BOUNDARY_LABELS.get(source, source)} {count} 局" for source, count in sorted(counts.items()))


def body_game_review(facts: MatchFacts) -> str:
    if not facts.games:
        return "本场未登记任何小局切片，逐局复盘无可复核的边界事实。\n" + INTERPRETATION_MARK + "不做没有边界依据的复盘。"
    blocks = ["逐局事实："]
    blocks.extend(_game_line(game) for game in facts.games)
    busiest = max(facts.games, key=lambda game: int(game.metrics["danmu_total"]["count"]))
    blocks.append("")
    blocks.append(
        INTERPRETATION_MARK
        + f"按弹幕总量看，讨论最集中的是 G{busiest.window.game_no}；"
        + "这只说明观众注意力所在，不等于局势判断。"
    )
    return "\n".join(blocks)


def body_team_profile(facts: MatchFacts) -> str:
    match = facts.match
    if not facts.games:
        detail = "本场没有可用的小局切片，因此没有任何可归属到队伍的事实。"
    else:
        busiest = max(facts.games, key=lambda game: int(game.metrics["danmu_total"]["count"]))
        detail = (
            f"弹幕总量最高的小局是 G{busiest.window.game_no}（{busiest.metrics['danmu_total']['count']} 条），"
            "说明该局的讨论热度最高。"
        )
    return (
        f"{INTERPRETATION_MARK}本报告不含任何可归属到 {match.team_a} / {match.team_b} 的结构化数据，"
        f"因此不对两队的实力与风格下结论。{detail}"
        "队伍画像需要跨场累积（设计 §19 M6），单场弹幕样本不足以支撑。"
    )


def body_player_profile(facts: MatchFacts) -> str:
    return (
        f"{INTERPRETATION_MARK}本系统的原始记录不落明文身份（设计 §5.2：只存加盐用户哈希），"
        "因此无法产出人员级画像，也不做任何点名。"
        f"本场共有 {len(facts.all_lines)} 条弹幕，只用于热度与去重计数，不用于评价个人。"
    )


def body_gray_signals(facts: MatchFacts) -> str:
    """灰信号汇总（需求 §6.5 的 6 条硬约束在渲染层的落点）。

    硬约束 2「不指控、不点名」：本函数出口前会逐一比对全部 `user_hash`，
    只要正文里出现任何一个，立即抛错 —— **渲不出来比渲出来强**。
    """
    config = facts.stats_config
    reportable_signals = facts.reportable_gray_signals
    lines = [
        "风险提示：以下内容是弹幕里出现的**讨论聚集现象**，只作风险提示，"
        "不构成对任何个人或队伍的任何指控，也不代表比赛存在任何问题。",
    ]
    if not reportable_signals:
        lines.append(f"本场未产出达到门槛的灰信号（门槛：命中 ≥{config.gray_min_hits} 次、"
                     f"独立发言者 ≥{config.gray_min_users} 人、覆盖 ≥{config.gray_min_windows} 个时段）。")
    for signal in reportable_signals:
        lines.append(
            f"【{signal.category_label}】关键词「{signal.keyword}」：命中 {signal.hit_count} 条｜"
            f"独立发言者 {signal.distinct_users} 人｜覆盖 {signal.window_count} 个时段｜状态 {signal.status}"
        )
        for sample in signal.samples:
            lines.append(f"  样本：{format_ts(sample.ts)}｜原文「{sample.text}」（{sample.rel_path} 第 {sample.line_no} 行）")
    if facts.gray_signals:
        discarded = len(facts.gray_signals) - len(reportable_signals)
        lines.append(
            f"另有 {discarded} 个关键词命中未达证据门槛，已按纪律作废并留原因（不进报告）。"
            if discarded
            else "所有关键词命中均达到证据门槛。"
        )
    lines.append(
        "纪律（需求 §6.5）：灰信号只作风险提示，不出现指控性结论、不指名个人或队伍、"
        "必须附样本（时间 + 原文片段）、必须满足多人多时段的证据门槛；"
        "不达门槛即作废并留原因；不得用于任何勒索、威胁或交易，也不提供对外导出。"
    )
    body = "\n".join(lines)
    leaked = contains_identity(body, {line.event.user_hash for line in facts.all_lines})
    if leaked:
        raise ValueError(f"灰信号渲染层禁止输出任何身份标识（需求 §6.5 第 2 条）：{len(leaked)} 处命中")
    return body


def body_league_patterns(facts: MatchFacts) -> str:
    return (
        f"{INTERPRETATION_MARK}单场样本不足以形成联赛规律。本场只有 {len(facts.games)} 个小局的弹幕证据，"
        "跨场规律需要历史库累积（设计 §19 M6）。本段不引入事实段之外的任何数字。"
    )


def body_prediction_check(facts: MatchFacts) -> str:
    return (
        "本场没有公开发布过的预测记录（预测台账与验证闭环属 T5），因此无可对照项。\n"
        f"{INTERPRETATION_MARK}不做事后追认：没有留痕的预测不参与对错统计（需求 NFR-L-4）。"
    )


def body_market_talk(facts: MatchFacts) -> str:
    return (
        f"{INTERPRETATION_MARK}本票未接入盘口数据源，弹幕中的盘口讨论也尚未做结构化抽取"
        "（关键词与灰信号统计属 T4）。"
        f"本场 {len(facts.all_lines)} 条弹幕里是否提及盘口，本报告不作判断——"
        "没有抽取过程就没有可信结论。"
    )


def body_outlook(facts: MatchFacts) -> str:
    if not facts.games:
        points = "本场没有可用的小局切片，无法给出观察点。"
    else:
        busiest = max(facts.games, key=lambda game: int(game.metrics["danmu_total"]["count"]))
        points = (
            f"① 回看 G{busiest.window.game_no} 的弹幕密度变化：{_peak_phrase(busiest)}；"
            "密度最高的时刻通常对应比赛的关键事件。"
        )
    return (
        f"{INTERPRETATION_MARK}观察点由事实段推出，不新增事实：{points}"
        "② 本报告的数据缺口请对照第 10 段的取材范围与文件清单，缺口即证据边界。"
    )


def body_sources(facts: MatchFacts) -> str:
    lines = [
        f"算法版本：{facts.algo_version}",
        f"原始记录文件：{len(facts.segments)} 个；弹幕总数：{len(facts.all_lines)} 条",
    ]
    for segment in facts.segments:
        lines.append(
            f"- {segment.rel_path}｜{segment.msg_count} 条｜"
            f"{format_ts(segment.first_ts)} – {format_ts(segment.last_ts)}｜SHA256 {segment.sha256}"
        )
    lines.append("每个来源都可用「文件 + 行范围 + SHA256」独立复核（页面上逐项展开）。")
    return "\n".join(lines)


def build_report(facts: MatchFacts) -> list[Segment]:
    """按十一段结构直出报告。**缺段/空段会直接抛错，产不出缺段页面。**"""
    all_lines_refs = refs_for(facts.all_lines, facts)
    games_refs = refs_for([line for game in facts.games for line in game.lines], facts)
    bodies = {
        0: (body_match_info(facts), all_lines_refs),
        1: (body_result_overview(facts), games_refs),
        2: (body_game_review(facts), games_refs),
        3: (body_team_profile(facts), games_refs),
        4: (body_player_profile(facts), all_lines_refs),
        5: (body_gray_signals(facts), games_refs),
        6: (body_league_patterns(facts), games_refs),
        7: (body_prediction_check(facts), games_refs),
        8: (body_market_talk(facts), all_lines_refs),
        9: (body_outlook(facts), games_refs),
        10: (body_sources(facts), all_lines_refs),
    }
    return build_segments(bodies)
