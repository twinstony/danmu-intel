# 弹幕情报库 · 工程设计文档 v2.0

> **本文性质**：设计文档（怎么做）。需求唯一来源是 `docs/requirements/DANMU_INTEL_REQUIREMENTS.md`（= Issue #2），本文不重新定义需求。
>
> **v2.0 与 v1 的区别**：v1 未经 grill 流程直接成文，含多处单方面拍板。v2.0 的每一条设计决策都经过与用户的逐项 grill，已确认项落为 `docs/adr/`，未确认项集中于第 19 章「开放项」。
>
> 状态：**待用户 review**（Issue #3，标签 `ready-for-human`）· 日期：2026-09-22

---

## 0. 输入与依据

| 来源 | 位置 | 用途 |
|---|---|---|
| 需求文档（唯一需求来源） | `docs/requirements/DANMU_INTEL_REQUIREMENTS.md` | 能力/规则/非功能/验收 的权威定义 |
| 参考实现（他人项目，只读） | `~/Workspace/danmu-intel-local` | 可复用素材的实证来源；**不引入其代码** |
| 领域文档 | `CONTEXT.md`、`docs/adr/` | 术语口径与已拍板决策 |
| 流程 | grill-with-docs → to-spec | 本设计的产出流程 |

**本仓库当前状态**：只有需求文档 + agent 基建，**零产品代码**。所有能力均为新建；蓝本只提供"哪个做法可行/哪个坑已踩过"的证据。

---

## 1. 设计决策总表

| # | 决策 | 结论 | 落点 |
|---|---|---|---|
| D1 | 运行载体 | 单机：**本机 10.0.0.138 开发验证并最终承载**（原需求 NFR-A-3 单机形态） | ADR-0001 |
| D2 | 站点形态 | 页面按蓝图的 **Vercel 静态部署**方案产出（静态骨架 + 免费内容公开） | ADR-0001 |
| D3 | 公网入口 | 本机后端经 **Tailscale Funnel** 出公网（只开必要接口）；后台仅 tailnet 内可达 | ADR-0001 |
| D4 | 付费内容位置 | **付费段落不进静态产物**，由后端 API 凭凭据返回（否则 curl 即可白嫖） | ADR-0001 |
| D5 | 存储 | **SQLite 单文件**（stdlib `sqlite3`，无外部进程） | ADR-0002 |
| D6 | 原始弹幕落盘 | 仓库**外** `~/danmu-intel-data/`；git 只装代码 + 站点产物；6 个月后归档 NAS | ADR-0002 |
| D7 | 解读层 | **直连 LLM API（DeepSeek 官方）**，结构化输入 + 受约束输出 | ADR-0003 |
| D8 | 成本硬闸 | 单场 ≤ ¥0.3、每日 ≤ ¥10；超闸 → 规则直出 + 报警 | ADR-0003 |
| D9 | LLM 降级 | **规则直出兜底**，报告按时发布（保时效、降质量并标注） | ADR-0003 |
| D10 | 收款地址 | Polygon：**xpub 派生 watch-only 地址**；Solana：**单一收款地址 + 每单唯一 memo** | ADR-0004 |
| D11 | 链上监听 | Polygon=**Polygonscan API**；Solana=**Helius**；均含额度记账 + 阈值报警 | ADR-0005 |
| D12 | 会员开通 | **页面全自助**（无人工）：下单 → 付款 → 自动检测 → 自动开通 → 用户凭通讯账号领取 | ADR-0006 |
| D13 | 后台形态 | **独立 Python 进程**（服务端渲染），仅 tailnet 可达 | ADR-0007 |
| D14 | 首发平台 | 架构支持四平台，本版**只启用虎牙 + SOOP** | ADR-0008 |
| D15 | 免费/付费判定 | 按**比赛状态机**判定，不看文件名（修正蓝本"按文件名一刀切"的教训） | ADR-0009 |
| D16 | 发布原子性 | `site/.staging` → 6 项检查 → 原子替换 → 提交；回滚 = Vercel instant rollback（秒级）+ `git revert`（账本一致） | ADR-0009 |
| D17 | 通知通道 | 告警与到期提醒走 **QQ Bot（主）+ Telegram（备）**；5 分钟未送达即销毁 | ADR-0010 |

---

## 2. 当前状态 vs 目标状态

### 2.1 蓝图实证核查（39 项被点名素材）

- **36 项真实存在**：可直接作为"做法可行性"的参考。
- **3 项对不上，构成本设计的输入缺口**：
  1. `INTEL_PRODUCT_FRAMEWORK.md` → 实际文件名漂移为 `docs/task/INTEL_PRODUCT_FRAMEWORK_2026-08-31.md`（含 §B1–B6，其中"时间窗纪律"被需求文档标为硬要求）。
  2. `config/streamers.json` → 蓝本快照中**不存在**；最接近的是 `docs/data/intel/streamer_profiles.json`（数据）与 `config/AGENTS.md`。
  3. `members.json` → **不在版本控制内**，只存在于服务器 `/opt/danmu-intel/` 上。
  → **设计含义**：凡需求文档引用这三项的地方，设计必须自定并落 ADR，不能照抄。

### 2.2 蓝图可复用（做法/教训）与不可复用（代码）

| 蓝本资产 | 处理 | 理由 |
|---|---|---|
| 四平台采集脚本的做法（匿名连接、断线重连、按房间落 JSONL） | **参考做法**，重写 | 需统一适配器契约与健康上报，蓝本是各脚本各自为政 |
| 规则直出渲染器（零 LLM、固定模板） | **参考并被复用为兜底渲染器** | 它已证明"不带 LLM 也能出可读报告"，正好当 D9 的降级路径 |
| 付费墙注入器（按文件名正则决定加锁） | **不采用做法，仅记录教训** | 按文件名判定已造成误锁（2026-08-26 修复）；D15 改为按状态机 |
| 会员体系（服务器上 `members.json` + Vercel Serverless 校验 + 人工登记） | **不采用** | 状态出仓库、校验在两端、开通靠人工，与 D12/ADR-0002 冲突 |
| 站点静态页面 + 导航 + 索引构建脚本 | **参考做法**，重写 | 结构可借鉴，但需接入发布原子性与 6 项检查 |
| 蓝本全部 Python 代码 | **不引入** | 需求文档规定蓝图素材"参考/参考不采用"，且本文档 AGENTS.md 要求不留兼容层、不照搬 |

---

## 3. 总体架构

### 3.1 运行拓扑

```
                        ┌──────────── 公网（用户浏览器）────────────┐
                        │                                          │
             静态骨架 + 免费内容                          付费内容 / 校验 / 统计上报
                        │                                          │
                Vercel（静态托管）                    Tailscale Funnel（HTTPS, :8443）
                        │                                          │
                        └──────────────► 本机 danmu-intel 后端 ◄───┘
                                          · FastAPI（site-api 进程）
                                          · SQLite（单文件）
                                          · 静态产物生成器
                                                 ▲
                                                 │ tailnet-only
                                          ┌──────┴───────┐
                                          │ 后台（admin）│ 仅 100.118.248.92 可达
                                          └──────────────┘

  本机常驻进程组（全部由 systemd 拉起，Restart=always）
   ├─ collector       采集调度 + 房间进程管理（一房间一子进程）
   ├─ slicer-stats    切片 + 规则统计（事件驱动）
   ├─ reporter        报告生成（规则层 → LLM 解读 → 渲染）
   ├─ publisher       发布器（产物生成 → 6 项检查 → 原子替换 → 提交/部署）
   ├─ chain-watcher   链上入账监听（Polygon / Solana）
   ├─ site-api        FastAPI：校验 / 付费内容 / 统计上报 / 下单 / 后台
   └─ notifier        通知投递（含 5 分钟时效闸门）
```

### 3.2 数据流（一场比赛）

```
直播间音频? 否 —— 弹幕文本流
 ① 采集：多房间并发 → 规范化弹幕事件 → 按(平台,房间,小时)追加 JSONL（只增不改）
 ② 发现/切片：赛程/人工登记 → 比赛实体；官方时间 > 弹幕信号 > 已发布报告窗口 的三级边界
 ③ 规则统计：密度曲线/峰值/比分/击杀轴/灰信号候选/终局信号（全部纯函数，可重算）
 ④ 报告：赛中快报（节点结束 ≤2min）/ 完整版（赛后 ≤10min）/ 赛后复盘（≤15min）
 ⑤ 解读：LLM 只读"事实层产物"，只填解读段落，禁止引入新事实
 ⑥ 发布：十一段报告 → 产物生成 → 6 项检查 → 原子替换 site/ → 提交 → Vercel 部署
 ⑦ 会员/付费：赛中转付费、结束即转公开（状态机触发再发布）
 ⑧ 可回溯：任意事实 → 落盘文件 + 起止时间 + 校验和
```

### 3.3 三条贯穿原则的架构落点

| 需求原则 | 架构落点 |
|---|---|
| 事实优先 | 事实层（②③）与解读层（⑤）物理分离；解读层输入只含事实层产物；报告里事实段带来源标注 |
| 不丢证据 | 原始 JSONL 只增不改 + 每段落 SHA256 校验和 + 切片边界来源留痕 + 人工修正留痕 |
| 失败要响 | 每个进程写健康心跳；告警 5 分钟时效闸门（超时销毁）；额度/成本超闸报警；额度受限即报 |

---

## 4. 技术选型

| 维度 | 选择 | 备选 | 理由 |
|---|---|---|---|
| 语言 | Python 3.11 | Node/Go | 蓝图全 Python；本机 3.11.16；生态直接可用 |
| Web 框架 | FastAPI + Uvicorn | Flask | 自带 OpenAPI 与 pydantic 校验，前后端契约即代码 |
| 后台页面 | Jinja2 服务端渲染 | SPA | AGENTS.md「最简实现」；无需前端构建链 |
| 存储 | SQLite（`sqlite3` stdlib, WAL） | PostgreSQL / JSON 文件 | 单机、单写者、零外部进程；JSON 文件无法支撑索引/事务/并发读 |
| 原始数据 | 按(平台,房间,小时)的 append-only JSONL | 全塞数据库 | 只增不改天然满足"不丢证据"；便于校验和与归档 |
| 调度 | systemd 服务 + 进程内 asyncio 定时器 | cron / APScheduler | 不引入新依赖；`Restart=always` 天然满足 NFR-A-4 |
| 静态站点 | 生成 → git 提交 → Vercel | 本机自托管全站 | 用户选定的蓝图路线；CDN 承担流量，Funnel 带宽上限只用于 API 小载荷 |
| 公网入口 | Tailscale Funnel（:8443） | Cloudflare Tunnel + 自有域名 | 免费、无需域名、无需新装组件；限制=仅 ts.net 域名与 443/8443/10000 端口、带宽上限不可配 |
| 解读 LLM | DeepSeek 官方 API（OpenAI 兼容） | Codex CLI / 纯规则 | 延迟可控（进 2 分钟预算）、成本可测、中文强 |
| 链上数据 | Polygonscan API + Helius | Alchemy / 自建节点 | 免费额度均已覆盖实际用量（见 §14.3） |
| 通知 | QQ Bot API + Telegram Bot API | 微信 | 均为本机已有通道；时效闸门统一 |
| 测试 | pytest + 内存 SQLite + fixture 回放 | 依赖真实直播 | 覆盖率 ≥90% 必须能离线跑（NFR-GA） |

**明确不引入**：不引入前端框架/构建链、不引入消息队列、不引入 Redis、不引入 ORM（用 stdlib `sqlite3` + 手写 SQL）、不引入加速层（需求 Q-3 已拍板）、不引入容器（单机 systemd 直接跑）。

> **落地修正**：语言行写的是「Python 3.11」，而运行环境是 **Python 3.14**（`python3 -V` = 3.14.4）：
> 后台实际用 `aiohttp` 而不是 FastAPI/Jinja2（ADR-0020），归档压缩用 stdlib `compression.zstd`
> 而不是第三方 `zstandard`（ADR-0021），不引入新依赖的底线因此保持，代价是
> `requires-python >= 3.14`。

---

## 5. 数据模型

### 5.1 SQLite 表（`~/danmu-intel-data/db.sqlite3`，WAL 模式）

**比赛与采集**

```sql
CREATE TABLE matches(            -- 比赛实体（状态机主体）
  id INTEGER PRIMARY KEY, league TEXT NOT NULL, stage TEXT,
  team_a TEXT NOT NULL, team_b TEXT NOT NULL,
  scheduled_at INTEGER, started_at INTEGER, ended_at INTEGER,
  state TEXT NOT NULL,           -- scheduled|live|between_games|ended|aborted
  official_result TEXT,          -- JSON：比分/局列表，来自官方
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);

CREATE TABLE rooms(             -- 直播间（采集目标）
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, room_id TEXT NOT NULL,
  url TEXT NOT NULL, streamer TEXT, discovered_by TEXT NOT NULL, -- manual|schedule|pool
  is_live INTEGER NOT NULL DEFAULT 0, last_seen_at INTEGER,
  UNIQUE(platform, room_id));

CREATE TABLE room_sessions(     -- 一次采集会话（进程级）
  id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL, match_id INTEGER,
  pid INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER,
  state TEXT NOT NULL,          -- running|exited|stalled|no_stream
  restart_count INTEGER NOT NULL DEFAULT 0, last_msg_at INTEGER);

CREATE TABLE danmu_segments(    -- 落盘文件索引（证据链）
  id INTEGER PRIMARY KEY, room_session_id INTEGER NOT NULL,
  rel_path TEXT NOT NULL UNIQUE, -- 证据当前在哪（归档后 = archive/…jsonl.zst，ADR-0021）
  sha256 TEXT NOT NULL,          -- 内容摘要（未压缩字节），归档前后不变（封存值）
  first_ts INTEGER, last_ts INTEGER, msg_count INTEGER NOT NULL, sealed_at INTEGER,
  archived_at INTEGER,           -- NULL = 仍在线（T13）
  archive_sha256 TEXT);          -- 归档件自身字节的摘要（T13）
```

**切片与统计**

```sql
CREATE TABLE slices(            -- 小局切片（边界可复核）
  id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL, game_no INTEGER NOT NULL,
  start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
  boundary_source TEXT NOT NULL,  -- official|danmu_signal|report_window|manual
  conflict_note TEXT,             -- 多来源冲突事实（高优先级胜出照样记录）
  override_by TEXT, override_at INTEGER, override_reason TEXT,
  UNIQUE(match_id, game_no));

CREATE TABLE metrics(           -- 规则统计产物（可重算）
  id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL, game_no INTEGER,
  metric_key TEXT NOT NULL,      -- density_curve|peak|score|kill_timeline|...
  value_json TEXT NOT NULL, computed_at INTEGER NOT NULL, algo_version TEXT NOT NULL);

CREATE TABLE gray_signals(      -- 灰信号（风险提示，不含指控）
  id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL, keyword TEXT NOT NULL,
  hit_count INTEGER NOT NULL, distinct_users INTEGER NOT NULL,
  window_count INTEGER NOT NULL, samples_json TEXT NOT NULL, -- 必须附样本
  status TEXT NOT NULL,          -- candidate|escalated|discarded
  created_at INTEGER NOT NULL, evaluated_at INTEGER);
```

**报告与发布**

```sql
CREATE TABLE reports(           -- 报告实例
  id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL, game_no INTEGER,
  kind TEXT NOT NULL,            -- live_brief|full|review
  version INTEGER NOT NULL, generated_at INTEGER NOT NULL,
  state TEXT NOT NULL,           -- drafting|ready|published|failed
  content_json TEXT NOT NULL,    -- 十一段结构（段级）
  fact_layer_hash TEXT NOT NULL, -- 解释层输入的指纹（可溯源性）
  llm_state TEXT NOT NULL,       -- llm|rule_fallback
  path TEXT, checks_json TEXT, UNIQUE(match_id, kind, version));

CREATE TABLE releases(          -- 发布批次（原子单位）
  id INTEGER PRIMARY KEY, version TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL, site_dir TEXT NOT NULL,
  checks_json TEXT NOT NULL, state TEXT NOT NULL,  -- staged|live|rolled_back
  deploy_ref TEXT, rolled_back_at INTEGER, rolled_back_by TEXT);

CREATE TABLE publish_checks(    -- 6 项发布前检查的逐项结果
  id INTEGER PRIMARY KEY, release_id INTEGER NOT NULL,
  check_key TEXT NOT NULL, passed INTEGER NOT NULL, detail TEXT);
```

**会员与付费**

```sql
CREATE TABLE members(
  id INTEGER PRIMARY KEY,
  contact_platform TEXT NOT NULL,          -- telegram|qq
  username TEXT NOT NULL, username_norm TEXT NOT NULL, -- 规范化（小写去@）用于唯一性
  tier TEXT NOT NULL,                      -- standard|trial
  status TEXT NOT NULL,                    -- pending|active|grace|expired|revoked
  expires_at INTEGER, created_at INTEGER NOT NULL, revoked_at INTEGER,
  UNIQUE(contact_platform, username_norm));

CREATE TABLE orders(            -- 订单 = 一个收款要求
  id INTEGER PRIMARY KEY, public_ref TEXT NOT NULL UNIQUE, -- 页面展示的短引用
  member_id INTEGER, network TEXT NOT NULL,   -- polygon|solana
  address TEXT NOT NULL,                       -- 专属地址（polygon）或收款地址（solana）
  memo TEXT,                                   -- solana 每单唯一标识
  amount_due_units TEXT NOT NULL,              -- 最小单位整数（避免浮点）
  asset TEXT NOT NULL, status TEXT NOT NULL,   -- pending|paid|short|expired|refunded
  created_at INTEGER NOT NULL, expires_at INTEGER, paid_at INTEGER,
  tx_ref TEXT, paid_units TEXT, shortage_units TEXT);

CREATE TABLE chain_cursors(     -- 补扫与幂等的基础
  id INTEGER PRIMARY KEY, network TEXT NOT NULL, scope TEXT NOT NULL,
  cursor TEXT NOT NULL,          -- polygon: 最后扫描区块；solana: 最后签名
  updated_at INTEGER NOT NULL, UNIQUE(network, scope));

CREATE TABLE quota_usage(       -- 额度记账（FR-C6-11）
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, day TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, credits REAL NOT NULL DEFAULT 0,
  last_error TEXT, updated_at INTEGER NOT NULL, UNIQUE(provider, day));
```

**统计 / 通知 / 审计 / 配置**

```sql
CREATE TABLE stats_events(      -- 站点统计原始事件（不跨站跟踪）
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, page TEXT NOT NULL,
  visitor_hash TEXT NOT NULL,   -- 每日盐 + IP + UA 的哈希，次日不可回溯同一人
  member_id INTEGER, dwell_ms INTEGER);

CREATE TABLE stats_daily(       -- 汇总
  day TEXT PRIMARY KEY, page_views INTEGER, sessions INTEGER,
  unique_visitors INTEGER, paid_page_views INTEGER, leads INTEGER);

CREATE TABLE notifications(     -- 通知投递（5 分钟时效闸门）
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, severity TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at INTEGER NOT NULL,
  state TEXT NOT NULL,          -- pending|delivered|suppressed|dropped_expired|failed
  delivered_at INTEGER, channel TEXT, attempts INTEGER NOT NULL DEFAULT 0);

CREATE TABLE alerts(            -- 告警去重/抑制（T11 落地，见 ADR-0019）
  id INTEGER PRIMARY KEY, alert_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  count INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL,  -- firing|resolved
  last_sent_at INTEGER, resolved_at INTEGER);

CREATE TABLE audit_log(         -- 一切人工/自动写操作留痕
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, target TEXT, detail_json TEXT);

CREATE TABLE config(            -- 后台可视化配置（≤60s 生效）
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL, updated_by TEXT NOT NULL);
```

### 5.2 原始弹幕记录契约（JSONL，只增不改）

每行一条，UTF-8，字段固定：

```json
{"ts":1758451200123,"platform":"huya","room_id":"660000","match_id":null,
 "user_hash":"3f9a…","text":"这波团开得太急了","extra":{"level":12,"badge":null}}
```

- `user_hash`：平台用户 ID 的加盐哈希（**不落明文身份**，满足隐私要求）。
- 落盘路径：`~/danmu-intel-data/raw/<platform>/<yyyy-mm-dd>/<room_id>-<hh>.jsonl`。
- 文件封存时计算 SHA256 写入 `danmu_segments`；写入用 `O_APPEND`，崩溃最多丢最后一行。

### 5.3 保留与归档

| 数据 | 在线期 | 到期处理 |
|---|---|---|
| 原始弹幕 JSONL | 6 个月（需求 NFR-D） | 压缩为 `.jsonl.zst` 迁至 NAS，DB 索引行保留（rel_path 改指向归档挂载点） |
| 切片/统计/报告/订单/会员/审计 | 长期 | 不删（报告与订单是账本） |
| 统计原始事件 | 90 天 | 汇总入 `stats_daily` 后删除明细 |

> **落地（T13，见 ADR-0021）**：NAS 由**挂载点**接入 —— 把共享挂到 `<data>/archive`，
> 归档根默认必须是独立挂载点（`st_dev` 不同），否则归档命令拒绝执行（`--allow-same-disk`
> 只给演练/测试）；压缩用 stdlib `compression.zstd`（Python 3.14 起自带），因此
> `requires-python >= 3.14`（本项目运行环境就是 3.14）。索引行的 `rel_path` 归档后指向
> `archive/…jsonl.zst`，而 `sha256` **仍是压缩前的整文件摘要**（封存值）；两个地址
> （`raw/A.jsonl` ↔ `archive/A.jsonl.zst`）互为纯函数，所以报告里冻结的在线引用在归档后
> 照样复核得过（NFR-D-4），发布检查不受影响。归档只碰 `danmu_segments` 与 `audit_log`
> （切片/统计/报告/订单/会员/审计长期不删）；统计明细的 90 天汇总仍是 T10 的
> `site-stats --prune`。

---

## 6. 目录结构

```
danmu-intel/
├── AGENTS.md
├── CONTEXT.md                      ← 术语表 + 领域约定
├── docs/
│   ├── requirements/DANMU_INTEL_REQUIREMENTS.md
│   ├── design/ENGINEERING_DESIGN_v2.md     ← 本文
│   ├── adr/0001..0010-*.md
│   └── agents/{domain,issue-tracker,triage-labels}.md
├── src/danmu_intel/
│   ├── collect/     adapter 基类 + huya/soop 适配器 + 调度器 + 房间进程管理
│   ├── slice/       切片（三级边界 + 冲突记录 + 人工修正）
│   ├── stats/       规则统计（纯函数集）+ 灰信号 + 终局判定
│   ├── report/      十一段组装 + 解读层（LLM 客户端/受约束提示/成本闸）+ 规则直出兜底
│   ├── publish/     产物生成 + 6 项检查 + 原子替换 + 回滚
│   ├── billing/     下单/地址派生/链上监听/开通（幂等）+ 对账
│   ├── site/        FastAPI 应用（公开接口 + 后台）+ Jinja2 模板
│   ├── notify/      通知投递 + 时效闸门 + 告警抑制
│   └── common/      DB、配置（含 60s 缓存）、日志、健康心跳、时间源
├── tests/           unit / contract / e2e（含 fixture 回放）
├── site/            生成产物（提交入库 → Vercel）
├── deploy/          systemd unit、Funnel 配置、环境模板（无凭据）
└── prompts/         报告各段的提示词模板（版本化）
```

**仓库外**（不进 git）：`~/danmu-intel-data/{db.sqlite3,raw/,archive/,llm-logs/}`、`.env`（凭据，仅本机）。

---

## 7. C1 采集层设计

### 7.1 适配器契约（新增平台不改动已有逻辑 —— FR-C1-2 的落点）

```python
class Adapter(Protocol):
    platform: str                                   # "huya" | "soop" | "twitch" | "kick"
    def parse_room(self, url: str) -> RoomKey: ...   # url → (platform, room_id)
    async def stream(self, room: RoomKey) -> AsyncIterator[DanmuEvent]: ...
    async def probe(self, room: RoomKey) -> Probe: ...   # 是否在播/标题/主播名
```

- 事件是统一的 `DanmuEvent(ts, platform, room_id, user_hash, text, extra)`，**适配器只负责"平台原始 payload → DanmuEvent"**，其余全部共性逻辑在采集器。
- 注册表：`ADAPTERS: dict[str, Adapter]`，新增平台 = 新增一个模块 + 注册一行；已有平台代码零改动。
- **契约测试**（`tests/contract/test_adapter_contract.py`）对每个适配器跑同一套断言：字段齐备、时间单调、非法 payload 不崩、断流触发重连。这是 FR-C1-2「不得改动已有平台逻辑」的可验证防线。

### 7.2 直播间发现（三级优先级 —— FR-C1-3）

1. **人工登记**（后台维护，最高优先）：写 `rooms.discovered_by='manual'`。
2. **赛程关联**：比赛进入 `scheduled` 后按官方赛程中的直播信息自动补齐；无官方直播链接时留空，不猜。
3. **候选池**：常用直播间池（`config` 表内维护），仅作候选，**不与已选用房间重复**。
- 去重键 `UNIQUE(platform, room_id)`；同一声明出现在多级时按优先级落库并记录来源。

### 7.3 起止判定（FR-C1-4/5）

| 状态 | 进入条件 | 退出条件 |
|---|---|---|
| `connecting` | 进程启动 | 首条弹幕到达 → `running`；120s 无首条 → `no_stream` |
| `running` | 收到弹幕 | 60s 无弹幕 → `stalled`（触发重连） |
| `stalled` | 静默超时 | 重连成功 → `running`；连续 5 次失败 → 报警 |
| `ended` | 比赛状态机 `ended` 且静默 ≥10 分钟 | —— |

**收尾判定优先用比赛状态机，而不是"某个主播下线"**——同一场比赛常有多个解说房间，单个房间下线不代表比赛结束。

### 7.4 进程模型与容错

- **一房间一子进程**（不是 asyncio task）：平台 SDK 多有不可控的阻塞/泄漏，进程隔离让单房间崩溃不影响全局，也让"重启"语义干净。
- 采集器主进程只做：调度（哪些房间该跑）、子进程管理（启动/监控/重启/收尸）、心跳与统计落库。**主进程不碰网络**，因此它崩的概率极低。
- 重启退避：1s → 2s → 4s → … → 60s 上限；同一房间 30 分钟内重启 >5 次 → 停止重试 + 报警（防雪崩）。
- 心跳：子进程每 5s 更新 `room_sessions.last_msg_at` + 写 `runtime/heartbeat/<session>.json`；主进程 15s 检查一次，超时按 7.3 处理（满足 NFR-A-4 自动恢复）。
- 写入：批量 flush（每 1s 或累计 64 条），`O_APPEND`；崩溃最多丢最后一批并在重启后补记一条 `gap` 事实（**不假装没丢**）。

### 7.5 观测与失败要响（C9 的输入）

每房间曝光字段：`state / last_msg_at / msg_count / reconnects / drop_count / queue_depth`。触发告警的事件：进程退出、重启超限、静默超时、丢包率超阈值、磁盘可用 < 5GB、平台接口返回限速。

---

## 8. C2 切片层设计

### 8.1 边界来源与优先级（FR-C2 的核心）

| 优先级 | 来源 | 判定方式 |
|---|---|---|
| 1 | `official` | 官方赛程/比分数据中的小局起止时间 |
| 2 | `danmu_signal` | 弹幕信号复核：开局/收局语义的词法模式（如"开了/结束了/GG"类）+ 密度骤变，**需 ≥2 类相互独立信号** |
| 3 | `report_window` | 已发布报告里记录的时间窗口（回填历史比赛时用） |
| 4 | `manual` | 人工修正（后台操作，永远记录操作者与理由） |

- **冲突必记录**：任何一次"多来源给出不同边界"，都要把差异写进 `slices.conflict_note`，即使高优先级胜出。**这是"不丢证据"在切片层的落点**（需求明确要求冲突时以高优先级为准并记录冲突事实）。
- 每个切片必须带 `boundary_source`，页面展示时如实标注来源类型。
- **人工修正留痕**：`override_by/at/reason` 三字段必填，修正后自动重算该场统计（`metrics.algo_version` 递增），旧版本不删。

### 8.2 切片粒度

`(match_id, game_no)` 为唯一键；同时保留**宏观阶段**（赛前/BP/局中/局间/赛后）作为统计的分组维度，不另建表。

---

## 9. C3 规则统计层设计

**铁律：本层全部是纯函数**——输入（事件列表 + 切片 + 源码版本）→ 输出（`metrics` 行）。不读时钟、不读网络、不读全局状态。这条铁律同时买到三件事：可重算（证据可回溯）、可测试（≥90% 覆盖率的载体）、可并行。

### 9.1 指标与算法

| 指标 | 算法（编号步骤） |
|---|---|
| 密度曲线 | ① 窗口 60s、步长 30s 滑窗；② 逐窗计数；③ 输出 `[(t_start, count)]` |
| 峰值 | ① 取密度曲线；② 判定 `count > mean + 3σ` 或 `count > 绝对阈值`；③ 输出峰值时刻 + 窗口内容摘要 |
| 比分/小局结果 | ① 取官方数据；② 与弹幕信号交叉校验；③ 不一致时**以官方为准并记录差异** |
| 击杀时间轴 | ① 官方事件序列（若缺）② 弹幕信号抽取；③ 输出 `[(t, side, note)]` + 来源标记 |
| 双方指标对比 | 按小局聚合上述指标，双侧并列 |
| 灰信号候选 | ① 关键词表命中；② 门槛：命中 ≥N 次 **且** 独立用户 ≥M 人 **且** 覆盖 ≥K 个时段；③ 附样本（原文 + 时间） |
| 终局信号 | 见 9.2 |

**灰信号 6 条硬约束的架构强制**（需求 6.5）：
1. 只作风险提示 → 数据模型里字段名就是 `gray_signals`（不是 `cheat_*`），状态机只有 `candidate|escalated|discarded`；
2. 不指控、不点名 → **渲染层禁止输出任何用户名**（`user_hash` 只用于去重计数，永不展示）；
3. 须附样本 → `samples_json` 非空是落库前置校验；
4. 多人多时段门槛 → 门槛参数写在 `config` 表，改动留审计；
5. 不得用于勒索/威胁/交易 → 不提供任何导出接口、不进付费内容、不生成对外文件；
6. 不达标即 `discarded` 且留原因。

### 9.2 终局判定（需求 6.4）

**≥3 类相互独立信号同时成立，且此后 2 分钟内无反转。** 信号类别（互不依赖）：
1. 官方数据宣布结果；2. 收局语义弹幕密度骤增；3. 长时间静默（≥10 分钟）；4. 主播/官方宣告文本。
- 反转窗口：首次满足 3 类后开 120s 计时器，期间任一信号失效 → 撤销并记录"曾判定/已撤销"事实。
- **宁可不判，不可误判**：不确定时保持 `live`，由人工在后台确认。

### 9.3 重算与版本

`algo_version` 随算法变更递增；重算写新行，不覆盖旧行。任何报告都能反查到"它当时用的是哪版算法 + 哪份切片"。

---

## 10. C4 报告生成层设计

### 10.1 十一段固定结构（需求 6.6 —— 顺序与标题不可增删）

`content_json` 以段为单位存储：`[{no:0,title:"比赛信息",kind:"fact",body:"…",sources:[…]}, …]`。

| 段 | 标题 | 内容性质（需求原文） |
|---|---|---|
| 0 | 比赛信息 | 事实 |
| 1 | 结果总览 | 事实 |
| 2 | 逐局复盘 | 事实 + 解读 |
| 3 | 队伍画像 | **解读** |
| 4 | 人员画像 | **解读** |
| 5 | 灰信号汇总 | 事实（风险提示） |
| 6 | 联赛规律与版本 | **解读** |
| 7 | 预测验证 | 事实 + 解读 |
| 8 | 盘口讨论 | **解读** |
| 9 | 情报含义与后续观察点 | **解读** |
| 10 | 数据与溯源 | 事实 |

- `kind` 取值即上表「内容性质」：`fact` / `fact+interpretation` / `interpretation` / `fact(gray)`。
- **缺段即报告不合格**；**纯解读段（3/4/6/8/9）缺失同样不得发布**（需求明确"解读不可省略"→ AC-16）。

### 10.2 三个交付时点

| 类型 | 触发 | 时效（NFR-T） | 段落范围 |
|---|---|---|---|
| `live_brief` 赛中快报 | 关键节点结束事件 | **2 分钟内** | 核心事实段（结果/关键局/关键数据） |
| `full` 完整版 | 比赛结束 | **10 分钟内** | 全十一段 |
| `review` 赛后复盘 | 比赛结束 | **15 分钟内** | 全十一段 + 预测验证 + 数据修正 |

时效预算拆解（以 `live_brief` 的 120s 为例）：统计就绪 ≤20s → 事实层组装 ≤10s → LLM 解读 ≤25s（单次超时）→ 校验 ≤5s → 渲染 ≤5s → 发布检查 ≤10s → 提交/部署 ≤40s。**预算表进代码为常量并被测试断言**，避免"事后才发现来不及"。

### 10.3 事实层与解读层的强制分离（需求 Q-1 的落点）

- 解读层**输入只有事实层产物**：`fact_layer_hash` 记录输入指纹，进报告溯源段。
- 提示词模板版本化（`prompts/`），输出受 JSON Schema 约束（段 id → 文本）。
- **反幻觉后置校验**（不靠提示词自觉）：① 抽取解读文本中的数字/选手名/比分；② 逐个检查是否出现在事实层输入中；③ 出现新事实 → 该段丢弃重试 1 次 → 再失败则整段降级为规则直出文本并标记 `llm_state='rule_fallback'`。
- 事实段与解读段的渲染样式与标注在页面上必须可区分（读者需要知道哪些是事实、哪些是解读）。

### 10.4 成本闸与降级（ADR-0003 的落点）

- 逐次调用记账：`model / prompt_tokens / completion_tokens / cost_cny / latency_ms`。
- **单场 ≤ ¥0.3、每日 ≤ ¥10 硬闸**；触及任一 → 立即转规则直出（`llm_state='rule_fallback'`）+ 报警。
- 长时间 LLM 不可用（连续 3 次失败）→ 全局降级并在站点显示"解读能力降级"状态（**不静默降级**）。

---

## 11. C5 发布层设计

### 11.1 产物与付费边界（ADR-0001 的落点）

- 静态产物（Vercel）：`index.html`、赛程/历史库/画像库/灰信号汇总/验证闭环/订阅页，以及每场比赛的**免费部分**（赛后复盘对所有人公开 —— 需求 Q-6/FR-C5-10）。
- **付费内容不写进静态文件**：赛中快报与完整版的付费段由后端 `GET /api/report/<id>/paid` 在凭据校验通过后返回。原因：静态文件一旦上线，任何人 `curl` 即可白嫖，付费边界形同虚设（蓝本的 JS 遮罩式付费墙正是这个缺陷）。
- 免费/付费判定**只由比赛状态机驱动**（ADR-0009）：`state != ended` → 付费；`state == ended` → 自动再发布为公开版。**永不依赖文件名/路径正则**。

### 11.2 发布前 6 项检查（需求 6.8 —— 任一项不通过即不得发布，全部实现为纯函数）

| # | 检查（需求原文） | 判定 |
|---|---|---|
| 1 | 全站导航唯一，无重复导航项 | 无重复条目、无孤儿链接 |
| 2 | 无旧模板残留内容 | 模板指纹（旧版占位符/废弃组件）计数为 0 |
| 3 | 应受付费墙保护的页面，付费墙齐全；应公开的页面无付费墙 | 逐页对照比赛状态机结论（6.7）——**不看文件名/路径** |
| 4 | 报告分段完整（对照 6.6 的十一段），无缺段 | 十一段齐备、无空段、纯解读段不得缺失 |
| 5 | 页面 × 联赛 × 标识的关联一致 | 跨表引用可解析（比赛↔联赛↔队伍/选手） |
| 6 | 无「速览卡」类残留物 | 速览卡组件/模板指纹计数为 0 |

**额外加固检查（不在需求 6 项之列，但失败同样中止发布）**：报告来源引用可达性（每个来源文件存在且 SHA256 匹配）——它是 AC-17/AC-13 溯源性在发布时刻的守卫。

**任一项不通过 → 中止发布，保留上一版可用的站点，报警**（需求 6.8 末句 + NFR-A-1）。

### 11.3 原子发布与秒级回滚

1. 生成到 `site/.staging/`（不触碰线上产物）。
2. 跑 6 项检查（§11.2）；失败即中止。
3. 原子替换：`os.replace` 目录交换（同分区，瞬时）；写 `releases` 行（`state='live'`）。
4. 提交并推送 → Vercel 构建部署；记 `deploy_ref`。
5. **回滚**：后台一个按钮 → ① 调 Vercel instant rollback（秒级，`deploy_ref` 指回上一版）② `git revert` 异步跟进（保持"仓库=线上"的账本一致）。回滚动作写 `audit_log`。

### 11.4 站点统计（自建，不引入第三方）

- 前端 beacon → `POST /api/stats/beacon`（同域/Funnel）。
- 独立访客口径：`sha256(每日盐 + IP + UA)`，**每日换盐**，故不可跨日追踪同一人；不采集跨站信息、不加载任何第三方脚本（满足隐私要求）。
- 留资与付费关联：beacon 携带 `member_id`（仅在已登录凭据下），使"访问付费页→下单→付款"漏斗可算。

---

## 12. C6 会员与付费设计

### 12.1 档位与价格

- 档位：**标准档** + **试用档**（需求 Q-6 已定）。价格与时长存 `config` 表（后台可改，1 分钟内生效）——**具体数值属开放项**（§20），不写死在代码里。
- 试用档限制：同一 `(contact_platform, username_norm)` 只能享受一次（数据库唯一性 + 状态检查）。

### 12.2 全自助开通流程（ADR-0006；对应 AC-3）

```
①用户在订阅页选档 → 填既有通讯账号（平台 + 用户名，如 Telegram @name / QQ 号）
②规范化 + 校验格式；若该账号已有 pending 订单 → 复用该订单（不生成新地址）
③生成订单 orders(public_ref, network, address, memo, amount_due_units, expires_at=now+30min)
   · Polygon：从 xpub 派生下一个索引地址（watch-only，只存地址与索引）
   · Solana ：使用单一收款地址 + memo = public_ref（每单唯一标识）
④页面展示：地址 / 金额（金额含唯一小额尾数，用于同额多单区分）/ 二维码 / 剩余时间
⑤chain-watcher 每 60s 轮询 + 启动时补扫 → 按 (地址|memo, 金额) 匹配订单
⑥足额 → orders.status='paid'，记录 tx_ref → 触发开通（幂等）
⑦不足 → orders.status='short' + shortage_units + 通知差额（不静默失败）
⑧30 分钟未付 → 'expired'；**已公开的派生地址不再分配给他人**（防串单），索引只前进不回退
```

### 12.3 幂等与对账（AC-4 / AC-5）

- **开通幂等**：`orders.tx_ref` 唯一约束 + 开通函数以 `(tx_ref)` 为幂等键；重复检测同一笔付款只延期一次。
- **到期延期语义**：续费从**原到期日**顺延（AC-6），不是从付款日起算。
- **补扫**（AC-5）：`chain_cursors` 记录每个 scope 的最后游标；watcher 启动、检测失败恢复、以及后台"立即补扫"按钮，都从游标向后扫。另外保留"按订单地址独立查历史"的路径（不依赖游标）——两条独立路径互为兜底。
- **人工补开通**：后台按钮，**必填理由**，写 `audit_log`（AC-5 后半段）。

### 12.4 防枚举与隐私（AC-10 / AC-18 / NFR-P-2）

- 校验接口 `POST /api/verify`：对「账号不存在」「账号存在但未开通」「账号已过期」**返回完全相同的响应体与状态码**，仅凭随机生成的凭据区分成功路径。
- 限流：按 IP 与按账号双维度（`NFR-S-2`），触发限流返回与失败同样的响应形态（不给攻击者额外信息）。
- 凭据形态：领取时生成 32 字节随机码，**只存哈希**；下发 HttpOnly + Secure + SameSite=Lax 的 cookie；可重发（凭通讯账号 + 限流）。
- 会员身份对外零泄露（NFR-P-1）：任何公开页面、统计、导出都不含用户名。

### 12.5 资金安全（AC-12 / NFR-S-1/5）

- 系统内**只有** xpub + 派生地址 + memo + 收款地址；**永不出现**私钥、助记词、keystore、交易所 API key（FR-C6-17/18/19）。
- 收款网络白名单枚举，仅 `polygon` / `solana`（FR-C6-3；明确排除 Base）。
- 凭据只允许存在于仓库外 `.env`（0600）；**pre-commit 检查 + 测试**双重阻止其进入版本库（NFR-S-4，见 §14.4）。
- 一切资金相关写操作（开通/延期/撤权/人工补开通）入 `audit_log`，只追加不可改（NFR-S-5）。

### 12.6 到期、宽限与提醒

- 状态机：`active` →（到期）`grace`（默认 24h，可配置，宽限期内仍可访问 —— AC-6）→ `expired` → 降级为访客。
- 提醒：到期前 3 天 / 1 天 / 当日各一次（走通知通道，去重）。**具体提醒档位属开放项**（需求未规定）。

### 12.7 链上监听与额度（ADR-0005；AC-11 的一半）

| 链 | 数据源 | 免费额度（实测） | 60s 轮询月用量 | 补扫方式 |
|---|---|---|---|---|
| Polygon | Polygonscan API | 5 calls/s、10 万 calls/天、1000 条/次 | ~4.3 万次/月 | 按地址查 txlist + 区块范围 |
| Solana | Helius | 1M credits/月、10 req/s | ~8.6 万次/月 | `getSignaturesForAddress` 向前翻页 |

- 每次调用记 `quota_usage`；**用量 >80% 或收到限速响应 → 报警**（FR-C6-11）。
- 后台可查当日/当月成本与额度（NFR-C-3）。

---

## 13. C7 站点统计（自建）

| 维度 | 设计 |
|---|---|
| 采集 | 前端 beacon → `POST /api/stats/beacon`（无第三方脚本、无跨站跟踪） |
| 独立访客 | `sha256(每日盐 + IP + UA)`，**每日换盐** → 可算"某天多少人"，不可跨日还原同一人（AC-9） |
| 可回答 | 某天访问量、访问付费页人数、下单转化、付费转化、留资数 |
| 不可回答 | "具体是谁"（除非其主动留资或付费 —— AC-9 的"除非"） |
| 保留 | 明细 90 天 → 汇总入 `stats_daily` |

---

## 14. C8 后台设计（AC-7 / NFR-S-3）

### 14.1 页面清单

概览（进程健康/比赛状态/待办）· 比赛管理（状态机操作）· 房间与数据源（含人工登记、候选池）· 切片复核（改边界 + 必填理由）· 灰信号评审（escalate/discard + 理由）· 报告（预览/重生成/发布）· 发布与回滚（6 项检查结果 + 回滚按钮）· 会员与订单（搜索/人工开通/撤权）· 通知与告警 · 配置（关键词表/门槛/价格/时效预算/提醒档位）· 审计日志 · 成本与额度。

### 14.2 隔离与鉴权

- 独立进程 + **只监听 tailnet 接口**（不挂 Funnel）；非 tailnet 网络不可达（NFR-S-3）。
- 单管理员一类角色（需求 Q-8）；口令以哈希存于仓库外 `.env`；登录态为短期签名 cookie。
- 所有写操作写 `audit_log`（actor = admin）。

### 14.3 配置 1 分钟生效（NFR-T-4）

`config` 读取走进程内 60s TTL 缓存；后台保存后立即失效本进程缓存。**采集器子进程**通过"配置版本号"感知变更：主进程每次调度循环读取版本号，变化则重启受影响的房间子进程（配置改动 → 1 分钟内生效的硬保证）。

### 14.4 凭据不进库的自动防线（NFR-S-4）

1. `tools/check_no_secrets.py`：按模式扫描（`0x`+64 hex 私钥、`-----BEGIN ... PRIVATE KEY-----`、常见 API key 前缀、`.env` 文件名），命中即非零退出。
2. git `pre-commit` 钩子调用它（仓库内 `deploy/hooks/pre-commit`，安装脚本写入 `.git/hooks/`）。
3. 测试用例：往临时文件塞假私钥 → 断言检查器报错（防止防线本身失效）。

---

## 15. C9 通知与报警设计（AC-11 / NFR-T-5）

| # | 事件 | 触发条件 | 级别 | 通道 |
|---|---|---|---|---|
| 1 | 采集进程退出 / 重启超限 | 子进程退出且重启 >5 次/30min | 高 | QQ Bot + TG |
| 2 | 采集静默 | 房间 `stalled` > 5 分钟 | 中 | QQ Bot |
| 3 | 落盘丢包率超阈 | `drop_count / msg_count > 2%` | 中 | QQ Bot |
| 4 | 磁盘将满 | 可用 < 5GB | 高 | QQ Bot + TG |
| 5 | 发布失败 / 检查不通过 | 任一检查项 false | 高 | QQ Bot + TG |
| 6 | 链上检测异常 / 额度受限 | 限速响应或用量 >80% | 高 | QQ Bot + TG |
| 7 | 付款到账但开通失败 | 开通流程异常 | 高 | QQ Bot + TG |
| 8 | 订单待补款 | `status='short'` | 中 | QQ Bot |
| 9 | LLM 降级 / 成本超闸 | `llm_state='rule_fallback'` 或超 ¥0.3/¥10 | 中 | QQ Bot |
| 10 | 成员批处理失败 | 到期降级任务异常 | 中 | QQ Bot |

**5 分钟时效闸门（需求明示，覆盖所有类型）**：通知入库即 `created_at`；`notifier` 每 30s 扫描；超过 5 分钟未送达 → `state='dropped_expired'`（**销毁，不补发**）；尝试失败重试 2 次（间隔 30s）后同样销毁。
**抑制与去重**：同 `alert_key` 在冷却期（默认 15 分钟）内只发一次；恢复时发送一次恢复通知。

> 落地细节（被抑制的通知写 `state='suppressed'` 留痕、`alert_key` 由 kind + 身份字段拼出、
> 冷却期从最后一次送达起算、通道降级与高危冗余、断网留痕到销毁）见 ADR-0019。

---

## 16. 非功能需求的逐条落点

| 编号 | 要求（摘要） | 设计落点 |
|---|---|---|
| NFR-T-1/2/3 | 快报 2min / 完整 10min / 复盘 15min | §10.2 预算表 + 测试断言；超时即降级而非延迟 |
| NFR-T-4 | 配置 1 分钟生效 | §14.3 版本号 + 缓存失效 + 子进程重启 |
| NFR-T-5 | 报警 5 分钟送达 + 超时丢弃 | §15 时效闸门 |
| NFR-C-1/2 | 每份数分钱、每晚数元；随场次线性 | §10.4 成本闸 + 记账；无累积型成本（纯函数重算不调 LLM、历史不重复付费） |
| NFR-C-3 | 随时查看当日/当月成本 | 后台"成本与额度"页（`llm_calls` + `quota_usage`） |
| NFR-Q-1 | 事实零错误 | 事实层只来自官方/规则统计，LLM 不产事实；数字后置校验（§10.3） |
| NFR-Q-2 | 结构稳定 | 段序固定 0–10 + `prompts/` 版本化 + 发布前第 4 项检查 |
| NFR-Q-3 | 解读明确标注 | 每段 `kind` 字段 + 页面样式与标注区分 |
| NFR-Q-4 | 可读 | 提示词约束 + 人工抽检（后台） |
| NFR-Q-5 | 解读不引新事实 | §10.3 反幻觉后置校验（数字/名字比对） |
| NFR-A-1 | 发布失败不导致站点不可用 | §11.3 staging + 保留上一版 |
| NFR-A-2 | 手机可读、首屏不慢 | 静态页 + 轻量 CSS、无外部脚本、图片懒加载 |
| NFR-A-3 | 单机 | ADR-0001 |
| NFR-A-4 | 自动恢复 | systemd `Restart=always` + 心跳 + 重启退避（§7.4） |
| NFR-A-5 | 不做备份/恢复演练 | 明确不实现；风险由运营者接受（对应 §9 范围外 + Q-7） |
| NFR-S-1 | 不出现可动用凭据 | §12.5 + §14.4 + AC-12 全库检索 |
| NFR-S-2 | 写接口鉴权 + 限流 | 校验/领码/下单/beacon 全部限流（§12.4） |
| NFR-S-3 | 后台与公开面隔离 | §14.2（仅 tailnet） |
| NFR-S-4 | 凭据不入库 + 自动检查 | §14.4 |
| NFR-S-5 | 资金操作不可篡改审计 | `audit_log` 只追加（§12.5） |
| NFR-P-1/2 | 不泄露会员身份；不可枚举 | §12.4 |
| NFR-P-3/4 | 无跨站跟踪、不交第三方；留资仅用于订阅 | §13；留资不与统计明细混存 |
| NFR-L-1 | 只用公开弹幕 | 适配器只读公开接口，不出现在突破访问限制的手段 |
| NFR-L-2 | 灰信号 6 条约束 | §9.1 六条架构强制 |
| NFR-L-3/4 | 标明取材范围；预测与结果公开对照（含失败） | 报告第 10 段 + 「验证闭环」页（含预测失败场次） |
| NFR-M-* | 不保留兼容层、分层生长 | AGENTS.md 原则；`algo_version` + 不做 migration |
| NFR-GA / AC-14 | 覆盖率 ≥90%，无外网可跑 | §18 测试缝 + fixture 回放；CI 门禁 |
| NFR-D-* | 原始记录在线 6 个月、归档可调取 | §5.3；归档件保留校验和与索引 |

---

## 17. 验收标准 → 设计落点与验证方式

| AC | 验收标准（需求原文摘要） | 设计落点 | 验证方式 |
|---|---|---|---|
| AC-1 | 已结束比赛产出合格报告并按时上线 | §10 全章 | e2e fixture：录制的弹幕 + 官方数据 → 断言十一段 + 时效 |
| AC-2 | 进行中会员可见、访客不可见；结束后自动公开 | §11.1 | 状态机测试：`live`→`ended` 断言内容可见性翻转 |
| AC-3 | 订阅→访问权全程无人工 | §12.2 | e2e：假链上事件 → 断言会员状态与凭据下发 |
| AC-4 | 重复检测只开通一次 | §12.3 | 同一 `tx_ref` 连投 3 次 → 断言仅一次延期 |
| AC-5 | 漏检后补扫可发现；管理员可人工补开通 | §12.3 | 跳游标 + 补扫断言；人工路径断言 `audit_log` |
| AC-6 | 到期降级、宽限期可访问、续费顺延 | §12.6 | 时间注入（可控时钟）→ 断言状态与 `expires_at` |
| AC-7 | 非管理员入口不可达；配置 1 分钟生效 | §14 | 非 tailnet 请求断言 403/无响应；改配置断言 60s 内生效 |
| AC-8 | 检查不通过则拒绝发布且站点可用；可立即回滚 | §11.2/§11.3 | 注入失败项 → 断言旧版仍在；回滚断言 `deploy_ref` 回退 |
| AC-9 | 统计能答"某天多少人访问付费页"，不能答"是谁" | §13 | 断言 API 无 IP/身份字段；跨日盐不同 |
| AC-10 | 校验接口对"不存在"与"未开通"不可区分 | §12.4 | 两请求响应逐字节比对 |
| AC-11 | 五类异常均 5 分钟内送达报警 | §15 | 逐事件注入 → 断言投递；含超时销毁用例 |
| AC-12 | 全库零命中可动用凭据 | §12.5/§14.4 | `check_no_secrets.py` 全库扫描 + 测试用例 |
| AC-13 | 删统计后仅凭原始记录 + 配置可重算同样结果 | §9 纯函数铁律 | 重算断言逐字节相等（含浮点规整） |
| AC-14 | 覆盖率 ≥90% 且无外网可跑 | §18 | CI：`pytest --cov` 门禁 90% + 断网运行 |
| AC-15 | 多房间跨平台不重不漏，能说明每房间贡献量 | §7.2/§7.3 | 双房间 fixture 回放 → 断言去重与按房间计数 |
| AC-16 | 缺解读段不得发布；解读中数字都能溯源 | §10.1/§10.3 | 抽掉解读段断言发布失败；数字溯源断言 |
| AC-17 | 6 个月后归档、归档可调取、溯源仍可核验 | §5.3 | 归档 e2e：移动文件 + 断言索引与校验和可用 |
| AC-18 | 仅凭既有通讯账号标识校验，无需注册 | §12.2/§12.4 | e2e：仅给用户名 → 断言可领取；无注册入口 |

---

## 18. 测试缝（seams）

> `to-spec` 要求先与你确认测试缝。以下为提案 —— **请确认或修正**。原则：缝尽可能少、尽可能高（在系统的外边界上测）。

| # | 缝 | 位置 | 覆盖 | 为什么在这里 |
|---|---|---|---|---|
| S1 | **适配器契约缝** | 适配器接口（平台原始 payload → `DanmuEvent`） | C1 全部 + AC-15 | 唯一的平台相关边界；新增平台只加一个契约测试，不动其他缝 |
| S2 | **纯函数缝（规则统计/切片）** | `facts = f(events, slices, algo_version)` | C2/C3 + AC-13/AC-16 | 无 I/O、无时钟 → 覆盖率与重算性的主要载体 |
| S3 | **支付决策缝** | `decision = f(observed_txs, orders)` | C6（幂等/补款/补扫）+ AC-3/4/5 | 链上不确定性被压成纯数据输入，测试无需真链 |
| S4 | **HTTP 边界缝** | FastAPI `TestClient` + 内存 SQLite | 校验/统计/下单/后台 + AC-7/9/10/18 | 最高缝：鉴权、限流、不可区分性都在这层可验 |
| S5 | **发布缝** | `checks = f(site_dir, match_states)` | C5 + AC-8/16 | 6 项检查是实现为纯函数，可离线对着 fixture 产物跑 |

**不新建的缝**：不为 LLM 单独造抽象层——用"注入一个假客户端"在 S2/S4 里替身即可（避免为测试引入生产抽象）。

---

## 19. 实施分层（每层结束都是可运行的完整产品）

| 层 | 目标 | 产物 | 验收门 |
|---|---|---|---|
| M1 | 采集跑通单平台单房间 | 采集器 + 虎牙适配器 + 落盘 + 心跳 | S1 契约测试 + 真实房间 1 小时无断流 |
| M2 | 切片 + 规则统计 + 规则直出报告 | 纯函数层 + 兜底渲染器 + 站点骨架 | S2 + AC-13 重算 + 覆盖率 ≥90% |
| M3 | 发布闭环（Vercel + 6 项检查 + 回滚 + 转公开） | 发布器 + Funnel API + 后台只读 | S5 + AC-8 + AC-2 |
| M4 | 会员付费闭环（xpub/memo + 监听 + 自助开通） | billing + chain-watcher + 订阅页 | S3 + S4 + AC-3/4/5/6/10/18 |
| M5 | 解读层（LLM 受约束 + 成本闸 + 降级） | reporter + prompts | AC-16 + 成本闸测试 |
| M6 | 后台全量 + 通知报警 + 统计 + 归档 | admin + notify + stats + archive | AC-7/9/11/17 |
| M7 | 第二平台（SOOP）接入 | SOOP 适配器 | S1 复用 + AC-15 跨平台 |

**原则**：任一层的失败都不得让上一层已工作的产品退化为不可用（AGENTS.md「分层生长」）。

---

## 20. 风险与开放项（需你或执行体确认）

| # | 项 | 现状 | 影响 | 建议 |
|---|---|---|---|---|
| O1 | **价格档位具体数值**（标准档价/时长、试用档价/时长） | 需求未规定，蓝图有历史定价材料 | 影响订阅页与订单校验 | 你定数值，或明确继承蓝图定价 |
| O2 | **xpub 从哪来** | ADR-0004 定了方案，但需要你提供 Polygon 收款钱包的 xpub（watch-only 公开信息） | 阻塞 M4 的地址派生 | 你从硬件钱包导出；**只给 xpub，绝不给私钥/助记词** |
| O3 | **Solana 收款地址** | 同上 | 阻塞 M4 | 你提供一个收款地址 |
| O4 | **Vercel 项目与 API token** | 本机无 `vercel` CLI、无项目 | 影响 M3 自动部署与秒级回滚 | 你决定：装 CLI + 建项目；或暂时先"生成产物 + 手动部署"，回滚改为 `git revert` |
| O5 | **Funnel 带宽上限与"加速层"张力** | 需求 §9.9 明确不做加速层；Vercel 静态托管本身含 CDN 能力 | 严格解释下需你裁定边界 | 我的解读：**不自建额外加速层**（不买 CDN、不做缓存层），沿用蓝图已在用的静态托管；请确认 |
| O6 | **到期提醒档位**（提前几天、几次） | 需求未规定 | 影响通知实现 | 建议 3 天/1 天/当日，可后台改 |
| O7 | **灰信号关键词表与门槛数值**（N/M/K） | 需求给了 6 条约束但未给数值 | 影响 C3 输出 | 建议初值由蓝图 `INTEL_RULES_V2.md`/`gray_signals.json` 实证迁移，且门槛进 `config` 可调 |
| O8 | **官方数据源**（比分/赛程/局时间）用哪个接口 | 需求只要求"事实与官方一致" | 影响 C2 第 1 优先级边界与 C3 比分 | 需调研并落 ADR（可用蓝图实证） |
| O9 | **直播平台合规与稳定性** | 虎牙/SOOP 均为公开弹幕 | 可能随时变更协议 | 适配器契约 + 断线重连 + 报警已覆盖；协议变更即重启房间 |

---

## 21. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-09-22 | v1.0 | 初稿（**未走 grill 流程**，多处单方面拍板；已由 v2.0 取代） |
| 2026-09-22 | v2.1 | 按 `to-tickets` 拆出第一波 7 张票（#4–#10，原生阻断边）并新增 §22 |
| 2026-09-22 | v2.0 | 走完 grill-with-docs（三轮共 9 项问答）→ 决策落 `docs/adr/0001–0010`；修正 v1 的 6.8 第 3/6 项误写；补充测试缝、实施分层、开放项；付费内容不进静态产物 |

## 22. 拆票（to-tickets）· 第一波已发布

按 `to-tickets` 规则拆成 **tracer-bullet 垂直切片**（每片横穿各层、可独立验收、单 context window 装得下），发布于 GitHub issue，并用 **GitHub 原生 issue 依赖**设置阻断边（`gh api --method POST repos/twinstony/danmu-intel/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`）。

用户已 review 设计（#3）与 T1（#4）：**T1 已翻 `ready-for-agent`**（唯一 frontier，可开工）；其余 6 张保持 `ready-for-human` —— 被阻断的票因此不会被流水线提前捡起，需用户逐张 review 后再翻。

**结构**：#3 作为第一波的追踪父 issue 保持 OPEN，#4–#10 全部以 **GitHub 原生 sub-issue** 挂在其下（`gh api --method POST repos/twinstony/danmu-intel/issues/3/sub_issues -F sub_issue_id=<child-db-id>`），进度由 `sub_issues_summary` 实时反映；待第一波全部关闭或设计被 v3 取代时再关闭 #3。需求 #2 与设计 #3 保持 OPEN、未作修改（to-tickets 明文规定不得关闭或修改父 issue）。

### 第一波 ·「采集→发布」闭环

| 票 | Issue | Blocked by | 端到端交付物 |
|---|---|---|---|
| T1 采集→静态页最薄闭环 | #4 | 无（frontier） | 虎牙单直播间真弹幕 → JSONL → 手动切片 → 基础统计 → 规则直出十一段静态页 |
| T2 多房间采集与监督 | #5 | #4 | 多房间并发 + 心跳/重连/重启上限 + 健康状态 + 异常事件 |
| T3 SOOP 适配器接入 | #6 | #4 | 契约测试 + 注册表一行接入，已有平台逻辑零改动 |
| T4 切片引擎与统计全集 | #7 | #4 | 边界优先级/冲突/人工修正 + 统计全集 + 终局判定 + 灰信号硬约束 |
| T5 报告三形态与溯源 | #8 | #7 | 快报 2 分钟 / 完整版 10 分钟 / 复盘版 15 分钟 + 事实·解读分层 + SHA256 溯源 |
| T6 解读层 LLM | #9 | #8 | DeepSeek 受约束 + 反幻觉校验 + ¥0.3/场·¥10/天硬闸 + 规则直出降级 |
| T7 发布闭环 | #10 | #8 | 6 项检查 + 原子发布 + 秒级回滚 + 结束自动转公开 + 付费段不进静态产物 |

### 第二波 · 变现与运维（**暂不发布**，等第一波跑通再拆）

| 票 | Blocked by（预计） | 说明 |
|---|---|---|
| T8 链上监听 | 无 | Polygonscan + Helius + 游标 + 补扫 + 额度记账报警 |
| T9 会员付费全自助闭环 | T7, T8 | xpub 派生 / Solana memo / 订单状态机 / 防枚举 / 凭据 |
| T10 站点统计自建 | T7 | beacon + 每日盐 + 口径 |
| T11 通知与报警 | T2 | 统一 5 分钟闸门，覆盖全事件类型 |
| T12 后台 | T5 | 页面 + 权限 + 配置 1 分钟生效 + 审计 |
| T13 归档 | T2 | 6 个月 → NAS，归档后可调取、可核验 |

> 拆票只覆盖第一波是有意为之：设计若在 review 中变更，未发布的第二波不受影响（返工面最小），符合 AGENTS.md「宁缺毋滥、一点一点上」。
