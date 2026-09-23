"""比赛实体读写（`matches` 表）。

T1 只支持人工登记与人工改状态：官方赛程/比分数据源属设计 §20 O8 的开放项，
本票不接。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

MATCH_STATES = ("scheduled", "live", "between_games", "ended", "aborted")


@dataclass(frozen=True, slots=True)
class Match:
    id: int
    league: str
    stage: str | None
    team_a: str
    team_b: str
    scheduled_at: int | None
    started_at: int | None
    ended_at: int | None
    state: str
    official_result: dict | None

    @property
    def title(self) -> str:
        return f"{self.team_a} vs {self.team_b}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def create_match(
    conn: sqlite3.Connection,
    *,
    league: str,
    team_a: str,
    team_b: str,
    state: str = "ended",
    stage: str | None = None,
    scheduled_at: int | None = None,
    started_at: int | None = None,
    ended_at: int | None = None,
    official_result: dict | None = None,
) -> int:
    if state not in MATCH_STATES:
        raise ValueError(f"非法比赛状态：{state}（允许：{','.join(MATCH_STATES)}）")
    now = _now_ms()
    cursor = conn.execute(
        """
        INSERT INTO matches(league, stage, team_a, team_b, scheduled_at, started_at, ended_at,
                            state, official_result, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            league,
            stage,
            team_a,
            team_b,
            scheduled_at,
            started_at,
            ended_at,
            state,
            json.dumps(official_result, ensure_ascii=False) if official_result else None,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_match(conn: sqlite3.Connection, match_id: int) -> Match:
    row = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if row is None:
        raise LookupError(f"未找到比赛 #{match_id}")
    return _to_match(row)


def list_matches(conn: sqlite3.Connection) -> list[Match]:
    """全部比赛（按 id 升序）—— 站点产物按它列出比赛与联赛。"""
    return [_to_match(row) for row in conn.execute("SELECT * FROM matches ORDER BY id")]


def set_match_state(
    conn: sqlite3.Connection,
    match_id: int,
    *,
    state: str,
    ended_at: int | None = None,
    official_result: dict | None = None,
) -> Match:
    """写比赛状态（状态机是付费墙判定的唯一真相源，因此状态写入必须走一个入口）。"""
    if state not in MATCH_STATES:
        raise ValueError(f"非法比赛状态：{state}（允许：{','.join(MATCH_STATES)}）")
    get_match(conn, match_id)
    conn.execute(
        "UPDATE matches SET state=?, ended_at=COALESCE(?, ended_at), "
        "official_result=COALESCE(?, official_result), updated_at=? WHERE id=?",
        (
            state,
            ended_at,
            json.dumps(official_result, ensure_ascii=False) if official_result is not None else None,
            _now_ms(),
            match_id,
        ),
    )
    conn.commit()
    return get_match(conn, match_id)


def _to_match(row: sqlite3.Row) -> Match:
    return Match(
        id=int(row["id"]),
        league=row["league"],
        stage=row["stage"],
        team_a=row["team_a"],
        team_b=row["team_b"],
        scheduled_at=row["scheduled_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        state=row["state"],
        official_result=json.loads(row["official_result"]) if row["official_result"] else None,
    )
