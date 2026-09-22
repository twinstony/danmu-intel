"""Tars 线格式的最小实现（只覆盖虎牙弹幕链路用到的类型）。

虎牙弹幕走 Tars 编码的 WebSocket 帧。本项目**不引入 Tars 框架**（重量级、
面向 RPC），只实现本仓库真正用到的线格式子集：整数 / 布尔 / 字符串 / bytes /
结构体（读取时按字段头跳过未知字段）。

字段头：1 字节（tag < 15）或 2 字节（tag >= 15）。高 4 位是 tag，低 4 位是类型；
tag >= 15 时首字节高 4 位恒为 0xF，tag 放在第二个字节。
"""

from __future__ import annotations

import struct

EN_INT8 = 0
EN_INT16 = 1
EN_INT32 = 2
EN_INT64 = 3
EN_FLOAT = 4
EN_DOUBLE = 5
EN_STRING1 = 6
EN_STRING4 = 7
EN_MAP = 8
EN_LIST = 9
EN_STRUCT_BEGIN = 10
EN_STRUCT_END = 11
EN_ZERO = 12
EN_BYTES = 13

_INT_TYPES = {
    EN_INT8: ("!b", 1),
    EN_INT16: ("!h", 2),
    EN_INT32: ("!i", 4),
    EN_INT64: ("!q", 8),
}


class TarsError(ValueError):
    """线格式非法（截断、类型不匹配、未知类型）。"""


def _head(tag: int, vtype: int) -> bytes:
    if tag < 15:
        return struct.pack("!B", (tag << 4) | vtype)
    return struct.pack("!H", (0xF0 | vtype) << 8 | tag)


class TarsWriter:
    """编码器。整数按能容纳它的最窄类型落盘（与官方实现一致）。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    def write_int(self, tag: int, value: int) -> None:
        if value == 0:
            self._buf += _head(tag, EN_ZERO)
        elif -128 <= value <= 127:
            self._buf += _head(tag, EN_INT8) + struct.pack("!b", value)
        elif -32768 <= value <= 32767:
            self._buf += _head(tag, EN_INT16) + struct.pack("!h", value)
        elif -2147483648 <= value <= 2147483647:
            self._buf += _head(tag, EN_INT32) + struct.pack("!i", value)
        else:
            self._buf += _head(tag, EN_INT64) + struct.pack("!q", value)

    def write_bool(self, tag: int, value: bool) -> None:
        self.write_int(tag, 1 if value else 0)

    def write_string(self, tag: int, value: str) -> None:
        raw = value.encode("utf-8")
        if len(raw) <= 255:
            self._buf += _head(tag, EN_STRING1) + struct.pack("!B", len(raw)) + raw
        else:
            self._buf += _head(tag, EN_STRING4) + struct.pack("!I", len(raw)) + raw

    def write_struct(self, tag: int, body: bytes) -> None:
        """结构体：BEGIN 头 + 已编码的字段 + END 头。"""
        self._buf += _head(tag, EN_STRUCT_BEGIN) + body + _head(0, EN_STRUCT_END)

    def write_bytes(self, tag: int, value: bytes) -> None:
        self._buf += _head(tag, EN_BYTES)
        self._buf += _head(0, EN_INT8)  # bytes 的长度字段固定带 tag 0 / INT8 头
        self.write_int(0, len(value))
        self._buf += value

    def getvalue(self) -> bytes:
        return bytes(self._buf)


class TarsReader:
    """解码器。按 tag 读取字段；未请求的字段按类型跳过。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    # ---- 底层 ----------------------------------------------------------

    def _peek_head(self) -> tuple[int, int, int]:
        if self._pos >= len(self._data):
            raise TarsError("字段头越界")
        first = self._data[self._pos]
        tag = first >> 4
        vtype = first & 0x0F
        if tag < 15:
            return tag, vtype, 1
        if self._pos + 1 >= len(self._data):
            raise TarsError("两字节字段头越界")
        return self._data[self._pos + 1], vtype, 2

    def _advance(self, count: int) -> None:
        self._pos += count
        if self._pos > len(self._data):
            raise TarsError("字段越界")

    def _take(self, count: int) -> bytes:
        raw = self._data[self._pos : self._pos + count]
        if len(raw) != count:
            raise TarsError("字段越界")
        self._pos += count
        return raw

    def _value_of(self, vtype: int) -> int:
        """读取当前位置上、类型已由字段头给出的整数。"""
        if vtype == EN_ZERO:
            return 0
        if vtype not in _INT_TYPES:
            raise TarsError(f"期望整数，实际类型 {vtype}")
        fmt, size = _INT_TYPES[vtype]
        return int(struct.unpack(fmt, self._take(size))[0])

    def _skip(self, vtype: int) -> None:
        if vtype == EN_ZERO:
            return
        if vtype in _INT_TYPES:
            self._advance(_INT_TYPES[vtype][1])
        elif vtype == EN_FLOAT:
            self._advance(4)
        elif vtype == EN_DOUBLE:
            self._advance(8)
        elif vtype == EN_STRING1:
            self._advance(self._take(1)[0])
        elif vtype == EN_STRING4:
            self._advance(struct.unpack("!I", self._take(4))[0])
        elif vtype == EN_BYTES:
            self._read_head()  # 固定带的 tag 0 / INT8 头
            self._advance(self._value_of(self._read_head()[1]))
        elif vtype in (EN_LIST, EN_MAP):
            count_type = self._read_head()[1]
            size = self._value_of(count_type)
            for _ in range(size * (2 if vtype == EN_MAP else 1)):
                self._skip(self._read_head()[1])
        elif vtype == EN_STRUCT_BEGIN:
            self._skip_to_struct_end()
        else:
            raise TarsError(f"未知类型 {vtype}")

    def _read_head(self) -> tuple[int, int]:
        tag, vtype, head_len = self._peek_head()
        self._pos += head_len
        return tag, vtype

    def _skip_to_struct_end(self) -> int:
        """跳过结构体主体，返回 EN_STRUCT_END 字段头的起始位置。"""
        while True:
            head_start = self._pos
            _, vtype = self._read_head()
            if vtype == EN_STRUCT_END:
                return head_start
            self._skip(vtype)

    def _find(self, tag: int) -> int | None:
        """定位到指定 tag 的值，返回其类型；不存在则返回 None。"""
        while self._pos < len(self._data):
            cur_tag, vtype, head_len = self._peek_head()
            if vtype == EN_STRUCT_END or cur_tag > tag:
                return None
            self._pos += head_len
            if cur_tag == tag:
                return vtype
            self._skip(vtype)
        return None

    # ---- 取值 ----------------------------------------------------------

    def read_int(self, tag: int, default: int | None = None) -> int | None:
        vtype = self._find(tag)
        if vtype is None:
            return default
        return self._value_of(vtype)

    def read_string(self, tag: int, default: str | None = None) -> str | None:
        vtype = self._find(tag)
        if vtype is None:
            return default
        if vtype == EN_STRING1:
            length = self._take(1)[0]
        elif vtype == EN_STRING4:
            length = struct.unpack("!I", self._take(4))[0]
        else:
            raise TarsError(f"tag {tag} 期望字符串，实际类型 {vtype}")
        return self._take(length).decode("utf-8", errors="replace")

    def read_bytes(self, tag: int, default: bytes | None = None) -> bytes | None:
        vtype = self._find(tag)
        if vtype is None:
            return default
        if vtype != EN_BYTES:
            raise TarsError(f"tag {tag} 期望 bytes，实际类型 {vtype}")
        if self._read_head()[1] != EN_INT8:
            raise TarsError("bytes 长度字段缺少固定头")
        return self._take(self._value_of(self._read_head()[1]))

    def read_struct(self, tag: int) -> "TarsReader | None":
        vtype = self._find(tag)
        if vtype is None:
            return None
        if vtype != EN_STRUCT_BEGIN:
            raise TarsError(f"tag {tag} 期望结构体，实际类型 {vtype}")
        start = self._pos
        end = self._skip_to_struct_end()
        return TarsReader(self._data[start:end])
