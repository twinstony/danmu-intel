"""直播间登记（`rooms` 表）的人工增删改查（需求 FR-C8-1 / FR-C8-2 / FR-C8-5）。

采集进程自己也会登记房间（`collect/runner.py: upsert_room`，`discovered_by='manual'` 的第一优先级
来自 FR-C1-3）；本模块管的是**后台那一半**：管理员在页面上加/改/删直播间，不需要碰任何文件，
并且每一次改动都留审计（FR-C8-4）。

删除是**有闸的**：已经采集过的房间（有 `room_sessions` 行）不许删 —— 原始 JSONL 是账本，
房间行删掉之后没人解释得清那些文件是从哪来的。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from danmu_intel.common import audit

DISCOVERED_BY_MANUAL = "manual"
DISCOVERED_BY_SCHEDULE = "schedule"
DISCOVERED_BY_POOL = "pool"
DISCOVERED_BY = (DISCOVERED_BY_MANUAL, DISCOVERED_BY_SCHEDULE, DISCOVERED_BY_POOL)

ACTION_ADD = "room.add"
ACTION_UPDATE = "room.update"
ACTION_DELETE = "room.delete"


@dataclass(frozen=True, slots=True)
class Room:
    id: int
    platform: str
    room_id: str
    url: str
    streamer: str | None
    discovered_by: str
    is_live: bool
    last_seen_at: int | None

    @property
    def label(self) -> str:
        return f"{self.platform}/{self.room_id}"


def _to_room(row: sqlite3.Row) -> Room:
    return Room(
        id=int(row["id"]),
        platform=row["platform"],
        room_id=row["room_id"],
        url=row["url"],
        streamer=row["streamer"],
        discovered_by=row["discovered_by"],
        is_live=bool(row["is_live"]),
        last_seen_at=row["last_seen_at"],
    )


def list_rooms(conn: sqlite3.Connection) -> list[Room]:
    return [_to_room(row) for row in conn.execute("SELECT * FROM rooms ORDER BY id")]


def get_room(conn: sqlite3.Connection, room_row_id: int) -> Room:
    row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_row_id,)).fetchone()
    if row is None:
        raise LookupError(f"未找到直播间 #{room_row_id}")
    return _to_room(row)


def _named(conn: sqlite3.Connection, *, platform: str, room_id: str) -> Room:
    row = conn.execute(
        "SELECT * FROM rooms WHERE platform=? AND room_id=?", (platform, room_id)
    ).fetchone()
    if row is None:
        raise LookupError(f"未找到直播间 {platform}/{room_id}")
    return _to_room(row)


def add_room(
    conn: sqlite3.Connection,
    *,
    platform: str,
    room_id: str,
    url: str,
    streamer: str | None = None,
    discovered_by: str = DISCOVERED_BY_MANUAL,
    actor: str,
    ts: int | None = None,
) -> Room:
    """登记一个直播间（同平台同房间号即视为同一个，重复登记就更新地址与主播名）。"""
    if not platform or not room_id or not url:
        raise ValueError("登记直播间需要 平台 / 房间标识 / 地址 三样都填")
    if discovered_by not in DISCOVERED_BY:
        raise ValueError(f"未知的来源标记：{discovered_by}（允许：{','.join(DISCOVERED_BY)}）")
    existing = conn.execute(
        "SELECT * FROM rooms WHERE platform=? AND room_id=?", (platform, room_id)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO rooms(platform, room_id, url, streamer, discovered_by, is_live, last_seen_at)"
            " VALUES(?, ?, ?, ?, ?, 0, NULL)",
            (platform, room_id, url, streamer, discovered_by),
        )
    else:
        conn.execute(
            "UPDATE rooms SET url=?, streamer=COALESCE(?, streamer), discovered_by=? WHERE id=?",
            (url, streamer, discovered_by, int(existing["id"])),
        )
    conn.commit()
    room = _named(conn, platform=platform, room_id=room_id)
    audit.record(
        conn,
        actor=actor,
        action=ACTION_ADD,
        target=room.label,
        detail={"url": url, "streamer": streamer, "discovered_by": discovered_by, "created": existing is None},
        ts=ts,
    )
    return room


def update_room(
    conn: sqlite3.Connection,
    room_row_id: int,
    *,
    url: str | None = None,
    streamer: str | None = None,
    actor: str,
    ts: int | None = None,
) -> Room:
    """改地址或主播名（改动进审计：谁、何时、改了什么）。"""
    before = get_room(conn, room_row_id)
    if url is None and streamer is None:
        raise ValueError("没有要改的字段：地址与主播名至少要给一个")
    conn.execute(
        "UPDATE rooms SET url=COALESCE(?, url), streamer=COALESCE(?, streamer) WHERE id=?",
        (url, streamer, room_row_id),
    )
    conn.commit()
    after = get_room(conn, room_row_id)
    audit.record(
        conn,
        actor=actor,
        action=ACTION_UPDATE,
        target=after.label,
        detail={"before": {"url": before.url, "streamer": before.streamer},
                "after": {"url": after.url, "streamer": after.streamer}},
        ts=ts,
    )
    return after


def delete_room(
    conn: sqlite3.Connection, room_row_id: int, *, actor: str, ts: int | None = None
) -> Room:
    """删掉一个直播间；采集过的房间不许删（原始记录是账本，房间行是它的出处）。"""
    room = get_room(conn, room_row_id)
    sessions = conn.execute(
        "SELECT COUNT(*) AS n FROM room_sessions WHERE room_id=?", (room_row_id,)
    ).fetchone()["n"]
    if int(sessions):
        raise ValueError(
            f"直播间 {room.label} 已经有 {int(sessions)} 个采集会话，不能删除"
            "（原始 JSONL 是账本，房间行是它的出处）"
        )
    conn.execute("DELETE FROM rooms WHERE id=?", (room_row_id,))
    conn.commit()
    audit.record(conn, actor=actor, action=ACTION_DELETE, target=room.label, detail={}, ts=ts)
    return room
