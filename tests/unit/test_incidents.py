"""采集异常事件的出口（纯库操作，不连网络）。"""

from __future__ import annotations

import json

import pytest

from danmu_intel.collect.incidents import (
    DISK_LOW,
    PROCESS_EXIT,
    STALLED,
    SessionIncidents,
    emit,
    recent,
    worst,
)

from conftest import BASE_TS


def test_emit_writes_pending_notification(conn):
    emit(
        conn,
        PROCESS_EXIT,
        severity="warning",
        platform="huya",
        room_id="660000",
        match_id=3,
        detail={"exit_code": -9},
        timestamp=BASE_TS,
    )
    row = conn.execute("SELECT * FROM notifications").fetchone()
    assert row["kind"] == PROCESS_EXIT and row["severity"] == "warning"
    assert row["state"] == "pending" and row["attempts"] == 0 and row["created_at"] == BASE_TS
    assert json.loads(row["payload_json"]) == {
        "exit_code": -9,
        "match_id": 3,
        "platform": "huya",
        "room_id": "660000",
    }


def test_emit_rejects_unknown_kind_or_severity(conn):
    with pytest.raises(ValueError):
        emit(conn, "not_a_kind", severity="info", platform="huya", room_id="660000")
    with pytest.raises(ValueError):
        emit(conn, PROCESS_EXIT, severity="fatal", platform="huya", room_id="660000")


def test_recent_filters_and_orders(conn):
    emit(conn, PROCESS_EXIT, severity="warning", platform="huya", room_id="660000", match_id=1, timestamp=1)
    emit(conn, DISK_LOW, severity="critical", platform="huya", room_id="323444", match_id=1, timestamp=2)
    emit(conn, STALLED, severity="warning", platform="huya", room_id="660000", match_id=2, timestamp=3)

    assert [item.kind for item in recent(conn)] == [STALLED, DISK_LOW, PROCESS_EXIT]
    assert [item.kind for item in recent(conn, match_id=1)] == [DISK_LOW, PROCESS_EXIT]
    assert [item.kind for item in recent(conn, room_id="660000")] == [STALLED, PROCESS_EXIT]
    assert [item.kind for item in recent(conn, match_id=1, limit=1)] == [DISK_LOW]
    assert recent(conn, match_id=99) == []


def test_session_incidents_reports_each_kind_once(conn):
    sink = SessionIncidents(conn, platform="huya", room_id="660000", match_id=1)
    assert sink.emit_once(STALLED, severity="warning", detail={"silence_ms": 60_000}, timestamp=BASE_TS) is True
    assert sink.emit_once(STALLED, severity="warning", timestamp=BASE_TS + 1) is False
    assert sink.emit_once(DISK_LOW, severity="critical", timestamp=BASE_TS + 2) is True
    incidents = recent(conn, match_id=1)
    assert [item.kind for item in incidents] == [DISK_LOW, STALLED]
    assert incidents[-1].payload["silence_ms"] == 60_000


def test_worst_picks_highest_severity():
    assert worst("info", "warning") == "warning"
    assert worst("warning", "critical", "info") == "critical"
    assert worst("info") == "info"
