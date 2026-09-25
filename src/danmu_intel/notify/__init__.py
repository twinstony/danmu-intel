"""通知投递（T11）：统一 5 分钟时效闸门，覆盖全事件类型（ADR-0010 / 设计 §15）。

三层，各自一件事：

| 模块 | 职责 |
|---|---|
| `channels.py` | 出口：QQ Bot（主）+ Telegram（备），高危主备都发、其余主通道失败时备通道兜底 |
| `suppression.py` | 抑制：`alert_key` → `alerts` 台账，冷却期内只发一次、恢复发一次恢复通知 |
| `notifier.py` | 闸门与投递：每 30 秒扫一遍 `notifications(state='pending')`，超 5 分钟未送达即销毁 |

事件本身由各产出方写进 `common/notifications.py`（「异常不得静默」），本包只负责**送出去**。
"""

from __future__ import annotations

from danmu_intel.notify.channels import (
    Channel,
    ChannelError,
    ChannelNotConfigured,
    ChannelSet,
    QQBotChannel,
    TelegramChannel,
    Transport,
    UrllibTransport,
    channels_from_credentials,
)
from danmu_intel.notify.config import NotifyConfig, load_notify_config, save_notify_config
from danmu_intel.notify.notifier import Outcome, Pass, deliver_once, run_loop
from danmu_intel.notify.render import KIND_LABELS, format_message
from danmu_intel.notify.suppression import Alert, alert_key, get_alert, resolve, list_alerts

__all__ = [
    "KIND_LABELS",
    "Alert",
    "Channel",
    "ChannelError",
    "ChannelNotConfigured",
    "ChannelSet",
    "NotifyConfig",
    "Outcome",
    "Pass",
    "QQBotChannel",
    "TelegramChannel",
    "Transport",
    "UrllibTransport",
    "alert_key",
    "channels_from_credentials",
    "deliver_once",
    "format_message",
    "get_alert",
    "list_alerts",
    "load_notify_config",
    "resolve",
    "run_loop",
    "save_notify_config",
]
