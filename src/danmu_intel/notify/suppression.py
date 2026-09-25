"""告警抑制与恢复（`alerts` 表，ADR-0010 / 设计 §15）。

一件事：**同一个 `alert_key` 在冷却期内只发一次；恢复时发一次恢复通知。**

`alert_key` 是"同一件事"，不是"同一条消息"。它由 `kind` + **身份字段**拼出来，
身份字段只取那几个能回答"是哪一场比赛 / 哪个房间 / 哪家供应商 / 哪张订单"的键：

| payload 字段 | 用在哪种事件上 |
|---|---|
| `match_id` | 解读层降级、成本闸、比赛相关的发布失败 |
| `platform` + `room_id` | 采集异常（进程退出、断流、磁盘、丢包） |
| `provider` | 链上异常（限速 / 拉取失败 / 额度越线） |
| `order_ref` | 付费相关（待补款、开通失败） |
| `member_id` | 会员相关 |

于是"采集进程退出"每崩一次都是一件事（房间 + 比赛不同就是不同的事），而"发布失败"
就一件事（发布器反复重试不该刷屏）—— 这正是冷却期要压的噪声。产出方也可以直接给
`payload["alert_key"]` 显式覆盖。

**恢复**：谁看得见"好了"谁调 `resolve`（链上供应商又通了是现成的例子）。恢复只在
台账处于 `firing` 时发一次，kind 加 `.resolved` 后缀、级别 `info`；恢复后的下一次
告警不再被冷却期压住（新的一轮故障要能立刻报出来）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Mapping

from danmu_intel.common.notifications import Notification, emit, now_ms

FIRING = "firing"
RESOLVED = "resolved"
#: 恢复通知的 kind 后缀（`<原 kind>.resolved`）。
RECOVERY_SUFFIX = ".resolved"

#: 参与 `alert_key` 的身份字段（顺序无关，拼 key 时排序）。
IDENTITY_FIELDS = ("match_id", "platform", "room_id", "provider", "order_ref", "member_id")


@dataclass(frozen=True, slots=True)
class Alert:
    """`alerts` 表的一行：一件事的发生台账。"""

    id: int
    alert_key: str
    kind: str
    first_seen: int
    last_seen: int
    count: int
    state: str
    last_sent_at: int | None
    resolved_at: int | None


@dataclass(frozen=True, slots=True)
class Admission:
    """`admit` 的判定结果：发不发、以及为什么。"""

    key: str
    send: bool
    reason: str  # first | recurred | cooldown


def alert_key(kind: str, payload: Mapping[str, object]) -> str:
    """`kind` + 身份字段 → 稳定字符串（同一件事的多次发生共用一个 key）。"""
    explicit = payload.get("alert_key")
    if explicit:
        return f"{kind}:{explicit}"
    identity = {
        field: payload[field] for field in IDENTITY_FIELDS if payload.get(field) is not None
    }
    if not identity:
        return kind
    return f"{kind}:{json.dumps(identity, ensure_ascii=False, sort_keys=True)}"


def is_recovery(notification: Notification) -> bool:
    """这条是不是恢复通知（恢复通知不参与抑制，也不进发生台账）。"""
    return notification.kind.endswith(RECOVERY_SUFFIX)


def get_alert(conn: sqlite3.Connection, key: str) -> Alert | None:
    row = conn.execute("SELECT * FROM alerts WHERE alert_key=?", (key,)).fetchone()
    return None if row is None else _to_alert(row)


def list_alerts(conn: sqlite3.Connection, *, state: str | None = None, limit: int = 50) -> list[Alert]:
    """告警台账（新→旧：先看最近发生过的）。"""
    sql = "SELECT * FROM alerts"
    params: tuple[object, ...] = ()
    if state is not None:
        sql += " WHERE state=?"
        params = (state,)
    sql += " ORDER BY last_seen DESC, id DESC LIMIT ?"
    return [_to_alert(row) for row in conn.execute(sql, (*params, limit)).fetchall()]


def admit(
    conn: sqlite3.Connection,
    kind: str,
    *,
    key: str,
    at: int,
    cooldown_ms: int,
) -> Admission:
    """记一次发生并判定要不要发（冷却期内的后来者不发）。

    `at` 用通知**入库时刻**而不是"notifier 跑到它的时刻"：冷却期与发生的先后因此
    与投递节奏无关（进程重启、扫描延迟都不会把顺序算歪）。
    """
    previous = get_alert(conn, key)
    send = not _within_cooldown(previous, at, cooldown_ms)
    if previous is None:
        reason = "first"
    elif previous.state != FIRING:
        reason = "recurred"  # 恢复后再出问题：立刻报
    else:
        reason = "cooldown" if not send else "recurred"
    _note_occurrence(conn, kind, key=key, at=at, previous=previous)
    return Admission(key=key, send=send, reason=reason)


def note_sent(conn: sqlite3.Connection, key: str, *, at: int) -> Alert:
    """一次真正送达：冷却期的钟从这里起算。"""
    conn.execute("UPDATE alerts SET state=?, last_sent_at=? WHERE alert_key=?", (FIRING, at, key))
    conn.commit()
    return _require(conn, key)


def resolve(
    conn: sqlite3.Connection,
    kind: str,
    *,
    identity: Mapping[str, object],
    detail: Mapping[str, object] | None = None,
    timestamp: int | None = None,
) -> Alert | None:
    """某件事好了：台账转 `resolved` 并写一条恢复通知。不在 `firing` 时什么都不做。

    返回更新后的台账行；本就没有这条告警（或已经恢复过）时返回 `None` —— 恢复通知
    只发一次，不重复骚扰。
    """
    key = alert_key(kind, identity)
    alert = get_alert(conn, key)
    if alert is None or alert.state != FIRING:
        return None
    stamp = now_ms() if timestamp is None else timestamp
    conn.execute(
        "UPDATE alerts SET state=?, resolved_at=? WHERE alert_key=?", (RESOLVED, stamp, key)
    )
    conn.commit()
    payload: dict[str, object] = {field: identity[field] for field in IDENTITY_FIELDS if identity.get(field) is not None}
    payload["resolved"] = True
    payload["alert"] = kind
    payload.update(detail or {})
    emit(conn, f"{kind}{RECOVERY_SUFFIX}", severity="info", payload=payload, timestamp=stamp)
    return _require(conn, key)


# —— 内部 ——


def _within_cooldown(alert: Alert | None, at: int, cooldown_ms: int) -> bool:
    return (
        alert is not None
        and alert.state == FIRING
        and alert.last_sent_at is not None
        and at - alert.last_sent_at < cooldown_ms
    )


def _note_occurrence(
    conn: sqlite3.Connection, kind: str, *, key: str, at: int, previous: Alert | None
) -> None:
    if previous is None:
        conn.execute(
            "INSERT INTO alerts(alert_key, kind, first_seen, last_seen, count, state) "
            "VALUES(?, ?, ?, ?, 1, ?)",
            (key, kind, at, at, FIRING),
        )
    elif previous.state != FIRING:
        # 新一轮故障：从这一刻重新计时（上一轮的冷却是上一轮的事）
        conn.execute(
            "UPDATE alerts SET kind=?, first_seen=?, last_seen=?, count=1, state=?, "
            "last_sent_at=NULL, resolved_at=NULL WHERE alert_key=?",
            (kind, at, at, FIRING, key),
        )
    else:
        conn.execute(
            "UPDATE alerts SET last_seen=?, count=count+1 WHERE alert_key=?", (at, key)
        )
    conn.commit()


def _to_alert(row: sqlite3.Row) -> Alert:
    return Alert(
        id=int(row["id"]),
        alert_key=row["alert_key"],
        kind=row["kind"],
        first_seen=int(row["first_seen"]),
        last_seen=int(row["last_seen"]),
        count=int(row["count"]),
        state=row["state"],
        last_sent_at=int(row["last_sent_at"]) if row["last_sent_at"] is not None else None,
        resolved_at=int(row["resolved_at"]) if row["resolved_at"] is not None else None,
    )


def _require(conn: sqlite3.Connection, key: str) -> Alert:
    alert = get_alert(conn, key)
    if alert is None:  # pragma: no cover - 只有并发删除才可能走到
        raise LookupError(f"告警台账不存在：{key}")
    return alert
