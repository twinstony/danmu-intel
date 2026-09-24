"""链上监听的账本层测试：额度记账与阈值、限速、游标、入账形状、报警出口。

全部断网可跑（NFR-GA-4）：这一层只碰数据库与纯函数。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from danmu_intel.chain import alerts, cursor
from danmu_intel.chain.quota import (
    ALERT_RATIO,
    HELIUS,
    LIMITS,
    POLYGONSCAN,
    Limit,
    QuotaLedger,
    RateLimiter,
    day_key,
    limit_for,
    month_key,
)
from danmu_intel.chain.transfer import NATIVE_ASSET, Transfer, sort_key
from danmu_intel.common.notifications import recent

#: 2026-09-24 12:00:00 本地时间（固定基准，不依赖当前时间）。
#: 凡按它取窗口的账本（`usage(at_ms=…)` / `day_key`），写入也必须用同一时刻：
#: `record()` 的默认时钟是 `now_ms()`，两边不同源时窗口会错位（2026-09-25 实测）。
BASE_MS = int(datetime(2026, 9, 24, 12, 0, 0).timestamp() * 1000)


def test_limit_table_matches_adr_0005():
    assert LIMITS[POLYGONSCAN].unit == "calls"
    assert LIMITS[POLYGONSCAN].window == "day"
    assert LIMITS[POLYGONSCAN].cap == 100_000
    assert LIMITS[POLYGONSCAN].rate_per_s == 5.0
    assert LIMITS[HELIUS].unit == "credits"
    assert LIMITS[HELIUS].window == "month"
    assert LIMITS[HELIUS].cap == 1_000_000
    assert LIMITS[HELIUS].rate_per_s == 10.0
    assert LIMITS[POLYGONSCAN].window_label == "日"
    assert LIMITS[HELIUS].window_label == "月"


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="未登记的供应商"):
        limit_for("alchemy")


def test_day_and_month_keys_are_local_calendar():
    assert day_key(BASE_MS) == "2026-09-24"
    assert month_key(BASE_MS) == "2026-09"


def test_ledger_accumulates_within_the_day(conn):
    ledger = QuotaLedger(conn, POLYGONSCAN)
    ledger.record(calls=1, at_ms=BASE_MS)
    ledger.record(calls=1, at_ms=BASE_MS + 60_000)
    ledger.record(calls=2, at_ms=BASE_MS + 120_000)

    usage = ledger.usage(at_ms=BASE_MS)
    assert usage.day_used == 4
    assert usage.used == 4
    assert usage.month_used == 4
    assert usage.window_key == "2026-09-24"
    assert usage.cap == 100_000
    assert usage.over_threshold is False


def test_ledger_days_are_separate_rows(conn):
    ledger = QuotaLedger(conn, POLYGONSCAN)
    ledger.record(calls=3, at_ms=BASE_MS)
    ledger.record(calls=5, at_ms=BASE_MS + 24 * 3600 * 1000)

    assert ledger.usage(at_ms=BASE_MS).day_used == 3
    assert ledger.usage(at_ms=BASE_MS + 24 * 3600 * 1000).day_used == 5
    # 当月是跨日的合计，上限窗口是「日」时 `used` 只算当日
    assert ledger.usage(at_ms=BASE_MS + 24 * 3600 * 1000).month_used == 8


def test_helius_monthly_window_sums_the_whole_month(conn):
    # 账本时钟钉在基准上：`record()` 默认走 `now_ms()`，而断言按 `BASE_MS` 取窗口 ——
    # 两边不同源时窗口会错位（日窗口错位必红，月窗口要跨月才红）。
    ledger = QuotaLedger(conn, HELIUS, clock=lambda: BASE_MS)
    ledger.record(credits=1)
    ledger.record(credits=2)

    usage = ledger.usage(at_ms=BASE_MS)
    assert usage.used == 3
    assert usage.month_used == 3
    assert usage.window_key == "2026-09"
    assert usage.cap == 1_000_000


def test_threshold_is_strictly_above_eighty_percent(conn):
    ledger = QuotaLedger(conn, POLYGONSCAN, limit=Limit(unit="calls", window="day", cap=10, rate_per_s=5))
    ledger.record(calls=8, at_ms=BASE_MS)
    assert ledger.usage(at_ms=BASE_MS).ratio == pytest.approx(ALERT_RATIO)
    assert ledger.usage(at_ms=BASE_MS).over_threshold is False

    ledger.record(calls=1, at_ms=BASE_MS)
    assert ledger.usage(at_ms=BASE_MS).used == 9
    assert ledger.usage(at_ms=BASE_MS).over_threshold is True
    assert "90.0%" in ledger.usage(at_ms=BASE_MS).summary()


def test_last_error_reflects_the_most_recent_call(conn):
    ledger = QuotaLedger(conn, POLYGONSCAN)
    ledger.record(error="HTTP 502", at_ms=BASE_MS)
    assert ledger.usage(at_ms=BASE_MS).last_error == "HTTP 502"

    ledger.record(calls=1, at_ms=BASE_MS)
    assert ledger.usage(at_ms=BASE_MS).last_error is None


def test_usage_on_an_empty_ledger_is_zero(conn):
    usage = QuotaLedger(conn, HELIUS).usage(at_ms=BASE_MS)
    assert (usage.used, usage.day_used, usage.month_used) == (0, 0, 0)
    assert usage.ratio == 0.0


def test_zero_cap_never_divides_by_zero(conn):
    usage = QuotaLedger(
        conn, POLYGONSCAN, limit=Limit(unit="calls", window="day", cap=0, rate_per_s=1)
    ).usage(at_ms=BASE_MS)
    assert usage.ratio == 0.0


class FakeClock:
    """可注入的时钟：`sleep` 会把时间往前推（限速测试不需要真的等）。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_does_not_wait_when_spaced_out():
    clock = FakeClock()
    limiter = RateLimiter(5.0, clock=clock.monotonic, sleep=clock.sleep)

    assert limiter.acquire() == 0.0
    clock.now += 60.0  # 下一次轮询在 60 秒后
    assert limiter.acquire() == 0.0
    assert clock.slept == []


def test_rate_limiter_spaces_back_to_back_calls():
    clock = FakeClock()
    limiter = RateLimiter(10.0, clock=clock.monotonic, sleep=clock.sleep)

    waits = [limiter.acquire() for _ in range(3)]
    assert waits == [0.0, pytest.approx(0.1), pytest.approx(0.1)]
    assert clock.slept == [pytest.approx(0.1), pytest.approx(0.1)]


def test_rate_limiter_rejects_non_positive_rate():
    with pytest.raises(ValueError, match="限速必须为正"):
        RateLimiter(0)


def test_transfer_rejects_unknown_network_and_negative_amount():
    with pytest.raises(ValueError, match="未知的收款网络"):
        Transfer(network="base", address="a", tx_ref="t", asset=NATIVE_ASSET, units=1, at_ms=0)
    with pytest.raises(ValueError, match="入账金额必须为正"):
        Transfer(network="polygon", address="a", tx_ref="t", asset=NATIVE_ASSET, units=0, at_ms=0)


def test_transfers_sort_by_chain_time_then_tx_ref():
    early = Transfer(network="polygon", address="a", tx_ref="b", asset=NATIVE_ASSET, units=1, at_ms=1)
    later = Transfer(network="solana", address="a", tx_ref="a", asset=NATIVE_ASSET, units=1, at_ms=2)
    assert sorted([later, early], key=sort_key) == [early, later]


def test_cursor_roundtrip_and_monotonic_guard(conn):
    assert cursor.get(conn, "polygon", "0xabc") is None

    assert cursor.advance(conn, "polygon", "0xabc", "100", at_ms=BASE_MS) == "100"
    assert cursor.get(conn, "polygon", "0xabc") == "100"

    # 补扫拿到更旧的区块也不许把游标往回拨（回拨 = 同一笔付款可能被算两次）
    assert cursor.advance(conn, "polygon", "0xabc", "90", at_ms=BASE_MS + 1000) == "100"
    assert cursor.advance(conn, "polygon", "0xabc", "120", at_ms=BASE_MS + 2000) == "120"

    rows = cursor.rows(conn)
    assert [(row.network, row.scope, row.cursor) == ("polygon", "0xabc", "120") for row in rows]
    assert rows[0].updated_at == BASE_MS + 2000


def test_solana_cursor_is_pushed_by_the_caller(conn):
    cursor.advance(conn, "solana", "wallet", "sig-1")
    assert cursor.advance(conn, "solana", "wallet", "sig-2") == "sig-2"
    assert cursor.rows(conn, network="solana")[0].scope == "wallet"
    assert cursor.rows(conn, network="polygon") == []


def test_cursor_rejects_unknown_network(conn):
    with pytest.raises(ValueError, match="未知的收款网络"):
        cursor.advance(conn, "base", "0xabc", "1")


def test_alerts_are_critical_pending_notifications(conn):
    alerts.alert(conn, alerts.RATE_LIMITED, provider=POLYGONSCAN, detail={"scope": "0xabc"})

    [item] = recent(conn, limit=5)
    assert item.kind == "chain_rate_limited"
    assert item.severity == "critical"
    assert item.state == "pending"
    assert item.payload == {"provider": POLYGONSCAN, "scope": "0xabc"}


def test_alert_kinds_are_closed_set():
    with pytest.raises(ValueError, match="未知的链上报警类型"):
        alerts.alert(conn=None, kind="chain_meltdown", provider=POLYGONSCAN)  # type: ignore[arg-type]


def test_alert_gate_reports_each_window_once(conn):
    gate = alerts.AlertGate(conn)

    assert gate.emit(alerts.QUOTA_HIGH, provider=POLYGONSCAN, window="2026-09-24", detail={"ratio": 0.81})
    assert not gate.emit(alerts.QUOTA_HIGH, provider=POLYGONSCAN, window="2026-09-24")
    assert gate.emit(alerts.QUOTA_HIGH, provider=POLYGONSCAN, window="2026-09-25")
    assert gate.emit(alerts.QUOTA_HIGH, provider=HELIUS, window="2026-09")

    assert len(recent(conn, limit=10)) == 3

    gate.forget(alerts.QUOTA_HIGH, provider=POLYGONSCAN)
    assert gate.emit(alerts.QUOTA_HIGH, provider=POLYGONSCAN, window="2026-09-24")
    assert len(recent(conn, limit=10)) == 4
