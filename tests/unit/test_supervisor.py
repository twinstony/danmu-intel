"""采集监督状态机（测试缝：假时钟 + 假子进程 + 真心跳文件）。

对应 issue #5 的验收：`kill` 一个采集进程后 10 秒内自动拉起、断流重连计入
`reconnects`、重启超限后「原因可查」、异常一律产生事件。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from danmu_intel.collect import supervisor as supervisor_module
from danmu_intel.collect.adapter import RoomKey
from danmu_intel.collect.heartbeat import (
    SUPERVISION_ENV,
    Heartbeat,
    Supervision,
    read_room_heartbeat,
    write_heartbeat,
)
from danmu_intel.collect.incidents import PROCESS_EXIT, PROCESS_HUNG, RESTART_EXCEEDED, recent
from danmu_intel.common.config import save_stats_config
from danmu_intel.collect.supervisor import (
    HEARTBEAT_STALE_S,
    RESTART_LIMIT,
    RESTART_WINDOW_S,
    RoomRun,
    Supervisor,
    backoff_delay,
    child_invocation,
    finalize_session,
)

from conftest import BASE_TS, make_event

ROOM = RoomKey("huya", "660000", "https://www.huya.com/660000")
OTHER_ROOM = RoomKey("huya", "323444", "https://www.huya.com/323444")


class FakeProcess:
    """假子进程：`returncode=None` 表示活着；`terminate/kill` 立刻生效。"""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode

    def exits(self, returncode: int = 1) -> None:
        self.returncode = returncode


class Harness:
    """一台「假时钟 + 假子进程 + 真心跳/真库」的监督环境。"""

    def __init__(self, conn, rooms=None, *, match_id: int = 1, data_root: Path) -> None:
        self.conn = conn
        self.now = BASE_TS
        self.processes: list[FakeProcess] = []
        self.environments: list[dict[str, str]] = []
        self.beats: dict[str, Heartbeat] = {}
        self.supervisor = Supervisor(
            conn,
            list(rooms or [ROOM]),
            match_id=match_id,
            data_root=data_root,
            clock=lambda: self.now,
            sleep=lambda seconds: self.advance(seconds),
            spawn=self.spawn,
            read_beat=lambda room: self.beats.get(room.room_id),
        )

    # —— 假外部世界 ——

    def spawn(self, command, environment) -> FakeProcess:
        self.environments.append(environment)
        process = FakeProcess(pid=1000 + len(self.processes))
        self.processes.append(process)
        return process

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1000)

    def tick(self) -> None:
        self.supervisor.tick(self.now)

    def alive(self, index: int = 0) -> FakeProcess:
        process = self.supervisor.runs[index].process
        assert process is not None
        return process

    def seed_session(
        self,
        *,
        index: int = 0,
        last_msg_at: int | None = BASE_TS + 1_000,
        state: str = "running",
        severity: str = "info",
        msg_count: int = 3,
    ) -> int:
        """模拟子进程已经落下的会话行（子进程侧行为由 test_session_runtime 覆盖）。"""
        run = self.supervisor.runs[index]
        self.conn.execute(
            "INSERT OR IGNORE INTO rooms(platform, room_id, url, discovered_by) VALUES('huya', ?, ?, 'manual')",
            (run.room.room_id, run.room.url),
        )
        room_row_id = self.conn.execute(
            "SELECT id FROM rooms WHERE room_id=?", (run.room.room_id,)
        ).fetchone()["id"]
        cursor = self.conn.execute(
            """
            INSERT INTO room_sessions(room_id, match_id, pid, started_at, state, restart_count,
                                      reconnects, severity, last_msg_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_row_id,
                run.match_id,
                run.pid,
                BASE_TS,
                state,
                run.restarts,
                run.reconnects,
                severity,
                last_msg_at,
            ),
        )
        self.conn.commit()
        session_id = int(cursor.lastrowid)
        self.heartbeat(index=index, session_id=session_id, last_msg_at=last_msg_at, msg_count=msg_count)
        return session_id

    def heartbeat(
        self,
        *,
        index: int = 0,
        session_id: int,
        state: str = "running",
        reconnects: int = 0,
        last_msg_at: int | None = BASE_TS + 1_000,
        age_s: float = 0.0,
        pid: int | None = None,
        msg_count: int = 3,
    ) -> Heartbeat:
        run = self.supervisor.runs[index]
        beat = Heartbeat(
            pid=run.pid if pid is None else pid,
            session_id=session_id,
            platform=run.room.platform,
            room_id=run.room.room_id,
            state=state,
            started_at=BASE_TS,
            last_msg_at=last_msg_at,
            msg_count=msg_count,
            reconnects=reconnects,
            restart_count=run.restarts,
            written_at=self.now - int(age_s * 1000),
        )
        self.beats[run.room.room_id] = beat
        return beat

    def incident_kinds(self) -> list[str]:
        return [item.kind for item in recent(self.conn, match_id=self.supervisor.match_id)]

    def died(self, *, returncode: int = 1, index: int = 0) -> None:
        """子进程死了（外部 kill / 崩溃），再轮询一次。"""
        self.alive(index).exits(returncode)
        self.tick()


def test_child_invocation_carries_data_dir_and_supervision(data_root):
    run = RoomRun(room=ROOM, match_id=9, restarts=3, reconnects=2)
    command, environment = child_invocation(run, data_root=data_root)
    assert command[:4] == [sys.executable, "-m", "danmu_intel", "collect"]
    assert command[command.index("--url") + 1] == ROOM.url
    assert command[command.index("--match-id") + 1] == "9"
    assert command[command.index("--platform") + 1] == "huya"
    assert environment["DANMU_INTEL_DATA"] == str(data_root)
    assert json.loads(environment[SUPERVISION_ENV]) == {"restart_count": 3, "reconnects": 2}


def test_tick_starts_one_child_per_room(conn, data_root):
    harness = Harness(conn, [ROOM, OTHER_ROOM], data_root=data_root)
    harness.tick()
    assert len(harness.processes) == 2
    assert [run.state for run in harness.supervisor.runs] == ["running", "running"]
    assert [run.pid for run in harness.supervisor.runs] == [1000, 1001]
    harness.tick()
    assert len(harness.processes) == 2, "活着就不许重复拉起"


def test_killed_child_is_restarted_within_ten_seconds(conn, data_root):
    """验收：`kill` 掉采集进程后 10 秒内自动拉起（有日志与库记录）。"""
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    first = harness.alive()
    session_id = harness.seed_session()
    killed_at = harness.now

    harness.died(returncode=-9)
    run = harness.supervisor.runs[0]
    assert run.state == "restarting" and run.process is None
    assert harness.incident_kinds() == [PROCESS_EXIT], "进程退出必须留事件"

    harness.tick()  # 退避 1 秒还没到 → 不拉
    assert run.process is None
    harness.advance(1)
    harness.tick()
    assert run.process is not None
    assert harness.now - killed_at <= 10_000, "10 秒内必须拉起来"

    row = conn.execute("SELECT * FROM room_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["state"] == "exited" and row["ended_at"] is not None, "死掉的会话行必须收尾"
    assert json.loads(harness.environments[-1][SUPERVISION_ENV]) == {"restart_count": 1, "reconnects": 0}


def test_wait_shortens_to_the_next_restart_deadline(conn, data_root):
    """退避到点就拉，不许被 5 秒轮询粒度拖后（关系「kill 后 10 秒内拉起」）。"""
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    assert harness.supervisor.next_wait_s(harness.now) == supervisor_module.POLL_INTERVAL_S
    harness.seed_session()
    harness.died(returncode=-9)
    assert harness.supervisor.next_wait_s(harness.now) == 1.0
    harness.advance(1)
    assert harness.supervisor.next_wait_s(harness.now) == 0.0


def test_backoff_ladder_grows_to_sixty_seconds(conn, data_root):
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    observed: list[int] = []
    for _ in range(4):
        harness.seed_session()
        harness.died(returncode=3)
        observed.append(harness.supervisor.runs[0].next_attempt_at - harness.now)
        harness.advance(120)
        harness.tick()
    assert observed == [1_000, 2_000, 4_000, 8_000]
    assert [backoff_delay(n) for n in range(1, 9)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_restart_limit_stops_retrying_and_keeps_reason(conn, data_root):
    """重启超限即停止重试，原因进库（不是只在内存里）。"""
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    while not harness.supervisor.runs[0].stopped:
        harness.seed_session()
        harness.died(returncode=1)
        harness.advance(120)
        harness.tick()

    run = harness.supervisor.runs[0]
    assert run.restarts == RESTART_LIMIT
    assert "停止重试" in (run.reason or "")
    assert harness.supervisor.stopped_rooms() == [run]

    spawned = len(harness.processes)
    harness.advance(RESTART_WINDOW_S * 2)
    harness.tick()
    assert len(harness.processes) == spawned, "停止重试后绝不再拉"

    incidents = recent(conn, match_id=1)
    assert incidents[0].kind == RESTART_EXCEEDED and incidents[0].severity == "critical"
    assert incidents[0].payload["reason"] == run.reason
    assert incidents[0].payload["restart_limit"] == RESTART_LIMIT
    assert incidents[0].payload["restarts_in_window"] == RESTART_LIMIT
    assert conn.execute("SELECT severity FROM room_sessions").fetchall()[-1]["severity"] == "critical"


def test_restart_history_window_forgets_old_restarts(conn, data_root):
    """窗口外的重启不计入上限（30 分钟前崩过不算这次的账）。"""
    harness = Harness(conn, data_root=data_root)
    run = harness.supervisor.runs[0]
    run.restart_history = [harness.now - int(RESTART_WINDOW_S * 1000) - 1] * RESTART_LIMIT
    harness.tick()
    harness.seed_session()
    harness.died(returncode=1)
    assert run.stopped is False, "旧账过期后应继续重试"
    assert len(run.restart_history) == 1 and run.restarts == 1


def test_stale_heartbeat_kills_and_restarts_child(conn, data_root):
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    session_id = harness.seed_session()
    harness.heartbeat(session_id=session_id, age_s=HEARTBEAT_STALE_S + 1)
    harness.tick()

    run = harness.supervisor.runs[0]
    assert run.process is None, "心跳老化 = 僵死，必须杀掉"
    assert harness.processes[0].terminated is True
    kinds = harness.incident_kinds()
    assert set(kinds) == {PROCESS_HUNG, PROCESS_EXIT}, "僵死与它引起的退出都要留痕"
    assert recent(conn)[0].payload["reason"] == "hung"
    row = conn.execute("SELECT * FROM room_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["severity"] == "warning" and row["state"] == "exited"

    harness.advance(1)
    harness.tick()
    assert run.process is not None, "僵死也要按退避曲线重拉"


def test_fresh_heartbeat_keeps_child_and_records_reconnects(conn, data_root):
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    process = harness.alive()
    harness.heartbeat(session_id=7, reconnects=3, state="stalled", age_s=HEARTBEAT_STALE_S - 1)
    harness.tick()
    run = harness.supervisor.runs[0]
    assert run.process is process, "心跳新鲜就不许动它"
    assert run.reconnects == 3 and run.session_id == 7


def test_first_heartbeat_gets_startup_grace(conn, data_root):
    """子进程要先探测房间（HTTP 最多 15 秒）才写第一条心跳，不能因此被误杀。"""
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    harness.advance(supervisor_module.HEARTBEAT_STARTUP_GRACE_S - 5)
    harness.tick()
    assert harness.supervisor.runs[0].process is not None
    harness.advance(6)  # 超过宽限仍无心跳 → 僵死
    harness.tick()
    assert harness.supervisor.runs[0].process is None
    assert PROCESS_HUNG in harness.incident_kinds()


def test_heartbeat_from_previous_session_is_ignored(conn, data_root):
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    harness.heartbeat(session_id=7, pid=999_999, age_s=0.0)  # 上一轮残留的心跳文件
    harness.advance(supervisor_module.HEARTBEAT_STARTUP_GRACE_S + 1)
    harness.tick()
    assert harness.supervisor.runs[0].process is None, "pid 对不上的心跳不算心跳"


def test_dead_child_files_are_sealed_by_supervisor(conn, data_root):
    """子进程被 kill 时来不及封存自己写的文件，主进程必须补封（证据不许漏）。"""
    from danmu_intel.common import paths

    harness = Harness(conn, data_root=data_root)
    harness.tick()
    last_msg_at = BASE_TS + 1_000
    path = paths.raw_path("huya", "660000", last_msg_at, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(make_event(last_msg_at).to_line() + "\n", encoding="utf-8")

    session_id = harness.seed_session(last_msg_at=last_msg_at)
    harness.died(returncode=-9)

    row = conn.execute("SELECT * FROM danmu_segments").fetchone()
    assert row is not None, "被 kill 的子进程留下的文件必须进索引"
    assert row["rel_path"].endswith("660000-16.jsonl") and row["msg_count"] == 1
    assert row["room_session_id"] == session_id


def test_spawn_failure_walks_the_same_backoff_and_limit(conn, data_root):
    """连进程都拉不起来（解释器/路径坏了）也要留痕，不能空转烧 CPU。"""
    harness = Harness(conn, data_root=data_root)

    def broken_spawn(command, environment):
        raise OSError("no such interpreter")

    harness.supervisor.spawn = broken_spawn
    harness.tick()
    run = harness.supervisor.runs[0]
    assert run.process is None and run.restarts == 1
    assert run.next_attempt_at == harness.now + 1_000, "启动失败也走同一条退避曲线"
    assert harness.incident_kinds() == [PROCESS_EXIT]
    assert recent(conn)[0].payload["reason"] == "spawn_failed"


def test_run_loops_with_injected_clock_and_stops_on_deadline(conn, data_root):
    harness = Harness(conn, data_root=data_root)
    harness.supervisor.run(seconds=0.05)
    assert harness.processes, "至少拉起一次"
    assert harness.supervisor.runs[0].process is None, "退出前必须把子进程停掉"
    assert harness.processes[0].terminated is True
    assert harness.incident_kinds() == [], "监督自己收工不算异常退出"


def test_run_exits_when_every_room_gave_up(conn, data_root):
    harness = Harness(conn, [ROOM, OTHER_ROOM], data_root=data_root)
    for run in harness.supervisor.runs:
        run.stopped = True
    harness.supervisor.run()
    assert harness.processes == []


def test_shutdown_terminates_and_finalizes(conn, data_root):
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    session_id = harness.seed_session()
    harness.supervisor.shutdown()
    assert harness.processes[0].terminated is True
    assert harness.supervisor.runs[0].process is None
    assert harness.supervisor.runs[0].state == "stopped", "收工后不该显示成还在等重拉"
    row = conn.execute("SELECT * FROM room_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["state"] == "exited" and row["ended_at"] is not None
    assert row["severity"] == "info", "人工收工不是异常"


def test_finalize_session_is_idempotent_and_severity_only_rises(conn):
    conn.execute("INSERT INTO rooms(platform, room_id, url, discovered_by) VALUES('huya','1','u','manual')")
    conn.execute("INSERT INTO room_sessions(room_id, pid, started_at, state) VALUES(1, 5, 100, 'running')")
    conn.commit()
    session_id = int(conn.execute("SELECT id FROM room_sessions").fetchone()["id"])

    finalize_session(conn, session_id, state="exited", severity="warning", timestamp=BASE_TS)
    finalize_session(conn, session_id, state="exited", severity="info", timestamp=BASE_TS + 999)
    row = conn.execute("SELECT * FROM room_sessions").fetchone()
    assert row["state"] == "exited" and row["ended_at"] == BASE_TS
    assert row["severity"] == "warning", "严重级别只升不降"
    finalize_session(conn, None)
    finalize_session(conn, 9999)


def test_default_beat_reader_matches_child_heartbeat_path(data_root):
    """supervisor 读心跳的路径必须与子进程写的路径一致。"""
    beat = Heartbeat(
        pid=1,
        session_id=7,
        platform="huya",
        room_id="660000",
        state="running",
        started_at=BASE_TS,
        last_msg_at=BASE_TS,
        msg_count=1,
        reconnects=0,
        restart_count=0,
        written_at=BASE_TS,
    )
    assert read_room_heartbeat(ROOM, data_root=data_root) is None
    write_heartbeat(beat, data_root=data_root)
    assert read_room_heartbeat(ROOM, data_root=data_root) == beat


@pytest.mark.parametrize("severity", ["info", "warning", "critical"])
def test_varied_severities_are_accepted_by_the_state_machine(conn, data_root, severity):
    """会话行里已存在的严重级别不参与状态机判定（只影响汇总展示）。"""
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    session_id = harness.seed_session(severity=severity)
    harness.died(returncode=1)
    row = conn.execute("SELECT severity FROM room_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["severity"] == max(severity, "warning", key=["info", "warning", "critical"].index)


# —— 配置版本号 → 子进程重启（NFR-T-4：配置改动 1 分钟内生效）——


def test_config_change_restarts_child_immediately(conn, data_root):
    """配置版本号变了：立刻（不退避）杀掉旧子进程并拉起新子进程。"""
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    old = harness.alive()
    session_id = harness.seed_session()
    run = harness.supervisor.runs[0]
    assert run.config_restarts == 0

    save_stats_config(conn, actor="管理员", changes={"gray_min_hits": 9})
    harness.tick()

    assert old.terminated is True, "旧子进程必须停下来（不能带着旧配置继续跑）"
    assert run.config_restarts == 1
    # 同一个 tick 里就拉起来了：next_attempt_at = 现在，不等退避（≤5 秒的轮询粒度 ≪ 60 秒）
    assert len(harness.processes) == 2
    assert run.process is not old and run.state == "running"
    assert harness.now - BASE_TS < supervisor_module.POLL_INTERVAL_S * 1000, "配置生效不等退避"
    row = conn.execute("SELECT * FROM room_sessions WHERE id=?", (session_id,)).fetchone()
    assert row["state"] == "exited" and row["ended_at"] is not None
    assert row["severity"] == "info", "人工改配置不是采集异常"


def test_config_restart_does_not_count_toward_restart_limit(conn, data_root):
    """配置重起不是故障：改几次配置都不该把房间推进「重启超限停止重试」。"""
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    for index in range(RESTART_LIMIT + 2):
        save_stats_config(conn, actor="管理员", changes={"gray_min_hits": 5 + index})
        harness.tick()
        harness.tick()
    run = harness.supervisor.runs[0]
    assert run.config_restarts == RESTART_LIMIT + 2
    assert run.restarts == 0 and run.restart_history == [] and run.stopped is False
    assert run.process is not None, "改完配置照样在采"
    assert harness.incident_kinds() == [], "配置重起不产生「采集进程退出」事件"


def test_config_change_without_running_children_does_nothing(conn, data_root):
    """没有在跑的子进程（比如全部停下来了）就不重起，只把版本号记下来。"""
    harness = Harness(conn, data_root=data_root)
    save_stats_config(conn, actor="管理员", changes={"gray_min_hits": 7})
    harness.tick()
    assert len(harness.processes) == 1, "该拉起来还是拉起来（版本号变了不影响首次启动）"
    assert harness.supervisor.runs[0].config_restarts == 0


def test_config_version_is_reread_from_the_db(conn, data_root):
    """版本号从库里读：监督进程重启后再接着跑也不会错过一次配置变更。"""
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    save_stats_config(conn, actor="管理员", changes={"gray_min_hits": 4})
    restarted = Harness(conn, data_root=data_root)
    assert restarted.supervisor.config_version == 1
    restarted.tick()
    assert restarted.supervisor.runs[0].config_restarts == 0, "版本号已在启动时对齐，不必白重起"


# —— 数据源登记表 → 增/停子进程（FR-C8-2：1 分钟内生效）——


def test_registry_adds_and_removes_rooms_on_version_change(conn, data_root):
    """后台登记/删除直播间 → 版本号变 → 监督进程对齐房间集合并拉/停子进程。"""
    from danmu_intel.common import rooms as rooms_module

    def read_registry() -> list[RoomKey]:
        return [
            RoomKey(room.platform, room.room_id, room.url)
            for room in rooms_module.list_rooms(conn)
        ]

    harness = Harness(conn, [ROOM], data_root=data_root)
    harness.supervisor.registry = read_registry
    harness.tick()
    assert len(harness.supervisor.runs) == 1

    # 后台登记第二个直播间：版本号一变，新房间立刻纳入监督
    rooms_module.add_room(conn, platform="huya", room_id="323444", url=OTHER_ROOM.url, actor="admin")
    harness.tick()
    assert [run.room.room_id for run in harness.supervisor.runs] == ["660000", "323444"]
    assert harness.supervisor.runs[1].process is not None, "新房间要真的被拉起来"

    # 后台删掉第一个：它的子进程被停掉，并记下原因（不是重启超限）
    first = rooms_module.list_rooms(conn)[0]
    rooms_module.delete_room(conn, first.id, actor="admin")
    harness.tick()
    removed = harness.supervisor.runs[0]
    assert removed.stopped is True and removed.state == "removed"
    assert "登记表" in (removed.reason or "")
    assert harness.processes[0].terminated is True
    assert removed.config_restarts == 0, "被删除的房间不该再按配置重起一遍"


def test_registry_is_ignored_when_not_configured(conn, data_root):
    harness = Harness(conn, data_root=data_root)
    harness.tick()
    save_stats_config(conn, actor="管理员", changes={"gray_min_hits": 8})
    harness.tick()
    assert [run.room.room_id for run in harness.supervisor.runs] == ["660000"]
