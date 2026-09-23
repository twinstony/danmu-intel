"""解读层的报警出口（ADR-0003 / ADR-0014：降级不静默）。

两类事件，都写进共享的待投递出口（`common/notifications.py`，投递属 T11）：

| kind | 何时 | 严重级别 |
|---|---|---|
| `llm_cost_gate` | 单场 ¥0.3 或当日 ¥10 的成本硬闸被触及 | warning |
| `llm_unavailable` | 连续 3 次调用失败，解读能力全局降级 | critical |

**单次失败不报警**：一次超时/一次幻觉只让那一段降级，报告里已经如实标注
（`llm_state='rule_fallback'` + 降级原因），再发一条通知只会变成噪声。
"""

from __future__ import annotations

import sqlite3

from danmu_intel.common.notifications import emit

COST_GATE = "llm_cost_gate"
UNAVAILABLE = "llm_unavailable"
KINDS = (COST_GATE, UNAVAILABLE)


def alert(
    conn: sqlite3.Connection,
    kind: str,
    *,
    match_id: int | None,
    severity: str,
    detail: dict[str, object] | None = None,
    timestamp: int | None = None,
) -> int:
    """写一条待投递的解读层报警。"""
    if kind not in KINDS:
        raise ValueError(f"未知的解读层报警类型：{kind}（允许：{','.join(KINDS)}）")
    payload: dict[str, object] = {"match_id": match_id}
    payload.update(detail or {})
    return emit(conn, kind, severity=severity, payload=payload, timestamp=timestamp)
