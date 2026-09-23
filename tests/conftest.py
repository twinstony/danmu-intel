"""测试公共设施。

**断网可重复运行**（NFR-GA-4）：所有测试都不连真实直播、不调外网；
平台数据一律用 `tests/fixtures/` 里录制并脱敏的帧回放。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from danmu_intel.common import paths
from danmu_intel.common.db import open_db
from danmu_intel.common.events import DanmuEvent
from danmu_intel.common.matches import create_match
from danmu_intel.slice.manual import add_manual_slice

FIXTURES = Path(__file__).parent / "fixtures"
HUYA_FRAMES = FIXTURES / "huya" / "frames.jsonl"
SOOP_FRAMES = FIXTURES / "soop" / "frames.jsonl"
PLATFORM_FRAMES = {"huya": HUYA_FRAMES, "soop": SOOP_FRAMES}

# 固定时间基准（避免测试依赖当前时间）
BASE_TS = 1_790_064_000_123
REL_PATH = "raw/huya/2026-09-22/660000-16.jsonl"
ROOM_ID = "660000"
THREE_GAME_REL_PATH = "raw/huya/2026-09-22/660000-17.jsonl"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch) -> None:
    """任何测试都不得写到真实的 `~/danmu-intel-data` 或仓库 site/ 目录。

    autouse：漏写 data_root 的测试也不会污染真实数据目录（曾经漏过一次）。
    """
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "danmu-intel-data"))
    monkeypatch.setenv(paths.SITE_DIR_ENV, str(tmp_path / "site"))


@pytest.fixture
def data_root(isolated_dirs, tmp_path) -> Path:
    root = tmp_path / "danmu-intel-data"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def site_root(isolated_dirs, tmp_path) -> Path:
    return tmp_path / "site"


@pytest.fixture
def conn(data_root):
    connection = open_db(paths.db_path())
    yield connection
    connection.close()


def make_event(
    ts: int,
    *,
    text: str = "弹幕",
    user: str = "user-1",
    room_id: str = ROOM_ID,
    platform: str = "huya",
    match_id: int | None = None,
) -> DanmuEvent:
    return DanmuEvent(
        ts=ts,
        platform=platform,
        room_id=room_id,
        user_hash=user,
        text=text,
        extra={},
        match_id=match_id,
    )


def write_jsonl(path: Path, events: list[DanmuEvent]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(event.to_line() + "\n" for event in events)
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Ledger:
    """一场比赛的最小完整账本（原始记录 + 库内索引 + 切片）。"""

    conn: object
    data_root: Path
    match_id: int
    rel_path: str
    events: list[DanmuEvent]
    game1: tuple[int, int]
    game2: tuple[int, int]


@pytest.fixture
def ledger(data_root, conn) -> Ledger:
    """G1：[BASE_TS, +300s) 内 55 条（含 45 条突发，构成峰值窗口）；G2：后 60s 内 10 条。"""
    events = [make_event(BASE_TS + i * 3_000, text=f"G1 散落 {i}") for i in range(10)]
    events += [make_event(BASE_TS + 60_000 + i * 1_000, text=f"G1 突发 {i}") for i in range(45)]
    events += [make_event(BASE_TS + 300_000 + i * 6_000, text=f"G2 弹幕 {i}") for i in range(10)]
    digest = write_jsonl(data_root / REL_PATH, events)

    match_id = create_match(
        conn,
        league="LPL",
        team_a="iG",
        team_b="LNG",
        state="ended",
        official_result={"score": "2:0"},
    )
    _index_segment(conn, match_id=match_id, rel_path=REL_PATH, digest=digest, events=events)

    game1 = (BASE_TS, BASE_TS + 300_000)
    game2 = (BASE_TS + 300_000, BASE_TS + 360_000)
    add_manual_slice(conn, match_id=match_id, game_no=1, start_ms=game1[0], end_ms=game1[1])
    add_manual_slice(conn, match_id=match_id, game_no=2, start_ms=game2[0], end_ms=game2[1])

    return Ledger(
        conn=conn,
        data_root=data_root,
        match_id=match_id,
        rel_path=REL_PATH,
        events=events,
        game1=game1,
        game2=game2,
    )


def _index_segment(
    conn, *, match_id: int, rel_path: str, digest: str, events: list[DanmuEvent]
) -> None:
    """建房间行 + 采集会话行 + 落盘文件索引（测试里的最小取证链）。"""
    conn.execute(
        "INSERT INTO rooms(platform, room_id, url, streamer, discovered_by, is_live, last_seen_at) "
        "VALUES('huya', ?, 'https://www.huya.com/660000', '样例主播', 'manual', 1, ?)",
        (ROOM_ID, BASE_TS),
    )
    room_row_id = conn.execute("SELECT id FROM rooms ORDER BY id DESC").fetchone()["id"]
    conn.execute(
        "INSERT INTO room_sessions(room_id, match_id, pid, started_at, ended_at, state, last_msg_at) "
        "VALUES(?, ?, 1, ?, ?, 'exited', ?)",
        (room_row_id, match_id, BASE_TS, events[-1].ts, events[-1].ts),
    )
    session_id = conn.execute("SELECT id FROM room_sessions ORDER BY id DESC").fetchone()["id"]
    conn.execute(
        "INSERT INTO danmu_segments(room_session_id, rel_path, sha256, first_ts, last_ts, msg_count, sealed_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (session_id, rel_path, digest, events[0].ts, events[-1].ts, len(events), BASE_TS + 400_000),
    )
    conn.commit()


@dataclass
class ThreeGameLedger:
    """三个节点（小局）的比赛账本：G1/G2 已完成、G3 进行中。

    issue #8 的验收底座：赛中快报只发布已完成节点的段落。
    """

    conn: object
    data_root: Path
    match_id: int
    rel_path: str
    events: list[DanmuEvent]
    windows: dict[int, tuple[int, int]]
    completed_games: tuple[int, ...]
    in_progress_game: int

    def counts(self) -> dict[int, int]:
        return {
            no: sum(1 for event in self.events if start <= event.ts < end)
            for no, (start, end) in self.windows.items()
        }


@pytest.fixture
def three_game_ledger(data_root, conn) -> ThreeGameLedger:
    """G1：24 条；G2：12 条；G3（进行中）：8 条 —— 一场未结束的比赛（state=live）。"""
    span = 300_000
    windows = {no: (BASE_TS + (no - 1) * span, BASE_TS + no * span) for no in (1, 2, 3)}
    counts = {1: 24, 2: 12, 3: 8}
    events: list[DanmuEvent] = []
    for no, (start, _) in windows.items():
        events += [
            make_event(start + i * 10_000, text=f"G{no} 弹幕 {i}", user=f"user-{no}-{i}")
            for i in range(counts[no])
        ]
    digest = write_jsonl(data_root / THREE_GAME_REL_PATH, events)

    match_id = create_match(
        conn, league="LPL", team_a="iG", team_b="LNG", state="live"
    )
    _index_segment(
        conn, match_id=match_id, rel_path=THREE_GAME_REL_PATH, digest=digest, events=events
    )
    for no, (start, end) in windows.items():
        add_manual_slice(conn, match_id=match_id, game_no=no, start_ms=start, end_ms=end)

    return ThreeGameLedger(
        conn=conn,
        data_root=data_root,
        match_id=match_id,
        rel_path=THREE_GAME_REL_PATH,
        events=events,
        windows=windows,
        completed_games=(1, 2),
        in_progress_game=3,
    )


def load_fixture(platform: str) -> list[dict]:
    """读某个平台的脱敏录制帧（契约测试与适配器单测共用）。"""
    path = PLATFORM_FRAMES[platform]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
