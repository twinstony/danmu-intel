# 弹幕情报库 · 工程设计文档 v1.0（可落地）

> 本仓库 `docs/requirements/DANMU_INTEL_REQUIREMENTS.md` 是**唯一需求来源**。本文档只回答"怎么做"，不重新定义"要什么"。
>
> 状态：设计定稿 · 待执行体认领  
> 日期：2026-09-22  
> 需求输入：Issue #2（需求文档，760 行，§11 八项决策已全部拍板）  
> 蓝图参考：`/home/tony/Workspace/danmu-intel-local`（他人项目，只读，仅作为实现参考，不引入其需求或架构）

---

## 0. 设计总纲

### 0.1 运行环境

| 维度 | 取值 |
|------|------|
| 开发验证机 | 本机 tony-macbookpro（Lubuntu，10.0.0.138 / tailnet 100.118.248.92） |
| 开发语言 | Python 3.11（与蓝图工具链同构，复用其采集/分析模块） |
| 站点托管 | **Vercel 静态部署**（沿用蓝图路线，HTML/MD/JSON 均为构建产物，无服务端运行时） |
| 采集执行 | 本机常驻进程（systemd user session 或 nohup screen，与蓝图 VPS 路线不同——蓝图是 VPS 采集→回传本地→分析发布；本机方案省去回传环节，但需承担直播时段机器必须在线的代价） |
| 解读生成 | **直连 LLM API**（DeepSeek 等），按 token 计费，单场设上限 |
| 首发平台 | **虎牙 + SOOP**（架构层预留 4 平台适配器注册表，Twitch/KICK 留一行接入） |
| 数据库 | **SQLite**（单文件，无外部服务依赖，会员/订阅/配置/审计日志一库化） |
| 在线保留期 | 6 个月（SQLite 分区 + 归档标记） |

### 0.2 总体架构（三链一库一站点）

```
┌────────────────────────── 本机 ──────────────────────────┐
│                                                          │
│  采集链 (Capture Chain)                                   │
│  capture_supervisor.py ─── 房间池 (SQLite rooms 表)       │
│       │                     │                            │
│       ├── HuyaAdapter ── fetch_huya_danmu.py (vendor)     │
│       ├── SOOPAdapter ── fetch_soop_danmu.py              │
│       ├── TwitchAdapter (注册表留位)                       │
│       └── KICKAdapter  (注册表留位)                       │
│       │                                                  │
│       ▼ 原始弹幕 JSONL（只增不改）                        │
│  docs/data/danmu/<platform>/<date>_<source>.jsonl         │
│                                                          │
│  分析链 (Analysis Chain)                                  │
│  danmu_intel.py (规则层)  ──→ runtime/danmu_intel.json   │
│  slice_danmu_by_match.py  ──→ docs/data/danmu/slices/    │
│  verify_match_end.py      ──→ 终局信号                   │
│  build_gray_stats.py      ──→ 灰信号聚合                 │
│  accumulate_team_intel.py ──→ teams.json 画像沉淀        │
│  fetch_official_game_data.py ──→ 官方数据回填            │
│                                                          │
│  情报链 (Intel Chain)                                     │
│  intel_report_generator.py ──→ 调用 LLM API             │
│       │  输入：intel JSON + 切片 + 官方数据 + 画像库       │
│       │  输出：11 段固定结构 HTML（完整版 / 赛中快报）    │
│       ▼                                                  │
│  .danmu_intel_site/intel/                                │
│       │                                                  │
│       ▼ add_paywall.py 注入付费墙                        │
│       ▼ add_site_nav.py 注入导航                         │
│       ▼ publish → git push → Vercel 构建 → HTTPS       │
│                                                          │
│  运营支撑                                                 │
│  SQLite: members / subscriptions / audit_log / config    │
│  admin.py: 后台管理页面（管理员 CRUD + 配置热生效）       │
│  payment_watcher.py: 链上入账监听（Polygon + Solana）     │
│  notify.py: QQ 报警                                      │
│  watchdog.py: 采集健康 + 自检                            │
└──────────────────────────────────────────────────────────┘
```

### 0.3 状态机（比赛生命周期）

```
离线(offline) ──开播检测──▶ 等待弹幕(live_waiting) ──首条弹幕──▶ 采集中(capturing)
    ▲                            │                                │
    │                            │ (120s 无弹幕)                   │ (终局判定 ≥3 类信号)
    │                            ▼                                ▼
    │                       无弹幕告警(live_no_danmu)         已结束(finished)
    │                                                                │
    └────────────── 人工干预 / 下一场 ──────────────────────────────┘
```

- 终局判定（需求 §6.4）：**≥3 类独立信号同时成立** — ① 终结类弹幕高密度聚集 ≥2 分钟 ② 比分与官方一致 ③ 流量降至峰值一成以下 ≥5 分钟 ④ 官方/主播明确宣布。此后 **2 分钟无反转** 才落 `finished`。
- 付费墙判定（需求 §6.7）：**禁止按文件名/路径一刀切**。依据「该场比赛是否已结束」——任一成立即可：结算信息已回填 / 全部小局均标记结束 / 比赛结果已回填。

### 0.4 文件协议交接表

| 上游产物 | 路径模式 | 下游消费者 | 字段契约 |
|----------|----------|------------|----------|
| 原始弹幕 | `docs/data/danmu/<plat>/<date>_<src>.jsonl` | danmu_intel.py, slice_danmu_by_match | `{ts, nick, uid, text, source, room_id}`（蓝图 B4: huya 用 `text`，SOOP 用 `message`，分析层统一兼容） |
| 规则层情报 | `runtime/danmu_intel.json` | intel_report_generator.py, render_fast_intel.py | 见 §3.1 |
| 切片 | `docs/data/danmu/slices/<slug>_<node>.jsonl` | report generator | 时间窗起止 + 弹幕数组 |
| 官方数据 | `docs/data/intel/matches.json` 回填字段 | verify_match_end, report §1/§7 | gameId / score / status |
| 画像库 | `docs/data/intel/teams.json` / `players.json` / `gray_signals.json` | report §3/§4/§5 | 见蓝图 accumulate_*.py |
| 情报 HTML | `.danmu_intel_site/intel/<slug>_<node>.html` | add_paywall / add_site_nav / publish | 11 段固定结构 |
| 会员库 | SQLite `members` 表 | add_paywall 的 verify-member | 见 §3.6 |

---

## 1. 模块详细设计

### 1.1 C1 采集层

#### 1.1.1 多平台适配器架构

**目标**：新增平台不得改动已有平台逻辑（需求 FR-C1-2）。

```python
# adapters/base.py
class DanmuAdapter(ABC):
    @abstractmethod
    async def connect(self, url: str, queue: asyncio.Queue): ...
    @abstractmethod
    async def disconnect(self): ...
    @property
    @abstractmethod
    def platform(self) -> str: ...

# adapters/__init__.py 注册表
ADAPTERS: dict[str, type[DanmuAdapter]] = {
    "huya": HuyaAdapter,      # 蓝图已验证
    "soop": SOOPAdapter,      # 蓝图已验证
    "twitch": TwitchAdapter,  # 留位，第一版不启用
    "kick": KICKAdapter,      # 留位，第一版不启用
}
```

**平台对接详情**（蓝图代码实证）：

| 平台 | 协议 | 鉴权 | 心跳/限速 | 依赖 | 已知坑 |
|------|------|------|-----------|------|--------|
| 虎牙 | real-url WebSocket 客户端（vendor/real-url_danmu） | 匿名，无需 cookie/token | 首条弹幕超时 120s → 告警；连接断线自动重连 | aiohttp, pycryptodome, protobuf==3.20.3（纯 Python 模式） | ① `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` 必须设（蓝图 install.sh）② 离线房间会 NoneType.crash，必须先 HTTP 检测 `liveStatus-on`（蓝图 fetch_huya_danmu.py L94/L145-176）③ 开播后 60s 复查一次，防"卡死未开播"（蓝图 B18 教训） |
| SOOP | 弹幕 API（蓝图字段 `message` 非 `text`） | 匿名 | 未知 | aiohttp | 字段命名与虎牙不一致，分析层统一用 `text=r.get("text", r.get("message",""))` 兼容（蓝图 B4） |
| Twitch | IRC over WebSocket | OAuth token 可选（匿名可读） | 限速更严 | — | 本期不启用，注册表留位 |
| KICK | WebSocket/PubSub | 匿名可读 | — | — | 本期不启用，注册表留位 |

#### 1.1.2 采集进程模型

**沿用蓝图同构设计**：每房间独立 subprocess + supervisor 轮询重启。

```
capture_supervisor.py
  ├── 主进程：读 SQLite rooms 表，按房间启动子进程
  ├── 每房间：python fetch_<plat>_danmu.py --url <url> --out <jsonl> --status <status.json>
  ├── 轮询间隔：5s（蓝图 capture_server.py L116）
  ├── 重启延迟：10s（蓝图 L80）
  ├── 落盘路径：docs/data/danmu/<platform>/<YYYY-MM-DD>_<source>.jsonl
  └── 退出信号：SIGTERM → 子进程 terminate → 5s 轮询内自动拉起
```

**与蓝图的差异**：蓝图是 VPS 7×24 + 本地 rsync 回传；本机方案**省略回传**，采集落盘后直接可被分析链读取。代价：直播时段本机必须在线（含深夜 LCK）。

#### 1.1.3 直播间发现三级优先级（需求 FR-C1-3）

1. **手动配置**（管理员通过后台直接指定 source=url）→ 写入 SQLite `rooms` 表 `source=manual`
2. **赛程关联**（管理员通过后台指定"该直播间解说该场比赛"）→ 写入 `rooms` 表 `source=schedule`，关联 `schedule_id`
3. **候选池**（同一主播多房间去重）→ 写 `rooms` 表 `source=candidate`，同一主播仅保留优先级最高的房间

**去重逻辑**：同一主播名（streamers.json nickname 匹配）只保留一个活跃房间；优先级 manual > schedule > candidate。

#### 1.1.4 采集起止判定（需求 §6.1）

| 判定项 | 实现方式 | 阈值/常量 |
|--------|----------|-----------|
| 开播检测 | HTTP 抓取房间页，检测 `liveStatus-on`（蓝图 fetch_huya_danmu.py L94） | 60s 轮询复查（蓝图 B18 教训） |
| "确为解说该场" | 房间关联 schedule_id + 弹幕关键词匹配（双方队名同时出现 ≥3 条/min） | 1 分钟窗口 |
| 停止=终结表述 | 终结词表命中（见蓝图 SITUATION_KW + 自定义） | 高密度聚集 ≥2 分钟 |
| 停止=讨论下一场 | 下一场队名密集出现 | 连续 1 分钟 |
| 停止=结束时间+静默 | `end_time`（赛程给定或开播后推算）已过 + 弹幕密度 < 峰值 10% | 5 分钟 |

#### 1.1.5 健康状态字段（写入 status.json）

```json
{
  "schema_version": 1,
  "platform": "huya",
  "source": "we957",
  "url": "https://www.huya.com/957",
  "state": "capturing|live_waiting|offline_waiting|live_no_danmu|finished|error",
  "started_at": "2026-09-22T14:00:00+00:00",
  "heartbeat_at": "...",
  "last_message_at": "...",
  "message_count": 1234,
  "warning": null,
  "error": null
}
```

---

### 1.2 C2 切片层

#### 1.2.1 切片边界判定（需求 §6.3 三档优先级）

**蓝本现状**：蓝图有两套互不调用的切片实现（`slice_danmu_by_match.py` 和 `danmu_intel.py`），均无三档优先级与冲突处理（子 agent task-1 确认）。**本期新造统一切片器**。

```python
# slice_engine.py
def determine_boundary(raw_slice, candidates: list[BoundaryCandidate]) -> BoundaryResult:
    """
    优先级：① 官方时间 > ② 弹幕信号复核 > ③ 报告窗口
    冲突时以高优先级为准，并记录 conflict_fact
    """
    official = candidates_by_type(candidates, "official")
    danmu_signal = candidates_by_type(candidates, "danmu_signal")
    report_window = candidates_by_type(candidates, "report_window")
    
    winner = official or danmu_signal or report_window
    return BoundaryResult(
        boundary_at=winner.timestamp,
        source_type=winner.type,
        conflict_fact=build_conflict_note(candidates) if has_conflict(candidates) else None
    )
```

| 优先级 | 输入来源 | 字段 | 说明 |
|--------|----------|------|------|
| ① 官方时间 | matches.json → game_start/game_end | `start_time`, `end_time` | 官方赛程给定的时间窗 |
| ② 弹幕信号 | 终结词表命中 + 比分弹幕共振 + 流量骤降 | 多信号投票 | 至少 2 类信号同时触发 |
| ③ 报告窗口 | 已发布报告的时间窗口 | `report.issued_at` | 兜底，仅在①②均无时启用 |

**边界来源必须记录**：每个切片元数据写入 `source_type`（official / danmu_signal / report_window）+ `conflict_fact`（如有冲突）。

#### 1.2.2 切片元数据结构

```json
{
  "slug": "lck-bfx-t1-2026-08-29",
  "node": "G3",
  "node_label": "第三局",
  "boundary_start": {"at": "...", "source": "official", "conflict": null},
  "boundary_end": {"at": "...", "source": "danmu_signal", "conflict": "官方时间 22:40 但弹幕终结信号 22:38 起"},
  "danmu_count": 5432,
  "sources": ["huya.we957", "soop.lck_cl"],
  "sample_count_by_source": {"huya.we957": 3200, "soop.lck_cl": 2232}
}
```

#### 1.2.3 人工复核修正（需求 FR-C2-5）

管理员可通过后台对切片边界进行修正（拖拽时间轴或输入新时间戳）。修正记录写入 `slice_audit` 表：

```sql
CREATE TABLE slice_audit (
  id INTEGER PRIMARY KEY,
  slice_id TEXT NOT NULL,
  field TEXT NOT NULL,           -- 'boundary_start' | 'boundary_end'
  old_value TEXT,
  new_value TEXT,
  reason TEXT,
  changed_by TEXT,               -- admin identifier
  changed_at TEXT DEFAULT (datetime('now'))
);
```

---

### 1.3 C3 规则统计层

#### 1.3.1 规则层情报 JSON（输出契约）

沿用蓝图 `danmu_intel.py` 的输出结构（task-1 子 agent 实证），扩展队伍特质与官方数据：

```json
{
  "meta": {
    "total": 5432,
    "active_users": 892,
    "window_utc": ["2026-08-29 14:00", "2026-08-29 16:30"],
    "window_cn": ["2026-08-29 22:00", "2026-08-29 00:30+1"],
    "density_per_min": 25.3,
    "sample_status": "ready",
    "sources": ["huya.we957", "soop.lck_cl"],
    "source_breakdown": {"huya.we957": 3200, "soop.lck_cl": 2232}
  },
  "teams": {
    "T1": {"mentions": 800, "pos": 420, "neg": 200, "samples": ["..."]},
    "BFX": {"mentions": 750, "pos": 300, "neg": 350, "samples": ["..."]}
  },
  "players": { "...": "..." },
  "odds_discussion": {"count": 54, "samples": ["..."]},
  "situation": {"count": 120, "samples": ["..."]},
  "gray_signals": {"count": 8, "samples": ["..."], "by_category": {"假赛": 3, "剧本": 2, "...": 3}},
  "team_traits": {
    "categories": {"逆风崩盘": {"count": 12, "samples": ["..."]}},
    "by_entity": {"T1": {"韧性逆转": {"count": 3, "samples": ["..."]}}}
  },
  "density_bursts": [
    {"minute_cn": "22:40", "count": 180, "samples": ["..."], "dominant_theme": "比赛局势"}
  ],
  "bp_discussion": {"count": 45, "samples": ["..."]},
  "top_users": [["user_a", 89], ["user_b", 67]],
  "official": {
    "game_id": "2026-08-29_T1_BFX_G3",
    "score": "1:0",
    "status": "finished",
    "source": "riot-esports-api"
  }
}
```

**关键增量**（相对蓝图）：
- `window_cn`：所有时间戳默认北京时间（UTC 仅括号备注）
- `sources` + `source_breakdown`：多平台贡献量可说明（需求 FR-C1-4 / AC-15）
- `by_category`：灰信号按类别细分
- `bp_discussion`：BP 讨论从 situation 中独立出来（需求 FR-C3-6）
- `official`：官方数据回填槽位

#### 1.3.2 指标清单（需求 FR-C3）

| 指标 | 算法 | 代码位置 | 阈值常量 |
|------|------|----------|----------|
| 比分 | 官方数据回填 + 弹幕"X-Y"弹幕投票 | `verify_match_result.py` | 优先官方；弹幕投票仅兜底 |
| 击杀时间轴 | 弹幕"击杀"/"First Blood"/"一血"关键词命中 | `danmu_intel.py` SITUATION_KW | 按分钟桶聚合 |
| 双方指标对比 | teams 表 pos/neg 对比 | `render_intel.py` | — |
| 弹幕总量与密度曲线 | 按分钟桶计数，密度 = 条数/min | `danmu_intel.py` L274-293 | — |
| 峰值 | mean + 2×std 或绝对阈值 6 条/min 取较大值 | `danmu_intel.py` L290 | `MAX(mean+2*std, 6)` |
| 灰信号密度与人数 | 关键词命中 + 独立用户数 | `build_gray_stats.py` | 多人多时段：≥3 用户 × ≥2 时段 |
| 队伍特质倾向 | 跨场累计，不依赖单场 | `accumulate_team_intel.py` | 跨 3 场以上才进画像 |

#### 1.3.3 灰信号识别（需求 §6.5）

**词表**（沿用蓝图 + 扩展）：

```python
GRAY_KW_CATEGORIES = {
    "假赛质疑": ["假赛", "剧本", "演了", "在演", "明演", "开演", "演员", "322", "菠菜", "买了", "卡盘", "送分", "故意", "操控", "收钱", "黑帮", "送人头"],
    "韩文灰信号": ["고의", "던지노", "던짐", "던졌", "던지네", "던지고", "지령", "조작", "대리"],  # 蓝图 B4
    "英文灰信号": ["throw", "rigged", "match fix", "selling", "int"],
    "裁判质疑": ["黑哨", "裁判", "不公", "盲审"],
    "设备质疑": ["延迟", "掉线", "设备", "ping"],
}
```

**硬约束**（需求 §6.5 六条）：
1. 只作风险提示、不指控、不点名 → 所有灰信号段必须含"纪律声明"模板
2. 必须附样本引用（时间 + 原文片段）→ `samples` 字段不能为空
3. 需多人多时段聚集门槛 → 独立用户 ≥3 且跨 ≥2 个 5 分钟时段
4. 不得用于勒索/威胁/交易 → 仅内部情报页展示，不对外推送
5. 赛后与报告一致 → 发布前检查
6. 只作风险标注，不上升结论 → HTML 文案固定："观众质疑·非结论·仅风险提示"

---

### 1.4 C4 情报报告层

#### 1.4.1 固定十一段结构（需求 §6.6，最高纪律）

> **顺序与标题不可增删**。每段 `<h2><span class="no">N</span>标题</h2>` 编号：

| # | 标题 | 性质 | 内容要点 |
|---|------|------|----------|
| 0 | 核心情报速览 | 事实+浓缩 | 比分/进度 + TOP 信号 3-5 条（风险→锚点→盘口→共识 顺序）+ 决策落点 |
| 1 | 比赛信息与结果总览 | 事实 | 元数据（联赛/队伍/赛制/时间/来源）；比分（弹幕口径·官方待回填） |
| 2 | 逐局复盘 | 事实+时间线 | 每局起止时间（边界来源标注）+ 关键事件 + 弹幕证据（≥10 条/局） |
| 3 | 队伍画像 | 解读（长期库） | 跨场累计特质，带提及量，标注"弹幕口径·待验证" |
| 4 | 人员画像 | 解读（长期库） | 选手历史表现倾向，带原文证据链 |
| 5 | 灰信号汇总 | 事实+风险标注 | 按类别聚合，附样本引用（时间+原文），纪律声明 |
| 6 | 联赛规律与版本 | 解读 | 跨场次联赛统计（仅当样本≥3 场时可输出，否则写"样本不足"） |
| 7 | 预测验证 | 事实 | 回填命中率/灰信号兑现率/BP 判负验证 |
| 8 | 盘口讨论 | 事实+解读 | 让分/人头/水位讨论摘要，需区分"观众讨论"与"实际盘口" |
| 9 | 情报含义与后续观察点 | 解读 | LONG/SHORT 方向性分析，标注置信度（确认/多源/单源待验证/分歧） |
| 10 | 数据与溯源 | 事实 | 实际数据源/预期数据源/缺口；情报输出时间（北京时间） |

**fact/解读 区分**（需求 §6.9）：
- 事实段（0/1/2/5/7/10）：不加粗任何主观判断句，数字必须可在输入 JSON 中找到来源
- 解读段（3/4/6/8/9）：每段开头固定模板："**以下解读基于报告内已列事实，分析仅供参考。**"；每句推断必须带"基于…"引用

#### 1.4.2 三个交付时点

| 时点 | 触发 | 内容 | 时效要求 |
|------|------|------|----------|
| 赛中快报 | 关键节点结束（BP 完成 / 一局结束 / 一波团战） | 节点页 HTML（0/1/2/5/10 段，速览制） | NFR-T-1: 2 分钟内 |
| 完整版 | 比赛结束后 | 完整 11 段 HTML | NFR-T-2: 10 分钟内 |
| 复盘版 | 比赛结束后 | 精简版（0/1/2/3/4/10 段，免费公开） | NFR-T-3: 15 分钟内 |

#### 1.4.3 解读层生成方案（直连 LLM API）

**蓝本教训**：蓝图曾用 Codex CLI（订阅制，延迟高、输出结构漂移）→ 后弃用"快节点方案（直连 DeepSeek）"因为"未经验证、输出结构混乱" → 退回 Codex 全量路径。**本设计采用直连 LLM API，但必须解决蓝图弃用直连时的两个核心问题**：① 输出结构不稳定 ② 可能引入新事实。

**解决方案**：

1. **结构化输入契约**：只向 LLM 发送规则层 JSON（不发送原始弹幕流），输入中每条数字都有溯源
2. **输出契约**：LLM 只输出**纯文本段落**（不含 HTML 标签），每段开头标注 `[FACT]` 或 `[OPINION]`
3. **程序后处理校验**：
   - 提取 `[OPINION]` 段落中的所有数字 → 在输入 JSON 中查找来源 → 找不到的标记为违规
   - 找不到来源的数字 → 删除该句或替换为"样本不足"
   - 输出 HTML 时每句 `[OPINION]` 自动包裹 `<span class="opinion">` + 标注"分析仅供参考"
4. **单场 token 上限**：输入 ≤8k tokens，输出 ≤4k tokens（蓝图 C19 教训：max_tokens=2200 太小导致截断，提升到 4000）
5. **完整性兜底**：LLM 调用失败时，回退到规则直出（`render_fast_intel.py`），页面标注"速览版·规则直出（完整版生成失败，可刷新）"

**提示词模板**（`prompts/report_full.md` 改造）：

```
## 输入数据（规则层情报，所有数字均已带溯源）
{INTEL_JSON}

## 生成要求
1. 严格按 11 段顺序输出，每段标题不可增删
2. 事实段（0/1/2/5/7/10）每句必须来自输入数据，输出 [FACT] 前缀
3. 解读段（3/4/6/8/9）必须基于已列事实，输出 [OPINION] 前缀
4. [OPINION] 中的每个数字必须在输入 JSON 中可找到来源，否则删除该句
5. 所有时间用北京时间（UTC+8）
6. 每段密度：复盘 HTML ≥16KB（蓝图标准）
```

---

### 1.5 C5 发布层

#### 1.5.1 发布流程（沿用蓝图 Vercel 路线）

```
本地生成 HTML → .danmu_intel_site/intel/
  → add_paywall.py 注入付费墙（按比赛状态判定，不按文件名）
  → add_site_nav.py 注入导航（幂等，先清后注）
  → add_favicon.py 注入 favicon
  → 发布前检查（6 项，需求 §6.8）
  → git commit + push → Vercel webhook → 构建 → HTTPS 上线
```

#### 1.5.2 付费墙注入（重设计，修复蓝图"一刀切"教训）

**蓝图教训**（蓝图 add_paywall.py 注释 + LESSONS_LOG C14/C17/C19）：蓝图付费墙按文件名正则匹配 Pro 页面（`PRO_RE = r"intel_danmu_.*_(pre|live|bp|g[1-9])"`），导致"已结束比赛节点页仍被锁"。

**新设计**：付费墙判定**只依据比赛状态**，不按文件名。

```python
def should_inject_paywall(page, match_status_lookup):
    """
    付费墙判定依据（需求 §6.7）：
    - 已结束比赛 → 所有节点免费（FR-C5-10）
    - 未结束比赛 → 赛中/赛前节点付费
    """
    slug = extract_slug(page)          # 从 HTML 元数据提取 slug（非文件名！）
    if not slug:
        return False                    # 无法判定 → 不注入（宁可漏锁不误锁）
    
    status = match_status_lookup.get(slug)
    if status == "finished":
        return False                    # 已结束 → 全部免费
    return True                         # 进行中/赛前 → 付费
```

**关键变更**：
- slug 从 HTML `<meta name="match-slug">` 提取（生成时写入），不从文件名解析（文件名可能带联赛前缀、队伍顺序不定——蓝图 page_key() 的教训）
- `finished_slugs` 集合由 `verify_match_end.py` 在比赛结束时写入 matches.json，发布时读取
- **幂等**：注入前先清除旧脚本（蓝图已实现 `PAYWALL_RE.sub("", text)`）

#### 1.5.3 发布前检查（需求 §6.8，6 项）

| # | 检查项 | 判定方式 | 失败处理 |
|---|--------|----------|----------|
| 1 | 导航唯一 | 页面内含且仅含一条 nav 脚本（匹配 `danmu_nav_v1`） | 拒绝发布，保留上一版 |
| 2 | 无旧模板残留 | 不含 `div.top` / `class="top-bar"` 等旧标记 | 拒绝发布 |
| 3 | 付费墙齐全正确 | Pro 页含 `danmu_member_v1`；免费页不含 | 拒绝发布 |
| 4 | 报告分段完整 | 含 `<h2>` 数量 = 11，标题与 6.6 逐字匹配 | 拒绝发布 |
| 5 | 页面×联赛×标识关联一致 | meta slug 与文件名 slug 归一后一致 | 拒绝发布 |
| 6 | 无速览卡残留 | 完整版不含 `速览版·规则直出` 标记 | 拒绝发布 |

**任一不过 → 不得发布 + 站点保留上一版可用 + 报警通知管理员**。

#### 1.5.4 原子性与回滚

- 蓝图教训（C17）：scp 进 working directory 后被 git reset 覆盖 → **发布以本地 `.danmu_intel_site/` 为单一来源**，push 到 site_repo
- Vercel 每次构建都是完整重建，天然原子
- 回滚：`git revert` + push → Vercel 重新构建

---

### 1.6 C6 会员与付费层

#### 1.6.1 会员模型

```sql
CREATE TABLE members (
  id INTEGER PRIMARY KEY,
  identifier TEXT UNIQUE NOT NULL,       -- TG 用户名 / QQ 号 / 邮箱（需求 Q-5：凭第三方通讯账号标识）
  identifier_type TEXT NOT NULL,          -- 'telegram'|'qq'|'email'
  status TEXT NOT NULL DEFAULT 'active',  -- 'active'|'expired'|'trial'|'grace'
  plan TEXT NOT NULL,                     -- 'standard'|'trial'
  price_paid_usd REAL,
  currency TEXT,                          -- 'USDC'|'SOL'...
  tx_hash TEXT,                           -- 链上交易哈希（可核验）
  payment_network TEXT NOT NULL,          -- 'polygon'|'solana'（需求 FR-C6-3：仅此两个网络）
  wallet_address TEXT NOT NULL,           -- 运营者收款地址（直达，永不持有用户资产 - FR-C6-17/18/19）
  opened_at TEXT NOT NULL,                -- 开通时间
  expires_at TEXT NOT NULL,               -- 到期时间
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE subscriptions_audit (
  id INTEGER PRIMARY KEY,
  member_id INTEGER REFERENCES members(id),
  action TEXT NOT NULL,        -- 'open'|'renew'|'expire'|'grace'|'manual_open'|'rescan_open'
  tx_hash TEXT,
  detail TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
```

#### 1.6.2 收款网络（需求 FR-C6-3：仅 Polygon 与 Solana）

| 网络 | 代币 | 收款方式 | 运营者地址类型 |
|------|------|----------|----------------|
| Polygon | USDC (ERC-20) | 每位用户**唯一地址**（运营者钱包可生成大量子地址/HD 派生） | EOA / HD Wallet |
| Solana | USDC (SPL) | 若做不到唯一地址，则用**唯一 memo/tag** 区分（Solana 支持交易带 memo instruction） | EOA + memo |

**资产直达**（需求 FR-C6-17/18/19）：
- 运营者预生成 N 个收款地址（或地址+memo 组合），存入 `payment_addresses` 表
- 用户订阅时分配一个专属地址，所有入账直达运营者钱包
- **平台永不持有可动用资产凭据**（NFR-S-1）：不留私钥、不托管资产
- 每个地址分配后标记 `assigned_to`，防止重复分配

#### 1.6.3 自动开通流程（需求 FR-C6-5/6/9/10）

```
payment_watcher.py (链上轮询，每 60s)
  │
  ├── 检测到入账事件（Polygon/Solana RPC / Alchemy / Helius）
  │     ├── 匹配待支付订单（pending_subscriptions 表）
  │     │     ├── 金额 ≥ 应付款 → 开通 / 续费（幂等，FR-C6-6）
  │     │     │     └── 写入 audit_log（action='auto_open'）
  │     │     └── 金额 < 应付款 → 标记 partial_payment，告知差额
  │     └── 未匹配订单 → 标记 unrecognized，管理员人工处理
  │
  ├── 到期检查（每日一次）
  │     ├── 到期前 3/1/0 天 → 发送续费提醒
  │     ├── 到期 → status='expired'（不立即降级，给 3 天宽限期）
  │     └── 宽限期过 → 真正降级
  │
  └── 事后补扫（FR-C6-9）
        ├── 管理员提供 tx_hash → rescan 单个交易
        └── 批量补扫：按时间窗口扫描运营者地址全部入账
```

#### 1.6.4 校验接口（需求 FR-C6-21 / AC-18 / NFR-P-3/4）

```
POST /api/verify-member
Body: { "identifier": "TG用户名或QQ号" }
Response: { "member": true/false, "plan": "standard"|"trial"|null, "expires_at": "..."|null }

安全约束（NFR-P-3/4）：
- 对"不存在"与"未开通"两种情况，响应时间与响应体不可区分（防枚举）
- 实现：固定响应时间（sleep 到 200ms ± 10ms）；响应体统一 { "member": false }
- 不返回任何提示如"请先订阅"或"账号不存在"
```

#### 1.6.5 订阅价格（沿用蓝图，`PRICING_ESPORTS_BUNDLE_2026.md`）

| 档位 | 价格 | 说明 |
|------|------|------|
| 试用 | $1 / 3 天 | 早鸟限前 20 名或 9/15 截止 |
| 月付 | $39/月 | 早鸟价（正式 $59 起） |
| 季付 | $105 | — |
| 年付 | $390 | 订阅后终身锁价 |

> 价格配置通过后台可视化配置（需求 Q-2 / FR-C8-6），不在代码中硬编码。

---

### 1.7 C7 站点统计层

#### 1.7.1 统计口径（沿用蓝图 `stats_server.py`）

| 指标 | 算法 | 输出 |
|------|------|------|
| 总访问量 | 所有 `/intel/*` 请求计数 | 整数 |
| 付费页访问量 | Pro 页请求计数 | 整数 |
| 独立访客（按日） | localStorage 客户端 UUID + 服务端 session（不收集身份） | 整数 |
| 会员校验次数 | `/api/verify-member` 调用计数 | 整数 |
| 订阅转化 | 校验通过人数 / 订阅介绍页访客 | 百分比 |
| 采集健康 | 活跃房间数 / 今日条数 / 告警数 | JSON |

**隐私约束（NFR-P-1/2/3/4）**：
- 不收集钱包私钥、助记词
- 不跨站跟踪
- 留资仅用于订阅联系
- 校验接口不可枚举（见 §1.6.4）
- 统计展示只给聚合数字，不给具体访客列表

#### 1.7.2 统计写入

```sql
CREATE TABLE stats_daily (
  date TEXT PRIMARY KEY,            -- '2026-09-22'
  page_views INTEGER DEFAULT 0,
  pro_page_views INTEGER DEFAULT 0,
  unique_visitors INTEGER DEFAULT 0,
  verify_attempts INTEGER DEFAULT 0,
  subscriptions_opened INTEGER DEFAULT 0,
  danmu_total INTEGER DEFAULT 0,
  active_rooms INTEGER DEFAULT 0,
  alerts INTEGER DEFAULT 0
);

CREATE TABLE stats_hourly (
  hour TEXT PRIMARY KEY,            -- '2026-09-22 14'
  page_views INTEGER DEFAULT 0,
  pro_page_views INTEGER DEFAULT 0
);
```

> 方案选择：**不依赖 Vercel Analytics**（第三方），统计由本站后端（SQLite + admin.py 的 `/api/stats`）自己记录。每次页面加载时 fetch `/api/stats/ping`（带本地 UUID）写计数。这避免了与 Vercel 的耦合，也满足"不跨站跟踪"。

---

### 1.8 C8 后台管理层

#### 1.8.1 需求定位（需求 Q-2 / FR-C8-1..6）

**第一版必须有可视化配置**，不得用"配置文件 + 重启"替代。

#### 1.8.2 管理后台鉴权

```
管理员注册：
  首个 admin 由环境变量 DANMU_INTEL_ADMIN_TOKEN 注册（蓝图方案）
  后续 admin 由现有 admin 通过后台添加

登录机制：
  POST /admin/login → { token } → localStorage → 后续请求 Header: Authorization: Bearer <token>
  Token 有效期 7 天，可撤销
```

#### 1.8.3 后台页面清单

| 页面 | 功能 |
|------|------|
| `/admin/dashboard` | 总览：今日场次 / 采集健康 / 会员数 / 待处理告警 |
| `/admin/sources` | 数据源 CRUD（直播间配置）：增删改 + 启停；**改动 1 分钟内生效（NFR-T-4）** |
| `/admin/members` | 会员列表 / 人工开通 / 手动续费 / 审计日志 |
| `/admin/config` | 价格配置 / 试用开关 / 付费墙文案 / 收款地址 |
| `/admin/publish` | 手动触发发布 / 回滚 / 查看发布日志 |
| `/admin/slices` | 切片边界可视化修正（拖拽时间轴） |
| `/admin/audit` | 审计日志查看（操作人/时间/对象/前后值） |

#### 1.8.4 配置热生效（NFR-T-4）

- 配置写入 SQLite `config` 表
- 采集 supervisor 每 60s 重读 rooms 表 → 增/删/改的房间实时生效
- 发布配置（价格、付费墙文案）每次发布时读取最新值，无需重启

---

### 1.9 C9 通知与报警层

#### 1.9.1 报警类别与阈值（需求 FR-C9-1..4 / NFR-T-5）

| 类别 | 触发条件 | 通知渠道 | 时限 |
|------|----------|----------|------|
| 采集异常 | 房间断线 > 5 分钟 / 心跳停止 > 3 分钟 / 0 弹幕告警 | QQ（主） | 5 分钟内 |
| 比赛状态异常 | 终局误判（单信号触发）/ 比分回填失败 | QQ | 5 分钟内 |
| 发布失败 | 付费墙注入失败 / 分段不完整 / 导航重复 | QQ | 即时 |
| 会员异动 | 大额入账 / 未匹配交易 / 连续开通失败 | QQ | 5 分钟内 |
| 系统资源 | 磁盘 > 85% / 内存 > 90% / venv 异常 | QQ | 5 分钟内 |

#### 1.9.2 报警送达约束（NFR-T-5）

- 5 分钟内送达
- **超时即丢弃**（不补送旧告警）
- 去重：同一房间同一类型告警 10 分钟内只发一次
- 通知渠道：QQ（通过现有 QQ Bot API 推送，本期仅此一个渠道）

---

## 2. 数据保留与归档（需求 §7.10 / NFR-D-1..4）

```sql
-- 原始弹幕分区：按月份分表或带 partition_key
CREATE TABLE danmu_raw (
  id INTEGER PRIMARY KEY,
  platform TEXT,
  source TEXT,
  ts REAL,
  nick TEXT,
  text TEXT,
  match_date TEXT,          -- 所属比赛日期，用于归档
  partition_key TEXT,       -- '2026-09'
  archived INTEGER DEFAULT 0  -- 0=在线, 1=已归档
);

-- 6 个月前数据标记 archived=1（不删，迁出"在线统计"范围）
-- 在线视图：CREATE VIEW danmu_online AS SELECT * FROM danmu_raw WHERE archived=0;
```

| 维度 | 策略 |
|------|------|
| 在线保留期 | 6 个月（SQLite 内 partition_key 标记） |
| 归档 | 超期数据标记 archived=1，仍可查询但不参与日常统计 |
| 可核验 | 归档操作写入 audit_log（操作时间/数量/校验和） |
| 不破坏在线期内回溯 | 归档只标记不删，在线期内比赛仍可完整回溯 |

> NFR-D-4（归档不得破坏在线保留期内比赛的可回溯性）为派生要求，待实现时确认具体约束。

---

## 3. 非功能需求实现方案

### 3.1 时效（NFR-T-1..5）

| 需求 | 方案 |
|------|------|
| 赛中快报 2 分钟内 | 规则直出（render_fast_intel.py）先行上线；LLM 完整版异步生成后覆盖 |
| 完整版 10 分钟内 | LLM 调用 ≤30s + 后处理 ≤10s + 发布 ≤30s，总 <2 分钟，远低于 10 分钟上限 |
| 复盘版 15 分钟内 | 完整版生成后自动转公开（改标记即可，无需重新生成） |
| 配置 1 分钟内生效 | supervisor 60s 轮询 rooms/config 表 |
| 报警 5 分钟内 |  watchdog 60s 轮询 → 触发后即时推送到 QQ Bot |

**时效与准确性冲突** → 准确性优先（NFR-T-3 原文）。LLM 生成失败时回退规则直出 + 标注，不等待。

### 3.2 成本（NFR-C-1..3）

| 项目 | 估算 | 依据 |
|------|------|------|
| LLM 解读成本/场 | ≈ ¥0.05-0.15（DeepSeek V3 输入 ¥1/M + 输出 ¥2/M × 约 6k tokens） | 蓝图"约数分钱/份报告" |
| Vercel 构建 | Free tier（100GB bandwidth/月，初期足够） | 蓝图 Vercel 路线 |
| 站点托管 | Vercel Pro $20/月（如需；Free 起步可零成本） | — |
| 服务器（本机） | 零额外费用（利用现有笔记本） | — |
| 域名 | 已有（danmu-intel 品牌） | — |

### 3.3 质量（NFR-Q-1..5）

| 需求 | 防线 |
|------|------|
| 事实零错误 | 规则层数字只来自 LLM 输入 JSON + 发布前数字溯源校验 |
| 结构稳定 | 11 段标题硬编码，模板不允许 LLM 增删 |
| 解读可辨 | `[OPINION]` 前缀 + HTML class `opinion` + 颜色区分 |
| 中文通顺 | LLM 本身保证；规则直出模板人工撰写 |
| 解读不引新事实 | 后处理校验：OPINION 中段数字必须在输入 JSON 中可溯源 |

### 3.4 可用性（NFR-A-1..5）

| 需求 | 方案 |
|------|------|
| 本期单机 | 本机 Lubuntu，systemd user session 自启 |
| 可接受短时中断 | supervisor 自动重启；发布失败保留上一版 |
| 进程异常自动拉起 | systemd Restart=always + RestartSec=10 |
| 不做数据备份 | 用户接受（需求 Q-7） |
| 不要求恢复演练 | 用户接受（需求 Q-7） |

### 3.5 安全（NFR-S-1..5）

| 需求 | 方案 |
|------|------|
| 任何位置不得有可动用资产凭据 | 运营者私钥不在服务器上；收款直达运营者地址（不托管） |
| 写接口鉴权限流 | admin 后台 token 鉴权；verify-member 固定响应时间防枚举 |
| 后台隔离 | /admin/* 仅 localhost + tailnet IP 可访问（Nginx allow/deny） |
| 敏感配置不入库 | admin token / API key / 私钥 通过环境变量注入，不入库 |
| 资金操作不可篡改审计 | subscriptions_audit 表记录所有开通/续费/补扫 |

### 3.6 隐私（NFR-P-1..4）

| 需求 | 方案 |
|------|------|
| 不泄露会员身份 | 校验接口固定响应；会员列表仅管理员可见 |
| 校验接口不可枚举 | 不存在/未开通响应体统一 `{member:false}`，时间恒定 |
| 不跨站跟踪 | 无第三方统计，统计自建 |
| 留资仅用于订阅联系 | members 表 identifier 只用于校验与续费提醒 |

### 3.7 合规（NFR-L-1..4）

| 需求 | 方案 |
|------|------|
| 仅公开可访问弹幕 | 采集仅抓公开直播间弹幕；不破解/不登录 |
| 灰信号守 §6.5 | 词表 + 多人多时段门槛 + 纪律声明 |
| 标明取材范围 | 每页底部标注"数据来源：虎牙（多路）/ SOOP / 官方" |
| 预测与结果公开对照 | 报告 §7 固定段落，含失败场次 |

### 3.8 可维护性（NFR-M-1..5）

| 需求 | 方案 |
|------|------|
| 不保留向后兼容 | 过时路径直接删（NFR-M-1） |
| 最简实现 | 第一版不造通用框架，硬编码 11 段 |
| 分层生长 | 采集/分析/情报/发布 四层独立演进 |
| 模块化 | 每能力一个 Python 模块 + 一个测试文件 |
| 优先成熟依赖 | aiohttp / sqlite3 / requests / jinja2（已有） |

### 3.9 质量保证（NFR-GA-1..4）

| 需求 | 方案 |
|------|------|
| 关键规则与主路径自动化回归 | tests/ 目录，覆盖 C1-C9 核心路径 |
| 覆盖率 ≥90% | pytest-cov 门禁 |
| 不达标不得合入 | CI 检查 coverage.xml ≥ 90% |
| 无外网/无真实资金可重复运行 | 测试 mock LLM API + mock 链上 RPC + mock 弹幕源 |

---

## 4. 蓝图复用清单

> 蓝图 = `/home/tony/Workspace/danmu-intel-local`（他人项目，只读）。本节仅列**可复用的实现素材**，每项标注复用方式。

| 蓝图素材 | 本地路径 | 需求文档是否点名 | 复用方式 |
|----------|----------|------------------|----------|
| `fetch_huya_danmu.py` | `tools/fetch_huya_danmu.py` | ✅ 是 | 直接复制并小改（离线等待 60s 复查已含） |
| `fetch_soop_danmu.py` | `tools/fetch_soop_danmu.py` | ✅ 是 | 直接复制 |
| `capture_server.py` | `deploy/danmu_server/capture_server.py` | ❌ 否 | 参考其 subprocess + 5s 轮询模型，本机重写为 systemd user unit |
| `danmu_intel.py` | `tools/danmu_intel.py` | ❌ 否 | 核心复用：规则层分析；需扩展 `sources`/`window_cn`/`bp_discussion`/`official` |
| `render_fast_intel.py` | `tools/render_fast_intel.py` | ❌ 否 | 复用为"速览版·规则直出"兜底；需改造为 11 段（当前仅 7 段） |
| `add_paywall.py` | `tools/add_paywall.py` | ❌ 否 | **重构**：文件名匹配 → 比赛状态匹配；注入脚本可复用 |
| `add_site_nav.py` | `tools/add_site_nav.py` | ❌ 否 | 直接复用（幂等注入） |
| `INTEL_HTML_TEMPLATE.md` | `knowledge/INTEL_HTML_TEMPLATE.md` | ✅ 是 | 作为 HTML 结构规范引用（12 段决策导向模板 → 需对齐为 11 段需求结构） |
| `LIVE_INTEL_SCHEMA.md` | `knowledge/LIVE_INTEL_SCHEMA.md` | ✅ 是 | 作为情报 JSON schema 参考 |
| `INTEL_RULES_V2.md` | `docs/task/INTEL_RULES_V2.md` | ✅ 是 | 采集层与输出层规范参考 |
| `SUBSCRIPTION_LEDGER.md` | `docs/task/SUBSCRIPTION_LEDGER.md` | ✅ 是 | 订阅模型参考 |
| `PRICING_ESPORTS_BUNDLE_2026.md` | `docs/task/PRICING_ESPORTS_BUNDLE_2026.md` | ✅ 是 | 价格配置参考（具体值走后台可配置） |
| `DANMU_INTEL_PRODUCT_DECISIONS.md` | `docs/task/DANMU_INTEL_PRODUCT_DECISIONS.md` | ✅ 是 | 产品决策参考 |
| `DANMU_WORKFLOW.md` | `knowledge/DANMU_WORKFLOW.md` | ✅ 是 | SOP 五阶段（准备→启动→监控→情报→复盘）沿用 |
| `LESSONS_LOG.md` | `docs/task/LESSONS_LOG.md` | ✅ 是 | **逐条读**，每条教训写入对应模块的防错清单 |
| `DATA_INTEGRITY_CHECKLIST.md` | `docs/task/DATA_INTEGRITY_CHECKLIST.md` | ✅ 是 | 发布前检查扩展为 6 项的基础 |
| `VERIFICATION_METHODOLOGY.md` | `docs/task/VERIFICATION_METHODOLOGY.md` | ✅ 是 | 验证回填方法参考 |
| `INTEL_LIBRARY_TAXONOMY.md` | `knowledge/INTEL_LIBRARY_TAXONOMY.md` | ✅ 是 | 分类框架参考（实体域/事件域/信号域/市场域） |
| `DANMU_CAPTURE_RULES.md` | `knowledge/DANMU_CAPTURE_RULES.md` | ✅ 是 | 采集纪律参考 |
| `STREAMER_PROFILES.md` | `knowledge/STREAMER_PROFILES.md` | ✅ 是 | 主播画像参考 |
| `INTEL_PRODUCT_FRAMEWORK_2026-08-31.md` | `docs/task/INTEL_PRODUCT_FRAMEWORK_2026-08-31.md` | ⚠️ 名字漂移（需求写 `INTEL_PRODUCT_FRAMEWORK.md`） | 产品框架参考 |
| 187 份 node_data JSON | `docs/data/intel/node_data/*.json` | ✅ 是 | 仅 `bp_signals.json` / `gray_signals.json` 作引用典型；其余不引入 |

**明确不引入的蓝图素材**（需求文档 §参考资料 注明）：
- `DANMU_README.md`（蓝本 README）— 不引入

**本地蓝本没有的素材**（需求点名但对不上）：
- `config/streamers.json` → 新系统用 SQLite rooms 表替代
- `members.json` → 会员名单只在服务器上（本地无），新系统用 SQLite members 表

---

## 5. 缺口与新造组件清单

> 蓝本从未实现或实现不符合需求的组件（新系统必须新建）：

| 缺口 | 对应需求 | 新造组件 |
|------|----------|----------|
| 切片层三档优先级 + 冲突处理 + 边界来源记录 | §6.3 / FR-C2-1..5 | `slice_engine.py`（新造） |
| 11 段固定结构报告（蓝图仅 7 段速览版） | §6.6 / FR-C4-1..11 | `intel_report_generator.py`（新造） |
| 直连 LLM API + 结构化输入输出 + 不引新事实校验 | §6.9 / Q-1 | `llm_client.py`（改造蓝图版本） |
| 付费墙按比赛状态判定（蓝图按文件名正则） | §6.7 / FR-C5-10 | `add_paywall.py`（重构） |
| 后台可视化配置页面 | Q-2 / FR-C8-1..6 | `admin.py`（新造） |
| SQLite 会员 + 订阅 + 审计 + 配置库 | C6 / FR-C6-1..21 | `db/schema.sql` + `models.py`（新造） |
| 链上入账自动检测（Polygon + Solana） | FR-C6-5 | `payment_watcher.py`（新造） |
| QQ 报警（5 分钟时限 + 去重 + 超时丢弃） | FR-C9-1..4 / NFR-T-5 | `notify.py`（新造） |
| 发布前 6 项检查（导航唯一/旧模板残留/付费墙/分段完整/关联一致/无速览卡） | §6.8 | `publish_audit.py`（新造） |
| 数据保留归档（在线 6 个月 + 归档可核验） | §7.10 / NFR-D-1..4 | `archive.py`（新造） |
| 统计模块（自建，不依赖第三方） | FR-C7-1..6 | `stats.py` + admin `/api/stats`（新造） |
| 多平台贡献量可说明（sources + breakdown） | FR-C1-4 / AC-15 | `danmu_intel.py` 扩展 |
| 校验接口不可枚举（固定响应 + 恒定时间） | FR-C6-21 / AC-18 | `verify_member.py`（新造） |

---

## 6. 部署与发布架构

### 6.1 本机开发验证

```bash
# 项目根目录: ~/Workspace/danmu-intel
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 初始化数据库
python -m db.init

# 启动采集（开发模式）
python -m capture.supervisor --rooms "we957=https://www.huya.com/957"

# 启动本地预览站点
python -m admin.server --port 8001   # 后台 + 静态站预览

# 手动触发一次发布到 Vercel
python -m publish.deploy --env preview
```

### 6.2 Vercel 静态部署

```
.danmu_intel_site/           ← 构建产物目录
  ├── index.html             ← 首页（免费）
  ├── subscribe.html         ← 订阅介绍页（免费）
  ├── intel/
  │   ├── index.html         ← 历史情报库列表（免费）
  │   ├── <slug>_full.html   ← 完整版（Pro / 赛后免费）
  │   ├── <slug>_G1.html     ← 节点页（Pro / 赛后免费）
  │   └── ...
  └── .vercel/               ← 项目配置

发布触发：
  git push origin trunk → Vercel webhook → build → deploy
```

> 蓝本教训（C17/C19）：订阅页等静态页必须纳入版本库单一来源，不在工作目录直接 scp 覆盖。

### 6.3 生产部署（待 VPS 方案确定后补充）

当前方案：本机开发验证 + Vercel 发布。生产采集若需 7×24 迁移到 VPS，参考蓝图 `VPS_HANDOFF.md` 路线，但采集与分析留在本机统一运维（省去 rsync 回传环节）。

---

## 7. 验收标准映射（AC → 实现组件）

| AC | 需求 | 实现组件 | 验证方式 |
|----|------|----------|----------|
| AC-1 | 报告符合 6.6 + 事实零错误 + 时效 | intel_report_generator.py + publish_audit.py | 自动化：检查 HTML 含 11 段 + 数字溯源 |
| AC-2 | 赛中会员可见 / 访客不可见 / 结束自动转公开 | add_paywall.py (should_inject_paywall) | 自动化：mock finished 状态 → 检查 Pro 页无 paywall 脚本 |
| AC-3 | 订阅到访问全程无人工 + 款项直达运营者地址 | payment_watcher.py + verify_member.py | 自动化：mock 链上入账 → 校验开通 |
| AC-4 | 重复检测只开通一次 | payment_watcher.py (幂等) | 自动化：双写入同一 tx_hash → 只开一次 |
| AC-5 | 模拟调用额度受限漏检可补扫 + 管理员凭凭证补开通 | vps_backfill_nodes.py + admin 后台 | 自动化：超频调用后补扫 |
| AC-6 | 到期降级 + 宽限 + 续费顺延 | payment_watcher.py (到期检查) | 自动化：mock 时间推进 |
| AC-7 | 后台非管理员不可访问 + 配置 1 分钟内生效 | admin.py + supervisor | 自动化：未鉴权访问 /admin → 403 |
| AC-8 | 发布前检查不过拒绝发布 + 可回滚 | publish_audit.py | 自动化：注入坏页 → 拒绝发布 |
| AC-9 | 能答"某天多少人访问付费页"但不能答是谁 | stats.py (聚合口径) | 人工检查统计接口返回 |
| AC-10 | 校验对"不存在""未开通"响应不可区分 | verify_member.py (恒定响应) | 自动化：两种不存在场景 → 响应体字节一致 + 时间差 <20ms |
| AC-11 | 五类异常 5 分钟内报警 | watchdog.py + notify.py | 自动化：注入异常 → 5 分钟内 QQ 推送 |
| AC-12 | 全库零命中可动用资产凭据 | 安全扫描脚本 | CI 正则扫描仓库：私钥/助记词格式 |
| AC-13 | 在线保留期内删除全部统计后仅凭原始记录与配置重建 | rebuild 脚本 | 测试：清空 stats → 重建 → 对比 |
| AC-14 | 覆盖率 ≥90% + 无外网可重复 | pytest + pytest-cov + mock | CI 门禁 |
| AC-15 | 多直播间跨平台不重不漏 + 可说明每场直播间贡献量 | capture supervisor + danmu_intel sources breakdown | 自动化：双平台同播 → 检查 sources 字段 |
| AC-16 | 缺解读段不得发布 + 解读中数字可在事实部分找到来源 | publish_audit.py + llm 后处理 | 自动化：注入缺段/数字无来源 → 拒绝发布 |
| AC-17 | 超 6 个月归档仍可调取 + 在线期内溯源可核验 | archive.py | 自动化：归档后仍可查询 |
| AC-18 | 凭既有第三方通讯账号标识校验 + 无需注册 + 响应不可区分 | verify_member.py | 自动化：TG/QQ/邮箱三种 identifier 均可用 |

---

## 8. 项目结构（执行体开工时的目录骨架）

```
~/Workspace/danmu-intel/
├── docs/
│   ├── requirements/
│   │   └── DANMU_INTEL_REQUIREMENTS.md    ← 唯一需求来源（不动）
│   └── design/
│       └── ENGINEERING_DESIGN_v1.md       ← 本设计文档（Issue #3 同步）
├── db/
│   ├── schema.sql                         ← 全库 schema
│   ├── migrations/                        ← 增量迁移脚本
│   └── init.py                            ← 初始化 + 种子数据
├── capture/
│   ├── supervisor.py                      ← 采集主进程
│   ├── adapters/
│   │   ├── base.py                        ← DanmuAdapter ABC
│   │   ├── huya.py
│   │   ├── soop.py
│   │   ├── twitch.py                      ← 空实现，注册表留位
│   │   └── kick.py                        ← 空实现，注册表留位
│   └── rooms.py                           ← rooms 表 CRUD
├── analysis/
│   ├── danmu_intel.py                     ← 规则层（扩展蓝图版本）
│   ├── slice_engine.py                    ← 切片层（新造）
│   ├── verify_match_end.py                ← 终局判定
│   ├── gray_stats.py                      ← 灰信号聚合
│   └── official_data.py                   ← 官方数据回填
├── intel/
│   ├── report_generator.py                ← 11 段报告生成（新造）
│   ├── render_fast.py                     ← 规则直出速览版（兜底）
│   ├── llm_client.py                      ← LLM API 调用 + 后处理
│   └── templates/
│       ├── report_full.md                 ← 完整版提示词
│       ├── report_live.md                 ← 赛中快报提示词
│       └── report_review.md               ← 复盘版提示词
├── publish/
│   ├── paywall.py                         ← 付费墙注入（重构）
│   ├── nav.py                             ← 导航注入
│   ├── audit.py                           ← 发布前 6 项检查
│   └── deploy.py                          ← Vercel 部署
├── admin/
│   ├── server.py                          ← 后台 Web 服务
│   ├── auth.py                            ← 鉴权
│   └── pages/                             ← 后台页面模板
├── payment/
│   ├── watcher.py                         ← 链上入账监听
│   ├── polygon.py                         ← Polygon RPC
│   ├── solana.py                          ← Solana RPC
│   └── verify.py                          ← 校验接口
├── stats/
│   ├── collector.py                       ← 统计写入
│   └── views.py                           ← 统计查询
├── notify/
│   └── qq_bot.py                          ← QQ 报警推送
├── watchdog/
│   └── health.py                          ← 采集健康 + 自检
├── archive/
│   └── retention.py                       ← 归档任务
├── tests/                                 ← 回归测试（覆盖率 ≥90%）
├── docs/data/danmu/                       ← 原始弹幕 JSONL（只增不改）
├── docs/data/intel/                       ← 结构化情报库
├── runtime/                               ← 运行时状态（不入库）
├── .danmu_intel_site/                     ← 站点构建产物
├── requirements.txt
└── README.md
```

---

## 9. 与蓝图的关键差异（执行体必读）

| 维度 | 蓝图（danmu-intel-local） | 本系统（danmu-intel） | 差异原因 |
|------|---------------------------|----------------------|----------|
| 采集运行位置 | VPS 7×24 | 本机常驻进程 | 本机方案，省去回传 |
| 分析运行位置 | 本机 | 本机（统一） | 统一运维 |
| 会员库位置 | 服务器文件 members.json | SQLite members 表 | 一致性 + 审计 |
| 数据源配置 | config/streamers.json | SQLite rooms 表 + 后台 CRUD | 热生效（NFR-T-4） |
| 付费墙判定 | 文件名正则（PRO_RE） | 比赛状态（finished_slugs） | 修复蓝图"一刀切"教训 |
| 报告结构 | 7 段速览版 | 11 段完整版（固定） | 需求 §6.6 |
| 解读生成 | Codex CLI → 弃用快节点 → 回退 Codex | 直连 LLM API + 结构化输入输出 | 时效要求（2 分钟快报） |
| 后台配置 | 无（配置文件 + 重启） | 第一版必须有可视化后台 | 需求 Q-2 |
| 收款网络 | 人工登记 | Polygon + Solana 链上自动检测 | 需求 FR-C6-3/5 |
| 报警渠道 | QQ / TG | QQ（本期仅此一个） | 简化 |
| 统计 | Vercel + 自建混用 | 自建（不依赖第三方） | NFR-P |

---

## 10. 执行优先级建议（用户未指定，供参考）

> 按"数据流正向顺序 + 蓝本已验证优先"排列：

1. **P0 基础设施**：项目骨架 + SQLite schema + 配置热生效 + 后台框架
2. **P1 采集链**：Huya + SOOP 适配器 + supervisor + 落盘 JSONL
3. **P2 规则层**：danmu_intel.py 扩展 + 终局判定 + 灰信号统计
4. **P3 发布链**：add_paywall 重构 + 发布前检查 + Vercel 部署
5. **P4 情报链**：11 段报告生成 + LLM 直连 + 提示词模板
6. **P5 会员链**：支付监听 + 校验接口 + 后台会员管理
6. **P6 运营链**：报警 + 统计 + 归档

每 P 完成后独立可演示、可验收，不跨 P 耦合。
