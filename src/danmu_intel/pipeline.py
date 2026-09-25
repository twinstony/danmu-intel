"""流水线：原始记录 + 切片 → 规则统计 → 报告三形态 → 静态页。

一条命令跑通整条链路的收口处：

    collect（落盘+建库） → match add / boundaries / slice（定边界） → stats（统计全集）
    → report（三形态报告 + 发布检查） → verify-sources（逐项 SHA256 复核）

设计 §9 的铁律在这里的体现：`rebuild_metrics` 能**删掉统计结果后仅凭原始记录 +
切片 + 配置**重算出逐字节相同的统计（AC-13）—— 包括终局判定与灰信号。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from danmu_intel.common import evidence, paths
from danmu_intel.common.config import load_stats_config
from danmu_intel.common.matches import get_match
from danmu_intel.common.sources import SourceRef, verify
from danmu_intel.report.assemble import build_content
from danmu_intel.report.facts import GameFacts, MatchFacts, SegmentFacts, scope_facts
from danmu_intel.report.forms import KIND_LIVE_BRIEF, ReportScope, Timing, form_of
from danmu_intel.report.html import parse_sources
from danmu_intel.report.interpreter import Interpreter
from danmu_intel.report.publish import PublishResult, next_version, publish
from danmu_intel.slice import engine
from danmu_intel.slice.manual import load_slices
from danmu_intel.stats import final as final_signals
from danmu_intel.stats import full
from danmu_intel.stats.basic import RawLine
from danmu_intel.stats.gray import STATUS_CANDIDATE, STATUS_ESCALATED, evaluate_gray_signals

SEGMENT_QUERY = """
SELECT seg.rel_path AS rel_path, seg.sha256 AS sha256, seg.msg_count AS msg_count,
       seg.first_ts AS first_ts, seg.last_ts AS last_ts,
       r.platform AS platform, r.room_id AS room_id
FROM danmu_segments seg
JOIN room_sessions s ON s.id = seg.room_session_id
JOIN rooms r ON r.id = s.room_id
WHERE s.match_id = ?
ORDER BY s.id, seg.rel_path
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def load_segment_facts(conn: sqlite3.Connection, match_id: int) -> tuple[SegmentFacts, ...]:
    rows = conn.execute(SEGMENT_QUERY, (match_id,)).fetchall()
    return tuple(
        SegmentFacts(
            rel_path=row["rel_path"],
            platform=row["platform"],
            room_id=row["room_id"],
            msg_count=int(row["msg_count"]),
            first_ts=row["first_ts"],
            last_ts=row["last_ts"],
            sha256=row["sha256"],
        )
        for row in rows
    )


def load_lines(conn: sqlite3.Connection, match_id: int, *, data_root: Path | None = None) -> list[RawLine]:
    """读回该场比赛的全部原始弹幕，附带取证坐标（文件 + 行号）。

    `rel_path` 用索引里的原值（可能在归档件上），位置由 `evidence.locate` 解析：
    归档后重算统计因此仍成立（AC-13）。
    """
    from danmu_intel.common.events import iter_events

    root = data_root or paths.data_dir()
    lines: list[RawLine] = []
    for segment in load_segment_facts(conn, match_id):
        path = evidence.locate(segment.rel_path, data_root=root)
        for line_no, event in iter_events(path):
            lines.append(RawLine(rel_path=segment.rel_path, line_no=line_no, event=event))
    return lines


def collect_facts(
    conn: sqlite3.Connection, match_id: int, *, data_root: Path | None = None
) -> MatchFacts:
    """组装该场的全部事实：逐局统计全集 + 终局判定 + 灰信号（全部为纯函数的输出）。"""
    root = data_root or paths.data_dir()
    match = get_match(conn, match_id)
    config = load_stats_config(conn)
    segments = load_segment_facts(conn, match_id)
    lines = load_lines(conn, match_id, data_root=root)
    windows = load_slices(conn, match_id)
    side_names = {"team_a": match.team_a, "team_b": match.team_b}
    games = tuple(
        GameFacts(
            window=window,
            lines=tuple(full.select(lines, window)),
            metrics=full.compute_game(
                lines, window, official_result=match.official_result, side_names=side_names
            ),
        )
        for window in windows
    )
    observed_until = full.observed_until(lines)
    signal_facts = final_signals.collect_signal_facts(
        lines,
        score=full.closing_score(lines, windows, match.official_result),
        official_ended_at=match.ended_at,
        observed_until_ms=observed_until or 0,
        config=config,
    )
    return MatchFacts(
        match=match,
        games=games,
        all_lines=tuple(lines),
        segments=segments,
        algo_version=engine.algo_version(conn, match_id),
        data_root=root,
        generated_at=now_ms(),
        stats_config=config,
        final_judgement=final_signals.judge_final(
            signal_facts, observed_until_ms=observed_until or 0, config=config
        ),
        gray_signals=evaluate_gray_signals(lines, config=config),
        signal_facts=signal_facts,
    )


def clear_metrics(
    conn: sqlite3.Connection, match_id: int, *, algo_version: str | None = None
) -> None:
    """清统计行。给 `algo_version` 只清那一版（设计 §9.3：重算写新行，不覆盖旧行）。"""
    if algo_version is None:
        conn.execute("DELETE FROM metrics WHERE match_id=?", (match_id,))
    else:
        conn.execute("DELETE FROM metrics WHERE match_id=? AND algo_version=?", (match_id, algo_version))
    conn.commit()


def match_metrics(facts: MatchFacts) -> dict[str, dict[str, object]]:
    """比赛级指标（`game_no` 为空）：终局判定与灰信号。"""
    candidates = [signal for signal in facts.gray_signals if signal.status == STATUS_CANDIDATE]
    return {
        "final_signal": facts.final_judgement.as_dict(),
        "gray_signals": {
            "candidates": [signal.as_dict() for signal in candidates],
            "candidate_count": len(candidates),
            "discarded_count": len(facts.gray_signals) - len(candidates),
            "discarded": [
                {"category": signal.category, "keyword": signal.keyword,
                 "hit_count": signal.hit_count, "distinct_users": signal.distinct_users,
                 "window_count": signal.window_count, "reason": signal.reason}
                for signal in facts.gray_signals
                if signal.status != STATUS_CANDIDATE
            ],
        },
    }


def write_metrics(conn: sqlite3.Connection, facts: MatchFacts) -> int:
    """把规则统计写入 `metrics`（同一算法版本先清空，旧版本的行保留）。"""
    version = facts.algo_version
    clear_metrics(conn, facts.match.id, algo_version=version)
    computed_at = now_ms()
    count = 0
    rows: list[tuple[int | None, str, dict[str, object]]] = [
        (game.window.game_no, metric_key, value)
        for game in facts.games
        for metric_key, value in game.metrics.items()
    ]
    rows.extend((None, metric_key, value) for metric_key, value in match_metrics(facts).items())
    for game_no, metric_key, value in rows:
        conn.execute(
            """
            INSERT INTO metrics(match_id, game_no, metric_key, value_json, computed_at, algo_version)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                facts.match.id,
                game_no,
                metric_key,
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                computed_at,
                version,
            ),
        )
        count += 1
    write_gray_signals(conn, facts, now=computed_at)
    conn.commit()
    return count


def write_gray_signals(conn: sqlite3.Connection, facts: MatchFacts, *, now: int | None = None) -> int:
    """灰信号落库。**落库前置校验：必须附样本**（需求 §6.5 第 3 条）。

    人工升级过的行（`escalated`）不被重算覆盖 —— 人的判断优先于自动重算（T12 后台）。
    """
    for signal in facts.gray_signals:
        if not signal.samples:
            raise ValueError(f"灰信号必须附样本（需求 §6.5 第 3 条）：{signal.keyword}")
    moment = now_ms() if now is None else now
    conn.execute(
        "DELETE FROM gray_signals WHERE match_id=? AND status != ?",
        (facts.match.id, STATUS_ESCALATED),
    )
    for signal in facts.gray_signals:
        conn.execute(
            """
            INSERT INTO gray_signals(match_id, category, keyword, hit_count, distinct_users,
                                     window_count, samples_json, status, reason, created_at, evaluated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                facts.match.id,
                signal.category,
                signal.keyword,
                signal.hit_count,
                signal.distinct_users,
                signal.window_count,
                json.dumps([asdict(sample) for sample in signal.samples], ensure_ascii=False, sort_keys=True),
                signal.status,
                signal.reason,
                moment,
                moment,
            ),
        )
    conn.commit()
    return len(facts.gray_signals)


def metrics_snapshot(
    conn: sqlite3.Connection, match_id: int, *, algo_version: str | None = None
) -> list[tuple[int | None, str, str]]:
    sql = "SELECT game_no, metric_key, value_json FROM metrics WHERE match_id=?"
    params: list[object] = [match_id]
    if algo_version is not None:
        sql += " AND algo_version=?"
        params.append(algo_version)
    sql += " ORDER BY game_no IS NOT NULL, game_no, metric_key"
    rows = conn.execute(sql, params).fetchall()
    return [
        (row["game_no"] if row["game_no"] is None else int(row["game_no"]), row["metric_key"], row["value_json"])
        for row in rows
    ]


def rebuild_metrics(
    conn: sqlite3.Connection, match_id: int, *, data_root: Path | None = None
) -> bool:
    """AC-13 自检：删掉**当前算法版本**的统计结果，仅凭原始记录 + 切片 + 配置重算，
    比对是否逐字节相同。旧版本的行是历史账本，不参与比对（也不删除）。
    """
    facts = collect_facts(conn, match_id, data_root=data_root)
    version = facts.algo_version
    if not metrics_snapshot(conn, match_id, algo_version=version):
        write_metrics(conn, facts)
    before = metrics_snapshot(conn, match_id, algo_version=version)
    clear_metrics(conn, match_id, algo_version=version)
    write_metrics(conn, collect_facts(conn, match_id, data_root=data_root))
    return metrics_snapshot(conn, match_id, algo_version=version) == before


def generate_and_publish(
    conn: sqlite3.Connection,
    match_id: int,
    *,
    kind: str,
    completed_games: tuple[int, ...] | None = None,
    trigger_game_no: int | None = None,
    interpreter: Interpreter | None = None,
    data_root: Path | None = None,
    clock=None,
    generated_at: int | None = None,
) -> PublishResult:
    """报告三形态的统一入口：取材 → 组装 → 检查 → 发布（版本递增）。

    `completed_games` 是本次发布覆盖的节点（小局）。赛中快报**必须**显式声明：正在打的那一局
    不能当已完成的发（进行中的节点既没有完整事实，也不该出现在快报里）。赛后形态不传即
    覆盖全部已登记的小局。时限按形态的 `deadline_ms` 对齐。
    """
    form = form_of(kind)
    if form.kind == KIND_LIVE_BRIEF and completed_games is None:
        raise ValueError(
            "赛中快报必须声明已完成节点（completed_games / --completed-game N，可重复）："
            "进行中的节点不得进快报"
        )
    timing = Timing(clock=clock)
    facts = scope_facts(
        collect_facts(conn, match_id, data_root=data_root),
        ReportScope(completed_games=completed_games, trigger_game_no=trigger_game_no),
    )
    timing.mark("stats_ready")
    content = build_content(
        facts,
        form=form,
        version=next_version(conn, match_id, kind),
        generated_at=generated_at if generated_at is not None else now_ms(),
        interpreter=interpreter,
        trigger_game_no=trigger_game_no,
        timing=timing,
    )
    seals = {segment.rel_path: segment.sha256 for segment in facts.segments}
    return publish(conn, content, data_root=facts.data_root, timing=timing, seals=seals)


def verify_sources(
    match_id: int,
    *,
    kind: str,
    data_root: Path | None = None,
    page_path: Path | None = None,
) -> list[SourceRef]:
    """对着**已生成的页面**逐项复核来源，返回**校验失败**的引用（空列表即全部通过）。

    校验对象是产物里冻结的哈希，而不是刚刚现算的哈希，因此能真正发现
    「页面发出之后原始记录被改动」。
    """
    root = data_root or paths.data_dir()
    page = page_path or paths.report_page_path(match_id, kind)
    if not page.exists():
        raise LookupError(f"页面尚未生成：{page}（请先运行 report --kind {kind}）")
    return [ref for ref in parse_sources(page.read_text(encoding="utf-8")) if not verify(ref, data_root=root)]
