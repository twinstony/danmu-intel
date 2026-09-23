"""凭据读取：只认仓库外 `.env`（0600，设计 §14.4 / NFR-S-4 / AC-12）。

一条硬规矩：**凭据只允许存在于仓库外的 `.env`**（默认 `<数据目录>/.env`，即
`~/danmu-intel-data/.env`）。本模块因此只做三件事：读那个文件、查权限、按需覆盖进程环境。

- 权限位不是 0600（或多出任何 group/other 位）→ **拒绝读取**，宁可不发不可泄露；
- 文件不存在 → 返回空（调用方据此降级并如实标注，不静默）；
- 密钥值**绝不**出现在异常消息、日志、错误里（异常只说"哪个键缺失/文件什么权限"）。

`.env` 语法就是 `KEY=VALUE` 一行一条：`#` 开头是注释，值两侧可带单/双引号，允许 `export `
前缀。不做变量插值——凭据文件里插值只会让"哪里来的值"变模糊。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping

from danmu_intel.common import paths

REQUIRED_MODE = 0o600


class CredentialError(RuntimeError):
    """凭据不可用（文件不存在 / 权限不安全）。消息里只有键名与路径，没有值。"""


def parse_env(text: str) -> dict[str, str]:
    """解析 `.env` 文本（不插值；空值跳过）。"""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise CredentialError(f".env 行格式应为 KEY=VALUE：{line.split('=')[0][:32]}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key or not value:
            continue
        values[key] = value
    return values


def check_permissions(path: Path) -> None:
    """权限位必须严格是 0600（group/other 有任何一位就拒绝）。"""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != REQUIRED_MODE:
        raise CredentialError(
            f"凭据文件权限不安全：{path} 是 {mode:04o}，必须是 {REQUIRED_MODE:04o}"
            f"（chmod 600 {path}）"
        )


def load_env(path: Path | None = None, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """读 `.env`。文件不存在返回空 dict；存在则先查权限再解析。"""
    target = path or paths.env_path()
    if not target.exists():
        return {}
    check_permissions(target)
    return parse_env(target.read_text(encoding="utf-8"))


def get_secret(
    name: str,
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """取一个凭据：`.env` 优先，进程环境兜底（systemd 的 EnvironmentFile 等）。"""
    from_file = load_env(path).get(name)
    if from_file:
        return from_file
    environment = os.environ if environ is None else environ
    value = environment.get(name)
    return value or None


def require_secret(name: str, *, path: Path | None = None) -> str:
    """取一个必需凭据；缺失即抛错（调用方据此降级，消息里不含任何值）。"""
    value = get_secret(name, path=path)
    if not value:
        target = path or paths.env_path()
        raise CredentialError(
            f"缺少凭据 {name}：请写入 {target}（chmod 600 {target}）或设进进程环境"
        )
    return value
