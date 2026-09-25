"""证据层：在线 JSONL 与归档件（`.jsonl.zst`）的地址互换、透明读取与摘要口径。

对应 issue #23 的「归档后可调取、可核验（校验和可用）」在字节层的部分：
归档件解压后内容与行号必须逐字节相同，两个摘要口径不能混。
"""

from __future__ import annotations

import hashlib

import pytest
from compression import zstd

from danmu_intel.common import evidence
from danmu_intel.common.events import count_lines, iter_events
from danmu_intel.common.sources import SourceRef, evidence_key, make_ref, verify

ONLINE = "raw/huya/2026-03-01/660000-16.jsonl"
ARCHIVED = "archive/huya/2026-03-01/660000-16.jsonl.zst"
BODY = "".join(
    f'{{"ts":{i},"platform":"huya","room_id":"660000","match_id":1,'
    f'"user_hash":"u{i}","text":"弹幕 {i}","extra":{{}}}}\n'
    for i in range(1, 6)
)


def write_online(data_root, rel_path: str = ONLINE, body: str = BODY):
    path = data_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_archive_rel_path_is_pure_and_idempotent():
    assert evidence.archive_rel_path(ONLINE) == ARCHIVED
    assert evidence.archive_rel_path(ARCHIVED) == ARCHIVED
    assert evidence.online_rel_path(ARCHIVED) == ONLINE
    assert evidence.online_rel_path(ONLINE) == ONLINE
    assert evidence.is_archive(ARCHIVED) and not evidence.is_archive(ONLINE)


@pytest.mark.parametrize("bad", ["archive/huya/x.jsonl", "raw/huya/2026-03-01/x.txt", "x.jsonl"])
def test_archive_rel_path_rejects_non_segments(bad):
    with pytest.raises(ValueError):
        evidence.archive_rel_path(bad)


def test_online_rel_path_rejects_foreign_archive_path():
    """归档件地址必须真的在 `archive/` 下：不然「改回在线地址」会凭空造出一个路径。"""
    with pytest.raises(ValueError):
        evidence.online_rel_path("elsewhere/x.jsonl.zst")
    # 非归档路径原样返回（幂等），不做形状校验 —— 校验只发生在 `.zst` 上
    assert evidence.online_rel_path("archive/x.txt") == "archive/x.txt"


def test_locate_prefers_the_given_address_then_the_other_one(data_root):
    online = write_online(data_root)
    assert evidence.locate(ONLINE, data_root=data_root) == online

    archived = data_root / ARCHIVED
    evidence.compress_file(online, archived)
    online.unlink()
    # 报告里冻结的是在线地址，证据已归档 → 找到归档件
    assert evidence.locate(ONLINE, data_root=data_root) == archived
    assert evidence.locate(ARCHIVED, data_root=data_root) == archived


def test_locate_returns_the_given_path_when_nothing_exists(data_root):
    """两个位置都没有时返回给定地址，好让上层报出调用方提到的那个路径。"""
    assert evidence.locate(ONLINE, data_root=data_root) == data_root / ONLINE


def test_compress_roundtrip_keeps_content_lines_and_hashes(data_root):
    online = write_online(data_root)
    archived = data_root / ARCHIVED
    size = evidence.compress_file(online, archived)

    assert size == archived.stat().st_size and size > 0
    assert archived.read_bytes().startswith(b"\x28\xb5\x2f\xfd")  # zstd 魔数：任何 zstd 工具都解得开
    assert evidence.read_bytes(archived) == BODY.encode("utf-8")
    assert evidence.content_sha256(archived) == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert evidence.stored_sha256(archived) != evidence.content_sha256(archived)
    assert evidence.line_range_sha256(archived, 2, 4) == evidence.line_range_sha256(online, 2, 4)


def test_archived_segment_reads_like_the_online_one(data_root):
    online = write_online(data_root)
    archived = data_root / ARCHIVED
    evidence.compress_file(online, archived)

    assert count_lines(archived) == count_lines(online) == 5
    assert [line_no for line_no, _ in iter_events(archived)] == [1, 2, 3, 4, 5]
    assert [event.text for _, event in iter_events(archived)] == [f"弹幕 {i}" for i in range(1, 6)]


def test_line_range_sha256_rejects_bad_range(data_root):
    path = write_online(data_root)
    with pytest.raises(ValueError):
        evidence.line_range_sha256(path, 0, 1)
    with pytest.raises(ValueError):
        evidence.line_range_sha256(path, 1, 99)


def test_compress_file_replaces_an_existing_artifact_atomically(data_root):
    """重复归档（上一次跑到一半留下的件）直接换掉，不留 `.part`。"""
    online = write_online(data_root)
    archived = data_root / ARCHIVED
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_bytes(b"stale")
    evidence.compress_file(online, archived)
    assert evidence.read_bytes(archived) == BODY.encode("utf-8")
    assert not (archived.with_name(archived.name + ".part")).exists()


def test_compress_file_leaves_no_part_file_when_the_source_is_missing(data_root):
    archived = data_root / ARCHIVED
    with pytest.raises(OSError):
        evidence.compress_file(data_root / ONLINE, archived)
    assert not archived.exists()
    assert not (archived.with_name(archived.name + ".part")).exists()


def test_source_ref_verifies_after_archiving(data_root):
    """归档**不改**引用语义：同一个 `SourceRef`（在线地址 + 行范围 + SHA256）照样复核得过。"""
    online = write_online(data_root)
    ref = make_ref(ONLINE, 2, 3, data_root=data_root)
    evidence.compress_file(online, data_root / ARCHIVED)
    online.unlink()

    assert verify(ref, data_root=data_root)
    assert evidence_key(ref.rel_path) == ONLINE
    # 内容被改过（归档件被换掉）→ 复核不过
    tampered = data_root / ARCHIVED
    tampered.write_bytes(zstd.compress(b"x\n"))
    assert not verify(ref, data_root=data_root)


def test_verify_reports_false_for_a_missing_segment(data_root):
    assert not verify(SourceRef(ONLINE, 1, 1, "0" * 64), data_root=data_root)
