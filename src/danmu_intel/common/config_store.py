"""配置的存与取：**版本号 + 60 秒 TTL 缓存**（NFR-T-4 / 设计 §14.3）。

需求 Q-2 与 NFR-T-4 要求「后台可视化配置，改动 1 分钟内生效」，不得用「改配置文件 + 重启」
替代。这一条落在三个机制上，本模块负责前两个：

1. **写入即递增版本号**（`config_version` 表，单行）：改的是哪几把键、谁改的、什么时候改的，
   都是可查事实。跨进程传播变更靠它 —— 主进程（`collect/supervisor.py`）每轮调度读一次，
   变了就把采集子进程按新配置重起（第三个机制）。
2. **读取走进程内 60 秒 TTL 缓存**（`CACHE_TTL_MS`）：配置读取在热路径上（每页请求、
   每轮轮询、每条消息）被反复调用，缓存把「每次读一行 JSON」变成「最多 60 秒读一次」，
   因此任何进程对配置的滞后**不超过 60 秒**。后台保存自己那份立刻失效（同一进程内立即生效）。
3. 只有落到文件的库才缓存：`:memory:` 的库没有稳定身份键，直读（宁可慢，不把 A 库的配置
   当成 B 库的）。

配置值一律是 JSON 对象（`config.value_json`），取值语义（默认值、未知键报错）由各自的
配置模块负责（`common/config.py` 的统计门槛、`billing/pricing.py` 的收款配置）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: 配置缓存的有效期：NFR-T-4「配置改动 1 分钟内生效」的那个 1 分钟。
CACHE_TTL_MS = 60_000

#: 单调时钟（测试缝：注入假时钟即可跨过 TTL，不必真的等 60 秒）。
CLOCK: Callable[[], float] = time.monotonic

#: 进程内缓存：`(库文件路径, 配置键) → (写入单调时刻, 配置值)`。
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True, slots=True)
class Version:
    """配置版本号（`config_version` 单行）：每一次保存递增一级。"""

    version: int
    keys: tuple[str, ...]
    updated_at: int
    updated_by: str


def _db_path(conn: sqlite3.Connection) -> str | None:
    """库文件的绝对路径；`:memory:`（或无主库）返回 `None`（这类库不缓存）。"""
    row = conn.execute("PRAGMA database_list").fetchone()
    name = row["file"] if isinstance(row, sqlite3.Row) else row[2]
    if not name:
        return None
    return str(Path(str(name)).resolve())


def invalidate(conn: sqlite3.Connection | None = None) -> None:
    """作废缓存：给了连接就只作废这个库的，不给就全清（测试与手工热更用）。"""
    if conn is None:
        _CACHE.clear()
        return
    path = _db_path(conn)
    if path is None:
        return
    for slot in [key for key in _CACHE if key[0] == path]:
        _CACHE.pop(slot, None)


def version(conn: sqlite3.Connection) -> int:
    """当前配置版本号（还没保存过任何配置时为 0）。"""
    row = conn.execute("SELECT version FROM config_version WHERE id=1").fetchone()
    return 0 if row is None else int(row["version"])


def latest(conn: sqlite3.Connection) -> Version | None:
    """最近一次配置变更（版本号 + 改了哪几把键 + 谁改的 + 何时）—— 后台配置页显示它。"""
    row = conn.execute("SELECT * FROM config_version WHERE id=1").fetchone()
    if row is None:
        return None
    return Version(
        version=int(row["version"]),
        keys=tuple(json.loads(row["keys_json"])),
        updated_at=int(row["updated_at"]),
        updated_by=row["updated_by"],
    )


def load(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    """读一把配置键（TTL 缓存内直接命中，过期即重读）。没有该键 → 空对象（调用方取默认值）。"""
    path = _db_path(conn)
    slot = (path, key) if path is not None else None
    if slot is not None:
        cached = _CACHE.get(slot)
        if cached is not None and (CLOCK() * 1000 - cached[0]) < CACHE_TTL_MS:
            return dict(cached[1])
    row = conn.execute("SELECT value_json FROM config WHERE key=?", (key,)).fetchone()
    payload = {} if row is None else json.loads(row["value_json"])
    if slot is not None:
        _CACHE[slot] = (CLOCK() * 1000, dict(payload))
    return payload


def save(
    conn: sqlite3.Connection,
    key: str,
    value: Mapping[str, Any],
    *,
    actor: str,
    ts: int | None = None,
) -> Version:
    """写一把配置键 → **递增版本号** → 本进程缓存立刻换成新值。返回新的版本号。

    只在这里写 `config` 与 `config_version`：于是「谁改了配置」与「改到第几版」永远是
    同一份事实（审计记录由调用方另写，带上改动前后的完整值）。
    """
    if not actor:
        raise ValueError("改配置必须写明 actor（谁改的）")
    moment = int(time.time() * 1000) if ts is None else ts
    payload_json = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO config(key, value_json, updated_at, updated_by) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, payload_json, moment, actor),
    )
    changelog = _bump(conn, keys=(key,), actor=actor, moment=moment)
    path = _db_path(conn)
    if path is not None:
        # 保存方**立刻**失效自己那份缓存：后台改完的下一屏就必须是新值（不等 TTL）。
        _CACHE[(path, key)] = (CLOCK() * 1000, dict(value))
    return changelog


def bump(conn: sqlite3.Connection, *, keys: Sequence[str], actor: str, ts: int | None = None) -> Version:
    """只递增版本号，不写 `config` 行：**存在别的表里、但同样要跨进程生效**的改动走它。

    今天只有数据源（`rooms` 表）：后台在页面上加/删一个直播间，采集监督进程要在 1 分钟内
    增/停对应的子进程（FR-C8-1/C8-2、需求 §3.4）。版本号是这条链路的唯一触发信号，
    因此数据源改动必须落在同一个计数器上。
    """
    if not actor:
        raise ValueError("改配置必须写明 actor（谁改的）")
    moment = int(time.time() * 1000) if ts is None else ts
    return _bump(conn, keys=tuple(keys), actor=actor, moment=moment)


def _bump(conn: sqlite3.Connection, *, keys: Sequence[str], actor: str, moment: int) -> Version:
    conn.execute(
        "INSERT INTO config_version(id, version, keys_json, updated_at, updated_by) "
        "VALUES(1, 1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET version=config_version.version+1, "
        "keys_json=excluded.keys_json, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (json.dumps(list(keys)), moment, actor),
    )
    conn.commit()
    return Version(version=version(conn), keys=tuple(keys), updated_at=moment, updated_by=actor)
