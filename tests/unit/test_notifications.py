"""待投递事件出口（`notifications` 表）：采集异常与解读层报警走同一张表。

出口是共享的，因此这里的断言只关于**结构**（kind/severity/payload/state/查询口径）：
kind 的取值由各自的产出方校验（`collect/incidents.py` / `report/llm/alerts.py`）。
"""

from __future__ import annotations

import pytest

from danmu_intel.common.notifications import SEVERITY_ORDER, emit, recent

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
