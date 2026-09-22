"""虎牙直播平台适配器（首发平台，ADR-0008）。

职责边界（设计 §7.1）：**只把平台原始 payload 变成统一的 `DanmuEvent`**。
其余共性逻辑（重连、落盘、建库）都在 `adapter.py` / `runner.py`。

协议来源：虎牙公开弹幕 WebSocket（`wss://cdnws.api.huya.com/`），帧用 Tars
编码（见 `tars.py`）。昵称等身份信息**不落盘**，只取平台用户 ID 做加盐哈希。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterator

import aiohttp

from danmu_intel.collect.adapter import Probe, RoomKey, reconnecting
from danmu_intel.collect.tars import TarsError, TarsReader, TarsWriter
from danmu_intel.common.events import DanmuEvent
from danmu_intel.common.identity import user_hash

PLATFORM = "huya"
WS_URL = "wss://cdnws.api.huya.com/"
MOBILE_PAGE = "https://m.huya.com/{room_id}"
UA = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/79.0.3945.88 Mobile Safari/537.36"
)
HTTP_TIMEOUT_S = 15.0
HEARTBEAT_INTERVAL_S = 60.0
# 协议固定的心跳帧（每个心跳周期原样发送）
HEARTBEAT_FRAME = (
    b"\x00\x03\x1d\x00\x00\x69\x00\x00\x00\x69\x10\x03\x2c\x3c\x4c\x56\x08\x6f\x6e\x6c"
    b"\x69\x6e\x65\x75\x69\x66\x0f\x4f\x6e\x55\x73\x65\x72\x48\x65\x61\x72\x74\x42\x65"
    b"\x61\x74\x7d\x00\x00\x3c\x08\x00\x01\x06\x04\x74\x52\x65\x71\x1d\x00\x00\x2f\x0a"
    b"\x0a\x0c\x16\x00\x26\x00\x36\x07\x61\x64\x72\x5f\x77\x61\x70\x46\x00\x0b\x12\x03"
    b"\xae\xf0\x0f\x22\x03\xae\xf0\x0f\x3c\x42\x6d\x52\x02\x60\x5c\x60\x01\x7c\x82\x00"
    b"\x0b\xb0\x1f\x9c\xac\x0b\x8c\x98\x0c\xa8\x0c "
)
# 帧字段：0=消息类型（7=弹幕广播），1=广播体；广播体：1=URI（1400=弹幕），2=负载
MSG_TYPE_BROADCAST = 7
URI_DANMAKU = 1400
LIVE_STATUS_ON = "2"  # 移动页 eLiveStatus：2=直播中

# 移动页里这些字段首次出现即为正确值（页面后段还有一处全 0 的占位）
_PAGE_FIELDS = ("lYyid", "lChannelId", "lSubChannelId", "lProfileRoom")
_NUM_RE = r'"%s":\s*"?(\d+)"?'
_STR_RE = r'"%s":\s*"((?:\\.|[^"\\])*)"'
_ROOM_ID_RE = re.compile(r"[A-Za-z0-9_]{1,32}\Z")


@dataclass(frozen=True, slots=True)
class RawDanmu:
    """一条平台原始弹幕（只留本仓库要用的字段，昵称不进内存）。"""

    uid: str
    text: str


@dataclass(frozen=True, slots=True)
class ConnectParams:
    room_id: str
    uid: int
    channel_id: int
    sub_channel_id: int


def parse_room_id(url: str) -> str:
    """从直播间链接（或裸房间号）里取出房间号。"""
    text = url.strip()
    if "huya.com/" in text:
        text = text.split("huya.com/")[-1].split("/")[0].split("?")[0]
    text = text.strip("/")
    if not _ROOM_ID_RE.match(text):
        raise ValueError(f"无法从 {url!r} 解析虎牙房间号")
    return text


def decode_frame(frame: bytes) -> list[RawDanmu]:
    """把一帧原始数据解成弹幕列表。**任何非法 payload 一律返回空列表**（不崩）。"""
    try:
        outer = TarsReader(frame)
        if outer.read_int(0) != MSG_TYPE_BROADCAST:
            return []
        broadcast = outer.read_bytes(1)
        if not broadcast:
            return []
        body = TarsReader(broadcast)
        if body.read_int(1) != URI_DANMAKU:
            return []
        payload_bytes = body.read_bytes(2)
        if not payload_bytes:
            return []
        payload = TarsReader(payload_bytes)
        user = payload.read_struct(0)
        uid = str(user.read_int(0, 0) or 0) if user is not None else "0"
        text = payload.read_string(3, "") or ""
    except (TarsError, IndexError, ValueError, TypeError):
        return []
    if not text.strip():
        return []
    return [RawDanmu(uid=uid, text=text)]


def register_payload(params: ConnectParams) -> bytes:
    """构造进房注册帧。"""
    body = TarsWriter()
    body.write_int(0, params.uid)
    body.write_bool(1, True)  # 匿名观看
    body.write_string(2, "")
    body.write_string(3, "")
    body.write_int(4, params.channel_id)
    body.write_int(5, params.sub_channel_id)
    body.write_int(6, 0)
    body.write_int(7, 0)
    frame = TarsWriter()
    frame.write_int(0, 1)
    frame.write_bytes(1, body.getvalue())
    return frame.getvalue()


def build_danmaku_frame(uid: str, nick: str, text: str) -> bytes:
    """按虎牙协议构造一帧弹幕 —— 协议自检与 fixture 生成用。"""
    user = TarsWriter()
    user.write_int(0, int(uid))
    user.write_string(2, nick)
    payload = TarsWriter()
    payload.write_struct(0, user.getvalue())
    payload.write_string(3, text)
    broadcast = TarsWriter()
    broadcast.write_int(0, 3)
    broadcast.write_int(1, URI_DANMAKU)
    broadcast.write_bytes(2, payload.getvalue())
    frame = TarsWriter()
    frame.write_int(0, MSG_TYPE_BROADCAST)
    frame.write_bytes(1, broadcast.getvalue())
    return frame.getvalue()


def _first_match(pattern: str, page: str) -> str | None:
    match = re.search(pattern, page)
    return match.group(1) if match else None


def _json_string(page: str, key: str) -> str | None:
    raw = _first_match(_STR_RE % re.escape(key), page)
    if raw is None:
        return None
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def parse_page(page: str) -> tuple[ConnectParams, Probe]:
    """解析虎牙公开移动页（纯函数，便于离线测试）。"""
    fields = {name: _first_match(_NUM_RE % name, page) for name in _PAGE_FIELDS}
    missing = [name for name in ("lYyid", "lChannelId", "lProfileRoom") if fields[name] is None]
    if missing:
        raise ValueError(f"虎牙房间页缺少字段：{','.join(missing)}（房间未开播、不存在，或页面协议变更）")
    params = ConnectParams(
        room_id=fields["lProfileRoom"] or "",
        uid=int(fields["lYyid"] or 0),
        channel_id=int(fields["lChannelId"] or 0),
        sub_channel_id=int(fields["lSubChannelId"] or fields["lChannelId"] or 0),
    )
    probe = Probe(
        is_live=_first_match(r'"eLiveStatus":\s*(\d+)', page) == LIVE_STATUS_ON,
        streamer=_json_string(page, "sNick"),
        title=_json_string(page, "sRoomName"),
        game=_json_string(page, "sGameFullName"),
    )
    return params, probe


async def fetch_page(room_id: str) -> str:
    """取虎牙公开移动页（本适配器唯一的 HTTP 入口）。"""
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as session:
        async with session.get(MOBILE_PAGE.format(room_id=room_id)) as response:
            response.raise_for_status()
            return await response.text()


class LiveTransport:
    """真实链路：取页面参数 → 连 WebSocket → 注册 → 心跳 → 吐原始帧。

    这是本适配器**唯一**的网络缝；测试用假 transport 回放录制帧（不连真实直播）。
    """

    def __init__(self, *, dump_frames: Path | None = None) -> None:
        self._dump_frames = dump_frames

    async def frames(self, room_id: str) -> AsyncIterator[bytes]:
        params, _ = parse_page(await fetch_page(room_id))
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=HTTP_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as session:
            async with session.ws_connect(WS_URL, heartbeat=None) as ws:
                await ws.send_bytes(register_payload(params))
                heartbeat = asyncio.create_task(self._heartbeat(ws))
                try:
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            self._dump(message.data)
                            yield message.data
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            return
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            with contextlib.suppress(Exception):
                await ws.send_bytes(HEARTBEAT_FRAME)

    def _dump(self, frame: bytes) -> None:
        if self._dump_frames is None:
            return
        self._dump_frames.parent.mkdir(parents=True, exist_ok=True)
        with self._dump_frames.open("a", encoding="utf-8") as handle:
            handle.write(frame.hex() + "\n")


class HuyaAdapter:
    """虎牙适配器，实现 `adapter.Adapter` 契约。"""

    platform = PLATFORM

    def __init__(self, *, transport: LiveTransport | None = None) -> None:
        self._transport = transport or LiveTransport()

    def parse_room(self, url: str) -> RoomKey:
        room_id = parse_room_id(url)
        return RoomKey(platform=PLATFORM, room_id=room_id, url=f"https://www.huya.com/{room_id}")

    async def probe(self, room: RoomKey) -> Probe:
        _, probe = parse_page(await fetch_page(room.room_id))
        return probe

    async def stream(self, room: RoomKey) -> AsyncIterator[DanmuEvent]:
        async for event in reconnecting(self._connect, room):
            yield event

    async def _connect(self, room: RoomKey) -> AsyncIterator[DanmuEvent]:
        # 虎牙帧不带可信发送时间，用本地接收时间（毫秒）并保证单调不减
        last_ts = 0
        async for frame in self._transport.frames(room.room_id):
            for raw in decode_frame(frame):
                last_ts = max(last_ts, int(time.time() * 1000))
                yield DanmuEvent(
                    ts=last_ts,
                    platform=PLATFORM,
                    room_id=room.room_id,
                    user_hash=user_hash(PLATFORM, raw.uid),
                    text=raw.text,
                    extra={},
                )


def iter_recorded_frames(path: Path) -> Iterator[bytes]:
    """读取录制帧文件（每行一帧 hex）——供 fixture 生成与回放使用。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            yield bytes.fromhex(line)
