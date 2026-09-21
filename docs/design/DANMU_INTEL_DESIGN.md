# 弹幕情报库（danmu-intel）完整设计文档

> **版本**：v2.0（可执行规格 · 基于 PRD v2.0 全文 + 付费闭环补充）
> **日期**：2026-09-21
> **状态**：待用户审阅
> **设计基准**：`danmu-intel-local/docs/task/DANMU_INTEL_CLOUD_PYTHON_PROJECT_PRD.md`（v2.0 定稿）
> 
> **可执行规格标准**：每段自包含 —— 引用的 PRD 原文规则直接抄入本文，执行体无需回读 PRD 即可开工。

---

## 0. 文档说明

本文档是 danmu-intel 项目的**唯一设计权威**。面向读者：开发者（照此实现）、运维（照此部署）、产品/运营（照此验收）。

与 v1.0 的差异：
1. **可执行规格**：每段自包含，PRD 原文规则直接抄入，执行体无需回读 PRD
2. **数据结构完整**：字段名、类型、含义、示例值
3. **算法步骤完整**：按步骤列出，可直接对照实现
4. **验收清单逐条可检查**：每条有明确的通过条件

---

## 1. 产品概述

### 1.1 一句话定位

**把电竞直播弹幕，变成可溯源、可支持决策的情报，并且越攒越值钱。**

### 1.2 我们做什么

多路直播间（虎牙为主）的实时弹幕，按比赛 × 节点（赛前 / BP 后 / 局中 / 局末 / 赛后复盘）加工成结构化情报；事实层（比赛时间、队伍、比分、选手×英雄/地图、结算）一律用官方/权威公开源校准（Riot esports-api / Liquipedia / HLTV / Polymarket 结算），弹幕只负责提供"观众怎么看、有什么信号、有什么质疑"。

### 1.3 用户价值

| 角色 | 价值 |
| --- | --- |
| 付费决策者（Pro） | 赛前到局中 1-2 分钟拿到别人刷几小时弹幕也整理不出的东西：灰信号、BP 锚点、观众共识、盘口讨论；每条可溯源、可验证 |
| 普通观众（免费） | 赛后复盘 + 验证闭环（当时怎么说、后来准不准）+ 知识摘要 |
| 长期资产 | 选手 / 队伍 / 英雄（地图）/ 联赛四维知识越攒越厚，形成复利情报库 |

### 1.4 与蓝本（danmu-intel-local）的核心差异

| 维度 | 蓝本 | 本项目 |
| --- | --- | --- |
| LLM 提炼 | `USE_LLM = False`（2026-08-30 主动关闭，手动调用） | 程序固化直连 DeepSeek，无人干预 |
| 付费闭环 | 零链上支付代码（表单→推 TG + 手工 members.json） | 唯一充值地址 + watch-only + 自动开通 |
| 工程化 | 脚本散落，未形成标准 Python 工程 | 标准工程（目录/依赖/配置/测试/文档齐全） |
| 生成路径 | 三条路径并存（Codex 会话 / 直连 DeepSeek / 规则直出） | 收敛为唯一权威（程序固化 + LLM 只走接口） |

---

## 2. 背景与问题

### 2.1 现状

弹幕情报库全链路已在本地/线上跑通（2026-08-18 起）：多直播间弹幕采集 → 按比赛×节点时间窗切片 → 规则层统计（词表/提及量/密度/灰信号）→ LLM 提炼 → 门禁校验 → HTML+MD 双格式发布 → 订阅付费墙。代码散落在本地项目与 VPS 脚本目录，未形成标准 Python 工程。

### 2.2 四类失效模式（必须在架构层根除）

1. **情报混淆**：跨联赛混源、选手×英雄配错、队名歧义（OKBRO 玩梗误收）
2. **时效滞后**：节点结束到上线 5-15 分钟，超出决策窗口
3. **价值下降**：页面有结构没内容、章节标题/时间线碎片当情报
4. **格式混乱**：多套模板并存（12 段/10 段），缺乏唯一权威

### 2.3 2026-08-31 实战教训（本 PRD 的防误设计来源）

Aurora vs G2（BLAST Open Porto，A 组败者组决赛，BO3）实战暴露：

> L1 弹幕提前庆祝误判：21:27-21:29 弹幕高密度"G2 回家 / GG / 带走"
> （928→2,110 条/分）= Aurora 12:11 赛点时的提前庆祝，实际 G2 追平进加时；
> L2 弹幕提前预测误判：21:41-21:45 与 21:54-21:55 弹幕"图三了 / 1615 /
> 没想到是这样结束" = 观众对加时结果的提前预测，实际打到三加时（19:16）；
> L3 官方页滞后：blast.tv 页面快照滞后约 5-10 分钟（曾显示 12:11 / 14:15 /
> 16:16 等过时比分），Liquipedia 图二 finished 字段长时间为空；
> L4 加时制差异：CS2 部分赛事为"4 回合加时块，先到 4 分者胜，平局重复"
> （本场实际 12:12 → 15:15 → 19:16，共 31 回合），"领先 2 分"等通用假设不成立；
> L5 弹幕比分喊话不可信："13-8 / 13-10 / 16:15 / 18:16" 等均曾出现，
> 只有官方源（blast.tv 最终 19:16 + 系列 1:1）可作终局依据。

**固化为设计规则**：

> R1 终局判定四信号齐备才定稿：官方比分（结构源） + 官方系列状态 + 弹幕多信号共振
>    + 流量骤降；弹幕"结束/比分/晋级"只作候选信号；
> R2 加时制参数化：config 中每赛事配置 OT 规则（4 回合加时块 / 无限 / 领先 2 分），
>    判终局先查 OT 规则，禁止通用假设；
> R3 官方源多级仲裁：Polymarket 结算 > 官方 API/官方页 > Liquipedia > 战报 > 弹幕；
>    官方页滞后时以"最新可核对快照 + 时间戳"标注，不臆测；
> R4 时间轴只用真实弹幕时间戳；页面必须带"本页数据截至"时间；
> R5 任何"比赛结束"类发布，先过 match_state_guard 四道闸（时间门槛/结构源优先/
>    反讽识别/比分源滞后），门禁不过 = 不交付。

---

## 3. 目标与非目标

### 3.1 目标

1. 做成**可部署、可维护、可回归的标准 Python 项目**（目录/依赖/配置/测试/文档齐全）
2. 全链路自动化：比赛开始 → 采集 → 节点触发 → 快报 → 完整版 → 发布 → 结束复盘 → 情报库沉淀 → 验证回填，无需人工干预
3. 输出质量稳定：结构唯一（旧 10 段框架）、事实层只信官方、弹幕结论可溯源、缺数据显式标「无」
4. 成本可控：单页 LLM 成本 ≈0.04-0.06 元，一场 BO3 ≈0.25-0.45 元
5. 满足 SLA：快报 ≤2 分钟、完整版 ≤10 分钟、赛后复盘 ≤15 分钟（北京时间）

### 3.2 非目标（明确不做）

- 不做 Dota2 / Valorant 采集（仅预留 league 枚举与源注册表结构）
- **不采集 Twitch**（用户 2026-08-26 明确决定，数据源质量问题）
- 不做弹幕意见自动聚类（2026-08-26 已回滚弃用）
- 不做"速览卡/方向板"12 段模板（已回归旧 10 段框架）
- 不在本项目内持有私钥或执行真实下单（交易另属 polydata 仓库）
- 不依赖 Codex 会话/CLI 生成（LLM 只通过 API 接口调用）

---

## 4. 总体架构

### 4.1 分层架构

```
┌────────────────────────── VPS（danmu-intel Python 项目） ──────────────────────────┐
│                                                                                    │
│  capture ──> schedule/event ──> slice ──> rules ──> refine ──> verify ──> render   │
│  采集          赛事/事件         切片       规则统计    LLM 提炼    门禁      输出     │
│                                                                                    │
│  ┌──────────────────────────────────────────┬────────────────────────────────────┐ │
│  │ library（情报库沉淀）                     │ publish（发布层）                  │ │
│  │ 选手/队伍/英雄/联赛四维 + 画像 + 验证闭环   │ 今日页/历史库/画像/灰信号/验证 +     │ │
│  │                                          │ 付费墙 + 时间轴壳 + 审计 + 部署      │ │
│  └──────────────────────────────────────────┴────────────────────────────────────┘ │
│                                                                                    │
│  scheduler（事件驱动 + systemd timer 兜底）   monitor（自检/SLA/成本/TG 告警）        │
│  api（verify-member / lead / stats）         storage（SQLite + JSONL + MD）         │
└────────────────────────────────────────────────────────────────────────────────────┘
            │
  官方数据源：Riot esports-api / feed window / Liquipedia / HLTV / Polymarket 结算
  LLM：DeepSeek API（程序直连，固定提示词）
  站点：GitHub Pages（danmupulse.com）或 nginx 直出
```

### 4.2 架构原则（来自朋友架构评审 + 产品决策）

> P1 程序固化一切：流程、模板、规则、门禁、提示词全部以代码/配置文件落地，
>    不通过大模型"每次重新理解"；LLM 只是接口调用（固定提示词 + 数据 → 分析结论）；
> P2 确定性优先：规则层（词表/密度/灰信号计数/时间戳）完全确定性输出，
>     LLM 只做判断性提炼且必须过门禁；
> P3 可替换性：LLM 提供方接口化（DeepSeek 起步，OpenAI 兼容），换模型不改流程；
> P4 幂等：所有生成/沉淀/发布步骤有幂等键，重跑不重复；
> P5 可观测：每个节点记录四段时间戳（触发/快报/完整/上线）+ LLM usage + 成本。

---

## 5. 功能需求详设

### 5.1 采集层（capture）

**职责**：多路多平台直播间弹幕流采集，独立落盘，异常自动重启。管理员通过 Web 后台完成数据源配置。

#### 5.1.0 用户角色与鉴权

| 角色 | 说明 | 权限范围 |
|---|---|---|
| `viewer` | 未登录访客 | 只读访问免费内容（赛后复盘） |
| `user` | 订阅用户 | 只读访问 Pro 内容、个人中心 |
| `admin` | 管理员 | 后台数据源配置、用户管理、系统监控 |

**鉴权实现**：
- 路由保护：`/admin/*` 路径要求 `session.role == 'admin'`，否则返回 403
- 首个 admin 注册：通过 `DANMU_INTEL_ADMIN_TOKEN` 环境变量注册（一次性，注册后 token 失效）
- 会话管理：signed cookie（`itsdangerous` 库），7 天过期，HTTPS only

#### 5.1.1 数据源配置管理（DB 驱动，非 config 文件）

> **PRD 原文（§7.1 FR-CAP-2）**：
> `config/streamers.json`：房间 ID、平台、联赛归属、启用开关、备注；
> `config/leagues.json`：每个联赛的默认采集集（isOn 标记）；
> 新增/停用房间改配置热生效，同一房间（如 maxixi 与 CSBOY 官方房）自动去重。

**本项目改进**：数据源配置由 Web 后台 CRUD 驱动，写入 DB `sources` 表，不读本地 config 文件。

**DB 表 `sources`**：

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| id | INTEGER PK | 自增主键 | 1 |
| platform | TEXT | 平台标识 | huya / douyu / bilibili / soop / kick / twitch |
| source_name | TEXT | 来源名称（直播间标题） | LCK CL 官方流 |
| room_url | TEXT | 房间 URL | https://www.huya.com/123456 |
| room_id | TEXT | 房间 ID（平台唯一标识） | 123456 |
| league_id | TEXT | 关联联赛 ID | lck-cl |
| is_active | INTEGER | 是否启用（0/1） | 1 |
| priority | INTEGER | 采集优先级（高→低） | 10 |
| created_at | TEXT | 创建时间（ISO8601） | 2026-09-21T10:00:00+08:00 |
| updated_at | TEXT | 更新时间（ISO8601） | 2026-09-21T10:00:00+08:00 |

**热生效机制**：
- 采集层每 60 秒读一次 `sources` 表（`SELECT * FROM sources WHERE is_active=1`）
- 对比内存中的房间集合：新增 → 启动采集任务；移除 → 停止任务；修改 → 重启任务
- 无需重启服务，配置变更 ≤60 秒生效

**Web 后台页面**：
- `/admin/sources`：数据源列表（分页 + 搜索 + 筛选）
- `/admin/sources/new`：新增数据源表单
- `/admin/sources/{id}/edit`：编辑数据源
- `/admin/sources/{id}/delete`：软删除（is_active=0）

#### 5.1.2 多平台适配器架构

> **PRD 原文（§5.2 数据源边界）**：
> | 平台 | 用途 | 状态 |
> |---|---|---|
> | 虎牙 | 全部主流直播间（官方流/957/毛毛/米勒/记得/硕硕/CSBOY×2/BLAST 等） | ✅ 主源，必须 |
> | SOOP | LCK CL 韩语官方流（afchall） | ✅ 可选（韩文弹幕需中文化后聚合） |
> | Twitch | — | ❌ 不采集（数据源质量问题，用户明确决定） |
> | KICK | — | ⏸ 预留（结构保留，默认关闭） |

**适配器接口（ABC）**：

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator

class DanmuAdapter(ABC):
    """弹幕采集适配器基类。每个平台实现一个具体适配器。"""
    
    @abstractmethod
    async def connect(self, room_url: str) -> bool:
        """建立连接，返回是否成功"""
        ...
    
    @abstractmethod
    async def listen(self) -> AsyncIterator[dict]:
        """异步迭代器，产出标准弹幕消息"""
        ...
    
    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接"""
        ...
    
    @abstractmethod
    def platform(self) -> str:
        """返回平台标识"""
        ...
```

**标准弹幕消息格式**（所有适配器统一输出）：

```json
{
  "ts": 1724054400.0,
  "nick": "用户名",
  "uid": "用户ID",
  "text": "弹幕内容",
  "source": "来源标识",
  "room_id": "房间ID",
  "platform": "huya"
}
```

**已实现的适配器**：

| 适配器 | 类名 | 平台 | 实现状态 |
|---|---|---|---|
| 虎牙 | HuyaAdapter | huya | ✅ 已有（vendor/real-url） |
| 斗鱼 | DouyuAdapter | douyu | ⏸ 预留 |
| 哔哩哔哩 | BiliAdapter | bilibili | ⏸ 预留 |
| SOOP | SoopAdapter | soop | ⏸ 预留（韩文需中文化） |
| KICK | KickAdapter | kick | ⏸ 预留 |
| Twitch | — | twitch | ❌ 不实现（用户决定） |

**注册表（一行扩展）**：

```python
ADAPTER_REGISTRY = {
    "huya": HuyaAdapter,
    "douyu": DouyuAdapter,
    "bilibili": BiliAdapter,
    "soop": SoopAdapter,
    "kick": KickAdapter,
}
```

新增平台只需：① 实现 `DanmuAdapter` 子类 ② 注册表加一行 ③ 无需改采集层核心逻辑。

#### 5.1.3 直播间发现机制

> **PRD 原文（§7.1 FR-CAP-1）**：
> 数据源：虎牙（官方流/957/毛毛/米勒/记得/硕硕/CSBOY×2/BLAST 等）、SOOP（LCK CL）；
> 每个直播间一个采集任务，独立落盘、独立健康状态、异常自动重启。

**三级发现机制**（优先级从高到低）：

1. **手动配置（最高优先级）**：管理员在 Web 后台手动添加数据源（房间 URL + 平台 + 联赛归属）
2. **赛程联动 API（中优先级）**：每日同步 `matches_today.json` 时，从官方 schedule 提取直播间信息（如 Riot esports-api 的 stream 字段），自动创建数据源（需管理员确认激活）
3. **房间池预注册（最低优先级）**：管理员预注册一组候选房间（如 CSBOY 官方房、957 等），采集层定时检测房间是否开播，开播后自动开始采集

**去重规则**：
- 同一 `room_id` + `platform` 组合唯一
- 同一房间 URL 不同写法（如 `https://www.huya.com/123` 与 `https://m.huya.com/123`）归一化后去重
- 同一房间多个来源（如 maxixi 与 CSBOY 官方房）按 `priority` 选主源

#### 5.1.4 采集起止时机

> **PRD 原文（§7.2 FR-EVT-2）**：
> | 事件 | 触发源 | 幂等键 |
> |---|---|---|
> | 开播 | 官方 schedule 到点 + 弹幕密度突变 | match_slug+pre |
> | BP 锁定 | 官方 window（LoL）/ Liquipedia 地图（CS2）+ 弹幕"BP/选人"话题 | match_slug+g{n}_bp |
> | 局中里程碑 | 比赛时钟 10/20/30 分钟 + 密度峰值 | match_slug+g{n}_mid |
> | 局末 | 官方 gameWins / closed / 结算价 ≥0.99 / 弹幕多信号 | match_slug+g{n}_end |
> | 加时 | CS2 OT 规则检测（比分超 12:12 且未结束） | match_slug+g{n}_ot |
> | 整场结束 | Polymarket 结算仲裁 + 官方 completed | match_slug+full |

**采集起止状态机**：

```
准备阶段（-30min）
  ↓ 官方 schedule 到点 OR 弹幕密度突变（阈值：100条/分持续2分钟）
开始采集
  ↓ 持续采集，心跳检测（每10s）
进行中
  ↓ 四信号齐备（官方比分 + 系列状态 + 弹幕共振 + 流量骤降）
停止采集
  ↓ 超时保护（BO3 最长 4 小时，超时强制停止）
手动干预（管理员可随时启停）
```

**容错机制**：
- 断线重连：≤60s 内自动重连，重连计数（>5 次/小时 → TG 告警）
- 假死检测：120s 无新数据且页面 live=true → 重启该房间任务
- 跨天滚动：采集 session 不因 UTC 日期变更而中断
- 完整性标注：每场情报标注「实际数据源 / 预期数据源 / 缺口」

#### 5.1.5 会话健康与自动重启

> **PRD 原文（§7.1 FR-CAP-3）**：
> 心跳：每个房间 last_message_at；超过阈值（如 120s 无新数据且页面 live=true）判假死，
> 告警并重启该房间任务；
> 断线重连计数与补记完整性；跨天滚动不中断。

**会话状态文件**（`runtime/danmu_sessions/<session_id>/status.json`）：

```json
{
  "schema_version": 1,
  "session_id": "2026-09-21_lck-cl_t1_vs_drx",
  "state": "capturing",
  "started_at": "2026-09-21T19:00:00+08:00",
  "rooms": {
    "huya_123456": {
      "state": "capturing",
      "last_message_at": "2026-09-21T19:30:00+08:00",
      "message_count": 1234,
      "reconnect_count": 0
    }
  }
}
```

#### 5.1.6 验收标准

- [ ] 管理员可在 Web 后台 CRUD 数据源配置（不改 config 文件）
- [ ] 配置变更 ≤60 秒热生效（无需重启服务）
- [ ] 三角色鉴权正确（viewer/user/admin 权限隔离）
- [ ] `/admin/*` 路由仅 admin 可访问
- [ ] 多平台适配器接口统一（`DanmuAdapter` ABC）
- [ ] 新增平台只需实现适配器 + 注册表一行
- [ ] 直播间发现三级机制（手动/赛程联动/房间池）
- [ ] 采集起止状态机正确（5 阶段 + 容错）
- [ ] 断线重连 ≤60s
- [ ] 假死检测（120s 无数据 → 重启）
- [ ] 完整性标注（实际/预期/缺口）
- [ ] 比赛开播后 1 分钟内开始采集
- [ ] 任一房间数据中断有日志与 TG 告警

---

### 5.2 切片层（slice）

**定位**：把「永不改写的原始弹幕流」转成「按比赛 / 小局 / 节点组织的可分析切片」。

> **PRD 原文（§7.3 FR-SLC-1）**：
> 节点定义：赛前（PRE）、BP 后（BP）、局中（MID）、局末（END）、整场（FULL）、
>   CS2 加时（OT）；
> 切片起点 = 本节点阶段起点（官方 schedule + 弹幕密度突变校准确认）；
> 禁止混入上一局尾段、局间闲聊、更早节点数据；
> 整场复盘切片 = 全窗口等距抽样（不只喂尾部）；
> 联赛源硬隔离：切片前按 league_files 过滤，CS2 只进 CS 直播间弹幕，LoL 各联赛只进
>   对应联赛直播间；混源整场作废（void_match_intel）。

**本节其余内容以蓝本真实实现为基准抄录**，来源：
`tools/slice_danmu_by_match.py`（6,583B）、`knowledge/DANMU_CAPTURE_RULES.md`（34,632B）
§1 / §11 / §12 / §19bis、`knowledge/LIVE_INTEL_SCHEMA.md`（5,125B）。

---

#### 5.2.0 先回答核心问题：弹幕的「阶段」到底怎么确定？

阶段不是单一维度，而是**三层正交标签**：

| 层 | 取值 | 判定依据 | 用途 |
|---|---|---|---|
| **宏观阶段** | `S0` `S1` `S2` `S3` `S4` | 局内计时（游戏内分钟） | 局中情报章节递进 |
| **节点** | `pre` `bp` `review`（PRD 目标含 `mid` `end` `ot` `full`） | 事件触发（BP 锁定 / 终局确认） | 一局产出多份情报 |
| **小局** | `g1` `g2` `g3`… | 局间切换 | 逐局切片 |

##### ① 宏观阶段 S0–S4（LoL 通用）

抄自 `knowledge/LIVE_INTEL_SCHEMA.md` §一「比赛阶段划分（LoL 通用）」：

```text
S0 选人阶段（pick/ban）：阵容选择、BP 评价、counter、版本英雄/负面符号、
  英雄熟练度质疑、BP 异常度、教练责任（详见 knowledge/BP_INTEL.md）
S1 0-10 分钟（对线期）：一血、对线压制、打野节奏、镀层
S2 10-20 分钟（资源期）：先锋、前两条龙、经济差、换线
S3 20-30 分钟（中期）：团战节奏、第三条龙、单带、大龙决策
S4 30+（终局）：龙魂、大龙、终结窗口、翻盘窗口
```

**判据要点**：
- 边界按**游戏内时间**（不是墙钟时间）：S0 终点 = BP 锁定；S1 起点 = 开局（进游戏 / 对线开始）；
- CS 与 LoL 分域：CS 无 S0–S4 英雄阶段，改用**图池/选边**（`knowledge/BP_INTEL.md` §4）；
- 输出必须带「当前阶段 + 阶段进度」（如「S3 中期 / 18 分钟」）——`LIVE_INTEL_SCHEMA` §二 第 0 条。

##### ② 节点 pre / bp / review

**证据**：蓝本 `docs/data/intel/node_data/` 实际存在 **187 份**节点文件，命名形如
`2026-08-19_dns_bro_g3_pre.json` / `_bp.json` / `_review.json`；且被 `tools/add_paywall.py`
的 `PRO_RE` 固化为付费层判定规则：

```python
PRO_RE = re.compile(r"intel_danmu_.*_(pre|live|bp|g[1-9])(?:[_.].*)?\.html$", re.I)
```

映射关系：

| 蓝本已实现节点 | 含义 | PRD §7.3 对应 |
|---|---|---|
| `pre` | 赛前（共识、状态核验） | PRE |
| `bp` | BP 阶段（选人即情报） | BP |
| （`live` / `g1`…`g9`） | 局中实时快照 | MID |
| `review` | 赛后复盘 | FULL / END |

> ⚠️ **设计一致性说明**：PRD 列了 6 个节点（PRE/BP/MID/END/OT/FULL），蓝本实际只
> 落地 3 个（pre/bp/review）。本设计**以 PRD 6 节点为目标**，并把蓝本 3 节点作为
> 最小可用子集；`ot`（CS2 加时）为 P1 增量。

##### ③ 小局 game_no

抄自 `knowledge/DANMU_CAPTURE_RULES.md` §11 第 2 条：

> 一场比赛内含多个小局（games[]）：G1/G2/G3 是比赛内部的切片，
> 局间切换（G1->G2）不重建比赛，只切换局标签；只有对阵/系列变更才开新比赛。

---

#### 5.2.1 采集与切片的起止时机

抄自 `knowledge/DANMU_CAPTURE_RULES.md` §1「抓取规则（什么时候开、什么时候停）」：

```text
开抓时机：博主开播且正在解说比赛时（直播页面标题含"XX vs XX"、弹幕讨论比赛内容）。
停抓时机：比赛结束（弹幕出现"GG/结束/下班"或观众讨论下一场时）即停，等下一场再开。
数据落盘：docs/data/danmu/<博主>/<日期>_<房间>.jsonl（JSONL 一行一条）。
字段：ts（秒级时间戳）/ nick（昵称）/ uid / text（内容）/ source（稳定来源）/ room_id。
```

**★ 比赛窗口来源优先级**（抄自 §11 第 4 条）——这是「窗口从哪来」的权威答案：

> 比赛窗口来源优先级：官方开赛/结束时间 > 弹幕 GG/比分信号复核 > 报告数据窗口；
> 小局窗口须可溯源（官方局间时间或弹幕信号），禁止拍脑袋切局。

| 优先级 | 来源 | 落地物 | 使用条件 |
|---|---|---|---|
| 1（最高） | 官方赛程时间 | `docs/data/danmu/schedule.json` | 默认路径；§14.a 要求切片前必须先查 |
| 2 | 弹幕 GG/比分信号复核 | 终局信号检测（§5.2.2） | 官方时间缺失/漂移时反推 |
| 3（兜底） | 报告数据窗口 | 上游报告记录 | 前两者都不可用 |

⛔ **禁止事项**：凭感觉切局（「禁止拍脑袋切局」）；用声明窗口冒充实际窗口
（必须落 `summary.window` 实测值）。

**主播切赛识别**（抄自 §19bis「三信号组合判定」，防止把切台主播的弹幕混进本场）：

```text
背景：957 / 米勒 / 硕硕等主播会切换直播不同比赛，且切换时标题往往不更新，
单纯依赖标题不可靠。改为三信号组合判定。

三信号：
  1. 时间窗预匹配（比赛播出时间表）：
     以 schedule.json / matches_today.json 的比赛起止时间定义"预期窗口"；
     该窗口内，若主播联赛覆盖集（第 19 节清单）含本场联赛 -> 期望本场在播；
  2. 内容实时校验（弹幕内容解读，主信号）：
     窗口内采样弹幕，用 team_names.json 归一统计队伍提及、比分/BP/局内术语；
     与本场队伍高度重合 -> 内容匹配；明显指向同时段另一场 -> 疑似切赛；
     队伍提及稀疏 -> 待核；标题（如可取）仅作辅助，不作主依据；
  3. 主播行为画像（模型逐步学习）：
     每主播长期统计：常播联赛/队伍、跟随率（内容与预期窗口一致占比）、
     切换特征；用于给源加权 + 自适应阈值。

判定与处置：
  1. 时间窗匹配 + 内容匹配        -> 并入（高置信），来源标注主播名；
  2. 时间窗匹配 + 内容指向他场    -> 不并入，该时段标"疑似切赛·数据不相关"；
```

**多直播间同场采集**（抄自 §19，实现「同场比赛多网站采集」）：

> 目标：数据覆盖全面 + 多主播交叉验证；同一比赛有多个直播间数据时尽量全部获取。
>
> 执行细则：
>   1. 比赛确认开播 -> 查本表 + docs/data/intel/leagues.json 该联赛采集集 ->
>      所有在线直播间一次性加入同一 run_danmu_session（跨平台混用，沿用第 17 条）；
>   2. 离线直播间在完整性三栏标注"离线未采"，不算静默缺源；
>   3. 多源交叉验证：两路及以上共振 = 多源确认；单源 = 待验证；方向背离 = 分歧信号；
>   4. 情报页"实际数据源"按第 17bis 优先级排序标注（虎牙在前）。

**采集进程存活信号**（抄自 §14）：

> 页面开播但 120 秒无首条弹幕标记 live_no_danmaku_alert，禁止解释成"无信号"。

---

#### 5.2.2 终局判定：怎么知道「结束了」

抄自 `knowledge/DANMU_CAPTURE_RULES.md` §12「比赛结束判定校验规则（防误判）」，**原文**：

> 1. 禁止用零散弹幕下"已结束/比分"结论。单条或少数弹幕可能是预测/玩梗/剧透/情绪。
> 2. 结束判定需 ≥3 类独立信号共振：
>    a) GG/恭喜/结束类弹幕高密度（连续 ≥2 分钟，达到比赛窗口峰值强度的相当比例，
>       且不只少数几条）；
>    b) 比分/图数信号与直播画面/流标题/官方比分源核对（"2:0"须有对应图数确认）；
>    c) 弹幕流量骤降（窗口密度降到峰值的 <10% 并持续 ≥5 分钟）；
>    d) 官方/第三方比分源（赛事官网、HLTV、官方房间标题）或主播明确宣布。

**可量化的判定参数**（从上述原文提取，供实现直接使用）：

| 信号 | 阈值参数 | 实现要点 |
|---|---|---|
| a 终局词密度 | 连续 ≥2 min；密度达窗口峰值强度的「相当比例」；条数 > 少数几条门槛 | 需定义词表（GG/恭喜/结束/下班/收汁/一波）+ 密度滑窗 |
| b 比分核对 | `"2:0"` 须有对应图数确认 | 与官方比分源交叉，不单信弹幕 |
| c 流量骤降 | 密度 < 峰值 10% 且持续 ≥5 min | 滑窗密度曲线 |
| d 官方源 | 官方比分源 / 主播明确宣布 | 最高权重，可单独成立 |

**终局窗口锚定**（§12 第 10 条，原文标注「最高优先级」）：

> 结果判定必须以"终局窗口"为锚——最后一波/GG/恭喜/结算收盘（如"一波/收汁/减号收了/
> 小33收"）出现并保持 2 分钟无反转才算结果。局中任何"XX拿下/XX赢了/1-1了/让一追二"
> 等表述，只要出现在终局窗口之前，一律先视为预测/情绪，禁止当结果；终局窗口内的
> "随随便便2-0/一波/收"类信号权重最高。教训：FNC-SHFT G2 把局中预测（"shft拿下"00:20、
> "假翻 右边拿下"00:23、"1:1了"00:15 起）当结果，漏看终局信号（"随随便便2-0"00:31、
> "减号收了/小33收"00:32-35），误判 SHFT 胜；实际 FNC 2:0。

**四类必须防的误判**（§12 第 11–13 条，每条都带真实事故）：

| # | 误判类型 | 原文要点 | 处置规则 |
|---|---|---|---|
| 1 | **边位跨局混用** | 「BO3/BO5 每局红蓝边位可互换——"左边/右边/蓝色方"的指代**不能跨局沿用**」 | 必须逐局用「队伍名 + 边位」显式绑定；无队伍名绑定的左右言论只作情绪 |
| 2 | **翻盘无主体** | 「"翻了/翻盘/炼金龙翻了/假翻"等只有方向无主体的信号，**禁止直接定胜负**」 | 用赛后归因确认：被集中批「送/带线/演」的一方 = 输方；顺序 = 终局窗口 → 赛后归因 → 队伍名绑定 → 官方确认 |
| 3 | **局间 hype 当结果** | 「"XX冠军了/XX 2:0 了/带走了/稳了/不用看了"局间 hype 词一律视为情绪」 | 系列结果只等真正终局窗口；小局结束才报小局结果，禁止顺延成系列结果 |
| 4 | **切台/流量降当结束** | 「二路/解说直播间按自己排期切台 ≠ 本场结束」 | 以官方流 / 官方比分源 / **市场 closed** 为准；Polymarket 结算价未到 1.0 时一律「系列进行中/待官方」 |

**状态粒度铁律**（§12 第 13.e，高频踩坑点）：

> 小局结束后的 BP 阶段 = **未开局**，输出一律写"G2 BP 阶段（尚未开局）"，
> 禁止写成"G2 进行中/已开始/已开局"；只有开局（进游戏/对线开始）后才写"G2 局中"。

**低样本与置信度**（§12 第 8 条）：

> 低样本门槛：单源且比赛窗口弹幕 <800 条时，结果默认"低置信·待确认"

**用户异议回退**（§12 第 9 条）：

> 用户异议优先回退：用户对结果提出异议时，先把已输出结论降级为"待确认"，
> 重新交叉验证后再修正落库

---

#### 5.2.3 切片算法（以蓝本真实实现为基准）

蓝本实现在 `tools/slice_danmu_by_match.py`（6,583B / 272 行）。

> ⚠️ **关键事实（必须知道，否则会误设计）**：**脚本本身不做阶段识别**。它读一份
> `manifest.json`，按其中**已写好的窗口**切。阶段判定的责任在 manifest 的产出方（上游，
> 即 §5.2.1/§5.2.2 的规则），脚本只做确定性切片。蓝本仓库内**没有自动生成 manifest 的
> 脚本**（`grep -rln 'slices/manifest'` 只命中文档与脚本自身）——manifest 是人工/分析
> 会话维护的。
>
> **本设计的改动**：把 manifest 生成**自动化**（读 schedule.json + 终局信号 → 产窗口），
> 消灭人工环节。

**处理单元 = MATCH**（一场 BO 系列），不是直播间、不是小时窗口。

**manifest schema**（抄自脚本 docstring 原文）：

```json
{
  "matches": [
    {
      "id": "2026-08-19_wbg_lng",
      "teams": ["WBG", "LNG"],
      "league": "LPL",
      "streams": [
        {"file": "docs/data/danmu/huya/2026-08-19_official_660000.jsonl",
         "source": "official_660000"}
      ],
      "window": {"start": "2026-08-19T15:12:00+08:00", "end": "2026-08-19T16:40:00+08:00"},
      "games": [
        {"game_no": 1, "window": {"start": "...", "end": "..."}},
        {"game_no": 2, "window": {"start": "...", "end": "..."}}
      ]
    }
  ]
}
```

**算法步骤**（逐步对应源码函数，可直接照抄实现）：

| # | 函数 | 逻辑 |
|---|---|---|
| 1 | `load_rows(file)` | 逐行读 JSONL；单行坏 JSON **跳过不报错**（`except json.JSONDecodeError: continue`）——一行坏不毁全量 |
| 2 | `row_ts(r)` | 时间戳归一化：优先 `unixtime`（SOOP），否则 `ts`（虎牙：数字秒 或 ISO 串，`+0800`→`+08:00` 修补）；解析失败返 `None` |
| 3 | `window_ts(w)` | 窗口字符串 → epoch 秒 |
| 4 | `slice_rows(rows, s, e)` | 取 `s <= ts <= e`，按 ts 升序返回 |
| 5 | 多 stream 合并 | 每个 `streams[]` 各切一次 → 合并 → 全局按 ts 排序 → 写 `all.jsonl` |
| 6 | 逐局切分 | 在 `all_rows` 上按每个 `games[].window` 再切 → 写 `game_N.jsonl` |
| 7 | `summarize(rows)` | 产摘要（字段见下表） |
| 8 | index upsert | `index.json` 同 id **先删后加**（幂等重跑不重复） |

**输出目录结构**：

```text
docs/data/danmu/slices/
├── manifest.json          # 输入：窗口清单（本设计将改为自动生成）
├── index.json             # 输出：全部比赛索引
└── <match_id>/
    ├── all.jsonl          # 整场合并（多 source 交叉验证）
    ├── game_1.jsonl       # 逐局
    ├── game_2.jsonl
    └── summary.json       # 摘要
```

**`summary.json` 字段**（抄自源码 `summarize()` 实际返回结构）：

| 字段 | 类型 | 计算方式 | 含义 |
|---|---|---|---|
| `id` / `teams` / `league` | — | 回填 manifest | 比赛标识 |
| `streams[]` | list | `{source, count}` | 各源命中条数（**多源交叉验证依据**） |
| `all.count` | int | `len(rows)` | 切片内弹幕条数 |
| `all.active_users` | int | `len({uid 或 user_id})` | 去重活跃用户数 |
| `all.window.start/.end` | str | 首条/末条 ts | **实际**数据窗口（非声明窗口） |
| `all.sources` | dict | `Counter(source 或 platform)` | 各来源条数分布 |
| `all.gaps_over_10min` | list | 相邻 ts 差 > 600s | 断档区间 `{from, to, gap_min}` |
| `games[]` | list | `{game_no, **summarize(game_rows)}` | 逐局摘要 |

**`gaps_over_10min` 为什么重要**（抄 §11 第 6 条）：

> 数据缺口纪律：切片 summary 自动检测 >10 分钟断档并记录；
> 报告/情报引用切片时必须如实标注缺口（如 TES vs AL 17:32-19:56 断档），
> 禁止把"切片窗口"当"完整数据"。

---

#### 5.2.4 切片数据的用途是什么

**设计意图的数据流**：

```text
原始层 JSONL（永不改写，按流/日落盘）
   └─▶ 切片层 slices/<match>/{all,game_N}.jsonl + summary.json
          ├─▶ 规则统计层：计数 / 密度曲线 / 词频 / 灰信号候选 / BP 统计
          ├─▶ 提炼层：按节点喂「统计摘要 + ≤60 条样本」→ LLM 打字
          └─▶ 缺口审计：gaps_over_10min → 报告「数据与溯源」段
                 └─▶ 校验门禁 → 输出层 HTML/MD → 发布
```

**⚠️ 蓝本现状（必须如实说明）**：`grep -rln 'slices/' tools/` 在蓝本仓库内
**只命中 `slice_danmu_by_match.py` 自身与 `tools/INDEX.md`** —— 即切片产物
**目前没有程序化下游消费**，它是「给人和给分析会话看的中间视图」。

**这就是你觉得 §3.3 讲不清用途的根本原因：在蓝本里它确实还没接上下游。**

**本设计的改动**：明确 3 个消费方，把切片层接进流水线：
1. **规则统计层**（程序）—— 逐局计数、密度曲线、词频、灰信号候选；
2. **提炼层**（LLM）—— 按节点（pre/bp/review）喂对应切片 + 统计摘要；
3. **缺口审计**（监控/报告）—— `gaps_over_10min` 进报告「0-10 数据与溯源」段。

**切片与报告十段结构的对应**：

| 报告段 | 用哪个切片 |
|---|---|
| 0 比赛信息 / 1 结果总览 | `all` + `index.json` |
| 2 逐局复盘 | `game_1/2/3.jsonl` |
| 3 队伍画像 / 4 人员画像 | 逐局切片 + `players.json`/`teams.json` 画像 |
| 5 灰信号汇总 | 全窗口切片（灰信号跨局共振） |
| 6 联赛规律与版本 | 跨场切片聚合（需 DB/索引） |
| 7 预测验证 | 对应节点切片（预测发生的那个节点） |
| 8 盘口讨论 | 含数字/盘口词的切片 + `price_paths.json` |
| 9 情报含义与后续观察点 | 全节点切片 |
| 10 数据与溯源 | `summary.json`（含 gaps） |

---

#### 5.2.5 存储选型：为什么用 JSONL？存数据库有什么优劣？

**为什么 JSONL（4 条理由，按对本项目的权重排序）**：

| # | 理由 | 具体收益 |
|---|---|---|
| 1 | **只增不改（append-only）** | 采集是持续写入的流。JSONL 追加零锁零事务，进程崩溃最多丢最后一行；DB 写入热点需处理并发/锁 |
| 2 | **一行坏不毁全量** | 蓝本 `load_rows()` 对坏行 `continue`。JSONL 天然行级隔离；单文件 JSON 数组一旦截断则整文件作废 |
| 3 | **免 schema 迁移** | 平台字段不统一（虎牙 `ts` / SOOP `unixtime` / KICK、Twitch 又各一套）。JSONL 允许逐行异构，新增平台不动历史数据；DB 需 migration |
| 4 | **可 diff / 可回溯 / 可 grep** | 文本行 = 天然 diff 单位；`git log`、`wc -l`、grep 直接可用，审计友好 |

**存数据库的优劣对比**：

| 维度 | JSONL（蓝本选择） | 数据库（SQLite / Postgres） |
|---|---|---|
| 写入 | ✅ append-only，无锁，流式 | ⚠️ 需事务/批量提交，高并发热点 |
| 查询聚合 | ❌ 每次全量扫文件（O(n)） | ✅ 索引 + 聚合下推，快几个量级 |
| 并发写 | ❌ 蓝本实测「**禁止同时跑两个抓取进程写同一文件（实测会混行）**」 | ✅ 原生并发控制 |
| Schema 演进 | ✅ 免迁移，逐行异构 | ⚠️ 需 migration + 历史回填 |
| 运维 | ✅ 零依赖，一个目录即全部状态 | ⚠️ 需备份/迁移/版本管理 |
| 体积 | ⚠️ 文本冗余大 | ✅ 压缩/列存更省 |
| 可审计 | ✅ 人类可读 + git 友好 | ⚠️ 需导出才可读 |

> **蓝本现状证据**：全仓库 `grep -rln 'sqlite3\|psycopg\|SQLAlchemy\|CREATE TABLE\|duckdb'`
> **零命中**（唯一命中 `tools/label_esports_users.py` 为无关用法）——**蓝本全程零数据库**。

**本设计的取舍（诚实结论，不照抄蓝本）**：

- **原始层 + 切片层继续用 JSONL** —— 理由 1/2/4 对「永不改写的原始证据」是刚需。
- **规则统计层引入 SQLite 做索引与聚合** —— 跨场统计（联赛规律、页面排行、灰信号共振）
  在全量扫文件下随数据量线性劣化，会冲垮 §10.1 的 SLA；SQLite 零运维、单文件，
  符合 AGENTS.md「最简实现 + 优先成熟依赖」。
- **★ 一条分界原则**：
  > **原始证据 = JSONL（append-only，永不改写）；可重算的统计 = DB（随时可从 JSONL 重建）。**
  >
  > DB 不是新的 ground truth，只是**派生缓存**——丢了可以从 JSONL 全量重跑。
  > 这条原则同时保证了：审计可回溯（JSONL 在）+ 查询够快（DB 在）。

---

#### 5.2.6 验收标准

**阶段判定**：
- [ ] 三层标签（S0-S4 / pre-bp-review / game_no）在切片产物中均可定位
- [ ] 每个窗口可回答「来源 = 官方 schedule / 弹幕信号 / 报告兜底」（可追溯，非拍脑袋）
- [ ] 起抓/停抓实现与 §5.2.1 原文一致（开播且解说中开；GG/结束/讨论下一场停）
- [ ] 终局判定实现 ≥3 类信号共振，4 类信号各自阈值有单元测试边界覆盖
- [ ] 回归锁定 §12 第 10 条 FNC-SHFT 案例：局中预测词不得判结果，终局窗口词优先
- [ ] 回归锁定 §12 第 11 条 SK 案例：边位不跨局沿用；无主体翻盘词不得定胜负
- [ ] 回归锁定 §12 第 12 条：局间 hype 词不得判系列结束
- [ ] 回归锁定 §12 第 13 条 TT-LGD 案例：切台/流量骤降不得判系列结束
- [ ] 回归锁定 §12 第 13.e：BP 静默期输出必须为「未开局」
- [ ] 低样本（单源 <800 条）自动降级「低置信·待确认」
- [ ] 用户异议可将已输出结论降级「待确认」并重新交叉验证

**切片实现**：
- [ ] 每个切片文件带窗口起止时间（UTC + 北京时间）
- [ ] 单行坏 JSON 不中断整批（坏行跳过并可计数）
- [ ] `unixtime` 与 `ts` 两种时间戳均能归一化（含 `+0800` 修补）
- [ ] 多 stream 合并后按 ts 全局有序
- [ ] manifest 幂等重跑不产生重复切片（index upsert）
- [ ] 跨联赛混源 = 0（回归测试锁定）
- [ ] 混源整场作废（`void_match_intel`）
- [ ] 整场复盘切片 = 全窗口等距抽样（不只喂尾部）
- [ ] `summary.json` 含 count / active_users / window / sources / gaps_over_10min
- [ ] 切片产物有明确下游消费方（规则层 + 提炼层），不得「只生成不消费」

**存储分工**：
- [ ] 原始层与切片层为 append-only JSONL，无原地改写路径
- [ ] 规则统计层的 SQLite 可由 JSONL 全量重建（重建脚本 + 测试）
- [ ] DB 丢失不导致证据丢失（JSONL 完备性测试）

### 5.3 规则统计层（rules）


#### 5.3.0 规则统计层产物清单（★ 用户问「规则统计层的诸多 json 数据」）

**蓝本实际产物**（来源：`find docs/data/intel -name '*.json'`、`ls docs/data/intel/node_data | wc -l`，
以及 `knowledge/DANMU_CAPTURE_RULES.md` §14 的三件套定义 —— 非虚构清单）：

| JSON | 位置 | 用途 | 消费方 |
|---|---|---|---|
| `matches.json` | `docs/data/intel/` | 比赛主档，含 `games[].bp_intel` | 页面生成、情报引用 |
| `players.json` | 同上 | 选手画像（近期状态 / 焦点英雄） | `LIVE_INTEL_SCHEMA` §三 引用 |
| `leagues.json` | 同上 | 联赛规律 + **每联赛默认采集集**（§19 直播源匹配用） | 采集层选源、统计层联赛口径 |
| `bp_signals.json` | 同上 | 单场 BP 信号（含赛后 `verdict` 回填） | BP 情报、赛后验证 |
| `bp_entities.json` | 同上 | 教练 / 选手 BP 留痕（再犯升级） | 灰信号升级链 |
| `rosters.json` | 同上 | 阵容名册 | 归属判定 |
| `aliases.json` | 同上 | 队名 / 选手别名归一 | 弹幕文本归一（`normalize`） |
| `compositions.json` | 同上 | 阵容 / 体系库 | BP 评价 |
| `maps.json` | 同上 | CS 地图池 | CS 分域统计 |
| `price_paths.json` | 同上 | 盘口价格轨迹 | 「盘口讨论」段、灰信号共振判定 |
| `streamer_profiles.json` | 同上 | 主播画像（常播联赛 / 跟随率） | §19bis 切赛识别 |
| `validation_samples.json` | 同上 | 预测验证样本 | 「预测验证」段 |
| `node_data/*.json` | `docs/data/intel/node_data/` | **187 份**节点情报，命名 `<date>_<a>_<b>_g<N>_{pre,bp,review}.json` | `tools/build_node_page.py` |
| `intel_signals.json` | `knowledge/` | 主观情报信号库（25,465 B） | 情报库检索 |
| `intel_signal.schema.json` | `schemas/` | 上述的 JSON Schema 契约（7,915 B） | 写库前校验 |

**BP 情报三件套**（抄自 `knowledge/DANMU_CAPTURE_RULES.md` §14）：

```text
(a) docs/data/intel/bp_signals.json  —— 单场 BP 信号
(b) docs/data/intel/bp_entities.json —— 教练/选手留痕（再犯升级）
(c) docs/data/intel/matches.json 的 games[].bp_intel —— 归入比赛档案
```

**`intel_signal.schema.json` 的 `required` 字段**（逐字抄自 schema 文件）：

```text
id                  信号编号，pattern ^IS-\d{4}-\d{2}-\d{2}-\d{3}$   （例 IS-2026-08-31-001）
date                采集日期 YYYY-MM-DD
event_slug          比赛 slug                      （例 lol-t1-hle1-2026-08-08）
source_person       来源人 / 来源房间              （例 957 / 二路解说多人 / 弹幕聚合）
source_type         枚举：caster_co | caster_official | streamer | official | community
credibility         可信度
quote               原话引用
tags                标签数组
object / object_type  指向对象（队伍 / 选手 / 教练）
direction           方向
timing              时机
verification        验证状态
```

**为什么规则层用 JSON（而不是也塞进 JSONL）**：

规则层是**结构化小对象集合**（固定字段、有 JSON Schema 契约、需按 `id` 去重与 upsert），
不是逐行追加的流。JSON + Schema 能直接约束「整份合法」；用 JSONL 反而丢掉整体校验能力，
且 upsert（如 `bp_signals.json` 回填 `verdict`）在 JSONL 上很难做。

**三层存储分界原则**（与 §5.2.5 一致，全项目统一）：

| 数据性质 | 载体 | 理由 |
|---|---|---|
| 流式原始证据（弹幕） | **JSONL**（append-only，永不改写） | 一行坏不毁全量、免 schema 迁移、可 diff |
| 结构化小档（规则层产物） | **JSON** + JSON Schema | 需整体校验、需 upsert、体量小 |
| 需要跨场聚合查询 | **SQLite**（本设计新增，可从 JSONL/JSON 重建） | 索引与聚合性能 |

> ⚠️ 蓝本现状：全仓库 `grep -rln 'sqlite3\|psycopg\|SQLAlchemy\|CREATE TABLE\|duckdb'` **零命中**
> （唯一命中 `tools/label_esports_userl.py` 为无关用法）—— 蓝本**全程零数据库**，聚合全靠扫文件。

**验收标准**：
- [ ] 上表每份 JSON 都有对应 JSON Schema（或字段说明），写库前校验
- [ ] `node_data/` 命名规范固化，`_pre` / `_bp` / `_review` 三节点齐全（缺一即告警）
- [ ] 规则层 JSON 的 upsert 幂等（同 id 重复写结果一致）
- [ ] `intel_signals.json` 的每条记录通过 `intel_signal.schema.json` 校验
- [ ] SQLite 派生视图可仅由 JSONL + JSON 完整重建（重建脚本 + 测试）
- [ ] 删除 SQLite 文件后系统仍能工作（仅降速，不丢数据）

**职责**：词表统计 + 密度时间线 + 灰信号计数。**纯确定性输出**。

> **PRD 原文（§7.4 FR-RUL-1）**：
> 词表：队伍（team_names.json 唯一权威）、选手、黑话双语映射、联赛词、灰信号词；
> 统计输出：弹幕总数、活跃用户、密度（条/分）、队伍/选手提及量（正/负）、正负锚、
>   群体共识主题、灰信号条数与样本、团队特质（TRAIT_KW 8 类）；
> 词表维护：半自动（LLM 发现候选词 → 人工确认入表 → 回归测试），禁止全自动
>   （防 OKBRO / bro tax 类玩梗误收）。

#### 5.3.1 词表结构

**`config/team_names.json`（队伍命名唯一权威）**：

```json
{
  "teams": [
    {
      "id": "T1",
      "abbr": "T1",
      "full": "T1",
      "aliases": ["T 1", "T一", "SKT", "SKT T1", "T1 Esports"]
    },
    {
      "id": "DRX.C",
      "abbr": "DRX.C",
      "full": "DRX Challengers",
      "aliases": ["DRX二队", "DRX 挑战者", "DRX CL"]
    }
  ]
}
```

**词表类型**：

| 词表 | 文件 | 说明 |
|---|---|---|
| 队伍 | config/team_names.json | 唯一权威，abbr 展示，全称用于详情 |
| 选手 | config/players.json | 选手 ID / 别名 / 所属队伍 |
| 黑话 | config/slang.json | 双语映射（中/英/韩） |
| 灰信号 | config/gray_signals.json | 假赛/剧本/卡盘质疑关键词 |
| 团队特质 | config/traits.json | TRAIT_KW 8 类（进攻/防守/运营/团战/BP/心态/版本/其他） |

#### 5.3.2 统计输出（intel.json）

**`data/intel/<match_slug>_g{game_no}_{node_type}.intel.json`**：

```json
{
  "meta": {
    "total": 1234,
    "active_users": 567,
    "density_per_min": 246.8,
    "window_utc": ["2026-09-21T11:00:00Z", "2026-09-21T11:05:00Z"],
    "window_cn": ["2026-09-21T19:00:00+08:00", "2026-09-21T19:05:00+08:00"]
  },
  "teams": {
    "T1": {
      "mentions": 456,
      "pos": 230,
      "neg": 226,
      "samples": ["T1 加油", "T1 要翻了", "T1 稳了"]
    }
  },
  "players": {
    "Faker": {
      "mentions": 123,
      "pos": 80,
      "neg": 43,
      "samples": ["Faker 还是稳", "Faker 老了"]
    }
  },
  "gray_signals": {
    "count": 23,
    "samples": ["这剧本吧", "假赛嫌疑", "卡盘了"]
  },
  "odds_discussion": {
    "count": 45,
    "samples": ["T1 赔率多少", "买 T1 独赢"]
  },
  "density_timeline": [
    {"minute_utc": "2026-09-21T11:00:00Z", "count": 120},
    {"minute_utc": "2026-09-21T11:01:00Z", "count": 200}
  ],
  "density_bursts": [
    {"minute_utc": "2026-09-21T11:03:00Z", "count": 500, "samples": ["一血！", "First Blood"]}
  ],
  "team_traits": {
    "T1": {
      "进攻": 45,
      "团战": 67,
      "运营": 89
    }
  }
}
```

#### 5.3.3 密度时间线算法

> **PRD 原文（§7.4 FR-RUL-2）**：
> 按分钟聚合弹幕条数，输出密度峰值（时间+条数+代表样本）；
> 事件反应识别：一血/团战/偷家/赛点/加时等关键节点弹幕爆发。

**步骤**：

1. 按分钟聚合：`SELECT strftime('%Y-%m-%dT%H:%M:00', ts, 'unixepoch') AS minute, COUNT(*) FROM danmu GROUP BY minute`
2. 峰值检测：当前分钟条数 > 前 5 分钟均值 × 2 → 标记为密度峰值
3. 样本提取：从峰值分钟内随机抽取 ≤3 条代表弹幕
4. 事件反应识别：峰值分钟内包含"一血/团战/偷家/赛点/加时"等关键词 → 标记事件类型

#### 5.3.4 验收标准

- [ ] 规则层输出 intel.json 字段固定、可回归
- [ ] 漏词有测试告警
- [ ] 灰信号只作聚合与风险标注
- [ ] 密度时间线与真实弹幕时间戳一致，无编造/平移
- [ ] 词表维护半自动（LLM 候选 → 人工确认 → 回归测试）

---

### 5.4 提炼层（refine · LLM 接口化）★ 核心差异点

**职责**：程序组装固定提示词 + 规则层统计摘要 + 代表弹幕样本，直连 DeepSeek API。

> **PRD 原文（§7.5 FR-REF-1）**：
> 程序组装固定提示词（prompts/ 目录：report_full / report_game / report_pre /
>   report_live / intel_asset）+ 规则层统计摘要 + 代表弹幕样本（≤60 条、每条 ≤50 字、
>   带北京时间戳），直连 DeepSeek API；
> 模型 deepseek-chat、temperature 0.3、max_tokens 首调 8000 / 重试 16000、重试 ≤3 次；
> **LLM 只当"打字员"**：结构/标准/校验全在程序，换模型不影响质量；
> 每次调用记录 usage（输入/输出 tokens）与成本。

#### 5.4.1 固定提示词（prompts/ 目录）

| 提示词文件 | 用途 | 输入 |
|---|---|---|
| prompts/report_full.md | 整场复盘 | 全窗口统计 + ≤60 条样本 |
| prompts/report_game.md | 单局复盘 | 单局统计 + ≤40 条样本 |
| prompts/report_pre.md | 赛前情报 | 历史画像 + ≤30 条样本 |
| prompts/report_live.md | 局中快报 | 当前局统计 + ≤20 条样本 |
| prompts/intel_asset.md | 情报资产提炼 | 本场全部节点情报 |

#### 5.4.2 输入组装

```python
def assemble_input(intel: dict, samples: list, prompt_template: str) -> dict:
    """组装 LLM 输入"""
    return {
        "system": prompt_template,  # 固定提示词
        "user": f"""
【规则层统计摘要】
{json.dumps(intel, ensure_ascii=False, indent=2)}

【弹幕样本（{len(samples)} 条，每条 ≤50 字，带北京时间戳）】
{format_samples(samples)}
"""
    }
```

#### 5.4.3 LLM 调用参数

| 参数 | 值 |
|---|---|
| model | deepseek-chat |
| temperature | 0.3 |
| max_tokens | 首调 8000，重试 16000 |
| 重试次数 | ≤3 次（指数退避：2s / 4s / 8s） |
| 超时 | 180 秒 |
| response_format | json_object |

#### 5.4.4 成本记录

```json
{
  "match_slug": "2026-09-21_lck-cl_t1_vs_drx",
  "node_type": "full",
  "model": "deepseek-chat",
  "input_tokens": 3300,
  "output_tokens": 7900,
  "cost_rmb": 0.05,
  "called_at": "2026-09-21T20:00:00+08:00"
}
```

#### 5.4.5 验收标准

- [ ] 单页输入 ≈3.3k / 输出 ≈7.9k tokens，成本 ≈0.05 元/页
- [ ] LLM 调用失败重试 ≤3 次
- [ ] 每次调用记录 usage 与成本
- [ ] 换模型不改流程（provider 接口化）

---

### 5.5 校验门禁层（verify）

**职责**：结构门禁 + 事实层门禁 + 终局四信号 + 来源反编造。

> **PRD 原文（§7.6）**：
> 
> **FR-VRF-1 结构门禁**：
> 每段 `<h2><span class="no">N</span>标题</h2>` 恰好出现 1 次（缺段/重复判不过并重试）；
> 段落标题关键词按节点类型校验（A 型 0-10 / B 型 0-11 / 赛前 0-8）。
> 
> **FR-VRF-2 事实层与结果门禁（match_state_guard 四道闸）**：
> 闸1 时间门槛：比赛已开始且进行 ≥30 分钟才允许判结束；未开始/刚开赛只走赛前/局中逻辑；
> 闸2 结构源优先：官方比分/比分机器人/权威比分站/用户确认 > 弹幕情绪；
> 闸3 反讽识别：英文/韩文/中文弹幕大量反讽玩梗（"HOLY X""自己回家"等）先做语气判定，
>     无法判定降级为"未确认"；
> 闸4 比分源滞后：CS2 Liquipedia/官方页可能滞后数分钟（今日 blast.tv 滞后 5-10 分钟），
>     终局必须多源交叉；"领先 2 分/加时块"规则按 config 参数化。
> 
> **FR-VRF-3 来源与反编造门禁**：
> 无中生有零容忍：无本场/前局/历史数据支撑的不写；
> 跨场话题（NiKo/Falcons、donk/Spirit、Zywoo/VIT）标注「跨场话题·不适用本场」；
> 时间线只用真实弹幕时间戳，禁止编造/换算/平移；
> 禁止无源编造胜率数字（预测/弹幕/估算胜率需带证据来源）；
> 页面必须带"本页数据截至 <时间>"；官方比分待回填时显式标注。
> 
> **FR-VRF-4 终局四信号定稿（2026-08-31 实战固化）**：
> 信号1 官方比分（结构源：官方 API / Liquipedia / HLTV / 战报，按优先级）；
> 信号2 官方系列状态（如 blast.tv 1:-:1、Polymarket 结算）；
> 信号3 弹幕多信号共振（结束语高密度 + 比分核对 + 晋级/回家共识，跨直播间）；
> 信号4 流量骤降（比赛结束后的正常回落，或直接进入下一场）。
> 四信号齐备才允许发布"已结束"结论；否则标"进行中/待官方"。

#### 5.5.1 结构门禁实现

```python
def verify_structure(html: str, node_type: str) -> tuple[bool, list[str]]:
    """校验 HTML 结构"""
    expected_sections = {
        "A": list(range(0, 11)),   # 0-10
        "B": list(range(0, 12)),   # 0-11
        "pre": list(range(0, 9)),  # 0-8
    }
    # 每段 <h2><span class="no">N</span>标题</h2> 恰好出现 1 次
    for n in expected_sections[node_type]:
        count = html.count(f'<span class="no">{n}</span>')
        if count != 1:
            return False, [f"Section {n} count={count}, expected 1"]
    return True, []
```

#### 5.5.2 终局四信号实现

```python
def verify_endgame(match_slug: str) -> dict:
    """终局四信号校验"""
    return {
        "signal1_official_score": check_official_score(match_slug),
        "signal2_series_status": check_series_status(match_slug),
        "signal3_danmu_resonance": check_danmu_resonance(match_slug),
        "signal4_traffic_drop": check_traffic_drop(match_slug),
    }

def can_publish_endgame(signals: dict) -> bool:
    """四信号齐备才允许发布"""
    return all(signals.values())
```

#### 5.5.3 验收标准

- [ ] A 型 0-10 / B 型 0-11 / 赛前 0-8 每段恰好一次
- [ ] 缺段/重复判不过并重试
- [ ] 终局四信号齐备才允许发布"已结束"
- [ ] 无中生有零容忍
- [ ] 时间线只用真实弹幕时间戳
- [ ] 页面必须带"本页数据截至"

---

### 5.6 输出层（render）

**职责**：HTML + MD 双格式输出。

> **PRD 原文（§7.7）**：
> 
> **FR-RND-1 模板唯一标准**：
> 唯一标准 = knowledge/INTEL_TEMPLATE_OLD_2026-08-31.md；
> A 型整场（0-10）：比赛信息 → 结果总览（弹幕口径·待回填）→ 逐局复盘（每局含阵容）
>   → 队伍画像（引用 teams.json）→ 人员画像（带提及量）→ 灰信号汇总 → 联赛规律与版本
>   → 预测验证（闭环）→ 盘口讨论（Polymarket 交叉）→ 情报含义与后续观察点（LONG/SHORT）
>   → 数据与溯源（实际/预期/缺口）；
> B 型局中（0-11）：在 A 基础上加「状态核验 / 密度时间线 / 方向性情报」；
> 赛前（0-8）：比赛信息 → 赛前共识与状态核验 → 队伍画像 → 人员画像 → 灰信号 →
>   联赛规律 → 盘口讨论 → 情报含义与观察点 → 数据与溯源；
> 方向表达只罗列证据（正锚/负锚/群体共识），不下「看好 X」结论；
> 加厚版要求：速览焦点（≤5）+ `<details>` 折叠证据层（弹幕原文/完整时间线/长画像）+
>   逐局弹幕时间线（真实时间戳带量）+ 共识 ≥5 行 + 整场密度目标 ≥16KB。
> 
> **FR-RND-2 快报 → 完整版同 URL 升级**：
> BP 后/局中先出快报：规则直出硬统计（比分/提及量/灰信号数/密度），
>   页面标注「速览版 · 完整版跟进」，1-2 分钟内上线；
> 完整版 ≤10 分钟跟进，同 URL 原地覆盖升级（不产生第二个页面）；
> 局末/整场直接完整版。
> 
> **FR-RND-3 双格式与镜像**：
> HTML（站点展示，SAP/Apple 风格：#f5f5f7 浅底、白圆角卡片、#0071e3 强调色、
>   系统字体栈、移动端优先）+ MD（入库核心）；
> MD 镜像必出，同步 knowledge/intel_pages/ 与 README 索引；
> 时间一律北京时间展示；每页数据截至时间戳必须存在。

#### 5.6.1 快报 → 完整版升级流程

```
事件触发 → 规则直出快报（硬统计，无语义结论）→ 上线（同 URL）
  → 后台生成完整版（模板 + 溯源）→ 门禁通过 → 同 URL 覆盖
页面始终只有一个 URL；快报期页面标注「速览版 · 完整版跟进」。
```

#### 5.6.2 验收标准

- [ ] 快报 ≤2 分钟上线
- [ ] 完整版 ≤10 分钟同 URL 覆盖
- [ ] HTML + MD 双格式
- [ ] SAP/Apple 风格（#f5f5f7 / 白圆角卡片 / #0071e3）
- [ ] 时间一律北京时间
- [ ] 每页数据截至时间戳存在

---

### 5.7 发布层（publish）

**职责**：站点生成 + 付费墙 + 发布审计。

> **PRD 原文（§7.8）**：
> 
> **FR-PUB-1 站点生成**：
> 页面类型：今日情报（赛程+节点进度+节点入口）、历史情报库、画像库、灰信号统计、
>   验证闭环、订阅页；
> 今日页每场必须展示节点进度（未开始/进行中/已结束 + 已出节点）；
> 时间轴壳（match_<slug>.html）必须与节点页同步（禁止「页面上线但入口显示暂无」）；
> 比赛一结束，该场所有节点页自动转免费公开。
> 
> **FR-PUB-2 付费墙**：
> Pro = 进行中的节点页 + 完整画像；其余全公开；
> 付费墙注入按「比赛是否已结束」判定（settlements / 全小局 closed / 结果回填），
>   禁止按文件名一刀切；
> 会员验证：TG 用户名/QQ 号 × 名单 × expires，本地 24h 缓存。
> 
> **FR-PUB-3 发布前审计与回滚**：
> 全站审计：导航唯一、无旧模板残留、Pro 页付费墙齐全、节点完整性、
>   页面 × 联赛 × slug 关联一致、速览卡残留为 0；
> 审计不通过 = 不发布；
> 发布链路：VPS → 站点仓库 → GitHub Pages（或 nginx 直出），目标 ≤1 分钟；
> 异常时保留上一版站点，修复后重新发布。

#### 5.7.1 验收标准

- [ ] 比赛结束 → 该场全部节点页转免费公开
- [ ] 付费墙按「比赛是否已结束」判定
- [ ] 审计不通过 = 不发布
- [ ] 发布链路 ≤1 分钟
- [ ] 异常时保留上一版站点

---

### 5.8 订阅层（subscribe）

**职责**：订阅登记 + 会员验证 + 到期管理。

> **PRD 原文（§7.9）**：
> 订阅页登记表单（昵称/TG 或 QQ/档位/备注）→ 推送到站长 TG；
> 试用档：1 美元 3 天统一付费试用；7 天免费名额仅信任圈/推广；
> 会员名单 members.json（VPS 接口优先，可回退 Gist）；
> 到期前 TG 自动提醒（剩余 N 天），到期自动失效。

#### 5.8.1 验收标准

- [ ] 订阅表单 → 推站长 TG
- [ ] 会员验证（TG/QQ × 名单 × expires）
- [ ] 到期前 TG 提醒
- [ ] 到期自动失效

---

### 5.9 情报库沉淀层（library · 知识资产）

**职责**：本场情报资产提炼 + 四维知识库 + 验证闭环。

> **PRD 原文（§7.10）**：
> 
> **FR-LIB-1 本场情报资产提炼（每场必做）**：
> 每场比赛结束后，基于本场全部节点情报产出「本场情报资产」；
> 资产固定结构：
>   `{主题, 维度(选手/队伍/英雄/联赛), 洞察一句话, 证据(时间+来源), 置信, 验证状态, 时间}`；
> 禁止把提及量/密度等原始统计直接当资产写入（统计只作佐证字段）；
> 至少覆盖：选手英雄池/风格新信号、队伍战术与 BP 倾向、英雄强度与 counter 共识、
>   联赛规律、灰信号及其兑现、观众黑话。
> 
> **FR-LIB-2 四维知识库（长期积累）**：
> | 维度 | 沉淀内容 |
> |---|---|
> | 选手 | 英雄池与招牌、风格标签、状态趋势、关键名场面、观众共识与黑话、BP 锚点、灰信号 |
> | 队伍 | 战术体系与版本适应、BP 倾向、翻盘/顺风能力、选手组合、状态趋势、跨场战绩、灰信号 |
> | 英雄/地图 | 强度认知与版本理解、counter 关系、出场/胜率趋势、观众共识、CS2 队伍×地图强图 |
> | 联赛 | 晋级规则/节奏/版本环境、强队格局、盘口习惯、赛季特征 |
> 
> 摘要免费展示，完整知识 Pro；画像引用必须带时间，禁止用历史画像冒充本场数据。
> 
> **FR-LIB-3 沉淀引擎（幂等合并与趋势）**：
> 幂等：同场不重复入库（runtime/accumulated_matches.json 防重复）；
> 合并规则：新资产追加；旧认知被新证据更新时保留时间与来源；
> 单源 = 待验证、多源共振 = 确认、方向背离 = 分歧（单独标注）；
> 趋势线：实体维度随比赛数延伸，以时间轴展示；
> 验证回填：灰信号兑现率、BP 锚点应验率、预测命中率持续累加。

#### 5.9.1 情报资产结构

```json
{
  "id": "asset_20260921_t1_faker_001",
  "match_slug": "2026-09-21_lck-cl_t1_vs_drx",
  "theme": "Faker 英雄池扩展",
  "dimension": "player",
  "insight": "Faker 近 3 场频繁使用阿兹尔，胜率 80%",
  "evidence": [
    {"time": "2026-09-21T19:30:00+08:00", "source": "huya_official", "text": "Faker 阿兹尔 NB"}
  ],
  "confidence": 0.8,
  "verification_status": "pending",
  "created_at": "2026-09-21T20:00:00+08:00"
}
```

#### 5.9.2 验收标准

- [ ] 每场比赛自动提炼情报资产
- [ ] 资产结构固定（主题/维度/洞察/证据/置信/验证状态/时间）
- [ ] 幂等：同场不重复入库
- [ ] 四维知识库可查、可溯源、可验证
- [ ] 验证回填持续累加

---

### 5.10 监控运维层（monitor）

**职责**：自检 + SLA 指标 + 成本指标 + 告警。

> **PRD 原文（§7.11）**：
> 自检：每日检查今日清单完整性、节点缺口、结果回填状态、站点审计；
> SLA 指标：每节点记录触发 → 快报 → 完整版 → 上线四段时间戳；
> 成本指标：每页记录 LLM usage 与成本，日报输出；
> 告警规则：
>   - 采集断线 / 0 条数据；
>   - 节点超时（快报 >2min / 完整版 >10min / 复盘 >15min）；
>   - 发布失败 / 审计不通过；
>   - 成本异常（单日超阈值）；
>   - 比赛结束但复盘未出（>15min）；
>   - 空结果必须过自检：禁止报「今日无比赛/无信号」而不告警。

#### 5.10.1 验收标准

- [ ] 每节点四段时间戳可查
- [ ] SLA 达标率日报
- [ ] 成本日报
- [ ] 异常 TG 告警
- [ ] 空结果必须过自检

---


### 5.11 对外 API

> **PRD 原文（§7.12「对外 API」）**：
> | API | 用途 | 说明 |
> |---|---|---|
> | `POST /api/lead` | 订阅登记 | 表单 → 推站长 TG |
> | `POST /api/verify-member` | 会员验证 | TG/QQ × 名单 × expires → 解锁 |
> | `GET /api/stats` | 站点统计 | 累计 / 今日 PV、页面排行 |

**本节以蓝本 `tools/stats_server.py`（8,067 B）与 `tools/add_paywall.py`（8,278 B）
的真实实现为基准抄录**，不是设计构想。

#### 5.11.0 接口总表（真实路由）

| 层 | 路由 | 方法 | 鉴权 | 数据落点 / 来源 | 调用方 |
|---|---|---|---|---|---|
| VPS `stats_server.py`（:8080，systemd `stats-server.service`） | `/track` | POST | header `X-Stats-Secret` | 追加 `runtime/stats/events.jsonl` | Vercel 转发 |
| 同上 | `/lead` | POST | header `X-Stats-Secret` | 追加 `runtime/leads.jsonl` | Vercel 转发 |
| 同上 | `/stats` | GET | `?secret=` | 只读聚合 | 站长 / 后台 |
| 同上 | `/leads` | GET | `?secret=` | 只读明细 | 站长 / 后台 |
| 同上 | `/members` | GET | `?secret=` | 只读 `members.json` | 付费墙 |
| Vercel `danmu-intel-api.vercel.app` | `/api/track` | POST | **公开** | → VPS `/track` | 页面打点脚本 |
| Vercel | `/api/lead` | POST | **公开** | → VPS `/lead` + 推 TG | 订阅表单 |
| Vercel | `/api/verify-member` | POST | **公开** | 读 `members.json` | 付费墙弹窗 |

`stats_server.py` 是零依赖 `ThreadingHTTPServer`（标准库），默认端口 8080，无框架。

#### 5.11.1 三个必须先说清的事实（否则实现必踩坑）

**① `POST /api/verify-member` 的实现不在蓝本仓库里。**

```text
$ grep -rln "verify-member\|verify_member" .
docs/task/DANMU_INTEL_CLOUD_PYTHON_PROJECT_PRD.md
tools/add_paywall.py        ← 只是调用方
```

蓝本**没有 `api/` 目录、没有 `vercel.json`** —— 它在 Vercel 项目 `danmu-intel-api.vercel.app`
里。也就是说：**付费链路有一半代码游离在版本控制之外**。

→ 这直接回答「verify-member 是给谁用的」：**给付费墙弹窗用的**（客户端 `fetch`），
调用方是**浏览器里的访客**，鉴权 = **无**（凭 `identifier` 查名单）。
→ 本设计必须把它**收回本仓库**（见 §15.6）。

**② `GET` 类接口的 secret 走 URL query**（`/stats?secret=...`），会进 access log。
→ 设计改为**统一走 header**。

**③ `/track` 与 `/lead` 是公开接口**（无鉴权，任何页面都能打点）。
→ 需要反滥用：**限流 + 来源校验**（v1 先做限流）。

#### 5.11.2 逐个接口的完整契约

**`POST /track`** —— 页面浏览打点

```text
headers: X-Stats-Secret: <STATS_SECRET>
body:    {"page": "/intel/intel_danmu_lck_...", "ref": "", "visitor": "<uuid>", "site": "danmu"}
```

VPS 落盘一行（所有字符串字段**截断防注入**）：

```json
{"ts":"2026-08-26T21:03:11+08:00","page":"/intel/...","ref":"","visitor":"<uuid>","site":"danmu"}
```

| 字段 | 截断上限 | 说明 |
|---|---|---|
| `ts` | — | 服务端生成，北京时间 ISO8601 |
| `page` | ≤200 | 页面路径 |
| `ref` | ≤200 | 来源页 / referrer |
| `visitor` | ≤64 | **匿名访客 ID**（见 §5.11.3） |
| `site` | ≤32 | 站点标识（`danmu` / `musk` / …），支持多站点合并统计 |

**`POST /lead`** —— 订阅登记留痕

```text
body: {"name":"...","contact":"...","plan":"...","note":"..."}
```

落盘 `runtime/leads.jsonl`，截断 name ≤100 / contact ≤100 / plan ≤60 / note ≤200。

源码注释原文（说明它为什么存在）：

> 订阅登记留痕（2026-08-26：表单只推 TG 无记录，漏通知即丢；现在落盘可审计）

**`GET /stats?secret=&site=`** —— 站点统计聚合

返回结构（抄自 `stats()` 返回值）：

```json
{
  "date": "2026-08-26",
  "site": "danmu",
  "total_views": 12345, "today_views": 456, "week_views": 3210,
  "total_visitors": 890, "today_visitors": 37, "week_visitors": 210,
  "days": [{"date": "2026-08-13", "views": 100, "visitors": 20}],
  "top_pages": [["/intel/intel_danmu_...", 120]]
}
```

- `days` = **近 14 天逐日**；`top_pages` = 前 15 页面排行
- `site` 不传 = 全部站点合并（兼容旧调用）

**`GET /leads?secret=`** —— 订阅登记明细（按 `ts` 倒序）

**`GET /members?secret=`** —— 会员名单（读 `members.json`；缺失返回 `{"members": []}`）

**`POST /api/verify-member`**（Vercel 端，本设计收回本仓库）

```json
请求  {"identifier": "<TG用户名或QQ号>"}
响应  {"member": true, "expires": "2026-10-01T00:00:00+08:00"}
```

客户端缓存：`localStorage["danmu_member_v1"] = {member, expires, checkedAt}`，
**24 小时内不重复校验**（判据 `Date.now() - checkedAt < 86400000`）。

#### 5.11.3 站点统计（★ 用户问「如何统计站点数据」）

**打点链路（三段）**：

```text
① 页面注入脚本（tools/add_stats_track.py 注入到所有 HTML 的 </body> 前）
   ├─ 访客 ID：localStorage["di_v"]，首次访问生成 crypto.randomUUID()，之后复用
   └─ fetch POST https://danmu-intel-api.vercel.app/api/track
        {page: location.pathname, visitor: v}      ← catch 静默失败，不阻塞页面
② Vercel /api/track 中转（附 X-Stats-Secret）
③ VPS stats_server.py /track → 追加 runtime/stats/events.jsonl
```

**PV / UV 定义**：

| 指标 | 定义 |
|---|---|
| **PV** | `events.jsonl` 行数（按 `site` + 时间窗过滤） |
| **UV** | `visitor` 字段的 `distinct` 计数 |
| 今日 | `ts[:10] == today` |
| 本周 | `day >= today - 6` |
| 累计 | 全部 |
| 逐日 | 近 14 天 |

**★「能知道每天有多少人来过付费页面？都是谁？」—— 分两问答：**

**① 有多少人来过付费页？能，而且现成。**

`events.jsonl` 每行都带 `page`，直接按 `page` 过滤聚合即可。现有 `/stats` 已返回
`top_pages`（页面排行）与 `days`（近 14 天逐日 PV/UV）。

- 方案 a（零改动）：`grep '"page":"/subscribe' runtime/stats/events.jsonl | wc -l`
- 方案 b（推荐，本设计采用）：给 `/stats` 加 `page` 过滤参数，返回该页的
  `today` / `week` / `total` + `days[]` 曲线

**②「都是谁」？—— 现有实现做不到，这是必须诚实说明的边界。**

`visitor` 是 **localStorage 里的随机 UUID**，它只能回答「**同一个浏览器来过几次**」，
**不能回答「这个人是谁」**：

| 偏差 | 情形 | 后果 |
|---|---|---|
| 换浏览器 / 清缓存 / 隐私模式 | 同一人不同 UUID | **UV 虚高** |
| 同一设备多人共用 | 多人同一 UUID | **UV 虚低** |
| 任何情况 | UUID 与真实身份（TG / QQ / 邮箱 / 链上地址）**无任何关联** | 无法归人 |

**要回答「都是谁」，必须建立身份关联。三条路径（按侵入性排序）**：

| 方案 | 做法 | 能知道什么 | 代价 |
|---|---|---|---|
| **A. 留资关联**（推荐，v1 采用） | 订阅表单提交时把 `visitor` UUID 一起带上 → 与 `contact` 绑定写入 `leads.jsonl` | 已留资者的完整访问轨迹 | 只能覆盖留资用户 |
| **B. 登录态关联** | 用户登录后 `visitor` ← 用户 ID | 登录用户的完整轨迹 | 需账户体系（可复用 `user` 角色） |
| **C. 会员解锁关联**（v1 采用） | `verify-member` 成功时回写 `visitor` ↔ `identifier` | 付费会员的访问轨迹 | 只能覆盖付费会员 |

**本设计采用 A + C**（v1 不强制登录）：在「表单提交」与「解锁成功」这两个**用户主动表明身份**
的时刻回写 `visitor ↔ 身份` 映射，此后该 UUID 的访问即可归属到人。

→ **明确边界**：未留资 / 未付费的匿名访客，**永远只能知道「来过几次」，不知道「是谁」**。
这是隐私上的正确设计（不是缺陷），必须在文档与页面上如实标注。

**付费页专项看板（本设计新增）**：

1. `/stats?page=<路径>` —— 单页 PV/UV + 逐日曲线
2. `/stats?funnel=1` —— 转化漏斗：**付费页 PV → 表单提交数 → 解锁成功数**
   （回答「漏斗哪一段在漏人」）

#### 5.11.4 验收标准

- [ ] `verify-member` 实现收回本仓库（消灭游离代码），有单元测试
- [ ] 所有 `GET` 类接口鉴权走 header，不再用 URL query
- [ ] `/track`、`/lead` 有速率限制；超限返回 429 且有测试
- [ ] `/stats?page=` 可单独统计任意页面（含付费页）的 PV/UV 与逐日曲线
- [ ] `/stats?funnel=1` 返回转化漏斗（PV → lead → 解锁）
- [ ] 留资 / 解锁时回写 `visitor ↔ 身份` 映射；映射表可审计、可删除
- [ ] 匿名访客统计口径在文档与页面上均标注「匿名 UUID，非实名」
- [ ] `events.jsonl` / `leads.jsonl` 追加写入幂等，单行坏不影响后续
- [ ] 各接口 401（secret 错）/ 400（字段缺）/ 429（限流）分支均有测试
- [ ] `top_pages` 与 `days` 口径有单元测试（跨日边界、site 过滤）

## 6. 数据设计

### 6.1 核心实体

| 实体 | 关键字段 |
|---|---|
| Match | slug, league, game, teams, date, started_at/ended_at（UTC+北京）, format(BO), status, result_inferred, settlement, ot_rule |
| Node | match_slug, game, phase(bp/mid/end/ot/pre/full), window_start/end, slice_path, intel_json, report_path, 四段时间戳 |
| Slice | match_slug, node, window, league_files, rows, gap 标注 |
| Intel | meta(总数/活跃/密度), teams/players 提及（正负）, gray_signals, odds_discussion, density_timeline, samples, team_traits |
| Entity | team/player 画像：提及量, 正负锚, 灰信号记录, BP 战绩, 时间窗 |
| IntelAsset | 情报资产：主题, 维度, 洞察, 证据, 置信, 验证状态, 时间 |
| Knowledge | 四维知识：选手/队伍/英雄（地图）/联赛 |
| GraySignal | 文本（脱敏）, 主题, 指向, 时间, 来源房间, 验证状态 |
| BpSignal | 主题（选手×英雄/队伍×地图）, 时间, 样本, 验证状态 |
| User/Member | TG 或 QQ, expires, 档位, 来源 |
| Metric | 节点四段时间戳, LLM usage/成本, 采集健康度 |
| Source | id, platform, source_name, room_url, room_id, league_id, is_active, priority, created_at, updated_at |
| User | id, role(viewer/user/admin), subscription_ref, created_at |
| PaymentAddress | user_id, chain, address, address_index, reference |
| Payment | user_id, chain, tx_hash, log_index, amount, status |
| Subscription | user_id, plan, status, started_at, expires_at, payment_id |

### 6.2 存储约定

- 原始弹幕 JSONL 只增不改，按 平台/日期 落盘
- 切片 JSONL 按 match_slug_g{game}_{phase} 命名
- 规则层 intel.json 与切片一一对应
- 状态文件（runtime/events/、runtime/vps_intel/）幂等：存在即跳过
- matches.json 为比赛元数据权威，result_inferred 结算后回填（含比分 3:2 等）
- SQLite 存结构化库（matches/teams/players/gray/bp/leagues/knowledge/assets/payments/subscriptions/sources/users）
- JSONL/JSON 存切片与状态，MD 存知识库与镜像
- 后台管理员配置通过 Web UI 写入 DB（sources、users 表），不读本地 config

### 6.3 目录结构（目标工程）

```
danmu-intel/
├── pyproject.toml / requirements.txt / uv.lock
├── README.md / AGENTS.md / CONTRIBUTING.md
├── config/
│   ├── settings.yaml            # 全局配置（联赛/房间/时间窗/SLA/成本阈值/OT规则）
│   ├── leagues.json             # 联赛默认采集集
│   ├── streamers.json           # 直播间注册表
│   └── team_names.json          # 队伍命名（唯一权威）
├── src/danmu_intel/
│   ├── capture/                 # 采集层（huya/soop/session/registry/heartbeat）
│   ├── schedule/                # 赛程与事件（matches_today/event_bus/ot_rules）
│   ├── slice/                   # 切片层（slicer/window/league_filter）
│   ├── rules/                   # 规则层（lexicon/stats/gray/density/traits）
│   ├── refine/                  # 提炼层（llm_client/prompts/renderer）
│   ├── verify/                  # 校验层（official/polymarket/gate/match_state）
│   ├── storage/                 # 存储层（models/sqlite/library）
│   ├── publish/                 # 发布层（site/paywall/timeline_shell/audit/deploy）
│   ├── scheduler/               # 调度层（event_loop/pipeline/timers）
│   ├── monitor/                 # 监控层（self_check/metrics/alerts）
│   └── api/                     # API（verify_member/lead/stats）
├── prompts/                     # 固定提示词（report_full/game/pre/live/asset）
├── data/                        # 运行时数据（切片/规则 JSON/状态）
├── reports/                     # 输出情报页（HTML+MD）
├── runtime/                     # 状态/日志/成本记录
├── tests/                       # 回归测试（含今日教训用例）
└── deploy/                      # systemd units / 部署脚本 / nginx 配置
```

---

## 7. 技术选型

| 层 | 选型 |
|---|---|
| 语言 | Python 3.11+ |
| 弹幕采集 | aiohttp + vendor/real-url 虎牙库（vendor 化，防平台改版） |
| 官方数据 | urllib/requests 直连 Riot esports-api、Liquipedia MediaWiki API（gzip+UA）、HLTV（封装/人工抽查） |
| LLM | DeepSeek API（OpenAI 兼容接口，provider 可替换） |
| 存储 | SQLite（结构化库）+ JSONL/JSON（切片与状态）+ MD（知识库） |
| 调度 | systemd service + timer（事件钩子 + 定时兜底） |
| 站点 | 静态页生成 + GitHub Pages / nginx |
| 通知 | Telegram Bot API |
| 测试 | pytest + 回归测试集 |
| 部署 | uv/venv + systemd + git 同步 |

---

## 8. 流程设计（端到端）

### 8.1 比赛生命周期

```
1. 每日清单同步（matches_today.json）→ 比赛进入今日页「未开始」
2. 开播检测（官方 schedule / 弹幕密度突变）→ 启动采集 session（多直播间同场）
3. BP 锁定事件 → 切片 G{1}_bp → 规则统计 → 快报（≤2min）→ 完整版（≤10min）同 URL 升级
4. 开局 / 一血 / 团战密度峰值 → 局中切片 → 局中快报 → 局中完整版
5. 小局结束（官方 gameWins / 结算价 ≥0.99 / closed / 弹幕多信号）→ G{n}_end 完整版
6. CS2 加时检测（比分超 12:12 且未结束）→ G{n}_ot 节点，按 OT 规则判定终局
7. 整场结束（Polymarket 结算仲裁）→ full 整场复盘（≤15min，全窗口抽样）
8. 比赛结束 → 该场全部节点页转免费公开
9. 情报库沉淀：本场情报资产提炼 → 幂等合并 → 趋势延伸 → 画像页更新
10. 验证回填：弹幕共识 vs 结果、灰信号兑现、BP 锚点应验
11. 历史库收录 + 今日页移除（零点刷新）
```

### 8.2 快报 → 完整版升级

```
事件触发 → 规则直出快报（硬统计，无语义结论）→ 上线（同 URL）
  → 后台生成完整版（模板 + 溯源）→ 门禁通过 → 同 URL 覆盖
页面始终只有一个 URL；快报期页面标注「速览版 · 完整版跟进」。
```

### 8.3 CS2 加时专项流程（今日实战固化）

```
1. 常规 12:12 后：监听比分源（blast.tv / Liquipedia / HLTV 轮询）+ 弹幕"加时"话题；
2. 按 config 的 ot_rule 判定终局（如 BLAST Open = 4 回合加时块先到 4 分者胜）；
3. 弹幕"图三了/1615/结束"一律只作候选信号，等待官方比分确认；
4. 官方页滞后（>5 分钟）时：保留最新可核对快照 + 时间戳，标注"官方待回填"；
5. 终局四信号齐备后才允许发布图末/整场结论。
```

### 8.4 失败重试与幂等

- LLM 调用失败重试 ≤3 次（指数退避）
- 生成失败保留快报页，状态文件标记 error，timer 兜底重试
- 发布失败保留上一版站点；审计不通过不发布
- 同一事件重复触发不重复生成（幂等键）

---

## 9. 部署设计

### 9.1 服务器组件

| 组件 | 说明 |
|---|---|
| danmu-session.service | 采集常驻（按直播间注册表自动启停） |
| danmu-intel-pipeline.timer | 管线每 10 分钟兜底 + 事件钩子 |
| danmu-publish.timer | 发布每 5 分钟（含审计） |
| danmu-api.service | 对外 API（verify-member / lead / stats，8080） |
| nginx | 站点反代 / 静态托管（或 GitHub Pages） |

### 9.2 环境与密钥

- `DEEPSEEK_API_KEY`（生成端）
- GitHub Deploy Key（站点推送）
- Telegram Bot Token / Chat ID（订阅提醒与告警）
- 所有密钥仅存服务器，权限 600；不入库不提交
- 兼容旧 `~/.codex/config.toml` 中的 DeepSeek 配置（迁移期）

### 9.3 部署剧本（逐步）

```bash
# 1. 拉取工程
git clone <repo> /opt/danmu-intel && cd /opt/danmu-intel
uv sync  # 或 python3 -m venv .venv && pip install -r requirements.txt

# 2. 配置
cp config/settings.example.yaml config/settings.yaml   # 填联赛/房间/SLA/OT规则
cp config/leagues.example.json config/leagues.json     # 联赛默认采集集
cp config/streamers.example.json config/streamers.json # 直播间注册表

# 3. 初始化
python -m danmu_intel.storage init

# 4. 回归测试
pytest -q

# 5. 启动采集与定时任务
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now danmu-session danmu-intel-pipeline.timer danmu-publish.timer danmu-api

# 6. 试跑一页 full 验证（对照样页结构与密度）
python -m danmu_intel.cli test-full --match <slug>

# 7. 发布上线，验证线上页面与付费墙
python -m danmu_intel.publish deploy --check
```

### 9.4 健康检查

- `systemctl status danmu-*` 全绿
- 采集心跳最新时间 <120s
- 今日清单每个"进行中"比赛都有 session
- 每节点四段时间戳可查，SLA 达标率日报

---

---

### 9.5 站点推送与 Deploy Key（★ 用户问「GitHub Deploy Key 是干什么的」）

**蓝本真实实现**——`tools/commit_site_pages.sh`（399 B，全文抄录）：

```bash
#!/bin/bash
# 服务器端：把 site_repo 中指定页面提交并推送（deploy key）。
# 用法：bash tools/commit_site_pages.sh "提交说明" file1 file2 ...
set -euo pipefail
cd /opt/danmu-intel/site_repo
export GIT_SSH_COMMAND="ssh -i /root/.ssh/github_deploy -o StrictHostKeyChecking=accept-new"
git add "${@:2}"
git commit -m "$1" -q
git push -q origin main
echo "committed and pushed"
```

#### 9.5.1 它是什么、干什么

| 项 | 说明 |
|---|---|
| **是什么** | 一对 **SSH 密钥**。公钥以 **Deploy Key** 形式挂在 GitHub 仓库设置里；私钥放在服务器 `/root/.ssh/github_deploy` |
| **干什么** | 服务器生成 HTML 页面 → 提交到 `site_repo`（**独立于代码仓库的站点仓库**）→ `git push origin main` |
| **为什么用 Deploy Key 而非账号** | Deploy Key 是**仓库级**凭据，不携带个人账号权限；泄露影响面最小，且可单仓库吊销 |
| **与部署的关系** | `site_repo` 收到 push → 触发 Vercel 自动构建部署（这就是「站点推送」链路） |
| **为什么两个仓库** | **代码仓库**（`danmu-intel`）+ **生成物仓库**（`site_repo`）分离。生成物不进代码仓库，避免每日报告 diff 污染代码历史 |

**一句话**：Deploy Key = 「让服务器有权把生成好的站点页面推到站点仓库」的**仓库写入凭据**，
它是**自动发布链路**的钥匙，不是访问密钥、也不是 API 密钥。

#### 9.5.2 本设计的安全要求

| 要求 | 具体做法 |
|---|---|
| **绝不入库** | 私钥文件内容、其 base64、任何片段都不得出现在仓库、issue、日志、文档中，值一律写 `[REDACTED]`；`config/` 只存**路径**不存内容 |
| **权限收紧** | `chmod 600 /root/.ssh/github_deploy`，属主 `root` |
| **最小权限** | Deploy Key **只挂站点仓库**，权限只给 write（**不给 admin**），勾选「Allow write access」即可 |
| **可轮换** | 泄露时在 GitHub 仓库 Settings → Deploy keys 单方面删除并重发，无需改服务器代码 |
| **CI 门禁** | 仓库扫描 `grep -rn "BEGIN OPENSSH PRIVATE KEY" .` 必须 0 命中 |
| **忽略规则** | `.gitignore` 覆盖 `site_repo/`、`*.pem`、`id_*`、`.env` |

#### 9.5.3 验收标准

- [ ] 部署文档含 Deploy Key 的**生成 → 挂载 → 校验**三步（含 `ssh-keyscan` 步骤）
- [ ] `ssh -i /root/.ssh/github_deploy -T git@github.com` 在部署自检中执行且期望输出含 `successfully authenticated`
- [ ] 仓库内 `grep -rn "BEGIN OPENSSH PRIVATE KEY"` = 0 命中（CI 门禁）
- [ ] `.gitignore` 覆盖 `site_repo/`、`*.pem`、`id_*`、`.env`
- [ ] 站点推送失败**有告警**（不静默），且不影响情报主流程
- [ ] `config/` 中只出现私钥**路径**，不出现内容

## 10. 非功能需求

### 10.1 性能与 SLA

| 指标 | 目标 |
|---|---|
| BP 后快报 | ≤2 分钟 |
| 局中快报 | ≤2 分钟 |
| 完整版 | ≤10 分钟 |
| 赛后复盘 | ≤15 分钟 |
| 发布部署 | ≤1 分钟 |
| 采集断线重连 | ≤60 秒 |
| 并发 | 同时处理 ≥4 场比赛 |

### 10.2 成本预算

- LLM：单页 ≈0.04-0.06 元；一场 BO3（5-8 页）≈0.25-0.45 元
- VPS：2C4G80G ≈ $18-24/月
- 每晚 5-6 场 ≈ 1.5-2.5 元 LLM 成本
- 每页记录 usage 与成本，超阈值 TG 告警

### 10.3 可用性与可靠性

- 采集/管线/发布由 systemd 托管，崩溃自动重启
- 幂等：状态文件存在即跳过（节点不重复生成、沉淀不重复入库）
- 数据端完备：缺源显式标注，禁止静默
- 全链路日志：journald + runtime 日志，异常有堆栈与告警

### 10.4 安全与合规

- 灰信号只作风险标注，对外永远写「观众质疑 · 非结论」
- 弹幕引用脱敏，不展示用户身份
- 服务条款一行「数据仅供参考」
- API Key 只存服务器，权限 600；不持有私钥
- 不做违法事项

---

## 11. 测试与回归

### 11.0 覆盖策略（强制要求）

| 维度 | 要求 |
|---|---|
| **整体覆盖率** | **≥ 90%**（语句覆盖率 + 分支覆盖率双达标） |
| **单元测试（单元层）** | 所有模块业务逻辑 100% 覆盖；含正常路径 + 边界 + 异常路径 |
| **E2E 浏览器测试** | 关键用户旅程端到端覆盖（Playwright / pytest-playwright） |
| **CI 门禁** | 覆盖率 < 90% → 合并禁止 |

### 11.1 单元测试清单

| 测试 | 锁定行为 |
|---|---|
| test_scan_regression | 扫描/抓取空结果自检；电竞赛事按 tag 全量拉取 |
| test_match_state_guard | 四道闸：刚开赛误判、仅弹幕定胜负、反讽、比分源滞后、跨时区 |
| test_speedcard_consistency | 速览卡门禁（已废弃模板的残留审计） |
| test_slice_league_isolate | 跨联赛混源 = 0（CS2 只进 CS 房） |
| test_node_window | 节点切片边界与官方 schedule 一致 |
| test_intel_schema | 规则层 intel.json 字段固定 |
| test_render_sections | A 型 0-10 / B 型 0-11 每段恰好一次 |
| test_ot_rules | CS2 加时制参数化：4 回合块/无限/领先 2 分 三种规则终局判定 |
| test_danmu_premature_end | **今日教训**：弹幕"回家/GG/图三了/1615"提前喊话不判终局 |
| test_official_lag | **今日教训**：官方页滞后快照不阻塞，标注待回填 |
| test_paywall_free | 比赛结束后该场节点页自动转免费 |
| test_library_idempotent | 沉淀引擎同场不重复入库 |
| test_verify_member | 会员验证（陌生拒绝/订阅解锁/过期失效） |
| test_adapter_registry | 适配器注册表完整性 |
| test_source_crud | 数据源 CRUD 热生效 |
| test_auth_roles | 三角色鉴权隔离 |
| test_payment_polygon | Polygon 入账检测（mock） |
| test_payment_solana | Solana reference 反查（mock） |

### 11.2 E2E 浏览器测试清单

| 测试 | 覆盖场景 |
|---|---|
| test_e2e_register_and_subscribe | 注册 → 选档位 → 展示地址 → 模拟入账 → 自动开通 |
| test_e2e_free_content_access | 免费用户访问赛后复盘 |
| test_e2e_pro_content_blocked | 免费用户访问 Pro 内容被拦截 |
| test_e2e_admin_data_source | 管理员 CRUD 数据源 |
| test_e2e_admin_role_protection | 非 admin 访问 /admin/* 被拒绝 |
| test_e2e_expired_member_downgrade | 到期自动降级 |
| test_e2e_timeline_upgrade | 快报 → 完整版同 URL 升级 |

### 11.3 发布门禁

```
pytest 全绿 → 覆盖率 ≥90% → 生成页结构门禁 → 事实层官方校准 → 终局四信号 → 全站审计
→ 发布 → 线上抽查（curl -L 跟随重定向 + 内容关键词，禁止单点地址下结论）
```

---

## 12. 里程碑与交付计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 | 工程骨架：目录、配置、依赖、CI 测试 | pytest 全绿 |
| M1 | 采集 + 切片 + 规则层 | 真实比赛多直播间采集、切片边界正确 |
| M2 | LLM 提炼 + 校验 + 输出 | 一页 full 对照样页逐段一致；成本达标 |
| M3 | 发布 + 付费墙 + 订阅 | 全链路自动化；Pro/免费分层正确 |
| M4 | 情报资产沉淀 + 四维知识库 + 验证闭环 | 每场提炼资产并幂等合并；跨场统计可见 |
| M5 | 监控 + SLA 指标 + 告警 | SLA 达标率日报；异常自动告警 |

---

## 13. 验收标准（总）

1. 项目为标准 Python 工程（目录/依赖/配置/测试/文档齐全），可一键部署到 VPS
2. 全链路自动化：比赛开始到复盘入库无需人工干预
3. 输出结构唯一（旧 10 段框架），样页逐段对照一致
4. 事实层只信官方，弹幕结论可溯源，缺数据显式标「无」
5. 四类失效模式有门禁与回归测试锁定
6. SLA：快报 ≤2min / 完整版 ≤10min / 复盘 ≤15min
7. 成本：单页 ≈0.05 元，每晚 5-6 场 ≤2.5 元
8. 订阅链路：登记 → 名单 → 解锁 → 到期全自动
9. 站点 Pro/免费分层正确，结束即免费开放
10. 情报资产：每场自动提炼并沉淀，四维可查、可溯源、可验证
11. 监控：SLA/成本/健康度日报 + 异常 TG 告警
12. **终局防误**：任何"比赛结束"发布必经四信号 + 四道闸（含 CS2 加时制参数化）
13. **测试覆盖 ≥ 90%**：单元测试 + E2E 浏览器测试双层覆盖；CI 门禁低于 90% 禁止合并
14. 支付模块：支持 Polygon + Solana 两条链
15. **站点统计**：PV/UV 可按 `site` 与 `page` 维度查询；付费页漏斗（PV → lead → 解锁）可查；
    匿名访客口径如实标注「匿名 UUID，非实名」（见 §5.11.3）
16. **密钥卫生**：仓库内零私钥（Deploy Key / 钱包密钥 / API Key），CI 扫描门禁；所有敏感值走环境变量（见 §9.5.2）

---

## 14. 风险与对策

| 风险 | 对策 |
|---|---|
| 弹幕源失效（房间改名/平台改版） | 注册表热更新 + 断线重连 + 完整性标注 + 告警 |
| 官方 API 失效 / 接口变更 | 多源交叉（Riot/Liquipedia/HLTV）+ 定期校验 + 备用提取 |
| LLM 输出幻觉 | 固定提示词 + 门禁 + 无中生有拦截 + 重试 ≤3 次 |
| 成本失控 | usage 记录 + 阈值告警 + 样本量限制 + 重试限制 |
| 站点审计卡发布 | 审计前置 + 白名单排除 + 失败回退 |
| 多场比赛并发 | 并行生成（≤4 并发）+ 状态幂等 + 队列 |
| 弹幕提前信号误判终局 | 终局四信号 + match_state_guard 四道闸 + OT 规则参数化（今日教训固化） |

---

## 15. 付费闭环（链上收款）★ 新增模块

### 15.1 方案选型

**采用方案 A：唯一充值地址 + 一次性付款 + 有效期**

| 链 | 地址派生 | 入账检测 | 备注 |
|---|---|---|---|
| Polygon | BIP-44 `m/44'/60'/0'/0/{index}` | `eth_getLogs` 扫 ERC-20 `Transfer` | xpub 派生，无私钥 |
| Solana | 单地址 | `getSignaturesForAddress(reference)` | 唯一 reference（Solana Pay 官方字段） |

### 15.2 无私钥方案

- **助记词由用户保管**（用户拍板），系统只需 watch-only 公钥/xpub
- Polygon：xpub（扩展公钥）派生地址，资产直达冷钱包
- Solana：单收款地址 + 唯一 reference 反查

### 15.3 流程

```
注册 → 选档位/链 → 异步幂等派生专属地址 → 展示地址+二维码+金额+有效期
→ 转账 → 轮询检测 → 自动写 subscriptions 表 → 解锁
```

### 15.4 配置（config/payment.yaml）

```yaml
payment:
  chains:
    polygon:
      enabled: true
      rpc_url: "${DANMU_INTEL_POLYGON_RPC:-https://polygon-rpc.com}"
      usdc_contract: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
      confirmation_blocks: 3
      poll_interval_seconds: 30
    solana:
      enabled: true
      rpc_url: "${DANMU_INTEL_SOLANA_RPC:-https://api.mainnet-beta.solana.com}"
      usdc_mint: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
      confirmation: "finalized"
      poll_interval_seconds: 30
```

### 15.5 验收标准

- [ ] 注册后异步派生专属地址（Polygon xpub / Solana reference）
- [ ] 展示地址 + 二维码 + 金额 + 有效期
- [ ] 轮询检测入账（Polygon 3 确认 / Solana finalized）
- [ ] 幂等：唯一键 (chain, tx_hash, log_index)
- [ ] 精确匹配金额，不足额→「待补款」
- [ ] 到期自动降级 + 宽限 3 天
- [ ] 每笔入账落库可回溯

---

#### 15.6 付费链路代码收回（消灭游离实现）

**问题**：`POST /api/verify-member` 的实现不在蓝本仓库内（`grep -rln "verify-member"` 全仓库
只命中文档与调用方 `tools/add_paywall.py`），实际代码在 Vercel 项目
`danmu-intel-api.vercel.app` 里，而蓝本**没有 `api/` 目录、没有 `vercel.json`**。
后果：付费链路**一半代码不在版本控制内**，无法 code review、无法回归测试、
无法与 `members.json` 的写入逻辑（VPS 侧）做跨端一致性校验。

**处置**：把 Vercel 端的 3 个 serverless function 一并纳入本仓库：

```
danmu-intel/
├── api/                    ← 新增：Vercel Serverless Functions
│   ├── track.js            # POST → 转发 VPS /track
│   ├── lead.js             # POST → 转发 VPS /lead + 推 TG
│   └── verify-member.js    # POST → 读 members.json 判定
├── vercel.json             ← 新增：路由与构建配置
```

**接口契约不变**（对外行为与蓝本一致，见 §5.11.2），只是把实现搬进仓库。
`members.json` 的**唯一写入方**必须是 VPS 的支付模块（§15.3），Vercel 端**只读**。

**验收**：
- [ ] `api/` 三个 function 在仓库内可见，有单元测试（mock `members.json`）
- [ ] `vercel.json` 在仓库内，部署配置可复现
- [ ] `members.json` 只有一处写入方（VPS 支付模块），有静态检查或测试锁定
- [ ] `verify-member` 的 `expires` 过期判定有边界测试（当天到期 / 已过期 / 无 `expires`）

## 16. 已拍板决策

| # | 决策 | 来源 |
|---|---|---|
| 1 | 早鸟问题忽略（不设早鸟窗口） | 用户 2026-09-21 |
| 2 | 钱包助记词用户保管，系统只需 watch-only 公钥 | 用户 2026-09-21 |
| 3 | RPC 用公共免费节点（Polygon / Solana） | 用户 2026-09-21 |
| 4 | 马斯克打包方案先不做 | 用户 2026-09-21 |
| 5 | 测试覆盖 ≥ 90%（单元 + E2E） | 用户 2026-09-21 |
| 6 | 设计基准 = PRD v2.0 | 用户 2026-09-21 |
| 7 | 收款链限 Polygon + Solana，排除 Base | 用户 2026-09-21 |
| 8 | Solana 用 reference 字段（ed25519 硬约束） | 用户 2026-09-21 |

---

## 附录 A：与蓝本（danmu-intel-local）的差异清单

| 项 | 蓝本 | 本项目 |
|---|---|---|
| LLM 提炼 | `USE_LLM = False`（手动调用） | 程序固化直连 DeepSeek |
| 付费闭环 | 零链上支付代码 | 唯一充值地址 + watch-only |
| 工程化 | 脚本散落 | 标准 Python 工程 |
| 生成路径 | 三条路径并存 | 收敛为唯一权威 |
| 数据源配置 | config/streamers.json | Web 后台 CRUD + DB |
| 用户角色 | 无 | viewer/user/admin 三角色 |
| 适配器 | 分平台模块 | 统一 DanmuAdapter ABC |

---

## 附录 B：参考资料

- PRD v2.0：`danmu-intel-local/docs/task/DANMU_INTEL_CLOUD_PYTHON_PROJECT_PRD.md`
- 产品框架：`danmu-intel-local/docs/task/INTEL_PRODUCT_FRAMEWORK_2026-08-31.md`
- 输出模板：`danmu-intel-local/knowledge/INTEL_TEMPLATE_OLD_2026-08-31.md`
- 采集规则：`danmu-intel-local/knowledge/DANMU_CAPTURE_RULES.md`
- 工作流：`danmu-intel-local/knowledge/DANMU_WORKFLOW.md`
- 验证方法论：`danmu-intel-local/knowledge/VERIFICATION_METHODOLOGY.md`
- Solana Pay 规范：https://docs.solanapay.com/spec
