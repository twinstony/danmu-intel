"""付费正文的唯一出口（需求 §6.10、AC-2，ADR-0015 决策 3）。

**静态产物里没有付费正文**（`report/html.py` 在付费时不写任何段正文），正文只能从这里取：

- 比赛已结束（`matches.state == ended`）→ 所有人可见；
- 比赛进行中 → 只有凭据校验通过（会员）才拿得到，否则抛 `PaidAccessDenied`。

「凭据校验」本身（会员库、凭据哈希、不可区分性）属 T9；本模块的入参 `credential_verified`
就是那条边界：调用方（将来的 `GET /api/report/<id>/paid`）先校验凭据，再调这里。
"""

from __future__ import annotations

import sqlite3

from danmu_intel.common import paywall
from danmu_intel.common.matches import get_match
from danmu_intel.report.assemble import ReportContent
from danmu_intel.report.publish import load_content


def latest_version(conn: sqlite3.Connection, match_id: int, kind: str) -> int:
    """该场该形态**已发布**的最新版本号（`state='published'` 的行；失败版本不算）。"""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM reports WHERE match_id=? AND kind=? AND state='published'",
        (match_id, kind),
    ).fetchone()
    if row is None or row["v"] is None:
        raise LookupError(f"比赛 #{match_id} 还没有发布过 {kind} 报告")
    return int(row["v"])


def published_versions(conn: sqlite3.Connection) -> tuple[tuple[int, str, int, int], ...]:
    """全部「已有页面的报告」：`(match_id, kind, version, generated_at)`（同场同形态取最新已发布版）。

    站点产物从账本取报告，因此**产物的内容与账本一致**：页面上印的版本、事实层哈希与
    `reports` 行是同一份东西（设计 §10.3「同一形态换版即新增版本」）。
    """
    rows = conn.execute(
        """
        SELECT match_id, kind, MAX(version) AS version, MAX(generated_at) AS generated_at
        FROM reports WHERE state='published'
        GROUP BY match_id, kind
        ORDER BY match_id, kind
        """
    ).fetchall()
    return tuple(
        (int(row["match_id"]), str(row["kind"]), int(row["version"]), int(row["generated_at"]))
        for row in rows
    )


def report_content(
    conn: sqlite3.Connection,
    match_id: int,
    kind: str,
    *,
    credential_verified: bool = False,
    version: int | None = None,
) -> ReportContent:
    """返回报告正文；比赛未结束且凭据未通过时拒绝（`PaidAccessDenied`）。"""
    match_state = get_match(conn, match_id).state
    paywall.require_access(match_state=match_state, credential_verified=credential_verified)
    target_version = latest_version(conn, match_id, kind) if version is None else version
    return load_content(conn, match_id, kind, target_version)
