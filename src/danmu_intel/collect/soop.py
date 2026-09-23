"""SOOP（原 AfreecaTV）直播平台适配器（首发第二平台，ADR-0008）。

职责边界（设计 §7.1）：**只把平台原始 payload 变成统一的 `DanmuEvent`**。
其余共性逻辑（重连、落盘、建库）都在 `adapter.py` / `runner.py`，本模块不改它们。

协议来源（SOOP 公开链路，与官方播放器同构）：

1. `POST https://live.sooplive.com/afreeca/player_live_api.php?bjid=<bj>` → `CHANNEL`：
   `CHIP`（聊天 IP）/ `CHPT`（聊天端口，wss 用 +1）/ `CHATNO`（频道号）/
   `BSTATUS`（`BROADING` = 直播中）。
2. 聊天域名由 IP 四位转十六进制得到，`wss://chat-<HEX>.sooplive.com:<CHPT+1>/Websocket/<bj>`，
   WebSocket 子协议 `chat`。
3. 二进制包：头 50 字节 = `0x1D 0x09` + 服务码(4 位 ASCII) + 长度(6 位 ASCII) + `00` + UUID(36)；
   体 = 字段以 `0x0C` 分隔（首尾各一个）。登录(1) → 进频道(2) → 弹幕(5)，每 60 秒保活(0)。

昵称等身份信息**不落盘**，只取平台用户 ID 做加盐哈希。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Iterator

import aiohttp

from danmu_intel.collect.adapter import Probe, RoomKey, reconnecting
from danmu_intel.common.events import DanmuEvent
from danmu_intel.common.identity import user_hash

PLATFORM = "soop"
PLAY_URL = "https://play.sooplive.com/{bj_id}"
LIVE_API = "https://live.sooplive.com/afreeca/player_live_api.php?bjid={bj_id}"
WEBSOCKET_PATH = "/Websocket/{bj_id}"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HTTP_TIMEOUT_S = 15.0
KEEPALIVE_INTERVAL_S = 60.0  # 协议要求：每 60 秒一个保活包
LIVE_STATUS = "BROADING"

# 二进制包结构（SOOP 聊天协议）
SEP = b"\x0c"  # 字段分隔符
HEAD_PREFIX = b"\x1d\t"  # 包头固定两字节
ZERO_UUID = b"00000000-0000-0000-0000-000000000000"
HEADER_LEN = 50  # 2 + 服务码 4 + 长度 6 + 2 + UUID 36
SERVICE_SLICE = slice(2, 6)
RETCODE_SLICE = slice(12, 14)
SVC_KEEPALIVE = 0
SVC_LOGIN = 1
SVC_JOINCH = 2
SVC_CHATMESG = 5
GUEST_FLAG = 16  # 游客登录标志
CHAT_MIN_FIELDS = 6  # 弹幕包字段数下限；[0]=消息、[1]=用户 ID、[5]=昵称
LOGIN_ACK = (SVC_LOGIN, 0)
CHAT_SUBPROTOCOL = "chat"

_BJ_ID_RE = re.compile(r"[A-Za-z0-9_]{2,32}\Z")


@dataclass(frozen=True, slots=True)
class RawDanmu:
    """一条平台原始弹幕（只留本仓库要用的字段，昵称不进内存）。"""

    uid: str
    text: str


@dataclass(frozen=True, slots=True)
class RoomInfo:
    """房间信息（`player_live_api` 的 `CHANNEL` 报价 —— 探测与连聊天都要它）。"""

    bj_id: str
    broad_no: str
    chat_no: int
    chip: str
    chpt: int
    is_live: bool
    streamer: str | None
    title: str | None
    game: str | None


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def parse_bj_id(url: str) -> str:
    """从直播间链接（或裸主播 ID）里取出主播 ID（`bj_id`）。

    SOOP 的房间标识是**主播频道**（`play.sooplive.com/<bj_id>/<broad_no>` 里的
    前一段）：`broad_no` 每场直播都变，频道才是稳定的采集目标（`rooms.room_id`）。
    """
    text = url.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in text.split("/") if part]
    while parts and "." in parts[0]:  # 去掉域名段（裸 ID 不含点）
        parts.pop(0)
    if not parts or not _BJ_ID_RE.match(parts[0]):
        raise ValueError(f"无法从 {url!r} 解析 SOOP 主播 ID")
    return parts[0]


def parse_room_info(payload: dict) -> RoomInfo:
    """解析 `player_live_api` 的返回体（纯函数，便于离线测试）。"""
    channel = payload.get("CHANNEL")
    if not isinstance(channel, dict):
        raise ValueError("SOOP 房间信息缺少 CHANNEL 字段（接口协议可能已变更）")
    tags = channel.get("CATEGORY_TAGS")
    game = tags[0] if isinstance(tags, list) and tags else None
    return RoomInfo(
        bj_id=str(channel.get("BJID") or ""),
        broad_no=str(channel.get("BNO") or ""),
        chat_no=int(channel.get("CHATNO") or 0),
        chip=str(channel.get("CHIP") or ""),
        chpt=int(channel.get("CHPT") or 0),
        is_live=channel.get("BSTATUS") == LIVE_STATUS,
        streamer=_text(channel.get("BJNICK")),
        title=_text(channel.get("TITLE")),
        game=_text(game),
    )


def chat_url(info: RoomInfo) -> str:
    """聊天服务器地址：IP 四位转十六进制 + 端口 +1（SOOP 聊天协议）。"""
    octets = info.chip.split(".")
    if len(octets) != 4:
        raise ValueError(f"SOOP 聊天服务器地址非法：{info.chip!r}（房间 {info.bj_id} 可能未开播）")
    hex_ip = "".join(f"{int(octet):02X}" for octet in octets)
    return f"wss://chat-{hex_ip}.sooplive.com:{info.chpt + 1}{WEBSOCKET_PATH.format(bj_id=info.bj_id)}"


def make_packet(service_code: int, body: bytes) -> bytes:
    """按 SOOP 聊天协议封包：50 字节头 + 体。"""
    header = (
        HEAD_PREFIX
        + str(service_code).zfill(4).encode()
        + str(len(body)).zfill(6).encode()
        + b"00"
        + ZERO_UUID
    )
    return header + body


def packet_header(packet: bytes) -> tuple[int, int]:
    """取包的 `(服务码, 返回码)`；长度不足即抛 `ValueError`。"""
    if len(packet) < HEADER_LEN:
        raise ValueError("SOOP 聊天包长度不足")
    return int(packet[SERVICE_SLICE]), int(packet[RETCODE_SLICE])


def login_body() -> bytes:
    """登录包体：匿名游客（ticket 与昵称都空，标志 = 游客）。"""
    fields = ("", "", str(GUEST_FLAG))
    return SEP + SEP.join(field.encode() for field in fields) + SEP


def join_body(chat_no: int, *, log: str = "") -> bytes:
    """进频道包体：频道号 + 粉丝券 + 标记 + 扩展串 + 日志串（与官方播放器同构）。"""
    fields = (str(chat_no), "", "0", "", log)
    return SEP + SEP.join(field.encode() for field in fields) + SEP


def build_log_string(quality: str = "HD", geo_cc: str = "HK", geo_rc: str = "01") -> str:
    """复刻官方播放器进频道时带的日志串（协议要求带全字段，漏字段会被拒）。"""
    pairs = {
        "set_bps": "8000",
        "view_bps": "8000",
        "quality": quality,
        "uuid": ZERO_UUID.decode(),
        "geo_cc": geo_cc,
        "geo_rc": geo_rc,
        "svc_lang": "ko_KR",
        "subscribe": "0",
        "lowlatency": "0",
    }
    log = "log\x11"
    log += "".join(f"\x06&\x06{key}\x06=\x06{value}" for key, value in pairs.items())
    log += "\x12"
    for key, value in {
        "pwd": "",
        "auth_info": "",
        "pver": "2",
        "access_system": "html5",
        "nation_lang": "ko_KR",
    }.items():
        log += f"{key}\x11{value}\x12"
    return log


def keepalive_body() -> bytes:
    return SEP


def _decode_utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _split_fields(body: bytes) -> list[str]:
    """体 = `0x0C` + 字段（`0x0C` 分隔）+ `0x0C`；不符合结构的体一律当空。"""
    if len(body) < 2 or not body.startswith(SEP) or not body.endswith(SEP):
        return []
    return [_decode_utf8(part) for part in body[1:-1].split(SEP)]


def decode_message(packet: bytes) -> list[RawDanmu]:
    """把一条聊天包解成弹幕列表。**任何非法/无关 payload 一律返回空列表**（不崩）。"""
    if len(packet) <= HEADER_LEN:
        return []
    try:
        if packet_header(packet)[0] != SVC_CHATMESG:
            return []
        fields = _split_fields(packet[HEADER_LEN:])
        if len(fields) < CHAT_MIN_FIELDS:
            return []
        uid = fields[1].strip()
        text = fields[0].replace("\r", "").strip()
    except (ValueError, IndexError, TypeError):
        return []
    if not text:
        return []
    return [RawDanmu(uid=uid, text=text)]


def build_chat_packet(uid: str, text: str, *, nickname: str = "样例用户甲") -> bytes:
    """按 SOOP 聊天协议构造一包弹幕 —— 协议自检与 fixture 生成用。"""
    fields = (text, uid, "", "", "", nickname)
    body = SEP + SEP.join(field.encode("utf-8") for field in fields) + SEP
    return make_packet(SVC_CHATMESG, body)


async def fetch_room_info(bj_id: str) -> RoomInfo:
    """取房间信息（本适配器唯一的 HTTP 入口）。`bno=0` = 当前一场直播。"""
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    headers = {"User-Agent": UA, "Referer": PLAY_URL.format(bj_id=bj_id)}
    data = {
        "bid": bj_id,
        "bno": "0",
        "type": "LIVE",
        "pwd": "",
        "player_type": "html5",
        "stream_type": "common",
        "quality": "HD",
        "mode": "live",
        "from_api": "",
        "is_revive": "0",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        url = LIVE_API.format(bj_id=bj_id)
        async with session.post(url, data=data) as response:
            response.raise_for_status()
            return parse_room_info(await response.json(content_type=None))


class LiveTransport:
    """真实链路：取房间信息 → 连聊天服务器 → 登录 → 进频道 → 吐原始包。

    这是本适配器**唯一**的网络缝；测试用假 transport 回放录制包（不连真实直播）。
    """

    def __init__(self, *, dump_packets: Path | None = None) -> None:
        self._dump_packets = dump_packets

    async def frames(self, bj_id: str) -> AsyncIterator[bytes]:
        info = await fetch_room_info(bj_id)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=HTTP_TIMEOUT_S)
        headers = {"User-Agent": UA, "Origin": PLAY_URL.format(bj_id=bj_id)}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.ws_connect(
                chat_url(info), protocols=(CHAT_SUBPROTOCOL,), heartbeat=None
            ) as ws:
                await ws.send_bytes(make_packet(SVC_LOGIN, login_body()))
                keepalive = asyncio.create_task(self._keepalive(ws))
                joined = False
                try:
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.BINARY:
                            if message.type in (
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                return
                            continue
                        packet = message.data
                        self._dump(packet)
                        if not joined and self._ack(packet):
                            await ws.send_bytes(make_packet(SVC_JOINCH, join_body(info.chat_no)))
                            joined = True
                        yield packet
                finally:
                    keepalive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await keepalive

    @staticmethod
    def _ack(packet: bytes) -> bool:
        """登录应答（服务码 + 返回码 0）——收到它才能进频道。"""
        try:
            return packet_header(packet) == LOGIN_ACK
        except ValueError:
            return False

    async def _keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            with contextlib.suppress(Exception):
                await ws.send_bytes(make_packet(SVC_KEEPALIVE, keepalive_body()))

    def _dump(self, packet: bytes) -> None:
        if self._dump_packets is None:
            return
        self._dump_packets.parent.mkdir(parents=True, exist_ok=True)
        with self._dump_packets.open("a", encoding="utf-8") as handle:
            handle.write(packet.hex() + "\n")


class SoopAdapter:
    """SOOP 适配器，实现 `adapter.Adapter` 契约。"""

    platform = PLATFORM

    def __init__(self, *, transport: LiveTransport | None = None) -> None:
        self._transport = transport or LiveTransport()

    def parse_room(self, url: str) -> RoomKey:
        bj_id = parse_bj_id(url)
        return RoomKey(platform=PLATFORM, room_id=bj_id, url=PLAY_URL.format(bj_id=bj_id))

    async def probe(self, room: RoomKey) -> Probe:
        info = await fetch_room_info(room.room_id)
        return Probe(
            is_live=info.is_live,
            streamer=info.streamer,
            title=info.title,
            game=info.game,
        )

    async def stream(
        self, room: RoomKey, *, on_reconnect: Callable[[str], None] | None = None
    ) -> AsyncIterator[DanmuEvent]:
        async for event in reconnecting(self._connect, room, on_reconnect=on_reconnect):
            yield event

    async def _connect(self, room: RoomKey) -> AsyncIterator[DanmuEvent]:
        # SOOP 包不带可信发送时间，用本地接收时间（毫秒）并保证单调不减
        last_ts = 0
        async for packet in self._transport.frames(room.room_id):
            for raw in decode_message(packet):
                last_ts = max(last_ts, int(time.time() * 1000))
                yield DanmuEvent(
                    ts=last_ts,
                    platform=PLATFORM,
                    room_id=room.room_id,
                    user_hash=user_hash(PLATFORM, raw.uid),
                    text=raw.text,
                    extra={},
                )


def iter_recorded_packets(path: Path) -> Iterator[bytes]:
    """读取录制包文件（每行一包 hex）——供 fixture 生成与回放使用。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            yield bytes.fromhex(line)
