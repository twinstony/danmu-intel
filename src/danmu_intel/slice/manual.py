"""切片层（设计 §8）。

T1 只做**手动切片**：给定 `start_ms` / `end_ms` 写入 `slices`，记录
`boundary_source='manual'`。三级自动边界（官方 / 弹幕信号 / 报告窗口）属 T4。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

BOUNDARY_SOURCES = ("official", "danmu_signal", "report_window", "manual")
MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class SliceWindow:
    """一个小局切片（含边界来源，设计 §8.1）。"""

    match_id: int
    game_no: int
    start_ms: int
    end_ms: int
    boundary_source: str
    conflict_note: str | None = None
    override_by: str | None = None
    override_reason: str | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def add_manual_slice(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    game_no: int,
    start_ms: int,
    end_ms: int,
    note: str | None = None,
    override_by: str | None = None,
    override_reason: str | None = None,
) -> int:
    """写入/覆盖一个小局切片。覆盖已有边界时必须留痕（设计 §8.1）。"""
    if start_ms >= end_ms:
        raise ValueError(f"切片起止非法：start_ms={start_ms} >= end_ms={end_ms}")
    existing = conn.execute(
        "SELECT * FROM slices WHERE match_id=? AND game_no=?", (match_id, game_no)
    ).fetchone()
    if existing is not None:
        changed = existing["start_ms"] != start_ms or existing["end_ms"] != end_ms
        if changed and (not override_by or not override_reason):
            raise ValueError("覆盖已有切片边界必须填写 override_by 与 override_reason（人工修正留痕）")
        conflict_note = note if note is not None else existing["conflict_note"]
        conn.execute(
            """
            UPDATE slices SET start_ms=?, end_ms=?, boundary_source=?, conflict_note=?,
                              override_by=?, override_at=?, override_reason=?
            WHERE match_id=? AND game_no=?
            """,
            (
                start_ms,
                end_ms,
                MANUAL,
                conflict_note,
                override_by or existing["override_by"],
                _now_ms() if changed else existing["override_at"],
                override_reason or existing["override_reason"],
                match_id,
                game_no,
            ),
        )
        conn.commit()
        return int(existing["id"])
    cursor = conn.execute(
        """
        INSERT INTO slices(match_id, game_no, start_ms, end_ms, boundary_source, conflict_note,
                           override_by, override_at, override_reason)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (match_id, game_no, start_ms, end_ms, MANUAL, note, override_by, None, override_reason),
    )
    conn.commit()
    return int(cursor.lastrowid)


def load_slices(conn: sqlite3.Connection, match_id: int) -> list[SliceWindow]:
    rows = conn.execute(
        "SELECT * FROM slices WHERE match_id=? ORDER BY game_no", (match_id,)
    ).fetchall()
    return [
        SliceWindow(
            match_id=int(row["match_id"]),
            game_no=int(row["game_no"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            boundary_source=row["boundary_source"],
            conflict_note=row["conflict_note"],
            override_by=row["override_by"],
            override_reason=row["override_reason"],
        )
        for row in rows
    ]
