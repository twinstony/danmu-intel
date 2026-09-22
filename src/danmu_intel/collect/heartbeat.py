"""进程级心跳（设计 §7.4；issue #5 §2/§3）。

一房间一子进程，supervisor 与子进程靠两件东西对话：

1. **心跳文件** `<data>/runtime/heartbeat/<platform>-<room_id>.json` —— 子进程每
   5 秒原子覆盖写一次（临时文件 + `os.replace`，读方永远看不到半截文件）；
2. **环境变量** `DANMU_INTEL_SUPERVISION` —— supervisor 把「这个房间第几次重启、
   已累计重连几次」交给子进程，子进程据此写自己的 `room_sessions` 行。

supervisor 只读心跳文件判活：文件里带 `pid`（用来识破上一轮的残留文件）与
`written_at`（用来算心跳年龄）。心跳老化超过 `HEARTBEAT_STALE_S` 即认定子进程
僵死——**网络断了子进程自己会重连，僵死才由父进程杀**。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from danmu_intel.common import paths

HEARTBEAT_INTERVAL_S = 5.0  # 子进程写心跳的周期（issue #5 §2）
HEARTBEAT_STALE_S = 15.0  # 主进程判僵死的阈值（设计 §7.4）
DISK_FREE_MIN_BYTES = 5 * 1024**3  # 磁盘可用下限（设计 §7.5：< 5GB 即报警）
HEARTBEAT_DIR = ("runtime", "heartbeat")
SUPERVISION_ENV = "DANMU_INTEL_SUPERVISION"


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """子进程当前状态的快照（心跳文件的内容）。"""

    pid: int
    session_id: int
    platform: str
    room_id: str
    state: str
    started_at: int
    last_msg_at: int | None
    msg_count: int
    reconnects: int
    restart_count: int
    written_at: int


@dataclass(frozen=True, slots=True)
class Supervision:
    """supervisor 交给子进程的接力棒（重启次数与累计重连数）。"""

    restart_count: int = 0
    reconnects: int = 0


def heartbeat_path(platform: str, room_id: str, *, data_root: Path | None = None) -> Path:
    root = data_root or paths.data_dir()
    return root.joinpath(*HEARTBEAT_DIR, f"{platform}-{room_id}.json")


def write_heartbeat(beat: Heartbeat, *, data_root: Path | None = None) -> Path:
    """原子写心跳文件（先写临时文件再 `os.replace`）。"""
    target = heartbeat_path(beat.platform, beat.room_id, data_root=data_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {field: getattr(beat, field) for field in Heartbeat.__slots__}
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_heartbeat(platform: str, room_id: str, *, data_root: Path | None = None) -> Heartbeat | None:
    """读心跳文件；文件不存在或内容残缺一律返回 `None`（不崩、不猜）。"""
    target = heartbeat_path(platform, room_id, data_root=data_root)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return Heartbeat(**payload)
    except (OSError, ValueError, TypeError):
        return None


def heartbeat_age_ms(beat: Heartbeat | None, now_ms: int, *, spawned_at_ms: int) -> int:
    """心跳年龄（毫秒）；完全没有心跳时按进程启动时刻算。"""
    return now_ms - (beat.written_at if beat else spawned_at_ms)


def free_bytes(path: Path) -> int:
    """`path` 所在文件系统的可用字节数（目录尚不存在时就近取已存在的父目录）。"""
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free


def disk_low(path: Path, *, minimum: int = DISK_FREE_MIN_BYTES) -> bool:
    """可用空间是否低于下限（设计 §7.5：< 5GB 即报警，不得静默）。"""
    return free_bytes(path) < minimum


def supervision_env(counts: Supervision) -> dict[str, str]:
    """supervisor 侧：把接力棒编码成环境变量。"""
    return {
        SUPERVISION_ENV: json.dumps(
            {"restart_count": counts.restart_count, "reconnects": counts.reconnects},
            separators=(",", ":"),
        )
    }


def supervision_state(env: Mapping[str, str] | None = None) -> Supervision:
    """子进程侧：读回接力棒。环境变量缺失即「无监督启动」（全 0）。"""
    raw = (env or os.environ).get(SUPERVISION_ENV)
    if not raw:
        return Supervision()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{SUPERVISION_ENV} 必须是 JSON 对象")
    return Supervision(
        restart_count=int(payload.get("restart_count", 0)),
        reconnects=int(payload.get("reconnects", 0)),
    )
