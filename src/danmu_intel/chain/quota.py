"""供应商额度记账与阈值判定（`quota_usage`，ADR-0005 / FR-C6-11 / NFR-C-3）。

两条链的额度形状不同，如实建模：

| provider | 计量单位 | 上限窗口 | 免费上限（ADR-0005） | 突发限速 |
|---|---|---|---|---|
| `polygonscan` | calls | 日 | 10 万 calls/天 | 5 calls/s |
| `helius` | credits | 月 | 100 万 credits/月 | 10 req/s |

三条纪律：

1. **每一次对外调用都记一行**（成功与失败都记，失败把原因写进 `last_error`）。
   "看不到用量"本身就是漏检的前兆，记账不能挑着记。
2. **用量 >80% → 报警**（`ALERT_RATIO`），阈值判定只读账本，因此可以脱离网络断言。
3. **限速用最小间隔限速**，宁可慢一点也不要撞 429：撞上去就是一段时间完全瞎。
   稳态下每 60 秒只有一两次调用，限速根本不生效；真正会打满速率的是补扫翻页。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from danmu_intel.chain.transfer import SOLANA

POLYGONSCAN = "polygonscan"
HELIUS = "helius"

PROVIDERS: tuple[str, ...] = (POLYGONSCAN, HELIUS)

#: 供应商 → 收款网络（记账按供应商，监听按网络，两者在这里对上）。
PROVIDER_NETWORKS: dict[str, str] = {POLYGONSCAN: "polygon", HELIUS: SOLANA}

#: 报警阈值：用量**超过**八成即报（设计 §12.7 / 需求 FR-C6-11）。
ALERT_RATIO = 0.8


@dataclass(frozen=True, slots=True)
class Limit:
    """一个供应商的额度形状。"""

    unit: str  # calls | credits
    window: str  # day | month
    cap: int
    rate_per_s: float

    @property
    def window_label(self) -> str:
        return {"day": "日", "month": "月"}[self.window]


#: 免费额度与限速（ADR-0005 实测值：Polygonscan 5 calls/s、10 万 calls/天；
#: Helius 1M credits/月、10 req/s）。
LIMITS: dict[str, Limit] = {
    POLYGONSCAN: Limit(unit="calls", window="day", cap=100_000, rate_per_s=5.0),
    HELIUS: Limit(unit="credits", window="month", cap=1_000_000, rate_per_s=10.0),
}


def limit_for(provider: str) -> Limit:
    try:
        return LIMITS[provider]
    except KeyError:
        raise ValueError(
            f"未登记的供应商：{provider}（已登记：{','.join(PROVIDERS)}）"
        ) from None


def now_ms() -> int:
    return int(time.time() * 1000)


def day_key(at_ms: int, *, tz: tzinfo | None = None) -> str:
    """本地日历日（ADR-0001 单机部署；额度按本机所在日的自然日重置）。"""
    return datetime.fromtimestamp(at_ms / 1000, tz=tz).strftime("%Y-%m-%d")


def month_key(at_ms: int, *, tz: tzinfo | None = None) -> str:
    return datetime.fromtimestamp(at_ms / 1000, tz=tz).strftime("%Y-%m")


def _month_bounds(at_ms: int, *, tz: tzinfo | None = None) -> tuple[str, str]:
    """当月首日与次月首日的 `YYYY-MM-DD`（用字符串区间扫当月，不依赖 SQL 日期函数）。"""
    moment = datetime.fromtimestamp(at_ms / 1000, tz=tz)
    first = moment.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return first.strftime("%Y-%m-%d"), following.strftime("%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class Usage:
    """一个供应商的用量快照（当日 / 当月都给出，NFR-C-3）。"""

    provider: str
    limit: Limit
    window_key: str  # 上限所在窗口的键：`2026-09-24`（日）或 `2026-09`（月）
    used: int  # 上限窗口内的用量
    day_calls: int
    day_credits: float
    month_calls: int
    month_credits: float
    last_error: str | None

    @property
    def cap(self) -> int:
        return self.limit.cap

    @property
    def ratio(self) -> float:
        return self.used / self.cap if self.cap else 0.0

    @property
    def over_threshold(self) -> bool:
        """用量**超过** 80%（需求 FR-C6-11 的"用量 >80%"）。"""
        return self.ratio > ALERT_RATIO

    def summary(self) -> str:
        return (
            f"{self.provider}：{self.limit.window_label}{self.limit.unit} "
            f"{self.used}/{self.cap}（{self.ratio * 100:.1f}%，阈值 {ALERT_RATIO * 100:.0f}%）"
        )


class QuotaLedger:
    """一个供应商的额度账本（`quota_usage` 按供应商按日累加，只增不改）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: str,
        *,
        limit: Limit | None = None,
        clock=now_ms,
        tz: tzinfo | None = None,
    ) -> None:
        self._conn = conn
        self._provider = provider
        self._limit = limit or limit_for(provider)
        self._clock = clock
        self._tz = tz

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def limit(self) -> Limit:
        return self._limit

    def record(
        self,
        *,
        calls: int = 0,
        credits: float = 0.0,
        error: str | None = None,
        at_ms: int | None = None,
    ) -> None:
        """记一次调用（成功的与失败的都记）。

        `last_error` 反映**最近一次**调用的结局：成功会把它清空——它回答的是
        "现在还出不出问题"，历史全在 `notifications` 里。
        """
        stamp = self._clock() if at_ms is None else at_ms
        self._conn.execute(
            "INSERT INTO quota_usage(provider, day, calls, credits, last_error, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(provider, day) DO UPDATE SET "
            "calls = calls + excluded.calls, "
            "credits = credits + excluded.credits, "
            "last_error = excluded.last_error, "
            "updated_at = excluded.updated_at",
            (self._provider, day_key(stamp, tz=self._tz), calls, credits, error, stamp),
        )
        self._conn.commit()

    def usage(self, *, at_ms: int | None = None) -> Usage:
        """当日 / 当月用量快照（上限窗口内的用量另算，供阈值判定）。"""
        stamp = self._clock() if at_ms is None else at_ms
        day = day_key(stamp, tz=self._tz)
        month_from, month_to = _month_bounds(stamp, tz=self._tz)
        day_row = self._conn.execute(
            "SELECT calls, credits, last_error FROM quota_usage WHERE provider=? AND day=?",
            (self._provider, day),
        ).fetchone()
        month_row = self._conn.execute(
            "SELECT COALESCE(SUM(calls), 0) AS calls, COALESCE(SUM(credits), 0) AS credits "
            "FROM quota_usage WHERE provider=? AND day>=? AND day<?",
            (self._provider, month_from, month_to),
        ).fetchone()
        day_calls = int(day_row["calls"]) if day_row else 0
        day_credits = float(day_row["credits"]) if day_row else 0.0
        month_calls = int(month_row["calls"])
        month_credits = float(month_row["credits"])
        if self._limit.unit == "calls":
            used = day_calls if self._limit.window == "day" else month_calls
        else:
            raw = day_credits if self._limit.window == "day" else month_credits
            used = int(round(raw))
        window_key = day if self._limit.window == "day" else month_key(stamp, tz=self._tz)
        return Usage(
            provider=self._provider,
            limit=self._limit,
            window_key=window_key,
            used=used,
            day_calls=day_calls,
            day_credits=day_credits,
            month_calls=month_calls,
            month_credits=month_credits,
            last_error=(day_row["last_error"] if day_row else None),
        )


class RateLimiter:
    """最小间隔限速（令牌桶容量 1 的等价形式）：稳态速率 == 供应商的每秒上限。

    不实现突发容量是有意的：稳态轮询每 60 秒才一两次调用，突发容量毫无用处；真正会打满
    速率的是补扫（一次翻很多页），而那正是需要"慢一点、别撞 429"的地方。不需要等待时
    不 sleep，因此正常轮询不会被拖慢。
    """

    def __init__(self, rate_per_s: float, *, clock=time.monotonic, sleep=time.sleep) -> None:
        if rate_per_s <= 0:
            raise ValueError(f"限速必须为正：{rate_per_s}")
        self._interval = 1.0 / rate_per_s
        self._clock = clock
        self._sleep = sleep
        self._next_at: float | None = None

    def acquire(self) -> float:
        """取一个放行名额，必要时先等待；返回实际等待秒数（测试与日志用）。"""
        now = self._clock()
        wait = 0.0 if self._next_at is None else self._next_at - now
        if wait > 0:
            self._sleep(wait)
            now = self._clock()
        self._next_at = max(now, self._next_at if self._next_at is not None else now) + self._interval
        return max(0.0, wait)
