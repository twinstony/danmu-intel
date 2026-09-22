"""流水线：原始记录 + 切片 → 规则统计 → 报告三形态 → 静态页。

一条命令跑通整条链路的收口处：

    collect（落盘+建库） → match add / slice（人工定边界） → stats（规则统计）
    → report（三形态报告 + 发布检查） → verify-sources（逐项 SHA256 复核）

设计 §9 的铁律在这里的体现：`rebuild_metrics` 能**删掉统计结果后仅凭原始记录 +
切片**重算出逐字节相同的统计（AC-13）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from danmu_intel.common import paths
from danmu_intel.common.matches import get_match
from danmu_intel.common.sources import SourceRef, verify
from danmu_intel.report.assemble import build_content
from danmu_intel.report.facts import GameFacts, MatchFacts, SegmentFacts, scope_facts
from danmu_intel.report.forms import ReportScope, Timing, form_of
from danmu_intel.report.html import parse_sources
from danmu_intel.report.interpreter import Interpreter
from danmu_intel.report.publish import PublishResult, next_version, publish
from danmu_intel.slice.manual import load_slices
from danmu_intel.stats.basic import ALGO_VERSION, RawLine, compute

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
    """读回该场比赛的全部原始弹幕，附带取证坐标（文件 + 行号）。"""
    from danmu_intel.common.events import iter_events

    root = data_root or paths.data_dir()
    lines: list[RawLine] = []
    for segment in load_segment_facts(conn, match_id):
        for line_no, event in iter_events(root / segment.rel_path):
            lines.append(RawLine(rel_path=segment.rel_path, line_no=line_no, event=event))
    return lines


def collect_facts(
    conn: sqlite3.Connection, match_id: int, *, data_root: Path | None = None
) -> MatchFacts:
    root = data_root or paths.data_dir()
    match = get_match(conn, match_id)
    segments = load_segment_facts(conn, match_id)
    lines = load_lines(conn, match_id, data_root=root)
    games = tuple(
        GameFacts(window=window, lines=tuple(lines), metrics=compute(lines, window))
        for window in load_slices(conn, match_id)
    )
    return MatchFacts(
        match=match,
        games=games,
        all_lines=tuple(lines),
        segments=segments,
        algo_version=ALGO_VERSION,
        data_root=root,
        generated_at=now_ms(),
    )


def clear_metrics(conn: sqlite3.Connection, match_id: int) -> None:
    conn.execute("DELETE FROM metrics WHERE match_id=?", (match_id,))
    conn.commit()


def write_metrics(conn: sqlite3.Connection, facts: MatchFacts) -> int:
    """把规则统计写入 `metrics`（同一场先清空，避免重算叠加旧行）。"""
    clear_metrics(conn, facts.match.id)
    computed_at = now_ms()
    count = 0
    for game in facts.games:
        for metric_key, value in game.metrics.items():
            conn.execute(
                """
                INSERT INTO metrics(match_id, game_no, metric_key, value_json, computed_at, algo_version)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    facts.match.id,
                    game.window.game_no,
                    metric_key,
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    computed_at,
                    facts.algo_version,
                ),
            )
            count += 1
    conn.commit()
    return count


def metrics_snapshot(conn: sqlite3.Connection, match_id: int) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT game_no, metric_key, value_json FROM metrics WHERE match_id=? "
        "ORDER BY game_no, metric_key",
        (match_id,),
    ).fetchall()
    return [(int(row["game_no"]), row["metric_key"], row["value_json"]) for row in rows]


def rebuild_metrics(
    conn: sqlite3.Connection, match_id: int, *, data_root: Path | None = None
) -> bool:
    """AC-13 自检：删掉统计结果，仅凭原始记录 + 切片重算，比对是否逐字节相同。

    若库里原本没有统计结果，先按当前原始记录算一份基线再比对。
    """
    if not metrics_snapshot(conn, match_id):
        write_metrics(conn, collect_facts(conn, match_id, data_root=data_root))
    before = metrics_snapshot(conn, match_id)
    clear_metrics(conn, match_id)
    write_metrics(conn, collect_facts(conn, match_id, data_root=data_root))
    return metrics_snapshot(conn, match_id) == before


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

    `completed_games` 是本次发布覆盖的节点（小局）：赛中快报只发布已完成节点
    （缺省即全部已登记的小局）；赛后形态不传。时限按形态的 `deadline_ms` 对齐。
    """
    form = form_of(kind)
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
    return publish(conn, content, data_root=facts.data_root, timing=timing)


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
