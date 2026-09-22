"""配置与审计测试（设计 §5.1 / §9.1 第 4 条）。

配置只影响「门槛」，不得影响「事实」：同一份原始记录 + 切片，换门槛只会改变
灰信号/终局判定的判定结果，不会改变条数、密度、比分这类客观量。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.common import audit
from danmu_intel.common.config import (
    CONFIG_KEY,
    DEFAULT_GRAY_KEYWORDS,
    GRAY_CATEGORY_LABELS,
    StatsConfig,
    load_stats_config,
    save_stats_config,
)
from danmu_intel.common.db import table_names


def test_t4_tables_exist(conn):
    assert set(table_names(conn)) >= {"config", "audit_log", "gray_signals"}


def test_default_config_matches_requirement_thresholds(conn):
    config = load_stats_config(conn)
    assert config == StatsConfig()
    # 需求 §6.5 第 4 条「多人、多时段」+ 设计 §9.1：命中 ≥N、独立用户 ≥M、覆盖 ≥K 时段
    assert (config.gray_min_hits, config.gray_min_users, config.gray_min_windows) == (5, 3, 2)
    # 需求 §6.4：≥3 类独立信号 + 2 分钟反转窗口；终局类弹幕 ≥2 分钟；流量降至峰值一成以下 ≥5 分钟
    assert config.min_signal_kinds == 3
    assert config.reversal_window_ms == 120_000
    assert config.end_burst_min_ms == 120_000
    assert (config.silence_ratio, config.silence_min_ms) == (0.1, 300_000)
    # 设计 §8.1：弹幕信号候选需 ≥2 类独立信号
    assert config.verify_min_kinds == 2
    assert config.gray_keywords == DEFAULT_GRAY_KEYWORDS
    assert set(GRAY_CATEGORY_LABELS) == {"cheat_suspicion", "betting"}


def test_save_config_persists_and_audits(conn):
    updated = save_stats_config(conn, actor="管理员", changes={"gray_min_users": 7}, ts=1_790_064_000_000)
    assert updated.gray_min_users == 7
    assert load_stats_config(conn).gray_min_users == 7

    row = conn.execute("SELECT * FROM config WHERE key=?", (CONFIG_KEY,)).fetchone()
    assert json.loads(row["value_json"])["gray_min_users"] == 7
    assert row["updated_by"] == "管理员"

    logs = audit.entries(conn, action=audit.CONFIG_UPDATE)
    assert len(logs) == 1
    assert logs[0].actor == "管理员" and logs[0].target == CONFIG_KEY
    assert logs[0].detail["before"]["gray_min_users"] == 3
    assert logs[0].detail["after"]["gray_min_users"] == 7

    # 覆盖写：同一 key 更新，不出现第二行
    save_stats_config(conn, actor="管理员", changes={"gray_min_users": 9})
    assert conn.execute("SELECT COUNT(*) AS n FROM config").fetchone()["n"] == 1
    assert len(audit.entries(conn, action=audit.CONFIG_UPDATE)) == 2


def test_config_roundtrip_json(conn):
    config = StatsConfig(gray_keywords=(("假赛", "cheat_suspicion"),), gray_min_hits=2)
    restored = StatsConfig.from_dict(json.loads(config.to_json()))
    assert restored == config
    assert restored.as_dict()["gray_keywords"] == (("假赛", "cheat_suspicion"),)


def test_unknown_config_key_is_rejected(conn):
    with pytest.raises(ValueError, match="未知的统计配置项"):
        save_stats_config(conn, actor="管理员", changes={"gray_min_hit": 3})
    with pytest.raises(ValueError, match="未知的统计配置项"):
        StatsConfig.from_dict({"nope": 1})
    assert load_stats_config(conn) == StatsConfig()


def test_audit_requires_actor(conn):
    with pytest.raises(ValueError, match="actor"):
        audit.record(conn, actor="", action=audit.SLICE_OVERRIDE)
    assert audit.entries(conn) == []
    assert audit.count(conn, action=audit.SLICE_OVERRIDE) == 0


def test_audit_filters_and_count(conn):
    audit.record(conn, actor="甲", action=audit.SLICE_OVERRIDE, target="match:1/game:1", ts=1)
    audit.record(conn, actor="乙", action=audit.SLICE_OVERRIDE, target="match:2/game:1", ts=2)
    audit.record(conn, actor="乙", action=audit.CONFIG_UPDATE, target=CONFIG_KEY, ts=3)

    assert audit.count(conn, action=audit.SLICE_OVERRIDE) == 2
    assert audit.count(conn, action=audit.SLICE_OVERRIDE, target_prefix="match:1/") == 1
    assert [entry.actor for entry in audit.entries(conn, action=audit.SLICE_OVERRIDE)] == ["甲", "乙"]
    assert audit.entries(conn, action="不存在") == []
    assert audit.entries(conn, target_prefix="match:2/")[0].detail == {}
