"""待投递事件出口（`notifications` 表）：采集异常与解读层报警走同一张表。

出口是共享的，因此这里的断言只关于**结构**（kind/severity/payload/state/查询口径）：
kind 的取值由各自的产出方校验（`collect/incidents.py` / `report/llm/alerts.py`）。
"""

from __future__ import annotations

import pytest

from danmu_intel.common.notifications import (
    DELIVERED,
    DROPPED_EXPIRED,
    FAILED,
    SEVERITY_ORDER,
    SUPPRESSED,
    counts,
    emit,
    get,
    mark_delivered,
    mark_dropped_expired,
    mark_failed,
    mark_suppressed,
    pending,
    recent,
    record_attempt,
)

from conftest import BASE_TS

COLLECT_KIND = "process_exit"
LLM_KIND = "llm_cost_gate"


def test_emit_writes_a_pending_event(conn):
    event_id = emit(
        conn,
        LLM_KIND,
        severity="warning",
        payload={"match_id": 7, "spent_match_cny": 0.31},
        timestamp=BASE_TS,
    )
    row = conn.execute("SELECT * FROM notifications WHERE id=?", (event_id,)).fetchone()
    assert row["kind"] == LLM_KIND
    assert row["severity"] == "warning"
    assert row["state"] == "pending" and row["attempts"] == 0
    assert row["created_at"] == BASE_TS


def test_emit_rejects_unknown_severity(conn):
    with pytest.raises(ValueError, match="未知的严重级别"):
        emit(conn, LLM_KIND, severity="fatal", payload={})


def test_severity_order_covers_the_three_levels():
    assert sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get) == ["info", "warning", "critical"]


def test_recent_filters_by_kind_match_and_room(conn):
    emit(conn, COLLECT_KIND, severity="warning", payload={"match_id": 1, "platform": "huya", "room_id": "660000"})
    emit(conn, LLM_KIND, severity="warning", payload={"match_id": 1})
    emit(conn, LLM_KIND, severity="critical", payload={"match_id": 2})

    assert [item.kind for item in recent(conn, kind=LLM_KIND)] == [LLM_KIND, LLM_KIND]
    assert [item.kind for item in recent(conn, match_id=1)] == [LLM_KIND, COLLECT_KIND]
    assert [item.kind for item in recent(conn, match_id=2)] == [LLM_KIND]
    assert [item.kind for item in recent(conn, kind=LLM_KIND, match_id=1, limit=1)] == [LLM_KIND]
    assert recent(conn, room_id="660000") == [item for item in recent(conn) if item.kind == COLLECT_KIND]
    assert recent(conn, match_id=99) == []


def test_recent_exposes_payload_and_state(conn):
    emit(conn, LLM_KIND, severity="critical", payload={"match_id": 7, "reason": "连续 3 次调用失败"})
    item = recent(conn)[0]
    assert item.payload["reason"] == "连续 3 次调用失败"
    assert item.state == "pending"
    assert item.id > 0 and item.created_at > 0
    assert item.channel is None and item.delivered_at is None and item.attempts == 0


def test_pending_is_oldest_first_and_excludes_terminal_states(conn):
    first = emit(conn, LLM_KIND, severity="warning", payload={"match_id": 1}, timestamp=1)
    second = emit(conn, COLLECT_KIND, severity="warning", payload={"match_id": 2}, timestamp=2)
    emit(conn, LLM_KIND, severity="warning", payload={"match_id": 3}, timestamp=3)

    assert [item.id for item in pending(conn)] == [first, second, 3]
    assert [item.id for item in pending(conn, limit=2)] == [first, second]

    mark_delivered(conn, first, channel="qq", at=BASE_TS)
    mark_suppressed(conn, second)
    assert [item.id for item in pending(conn)] == [3]


def test_state_transitions_are_recorded(conn):
    delivered_id = emit(conn, LLM_KIND, severity="warning", payload={}, timestamp=BASE_TS)
    assert record_attempt(conn, delivered_id) == 1
    assert record_attempt(conn, delivered_id) == 2
    item = mark_delivered(conn, delivered_id, channel="qq+telegram", at=BASE_TS + 5)
    assert (item.state, item.channel, item.delivered_at, item.attempts) == (DELIVERED, "qq+telegram", BASE_TS + 5, 2)

    suppressed_id = emit(conn, LLM_KIND, severity="warning", payload={}, timestamp=BASE_TS)
    assert mark_suppressed(conn, suppressed_id).state == SUPPRESSED

    expired_id = emit(conn, LLM_KIND, severity="warning", payload={}, timestamp=BASE_TS)
    assert mark_dropped_expired(conn, expired_id).state == DROPPED_EXPIRED

    failed_id = emit(conn, LLM_KIND, severity="warning", payload={}, timestamp=BASE_TS)
    assert mark_failed(conn, failed_id).state == FAILED

    assert all(item.state != "pending" for item in recent(conn))


def test_get_and_state_writes_reject_unknown_id(conn):
    assert get(conn, 42) is None
    with pytest.raises(LookupError, match="通知不存在"):
        mark_failed(conn, 42)


def test_counts_groups_by_state(conn):
    assert counts(conn) == {}
    first = emit(conn, LLM_KIND, severity="warning", payload={}, timestamp=BASE_TS)
    emit(conn, LLM_KIND, severity="warning", payload={}, timestamp=BASE_TS)
    mark_delivered(conn, first, channel="qq", at=BASE_TS)
    assert counts(conn) == {"pending": 1, "delivered": 1}
