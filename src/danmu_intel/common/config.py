"""统计门槛配置（设计 §9.1 灰信号第 4 条：门槛参数写在 `config` 表，改动留审计）。

**纯函数层不读库**：`stats/` 下的函数只接受一个 `StatsConfig` 入参（设计 §9 铁律）。
读库发生在流水线（`pipeline.py` / CLI）里，由 `load_stats_config` 完成。

默认值一律取自需求原文：

- 终局判定信号门槛 → 需求 §6.4（≥3 类独立信号、2 分钟反转窗口、终局类弹幕持续 ≥2 分钟、
  流量降至峰值一成以下持续 ≥5 分钟）。
- 灰信号门槛 N/M/K → 需求 §6.5 第 4 条「多人、多时段」+ 设计 §9.1（命中 ≥N 次、
  独立用户 ≥M 人、覆盖 ≥K 个时段）。关键词表可按 config 覆盖（设计 §20 O7）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, fields
from typing import Any

from danmu_intel.common import audit

CONFIG_KEY = "stats"

#: 灰信号类别（需求 FR-C3-3：产出灰信号的**类别**；类别名不得含指控意味）。
GRAY_CATEGORY_LABELS = {
    "cheat_suspicion": "比赛公正性讨论聚集",
    "betting": "盘口讨论聚集",
}

#: 灰信号关键词初值：(词，类别)。仅作**聚集现象**的检索起点，不构成任何指控。
DEFAULT_GRAY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("假赛", "cheat_suspicion"),
    ("打假", "cheat_suspicion"),
    ("放水", "cheat_suspicion"),
    ("演技", "cheat_suspicion"),
    ("剧本", "cheat_suspicion"),
    ("消极比赛", "cheat_suspicion"),
    ("收钱", "cheat_suspicion"),
    ("内幕", "cheat_suspicion"),
    ("盘口", "betting"),
    ("赔率", "betting"),
    ("下注", "betting"),
    ("押注", "betting"),
)


@dataclass(frozen=True, slots=True)
class StatsConfig:
    """规则统计的全部可调门槛（纯函数的「配置」入参）。"""

    # 灰信号门槛（需求 §6.5 第 4 条：多人、多时段）
    gray_keywords: tuple[tuple[str, str], ...] = DEFAULT_GRAY_KEYWORDS
    gray_min_hits: int = 5
    gray_min_users: int = 3
    gray_min_windows: int = 2
    gray_window_ms: int = 300_000
    gray_sample_size: int = 5
    # 终局判定门槛（需求 §6.4）
    end_burst_min_ms: int = 120_000
    end_burst_min_hits: int = 8
    silence_ratio: float = 0.1
    silence_min_ms: int = 300_000
    min_signal_kinds: int = 3
    reversal_window_ms: int = 120_000
    # 切片边界复核（设计 §8.1：弹幕信号候选需 ≥2 类独立信号）
    verify_min_kinds: int = 2
    boundary_cluster_ms: int = 120_000

    def as_dict(self) -> dict[str, Any]:
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StatsConfig":
        """从 config 表读出的 JSON 覆盖默认值。未知键直接报错（不静默吞配置）。"""
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(f"未知的统计配置项：{','.join(unknown)}（可选：{','.join(sorted(known))}）")
        data = dict(payload)
        if "gray_keywords" in data:
            data["gray_keywords"] = tuple(
                (str(keyword), str(category)) for keyword, category in data["gray_keywords"]
            )
        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


def load_stats_config(conn: sqlite3.Connection) -> StatsConfig:
    """读配置：默认值 + `config` 表里 `stats` 键的覆盖。"""
    row = conn.execute("SELECT value_json FROM config WHERE key=?", (CONFIG_KEY,)).fetchone()
    if row is None:
        return StatsConfig()
    return StatsConfig.from_dict(json.loads(row["value_json"]))


def save_stats_config(
    conn: sqlite3.Connection, *, actor: str, changes: dict[str, Any], ts: int | None = None
) -> StatsConfig:
    """改门槛并留审计（设计 §9.1 灰信号第 4 条）。返回生效后的配置。"""
    current = load_stats_config(conn)
    updated = StatsConfig.from_dict({**current.as_dict(), **changes})
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
        (
            CONFIG_KEY,
            updated.to_json(),
            int(time.time() * 1000) if ts is None else ts,
            actor,
        ),
    )
    conn.commit()
    return updated
