"""Tars 线格式的最小编解码测试。

期望字节按线格式规范手写（不调用编码器生成期望值），因此编码器与解码器
互为独立校验。
"""

from __future__ import annotations

import struct

import pytest

from danmu_intel.collect.tars import TarsError, TarsReader, TarsWriter


def test_int_types_use_narrowest_encoding():
    writer = TarsWriter()
    writer.write_int(0, 0)
    writer.write_int(1, 7)
    writer.write_int(2, 300)
    writer.write_int(3, 100_000)
    writer.write_int(4, 5_000_000_000)
    assert writer.getvalue() == bytes(
        [
            0x0C,  # tag0 ZERO
            0x10, 0x07,  # tag1 INT8 = 7
            0x21, 0x01, 0x2C,  # tag2 INT16 = 300
            0x32, 0x00, 0x01, 0x86, 0xA0,  # tag3 INT32 = 100000
            0x43, 0x00, 0x00, 0x00, 0x01, 0x2A, 0x05, 0xF2, 0x00,  # tag4 INT64
        ]
    )
    reader = TarsReader(writer.getvalue())
    assert [reader.read_int(tag) for tag in range(5)] == [0, 7, 300, 100_000, 5_000_000_000]


def test_bool_and_negative_int():
    writer = TarsWriter()
    writer.write_bool(0, True)
    writer.write_bool(1, False)
    writer.write_int(2, -5)
    assert writer.getvalue() == bytes([0x00, 0x01, 0x1C, 0x20, 0xFB])
    reader = TarsReader(writer.getvalue())
    assert reader.read_int(0) == 1
    assert reader.read_int(1) == 0
    assert reader.read_int(2) == -5


def test_two_byte_head_for_large_tag():
    writer = TarsWriter()
    writer.write_int(20, 3)
    raw = writer.getvalue()
    assert raw[0] == 0xF0
    assert raw[1] == 20
    assert TarsReader(raw).read_int(20) == 3


def test_write_read_string_and_bytes():
    writer = TarsWriter()
    writer.write_string(0, "短")
    writer.write_string(1, "长" * 300)
    writer.write_bytes(2, b"\x00\x01\x02")
    raw = writer.getvalue()
    assert raw[0] == 0x06  # tag0 STRING1
    assert raw[1] == 3  # "短" 的 UTF-8 长度
    reader = TarsReader(raw)
    assert reader.read_string(0) == "短"
    assert reader.read_string(1) == "长" * 300
    assert reader.read_bytes(2) == b"\x00\x01\x02"
    assert reader.read_int(9, 42) == 42
    assert reader.read_string(9) is None
    assert reader.read_bytes(9) is None
    assert reader.read_struct(9) is None


def test_bytes_length_uses_nested_head():
    writer = TarsWriter()
    writer.write_bytes(0, b"x" * 300)
    raw = writer.getvalue()
    assert raw[0] == 0x0D  # tag0 BYTES
    assert raw[1] == 0x00  # 固定头（tag0 INT8）
    assert raw[2] == 0x01  # 长度字段的字段头（tag0 INT16）
    assert TarsReader(raw).read_bytes(0) == b"x" * 300


def test_struct_writing_and_reading():
    inner = TarsWriter()
    inner.write_int(0, 99)
    inner.write_string(2, "昵称")
    outer = TarsWriter()
    outer.write_int(0, 1)
    outer.write_struct(1, inner.getvalue())
    outer.write_string(2, "在结构体之后")
    reader = TarsReader(outer.getvalue())
    assert reader.read_int(0) == 1
    struct = reader.read_struct(1)
    assert struct is not None
    assert struct.read_int(0) == 99
    assert struct.read_string(2) == "昵称"
    assert reader.read_string(2) == "在结构体之后"


def test_skipping_unknown_fields_inside_struct():
    inner = TarsWriter()
    inner.write_string(0, "跳过我")
    nested = TarsWriter()
    nested.write_int(0, 2)
    inner.write_struct(1, nested.getvalue())
    inner.write_int(2, 7)
    outer = TarsWriter()
    outer.write_struct(0, inner.getvalue())
    outer.write_string(1, "尾字段")
    parsed = TarsReader(outer.getvalue())
    body = parsed.read_struct(0)
    assert body.read_int(2) == 7
    assert parsed.read_string(1) == "尾字段"


def test_list_and_map_skipping():
    # tag0 LIST(count=2, 元素 5 与 6) 之后跟 tag1 INT8=15
    raw = bytes(
        [
            0x09, 0x00, 0x02,  # LIST 头 + count
            0x02, 0x00, 0x00, 0x00, 0x05,  # 元素 1：INT32
            0x02, 0x00, 0x00, 0x00, 0x06,  # 元素 2
            0x10, 0x0F,  # tag1 INT8 = 15
        ]
    )
    assert TarsReader(raw).read_int(1) == 15

    # tag0 MAP(count=1, k=1, v=2) 之后跟 tag1 INT8=15
    # 注意：值 1 / 2 的编码是「tag0 INT8 头 + 值」，不是 0x0F
    map_raw = bytes([0x08, 0x00, 0x01, 0x00, 0x01, 0x00, 0x02, 0x10, 0x0F])
    assert TarsReader(map_raw).read_int(1) == 15


def test_float_and_double_skipping():
    # tag0 FLOAT + tag1 DOUBLE + tag2 INT8
    raw = (
        bytes([0x04]) + struct.pack("!f", 1.0)
        + bytes([0x15]) + struct.pack("!d", 2.0)
        + bytes([0x20, 0x0F])
    )
    assert TarsReader(raw).read_int(2) == 15


def test_missing_tag_returns_default_or_none():
    reader = TarsReader(bytes([0x0C]))
    assert reader.read_bytes(1) is None
    assert reader.read_int(5, 3) == 3


def test_unknown_type_raises():
    frame = bytes([0x0E, 0x00, 0x00])  # tag0 vtype 14（无定义）
    with pytest.raises(TarsError, match="未知类型"):
        TarsReader(frame).read_int(1)


@pytest.mark.parametrize(
    ("raw", "tag"),
    [
        (b"\x1d", 1),  # BYTES 头后直接截断
        (b"\x1d\x00\x0f", 1),  # 长度声明 15 字节但数据不足
        (b"\x1d\x00\x01\x00\x05ab", 1),  # 同上，长度用 INT16 承载
        (b"\x1d\x0f\x00", 1),  # 固定头类型非法（不是 INT8）
        (b"\x1f", 1),  # tag >= 15 但第二字节缺失
    ],
)
def test_truncated_bytes_raise(raw, tag):
    with pytest.raises(TarsError):
        TarsReader(raw).read_bytes(tag)


@pytest.mark.parametrize(
    ("raw", "tag"),
    [(b"\x0d\x00", 0), (b"\x00\x01", 0), (b"\x1d\x00\x01\x02", 1)],
)
def test_type_mismatch_raises(raw, tag):
    """把整数当字符串读必须报错，而不是静默返回垃圾值。"""
    with pytest.raises(TarsError):
        TarsReader(raw).read_string(tag)


def test_int_read_rejects_string_type():
    with pytest.raises(TarsError):
        TarsReader(bytes([0x06, 0x01, 0x41])).read_int(0)


def test_struct_read_rejects_non_struct():
    with pytest.raises(TarsError):
        TarsReader(bytes([0x0C])).read_struct(0)
