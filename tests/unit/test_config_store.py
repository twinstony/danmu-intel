"""配置存储：版本号 + 60 秒 TTL 缓存（NFR-T-4 / 设计 §14.3）。

要守住的三件事：

1. **保存即递增版本号**，并记下「谁改的、改了哪几把键、什么时候」；
2. **本进程立刻生效**（保存方不等 TTL），**别的进程最多滞后 60 秒**（TTL）；
3. `:memory:` 的库不缓存（没有稳定身份键，宁可直读也不串库）。
"""

from __future__ import annotations

import sqlite3

import pytest

from danmu_intel.billing import pricing
from danmu_intel.common import config_store, paths
from danmu_intel.common.config import save_stats_config
from danmu_intel.common.db import open_db


@pytest.fixture(autouse=True)
def clean_cache():
    config_store.invalidate()
    yield
    config_store.invalidate()


def test_missing_config_reads_empty(conn):
    assert config_store.load(conn, "stats") == {}
    assert config_store.version(conn) == 0
    assert config_store.latest(conn) is None


def test_save_writes_value_and_bumps_version(conn):
    first = config_store.save(conn, "stats", {"gray_min_hits": 7}, actor="管理员", ts=1_790_064_000_000)
    second = config_store.save(conn, "stats", {"gray_min_hits": 9}, actor="管理员", ts=1_790_064_000_001)

    assert (first.version, second.version) == (1, 2)
    assert config_store.load(conn, "stats") == {"gray_min_hits": 9}
    assert config_store.version(conn) == 2
    row = conn.execute("SELECT * FROM config WHERE key=?", ("stats",)).fetchone()
    assert row["updated_by"] == "管理员"
    assert row["updated_at"] == 1_790_064_000_001
    latest = config_store.latest(conn)
    assert latest is not None and latest.keys == ("stats",) and latest.updated_by == "管理员"


def test_save_requires_actor(conn):
    with pytest.raises(ValueError):
        config_store.save(conn, "stats", {}, actor="")


def test_save_invalidates_own_cache_immediately(conn):
    """保存方不看 TTL：改完的下一屏就必须是新值（设计 §14.3）。"""
    save_stats_config(conn, actor="管理员", changes={"gray_min_hits": 3})
    assert config_store.load(conn, "stats")["gray_min_hits"] == 3
    save_stats_config(conn, actor="管理员", changes={"gray_min_hits": 11})
    assert config_store.load(conn, "stats")["gray_min_hits"] == 11


def test_other_process_sees_change_within_ttl(conn, tmp_path, monkeypatch):
    """另一个连接（= 另一个进程的等价物）在 TTL 内用缓存，TTL 一过就读到新值。"""
    clock = {"now": 1_000.0}
    monkeypatch.setattr(config_store, "CLOCK", lambda: clock["now"])
    other = open_db(paths.db_path())
    try:
        assert config_store.load(other, "stats") == {}
        # 绕过 config_store 直接改库（模拟「别的进程写的」）：缓存还在，读到的仍是旧值
        conn.execute("INSERT INTO config(key, value_json, updated_at, updated_by) VALUES('stats', '{\"gray_min_hits\": 42}', 1, '别人')")
        conn.commit()
        assert config_store.load(other, "stats") == {}
        clock["now"] += config_store.CACHE_TTL_MS / 1000
        assert config_store.load(other, "stats") == {"gray_min_hits": 42}
    finally:
        other.close()


def test_ttl_bound_is_the_documented_minute():
    assert config_store.CACHE_TTL_MS == 60_000


def test_invalidate_drops_only_that_db(conn, tmp_path):
    config_store.save(conn, "stats", {"gray_min_hits": 5}, actor="管理员")
    config_store.invalidate(conn)
    # 缓存清掉之后仍然读得到（回源到库里那一行）
    assert config_store.load(conn, "stats") == {"gray_min_hits": 5}


def test_memory_db_is_not_cached(tmp_path):
    """`:memory:` 库没有稳定身份键 → 直读，不拿 A 库的配置当 B 库的。"""
    left = sqlite3.connect(":memory:")
    left.row_factory = sqlite3.Row
    right = sqlite3.connect(":memory:")
    right.row_factory = sqlite3.Row
    left.executescript("CREATE TABLE config(key TEXT PRIMARY KEY, value_json TEXT, updated_at INTEGER, updated_by TEXT);")
    right.executescript("CREATE TABLE config(key TEXT PRIMARY KEY, value_json TEXT, updated_at INTEGER, updated_by TEXT);")
    try:
        left.execute("INSERT INTO config VALUES('stats', '{\"gray_min_hits\": 1}', 1, '甲')")
        left.commit()
        assert config_store.load(left, "stats") == {"gray_min_hits": 1}
        assert config_store.load(right, "stats") == {}
    finally:
        left.close()
        right.close()


def test_billing_save_bumps_version_too(conn):
    pricing.save_billing_config(conn, actor="管理员", changes={"grace_ms": 3_600_000})
    assert config_store.version(conn) == 1
    assert pricing.load_billing_config(conn).grace_ms == 3_600_000
