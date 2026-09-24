"""监听器测试：游标续扫、补扫不看游标、失败不静默、额度告警、轮询循环。

客户端全部用假的（鸭子类型），因此这一层既不联网也不依赖任何真实链上数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pytest

from danmu_intel.chain import cursor
from danmu_intel.chain.provider import ProviderError, RateLimited
from danmu_intel.chain.quota import (
    HELIUS,
    POLYGONSCAN,
    Limit,
    QuotaLedger,
)
from danmu_intel.chain.transfer import NATIVE_ASSET, Transfer
from danmu_intel.chain.watcher import POLL_SECONDS, Observation, WatchTarget, Watcher
from danmu_intel.common.notifications import recent

BASE_MS = int(datetime(2026, 9, 24, 12, 0, 0).timestamp() * 1000)
ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def polygon_transfer(block: int, *, units: int = 10, tx: str | None = None) -> Transfer:
    return Transfer(
        network="polygon",
        address=ADDRESS,
        tx_ref=tx or f"0x{block:04x}",
        asset=NATIVE_ASSET,
        units=units,
        at_ms=BASE_MS + block * 1000,
        block=block,
    )


def solana_transfer(signature: str, *, units: int = 7, memo: str | None = "mem-1") -> Transfer:
    return Transfer(
        network="solana",
        address=WALLET,
        tx_ref=signature,
        asset=NATIVE_ASSET,
        units=units,
        at_ms=BASE_MS,
        memo=memo,
        block=100,
    )


@dataclass
class FakePolygon:
    """假的 Polygonscan 客户端：按脚本返回入账，或抛异常。"""

    ledger: QuotaLedger
    script: list[object] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def transfers(self, address: str, *, from_block: int = 0) -> list[Transfer]:
        self.calls.append({"address": address, "from_block": from_block})
        if not self.script:
            return []
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return list(item)  # type: ignore[arg-type]


@dataclass
class FakeHelius:
    """假的 Helius 客户端：签名单 + 每笔交易解出的入账。"""

    ledger: QuotaLedger
    signatures_script: list[object] = field(default_factory=list)
    transactions: dict[str, object] = field(default_factory=dict)
    calls: list[dict[str, object]] = field(default_factory=list)

    def signatures(self, address: str, *, until: str | None = None) -> list[dict[str, str]]:
        self.calls.append({"address": address, "until": until})
        if not self.signatures_script:
            return []
        item = self.signatures_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return [{"signature": str(sig)} for sig in item]  # type: ignore[union-attr]

    def transaction(self, signature: str, *, address: str) -> list[Transfer]:
        self.calls.append({"transaction": signature, "address": address})
        item = self.transactions.get(signature, [])
        if isinstance(item, Exception):
            raise item
        return list(item)  # type: ignore[arg-type]


def target(network: str, address: str) -> WatchTarget:
    return WatchTarget(network=network, address=address)


def polygon_watcher(conn, **kwargs) -> tuple[Watcher, FakePolygon]:
    ledger = QuotaLedger(conn, POLYGONSCAN, clock=lambda: BASE_MS)
    fake = FakePolygon(ledger=ledger, **kwargs)
    watcher = Watcher(conn, [target("polygon", ADDRESS)], polygon=fake, clock=lambda: BASE_MS)
    return watcher, fake


def solana_watcher(conn, **kwargs) -> tuple[Watcher, FakeHelius]:
    ledger = QuotaLedger(conn, HELIUS, clock=lambda: BASE_MS)
    fake = FakeHelius(ledger=ledger, **kwargs)
    watcher = Watcher(conn, [target("solana", WALLET)], helius=fake, clock=lambda: BASE_MS)
    return watcher, fake


def test_watch_target_validates_network_and_address():
    with pytest.raises(ValueError, match="未知的收款网络"):
        WatchTarget(network="base", address=ADDRESS)
    with pytest.raises(ValueError, match="监听地址不能为空"):
        WatchTarget(network="polygon", address="")
    assert target("polygon", ADDRESS).scope == ADDRESS


def test_missing_client_for_a_target_network_is_a_loud_failure(conn):
    with pytest.raises(ValueError, match="缺少这些网络的客户端：solana"):
        Watcher(conn, [target("solana", WALLET)])


def test_poll_scans_from_the_cursor_and_advances_it(conn):
    watcher, fake = polygon_watcher(conn, script=[[polygon_transfer(100, tx="0xa")], [polygon_transfer(105, tx="0xb")]])

    first = watcher.poll()
    assert [item.tx_ref for item in first.transfers] == ["0xa"]
    assert fake.calls[0]["from_block"] == 0
    assert cursor.get(conn, "polygon", ADDRESS) == "100"

    # 断点续扫：第二次从 101 起（同区块的多笔在上一次响应里已经一起拿到）
    watcher.poll()
    assert fake.calls[1]["from_block"] == 101
    assert cursor.get(conn, "polygon", ADDRESS) == "105"


def test_poll_without_new_income_keeps_the_cursor(conn):
    watcher, fake = polygon_watcher(conn, script=[[polygon_transfer(100)], []])

    watcher.poll()
    watcher.poll()
    assert fake.calls[1]["from_block"] == 101
    assert cursor.get(conn, "polygon", ADDRESS) == "100"


def test_cursor_never_moves_backwards(conn):
    cursor.advance(conn, "polygon", ADDRESS, "500", at_ms=BASE_MS)
    watcher, fake = polygon_watcher(conn, script=[[polygon_transfer(400)]])

    watcher.poll()
    assert cursor.get(conn, "polygon", ADDRESS) == "500"


def test_rescan_ignores_the_cursor(conn):
    cursor.advance(conn, "polygon", ADDRESS, "500", at_ms=BASE_MS)
    watcher, fake = polygon_watcher(conn, script=[[polygon_transfer(490, tx="0xmissed")]])

    found = watcher.rescan()
    assert [item.tx_ref for item in found.transfers] == ["0xmissed"]
    assert fake.calls[0]["from_block"] == 0  # 按地址查全历史，不依赖游标
    assert cursor.get(conn, "polygon", ADDRESS) == "500"  # 单调保护照样生效


def test_polygon_cursor_takes_the_highest_block(conn):
    watcher, _ = polygon_watcher(
        conn, script=[[polygon_transfer(700, tx="0xa"), polygon_transfer(702, tx="0xb"), polygon_transfer(701, tx="0xc")]]
    )
    watcher.poll()
    assert cursor.get(conn, "polygon", ADDRESS) == "702"


def test_solana_poll_passes_the_signature_cursor(conn):
    watcher, fake = solana_watcher(
        conn,
        signatures_script=[["sig-1"], ["sig-2"]],
        transactions={"sig-1": [solana_transfer("sig-1")], "sig-2": [solana_transfer("sig-2", units=9)]},
    )

    first = watcher.poll()
    signature_calls = [call for call in fake.calls if "until" in call]
    assert signature_calls[0]["until"] is None
    assert [item.tx_ref for item in first.transfers] == ["sig-1"]
    assert cursor.get(conn, "solana", WALLET) == "sig-1"

    watcher.poll()
    assert [call for call in fake.calls if "until" in call][1]["until"] == "sig-1"
    assert cursor.get(conn, "solana", WALLET) == "sig-2"


def test_solana_cursor_advances_past_signatures_without_income(conn):
    watcher, _ = solana_watcher(
        conn, signatures_script=[["sig-1", "sig-2"]], transactions={"sig-1": [], "sig-2": []}
    )
    watcher.poll()
    # 没有入账也要推游标：否则这两个签名每轮都会被重新取回，白烧 credit
    assert cursor.get(conn, "solana", WALLET) == "sig-2"


def test_solana_rescan_ignores_the_cursor(conn):
    cursor.advance(conn, "solana", WALLET, "sig-9", at_ms=BASE_MS)
    watcher, fake = solana_watcher(
        conn, signatures_script=[["sig-9"]], transactions={"sig-9": [solana_transfer("sig-9")]}
    )
    assert len(watcher.rescan().transfers) == 1
    assert fake.calls[0]["until"] is None


def test_rate_limit_is_reported_and_other_targets_still_scan(conn):
    ledger = QuotaLedger(conn, POLYGONSCAN, clock=lambda: BASE_MS)
    fake = FakePolygon(ledger=ledger, script=[RateLimited("Polygonscan 限速：HTTP 429"), [polygon_transfer(10)]])
    other = "0x00000000000000000000000000000000000000ff"
    watcher = Watcher(
        conn,
        [target("polygon", ADDRESS), target("polygon", other)],
        polygon=fake,
        clock=lambda: BASE_MS,
    )

    found = watcher.poll()
    assert [item.units for item in found.transfers] == [10]  # 第二个地址照扫
    assert len(found.failures) == 1
    assert "限速" in found.failures[0]

    [item] = recent(conn, limit=5)
    assert item.kind == "chain_rate_limited"
    assert item.severity == "critical"
    assert item.payload["provider"] == POLYGONSCAN
    assert item.payload["scope"] == ADDRESS
    assert item.payload["network"] == "polygon"


def test_fetch_failure_is_reported_as_such(conn):
    watcher, _ = polygon_watcher(conn, script=[ProviderError("Polygonscan 不可达（URLError）")])
    found = watcher.poll()

    assert found.transfers == []
    [item] = recent(conn, limit=5)
    assert item.kind == "chain_fetch_failed"
    assert "不可达" in str(item.payload["reason"])


def test_repeated_failures_alert_once_until_recovery(conn):
    watcher, fake = polygon_watcher(
        conn,
        script=[
            RateLimited("限速"),
            RateLimited("限速"),
            [],
            RateLimited("限速"),
        ],
    )
    watcher.poll()
    watcher.poll()
    assert len(recent(conn, limit=10)) == 1  # 同一窗口同一类只报一次

    watcher.poll()  # 通了 → 忘掉去重记录
    watcher.poll()
    assert len(recent(conn, limit=10)) == 2  # 恢复后再出问题要能重新报


def test_quota_over_threshold_alerts_once_per_window(conn):
    ledger = QuotaLedger(
        conn, POLYGONSCAN, limit=Limit(unit="calls", window="day", cap=10, rate_per_s=5), clock=lambda: BASE_MS
    )
    ledger.record(calls=9, at_ms=BASE_MS)
    fake = FakePolygon(ledger=ledger, script=[[], []])
    watcher = Watcher(conn, [target("polygon", ADDRESS)], polygon=fake, clock=lambda: BASE_MS)

    assert watcher.poll().transfers == []
    assert len(recent(conn, limit=5)) == 1
    assert watcher.poll().transfers == []
    assert len(recent(conn, limit=5)) == 1

    [item] = recent(conn, limit=5)
    assert item.kind == "chain_quota_high"
    assert item.payload["provider"] == POLYGONSCAN
    assert item.payload["used"] == 9
    assert item.payload["cap"] == 10
    assert item.payload["unit"] == "calls"


def test_check_quota_reports_crossed_limits(conn):
    ledger = QuotaLedger(
        conn, POLYGONSCAN, limit=Limit(unit="calls", window="day", cap=10, rate_per_s=5), clock=lambda: BASE_MS
    )
    ledger.record(calls=4, at_ms=BASE_MS)
    watcher = Watcher(
        conn, [target("polygon", ADDRESS)], polygon=FakePolygon(ledger=ledger), clock=lambda: BASE_MS
    )

    assert watcher.check_quota() == []
    ledger.record(calls=5, at_ms=BASE_MS)
    [summary] = watcher.check_quota()
    assert "polygonscan" in summary and "9/10" in summary


def test_helius_alerts_dedup_within_the_month(conn):
    ledger = QuotaLedger(conn, HELIUS, clock=lambda: BASE_MS)
    fake = FakeHelius(
        ledger=ledger,
        signatures_script=[RateLimited("Helius 限速：HTTP 429"), RateLimited("Helius 限速：HTTP 429")],
    )
    watcher = Watcher(conn, [target("solana", WALLET)], helius=fake, clock=lambda: BASE_MS)

    watcher.poll()
    watcher.poll()

    [item] = recent(conn, limit=5)
    assert item.kind == "chain_rate_limited"
    assert item.payload["provider"] == HELIUS


def test_observation_merge_keeps_order_and_failures(conn):
    left = Observation(transfers=[polygon_transfer(2)], failures=["a"])
    right = Observation(transfers=[polygon_transfer(1)], failures=["b"])
    merged = left.merge(right)
    assert [item.block for item in merged.transfers] == [1, 2]
    assert merged.failures == ["a", "b"]


class TickingClock:
    """可注入的时钟 + sleep（轮询循环的测试不需要真的等 60 秒）。"""

    def __init__(self, now_ms: int = BASE_MS) -> None:
        self.now = now_ms
        self.slept: list[float] = []

    def clock(self) -> int:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += int(seconds * 1000)


def test_run_rescans_first_then_polls_by_interval(conn):
    clock = TickingClock()
    ledger = QuotaLedger(conn, POLYGONSCAN, clock=clock.clock)
    fake = FakePolygon(
        ledger=ledger,
        script=[[polygon_transfer(400, tx="0xold")], [polygon_transfer(500, tx="0xnew")], []],
    )
    watcher = Watcher(conn, [target("polygon", ADDRESS)], polygon=fake, clock=clock.clock, sleep=clock.sleep)
    seen: list[str] = []

    total = watcher.run(seconds=POLL_SECONDS + 1, on_transfer=lambda item: seen.append(item.tx_ref))

    # ① 启动补扫（不看游标，把历史全摆上桌）② 一轮增量 ③ 剩下 1 秒就只睡 1 秒，到点停
    assert [call["from_block"] for call in fake.calls] == [0, 401, 501]
    assert [item.tx_ref for item in total.transfers] == ["0xold", "0xnew"]
    assert seen == ["0xold", "0xnew"]
    assert clock.slept == [POLL_SECONDS, 1.0]


def test_run_can_skip_the_startup_rescan(conn):
    clock = TickingClock()
    ledger = QuotaLedger(conn, POLYGONSCAN, clock=clock.clock)
    fake = FakePolygon(ledger=ledger, script=[[polygon_transfer(7)]])
    Watcher(
        conn, [target("polygon", ADDRESS)], polygon=fake, clock=clock.clock, sleep=clock.sleep
    ).run(seconds=0, rescan_first=False)
    assert [call["from_block"] for call in fake.calls] == [0]
