"""通知投递：统一 5 分钟时效闸门 + 冷却期抑制 + 重试与销毁。

测试缝是**注入的假通道 + 显式时间戳**：`deliver_once(now=…)` 不读真实时钟，
`run_loop` 的 `clock`/`sleep` 也可注入，因此"5 分钟""30 秒"这些数字不需要真的等。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.common.notifications import (
    DELIVERED,
    DROPPED_EXPIRED,
    FAILED,
    PENDING,
    SUPPRESSED,
    emit,
    get,
    recent,
)
from danmu_intel.notify.channels import ChannelError, ChannelSet
from danmu_intel.notify.config import NotifyConfig, load_notify_config, save_notify_config
from danmu_intel.notify.notifier import (
    ACTION_DELIVERY_FAILED,
    ACTION_DROPPED,
    deliver_once,
    run_loop,
)
from danmu_intel.notify.suppression import FIRING, RECOVERY_SUFFIX, admit, alert_key, get_alert, note_sent, resolve

from conftest import BASE_TS, FakeChannel

GATE_MS = 5 * 60 * 1000
CFG = NotifyConfig()


def disk_low(conn, *, at=BASE_TS, room="660000", severity="critical", detail=None):
    return emit(
        conn,
        "disk_low",
        severity=severity,
        payload={"match_id": 1, "platform": "huya", "room_id": room, **(detail or {})},
        timestamp=at,
    )


def test_pending_event_is_delivered_with_the_channel_name(conn):
    event_id = disk_low(conn)
    qq = FakeChannel("qq")

    result = deliver_once(conn, channels=ChannelSet(primary=qq), config=CFG, now=BASE_TS + 1000)

    assert result.summary() == "扫描 1 条｜送达 1｜抑制 0｜销毁 0｜待重试 0"
    assert [item.state for item in result.outcomes] == [DELIVERED]
    assert result.delivered[0].channel == "qq"
    assert "【严重】磁盘将满｜比赛 #1｜huya/660000" in qq.texts[0]
    stored = get(conn, event_id)
    assert (stored.state, stored.channel, stored.delivered_at) == (DELIVERED, "qq", BASE_TS + 1000)
    assert get_alert(conn, alert_key("disk_low", {"match_id": 1, "platform": "huya", "room_id": "660000"})) is not None


def test_event_older_than_the_gate_is_destroyed_without_sending(conn):
    event_id = disk_low(conn, at=BASE_TS)
    qq = FakeChannel("qq")

    result = deliver_once(conn, channels=ChannelSet(primary=qq), config=CFG, now=BASE_TS + GATE_MS + 1)

    assert qq.texts == []  # 销毁不补发
    assert [item.state for item in result.outcomes] == [DROPPED_EXPIRED]
    assert get(conn, event_id).state == DROPPED_EXPIRED
    [entry] = [row for row in _audit(conn) if row["action"] == ACTION_DROPPED]
    assert entry["detail"]["age_ms"] == GATE_MS + 1


def test_event_exactly_at_the_gate_is_still_sent(conn):
    """闸门是"超过 5 分钟"，不是"到 5 分钟"。"""
    disk_low(conn, at=BASE_TS)
    qq = FakeChannel("qq")

    result = deliver_once(conn, channels=ChannelSet(primary=qq), config=CFG, now=BASE_TS + GATE_MS)

    assert [item.state for item in result.outcomes] == [DELIVERED]


def test_same_alert_key_is_sent_once_inside_the_cooldown(conn):
    first, second = disk_low(conn, at=BASE_TS), disk_low(conn, at=BASE_TS + 60_000)
    qq = FakeChannel("qq")

    result = deliver_once(conn, channels=ChannelSet(primary=qq), config=CFG, now=BASE_TS + 61_000)

    assert [item.state for item in result.outcomes] == [DELIVERED, SUPPRESSED]
    assert len(qq.texts) == 1
    assert get(conn, first).state == DELIVERED and get(conn, second).state == SUPPRESSED
    key = alert_key("disk_low", {"match_id": 1, "platform": "huya", "room_id": "660000"})
    assert get_alert(conn, key).count == 2  # 发生 2 次、只发 1 次


def test_same_alert_key_sends_again_after_the_cooldown(conn):
    first = disk_low(conn, at=BASE_TS)
    qq = FakeChannel("qq")
    deliver_once(conn, channels=ChannelSet(primary=qq), config=CFG, now=BASE_TS + 1000)

    second = disk_low(conn, at=BASE_TS + CFG.cooldown_ms + 1000)
    result = deliver_once(
        conn, channels=ChannelSet(primary=qq), config=CFG, now=BASE_TS + CFG.cooldown_ms + 1500
    )

    assert [item.state for item in result.outcomes] == [DELIVERED]
    assert len(qq.texts) == 2
    assert (get(conn, first).state, get(conn, second).state) == (DELIVERED, DELIVERED)


def test_different_rooms_are_different_alerts(conn):
    disk_low(conn, room="660000")
    disk_low(conn, room="323444")
    qq = FakeChannel("qq")

    result = deliver_once(conn, channels=ChannelSet(primary=qq), config=CFG, now=BASE_TS + 1)

    assert [item.state for item in result.outcomes] == [DELIVERED, DELIVERED]


def test_failed_delivery_retries_then_destroys(conn):
    event_id = disk_low(conn)
    qq, tg = FakeChannel("qq", fails=99), FakeChannel("telegram", fails=99)
    channel_set = ChannelSet(primary=qq, backup=tg)

    first = deliver_once(conn, channels=channel_set, config=CFG, now=BASE_TS + 1000)
    assert [item.state for item in first.outcomes] == [PENDING]
    assert get(conn, event_id).attempts == 1

    second = deliver_once(conn, channels=channel_set, config=CFG, now=BASE_TS + 31_000)
    assert [item.state for item in second.outcomes] == [PENDING]
    assert get(conn, event_id).attempts == 2

    third = deliver_once(conn, channels=channel_set, config=CFG, now=BASE_TS + 61_000)
    assert [item.state for item in third.outcomes] == [FAILED]
    assert get(conn, event_id).state == FAILED

    # 终态不再被扫描（既不再试，也不补发）
    assert deliver_once(conn, channels=channel_set, config=CFG, now=BASE_TS + 91_000).outcomes == []


def test_delivery_failure_is_audited_and_lists_every_channel(conn):
    disk_low(conn)
    channel_set = ChannelSet(primary=FakeChannel("qq", fails=1), backup=FakeChannel("telegram", fails=1))

    result = deliver_once(conn, channels=channel_set, config=CFG, now=BASE_TS + 1000)

    assert "qq" in (result.outcomes[0].detail or "") and "telegram" in (result.outcomes[0].detail or "")
    [entry] = [row for row in _audit(conn) if row["action"] == ACTION_DELIVERY_FAILED]
    assert entry["actor"] == "notifier" and entry["detail"]["attempts"] == 1
    assert entry["detail"]["max_attempts"] == 3


def test_backup_channel_covers_a_dead_primary(conn):
    disk_low(conn, severity="warning")
    channel_set = ChannelSet(primary=FakeChannel("qq", fails=1), backup=FakeChannel("telegram"))

    result = deliver_once(conn, channels=channel_set, config=CFG, now=BASE_TS + 1)

    assert result.delivered[0].channel == "telegram"


def test_critical_goes_to_both_channels(conn):
    disk_low(conn, severity="critical")
    qq, tg = FakeChannel("qq"), FakeChannel("telegram")

    result = deliver_once(conn, channels=ChannelSet(primary=qq, backup=tg), config=CFG, now=BASE_TS + 1)

    assert result.delivered[0].channel == "qq+telegram"
    assert len(qq.texts) == 1 and len(tg.texts) == 1


def test_recovery_notification_is_sent_and_does_not_arm_the_ledger(conn):
    key = alert_key("disk_low", {"match_id": 1, "platform": "huya", "room_id": "660000"})
    admit(conn, "disk_low", key=key, at=BASE_TS, cooldown_ms=CFG.cooldown_ms)
    note_sent(conn, key, at=BASE_TS)
    resolve(conn, "disk_low", identity={"match_id": 1, "platform": "huya", "room_id": "660000"}, timestamp=BASE_TS + 1)
    disk_low(conn, at=BASE_TS + 2)  # 恢复后的新一轮：不受冷却期压制

    qq = FakeChannel("qq")
    result = deliver_once(conn, channels=ChannelSet(primary=qq), config=CFG, now=BASE_TS + 3000)

    assert [item.state for item in result.outcomes] == [DELIVERED, DELIVERED]
    assert "（已恢复）" in qq.texts[0]
    kinds = [item.kind for item in recent(conn, limit=10)]
    assert kinds.count(f"disk_low{RECOVERY_SUFFIX}") == 1
    # 恢复通知本身没有把台账重新点着：而是随后那条真告警把它点着的
    assert get_alert(conn, key).state == FIRING


def test_max_attempts_comes_from_the_config_table(conn):
    save_notify_config(conn, actor="管理员", changes={"max_attempts": 1}, ts=BASE_TS)
    disk_low(conn)
    channel_set = ChannelSet(primary=FakeChannel("qq", fails=1))

    result = deliver_once(conn, channels=channel_set, now=BASE_TS + 1)

    assert [item.state for item in result.outcomes] == [FAILED]


def test_notify_config_defaults_and_overrides(conn):
    assert load_notify_config(conn) == NotifyConfig(gate_ms=GATE_MS, cooldown_ms=900_000, scan_interval_s=30.0, max_attempts=3)
    updated = save_notify_config(conn, actor="管理员", changes={"cooldown_ms": 60_000}, ts=BASE_TS)
    assert updated.cooldown_ms == 60_000
    assert load_notify_config(conn).cooldown_ms == 60_000
    [entry] = [row for row in _audit(conn) if row["action"] == "config.update" and row["target"] == "notify"]
    assert entry["detail"]["before"]["cooldown_ms"] == 900_000
    assert entry["detail"]["after"]["cooldown_ms"] == 60_000


def test_notify_config_rejects_unknown_keys(conn):
    with pytest.raises(ValueError, match="未知的通知配置项"):
        save_notify_config(conn, actor="管理员", changes={"cooldown_s": 60}, ts=BASE_TS)


def test_run_loop_runs_one_pass_for_zero_seconds(conn):
    disk_low(conn)
    qq = FakeChannel("qq")
    sleeps: list[float] = []

    passes = run_loop(
        conn,
        channels=ChannelSet(primary=qq),
        config=CFG,
        seconds=0,
        clock=lambda: 0.0,
        sleep=sleeps.append,
        now=lambda: BASE_TS + 1000,
    )

    assert passes == 1 and sleeps == [] and len(qq.texts) == 1


def test_run_loop_scans_every_interval_and_reports_each_pass(conn):
    disk_low(conn)
    qq = FakeChannel("qq")
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    seen: list[str] = []
    passes = run_loop(
        conn,
        channels=ChannelSet(primary=qq),
        config=CFG,
        seconds=45.0,
        clock=lambda: now[0],
        sleep=sleep,
        now=lambda: BASE_TS + 1000,
        on_pass=lambda result: seen.append(result.summary()),
    )

    assert passes == 2 and sleeps == [30.0, 15.0]
    assert seen[0] == "扫描 1 条｜送达 1｜抑制 0｜销毁 0｜待重试 0"
    assert seen[1] == "没有待投递事件"


def _audit(conn):
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    return [
        {"actor": row["actor"], "action": row["action"], "target": row["target"], "detail": json.loads(row["detail_json"])}
        for row in rows
    ]


def test_summary_labels_cover_every_state():
    import dataclasses

    from danmu_intel.notify.notifier import Outcome

    labels = {}
    for state in (DELIVERED, SUPPRESSED, DROPPED_EXPIRED, FAILED, PENDING):
        labels[state] = Outcome(id=1, kind="disk_low", severity="warning", state=state).label
    assert labels == {
        DELIVERED: "已送达",
        SUPPRESSED: "冷却期内不发",
        DROPPED_EXPIRED: "超时销毁",
        FAILED: "重试用尽销毁",
        PENDING: "待重试",
    }
    assert dataclasses.is_dataclass(Outcome)


def test_recent_still_shows_terminal_events_for_operators(conn):
    disk_low(conn)
    deliver_once(conn, channels=ChannelSet(primary=FakeChannel("qq")), config=CFG, now=BASE_TS + 1)

    [item] = recent(conn)
    assert item.state == DELIVERED and item.channel == "qq"
