"""证据文件的位置与读取：在线 JSONL 与归档件 `.jsonl.zst`（设计 §5.2/§5.3、ADR-0021）。

一份原始记录有两个地址，互为**纯函数**（`archive_rel_path` / `online_rel_path`）：

    在线：`raw/<platform>/<yyyy-mm-dd>/<room_id>-<hh>.jsonl`
    归档：`archive/<platform>/<yyyy-mm-dd>/<room_id>-<hh>.jsonl.zst`

`danmu_segments.rel_path` 指的是**证据当前在哪**（归档后指向归档件，ADR-0002 原文），
而报告里冻结的来源引用是**生成那一刻的地址**。两个地址之间的互换因此必须成立，
否则归档会打断「在线保留期内报告的数据溯源仍可核验」（NFR-D-4 / AC-17）。

压缩用 stdlib `compression.zstd`（Python 3.14 起自带，本机没有 pip 也不引第三方包）。
归档件是普通 zstd 流，NAS 上任何 `zstd -d` 都能解开，不绑定本仓库；反过来，归档件坏了
（传输截断、盘上被改）时抛 `DamagedArtifact` —— 人话、可被调用方拒交，不是裸 `ZstdError`。

摘要有两个口径，别混：

- **内容摘要**（`content_sha256`）：解压后的字节，等同 `danmu_segments.sha256`（封存值），
  报告的行范围摘要也在这个口径上；
- **归档件摘要**（`stored_sha256`）：文件自身的字节，用于查「归档件有没有损坏/被换掉」。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import IO

from compression import zstd

from danmu_intel.common import paths

RAW_PREFIX = "raw/"
ARCHIVE_PREFIX = "archive/"
JSONL_SUFFIX = ".jsonl"
ZST_SUFFIX = ".zst"
ARCHIVE_SUFFIX = JSONL_SUFFIX + ZST_SUFFIX
ZSTD_LEVEL = 10  # 归档是一次性批处理，取中高档换存储；解压速度不受影响


def is_archive(rel_path: str) -> bool:
    """这个地址是不是归档件（`.jsonl.zst`）。"""
    return rel_path.endswith(ARCHIVE_SUFFIX)


def archive_rel_path(rel_path: str) -> str:
    """在线地址 → 归档地址；已经是归档地址就原样返回（幂等）。"""
    if is_archive(rel_path):
        return rel_path
    if not rel_path.startswith(RAW_PREFIX) or not rel_path.endswith(JSONL_SUFFIX):
        raise ValueError(f"不是原始弹幕落盘路径：{rel_path}")
    return f"{ARCHIVE_PREFIX}{rel_path[len(RAW_PREFIX):]}{ZST_SUFFIX}"


def online_rel_path(rel_path: str) -> str:
    """归档地址 → 在线地址；已经是在线地址就原样返回（幂等）。

    反向派生是「归档后照旧可核验」的关键：报告里冻结的是在线地址，
    `danmu-segments` 里存的是归档后当前位置，两边靠它对齐。
    """
    if not is_archive(rel_path):
        return rel_path
    if not rel_path.startswith(ARCHIVE_PREFIX):
        raise ValueError(f"不是归档件路径：{rel_path}")
    return f"{RAW_PREFIX}{rel_path[len(ARCHIVE_PREFIX):-len(ARCHIVE_SUFFIX)]}{JSONL_SUFFIX}"


def resolve(rel_path: str, *, data_root: Path | None = None) -> Path:
    """按地址原样解析成交付路径（**不猜**位置：在线地址就是在线的那个路径）。"""
    return (data_root or paths.data_dir()) / rel_path


def locate(rel_path: str, *, data_root: Path | None = None) -> Path:
    """找到证据：先看给定地址，不在就看它的另一半地址（在线 ↔ 归档）。

    例：报告里冻结的是 `raw/…jsonl`，而这份证据已经归档 → 返回
    `archive/…jsonl.zst`。两个位置都没有时返回**给定地址**，好让调用方
    报出「文件不存在：raw/…」，而不是报一个调用方没提过的路径。
    """
    direct = resolve(rel_path, data_root=data_root)
    if direct.exists() or is_archive(rel_path):
        return direct
    archived = resolve(archive_rel_path(rel_path), data_root=data_root)
    return archived if archived.exists() else direct


# —— 读取（归档件透明解压） ——


def open_text(path: Path, *, encoding: str = "utf-8") -> IO[str]:
    if is_archive(path.name):
        return zstd.open(path, "rt", encoding=encoding)  # type: ignore[return-value]
    return path.open("r", encoding=encoding)


def open_binary(path: Path) -> IO[bytes]:
    if is_archive(path.name):
        return zstd.open(path, "rb")  # type: ignore[return-value]
    return path.open("rb")


class DamagedArtifact(ValueError):
    """归档件读不出来（损坏 / 被截断 / 根本不是 zstd 流）。

    归档件坏掉是**可预期的运维事实**（NAS 传输截断、盘上被改），不是程序 bug：
    因此这里把它翻成人话的 `ValueError`，让调用方拒交 / 报异常，而不是把压缩器的
    `zstd.ZstdError` 直接置到用户脸上。
    """


def read_bytes(path: Path) -> bytes:
    """读内容（归档件解压后）。归档件损坏时抛 `DamagedArtifact`（不是裸 `ZstdError`）。"""
    try:
        with open_binary(path) as handle:
            return handle.read()
    except zstd.ZstdError as exc:
        raise DamagedArtifact(f"归档件解压失败（文件损坏或被截断）：{path.name}（{exc}）")


# —— 摘要 ——


def content_sha256(path: Path) -> str:
    """内容摘要（未压缩字节）——与 `danmu_segments.sha256`、报告行范围摘要同口径。"""
    return hashlib.sha256(read_bytes(path)).hexdigest()


def stored_sha256(path: Path) -> str:
    """归档件自身字节的摘要（在线文件时等同内容摘要）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def line_range_sha256(path: Path, line_start: int, line_end: int) -> str:
    """对 `[line_start, line_end]` 行（含首尾、含行尾换行）的原始字节取 SHA256。"""
    if line_start < 1 or line_end < line_start:
        raise ValueError(f"非法行范围：{line_start}-{line_end}")
    lines = read_bytes(path).splitlines(keepends=True)
    if line_end > len(lines):
        raise ValueError(f"行范围超出文件：{line_end} > {len(lines)}")
    return hashlib.sha256(b"".join(lines[line_start - 1 : line_end])).hexdigest()


# —— 写入 ——


def compress_file(source: Path, target: Path, *, level: int = ZSTD_LEVEL) -> int:
    """把 `source` 压成 `target`（`.jsonl.zst`），返回归档件字节数。

    先写同目录的 `.part` 再 `os.replace`：NAS 上跑到一半被打断，不会留下一个
    看起来像归档件、其实截断的文件（半个文件比没有文件更坏）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.part")
    try:
        with source.open("rb") as src, zstd.open(temp, "wb", level=level) as dst:  # type: ignore[arg-type]
            shutil.copyfileobj(src, dst)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target.stat().st_size
