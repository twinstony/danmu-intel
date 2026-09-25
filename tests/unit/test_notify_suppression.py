"""告警抑制与恢复（`alerts` 台账）：同 alert_key 冷却期内只发一次、恢复发一次。

时间一律用显式时间戳传入（`at=` / `timestamp=`），因此冷却期的断言与"今天是哪一天"、
"notifier 什么时候跑"都无关。
"""

from __future__ import annotations

import pytest

from danmu_intel.common.notifications import emit, get, recent
from danmu_intel.notify.suppression import (
    FIRING,
    RECOVERY_SUFFIX,
    RESOLVED,
    admit,
    alert_key,
    get_alert,
    is_recovery,
    list_alerts,
    note_sent,
    resolve,
)

from conftest import BASE_TS

COOLDOWN_MS = 900_000


def test_alert_key_uses_only_identity_fields():
    assert alert_key("disk_low", {"platform": "huya", "room_id": "660000", "free_bytes": 1}) == alert_key(
        "disk_low", {"room_id": "660000", "platform": "huya", "free_bytes": 999}
    )
    assert alert_key("disk_low", {"platform": "huya", "room_id": "660000"}) != alert_key(
        "disk_low", {"platform": "huya", "room_id": "323444"}
    )
    assert alert_key("disk_low", {"platform": "huya", "room_id": "660000"}) != alert_key(
        "process_exit", {"platform": "huya", "room_id": "660000"}
    )


def test_alert_key_without_identity_or_with_explicit_override():
    assert alert_key("release.failed", {"release": 3, "reason": "检查不过"}) == "release.failed"
    assert alert_key("disk_low", {"alert_key": "my-key", "room_id": "1"}) == "disk_low:my-key"


def test_first_occurrence_sends_and_counts(conn):
    key = alert_key("disk_low", {"platform": "huya", "room_id": "660000"})

    verdict = admit(conn, "disk_low", key=key, at=BASE_TS, cooldown_ms=COOLDOWN_MS)

    assert verdict.send is True and verdict.reason == "first"
    alert = get_alert(conn, key)
    assert (alert.state, alert.count, alert.first_seen, alert.last_seen) == (FIRING, 1, BASE_TS, BASE_TS)
    assert alert.last_sent_at is None


def test_second_occurrence_inside_cooldown_is_suppressed(conn):
    key = alert_key("disk_low", {"platform": "huya", "room_id": "660000"})
    admit(conn, "disk_low", key=key, at=BASE_TS, cooldown_ms=COOLDOWN_MS)
    note_sent(conn, key, at=BASE_TS)

    verdict = admit(conn, "disk_low", key=key, at=BASE_TS + 60_000, cooldown_ms=COOLDOWN_MS)

    assert verdict.send is False and verdict.reason == "cooldown"
    alert = get_alert(conn, key)
    assert alert.count == 2 and alert.last_seen == BASE_TS + 60_000


def test_same_alert_sends_again_after_the_cooldown(conn):
    key = alert_key("disk_low", {"platform": "huya", "room_id": "660000"})
    admit(conn, "disk_low", key=key, at=BASE_TS, cooldown_ms=COOLDOWN_MS)
    note_sent(conn, key, at=BASE_TS)

    verdict = admit(conn, "disk_low", key=key, at=BASE_TS + COOLDOWN_MS, cooldown_ms=COOLDOWN_MS)

    assert verdict.send is True and verdict.reason == "recurred"
    assert get_alert(conn, key).count == 2


def test_a_different_alert_key_is_not_affected(conn):
    first = alert_key("stalled", {"platform": "huya", "room_id": "660000"})
    second = alert_key("stalled", {"platform": "huya", "room_id": "323444"})
    admit(conn, "stalled", key=first, at=BASE_TS, cooldown_ms=COOLDOWN_MS)
    note_sent(conn, first, at=BASE_TS)

    assert admit(conn, "stalled", key=second, at=BASE_TS + 1, cooldown_ms=COOLDOWN_MS).send is True


def test_resolve_records_recovery_and_emits_one_notification(conn):
    key = alert_key("chain_rate_limited", {"provider": "polygonscan"})
    admit(conn, "chain_rate_limited", key=key, at=BASE_TS, cooldown_ms=COOLDOWN_MS)
    note_sent(conn, key, at=BASE_TS)

    alert = resolve(
        conn,
        "chain_rate_limited",
        identity={"provider": "polygonscan"},
        detail={"reason": "又通了"},
        timestamp=BASE_TS + 10_000,
    )

    assert (alert.state, alert.resolved_at) == (RESOLVED, BASE_TS + 10_000)
    [recovery] = recent(conn, limit=5)
    assert recovery.kind == f"chain_rate_limited{RECOVERY_SUFFIX}"
    assert recovery.severity == "info"
    assert recovery.payload == {"provider": "polygonscan", "resolved": True, "alert": "chain_rate_limited", "reason": "又通了"}
    assert is_recovery(recovery) is True


def test_resolve_is_idempotent_and_silent_when_nothing_was_firing(conn):
    assert resolve(conn, "chain_fetch_failed", identity={"provider": "helius"}) is None
    assert recent(conn) == []

    key = alert_key("chain_fetch_failed", {"provider": "helius"})
    admit(conn, "chain_fetch_failed", key=key, at=BASE_TS, cooldown_ms=COOLDOWN_MS)
    note_sent(conn, key, at=BASE_TS)
    resolve(conn, "chain_fetch_failed", identity={"provider": "helius"}, timestamp=BASE_TS + 1)
    assert resolve(conn, "chain_fetch_failed", identity={"provider": "helius"}, timestamp=BASE_TS + 2) is None

    kinds = [item.kind for item in recent(conn, limit=10)]
    assert kinds.count(f"chain_fetch_failed{RECOVERY_SUFFIX}") == 1


def test_recovery_then_new_incident_sends_again_immediately(conn):
    key = alert_key("chain_rate_limited", {"provider": "helius"})
    admit(conn, "chain_rate_limited", key=key, at=BASE_TS, cooldown_ms=COOLDOWN_MS)
    note_sent(conn, key, at=BASE_TS)
    resolve(conn, "chain_rate_limited", identity={"provider": "helius"}, timestamp=BASE_TS + 1000)

    verdict = admit(conn, "chain_rate_limited", key=key, at=BASE_TS + 2000, cooldown_ms=COOLDOWN_MS)

    assert verdict.send is True and verdict.reason == "recurred"
    alert = get_alert(conn, key)
    assert (alert.state, alert.count, alert.first_seen, alert.last_sent_at, alert.resolved_at) == (
        FIRING,
        1,
        BASE_TS + 2000,
        None,
        None,
    )


def test_list_alerts_filters_and_orders_by_last_seen(conn):
    quiet = alert_key("disk_low", {"platform": "huya", "room_id": "1"})
    loud = alert_key("disk_low", {"platform": "soop", "room_id": "2"})
    admit(conn, "disk_low", key=quiet, at=BASE_TS, cooldown_ms=COOLDOWN_MS)
    admit(conn, "disk_low", key=loud, at=BASE_TS + 5000, cooldown_ms=COOLDOWN_MS)

    assert [alert.alert_key for alert in list_alerts(conn)] == [loud, quiet]
    assert [alert.alert_key for alert in list_alerts(conn, limit=1)] == [loud]
    assert [alert.alert_key for alert in list_alerts(conn, state=FIRING)] == [loud, quiet]
    assert list_alerts(conn, state=RESOLVED) == []


def test_unknown_state_reports_missing_alert(conn):
    with pytest.raises(LookupError):
        note_sent(conn, "没有这个 key", at=BASE_TS)


def test_recovery_payload_is_kept_out_of_the_firing_ledger(conn):
    """恢复通知本身不改台账（`resolve` 已经把台账转 resolved）。"""
    key = alert_key("llm_cost_gate", {"match_id": 7})
    admit(conn, "llm_cost_gate", key=key, at=BASE_TS, cooldown_ms=COOLDOWN_MS)
    note_sent(conn, key, at=BASE_TS)
    resolve(conn, "llm_cost_gate", identity={"match_id": 7}, timestamp=BASE_TS + 1)

    item = recent(conn, limit=1)[0]
    assert get(conn, item.id).state == "pending"
    assert get_alert(conn, key).state == RESOLVED
    emit(conn, "llm_cost_gate", severity="warning", payload={"match_id": 7}, timestamp=BASE_TS + 2)
    assert list_alerts(conn, state=FIRING) == []
