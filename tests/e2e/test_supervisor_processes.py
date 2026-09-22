"""端到端（真进程）：一房间一子进程的并发采集与监督。

对应 issue #5 的验收标准：

- 同场**3 个房间**并发采集，不重不漏——每房间条数、去重后条数、时间跨度都可查（AC-15）；
- `kill` 一个采集进程后 **10 秒内自动拉起**（有日志与库记录）；
- 进程退出 / 重启超限等异常**产生事件**（`notifications` 行，待 T11 投递）。

真进程、真 SQLite、真心跳、真 JSONL；只有网络那一段换成录制帧回放
（`tests/e2e/replay_child.py`），因此断网可跑。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from danmu_intel.collect.adapter import RoomKey
from danmu_intel.collect.health import room_contribution, room_health
from danmu_intel.collect.heartbeat import read_room_heartbeat
from danmu_intel.collect.incidents import DISK_LOW, PROCESS_EXIT, recent
from danmu_intel.collect.supervisor import Supervisor, child_invocation
from danmu_intel.common import paths
from danmu_intel.common.db import open_db
from danmu_intel.common.matches import create_match

from conftest import load_huya_fixture

REPLAY_CHILD = Path(__file__).parent / "replay_child.py"
FRAMES = Path(__file__).parent.parent / "fixtures" / "huya" / "frames.jsonl"
RUN_SECONDS = 20.0
FRAME_INTERVAL_S = 0.02
ROOM_IDS = ("660000", "323444", "11342412")


@pytest.fixture
def frame_count() -> int:
    return len([record for record in load_huya_fixture() if record["kind"] == "danmaku"])


def make_spawn(log_dir: Path):
    """把 supervisor 的真命令换成「回放子进程」——环境变量与生产完全一致。"""

    def spawn(command: list[str], environment: dict[str, str]) -> subprocess.Popen:
        url = command[command.index("--url") + 1]
        match_id = command[command.index("--match-id") + 1]
        room_id = url.rsplit("/", 1)[-1]
        replay = [
            sys.executable,
            str(REPLAY_CHILD),
            "--room-id",
            room_id,
            "--match-id",
            match_id,
            "--frames",
            str(FRAMES),
            "--interval",
            str(FRAME_INTERVAL_S),
        ]
        with (log_dir / f"{room_id}.log").open("ab") as log:  # Popen 会复制 fd，句柄可关
            return subprocess.Popen(replay, env=environment, stdout=log, stderr=subprocess.STDOUT)

    return spawn


def wait_until(predicate, *, timeout_s: float, what: str) -> float:
    """等到条件成立，返回等待耗时（秒）；超时即失败。"""
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        if predicate():
            return time.monotonic() - started
        time.sleep(0.1)
    raise AssertionError(f"等待超时（{timeout_s}s）：{what}")


def started_supervisor(data_root: Path, log_dir: Path, match_id: int) -> tuple[Supervisor, object]:
    conn = open_db(paths.db_path())
    rooms = [RoomKey("huya", room_id, f"https://www.huya.com/{room_id}") for room_id in ROOM_IDS]
    supervisor = Supervisor(conn, rooms, match_id=match_id, data_root=data_root, spawn=make_spawn(log_dir))
    return supervisor, conn


def test_three_rooms_collect_concurrently_without_duplicates_or_gaps(data_root, tmp_path, frame_count):
    conn = open_db(paths.db_path())
    try:
        match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    finally:
        conn.close()

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    supervisor, supervisor_conn = started_supervisor(data_root, log_dir, match_id)
    try:
        supervisor.run(seconds=RUN_SECONDS)
    finally:
        supervisor_conn.close()

    conn = open_db(paths.db_path())
    try:
        # ① 三个房间各自一个子进程，都跑过（库里有会话行）
        sessions = conn.execute("SELECT COUNT(*) AS n FROM room_sessions WHERE match_id=?", (match_id,)).fetchone()
        assert sessions["n"] == len(ROOM_IDS), "一房间一子进程，三个房间三条会话"

        # ② 不重不漏：每房间恰好 `frame_count` 条，去重后不变
        contributions = {item.room_id: item for item in room_contribution(conn, match_id, data_root=data_root)}
        assert set(contributions) == set(ROOM_IDS)
        for room_id, item in contributions.items():
            assert item.msg_count == frame_count, f"{room_id} 落盘条数不对（漏了或重了）"
            assert item.deduped_count == frame_count
            assert item.duplicate_count == 0
            assert item.session_count == 1
            assert item.last_ts is not None and item.first_ts is not None
            assert item.last_ts - item.first_ts == frame_count - 1, "固定时钟回放的时间跨度可复核"

        # ③ 贡献量按房间可查（AC-15），并集不重复
        assert sum(item.deduped_count for item in contributions.values()) == frame_count * len(ROOM_IDS)

        # ④ 健康状态可查：进程已收工，状态落到 exited
        health = {item.room_id: item for item in room_health(conn, match_id, data_root=data_root)}
        assert {item.state for item in health.values()} == {"exited"}

        # ⑤ 正常收工不该有「进程异常」类事件。`disk_low` 例外：测试的数据目录在小
        #    tmpfs 上（< 5GB 阈值），磁盘闸门如实报警是正确行为（生产数据目录在 /
        #    上，146G 可用，不会触发）。
        assert {item.kind for item in recent(conn, match_id=match_id)} <= {DISK_LOW}

        # ⑥ 每个房间的证据都封存进了 danmu_segments（SHA256 溯源）
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM danmu_segments seg JOIN room_sessions s ON s.id = seg.room_session_id "
            "WHERE s.match_id=?",
            (match_id,),
        ).fetchone()
        assert rows["n"] == len(ROOM_IDS)
    finally:
        conn.close()


def test_killed_child_is_restarted_within_ten_seconds(data_root, tmp_path, caplog, frame_count):
    """验收：`kill` 一个采集进程后 10 秒内自动拉起（有日志与库记录）。"""
    conn = open_db(paths.db_path())
    try:
        match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    finally:
        conn.close()

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    supervisor, supervisor_conn = started_supervisor(data_root, log_dir, match_id)
    victim = RoomKey("huya", ROOM_IDS[0], f"https://www.huya.com/{ROOM_IDS[0]}")
    outcome: dict[str, object] = {}

    def watch_and_kill() -> None:
        def finished_first_pass() -> bool:
            beat = read_room_heartbeat(victim, data_root=data_root)
            return beat is not None and beat.msg_count >= frame_count

        wait_until(finished_first_pass, timeout_s=15.0, what="第一个房间把录制帧收完")
        victim_pid = read_room_heartbeat(victim, data_root=data_root).pid
        killed_at = time.monotonic()
        os.kill(victim_pid, signal.SIGKILL)

        def replaced_child() -> bool:
            beat = read_room_heartbeat(victim, data_root=data_root)
            return beat is not None and beat.pid != victim_pid

        # 「拉起」以新进程写下第一条心跳为准（心跳是子进程活着的第一手证据）
        wait_until(replaced_child, timeout_s=10.0, what="被 kill 的采集进程 10 秒内被重新拉起")
        outcome["restart_s"] = time.monotonic() - killed_at
        outcome["old_pid"] = float(victim_pid)
        outcome["new_pid"] = float(read_room_heartbeat(victim, data_root=data_root).pid)

        def replaced_child_finished() -> bool:
            beat = read_room_heartbeat(victim, data_root=data_root)
            return beat is not None and beat.pid == outcome["new_pid"] and beat.msg_count >= frame_count

        wait_until(replaced_child_finished, timeout_s=15.0, what="重拉起来的进程继续收弹幕")

    def watcher() -> None:
        try:
            watch_and_kill()
        except Exception as exc:  # 线程里的失败必须带回主线程，否则只会沉默
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=watcher, daemon=True)
    thread.start()
    try:
        with caplog.at_level(logging.INFO, logger="danmu_intel.collect.supervisor"):
            supervisor.run(seconds=RUN_SECONDS)
    finally:
        thread.join(timeout=15.0)
        supervisor_conn.close()

    assert "error" not in outcome, outcome.get("error")
    assert "restart_s" in outcome, "看护线程没有走完（采集或重拉失败）"
    assert outcome["restart_s"] <= 10.0, f"kill 后 {outcome['restart_s']:.1f} 秒才拉起来，超过 10 秒"
    assert outcome["new_pid"] != outcome["old_pid"]

    # ① 库记录：被 kill 的会话行收尾，新会话行带上「第 1 次重启」与进程退出事件
    conn = open_db(paths.db_path())
    try:
        sessions = conn.execute(
            "SELECT s.*, r.room_id AS room_key FROM room_sessions s JOIN rooms r ON r.id = s.room_id "
            "WHERE s.match_id=? AND r.room_id=? ORDER BY s.id",
            (match_id, ROOM_IDS[0]),
        ).fetchall()
        assert len(sessions) == 2, "同一个房间重启后应留下两条会话行"
        assert sessions[0]["state"] == "exited" and sessions[0]["ended_at"] is not None
        assert sessions[1]["restart_count"] == 1
        assert sessions[1]["pid"] == int(outcome["new_pid"])
        kinds = [item.kind for item in recent(conn, match_id=match_id)]
        assert PROCESS_EXIT in kinds, "进程退出必须产生事件（为 T11 通知铺路）"
        exit_event = next(item for item in recent(conn, match_id=match_id) if item.kind == PROCESS_EXIT)
        assert exit_event.payload["room_id"] == ROOM_IDS[0]
        assert exit_event.payload["reason"] == "exited"

        # ② 重启后被 kill 的那一小时文件仍然进索引（不因进程被杀而漏证据）
        contribution = {
            item.room_id: item for item in room_contribution(conn, match_id, data_root=data_root)
        }[ROOM_IDS[0]]
        assert contribution.msg_count == frame_count * 2, "重放的两遍都落盘了（证据不丢）"
        assert contribution.deduped_count == frame_count, "同一批记录重放两遍，去重后仍是一份"
        assert contribution.duplicate_count == frame_count
        assert contribution.session_count == 2
    finally:
        conn.close()

    # ③ 日志记录：监督进程写下了重拉这一行
    assert any("已拉起采集子进程" in record.message for record in caplog.records), "日志里必须有重拉记录"
    assert any("心跳" in record.message or "重拉" in record.message for record in caplog.records)
    assert (log_dir / f"{ROOM_IDS[0]}.log").exists(), "子进程自己的日志也要落盘"


def test_production_child_command_is_runnable(data_root):
    """supervisor 递给子进程的命令行（`python -m danmu_intel collect …`）必须真的能跑。

    这里只验可启动性（`--help`），不连网络；真房间的采集由运维按 README 的命令跑。
    """
    from danmu_intel.collect.supervisor import RoomRun

    run = RoomRun(room=RoomKey("huya", ROOM_IDS[0], f"https://www.huya.com/{ROOM_IDS[0]}"), match_id=1)
    command, environment = child_invocation(run, data_root=data_root)
    completed = subprocess.run(
        [*command, "--help"], env=environment, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "--match-id" in completed.stdout and "--url" in completed.stdout
