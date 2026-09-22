"""采集健康状态与每房间贡献量（FR-C1-7 / AC-15）。"""

from __future__ import annotations

import os

from danmu_intel.collect.health import pid_alive, room_contribution, room_health
from danmu_intel.collect.heartbeat import Heartbeat, write_heartbeat
from danmu_intel.collect.incidents import PROCESS_HUNG, emit
from danmu_intel.common import paths
from danmu_intel.common.matches import create_match

from conftest import BASE_TS, make_event

DEAD_PID = 999_999


def add_room(conn, *, platform: str = "huya", room_id: str = "660000", streamer: str = "样例主播") -> int:
    conn.execute(
        "INSERT INTO rooms(platform, room_id, url, streamer, discovered_by, is_live) "
        "VALUES(?, ?, ?, ?, 'manual', 1)",
        (platform, room_id, f"https://www.{platform}.com/{room_id}", streamer),
    )
    conn.commit()
    return int(conn.execute("SELECT id FROM rooms ORDER BY id DESC LIMIT 1").fetchone()["id"])


def add_session(conn, room_row_id: int, match_id: int, **overrides) -> int:
    payload = {
        "pid": DEAD_PID,
        "started_at": BASE_TS,
        "ended_at": BASE_TS + 600_000,
        "state": "exited",
        "restart_count": 0,
        "reconnects": 0,
        "severity": "info",
        "last_msg_at": BASE_TS + 5_000,
    }
    payload.update(overrides)
    cursor = conn.execute(
        """
        INSERT INTO room_sessions(room_id, match_id, pid, started_at, ended_at, state,
                                  restart_count, reconnects, severity, last_msg_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            room_row_id,
            match_id,
            payload["pid"],
            payload["started_at"],
            payload["ended_at"],
            payload["state"],
            payload["restart_count"],
            payload["reconnects"],
            payload["severity"],
            payload["last_msg_at"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_segment(conn, session_id: int, room: tuple[str, str], events, *, hour_offset: int = 0) -> None:
    from conftest import write_jsonl

    platform, room_id = room
    rel_path = f"raw/{platform}/2026-09-22/{room_id}-{16 + hour_offset:02d}.jsonl"
    digest = write_jsonl(paths.data_dir() / rel_path, events)
    conn.execute(
        """
        INSERT INTO danmu_segments(room_session_id, rel_path, sha256, first_ts, last_ts, msg_count, sealed_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rel_path) DO UPDATE SET
          room_session_id=excluded.room_session_id, sha256=excluded.sha256,
          first_ts=excluded.first_ts, last_ts=excluded.last_ts, msg_count=excluded.msg_count,
          sealed_at=excluded.sealed_at
        """,
        (session_id, rel_path, digest, events[0].ts, events[-1].ts, len(events), BASE_TS),
    )
    conn.commit()


def make_beat(room: tuple[str, str], session_id: int, **overrides) -> Heartbeat:
    payload = {
        "pid": DEAD_PID,
        "session_id": session_id,
        "platform": room[0],
        "room_id": room[1],
        "state": "running",
        "started_at": BASE_TS,
        "last_msg_at": BASE_TS + 9_000,
        "msg_count": 42,
        "reconnects": 3,
        "restart_count": 0,
        "written_at": BASE_TS + 10_000,
    }
    payload.update(overrides)
    return Heartbeat(**payload)


def test_pid_alive_semantics():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(DEAD_PID) is False
    assert pid_alive(None) is False
    assert pid_alive(0) is False


def test_health_prefers_live_heartbeat_over_the_row(conn, data_root):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG")
    room_row = add_room(conn)
    session_id = add_session(conn, room_row, match_id, state="running", ended_at=None, severity="warning")
    write_heartbeat(
        make_beat(("huya", "660000"), session_id, pid=os.getpid(), state="stalled", msg_count=77, reconnects=4),
        data_root=data_root,
    )

    health = room_health(conn, match_id, data_root=data_root, now=BASE_TS + 12_000)[0]
    assert (health.state, health.pid, health.msg_count, health.reconnects) == ("stalled", os.getpid(), 77, 4)
    assert health.ended_at is None and health.heartbeat_age_ms == 2_000
    assert health.streamer == "样例主播" and health.is_live is True


def test_health_falls_back_to_row_when_heartbeat_pid_is_dead(conn, data_root):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG")
    room_row = add_room(conn)
    session_id = add_session(conn, room_row, match_id, severity="critical", state="exited")
    add_segment(conn, session_id, ("huya", "660000"), [make_event(BASE_TS + i) for i in range(4)])
    write_heartbeat(make_beat(("huya", "660000"), session_id, msg_count=99), data_root=data_root)

    health = room_health(conn, match_id, data_root=data_root, now=BASE_TS + 12_000)[0]
    assert health.pid == DEAD_PID and health.state == "exited"
    assert health.msg_count == 4, "进程死了就看封存下来的条数"
    assert health.severity == "critical" and health.ended_at is not None


def test_health_ignores_heartbeat_from_another_session(conn, data_root):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG")
    room_row = add_room(conn)
    session_id = add_session(conn, room_row, match_id)
    write_heartbeat(make_beat(("huya", "660000"), session_id + 99, pid=os.getpid()), data_root=data_root)

    health = room_health(conn, match_id, data_root=data_root, now=BASE_TS)[0]
    assert health.state == "exited", "会话号对不上就不是这条会话的心跳"


def test_health_exposes_last_incident_and_restart_count(conn, data_root):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG")
    room_row = add_room(conn)
    add_session(conn, room_row, match_id, restart_count=3, reconnects=2)
    emit(
        conn,
        PROCESS_HUNG,
        severity="warning",
        platform="huya",
        room_id="660000",
        match_id=match_id,
        detail={"heartbeat_age_ms": 20_000},
        timestamp=BASE_TS,
    )

    health = room_health(conn, match_id, data_root=data_root, now=BASE_TS)[0]
    assert health.restart_count == 3 and health.reconnects == 2
    assert health.last_incident is not None and health.last_incident.kind == PROCESS_HUNG
    assert health.last_incident.payload["heartbeat_age_ms"] == 20_000


def test_health_is_empty_for_match_without_sessions(conn, data_root):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG")
    assert room_health(conn, match_id, data_root=data_root) == []


def test_contribution_counts_per_room_and_dedupes(conn, data_root):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG")
    first_room = add_room(conn, room_id="660000")
    second_room = add_room(conn, room_id="323444")
    first_session = add_session(conn, first_room, match_id)
    second_session = add_session(conn, second_room, match_id)

    shared = make_event(BASE_TS, text="GG", user="u1", room_id="660000")
    add_segment(
        conn,
        first_session,
        ("huya", "660000"),
        [shared, shared, make_event(BASE_TS + 60_000, text="另一条", room_id="660000")],
    )
    add_segment(
        conn,
        second_session,
        ("huya", "323444"),
        [make_event(BASE_TS + 1_000, text="GG", user="u1", room_id="323444")],
    )

    by_room = {item.room_id: item for item in room_contribution(conn, match_id, data_root=data_root)}
    assert set(by_room) == {"660000", "323444"}
    first = by_room["660000"]
    assert (first.msg_count, first.deduped_count, first.duplicate_count) == (3, 2, 1)
    assert (first.first_ts, first.last_ts) == (BASE_TS, BASE_TS + 60_000)
    assert (by_room["323444"].msg_count, by_room["323444"].deduped_count) == (1, 1)
    assert first.session_count == 1


def test_contribution_counts_sessions_across_restarts(conn, data_root):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG")
    room_row = add_room(conn)
    first = add_session(conn, room_row, match_id, restart_count=0)
    second = add_session(conn, room_row, match_id, restart_count=1)
    add_segment(conn, first, ("huya", "660000"), [make_event(BASE_TS, text="a")])
    add_segment(conn, second, ("huya", "660000"), [make_event(BASE_TS + 3_600_000, text="b")], hour_offset=1)

    contribution = room_contribution(conn, match_id, data_root=data_root)[0]
    assert (contribution.msg_count, contribution.deduped_count, contribution.session_count) == (2, 2, 2)


def test_contribution_is_empty_without_segments(conn, data_root):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG")
    assert room_contribution(conn, match_id, data_root=data_root) == []
