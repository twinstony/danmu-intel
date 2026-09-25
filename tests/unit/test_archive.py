"""归档：到期集合、压缩迁档、索引行改指向、可核验、可调取（issue #23 的机制层）。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from compression import zstd

from danmu_intel import archive
from danmu_intel.common import audit, evidence, paths
from danmu_intel.common.events import iter_events

from conftest import BASE_TS, index_segment, make_event, write_jsonl

OLD = "raw/huya/2026-03-01/660000-16.jsonl"
OLD_ARCHIVED = "archive/huya/2026-03-01/660000-16.jsonl.zst"
RECENT = "raw/huya/2026-09-22/660000-16.jsonl"
CUTOFF = date(2026, 3, 25)


def index_file(
    conn,
    data_root: Path,
    rel_path: str,
    *,
    start: int = BASE_TS,
    count: int = 5,
    events: list | None = None,
    room_id: str = "660000",
) -> str:
    """落一份原始记录 + 建最小取证链（房间行 + 采集会话行 + 索引行）。"""
    payload = events if events is not None else [
        make_event(start + i * 1000, text=f"{rel_path} 第 {i} 条") for i in range(count)
    ]
    digest = write_jsonl(data_root / rel_path, payload)
    index_segment(conn, match_id=1, rel_path=rel_path, digest=digest, events=payload, room_id=room_id)
    return digest


def row_of(conn, online_rel_path: str):
    return conn.execute(
        "SELECT * FROM danmu_segments WHERE rel_path IN (?, ?)",
        (online_rel_path, evidence.archive_rel_path(online_rel_path)),
    ).fetchone()


def test_cutoff_date_is_six_calendar_months_back():
    assert archive.cutoff_date(today=date(2026, 9, 26)) == date(2026, 3, 26)
    assert archive.cutoff_date(today=date(2026, 3, 31)) == date(2025, 9, 30)  # 日号回退到当月最后一天
    assert archive.cutoff_date(today=date(2026, 1, 15), months=1) == date(2025, 12, 15)


def test_segment_day_reads_the_collection_date():
    assert archive.segment_day(OLD) == date(2026, 3, 1)
    with pytest.raises(ValueError):
        archive.segment_day("archive/huya/2026-03-01/660000-16.jsonl.zst")


def test_plan_selects_only_expired_online_segments(conn, data_root):
    index_file(conn, data_root, OLD)
    index_file(conn, data_root, RECENT)

    plan = archive.plan(conn, cutoff=CUTOFF, data_root=data_root)
    assert [segment.rel_path for segment in plan.due] == [OLD]
    assert plan.anomalies == ()


def test_plan_flags_expired_files_that_are_not_indexed(conn, data_root):
    """超期却没进索引的文件既不归档也不删：它不在证据链上，交给人处置。"""
    write_jsonl(data_root / "raw/huya/2026-02-10/660000-09.jsonl", [make_event(BASE_TS)])

    plan = archive.plan(conn, cutoff=CUTOFF, data_root=data_root)
    assert plan.due == ()
    assert [item.rel_path for item in plan.anomalies] == ["raw/huya/2026-02-10/660000-09.jsonl"]
    assert "未封存" in plan.anomalies[0].reason


def test_plan_ignores_foreign_files_under_raw(conn, data_root):
    """`raw/` 下不是「平台/日期/文件」形状的东西（手放的杂物）不参与归档判定。"""
    stray = data_root / "raw/huya/随手放/x.jsonl"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("{}\n", encoding="utf-8")

    plan = archive.plan(conn, cutoff=CUTOFF, data_root=data_root)
    assert plan.due == () and plan.anomalies == ()


def test_archived_file_ratio_does_not_divide_by_zero():
    item = archive.ArchivedFile("raw/a/b.jsonl", "archive/a/b.jsonl.zst", 1, "x", "y", 400, 100)
    assert item.ratio == 4.0
    empty = archive.ArchivedFile("raw/a/b.jsonl", "archive/a/b.jsonl.zst", 0, "x", "y", 0, 0)
    assert empty.ratio == 0.0


def test_plan_flags_index_rows_that_are_not_segment_paths(conn, data_root):
    index_file(conn, data_root, "elsewhere/x.jsonl", events=[])

    plan = archive.plan(conn, cutoff=CUTOFF, data_root=data_root)
    assert plan.due == ()
    assert "无法判定归档期" in plan.anomalies[0].reason


def test_run_moves_the_segment_and_rewrites_the_index_row(conn, data_root):
    index_file(conn, data_root, OLD)
    expected = (data_root / OLD).read_bytes()
    result = archive.run(
        conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True, now_ms=1_700_000_000_000
    )

    assert len(result.archived) == 1 and not result.anomalies
    moved = result.archived[0]
    assert moved.online_rel_path == OLD and moved.archive_rel_path == OLD_ARCHIVED
    assert moved.archive_bytes < moved.online_bytes
    assert not (data_root / OLD).exists()

    row = row_of(conn, OLD)
    assert row["rel_path"] == OLD_ARCHIVED
    assert row["archived_at"] == 1_700_000_000_000
    assert row["archive_sha256"] == moved.archive_sha256
    assert row["msg_count"] == 5
    # 内容摘要（封存值）不变 —— 报告的溯源引用拿它比对
    assert row["sha256"] == moved.sha256

    # 归档件就是原始内容（任何 zstd 工具解得开），行号不变
    artifact = data_root / OLD_ARCHIVED
    assert zstd.decompress(artifact.read_bytes()) == expected
    assert [event.text for _, event in iter_events(artifact)] == [f"{OLD} 第 {i} 条" for i in range(5)]


def test_run_with_only_anomalies_still_leaves_a_trace(conn, data_root):
    """一个文件都没归档、但有异常：摘要说清、审计也留痕（异常不许静默）。"""
    write_jsonl(data_root / "raw/huya/2026-02-10/660000-09.jsonl", [make_event(BASE_TS)])

    result = archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)
    assert result.archived == () and result.ratio == 0.0
    assert "已归档 0 个" in result.summary() and "异常 1 项" in result.summary()
    assert audit.entries(conn, action=archive.ARCHIVE_RUN)[0].detail["anomalies"]


def test_run_records_what_range_it_archived(conn, data_root):
    index_file(conn, data_root, OLD)
    archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True, now_ms=42)

    entries = audit.entries(conn, action=archive.ARCHIVE_RUN)
    assert len(entries) == 1
    detail = entries[0].detail
    assert entries[0].actor == "归档器" and entries[0].ts == 42
    assert detail["cutoff"] == CUTOFF.isoformat()
    assert detail["archived"] == 1 and detail["due"] == 1
    assert detail["range"] == [OLD, OLD]
    assert detail["anomalies"] == []
    # 这次归档的完整范围可查：索引行的 archived_at 就是这一批的同一个时刻
    archived_rows = conn.execute(
        "SELECT rel_path FROM danmu_segments WHERE archived_at=?", (detail["archived_at"],)
    ).fetchall()
    assert [row["rel_path"] for row in archived_rows] == [OLD_ARCHIVED]


def test_run_is_idempotent_and_quiet_when_nothing_is_due(conn, data_root):
    index_file(conn, data_root, OLD)
    archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)
    again = archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)

    assert again.due == 0 and again.archived == () and again.anomalies == ()
    assert "没有到期的原始记录" in again.summary()
    assert len(audit.entries(conn, action=archive.ARCHIVE_RUN)) == 1  # 没动作就不刷审计


def test_run_keeps_the_online_file_when_it_changed_after_sealing(conn, data_root):
    """在线件与封存摘要不一致：拒绝归档（不给它背一个假封存值），在线文件留着。"""
    index_file(conn, data_root, OLD)
    (data_root / OLD).write_text("被改过\n", encoding="utf-8")

    result = archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)

    assert result.archived == ()
    assert [item.reason for item in result.anomalies] == ["在线文件与封存摘要不一致（拒绝归档）"]
    assert (data_root / OLD).exists()
    assert not (data_root / OLD_ARCHIVED).exists()
    assert row_of(conn, OLD)["archived_at"] is None


def test_run_reports_a_missing_online_file(conn, data_root):
    index_file(conn, data_root, OLD)
    (data_root / OLD).unlink()

    result = archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)
    assert result.archived == ()
    assert "在线文件缺失" in result.anomalies[0].reason


def test_run_discards_the_artifact_when_it_cannot_be_verified(conn, data_root, monkeypatch):
    """归档件对不上封存摘要（例如压缩器出错）：丢归档件、留在线件，不静默。"""
    index_file(conn, data_root, OLD)
    monkeypatch.setattr(evidence, "compress_file", lambda source, target, **kw: _bad_artifact(source, target))

    result = archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)
    assert [item.reason for item in result.anomalies] == ["归档件解压后与封存摘要不一致（已丢弃归档件）"]
    assert (data_root / OLD).exists() and not (data_root / OLD_ARCHIVED).exists()


def _bad_artifact(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(zstd.compress(b"not the same content\n"))
    return target.stat().st_size


def test_run_requires_an_independent_mount(conn, data_root):
    """「迁 NAS」的机器可查判据：归档根要么是独立挂载点，要么不存在（没挂上）。"""
    index_file(conn, data_root, OLD)
    root = paths.archive_dir(data_root=data_root)

    with pytest.raises(ValueError, match="归档根不存在"):
        archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root)

    root.mkdir(parents=True)  # 目录在，但不是挂载点：同一块盘
    with pytest.raises(ValueError, match="在同一磁盘"):
        archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root)
    assert (data_root / OLD).exists()


def test_verify_passes_on_a_fresh_archive_and_flags_damage(conn, data_root):
    index_file(conn, data_root, OLD)
    archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)
    assert archive.verify(conn, data_root=data_root) == ()

    artifact = data_root / OLD_ARCHIVED
    good = artifact.read_bytes()
    artifact.write_bytes(good[:-4] + b"\x00\x00\x00\x00")
    problems = archive.verify(conn, data_root=data_root)
    assert "归档件自身摘要不一致" in problems[0].reason

    artifact.write_bytes(good)
    artifact.unlink()
    assert "归档件缺失" in archive.verify(conn, data_root=data_root)[0].reason


def test_verify_flags_an_artifact_whose_content_changed(conn, data_root):
    index_file(conn, data_root, OLD)
    archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)

    artifact = data_root / OLD_ARCHIVED
    artifact.write_bytes(zstd.compress(b'{"ts":1}\n'))
    conn.execute(
        "UPDATE danmu_segments SET archive_sha256=? WHERE rel_path=?",
        (evidence.stored_sha256(artifact), OLD_ARCHIVED),
    )
    conn.commit()

    assert "解压后与封存摘要不一致" in archive.verify(conn, data_root=data_root)[0].reason


def test_retrieve_takes_the_segment_back_by_either_address(conn, data_root):
    index_file(conn, data_root, OLD)
    expected = (data_root / OLD).read_text(encoding="utf-8")
    archive.run(conn, actor="归档器", cutoff=CUTOFF, data_root=data_root, allow_same_disk=True)

    for address in (OLD, OLD_ARCHIVED):
        path, content = archive.retrieve(conn, address, data_root=data_root)
        assert path == data_root / OLD_ARCHIVED
        assert content.decode("utf-8") == expected


def test_retrieve_refuses_a_missing_or_tampered_segment(conn, data_root):
    index_file(conn, data_root, OLD)
    with pytest.raises(LookupError):
        archive.retrieve(conn, "raw/huya/2026-03-01/000000-00.jsonl", data_root=data_root)

    (data_root / OLD).write_text("被改过\n", encoding="utf-8")
    with pytest.raises(ValueError, match="与封存摘要不一致"):
        archive.retrieve(conn, OLD, data_root=data_root)


def test_archive_touches_only_the_segment_index_and_the_audit_log():
    """切片/统计/报告/订单/会员/审计长期不删（NFR-D-2）：归档的 SQL 只许碰这两张表。"""
    allowed = {"danmu_segments", "audit_log"}
    pattern = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_]+)", re.IGNORECASE)
    for name in ("archive.py", "common/evidence.py"):
        source = (paths.repo_root() / "src" / "danmu_intel" / name).read_text(encoding="utf-8")
        sql = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith(("import ", "from ", "with "))
        )
        touched = set(pattern.findall(sql))
        assert touched <= allowed, f"{name} 动了不该动的表：{touched - allowed}"
