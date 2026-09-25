"""通知文本（给人看的最后一步）。

一条通知发到 QQ / Telegram 就是一段纯文本，格式固定三行以内：

```
【严重】采集进程退出｜比赛 #1｜huya/660000
pid 3 已退出（exit_code=-9）
2026-09-25 13:20:31
```

- 第一行：级别 + 事件名（`KIND_LABELS` 里的中文，未知 kind 原样输出，**不猜**）+ 主语；
- 第二行：其余细节（`key=value`，值超长截断）；
- 第三行：入库时刻。

不做 Markdown / HTML 转义：QQ 与 Telegram 的纯文本模式两边都能原样显示，少一层转义就
少一处出错。文本长度按 Telegram 的上限（4096）之外再收一道到 `MAX_DETAIL_CHARS`，
免得一条通知因为 payload 里塞了长原因而发不出去。
"""

from __future__ import annotations

import json
from datetime import datetime

from danmu_intel.common.notifications import Notification

from danmu_intel.notify.suppression import RECOVERY_SUFFIX

SEVERITY_LABELS = {"info": "提示", "warning": "注意", "critical": "严重"}
MAX_DETAIL_CHARS = 400

#: kind → 中文事件名（设计 §15 的十类 + 各自的变体）。
KIND_LABELS: dict[str, str] = {
    # 采集（设计 §15 #1/#2/#3/#4）
    "process_exit": "采集进程退出",
    "process_hung": "采集进程僵死",
    "restart_exceeded": "重启超限（已停止重试）",
    "no_stream": "没有首条弹幕",
    "stalled": "断流重连",
    "disk_low": "磁盘将满",
    "drop_rate_high": "落盘丢包率超阈",
    # 发布（设计 §15 #5）
    "release.failed": "发布失败",
    "release.deploy_unknown": "部署号读不到",
    "release.reconcile_failed": "版本库对齐失败",
    # 链上（设计 §15 #6）
    "chain_quota_high": "链上额度越线",
    "chain_rate_limited": "供应商限速",
    "chain_fetch_failed": "链上拉取失败",
    # 付费与会员（设计 §15 #7/#8/#10）
    "billing_payment_short": "订单待补款",
    "billing_transfer_unmatched": "入账对不上账",
    "billing_payment_extra": "已付订单又有入账",
    "billing_grant_failed": "付款到账但开通失败",
    "billing_member_sweep_failed": "会员到期批处理失败",
    # 解读层（设计 §15 #9）
    "llm_cost_gate": "解读层成本超闸",
    "llm_unavailable": "解读层降级",
}




def format_message(notification: Notification) -> str:
    """一条通知 → 一段纯文本。"""
    recovery = notification.kind.endswith(RECOVERY_SUFFIX)
    base_kind = notification.kind[: -len(RECOVERY_SUFFIX)] if recovery else notification.kind
    label = KIND_LABELS.get(base_kind, base_kind)
    if recovery:
        label = f"{label}（已恢复）"
    severity = SEVERITY_LABELS.get(notification.severity, notification.severity)
    subject, used = _subject(notification.payload)
    headline = f"【{severity}】{label}"
    if subject:
        headline += f"｜{subject}"
    lines = [headline]
    detail = _detail(notification.payload, skip=used)
    if detail:
        lines.append(detail)
    lines.append(_stamp(notification.created_at))
    return "\n".join(lines)


def _subject(payload: dict[str, object]) -> tuple[str, set[str]]:
    """比赛 / 房间 / 订单 / 供应商 —— 一句话说清"是哪儿的"（连带用掉了哪些字段）。"""
    parts: list[str] = []
    used: set[str] = set()
    if payload.get("match_id") is not None:
        parts.append(f"比赛 #{payload['match_id']}")
        used.add("match_id")
    if payload.get("platform") and payload.get("room_id"):
        parts.append(f"{payload['platform']}/{payload['room_id']}")
        used |= {"platform", "room_id"}
    elif payload.get("room_id"):
        parts.append(f"房间 {payload['room_id']}")
        used.add("room_id")
    if payload.get("order_ref"):
        parts.append(f"订单 {payload['order_ref']}")
        used.add("order_ref")
    if payload.get("provider"):
        parts.append(f"供应商 {payload['provider']}")
        used.add("provider")
    if payload.get("release") is not None and not parts:
        parts.append(f"发布批次 #{payload['release']}")
        used.add("release")
    return "｜".join(parts), used


def _detail(payload: dict[str, object], *, skip: set[str]) -> str:
    parts: list[str] = []
    for key, value in payload.items():
        if key in skip or key == "alert_key" or key == "resolved":
            continue
        if value is None or value == "":
            continue
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={rendered}")
    joined = "｜".join(parts)
    if len(joined) <= MAX_DETAIL_CHARS:
        return joined
    return joined[: MAX_DETAIL_CHARS - 1] + "…"


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
