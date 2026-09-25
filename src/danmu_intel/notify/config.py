"""通知投递的可调参数（ADR-0010：冷却期是默认值，写进 `config` 表可调）。

默认值一律取自 ADR-0010 / 设计 §15 的原文，改动走 `save_notify_config` 并留审计：

| 参数 | 默认 | 出处 |
|---|---|---|
| `gate_ms` | 5 分钟 | 需求 NFR-T-5「报警 5 分钟内送达，超时即丢弃」 |
| `cooldown_ms` | 15 分钟 | ADR-0010「同 `alert_key` 在冷却期内只发一次」 |
| `scan_interval_s` | 30 秒 | ADR-0010「`notifier` 每 30s 扫描」 |
| `max_attempts` | 3 | ADR-0010「重试 2 次」⇒ 首次 + 2 次重试 |
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, fields
from typing import Any

from danmu_intel.common import audit

CONFIG_KEY = "notify"


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    """通知投递的全部可调门槛。"""

    gate_ms: int = 5 * 60 * 1000
    cooldown_ms: int = 15 * 60 * 1000
    scan_interval_s: float = 30.0
    max_attempts: int = 3

    def as_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NotifyConfig":
        """从 config 表读出的 JSON 覆盖默认值。未知键直接报错（不静默吞配置）。"""
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(f"未知的通知配置项：{','.join(unknown)}（可选：{','.join(sorted(known))}）")
        return cls(**payload)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


def load_notify_config(conn: sqlite3.Connection) -> NotifyConfig:
    """读配置：默认值 + `config` 表里 `notify` 键的覆盖。"""
    row = conn.execute("SELECT value_json FROM config WHERE key=?", (CONFIG_KEY,)).fetchone()
    if row is None:
        return NotifyConfig()
    return NotifyConfig.from_dict(json.loads(row["value_json"]))


def save_notify_config(
    conn: sqlite3.Connection, *, actor: str, changes: dict[str, Any], ts: int | None = None
) -> NotifyConfig:
    """改门槛并留审计（同统计门槛：参数改动要能回答「谁改的」）。"""
    current = load_notify_config(conn)
    updated = NotifyConfig.from_dict({**current.as_dict(), **changes})
    audit.record(
        conn,
        actor=actor,
        action=audit.CONFIG_UPDATE,
        target=CONFIG_KEY,
        detail={"before": current.as_dict(), "after": updated.as_dict()},
        ts=ts,
    )
    conn.execute(
        "INSERT INTO config(key, value_json, updated_at, updated_by) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (CONFIG_KEY, updated.to_json(), int(time.time() * 1000) if ts is None else ts, actor),
    )
    conn.commit()
    return updated
