"""监听游标（`chain_cursors`，ADR-0005）：「上次扫到哪」这件事只存一处。

一个游标由一个 `(network, scope)` 唯一确定，**scope 就是被监听的收款地址**。按地址存
（而不是按链存一个全局游标）是有意的：多地址共用一个游标时，某个地址的入账会被另一个
地址推进的游标越过去，只能靠补扫兜底——把游标管到地址一级，"断点续扫"才是真的精确
（AC-5），补扫只是第二道保险，不是日常依赖。

游标语义（两条链的时间方向不同，如实表达）：

| network | cursor | 何时前进 |
|---|---|---|
| `polygon` | 已处理到的最新**入账区块号** | 只前进（`advance` 负责夹住） |
| `solana` | 已处理的最新**签名** | 只在真的处理了新签名时由调用方推动 |

Solana 的签名没有可用的大小序，夹不住，所以不假装能夹：`advance` 只对 polygon 做
单调保护，solana 的单调性由调用方「处理完新签名才写」保证。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from danmu_intel.chain.transfer import NETWORKS, POLYGON


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Cursor:
    """一个监听目标的游标现状。"""

    network: str
    scope: str
    cursor: str
    updated_at: int


def get(conn: sqlite3.Connection, network: str, scope: str) -> str | None:
    """取游标；从未扫过返回 `None`（调用方据此走"按地址查全历史"）。"""
    row = conn.execute(
        "SELECT cursor FROM chain_cursors WHERE network=? AND scope=?", (network, scope)
    ).fetchone()
    return None if row is None else str(row["cursor"])


def rows(conn: sqlite3.Connection, *, network: str | None = None) -> list[Cursor]:
    """列出游标（新→旧更新），供后台/CLI 查看"扫到哪了"。"""
    sql = "SELECT network, scope, cursor, updated_at FROM chain_cursors"
    params: tuple[object, ...] = ()
    if network is not None:
        sql += " WHERE network=?"
        params = (network,)
    sql += " ORDER BY updated_at DESC, id DESC"
    return [
        Cursor(
            network=row["network"],
            scope=row["scope"],
            cursor=row["cursor"],
            updated_at=int(row["updated_at"]),
        )
        for row in conn.execute(sql, params).fetchall()
    ]


def advance(
    conn: sqlite3.Connection,
    network: str,
    scope: str,
    cursor: str,
    *,
    at_ms: int | None = None,
) -> str:
    """推游标，返回落库后的值。

    polygon 的游标是区块号 → **只前进不回退**：补扫、乱序返回、重放都不会把游标往回拨
    （回拨等于把已经扫过的区间再扫一遍，最坏情况是把同一笔付款当成两笔）。
    """
    if network not in NETWORKS:
        raise ValueError(f"未知的收款网络：{network}（允许：{','.join(NETWORKS)}）")
    current = get(conn, network, scope)
    if network == POLYGON and current is not None and int(cursor) <= int(current):
        return current
    conn.execute(
        "INSERT INTO chain_cursors(network, scope, cursor, updated_at) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(network, scope) DO UPDATE SET cursor=excluded.cursor, "
        "updated_at=excluded.updated_at",
        (network, scope, str(cursor), now_ms() if at_ms is None else at_ms),
    )
    conn.commit()
    return str(cursor)
