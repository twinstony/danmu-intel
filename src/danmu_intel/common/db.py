"""SQLite 建库与连接（ADR-0002：stdlib `sqlite3`，WAL 模式，无 ORM）。

表按能力分批加，**不预留空表**：T1 是 `matches` / `rooms` / `room_sessions` /
`danmu_segments` / `slices` / `metrics` 六张；T2 加 `notifications`（采集异常事件
的出口，投递由 T11 接）；T5 加 `reports`（报告三形态的版本账本）；T6 加 `llm_calls`
（LLM 调用记账，成本硬闸的数据来源）。

T4 加 `gray_signals`（灰信号，含类别与作废原因）、`audit_log`（人工修正留痕）、
`config`（统计门槛，改动留审计）。T7 加 `releases`（发布批次账本：版本标识、树指纹、
部署与提交指针、本批付费的比赛）。T8 加 `chain_cursors`（链上监听游标，补扫与断点续扫的
依据）与 `quota_usage`（供应商额度记账，报警阈值的唯一数据源）。

T9 加 `members` / `orders` / `order_payments` / `member_credentials` / `rate_limits`
（会员付费全自助闭环：会员状态机、订单状态机、逐笔入账幂等、凭据只存哈希、限流桶）。

T10 加 `stats_events`（站点统计明细：只有每日盐下的访客哈希，**没有 IP / UA / 身份**）、
`stats_daily`（日汇总：明细 90 天到期后唯一留存的口径）与 `stats_salt`（每日盐，只留当天一行）。

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

CREATE TABLE IF NOT EXISTS reports(            -- 报告实例（同场同形态换版即新增行，旧版可溯）
  id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL, game_no INTEGER,   -- 快报：触发它的小局
  kind TEXT NOT NULL,            -- live_brief|full|review
  version INTEGER NOT NULL, generated_at INTEGER NOT NULL,
  state TEXT NOT NULL,           -- published|failed
  content_json TEXT NOT NULL,    -- 段级内容（十一段结构）
  fact_layer_hash TEXT NOT NULL, -- 解读层输入的指纹（可溯源性）
  llm_state TEXT NOT NULL,       -- llm|rule_fallback
  path TEXT, checks_json TEXT, timing_json TEXT,
  UNIQUE(match_id, kind, version));

CREATE TABLE IF NOT EXISTS releases(          -- 发布批次（一次原子发布：产物 + 检查 + 回滚指针）
  id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE,
  tree_digest TEXT NOT NULL,     -- 站点树指纹（幂等判定的依据）
  state TEXT NOT NULL,           -- live|superseded|rolled_back|failed
  deployment_id TEXT,            -- Vercel 部署标识（秒级回滚的目标）
  deploy_ref TEXT,               -- git 提交（git revert 跟进用）
  paywalled_matches TEXT NOT NULL DEFAULT '[]',  -- 本批付费的比赛（转公开的翻转依据）
  pages_json TEXT NOT NULL, checks_json TEXT NOT NULL,
  created_at INTEGER NOT NULL);

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

CREATE TABLE IF NOT EXISTS llm_calls(        -- LLM 调用记账（成本硬闸的账本）
  id INTEGER PRIMARY KEY, match_id INTEGER, segment_no INTEGER,
  model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
  cache_hit_tokens INTEGER NOT NULL DEFAULT 0, cost_cny REAL NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL,         -- ok|timeout|error|rejected|gated（gated=闸住没调）
  reason TEXT, created_at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS config(             -- 后台可视化配置（≤60s 生效）
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL, updated_by TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS chain_cursors(     -- 链上监听游标（补扫与断点续扫的依据）
  id INTEGER PRIMARY KEY, network TEXT NOT NULL, scope TEXT NOT NULL,
  cursor TEXT NOT NULL,         -- polygon: 已处理到的最新入账区块；solana: 已处理的最新签名
  updated_at INTEGER NOT NULL, UNIQUE(network, scope));

CREATE TABLE IF NOT EXISTS quota_usage(       -- 供应商额度记账（FR-C6-11 / NFR-C-3）
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, day TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, credits REAL NOT NULL DEFAULT 0,
  last_error TEXT, updated_at INTEGER NOT NULL, UNIQUE(provider, day));

CREATE TABLE IF NOT EXISTS members(            -- 会员（凭既有通讯账号标识，不注册本站账号）
  id INTEGER PRIMARY KEY,
  contact_platform TEXT NOT NULL,              -- telegram|qq
  username TEXT NOT NULL, username_norm TEXT NOT NULL,   -- 规范化：唯一性判定用
  tier TEXT NOT NULL,                          -- standard|trial
  status TEXT NOT NULL,                        -- pending|active|grace|expired|revoked
  expires_at INTEGER, created_at INTEGER NOT NULL, revoked_at INTEGER,
  UNIQUE(contact_platform, username_norm));

CREATE TABLE IF NOT EXISTS orders(             -- 订单 = 一个收款要求（专属地址或唯一 memo）
  id INTEGER PRIMARY KEY, public_ref TEXT NOT NULL UNIQUE,  -- 页面短引用（Solana 的 memo）
  claim_hash TEXT NOT NULL,                    -- 领取令牌的哈希（明文只在下单那一刻出现）
  member_id INTEGER NOT NULL, tier TEXT NOT NULL,
  network TEXT NOT NULL,                       -- polygon|solana
  address TEXT NOT NULL,                       -- 专属派生地址（polygon）或收款地址（solana）
  address_index INTEGER,                       -- 派生索引：只前进不回退（防串单）
  memo TEXT, asset TEXT NOT NULL,
  amount_due_units INTEGER NOT NULL,           -- 应收（最小单位整数，含唯一尾数）
  status TEXT NOT NULL,                        -- pending|short|paid|expired
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, paid_at INTEGER,
  tx_ref TEXT, paid_units INTEGER NOT NULL DEFAULT 0, shortage_units INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS order_payments(     -- 逐笔入账（tx_ref 唯一 = 幂等键，AC-4）
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, tx_ref TEXT NOT NULL UNIQUE,
  network TEXT NOT NULL, asset TEXT NOT NULL, units INTEGER NOT NULL,
  at_ms INTEGER NOT NULL, recorded_at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS member_credentials( -- 会员凭据（**只存哈希**，明文不落库）
  id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL, code_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL, last_used_at INTEGER, revoked_at INTEGER);

CREATE TABLE IF NOT EXISTS rate_limits(        -- 限流桶（按 IP 与按账号双维度，NFR-S-2）
  bucket TEXT PRIMARY KEY, window_started_at INTEGER NOT NULL, hits INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS stats_events(      -- 站点统计明细（不含 IP / UA / 联系方式）
  id INTEGER PRIMARY KEY, day TEXT NOT NULL,  -- 本地日历日（与 chain.quota.day_key 同口径）
  ts INTEGER NOT NULL, page TEXT NOT NULL,
  visitor_hash TEXT NOT NULL,                 -- sha256(每日盐 + IP + UA)，跨日不可还原同一人
  paid INTEGER NOT NULL DEFAULT 0,            -- 访问时该页是否受付费墙保护（状态机判定）
  member_id INTEGER);                         -- 仅凭据校验通过时记（自愿留资/付费者，AC-9 的「除非」）

CREATE INDEX IF NOT EXISTS stats_events_day ON stats_events(day);

CREATE TABLE IF NOT EXISTS stats_daily(       -- 站点统计日汇总（明细到期后唯一留存的口径）
  day TEXT PRIMARY KEY, page_views INTEGER NOT NULL, sessions INTEGER NOT NULL,
  unique_visitors INTEGER NOT NULL, paid_page_views INTEGER NOT NULL,
  paid_unique_visitors INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS stats_salt(        -- 每日盐（只保留当天一行：旧盐即时丢弃）
  day TEXT PRIMARY KEY, salt TEXT NOT NULL);
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
