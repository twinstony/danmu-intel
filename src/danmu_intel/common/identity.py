"""平台用户 ID 的加盐哈希（**不落明文身份** —— 设计 §5.2）。

盐值每次安装生成一次，存 `<data>/salt`（0600，仓库外）。同一平台用户 ID
在同一份数据目录内始终得到同一个 `user_hash`，可用于去重计数；跨数据目录
不可关联。
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

from danmu_intel.common import paths

SALT_BYTES = 32
HASH_LENGTH = 32


@lru_cache(maxsize=16)
def _load_or_create(target: Path) -> bytes:
    if target.exists():
        return target.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(SALT_BYTES)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # 并发创建：以已存在的为准
        return target.read_bytes()
    with os.fdopen(fd, "wb") as handle:
        handle.write(salt)
    return salt


def load_salt(path: Path | None = None) -> bytes:
    """读取（首次使用时生成）盐值。缓存按**解析后的路径**分桶，切数据目录即换盐。"""
    return _load_or_create(path or paths.salt_path())


def user_hash(platform: str, uid: str) -> str:
    salt = load_salt()
    digest = hashlib.sha256(salt + b"|" + platform.encode("utf-8") + b"|" + str(uid).encode("utf-8"))
    return digest.hexdigest()[:HASH_LENGTH]
