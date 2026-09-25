"""统一 5 分钟时效闸门的投递循环（ADR-0010 / 设计 §15，需求 NFR-T-5）。

**一套逻辑覆盖所有事件类型**（用户明示，不可有例外）：

1. 扫 `notifications(state='pending')`（旧→新），每 30 秒一遍；
2. **超 5 分钟未送达 → `dropped_expired`（销毁，不补发）**——先查闸门再谈发送，
   过期的通知不许再送出去；
3. 同一 `alert_key` 在冷却期（15 分钟）内只发一次，后来者记 `suppressed`（留痕不发）；
4. 其余按级别投递（QQ Bot 主 + Telegram 备），成功记 `delivered` + 通道名；
5. 投递失败 → 记一次尝试 + 写 `audit_log`（**断网 / 限速不静默**），下次扫描再试；
   尝试次数（首次 + 重试 2 次）用尽 → `failed`（销毁）。

失败与销毁都落在库里（`notifications.state` + `audit_log`），因此"没送到"这件事
事后查得清，而不是只活在进程日志里。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from danmu_intel.common import audit, notifications
from danmu_intel.common.notifications import (
    DELIVERED,
    DROPPED_EXPIRED,
    FAILED,
    PENDING,
    SUPPRESSED,
    Notification,
)

from danmu_intel.notify.channels import ChannelError, ChannelSet
from danmu_intel.notify.config import NotifyConfig, load_notify_config
from danmu_intel.notify.render import format_message
from danmu_intel.notify.suppression import admit, alert_key, is_recovery, note_sent

ACTOR = "notifier"
ACTION_DELIVERY_FAILED = "notify.delivery_failed"
ACTION_DROPPED = "notify.dropped_expired"


@dataclass(frozen=True, slots=True)
class Outcome:
    """一条待投递事件在本轮里的下场。"""

    id: int
    kind: str
    severity: str
    state: str  # delivered | suppressed | dropped_expired | failed | pending（待重试）
    channel: str | None = None
    detail: str | None = None

    @property
    def label(self) -> str:
        return {
            DELIVERED: "已送达",
            SUPPRESSED: "冷却期内不发",
            DROPPED_EXPIRED: "超时销毁",
            FAILED: "重试用尽销毁",
            PENDING: "待重试",
        }[self.state]


@dataclass(frozen=True, slots=True)
class Pass:
    """一轮扫描的结果。"""

    outcomes: list[Outcome] = field(default_factory=list)

    def by_state(self, state: str) -> list[Outcome]:
        return [item for item in self.outcomes if item.state == state]

    @property
    def delivered(self) -> list[Outcome]:
        return self.by_state(DELIVERED)

    @property
    def suppressed(self) -> list[Outcome]:
        return self.by_state(SUPPRESSED)

    @property
    def dropped(self) -> list[Outcome]:
        return self.by_state(DROPPED_EXPIRED) + self.by_state(FAILED)

    @property
    def retrying(self) -> list[Outcome]:
        return self.by_state(PENDING)

    def summary(self) -> str:
        if not self.outcomes:
            return "没有待投递事件"
        return (
            f"扫描 {len(self.outcomes)} 条｜送达 {len(self.delivered)}"
            f"｜抑制 {len(self.suppressed)}｜销毁 {len(self.dropped)}"
            f"｜待重试 {len(self.retrying)}"
        )


def deliver_once(
    conn: sqlite3.Connection,
    *,
    channels: ChannelSet,
    config: NotifyConfig | None = None,
    now: int | None = None,
) -> Pass:
    """扫一遍待投递队列并按闸门处理（一轮只给每条一次尝试）。"""
    cfg = config or load_notify_config(conn)
    stamp = notifications.now_ms() if now is None else now
    outcomes: list[Outcome] = []
    for item in notifications.pending(conn):
        if stamp - item.created_at > cfg.gate_ms:
            notifications.mark_dropped_expired(conn, item.id)
            audit.record(
                conn,
                actor=ACTOR,
                action=ACTION_DROPPED,
                target=str(item.id),
                detail={
                    "kind": item.kind,
                    "severity": item.severity,
                    "age_ms": stamp - item.created_at,
                    "gate_ms": cfg.gate_ms,
                },
                ts=stamp,
            )
            outcomes.append(
                Outcome(
                    id=item.id,
                    kind=item.kind,
                    severity=item.severity,
                    state=DROPPED_EXPIRED,
                    detail=f"入库 {(stamp - item.created_at) / 1000:.0f} 秒仍未送出（闸门 {cfg.gate_ms / 1000:.0f} 秒）",
                )
            )
            continue

        key = alert_key(item.kind, item.payload)
        if not is_recovery(item):
            # 发生时刻取入库时刻：冷却期的先后与投递节奏（进程重启、扫描延迟）无关
            verdict = admit(conn, item.kind, key=key, at=item.created_at, cooldown_ms=cfg.cooldown_ms)
            if not verdict.send:
                notifications.mark_suppressed(conn, item.id)
                outcomes.append(
                    Outcome(
                        id=item.id,
                        kind=item.kind,
                        severity=item.severity,
                        state=SUPPRESSED,
                        detail=f"同 {verdict.key} 在冷却期内（{cfg.cooldown_ms / 1000:.0f} 秒）",
                    )
                )
                continue

        outcomes.append(_attempt(conn, item, channels=channels, key=key, config=cfg, now=stamp))
    return Pass(outcomes=outcomes)


def run_loop(
    conn: sqlite3.Connection,
    *,
    channels: ChannelSet,
    config: NotifyConfig | None = None,
    seconds: float | None = None,
    interval: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_pass: Callable[[Pass], None] | None = None,
) -> int:
    """常驻投递：每 `interval` 秒扫一遍；`seconds` 给了就跑满那么久（0 = 只跑一轮）。

    返回扫描轮数。闸门与抑制都在 `deliver_once` 里，本函数只管节奏 ——
    `clock` / `sleep` 可注入，测试因此不用真的等 30 秒。
    """
    cfg = config or load_notify_config(conn)
    period = cfg.scan_interval_s if interval is None else interval
    deadline = None if seconds is None else clock() + seconds
    passes = 0
    while True:
        result = deliver_once(conn, channels=channels, config=cfg)
        passes += 1
        if on_pass is not None:
            on_pass(result)
        if deadline is None:
            sleep(period)
            continue
        remaining = deadline - clock()
        if remaining <= 0:
            return passes
        sleep(min(period, remaining))


def _attempt(
    conn: sqlite3.Connection,
    item: Notification,
    *,
    channels: ChannelSet,
    key: str,
    config: NotifyConfig,
    now: int,
) -> Outcome:
    """投一条：成功记通道，失败记尝试次数（用尽即销毁）。"""
    try:
        channel = channels.send(format_message(item), severity=item.severity)
    except ChannelError as exc:
        attempts = notifications.record_attempt(conn, item.id)
        audit.record(
            conn,
            actor=ACTOR,
            action=ACTION_DELIVERY_FAILED,
            target=str(item.id),
            detail={
                "kind": item.kind,
                "severity": item.severity,
                "channel_key": key,
                "attempts": attempts,
                "max_attempts": config.max_attempts,
                "error": str(exc),
            },
            ts=now,
        )
        if attempts >= config.max_attempts:
            notifications.mark_failed(conn, item.id)
            return Outcome(
                id=item.id,
                kind=item.kind,
                severity=item.severity,
                state=FAILED,
                detail=f"{attempts} 次尝试仍未送出：{exc}",
            )
        return Outcome(
            id=item.id,
            kind=item.kind,
            severity=item.severity,
            state=PENDING,
            detail=f"第 {attempts} 次尝试失败，下次扫描再试：{exc}",
        )
    notifications.mark_delivered(conn, item.id, channel=channel, at=now)
    if not is_recovery(item):
        note_sent(conn, key, at=now)
    return Outcome(
        id=item.id, kind=item.kind, severity=item.severity, state=DELIVERED, channel=channel
    )
