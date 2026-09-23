"""报告的事实输入（设计 §10.3：解读层的输入只有事实层产物）。

`MatchFacts` 是流水线在「原始记录 + 切片 + 统计全集（含终局判定与灰信号）」之上
组装好的一个不可变快照。规则直出渲染（`rule_render.py`）、解读层（`interpreter.py`）
与页面渲染（`html.py`）都只读它：**渲染层没有任何自己算数的入口**，它只能引用事实层
已经算出来的东西（ACI-11 / NFR-Q-5 的落点）。

`fact_layer_hash` 是**解读层输入的指纹**（设计 §10.3）：把事实层投影成规范 JSON
再取 SHA256。投影里包含比赛信息、算法版本、各小局的切片边界与规则统计、比赛级的
终局判定与灰信号、落盘文件清单，以及**覆盖到的原始记录行的摘要**——原始记录被改动，
指纹必然变。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from danmu_intel.common.config import StatsConfig
from danmu_intel.common.matches import Match
from danmu_intel.report.forms import ReportScope
from danmu_intel.slice.manual import SliceWindow
from danmu_intel.stats import final as final_signals
from danmu_intel.stats import full
from danmu_intel.stats.basic import RawLine
from danmu_intel.stats.final import FinalJudgement, SignalFact
from danmu_intel.stats.gray import GraySignal, evaluate_gray_signals, reportable


@dataclass(frozen=True, slots=True)
class GameFacts:
    """一个小局：切片边界 + **该边界内的**原始行 + 统计全集。"""

    window: SliceWindow
    lines: tuple[RawLine, ...]
    metrics: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class SegmentFacts:
    """一个落盘文件的取证事实（`danmu_segments` 行）。"""

    rel_path: str
    platform: str
    room_id: str
    msg_count: int
    first_ts: int | None
    last_ts: int | None
    sha256: str


@dataclass(frozen=True, slots=True)
class MatchFacts:
    match: Match
    games: tuple[GameFacts, ...]
    all_lines: tuple[RawLine, ...]
    segments: tuple[SegmentFacts, ...]
    algo_version: str
    data_root: Path
    generated_at: int
    stats_config: StatsConfig
    final_judgement: FinalJudgement
    gray_signals: tuple[GraySignal, ...]
    signal_facts: tuple[SignalFact, ...]
    excluded_games: tuple[int, ...] = ()  # 本报告不覆盖的小局（赛中快报：进行中的节点）

    @property
    def platforms(self) -> list[str]:
        return sorted({segment.platform for segment in self.segments})

    @property
    def room_ids(self) -> list[str]:
        return sorted({segment.room_id for segment in self.segments})

    @property
    def game_nos(self) -> tuple[int, ...]:
        return tuple(game.window.game_no for game in self.games)

    @property
    def reportable_gray_signals(self) -> tuple[GraySignal, ...]:
        """可进报告的灰信号：只有达门槛的（需求 §6.5 第 4/6 条）。"""
        return reportable(self.gray_signals)


def scope_facts(facts: MatchFacts, scope: ReportScope) -> MatchFacts:
    """按报告形态的取材范围收窄事实层（issue #8 范围第 3 条）。

    赛中快报只发布**已完成节点**的段落：未完成小局既不出现在正文的统计里，
    它那段时间的弹幕也不进「取材范围」的条数——报告里的每个数字都必须对得上它
    自己声明的覆盖范围（AC-1 事实零错误）。

    比赛级的统计（终局判定、灰信号）同样按覆盖范围**重算**：它们的判定依据就是那些
    原始记录，拿全量记录算出来的结论放进只覆盖部分节点的报告里，就等于正文引用了
    自己取材范围之外的证据。
    """
    if scope.completed_games is None:
        return facts
    covered = tuple(game for game in facts.games if scope.covers(game.window.game_no))
    excluded = tuple(
        game.window.game_no for game in facts.games if not scope.covers(game.window.game_no)
    )
    windows = tuple(game.window for game in covered)
    lines = tuple(
        line
        for line in facts.all_lines
        if any(window.start_ms <= line.event.ts < window.end_ms for window in windows)
    )
    observed_until = full.observed_until(lines) or 0
    signal_facts = final_signals.collect_signal_facts(
        lines,
        score=full.closing_score(lines, windows, facts.match.official_result),
        official_ended_at=facts.match.ended_at,
        observed_until_ms=observed_until,
        config=facts.stats_config,
    )
    return replace(
        facts,
        games=covered,
        all_lines=lines,
        excluded_games=excluded,
        gray_signals=evaluate_gray_signals(lines, config=facts.stats_config),
        signal_facts=signal_facts,
        final_judgement=final_signals.judge_final(
            signal_facts, observed_until_ms=observed_until, config=facts.stats_config
        ),
    )


def lines_digest(lines: tuple[RawLine, ...]) -> str:
    """覆盖到的原始记录行摘要（取证坐标 + 时间 + 原文）。"""
    payload = "\n".join(
        f"{line.rel_path}\t{line.line_no}\t{line.event.ts}\t{line.event.text}" for line in lines
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fact_layer_payload(facts: MatchFacts) -> dict[str, object]:
    """事实层的规范投影 —— 解读层拿到的全部东西都在这里，不多也不少。"""
    match = facts.match
    return {
        "match": {
            "id": match.id,
            "league": match.league,
            "stage": match.stage,
            "team_a": match.team_a,
            "team_b": match.team_b,
            "state": match.state,
            "scheduled_at": match.scheduled_at,
            "started_at": match.started_at,
            "ended_at": match.ended_at,
            "official_result": match.official_result or {},
        },
        "algo_version": facts.algo_version,
        "games": [
            {
                "game_no": game.window.game_no,
                "start_ms": game.window.start_ms,
                "end_ms": game.window.end_ms,
                "boundary_source": game.window.boundary_source,
                "metrics": game.metrics,
            }
            for game in facts.games
        ],
        "final_judgement": facts.final_judgement.as_dict(),
        "gray_signals": [signal.as_dict() for signal in facts.gray_signals],
        "segments": [
            {
                "rel_path": segment.rel_path,
                "platform": segment.platform,
                "room_id": segment.room_id,
                "msg_count": segment.msg_count,
                "first_ts": segment.first_ts,
                "last_ts": segment.last_ts,
                "sha256": segment.sha256,
            }
            for segment in facts.segments
        ],
        "lines": {"count": len(facts.all_lines), "digest": lines_digest(facts.all_lines)},
    }


def fact_layer_hash(facts: MatchFacts) -> str:
    """事实层指纹（`reports.fact_layer_hash`）：同输入必然同值，改一个数必然变。"""
    canonical = json.dumps(
        fact_layer_payload(facts), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
