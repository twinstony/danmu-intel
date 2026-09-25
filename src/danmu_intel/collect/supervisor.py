"""采集监督：一房间一子进程（设计 §7.4；issue #5 §1/§3/§6）。

主进程**只做调度，不碰网络**，因此它崩的概率极低：轮询、拉起、判活、收尸、留痕。

- 每 `POLL_INTERVAL_S`（5 秒）轮询一次；
- 子进程退出（崩溃、被外部 `kill`）→ 按 1s→2s→4s→8s→16s→32s→60s 退避拉起；
- **同一房间 30 分钟内重启超过 `RESTART_LIMIT` 次 → 停止重试**，原因写进库
  （`restart_exceeded` 事件），不靠内存里的状态；
- 子进程活着但心跳老化（>15 秒）→ 判定僵死，杀掉重启（`process_hung` 事件）；
- **配置版本号变了 → 立刻按新配置重起**（`config_changed`）：这是 NFR-T-4「配置改动
  1 分钟内生效」在子进程侧的那一半（轮询周期 5 秒 ≪ 60 秒）。配置改动对子进程的影响
  映射会随配置项而变，因此不去猜「哪把键影响哪个房间」，宁可多起一次（落盘与会话接着来），
  也不让旧配置继续跑；配置重起**不计入重启上限**（它不是故障，是人改的配置）；
- **数据源登记表变了 → 增/停对应的子进程**（`registry`）：后台在页面上登记或删掉一个
  直播间，改动同样递增版本号，于是这里读到新版本时顺手对齐房间集合
  （FR-C8-1/C8-2、需求 §3.4「添加直播间 → 一分钟内生效，无需重启服务」）；
- 子进程死了以后，它这一小时里已落盘但还没封存的文件由主进程补封（`seal_pending_files`），
  证据不许因为进程被杀而漏掉。

状态机的时钟与子进程句柄都可注入（测试缝：假时钟 + 假进程）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from danmu_intel.collect.adapter import RoomKey
from danmu_intel.collect.heartbeat import (
    HEARTBEAT_STALE_S,
    Heartbeat,
    Supervision,
    heartbeat_age_ms,
    read_heartbeat,
    supervision_env,
)
from danmu_intel.collect.incidents import (
    PROCESS_EXIT,
    PROCESS_HUNG,
    RESTART_EXCEEDED,
    emit,
    worst,
)
from danmu_intel.collect.runner import now_ms, seal_pending_files
from danmu_intel.common import config_store, paths

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0  # 主进程轮询周期（issue #5 §1）
RESTART_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)  # 设计 §7.4：1s → 60s 上限
RESTART_LIMIT = 5  # 窗口内允许的重启次数，超过即停止重试
RESTART_WINDOW_S = 1800.0  # 「30 分钟内」防雪崩窗口
HEARTBEAT_STARTUP_GRACE_S = 30.0  # 首次心跳宽限（进程启动+建库+首条心跳的正常耗时不超 1 秒）
KILL_GRACE_S = 5.0  # terminate 到 kill 的宽限
CHILD_MODULE = "danmu_intel"
CONFIG_CHANGED = "config_changed"  # 配置版本号变了，按新配置重起子进程


class ChildProcess(Protocol):
    """子进程句柄（`subprocess.Popen` 结构上满足；测试注入假进程）。"""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Spawner = Callable[[list[str], dict[str, str]], ChildProcess]
BeatReader = Callable[[RoomKey], Heartbeat | None]
RoomRegistry = Callable[[], list[RoomKey]]


@dataclass
class RoomRun:
    """一个房间的监督状态（重启历史与停止原因是可查事实，不是临时变量）。"""

    room: RoomKey
    match_id: int
    process: ChildProcess | None = None
    session_id: int | None = None
    restarts: int = 0  # 本次监督里该房间的累计重启次数（接力给子进程写进会话行）
    reconnects: int = 0  # 从心跳读回的房间累计重连次数
    restart_history: list[int] = field(default_factory=list)  # 窗口内重启时刻（epoch ms）
    next_attempt_at: int = 0
    spawn_ms: int = 0
    state: str = "starting"
    stopped: bool = False  # 停止重试（重启超限）
    reason: str | None = None  # 停止重试的原因
    config_restarts: int = 0  # 因配置版本号变化而重起的次数（不是故障，不计入上限）

    @property
    def pid(self) -> int | None:
        return None if self.process is None else self.process.pid


def child_invocation(
    run: RoomRun,
    *,
    data_root: Path,
    python: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """子进程命令行与环境（纯函数，便于断言；真拉起是 `subprocess.Popen`）。

    环境里三件事：`DANMU_INTEL_DATA` 让父子写同一个数据目录，
    `DANMU_INTEL_SUPERVISION` 把重启次数与累计重连数接力给子进程，
    `PYTHONPATH` 把父进程找到 `danmu_intel` 的那个目录接力给子进程。
    """
    command = [
        python or sys.executable,
        "-m",
        CHILD_MODULE,
        "collect",
        "--url",
        run.room.url,
        "--platform",
        run.room.platform,
        "--match-id",
        str(run.match_id),
    ]
    environment = dict(os.environ)
    environment.update(supervision_env(Supervision(run.restarts, run.reconnects)))
    environment[paths.DATA_DIR_ENV] = str(data_root)
    _inherit_import_path(environment)
    return command, environment


def _inherit_import_path(environment: dict[str, str]) -> None:
    """把「父进程是从哪儿 import 到 `danmu_intel` 的」递进子进程环境（就地改）。

    子进程是全新解释器，只继承环境变量、不继承 `sys.path`。装好包的生产环境不需要它，
    但**源树里跑**（`pytest` 的 `pythonpath=src`、`python -m danmu_intel`）时父进程的
    `sys.path` 是唯一线索 —— 不递过去，子进程会当场 `No module named danmu_intel` 退出，
    然后被当成故障反复重拉，真故障反而淹没在噪声里。
    """
    package = sys.modules.get(CHILD_MODULE)
    if package is None or not getattr(package, "__file__", None):
        return
    search = str(Path(package.__file__).resolve().parent.parent)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = f"{search}{os.pathsep}{existing}" if existing else search


def popen(command: list[str], environment: dict[str, str]) -> ChildProcess:
    return subprocess.Popen(command, env=environment)


def backoff_delay(restarts_in_window: int) -> float:
    """第 n 次重启的等待时长（设计 §7.4 的 1s→60s 阶梯）。"""
    index = max(0, restarts_in_window - 1)
    return RESTART_BACKOFF_S[min(index, len(RESTART_BACKOFF_S) - 1)]


def finalize_session(
    conn: sqlite3.Connection,
    session_id: int | None,
    *,
    state: str = "exited",
    severity: str | None = None,
    timestamp: int | None = None,
) -> None:
    """替死掉的子进程把会话行收尾（幂等：先到者为准，严重级别只升不降）。"""
    if session_id is None:
        return
    row = conn.execute("SELECT * FROM room_sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        return
    moment = timestamp if timestamp is not None else now_ms()
    ended_at = row["ended_at"] or moment
    final_state = row["state"] if row["ended_at"] else state
    conn.execute(
        "UPDATE room_sessions SET ended_at=?, state=?, severity=? WHERE id=?",
        (ended_at, final_state, worst(row["severity"], severity or "info"), session_id),
    )
    conn.commit()


class Supervisor:
    """多房间监督器：一个房间一个子进程，坏了就按退避曲线重来（有上限）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        rooms: Iterable[RoomKey],
        *,
        match_id: int,
        data_root: Path | None = None,
        clock: Callable[[], int] = now_ms,
        sleep: Callable[[float], None] = time.sleep,
        spawn: Spawner | None = None,
        read_beat: BeatReader | None = None,
        poll_interval: float | None = None,
        registry: RoomRegistry | None = None,
    ) -> None:
        self.conn = conn
        self.match_id = match_id
        self.data_root = data_root or paths.data_dir()
        self.clock = clock
        self.sleep = sleep
        # 缺省值在调用时取模块常量/函数，测试才能 monkeypatch 掉真进程与真等待
        self.spawn = spawn or popen
        self.poll_interval = POLL_INTERVAL_S if poll_interval is None else poll_interval
        self._read_beat = read_beat or (
            lambda room: read_heartbeat(room.platform, room.room_id, data_root=self.data_root)
        )
        self.config_version = config_store.version(conn)
        #: 数据源登记表的读取器（`None` = 房间集合由命令行给定，不随库变化）
        self.registry = registry
        self.runs: list[RoomRun] = [RoomRun(room=room, match_id=match_id) for room in rooms]

    # —— 对外 ——

    def run(self, *, seconds: float | None = None) -> None:
        """轮询到所有房间停下（或到达 `seconds` 上限），退出前收工。"""
        deadline = None if seconds is None else self.clock() + int(seconds * 1000)
        try:
            while True:
                self.tick()
                if deadline is not None and self.clock() >= deadline:
                    break
                if all(run.stopped for run in self.runs):
                    break
                self.sleep(self.next_wait_s())
        finally:
            self.shutdown()

    def next_wait_s(self, now: int | None = None) -> float:
        """下一次轮询前等多久：不晚于最近一个待拉起房间的退避到点时刻。

        固定睡满 5 秒会把「退避 1 秒」拖成 5 秒以上，`kill` 后 10 秒内拉起就没保证了。
        """
        moment = self.clock() if now is None else now
        wait_ms = int(self.poll_interval * 1000)
        pending = [
            run.next_attempt_at - moment for run in self.runs if run.process is None and not run.stopped
        ]
        if pending:
            wait_ms = max(0, min(wait_ms, min(pending)))
        return wait_ms / 1000

    def shutdown(self) -> None:
        """收工：把还活着的子进程停掉，并把它们的会话行补上结束时间。"""
        for run in self.runs:
            if run.process is None:
                continue
            pid = run.pid
            returncode = self._terminate(run)
            self._close_session(run, returncode=returncode, reason="supervisor_shutdown", emit_exit=False)
            logger.info("【%s/%s】监督收工，已停止子进程 pid=%s", run.room.platform, run.room.room_id, pid)

    def stopped_rooms(self) -> list[RoomRun]:
        return [run for run in self.runs if run.stopped]

    # —— 状态机 ——

    def tick(self, now: int | None = None) -> None:
        """一次轮询（时钟可注入：测试直接喂时刻，不必真等）。"""
        moment = self.clock() if now is None else now
        self._reload_config(moment)
        for run in self.runs:
            if run.process is None:
                if not run.stopped and moment >= run.next_attempt_at:
                    self._start(moment, run)
                continue
            returncode = run.process.poll()
            if returncode is not None:
                self._close_session(run, returncode=returncode, reason="exited")
                self._plan_restart(moment, run, returncode=returncode)
                continue
            self._check_heartbeat(moment, run)

    def _start(self, now: int, run: RoomRun) -> None:
        command, environment = child_invocation(run, data_root=self.data_root)
        run.spawn_ms = now
        try:
            process = self.spawn(command, environment)
        except Exception as exc:
            # 连进程都拉不起来：按「退出」走同一条退避/上限曲线，不空转烧 CPU
            logger.warning("【%s/%s】子进程启动失败：%s", run.room.platform, run.room.room_id, exc)
            self._close_session(run, returncode=None, reason="spawn_failed")
            self._plan_restart(now, run, returncode=None, reason="spawn_failed")
            return
        run.process = process
        run.session_id = None
        run.state = "running"
        logger.info(
            "【%s/%s】已拉起采集子进程 pid=%s（累计重启 %d 次）",
            run.room.platform,
            run.room.room_id,
            process.pid,
            run.restarts,
        )

    def _reload_config(self, now: int) -> None:
        """配置版本号变了 → 对齐数据源 + 让子进程按新配置重起（NFR-T-4 的跨进程那一半）。

        纯轮询判定，不靠信号也不靠内存里的时间戳：库里的版本号是唯一真相源，
        因此监督进程重启后再接着跑也不会错过一次配置变更。
        """
        current = config_store.version(self.conn)
        if current == self.config_version:
            return
        self.config_version = current
        self._sync_registry(now)
        for run in self.runs:
            if run.process is None or run.stopped:
                continue
            self._restart_for_config(now, run, version=current)

    def _sync_registry(self, now: int) -> None:
        """按登记表对齐房间集合：多出来的房间停掉，新登记的房间立刻排上。"""
        if self.registry is None:
            return
        wanted = {(room.platform, room.room_id): room for room in self.registry()}
        known = {(run.room.platform, run.room.room_id) for run in self.runs}
        for run in self.runs:
            if (run.room.platform, run.room.room_id) in wanted or run.stopped:
                continue
            self._drop_room(run)
        for key, room in wanted.items():
            if key in known:
                continue
            logger.info("【%s/%s】数据源新增，纳入监督", room.platform, room.room_id)
            self.runs.append(RoomRun(room=room, match_id=self.match_id, next_attempt_at=now))

    def _drop_room(self, run: RoomRun) -> None:
        """一个房间从数据源里被删掉了：停掉它的子进程，并记下原因（不是重启超限）。"""
        if run.process is not None:
            returncode = self._terminate(run)
            self._close_session(run, returncode=returncode, reason="room_removed", severity="info",
                                emit_exit=False)
            run.process = None
        run.stopped = True
        run.state = "removed"
        run.reason = "已从数据源登记表里删除（后台删掉了这个直播间）"
        logger.info("【%s/%s】%s", run.room.platform, run.room.room_id, run.reason)

    def _restart_for_config(self, now: int, run: RoomRun, *, version: int) -> None:
        """按新配置重起一个房间的子进程：立刻（不退避）且不动重启上限。

        退出事件不报（这不是故障，是人改了配置）、会话行按正常退出收尾
        （已落盘的文件照旧补封），新子进程自己开一条新会话。
        """
        pid = run.pid
        returncode = self._terminate(run)
        self._close_session(
            run, returncode=returncode, reason=CONFIG_CHANGED, severity="info", emit_exit=False
        )
        run.process = None
        run.session_id = None
        run.state = "restarting"
        run.next_attempt_at = now
        run.config_restarts += 1
        logger.info(
            "【%s/%s】配置已更新到 v%d，按新配置重起子进程（原 pid=%s，退避 0 秒）",
            run.room.platform,
            run.room.room_id,
            version,
            pid,
        )

    def _check_heartbeat(self, now: int, run: RoomRun) -> None:
        beat = self._beat_of(run)
        if beat is not None:
            run.session_id = beat.session_id
            run.reconnects = max(run.reconnects, beat.reconnects)
        age_ms = heartbeat_age_ms(beat, now, spawned_at_ms=run.spawn_ms)
        limit_ms = int(
            (HEARTBEAT_STALE_S if beat is not None else HEARTBEAT_STARTUP_GRACE_S) * 1000
        )
        if age_ms <= limit_ms:
            return
        logger.warning(
            "【%s/%s】心跳已停 %.0f 秒（pid=%s），判定僵死并重启",
            run.room.platform,
            run.room.room_id,
            age_ms / 1000,
            run.pid,
        )
        self._emit(
            PROCESS_HUNG,
            "warning",
            run,
            {"session_id": run.session_id, "heartbeat_age_ms": age_ms, "pid": run.pid},
        )
        returncode = self._terminate(run)
        self._close_session(run, returncode=returncode, reason="hung", severity="warning")
        self._plan_restart(now, run, returncode=returncode, reason="hung")

    def _plan_restart(self, now: int, run: RoomRun, *, returncode: int | None, reason: str = "exited") -> None:
        """退出后决定：退避重拉，还是到了上限停止重试。"""
        cutoff = now - int(RESTART_WINDOW_S * 1000)
        run.restart_history = [moment for moment in run.restart_history if moment >= cutoff]
        if len(run.restart_history) >= RESTART_LIMIT:
            run.stopped = True
            run.state = "stopped"
            run.reason = (
                f"{RESTART_WINDOW_S / 60:.0f} 分钟内已重启 {len(run.restart_history)} 次"
                f"（上限 {RESTART_LIMIT} 次），停止重试；最后一次退出码 {returncode}"
            )
            logger.error("【%s/%s】%s", run.room.platform, run.room.room_id, run.reason)
            finalize_session(self.conn, run.session_id, severity="critical", timestamp=now)
            self._emit(
                RESTART_EXCEEDED,
                "critical",
                run,
                {
                    "session_id": run.session_id,
                    "restart_limit": RESTART_LIMIT,
                    "restart_window_s": RESTART_WINDOW_S,
                    "restarts_in_window": len(run.restart_history),
                    "last_exit_code": returncode,
                    "reason": run.reason,
                },
            )
            return
        delay = backoff_delay(len(run.restart_history) + 1)
        run.restart_history.append(now)
        run.restarts += 1
        run.next_attempt_at = now + int(delay * 1000)
        logger.info(
            "【%s/%s】%.1f 秒后重拉（窗口内第 %d 次重启，原因：%s）",
            run.room.platform,
            run.room.room_id,
            delay,
            len(run.restart_history),
            reason,
        )

    def _close_session(
        self,
        run: RoomRun,
        *,
        returncode: int | None,
        reason: str,
        severity: str | None = None,
        emit_exit: bool = True,
    ) -> None:
        """子进程已经不在（死了或刚被我们杀掉）：补封文件、收尾会话行、报事件。"""
        beat = self._beat_of(run)
        session_id = run.session_id or (beat.session_id if beat is not None else None)
        if session_id is not None and beat is not None and beat.last_msg_at:
            seal_pending_files(
                self.conn,
                session_id,
                run.room,
                data_root=self.data_root,
                moments=[beat.last_msg_at, now_ms()],
            )
        finalize_session(
            self.conn,
            session_id,
            state="exited",
            severity=severity or ("info" if reason == "supervisor_shutdown" else "warning"),
            timestamp=now_ms(),
        )
        if emit_exit:
            self._emit(
                PROCESS_EXIT,
                "warning",
                run,
                {"session_id": session_id, "exit_code": returncode, "reason": reason, "pid": run.pid},
            )
        run.process = None
        run.session_id = session_id
        # 超限停止与「监督收工」都是 stopped；只有「还要重拉」才是 restarting
        run.state = "restarting" if emit_exit and not run.stopped else "stopped"

    def _terminate(self, run: RoomRun) -> int | None:
        """先 terminate，`KILL_GRACE_S` 内不走就 kill；返回退出码。"""
        process = run.process
        assert process is not None
        process.terminate()
        wait = getattr(process, "wait", None)
        if wait is not None:
            try:
                wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                process.kill()
        returncode = process.poll()
        if returncode is None:
            logger.error(
                "【%s/%s】pid=%s 连 SIGKILL 都停不下（信号被忽略？）",
                run.room.platform,
                run.room.room_id,
                process.pid,
            )
        return returncode

    def _beat_of(self, run: RoomRun) -> Heartbeat | None:
        """当前子进程的心跳；上一轮残留（pid 对不上）当作没有。"""
        beat = self._read_beat(run.room)
        if beat is None or run.process is None or beat.pid != run.process.pid:
            return None
        return beat

    def _emit(self, kind: str, severity: str, run: RoomRun, detail: dict[str, object]) -> None:
        emit(
            self.conn,
            kind,
            severity=severity,
            platform=run.room.platform,
            room_id=run.room.room_id,
            match_id=run.match_id,
            detail=detail,
        )
