"""T11 验收：**10 类事件 5 分钟内送达**，超时销毁不补发（AC-11 / NFR-T-5 / ADR-0010）。

十类事件一律通过**各自的出口模块**注入（采集异常走 `collect/incidents.py`、链上走
`chain/alerts.py`、会员批处理走真的 `members.sweep` 失败路径；发布与付费那几条的出口
嵌在完整流程里，这里直接用它们定义的 kind 常量写进共享出口 —— 出口本身由各自模块保证）。
投递侧全是假通道 + 注入时间戳，因此**不连外网、不碰真凭据**。
"""

from __future__ import annotations

import pytest

from danmu_intel.billing import members, settle
from danmu_intel.chain import alerts as chain_alerts
from danmu_intel.collect import incidents
from danmu_intel.common import notifications
from danmu_intel.common.notifications import recent
from danmu_intel.notify import KIND_LABELS, ChannelSet, deliver_once
from danmu_intel.notify.notifier import ACTION_DELIVERY_FAILED
from danmu_intel.publish import release
from danmu_intel.report.llm import alerts as llm_alerts

from conftest import BASE_TS, FakeChannel

GATE_MS = 5 * 60 * 1000
MINUTE = 60_000
DAY = 24 * 3600 * 1000
ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
ROOM = {"platform": "huya", "room_id": "660000", "match_id": 1}

#: 设计 §15 的十行 ↔ 出口产出的 kind。
EVENT_KINDS = (
    incidents.PROCESS_EXIT,  # 1 采集进程退出 / 重启超限
    incidents.NO_STREAM,  # 2 采集静默
    incidents.DROP_RATE_HIGH,  # 3 落盘丢包率超阈
    incidents.DISK_LOW,  # 4 磁盘将满
    release.KIND_PUBLISH_FAILED,  # 5 发布失败 / 检查不通过
    chain_alerts.RATE_LIMITED,  # 6 链上检测异常 / 额度受限
    chain_alerts.QUOTA_HIGH,
    settle.GRANT_FAILED,  # 7 付款到账但开通失败
    settle.PAYMENT_SHORT,  # 8 订单待补款
    llm_alerts.COST_GATE,  # 9 LLM 降级 / 成本超闸
    llm_alerts.UNAVAILABLE,
    members.MEMBER_SWEEP_FAILED,  # 10 成员批处理失败
)


def inject_collection_incidents(conn, *, at: int) -> None:
    """采集四类（退出 / 静默 / 丢包 / 磁盘）走同一个出口，各写一行。"""
    for kind, severity, detail in (
        (incidents.PROCESS_EXIT, "warning", {"exit_code": -9}),
        (incidents.NO_STREAM, "warning", {"wait_s": 120}),
        (incidents.DROP_RATE_HIGH, "warning", {"drop_count": 3, "msg_count": 10, "ratio": 0.3, "threshold": 0.02}),
        (incidents.DISK_LOW, "critical", {"free_bytes": 1, "minimum_bytes": 5 * 1024**3}),
    ):
        incidents.emit(conn, kind, severity=severity, detail=detail, timestamp=at, **ROOM)  # type: ignore[arg-type]


def inject_publish_and_chain_and_billing(conn, *, at: int) -> None:
    """发布失败、链上两类、付款两类、解读层两类 —— 都用各模块自己的 kind。"""
    notifications.emit(
        conn,
        release.KIND_PUBLISH_FAILED,
        severity="critical",
        payload={"release": 3, "reason": "检查不过", "failed_checks": ["sources_reachable"]},
        timestamp=at,
    )
    chain_alerts.alert(conn, chain_alerts.RATE_LIMITED, provider="polygonscan", detail={"scope": ADDRESS}, timestamp=at)
    chain_alerts.alert(conn, chain_alerts.QUOTA_HIGH, provider="helius", detail={"used": 9, "cap": 10}, timestamp=at)
    notifications.emit(
        conn,
        settle.GRANT_FAILED,
        severity="critical",
        payload={"order_ref": "DM1A2B3C4D", "tx_ref": "0x1", "member_id": 1, "reason": "写库失败"},
        timestamp=at,
    )
    notifications.emit(
        conn,
        settle.PAYMENT_SHORT,
        severity="warning",
        payload={"order_ref": "DM5E6F7A8B", "shortage_units": 5, "amount_due_units": 10, "paid_units": 5},
        timestamp=at,
    )
    llm_alerts.alert(conn, llm_alerts.COST_GATE, match_id=1, severity="warning", detail={"spent_match_cny": 0.31}, timestamp=at)
    llm_alerts.alert(conn, llm_alerts.UNAVAILABLE, match_id=1, severity="critical", detail={"failures": 3}, timestamp=at)


def inject_member_sweep_failure(conn, *, at: int, monkeypatch) -> None:
    """成员批处理失败：**真的跑一次会失败的 sweep**（不直接写通知行）。"""
    first = members.get_or_create_member(conn, platform="qq", username="12345678", tier="trial", now=at)
    second = members.get_or_create_member(conn, platform="telegram", username="@tonychan", tier="trial", now=at)
    members.grant(conn, member_id=first.id, tier="trial", tx_ref="0x1", now=at)
    members.grant(conn, member_id=second.id, tier="trial", tx_ref="0x2", now=at)
    # 两个人的到期日都往前拨过 `at`：于是这次 sweep 就是「两个都该降级」的那一批
    conn.execute("UPDATE members SET expires_at=? WHERE id IN (?, ?)", (at - DAY, first.id, second.id))
    conn.commit()

    original = members.get_member

    def flaky(connection, member_id):
        if member_id == second.id:
            raise RuntimeError("会员行读不出来")
        return original(connection, member_id)

    monkeypatch.setattr(members, "get_member", flaky)
    members.sweep(conn, now=at)


def test_ten_event_types_are_all_delivered_within_the_five_minute_gate(conn, monkeypatch):
    inject_collection_incidents(conn, at=BASE_TS)
    inject_publish_and_chain_and_billing(conn, at=BASE_TS)
    inject_member_sweep_failure(conn, at=BASE_TS, monkeypatch=monkeypatch)
    assert len(notifications.pending(conn)) == 12
    assert notifications.pending(conn)[-1].created_at == BASE_TS, "所有事件共用同一个时间基准"

    qq, telegram = FakeChannel("qq"), FakeChannel("telegram")
    result = deliver_once(
        conn, channels=ChannelSet(primary=qq, backup=telegram), now=BASE_TS + 30_000
    )

    assert result.summary() == "扫描 12 条｜送达 12｜抑制 0｜销毁 0｜待重试 0"
    delivered = recent(conn, limit=20)
    assert all(item.state == "delivered" for item in delivered)
    assert all(item.delivered_at is not None and item.delivered_at - item.created_at <= GATE_MS for item in delivered)
    # 设计 §15 的每一行都在里面（十类事件的 kind 全覆盖）
    assert set(EVENT_KINDS) <= {item.kind for item in delivered}
    # 中文事件名齐全：运维看到的一律是中文，不是 kind
    assert set(EVENT_KINDS) <= set(KIND_LABELS)

    # 高危主备都发、中低只发主通道
    critical = {item.kind for item in delivered if item.severity == "critical"}
    assert len(qq.texts) == 12
    assert len(telegram.texts) == len(critical) == 6
    assert all("【" in text for text in qq.texts)


def test_event_not_delivered_within_the_gate_is_destroyed_without_a_resend(conn):
    incidents.emit(conn, incidents.DISK_LOW, severity="critical", timestamp=BASE_TS, **ROOM)  # type: ignore[arg-type]
    qq = FakeChannel("qq")

    # notifier 整整晚了 5 分零 1 毫秒才轮到它（进程没在跑 / 卡住）
    result = deliver_once(conn, channels=ChannelSet(primary=qq), config=None, now=BASE_TS + GATE_MS + 1)

    assert [item.state for item in result.outcomes] == ["dropped_expired"]
    assert qq.texts == [], "销毁不补发"
    assert notifications.recent(conn)[0].state == "dropped_expired"
    # 销毁也要留痕（谁被销毁了、为什么）
    assert any(
        row["action"] == "notify.dropped_expired" for row in conn.execute("SELECT action FROM audit_log")
    )


def test_repeated_alerts_inside_the_cooldown_are_sent_once(conn):
    """同一个房间反复触发同一类异常：冷却期内只发一次（余下的留痕不发）。"""
    for index in range(5):
        incidents.emit(
            conn,
            incidents.STALLED,
            severity="warning",
            timestamp=BASE_TS + index * 1000,
            detail={"silence_s": 60},
            **ROOM,  # type: ignore[arg-type]
        )
    qq = FakeChannel("qq")

    result = deliver_once(conn, channels=ChannelSet(primary=qq), now=BASE_TS + 5000)

    assert len(result.delivered) == 1 and len(result.suppressed) == 4
    assert len(qq.texts) == 1
    states = [item.state for item in recent(conn, limit=10)]
    assert states.count("delivered") == 1 and states.count("suppressed") == 4


def test_offline_delivery_is_not_silent_and_eventually_destroys(conn):
    """断网/限速：每次尝试都留痕，重试 2 次后销毁（不假装送达、也不无限重试）。"""
    incidents.emit(conn, incidents.DISK_LOW, severity="critical", timestamp=BASE_TS, **ROOM)  # type: ignore[arg-type]
    channels = ChannelSet(primary=FakeChannel("qq", fails=99), backup=FakeChannel("telegram", fails=99))

    states = []
    for offset in (0, 30_000, 60_000):
        result = deliver_once(conn, channels=channels, now=BASE_TS + offset + 1000)
        states.append(result.outcomes[0].state)

    assert states == ["pending", "pending", "failed"]
    assert notifications.recent(conn)[0].state == "failed"
    failures = [
        row for row in conn.execute("SELECT * FROM audit_log").fetchall()
        if row["action"] == ACTION_DELIVERY_FAILED
    ]
    assert len(failures) == 3, "每一次失败都要留痕"
    detail = failures[0]["detail_json"]
    assert "qq" in detail and "telegram" in detail
    assert '"max_attempts": 3' in detail and '"attempts": 1' in detail


@pytest.mark.parametrize("kind", EVENT_KINDS)
def test_every_event_kind_has_a_chinese_headline(kind):
    """十类事件都有人在看的名字（运维不该只看到 `kind`）。"""
    assert KIND_LABELS[kind]
