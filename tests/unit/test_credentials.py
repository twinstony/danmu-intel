"""凭据读取（仓库外 `.env`，0600）：AC-12 的代码侧防线。

测试里的假密钥一律**拼接构造**（本文件自己也要能被 `tools/check_no_secrets.py` 扫过）。
"""

from __future__ import annotations

import os
import stat

import pytest

from danmu_intel.common import paths
from danmu_intel.common.credentials import (
    REQUIRED_MODE,
    CredentialError,
    check_permissions,
    get_secret,
    load_env,
    parse_env,
    require_secret,
)

# 形如密钥但**不是**真凭据：拼接构造，扫描器看见的只是两段字符串。
FAKE_KEY = "sk-" + "test" * 8
KEY_NAME = "DEEPSEEK_API_KEY"


def write_env(root, text: str, *, mode: int = REQUIRED_MODE):
    root.mkdir(parents=True, exist_ok=True)
    target = root / ".env"
    target.write_text(text, encoding="utf-8")
    target.chmod(mode)
    return target


def test_env_path_lives_outside_the_repo(data_root):
    """凭据默认落在数据目录（仓库外），不落在仓库目录里。"""
    assert paths.env_path() == data_root / ".env"
    assert paths.repo_root() not in paths.env_path().parents


def test_parse_env_reads_pairs_comments_and_quotes():
    text = "\n".join(
        [
            "# 注释不算配置",
            "",
            f"{KEY_NAME}={FAKE_KEY}",
            "export DEEPSEEK_MODEL=deepseek-v4-flash",
            'DEEPSEEK_BASE_URL="https://api.deepseek.com"',
            "EMPTY=",
            "NOT_A_PAIR",
        ]
    )
    with pytest.raises(CredentialError, match="KEY=VALUE"):
        parse_env(text)

    parsed = parse_env(text.replace("NOT_A_PAIR", ""))
    assert parsed == {
        KEY_NAME: FAKE_KEY,
        "DEEPSEEK_MODEL": "deepseek-v4-flash",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    }


def test_load_env_missing_file_is_empty(tmp_path):
    assert load_env(tmp_path / "nope" / ".env") == {}


def test_load_env_reads_a_private_file(data_root):
    target = write_env(data_root, f"{KEY_NAME}={FAKE_KEY}\n")
    assert load_env(target) == {KEY_NAME: FAKE_KEY}


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o600 | 0o040])
def test_insecure_permissions_are_refused(data_root, mode):
    target = write_env(data_root, f"{KEY_NAME}={FAKE_KEY}\n", mode=mode)
    with pytest.raises(CredentialError, match="权限不安全"):
        load_env(target)
    with pytest.raises(CredentialError, match="权限不安全"):
        check_permissions(target)


def test_permission_error_does_not_leak_the_value(data_root):
    target = write_env(data_root, f"{KEY_NAME}={FAKE_KEY}\n", mode=0o644)
    with pytest.raises(CredentialError) as excinfo:
        load_env(target)
    assert FAKE_KEY not in str(excinfo.value)


def test_get_secret_prefers_the_file_then_the_environment(data_root, monkeypatch):
    monkeypatch.delenv(KEY_NAME, raising=False)
    assert get_secret(KEY_NAME) is None

    monkeypatch.setenv(KEY_NAME, "from-environment")
    assert get_secret(KEY_NAME) == "from-environment"

    target = write_env(data_root, f"{KEY_NAME}={FAKE_KEY}\n")
    assert get_secret(KEY_NAME) == FAKE_KEY
    assert get_secret("NOT_THERE", path=target) is None


def test_require_secret_reports_the_path_without_the_value(data_root):
    with pytest.raises(CredentialError) as excinfo:
        require_secret(KEY_NAME)
    message = str(excinfo.value)
    assert KEY_NAME in message and str(paths.env_path()) in message

    target = write_env(data_root, f"{KEY_NAME}={FAKE_KEY}\n")
    assert require_secret(KEY_NAME, path=target) == FAKE_KEY


def test_env_file_is_not_committed_by_gitignore():
    """防手滑：仓库里万一出现 `.env` 也不会被提交（凭据只允许在仓库外）。"""
    ignore = (paths.repo_root() / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [line.strip() for line in ignore]


def test_env_file_is_not_world_readable_after_write_in_tests(data_root):
    """测试写出的假凭据文件也不该是全局可读的（避免把不安全权限当常态）。"""
    target = write_env(data_root, f"{KEY_NAME}={FAKE_KEY}\n")
    assert stat.S_IMODE(os.stat(target).st_mode) == REQUIRED_MODE
