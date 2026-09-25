"""日汇总与 90 天保留（设计 §13「明细 90 天 → 汇总入 `stats_daily`」）。

明细（`stats_events`）是**口径的原始依据**：一行一次页面访问，带当日盐下的访客哈希
与「当时是不是付费页」。汇总（`stats_daily`）是明细到期后唯一留存的那份数字 ——
一旦明细被裁剪，日数字就再也算不出来，所以裁剪**先汇总再删**，不丢账。

回答运营问题（设计 §13「可回答」）的口径：

| 问题 | 数字 | 来源 |
|---|---|---|
| 某天访问量 | `page_views`（次数）/ `unique_visitors`（人数）/ `sessions`（会话数） | 明细，或到期后的 `stats_daily` |
| 访问付费页人数（AC-9） | `paid_unique_visitors`（另给 `paid_page_views` 次数） | 同上 |
| 下单转化 | `orders` ÷ 当日 `paid_unique_visitors` | `orders` 账本（长期保留，不复制进汇总） |
| 付费转化 | `paid_orders` | `orders.paid_at` |
| 留资数 | `leads` | `members` 账本（长期保留） |

**留资与付费的关联靠 `member_id`，但汇总里只出计数**：明细里的 `member_id` 只是代理键，
联系方式留在 `members` 表（NFR-P-4「留资不与统计明细混存」）；对外回答只有聚合数字，
答不了「具体是谁」—— 除非那人自己留资或付费（AC-9 的「除非」，那一问由会员/订单账本回答）。

会话口径：同一访客哈希（同一天内）两次访问间隔 ≥ 30 分钟算新会话。没有客户端会话 ID，
这是自建统计能做到的确定性近似，且**由明细唯一确定**（重算得同一数字）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from danmu_intel.site_stats.beacon import day_of, now_ms

#: 明细在线保留期（设计 §13：明细 90 天 → 汇总入 `stats_daily`）。
DETAIL_RETENTION_DAYS = 90

#: 会话切分：同一访客两次访问间隔达到这个时长算新会话。
SESSION_GAP_MS = 30 * 60 * 1000

DAY_FORMAT = "%Y-%m-%d"


def parse_day(day: object) -> str:
    """校验 `YYYY-MM-DD`（对外接口的入参也走这里，避免把脏字符串塞进 SQL）。"""
    text = str(day or "").strip()
    try:
        return datetime.strptime(text, DAY_FORMAT).strftime(DAY_FORMAT)
    except ValueError:
        raise ValueError(f"日期应为 YYYY-MM-DD：{text!r}") from None


def day_start_ms(day: str) -> int:
    """该日本地零点（毫秒）。"""
    return int(datetime.strptime(parse_day(day), DAY_FORMAT).timestamp() * 1000)


def _window(day: str) -> tuple[int, int]:
    start = day_start_ms(day)
    return start, start + 24 * 3600 * 1000


@dataclass(frozen=True, slots=True)
class DaySummary:
    """某一天的站点统计口径（明细还在就现算，到期就读汇总）。"""

    day: str
    page_views: int = 0
    sessions: int = 0
    unique_visitors: int = 0
    paid_page_views: int = 0
    paid_unique_visitors: int = 0
    leads: int = 0
    orders: int = 0
    paid_orders: int = 0

    def as_dict(self) -> dict[str, object]:
        """对外响应体：**只有计数**，没有 IP / UA / 访客哈希 / 身份字段（AC-9）。"""
        return {
            "day": self.day,
            "page_views": self.page_views,
            "sessions": self.sessions,
            "unique_visitors": self.unique_visitors,
            "paid_page_views": self.paid_page_views,
            "paid_unique_visitors": self.paid_unique_visitors,
            "leads": self.leads,
            "orders": self.orders,
            "paid_orders": self.paid_orders,
        }


@dataclass(frozen=True, slots=True)
class PruneResult:
    """一次裁剪的结果：汇总并删除的日期 + 删掉的明细条数。"""

    days: tuple[str, ...]
    events: int

    def summary(self) -> str:
        if not self.days:
            return "没有超过保留期的明细"
        return (
            f"已汇总并删除 {len(self.days)} 天明细（{self.days[0]}…{self.days[-1]}，"
            f"共 {self.events} 条）"
        )


def _counts_from_detail(conn: sqlite3.Connection, day: str) -> tuple[int, int, int, int, int] | None:
    """从明细算 `(访问次数, 会话数, 独立访客, 付费页次数, 付费页人数)`；没有明细返回 `None`。"""
    rows = conn.execute(
        "SELECT visitor_hash, ts, paid FROM stats_events WHERE day=? ORDER BY visitor_hash, ts",
        (day,),
    ).fetchall()
    if not rows:
        return None
    page_views = len(rows)
    unique_visitors = len({str(row["visitor_hash"]) for row in rows})
    paid_page_views = sum(1 for row in rows if int(row["paid"]))
    paid_unique_visitors = len({str(row["visitor_hash"]) for row in rows if int(row["paid"])})
    sessions = 0
    last_hash: str | None = None
    last_ts = 0
    for row in rows:  # 已按 (visitor_hash, ts) 排序：同一访客的时间相邻，缺口即新会话
        digest = str(row["visitor_hash"])
        stamp = int(row["ts"])
        if digest != last_hash or stamp - last_ts >= SESSION_GAP_MS:
            sessions += 1
        last_hash, last_ts = digest, stamp
    return page_views, sessions, unique_visitors, paid_page_views, paid_unique_visitors


def _funnel(conn: sqlite3.Connection, day: str) -> tuple[int, int, int]:
    """留资 / 下单 / 付费三个计数：来自各自账本（长期保留，不复制进汇总）。"""
    start, end = _window(day)
    leads = conn.execute(
        "SELECT COUNT(*) AS n FROM members WHERE created_at >= ? AND created_at < ?", (start, end)
    ).fetchone()["n"]
    orders = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE created_at >= ? AND created_at < ?", (start, end)
    ).fetchone()["n"]
    paid_orders = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE paid_at IS NOT NULL AND paid_at >= ? AND paid_at < ?",
        (start, end),
    ).fetchone()["n"]
    return int(leads), int(orders), int(paid_orders)


def _stored(conn: sqlite3.Connection, day: str) -> tuple[int, int, int, int, int] | None:
    row = conn.execute("SELECT * FROM stats_daily WHERE day=?", (day,)).fetchone()
    if row is None:
        return None
    return (
        int(row["page_views"]),
        int(row["sessions"]),
        int(row["unique_visitors"]),
        int(row["paid_page_views"]),
        int(row["paid_unique_visitors"]),
    )


def rollup(conn: sqlite3.Connection, day: str) -> DaySummary:
    """把某天的明细汇总进 `stats_daily`（幂等：重算得同一行）。

    明细已被裁剪的日子不动既有的汇总行 —— 汇总行是那时唯一的口径，不能被空明细改写。
    """
    target = parse_day(day)
    counts = _counts_from_detail(conn, target)
    stored = _stored(conn, target) if counts is None else counts
    if counts is not None:
        conn.execute(
            "INSERT INTO stats_daily(day, page_views, sessions, unique_visitors, "
            "paid_page_views, paid_unique_visitors) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET page_views=excluded.page_views, "
            "sessions=excluded.sessions, unique_visitors=excluded.unique_visitors, "
            "paid_page_views=excluded.paid_page_views, "
            "paid_unique_visitors=excluded.paid_unique_visitors",
            (target, *counts),
        )
        conn.commit()
    return _summary_of(conn, target, stored)


def _summary_of(
    conn: sqlite3.Connection, day: str, counts: tuple[int, int, int, int, int] | None
) -> DaySummary:
    page_views, sessions, unique_visitors, paid_page_views, paid_unique_visitors = counts or (
        0,
        0,
        0,
        0,
        0,
    )
    leads, orders, paid_orders = _funnel(conn, day)
    return DaySummary(
        day=day,
        page_views=page_views,
        sessions=sessions,
        unique_visitors=unique_visitors,
        paid_page_views=paid_page_views,
        paid_unique_visitors=paid_unique_visitors,
        leads=leads,
        orders=orders,
        paid_orders=paid_orders,
    )


def summary(conn: sqlite3.Connection, day: str) -> DaySummary:
    """某天的口径：**明细优先**（它是最新鲜的），明细被裁剪后读汇总。"""
    target = parse_day(day)
    counts = _counts_from_detail(conn, target)
    if counts is None:
        counts = _stored(conn, target)
    return _summary_of(conn, target, counts)


def detail_days(conn: sqlite3.Connection) -> tuple[str, ...]:
    """有明细的日期（升序）。"""
    rows = conn.execute("SELECT DISTINCT day FROM stats_events ORDER BY day").fetchall()
    return tuple(str(row["day"]) for row in rows)


def prune(
    conn: sqlite3.Connection,
    *,
    at_ms: int | None = None,
    retention_days: int = DETAIL_RETENTION_DAYS,
) -> PruneResult:
    """保留期外的明细**先汇总再删**；返回删掉的日期与条数。

    裁剪的边界是「本地日历日」：`at_ms` 往前数 `retention_days` 天的那天零点之前全算到期。
    """
    if retention_days < 1:
        raise ValueError(f"保留天数必须为正：{retention_days}")
    stamp = now_ms() if at_ms is None else int(at_ms)
    cutoff = day_of(stamp - retention_days * 24 * 3600 * 1000)
    expired = tuple(day for day in detail_days(conn) if day < cutoff)
    deleted = 0
    for day in expired:
        rollup(conn, day)
        cursor = conn.execute("DELETE FROM stats_events WHERE day=?", (day,))
        deleted += int(cursor.rowcount)
    if expired:
        conn.commit()
    return PruneResult(days=expired, events=deleted)


def recent_days(conn: sqlite3.Connection, *, limit: int = 30) -> tuple[str, ...]:
    """有数据的最近若干天（升序）：明细与汇总合起来看，供后台/运维列表用。"""
    rows = conn.execute(
        "SELECT day FROM (SELECT DISTINCT day FROM stats_events "
        "UNION SELECT day FROM stats_daily) ORDER BY day DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return tuple(sorted(str(row["day"]) for row in rows))
