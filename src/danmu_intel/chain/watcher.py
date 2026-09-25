"""链上监听器：60s 轮询 + 启动补扫 + 双重路径兜底（ADR-0005 / AC-5 / FR-C6-11）。

两条**互相独立**的路径同时存在，这不是冗余而是兜底（ADR-0005「双重路径」）：

| 路径 | 起手式 | 管什么 |
|---|---|---|
| 增量轮询（`poll`） | 从该地址的游标往后扫 | 日常：每 60 秒看见新入账 |
| 按地址补扫（`rescan`） | 不看游标，直接查全历史 | 启动、异常恢复、手动补扫 |

游标坏了、跳了、被某个地址的入账越过去了，补扫都能把漏掉的那笔重新摆到桌面上
（AC-5 原文就是"故意漏掉一笔，事后补扫能发现它"）。因此 `rescan` **绝不**依赖游标，
`poll` 也**绝不**假设游标是对的。

一条纪律贯穿整个模块：**扫描失败不得静默**。某个地址这一轮扫不成，其它地址照扫，
同时写一条报警（限速 / 拉取失败），并把原因放进 `Observation.failures` 如实带回。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import tzinfo

from danmu_intel.chain import alerts, cursor
from danmu_intel.chain.alerts import AlertGate
from danmu_intel.chain.helius import HeliusClient
from danmu_intel.chain.polygonscan import PolygonscanClient
from danmu_intel.chain.provider import ProviderError, RateLimited
from danmu_intel.chain.quota import (
    HELIUS,
    POLYGONSCAN,
    QuotaLedger,
    day_key,
    month_key,
    now_ms,
)
from danmu_intel.chain.transfer import NETWORKS, SOLANA, Transfer, sort_key

#: 轮询间隔（设计 §12.2 第 ⑤ 步：chain-watcher 每 60s 轮询）。
POLL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class WatchTarget:
    """一个监听目标：某条链上的一个收款地址。"""

    network: str
    address: str

    def __post_init__(self) -> None:
        if self.network not in NETWORKS:
            raise ValueError(f"未知的收款网络：{self.network}（允许：{','.join(NETWORKS)}）")
        if not self.address:
            raise ValueError("监听地址不能为空")

    @property
    def scope(self) -> str:
        """游标 scope（就是地址本身：一地址一游标，见 `cursor.py`）。"""
        return self.address

    def label(self) -> str:
        return f"{self.network}/{self.address}"


@dataclass(frozen=True, slots=True)
class Observation:
    """一轮扫描的结果：看见的入账 + 没扫成的目标（原因如实带回）。"""

    transfers: list[Transfer] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def merge(self, other: "Observation") -> "Observation":
        return Observation(
            transfers=sorted([*self.transfers, *other.transfers], key=sort_key),
            failures=[*self.failures, *other.failures],
        )


class Watcher:
    """两条链的入账监听（客户端与报警出口都可注入 → 断网可跑）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        targets: list[WatchTarget],
        *,
        polygon: PolygonscanClient | None = None,
        helius: HeliusClient | None = None,
        gate: AlertGate | None = None,
        clock=now_ms,
        sleep=time.sleep,
        tz: tzinfo | None = None,
    ) -> None:
        self._conn = conn
        self._targets = list(targets)
        self._polygon = polygon
        self._helius = helius
        self._ledgers: dict[str, QuotaLedger] = {}
        if polygon is not None:
            self._ledgers["polygon"] = polygon.ledger
        if helius is not None:
            self._ledgers[SOLANA] = helius.ledger
        missing = sorted({target.network for target in self._targets} - set(self._ledgers))
        if missing:
            raise ValueError(
                f"缺少这些网络的客户端：{','.join(missing)}（监听目标："
                f"{'、'.join(target.label() for target in self._targets) or '无'}）"
            )
        self._gate = gate or AlertGate(conn)
        self._clock = clock
        self._sleep = sleep
        self._tz = tz

    @property
    def targets(self) -> list[WatchTarget]:
        return list(self._targets)

    def poll(self) -> Observation:
        """增量：每个目标从自己的游标往后扫（日常路径）。"""
        return self._sweep(rescan=False)

    def rescan(self) -> Observation:
        """补扫：按地址查全历史，**不依赖游标**（启动 / 恢复 / 手动补扫）。"""
        return self._sweep(rescan=True)

    def run(
        self,
        *,
        seconds: float | None = None,
        interval: float = POLL_SECONDS,
        on_transfer=None,
        rescan_first: bool = True,
    ) -> Observation:
        """启动补扫 → 每 `interval` 秒增量轮询，跑满 `seconds`（缺省则跑到中断）。

        `on_transfer` 是产出的出口：每看见一笔入账立刻交出去（订单匹配属 T9），
        不让调用方等到循环结束才拿到东西。
        """
        started = self._clock()
        deadline = None if seconds is None else started + int(seconds * 1000)
        total = Observation()
        pending = self.rescan() if rescan_first else None
        while True:
            observation = self.poll() if pending is None else pending
            pending = None
            total = total.merge(observation)
            if on_transfer is not None:
                for transfer in observation.transfers:
                    on_transfer(transfer)
            if deadline is None:
                self._sleep(interval)
                continue
            # 剩下不足一个间隔就只睡剩下的：`seconds` 是总时长，不是「最后多跑一轮」
            remaining = (deadline - self._clock()) / 1000
            if remaining <= 0:
                return total
            self._sleep(min(interval, remaining))

    def check_quota(self) -> list[str]:
        """额度越线的供应商：报警（同窗口只报一次）并返回说明，供 CLI 打印。

        额度回落（不再越线）同样要说话：把台账转 `resolved` 并发一条恢复通知。
        """
        crossed: list[str] = []
        for ledger in self._ledgers.values():
            usage = ledger.usage()
            if not usage.over_threshold:
                self._gate.forget(alerts.QUOTA_HIGH, provider=usage.provider)
                continue
            self._gate.emit(
                alerts.QUOTA_HIGH,
                provider=usage.provider,
                window=usage.window_key,
                detail={"used": usage.used, "cap": usage.cap, "unit": usage.limit.unit},
            )
            crossed.append(usage.summary())
        return crossed

    # —— 内部 ——

    def _sweep(self, *, rescan: bool) -> Observation:
        transfers: list[Transfer] = []
        failures: list[str] = []
        for target in self._targets:
            try:
                found, newest = self._fetch(target, rescan=rescan)
            except (RateLimited, ProviderError) as exc:
                failures.append(f"{target.label()}：{exc}")
                self._report(target, exc)
                continue
            if newest is not None:
                # 只在真的处理了新记录时才推游标：没扫到东西就是没扫到，不假装进度。
                cursor.advance(
                    self._conn, target.network, target.scope, newest, at_ms=self._clock()
                )
            # 这个供应商又通了：忘掉去重记录，下次再出问题要能重新报警。
            provider = self._provider_of(target.network)
            self._gate.forget(alerts.RATE_LIMITED, provider=provider)
            self._gate.forget(alerts.FETCH_FAILED, provider=provider)
            transfers.extend(found)
        self.check_quota()
        return Observation(transfers=sorted(transfers, key=sort_key), failures=failures)

    def _fetch(self, target: WatchTarget, *, rescan: bool) -> tuple[list[Transfer], str | None]:
        """扫一个目标，返回 (入账, 处理到的最新游标位置)。"""
        if target.network == SOLANA:
            return self._fetch_solana(target, rescan=rescan)
        return self._fetch_polygon(target, rescan=rescan)

    def _fetch_polygon(self, target: WatchTarget, *, rescan: bool) -> tuple[list[Transfer], str | None]:
        assert self._polygon is not None  # 构造时已校验：目标所在网络必须有客户端
        known = None if rescan else cursor.get(self._conn, target.network, target.scope)
        # 游标是「已处理到的最新入账区块」，因此从它的**下一块**起扫：同区块的多笔
        # 入账在上一次响应里已经一起拿到了，不重复扫也就不会重复报。
        from_block = 0 if known is None else int(known) + 1
        found = self._polygon.transfers(target.address, from_block=from_block)
        blocks = [item.block for item in found if item.block is not None]
        return found, (str(max(blocks)) if blocks else None)

    def _fetch_solana(self, target: WatchTarget, *, rescan: bool) -> tuple[list[Transfer], str | None]:
        assert self._helius is not None  # 构造时已校验
        until = None if rescan else cursor.get(self._conn, target.network, target.scope)
        rows = self._helius.signatures(target.address, until=until)
        found: list[Transfer] = []
        for row in rows:
            found.extend(self._helius.transaction(str(row["signature"]), address=target.address))
        # 游标用**已处理的最新签名**（哪怕那笔对我们没有入账）：否则没有入账的签名
        # 会被每一轮反复取回，白白烧 credit。
        return found, (str(rows[-1]["signature"]) if rows else None)

    def _report(self, target: WatchTarget, exc: ProviderError) -> None:
        kind = alerts.RATE_LIMITED if isinstance(exc, RateLimited) else alerts.FETCH_FAILED
        self._gate.emit(
            kind,
            provider=self._provider_of(target.network),
            window=self._window_of(target.network),
            detail={"scope": target.address, "network": target.network, "reason": str(exc)},
            timestamp=self._clock(),
        )

    def _provider_of(self, network: str) -> str:
        return HELIUS if network == SOLANA else POLYGONSCAN

    def _window_of(self, network: str) -> str:
        """告警去重用的窗口键：跟该供应商的额度窗口一致（日 / 月）。"""
        stamp = self._clock()
        limit = self._ledgers[network].limit
        if limit.window == "month":
            return month_key(stamp, tz=self._tz)
        return day_key(stamp, tz=self._tz)
