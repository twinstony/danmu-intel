"""链上异常的报警出口（设计 §15 #6，FR-C6-11）。

三类事件，都写进共享的待投递出口（`common/notifications.py`，投递属 T11）：

| kind | 何时 | 严重级别 |
|---|---|---|
| `chain_quota_high` | 额度用量越过 80% 阈值 | critical |
| `chain_rate_limited` | 供应商返回限速（429 / "Max rate limit reached"） | critical |
| `chain_fetch_failed` | 拉取失败（断网、接口报错、响应不是我们认识的形状） | critical |

**绝不静默**：需求 FR-C6-11 要的正是"检测能力受限时报警，而不是静默漏检"——这也是
本模块存在的唯一理由。严重级别写死在 `SEVERITIES` 里：设计 §15 的第 6 行把三种情况
一律定为「高」，级别不由调用方临时决定，免得同一种故障在不同调用点报出不同的档。
"""

from __future__ import annotations

import sqlite3

from danmu_intel.common.notifications import emit

QUOTA_HIGH = "chain_quota_high"
RATE_LIMITED = "chain_rate_limited"
FETCH_FAILED = "chain_fetch_failed"

KINDS = (QUOTA_HIGH, RATE_LIMITED, FETCH_FAILED)

SEVERITIES: dict[str, str] = {
    QUOTA_HIGH: "critical",
    RATE_LIMITED: "critical",
    FETCH_FAILED: "critical",
}


def alert(
    conn: sqlite3.Connection,
    kind: str,
    *,
    provider: str,
    detail: dict[str, object] | None = None,
    timestamp: int | None = None,
) -> int:
    """写一条待投递的链上报警。"""
    if kind not in KINDS:
        raise ValueError(f"未知的链上报警类型：{kind}（允许：{','.join(KINDS)}）")
    payload: dict[str, object] = {"provider": provider}
    payload.update(detail or {})
    return emit(conn, kind, severity=SEVERITIES[kind], payload=payload, timestamp=timestamp)


class AlertGate:
    """同一 `(kind, provider, window)` 只报一次（进程内）。

    额度阈值与限速在**每一次轮询后**都会重新判定，不去重就会每 60 秒写一条一模一样的
    通知，把待投递队列刷成噪声（同 T2 的 `SessionIncidents.emit_once` 是同一个理由）。
    去重只活在进程内是有意的：重启后再报一次恰恰有用——重启本身就是一次"重新看现状"，
    而"这件事还在吗"正是运维要的答案。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._seen: set[tuple[str, str, str]] = set()

    def emit(
        self,
        kind: str,
        *,
        provider: str,
        window: str,
        detail: dict[str, object] | None = None,
        timestamp: int | None = None,
    ) -> bool:
        """写一条报警；本进程内同一窗口同类的第二条起返回 `False` 且不写。"""
        key = (kind, provider, window)
        if key in self._seen:
            return False
        self._seen.add(key)
        alert(self._conn, kind, provider=provider, detail=detail, timestamp=timestamp)
        return True

    def forget(self, kind: str, *, provider: str) -> None:
        """忘掉某类告警的去重记录（额度回落/恢复正常后再出问题要能再报）。"""
        self._seen = {key for key in self._seen if not (key[0] == kind and key[1] == provider)}
