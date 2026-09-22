"""采集层：适配器契约 + 注册表 + 采集会话。

新增平台 = 新增一个模块 + 在 `ADAPTERS` 加一行。
"""

from __future__ import annotations

from danmu_intel.collect.adapter import Adapter, Probe, RoomKey, reconnecting
from danmu_intel.collect.huya import HuyaAdapter

ADAPTERS: dict[str, Adapter] = {
    HuyaAdapter.platform: HuyaAdapter(),
}


def get_adapter(platform: str) -> Adapter:
    try:
        return ADAPTERS[platform]
    except KeyError:
        raise ValueError(f"未注册的平台适配器：{platform}（已注册：{','.join(sorted(ADAPTERS))}）") from None


__all__ = ["ADAPTERS", "Adapter", "Probe", "RoomKey", "get_adapter", "reconnecting"]
