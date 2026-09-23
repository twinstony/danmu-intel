"""数据落盘路径与站点路径（ADR-0002）。

原始弹幕与数据库**不进 git**，放在仓库外 `~/danmu-intel-data/`；
站点产物进 git（`site/`）。测试用 `DANMU_INTEL_DATA` / `DANMU_INTEL_SITE`
指向临时目录。
"""

from __future__ import annotations

import os
from datetime import datetime, tzinfo
from pathlib import Path

DATA_DIR_ENV = "DANMU_INTEL_DATA"
SITE_DIR_ENV = "DANMU_INTEL_SITE"
DEFAULT_DATA_DIR = Path.home() / "danmu-intel-data"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    return Path(os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR).expanduser()


def db_path() -> Path:
    return data_dir() / "db.sqlite3"


def salt_path() -> Path:
    """用户哈希盐值文件（0600，仓库外，永不进 git）。"""
    return data_dir() / "salt"


def env_path() -> Path:
    """凭据文件（`.env`，0600，**仓库外**，永不进 git —— 设计 §14.4 / NFR-S-4）。

    放数据目录（默认 `~/danmu-intel-data/.env`）而不是仓库目录：仓库里的任何文件
    都可能被误提交，而数据目录整个在仓库之外（`.gitignore` 只为防手滑留了 `.env` 一条）。
    测试用 `DANMU_INTEL_DATA` 指向临时目录，因此凭据天然隔离。
    """
    return data_dir() / ".env"


def raw_dir(platform: str, *, data_root: Path | None = None) -> Path:
    return (data_root or data_dir()) / "raw" / platform


def raw_path(
    platform: str, room_id: str, ts_ms: int, *, tz: tzinfo | None = None, data_root: Path | None = None
) -> Path:
    """`<data>/raw/<platform>/<yyyy-mm-dd>/<room_id>-<hh>.jsonl`（设计 §5.2）。

    时间用采集机的本地时区（ADR-0001 单机部署）；`tz` 与 `data_root` 供测试注入。
    """
    moment = datetime.fromtimestamp(ts_ms / 1000, tz=tz)
    directory = raw_dir(platform, data_root=data_root) / moment.strftime("%Y-%m-%d")
    return directory / f"{room_id}-{moment.strftime('%H')}.jsonl"


def site_dir() -> Path:
    override = os.environ.get(SITE_DIR_ENV)
    return Path(override).expanduser() if override else repo_root() / "site"


def match_dir(match_id: int) -> Path:
    return site_dir() / "matches" / str(match_id)


def report_page_path(match_id: int, kind: str) -> Path:
    """一场比赛的一份报告页面：`site/matches/<match_id>/<kind>.html`。

    三形态各占一个文件（快报/完整版/复盘版可同时在线），同场同形态的新版本
    覆盖旧版本（版本历史在 `reports` 表里，不只靠文件）。
    """
    return match_dir(match_id) / f"{kind}.html"


def rel_to_site(path: Path) -> str:
    return path.resolve().relative_to(site_dir().resolve()).as_posix()


def rel_to_data(path: Path) -> str:
    """相对数据根目录的路径——`SourceRef.rel_path` 用它（可迁移、不含绝对路径）。"""
    return path.resolve().relative_to(data_dir().resolve()).as_posix()
