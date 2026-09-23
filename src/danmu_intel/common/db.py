"""SQLite 建库与连接（ADR-0002：stdlib `sqlite3`，WAL 模式，无 ORM）。

表按能力分批加，**不预留空表**：T1 是 `matches` / `rooms` / `room_sessions` /
`danmu_segments` / `slices` / `metrics` 六张；T2 加 `notifications`（采集异常事件
的出口，投递由 T11 接）。

T4 加 `gray_signals`（灰信号，含类别与作废原因）、`audit_log`（人工修正留痕）、
`config`（统计门槛，改动留审计）。

新增/改名列一律不做迁移（AGENTS.md 禁兼容层）：旧数据目录里的库不会被自动升级，
开发机上删掉它重建即可（原始 JSONL 是账本，库只是索引）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from danmu_intel.common import paths

DDL = """
CREATE TABLE IF NOT EXISTS matches(            -- 比赛实体（状态机主体）
  id INTEGER PRIMARY KEY, league TEXT NOT NULL, stage TEXT,
  team_a TEXT NOT NULL, team_b TEXT NOT NULL,
  scheduled_at INTEGER, started_at INTEGER, ended_at INTEGER,
  state TEXT NOT NULL,           -- scheduled|live|between_games|ended|aborted
  official_result TEXT,          -- JSON：比分/局列表，来自官方
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS rooms(              -- 直播间（采集目标）
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, room_id TEXT NOT NULL,
  url TEXT NOT NULL, streamer TEXT, discovered_by TEXT NOT NULL,  -- manual|schedule|pool
  is_live INTEGER NOT NULL DEFAULT 0, last_seen_at INTEGER,
  UNIQUE(platform, room_id));

CREATE TABLE IF NOT EXISTS room_sessions(      -- 一次采集会话（进程级）
  id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL, match_id INTEGER,
  pid INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER,
  state TEXT NOT NULL,           -- connecting|running|stalled|no_stream|exited
  restart_count INTEGER NOT NULL DEFAULT 0,   -- 房间第几次重启（supervisor 给）
  reconnects INTEGER NOT NULL DEFAULT 0,      -- 房间累计重连次数（跨重启接力）
  severity TEXT NOT NULL DEFAULT 'info',      -- info|warning|critical
  last_msg_at INTEGER);

CREATE TABLE IF NOT EXISTS danmu_segments(     -- 落盘文件索引（证据链）
  id INTEGER PRIMARY KEY, room_session_id INTEGER NOT NULL,
  rel_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
  first_ts INTEGER, last_ts INTEGER, msg_count INTEGER NOT NULL, sealed_at INTEGER);

CREATE TABLE IF NOT EXISTS slices(             -- 小局切片（边界可复核）
  id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL, game_no INTEGER NOT NULL,
  start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
  boundary_source TEXT NOT NULL,  -- official|danmu_signal|report_window|manual
  conflict_note TEXT,             -- 多来源冲突事实（高优先级胜出照样记录）
  override_by TEXT, override_at INTEGER, override_reason TEXT,
  UNIQUE(match_id, game_no));

CREATE TABLE IF NOT EXISTS metrics(            -- 规则统计产物（可重算）
  id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL, game_no INTEGER,
  metric_key TEXT NOT NULL,      -- density_curve|peak|score|kill_timeline|...
  value_json TEXT NOT NULL, computed_at INTEGER NOT NULL, algo_version TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS notifications(      -- 待投递事件（采集异常事件的出口）
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, severity TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at INTEGER NOT NULL,
  state TEXT NOT NULL,           -- pending|delivered|dropped_expired|failed
  delivered_at INTEGER, channel TEXT, attempts INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS gray_signals(       -- 灰信号（风险提示，不含指控）
  id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL,
  category TEXT NOT NULL,        -- 类别（需求 FR-C3-3：cheat_suspicion|betting|…）
  keyword TEXT NOT NULL, hit_count INTEGER NOT NULL, distinct_users INTEGER NOT NULL,
  window_count INTEGER NOT NULL, samples_json TEXT NOT NULL,  -- 必须附样本（需求 §6.5 第 3 条）
  status TEXT NOT NULL,          -- candidate|escalated|discarded
  reason TEXT,                   -- 作废/降级原因（不达标必须留原因）
  created_at INTEGER NOT NULL, evaluated_at INTEGER);

CREATE TABLE IF NOT EXISTS audit_log(          -- 一切人工/自动写操作留痕
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, target TEXT, detail_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS config(             -- 后台可视化配置（≤60s 生效）
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL, updated_by TEXT NOT NULL);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开数据库（WAL 模式）。调用方负责 `close()`。"""
    target = path or paths.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(DDL)
    conn.commit()
    return conn


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """打开并建表（幂等）。"""
    return init_db(connect(path))


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [row["name"] for row in rows]
