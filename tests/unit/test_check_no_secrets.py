"""凭据扫描器测试（设计 §14.4 第 3 条：防线自身也必须被测）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from danmu_intel.common import paths
from tools.check_no_secrets import main, scan_file, scan_tree

# 假凭据一律**拼接构造**：本测试文件自己也要能通过扫描，
# 因此不给扫描器开任何白名单/豁免（开了洞的防线不算防线）。
FAKE_PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA " + "PRIVATE KEY-----"
FAKE_HEX_KEY = "0x" + "a1b2c3d4" * 8
FAKE_OPENAI = "sk-" + "A" * 40
FAKE_GITHUB = "ghp_" + "B" * 36
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_MNEMONIC = "mnemonic" + ": abandon abandon abandon"
FAKE_TELEGRAM = "123456789" + ":AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
FAKE_KEYSTORE = "UTC--2024-01-01T00-00-00.000Z--" + "0123456789abcdef" * 2 + "01234567"


def test_scan_tree_is_clean_on_this_repo():
    assert scan_tree(paths.repo_root()) == []


@pytest.mark.parametrize(
    "content",
    [
        FAKE_PRIVATE_KEY,
        FAKE_HEX_KEY,
        FAKE_OPENAI,
        FAKE_GITHUB,
        FAKE_AWS,
        FAKE_MNEMONIC,
        FAKE_TELEGRAM,
        FAKE_KEYSTORE,
    ],
)
def test_scan_file_detects_secrets(tmp_path, content):
    target = tmp_path / "leak.py"
    target.write_text(f"KEY = {content!r}\n", encoding="utf-8")
    hits = scan_file(target)
    assert hits, f"未能识别：{content[:30]}"
    assert hits[0][1] == 1


def test_scan_skips_binary_and_skip_dirs(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.py").write_text(FAKE_OPENAI, encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG" + FAKE_OPENAI.encode())
    (tmp_path / "notes.md").write_text("正常文档，无凭据。", encoding="utf-8")
    assert scan_tree(tmp_path) == []


def test_main_reports_hits(tmp_path, capsys):
    (tmp_path / "config.json").write_text(f'{{"key": "{FAKE_OPENAI}"}}', encoding="utf-8")
    assert main([str(tmp_path)]) == 1
    assert "发现 1 处" in capsys.readouterr().err


def test_main_cleans_and_reports_success(tmp_path, capsys):
    (tmp_path / "a.md").write_text("干净", encoding="utf-8")
    assert main([str(tmp_path)]) == 0
    assert "零命中" in capsys.readouterr().out


def test_main_scans_single_file(tmp_path, capsys):
    target = tmp_path / "x.txt"
    target.write_text(FAKE_HEX_KEY, encoding="utf-8")
    clean = tmp_path / "clean.txt"
    clean.write_text("干净", encoding="utf-8")
    assert main([str(target)]) == 1
    assert main([str(clean)]) == 0
    assert main([str(target), str(clean)]) == 1
    capsys.readouterr()


def test_pre_commit_hook_blocks_secrets(tmp_path):
    """钩子脚本本体也要能拦住带凭据的提交（防「装了但没生效」）。"""
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "deploy" / "hooks").mkdir(parents=True)
    for name in ("check_no_secrets.py",):
        source = paths.repo_root() / "tools" / name
        (repo / "tools" / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    hook = paths.repo_root() / "deploy" / "hooks" / "pre-commit"
    (repo / "deploy" / "hooks" / "pre-commit").write_text(hook.read_text(encoding="utf-8"), encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    dirty = repo / "leak.env"
    dirty.write_text(f"OPENAI_API_KEY={FAKE_OPENAI}", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(repo / "tools" / "check_no_secrets.py"), str(repo)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "疑似可动用资产凭据" in result.stderr
