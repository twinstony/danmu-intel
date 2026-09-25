"""命令行：`archive`（试运行 / 归档 / 复核 / 取回）—— 退出码与输出要能当 cron 的眼睛。"""

from __future__ import annotations

from datetime import date

from danmu_intel import archive
from danmu_intel.cli import main

from test_archive import CUTOFF, OLD, OLD_ARCHIVED, index_segment


def test_archive_dry_run_lists_what_would_move(conn, data_root, capsys):
    index_segment(conn, data_root, OLD)
    assert main(["archive", "--dry-run", "--cutoff", CUTOFF.isoformat()]) == 0

    out = capsys.readouterr().out
    assert f"保留期截止 {CUTOFF.isoformat()}" in out
    assert "到期 1 个文件（未压缩、未移动）" in out
    assert f"到期｜{OLD}" in out
    assert (data_root / OLD).exists()  # 试运行真的没动文件


def test_archive_dry_run_is_quiet_when_nothing_is_due(conn, data_root, capsys):
    assert main(["archive", "--dry-run"]) == 0
    assert "到期 0 个文件" in capsys.readouterr().out


def test_archive_moves_and_reports_the_range(conn, data_root, capsys):
    index_segment(conn, data_root, OLD)
    assert main(["archive", "--cutoff", CUTOFF.isoformat(), "--allow-same-disk"]) == 0

    out = capsys.readouterr().out
    assert "到期 1 个文件 → 已归档 1 个" in out
    assert "索引行已改指向归档件" in out
    assert not (data_root / OLD).exists()
    assert (data_root / OLD_ARCHIVED).exists()
    assert main(["archive", "--cutoff", CUTOFF.isoformat(), "--allow-same-disk"]) == 0
    assert "没有到期的原始记录" in capsys.readouterr().out


def test_archive_refuses_to_write_onto_the_same_disk(conn, data_root, capsys):
    index_segment(conn, data_root, OLD)
    assert main(["archive", "--cutoff", CUTOFF.isoformat()]) == 2
    assert "归档根不存在" in capsys.readouterr().err
    assert (data_root / OLD).exists()


def test_archive_reports_anomalies_with_a_non_zero_exit(conn, data_root, capsys):
    """超期却没进索引的文件：列出来、非零退出（cron 会因此报警），不动它。"""
    import json

    stray = data_root / "raw/huya/2026-02-10/660000-09.jsonl"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(json.dumps({"ts": 1}) + "\n", encoding="utf-8")

    assert main(["archive", "--cutoff", CUTOFF.isoformat(), "--allow-same-disk"]) == 1
    captured = capsys.readouterr()
    assert "超期但不在索引里" in captured.err
    assert stray.exists()


def test_archive_verify_reports_damage(conn, data_root, capsys):
    index_segment(conn, data_root, OLD)
    main(["archive", "--cutoff", CUTOFF.isoformat(), "--allow-same-disk"])
    capsys.readouterr()

    assert main(["archive", "--verify"]) == 0
    assert "全部通过" in capsys.readouterr().out

    artifact = data_root / OLD_ARCHIVED
    artifact.write_bytes(b"broken")
    assert main(["archive", "--verify"]) == 1
    assert "归档件自身摘要不一致" in capsys.readouterr().err


def test_archive_retrieve_writes_to_stdout_or_a_file(conn, data_root, capsys, tmp_path):
    index_segment(conn, data_root, OLD)
    expected = (data_root / OLD).read_text(encoding="utf-8")
    main(["archive", "--cutoff", CUTOFF.isoformat(), "--allow-same-disk"])
    capsys.readouterr()

    assert main(["archive", "--retrieve", OLD]) == 0
    captured = capsys.readouterr()
    assert captured.out == expected
    assert "内容摘要与封存值一致" in captured.err

    target = tmp_path / "out" / "660000-16.jsonl"
    assert main(["archive", "--retrieve", OLD_ARCHIVED, "--out", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == expected
    assert "已取回" in capsys.readouterr().out

    assert main(["archive", "--retrieve", "raw/huya/2026-03-01/000000-00.jsonl"]) == 2
    assert "证据文件不存在" in capsys.readouterr().err


def test_archive_cli_defaults_to_six_calendar_months(conn, data_root, capsys):
    """不传 `--cutoff` 时用「今天回推 6 个月」；一份两年前的文件因此一定到期。"""
    old = "raw/huya/2024-01-05/660000-09.jsonl"
    index_segment(conn, data_root, old)

    assert main(["archive", "--allow-same-disk"]) == 0
    out = capsys.readouterr().out
    assert f"保留期截止 {archive.cutoff_date().isoformat()}" in out
    assert date.fromisoformat(out.split("保留期截止 ")[1][:10]) == archive.cutoff_date()
