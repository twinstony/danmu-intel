# 弹幕情报库（danmu-intel）完整设计文档

> **版本**：v1.0（基于 PRD v2.0 定稿 + 付费闭环补充）
> **日期**：2026-09-21
> **状态**：待用户审阅
> **设计基准**：`danmu-intel-local/docs/task/DANMU_INTEL_CLOUD_PYTHON_PROJECT_PRD.md`（v2.0 定稿）

---

## 0. 文档说明

本文档是 `danmu-intel` 项目的**实现级设计**，直接指导 executor 编写代码。它包含：

1. 产品概述与架构（PRD §0–§6 的精简实现版）
2. **12 个功能层的详细设计与验收标准**（PRD §7 全部 + 新增 §7.9.1 付费闭环）
3. 数据设计（PRD §8）
4. 技术选型（PRD §9）
5. 流程设计（PRD §10）
6. 部署设计（PRD §11）
7. 非功能需求（PRD §12）
8. 测试与回归（PRD §13）
9. 里程碑（PRD §14）
10. 验收标准总表（PRD §16）

**原则**：本文档即 ground truth。执行体读本文档 + PRD v2.0 即可开工，无需额外澄清。

---

## 1. 产品概述

### 1.1 一句话定位

把电竞直播弹幕变成**可溯源、可支持决策**的情报，并且越攒越值钱。

### 1.2 核心差异

| 维度 | 蓝本（danmu-intel-local） | 本项目（danmu-intel） |
|---|---|---|
| LLM 提炼 | 手动（`USE_LLM=False`，需人工改代码） | **自动**（程序直连 DeepSeek，无人干预） |
| 生成路径 | 三条并存（Codex/DeepSeek/规则直出） | **唯一权威**（程序固化 + LLM 接口化） |
| 付费闭环 | 表单推 TG + 手工维护 members.json | **链上收款自动开通**（Polygon + Solana） |
| 订阅验证 | TG/QQ 用户名客户端验证 | 链上支付记录 + 会员验证双轨 |

### 1.3 架构原则（PRD §6.2 原文）

```
P1 程序固化一切：流程、模板、规则、门禁、提示词全部以代码/配置文件落地
P2 确定性优先：规则层完全确定性输出，LLM 只做判断性提炼且必须过门禁
P3 可替换性：LLM 提供方接口化（DeepSeek 起步，OpenAI 兼容）
P4 幂等：所有生成/沉淀/发布步骤有幂等键
P5 可观测：每个节点记录四段时间戳 + LLM usage + 成本
```

---

## 2. 总体架构

### 2.1 分层架构

```
┌──────────────────── VPS（danmu-intel Python 项目）────────────────────┐
│                                                                        │
│  capture → schedule/event → slice → rules → refine → verify → render  │
│  采集        赛事/事件        切片     规则统计   LLM提炼   门禁     输出  │
│                                                                        │
│  ┌──────────────────────────────────┬───────────────────────────────┐ │
│  │ library（情报库沉淀）             │ publish（发布层）              │ │
│  │ 选手/队伍/英雄/联赛四维           │ 今日页/历史库/画像/灰信号/     │ │
│  │ + 画像 + 验证闭环                 │ 付费墙 + 时间轴壳 + 审计       │ │
│  └──────────────────────────────────┴───────────────────────────────┘ │
│                                                                        │
│  payment（链上收款 → 自动开通）  scheduler（事件驱动 + timer 兜底）     │
│  monitor（自检/SLA/成本/TG告警）  api（verify-member/lead/stats）       │
│  storage（SQLite + JSONL + MD）                                        │
└────────────────────────────────────────────────────────────────────────┘
          │
  官方数据源：Riot esports-api / Liquipedia / HLTV / Polymarket
  LLM：DeepSeek API（程序直连，固定提示词）
  链上：Polygon（USDC）+ Solana（USDC）→ 自动检测入账 → 开通会员
  站点：GitHub Pages 或 nginx 直出
```

### 2.2 与 PRD v2.0 的差异

| 项 | PRD v2.0 | 本文档 |
|---|---|---|
| 订阅层（§7.9） | 4 行：表单 → 推站长 TG | **新增 §7.9.1 完整付费闭环**（链上收款自动开通） |
| 支付模块 | 不存在 | **新增 `src/danmu_intel/payment/`** 模块 |
| 收款链 | 未指定 | **Polygon + Solana**（用户定稿） |
| 钱包 | 不持有私钥（§12.4） | **新建收款钱包 + xpub 只读派生**（零私钥风险） |

---

## 3. 功能需求详设

### 3.1 采集层（capture）

**职责**：多路直播间弹幕流采集，独立落盘，异常自动重启。

#### 3.1.1 实现设计

| 组件 | 实现 |
|---|---|
| 数据源 | 虎牙（官方流/957/毛毛/米勒等）、SOOP（LCK CL）；注册表驱动（`config/streamers.json`） |
| 采集器 | 每个直播间一个 `asyncio.Task`，独立 `last_message_at` 心跳 |
| 会话管理 | 同场比赛所有直播间放入同一 `session_id`（`config/leagues.json` 定义归属） |
| 落盘 | 原始弹幕 JSONL，路径 `data/capture/<platform>/<date>_<room_id>.jsonl`，**只增不改** |
| 健康检查 | 心跳超 120s 判假死 → TG 告警 + 自动重启该房间任务 |
| 去重 | 同一房间（如 maxixi 与 CSBOY 官方房）按 `room_id` 去重 |
| 完整性标注 | 每场情报标注 `actual_sources` / `expected_sources` / `gaps`；离线房间标 `"offline_not_captured"` |

#### 3.1.2 验收标准

- [ ] 比赛开播后 1 分钟内开始采集
- [ ] 断线自动重连 ≤60s
- [ ] 任一房间数据中断有日志 + TG 告警
- [ ] 采集元数据（房间/起止/条数/缺口）落盘
- [ ] 新增/停用房间改配置热生效（无需重启服务）

---

### 3.2 赛程与事件层（schedule / event）

**职责**：每日赛程清单 + 事件驱动触发器（节点流水线的触发源）。

#### 3.2.1 实现设计

| 组件 | 实现 |
|---|---|
| 每日清单 | `python -m danmu_intel.schedule sync` → `data/matches_today.json`（Riot esports-api + Liquipedia） |
| 事件总线 | `runtime/events/<idempotency_key>.json` 存在即跳过（幂等） |
| 事件类型 | 7 种（开播/BP锁定/局中里程碑/局末/加时/整场结束/每日零点），见 PRD §7.2 表 |
| 触发机制 | 事件钩子立即执行 + systemd timer 每 10 分钟兜底扫描 |
| CS2 加时 | `config/settings.yaml` 中 `ot_rules` 参数化（4回合块/无限/领先2分） |

#### 3.2.2 验收标准

- [ ] 每日清单包含当日所有比赛（slug/league/teams/format/时间）
- [ ] 事件触发后 30s 内启动对应节点流水线
- [ ] 重复触发不重复生成（幂等键验证）
- [ ] CS2 加时事件按 `ot_rule` 正确判定终局

---

### 3.3 切片层（slice）

**职责**：按节点时间窗切片，联赛源硬隔离。

#### 3.3.1 实现设计

| 组件 | 实现 |
|---|---|
| 节点类型 | PRE / BP / MID / END / OT / FULL |
| 切片起点 | 本节点阶段起点（官方 schedule + 弹幕密度突变校准确认） |
| 联赛隔离 | 切片前按 `league_files` 过滤；CS2 只进 CS 直播间；混源整场作废（`void_match_intel`） |
| 整场抽样 | 全窗口等距抽样（不只喂尾部） |
| 命名 | `data/slices/<match_slug>_g{game>_<phase>.jsonl` |

#### 3.3.2 验收标准

- [ ] 每个切片文件带窗口起止时间（UTC + 北京时间）
- [ ] 节点边界与官方 schedule 一致
- [ ] 跨联赛混源 = 0（回归测试锁定）

---

### 3.4 规则统计层（rules）

**职责**：词表统计 + 密度时间线 + 灰信号计数。**纯确定性输出**。

#### 3.4.1 实现设计

| 组件 | 实现 |
|---|---|
| 词表 | `config/team_names.json`（唯一权威）+ `config/lexicon.json`（黑话/灰信号词） |
| 统计输出 | `data/intel/<match_slug>_g{game>_<phase>.intel.json`（固定 schema） |
| 密度时间线 | 按分钟聚合弹幕条数，输出峰值（时间+条数+代表样本） |
| 词表维护 | 半自动（LLM 发现候选词 → 人工确认入表 → 回归测试） |
| 团队特质 | TRAIT_KW 8 类（PRD §7.4） |

#### 3.4.2 验收标准

- [ ] `intel.json` 字段固定、可回归
- [ ] 漏词有测试告警
- [ ] 灰信号只作聚合与风险标注

---

### 3.5 提炼层（refine · LLM 接口化）★ 核心差异点

**职责**：程序直连 DeepSeek，自动完成弹幕→情报的提炼。**这是用户最关心的自动化环节**。

#### 3.5.1 实现设计

| 组件 | 实现 |
|---|---|
| 调用方式 | `src/danmu_intel/refine/llm_client.py` 直连 DeepSeek API（OpenAI 兼容） |
| 提示词 | `prompts/` 5 份固定文件：`report_full.md` / `report_game.md` / `report_pre.md` / `report_live.md` / `intel_asset.md` |
| 输入组装 | 提示词模板 + 规则层统计摘要（intel.json）+ 代表弹幕样本（≤60 条、每条 ≤50 字、带北京时间戳） |
| 模型参数 | `deepseek-chat`、`temperature=0.3`、`max_tokens` 首调 8000 / 重试 16000 |
| 重试 | ≤3 次，指数退避 |
| 成本记录 | 每次调用记录 `input_tokens` / `output_tokens` / `cost_cny` |
| **关键约束** | **LLM 只当"打字员"**：结构/标准/校验全在程序，换模型不影响质量 |

#### 3.5.2 验收标准

- [ ] 单页输入 ≈3.3k / 输出 ≈7.9k tokens
- [ ] 成本 ≈0.05 元/页
- [ ] 调用失败自动重试 ≤3 次
- [ ] 每次调用记录 usage 与成本到 `runtime/llm_usage.jsonl`
- [ ] **无需人工干预**（与蓝本 `USE_LLM=False` 的根本差异）

---

### 3.6 校验门禁层（verify）

**职责**：四层门禁，拦截幻觉与误判。

#### 3.6.1 实现设计

| 门禁 | 实现 |
|---|---|
| 结构门禁 | 每段 `<h2><span class="no">N</span>标题</h2>` 恰好出现 1 次 |
| 事实层门禁 | `match_state_guard` 四道闸（时间门槛/结构源优先/反讽识别/比分源滞后） |
| 来源门禁 | 无中生有零容忍；跨场话题标注；时间线只用真实时间戳 |
| 终局四信号 | 官方比分 + 官方系列状态 + 弹幕多信号共振 + 流量骤降 → 齐备才允许发布"已结束" |

#### 3.6.2 验收标准

- [ ] A 型 0-10 / B 型 0-11 每段恰好一次
- [ ] 刚开赛误判终局 → 拦截
- [ ] 仅弹幕定胜负 → 拦截
- [ ] 反讽语气 → 降级为"未确认"
- [ ] 无数据支撑的结论 → 拦截

---

### 3.7 输出层（render）

**职责**：HTML + MD 双格式情报页，唯一模板标准。

#### 3.7.1 实现设计

| 组件 | 实现 |
|---|---|
| 模板权威 | `knowledge/INTEL_TEMPLATE_OLD_2026-08-31.md`（唯一标准） |
| A 型整场 | 0-10 十段结构（PRD §7.7 C2） |
| B 型局中 | 0-11（A 基础 + 状态核验/密度时间线/方向性情报） |
| 赛前 | 0-8 |
| 快报→完整版 | 同 URL 原地升级（快报标注"速览版 · 完整版跟进"） |
| 双格式 | HTML（站点展示，SAP/Apple 风格）+ MD（入库核心） |
| 视觉规范 | `#f5f5f7` 浅底、白圆角卡片、`#0071e3` 强调色、系统字体栈、移动端优先 |

#### 3.7.2 验收标准

- [ ] 每页结构符合模板（0-10 / 0-11 / 0-8）
- [ ] 快报 → 完整版同 URL 升级（不产生第二个页面）
- [ ] HTML + MD 双格式同步输出
- [ ] 时间一律北京时间展示
- [ ] 每页带"本页数据截至 <时间>"

---

### 3.8 发布层（publish）

**职责**：站点生成 + 付费墙注入 + 发布审计。

#### 3.8.1 实现设计

| 组件 | 实现 |
|---|---|
| 页面类型 | 今日情报、历史情报库、画像库、灰信号统计、验证闭环、订阅页 |
| 节点进度 | 今日页每场展示节点进度（未开始/进行中/已结束 + 已出节点） |
| 付费墙 | Pro = 进行中的节点页 + 完整画像；比赛结束 → 自动转免费 |
| 付费墙判定 | 按"比赛是否已结束"（settlements / 全小局 closed / 结果回填），禁止按文件名一刀切 |
| 全站审计 | 导航唯一、无旧模板残留、Pro 页付费墙齐全、节点完整性、页面×联赛×slug 关联一致 |
| 发布链路 | VPS → 站点仓库 → GitHub Pages（或 nginx 直出），目标 ≤1 分钟 |
| 回滚 | 异常时保留上一版站点 |

#### 3.8.2 验收标准

- [ ] 全站审计不通过 = 不发布
- [ ] 比赛结束后该场节点页自动转免费
- [ ] 发布 ≤1 分钟
- [ ] 异常时自动回退到上一版

---

### 3.9 订阅层（subscribe）

**职责**：会员登记 + 验证 + 到期管理。

#### 3.9.1 实现设计

| 组件 | 实现 |
|---|---|
| 登记表单 | 订阅页（昵称/TG 或 QQ/档位/备注）→ `runtime/leads.jsonl` 留痕 |
| 会员名单 | `data/members.json`（权威名单） |
| 验证 | `POST /api/verify-member`（identifier → 匹配 members.json → 返回 member/expires） |
| 到期提醒 | systemd timer 每日扫描，到期前 3天/1天 TG 提醒 |
| 到期失效 | 自动降级免费账号，宽限 3 天 |

#### 3.9.2 验收标准

- [ ] 登记表单提交后落盘可审计
- [ ] 验证接口：已订阅解锁 / 未订阅拒绝 / 过期失效
- [ ] 到期前 TG 自动提醒
- [ ] 到期自动降级

---

### 3.9.1 付费闭环（链上收款）★ 新增模块

**职责**：Polygon + Solana 链上收款 → 自动检测入账 → 自动开通会员。**这是蓝本完全没有的模块**。

#### 3.9.1.1 设计背景

| 项 | 现状（蓝本） | 本项目 |
|---|---|---|
| 收款 | 无 | Polygon USDC + Solana USDC |
| 地址派生 | N/A | EVM: xpub BIP-44 派生（每用户唯一地址） |
| 入账检测 | N/A | EVM: `eth_getLogs` 扫 Transfer 事件；Solana: `getSignaturesForAddress(reference)` |
| 私钥 | 不持有（PRD §12.4） | **零**（xpub 只读派生 + watch-only 监控） |
| 开通方式 | 手工维护 members.json | 入账确认 → 自动写入 members.json |

#### 3.9.1.2 用户流程

```
① 注册（用户名 + 邮箱）→ 写入 data/users.json
② 订阅页 → 选档位（月/季/年）→ 选链（Polygon / Solana）
③ 系统异步派生该用户专属地址（幂等 worker，不阻塞注册）
   - Polygon: xpub 派生 m/44'/60'/0'/0/{index}
   - Solana: 生成唯一 reference（32字节随机公钥）+ 单一收款地址
④ 展示：地址 + 二维码 + 金额（USDC）+ 有效期预览
⑤ 用户链上转账
⑥ 后台轮询检测入账（确认数达标）
⑦ 匹配成功 → 自动写入 members.json（expires = now + 档位时长）
⑧ 到期前 3天/1天 提醒（TG + 邮件）→ 手动续费（回 ④）
```

#### 3.9.1.3 关键设计（9 条）

| # | 设计点 | 说明 |
|---|---|---|
| 1 | **地址派生** | EVM BIP-44 `m/44'/60'/0'/0/{index}`，持久化 index；**异步 + 幂等，绝不在 web 请求里派生** |
| 2 | **Solana reference** | 32 字节随机公钥，`reference→user` 映射表；钱包必须将其作为"只读非签名账户"塞进转账指令 |
| 3 | **只读公钥** | xpub 派生 + watch-only 监控，**无私钥**。系统被完全攻破，一分钱也偷不走 |
| 4 | **轮询** | EVM 用 `eth_getLogs` 扫 ERC-20 `Transfer` 事件（不引入 indexer 依赖）；Solana 用 `getSignaturesForAddress(reference)` |
| 5 | **确认数** | Polygon 3 ／ Solana finalized |
| 6 | **幂等** | 唯一键 `(chain, tx_hash, log_index)` —— 重复入账不重复开通 |
| 7 | **金额匹配** | 稳定币按最小单位精确匹配；不足额 → 挂"待补款"状态 |
| 8 | **到期** | 自动降级免费账号，宽限 3 天 |
| 9 | **审计** | 每笔入账落库（user / tx_hash / chain / amount / 开通时长），可回溯 |

#### 3.9.1.4 技术约束（硬事实）

| 约束 | 说明 |
|---|---|
| EVM 无法附言 | ERC-20 `transfer(address,uint256)` calldata 占满，物理上塞不下附言。**唯一地址是行业标准做法** |
| Solana 无法 xpub 派生 | ed25519 曲线（SLIP-0010）只支持 hardened 派生，**不可能从公钥推子公钥**。reference 是官方标准替代 |
| 加密无原生 pull | 区块链交易必须由私钥持有者签名，无法像 Stripe 到期自动扣款。**务实做法 = 提醒 + 手动续费** |

#### 3.9.1.5 档位与价格

| 档位 | 价格 | 说明 |
|---|---|---|
| 试用 | $1 / 3 天 | 到期可转正式，试用费抵扣首月 |
| 早鸟 · 月付 | $39 / 月 | ⚠️ **早鸟截止 2026-09-15 已过（今天 2026-09-21），需用户决定新窗口** |
| 早鸟 · 季付 | $105 / 季 | ≈$35/月 |
| 早鸟 · 年付 | $390 / 年 | ≈$32.5/月，年付锁价 |
| 正式 · 月付 | $59 → $71 → $85 → $99 封顶 | 每满 20 名 +20% |
| 正式 · 年付 | $588 → $707 → $847 → $986 封顶 | 同梯度 |

#### 3.9.1.6 梯度规则

- 正式订阅每满 20 名整体 +20%，封顶价见上表
- 老用户锁定下单时价格，涨价只对新增订阅生效（终身锁价）
- 试用用户不计入付费梯度，转正式后计入

#### 3.9.1.7 数据表（新增）

```sql
-- 支付地址表
CREATE TABLE payment_addresses (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    chain TEXT NOT NULL,           -- 'polygon' | 'solana'
    address TEXT NOT NULL,         -- EVM 地址 / Solana 地址
    address_index INTEGER,         -- EVM 派生 index（Solana 为 NULL）
    reference TEXT,                -- Solana reference 公钥（EVM 为 NULL）
    created_at TEXT NOT NULL,
    UNIQUE(user_id, chain)
);

-- 入账记录表
CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    chain TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER,              -- EVM log index（Solana 为 NULL）
    amount INTEGER NOT NULL,       -- 最小单位（USDC = 6 位小数）
    status TEXT NOT NULL,          -- 'pending' | 'confirmed' | 'underpaid'
    detected_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(chain, tx_hash, log_index)
);

-- 订阅记录表
CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    plan TEXT NOT NULL,            -- 'trial' | 'monthly' | 'quarterly' | 'yearly'
    status TEXT NOT NULL,          -- 'active' | 'expired' | 'cancelled'
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payment_id INTEGER REFERENCES payments(id),
    created_at TEXT NOT NULL
);
```

#### 3.9.1.8 配置（config/payment.yaml）

```yaml
payment:
  chains:
    polygon:
      enabled: true
      rpc_url: "https://polygon-rpc.com"  # 可被环境变量覆盖
      usdc_contract: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
      confirmation_blocks: 3
      poll_interval_seconds: 30
    solana:
      enabled: true
      rpc_url: "https://api.mainnet-beta.solana.com"
      usdc_mint: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
      confirmation: "finalized"
      poll_interval_seconds: 20

  wallet:
    # xpub（扩展公钥）—— 只读，无私钥
    evm_xpub: "${DANMU_INTEL_XPU}"  # 环境变量注入
    # Solana 单一收款地址（用户转账时附 reference）
    solana_address: "${DANMU_INTEL_SOL_ADDRESS}"

  pricing:
    trial: { amount: 1, duration_days: 3, currency: "USDC" }
    monthly: { amount: 59, duration_days: 30, currency: "USDC" }
    quarterly: { amount: 105, duration_days: 90, currency: "USDC" }
    yearly: { amount: 390, duration_days: 365, currency: "USDC" }
    # 早鸟（需用户决定新窗口）
    early_bird:
      monthly: 39
      quarterly: 105
      yearly: 390
      deadline: "2026-09-15"  # ⚠️ 已过期，待用户更新

 梯度:
    threshold: 20
    increment_pct: 20
    max_monthly: 99
    max_yearly: 986
```

#### 3.9.1.9 验收标准

- [ ] 用户注册后系统异步派生专属地址（Polygon 唯一地址 / Solana 唯一 reference）
- [ ] 链上入账后自动检测（确认数达标）
- [ ] 入账匹配成功 → 自动写入 members.json 并开通会员
- [ ] 幂等：同一 tx 不重复开通
- [ ] 金额不足 → 挂"待补款"状态
- [ ] 到期自动降级免费账号
- [ ] 服务器**不持有私钥**（xpub 只读）
- [ ] 每笔入账落库可审计
- [ ] 支持 Polygon + Solana 两条链

---

### 3.10 情报库沉淀层（library · 知识资产）

**职责**：每场情报资产提炼 + 四维知识库长期积累 + 验证回填。

#### 3.10.1 实现设计

| 组件 | 实现 |
|---|---|
| 资产提炼 | 每场比赛结束后，基于全部节点情报产出"本场情报资产" |
| 资产结构 | `{主题, 维度, 洞察一句话, 证据, 置信, 验证状态, 时间}` |
| 四维知识库 | 选手 / 队伍 / 英雄·地图 / 联赛 |
| 幂等合并 | `runtime/accumulated_matches.json` 防重复；新资产追加；旧认知更新时保留时间与来源 |
| 验证回填 | 灰信号兑现率、BP 锚点应验率、预测命中率持续累加 |

#### 3.10.2 验收标准

- [ ] 每场比赛自动提炼资产并沉淀
- [ ] 同场不重复入库
- [ ] 四维知识可查、可溯源、可验证
- [ ] 画像引用必须带时间，禁止用历史画像冒充本场数据

---

### 3.11 监控运维层（monitor）

**职责**：自检 + SLA 指标 + 成本 + 告警。

#### 3.11.1 实现设计

| 组件 | 实现 |
|---|---|
| 自检 | 每日检查今日清单完整性、节点缺口、结果回填状态、站点审计 |
| SLA 指标 | 每节点记录触发 → 快报 → 完整版 → 上线四段时间戳 |
| 成本 | 每页记录 LLM usage 与成本，日报输出 |
| 告警 | 采集断线 / 节点超时 / 发布失败 / 成本异常 / 比赛结束但复盘未出 |

#### 3.11.2 验收标准

- [ ] SLA 达标率日报（快报 ≤2min / 完整版 ≤10min / 复盘 ≤15min）
- [ ] 异常自动 TG 告警
- [ ] 成本超阈值告警

---

### 3.12 对外 API

| API | 用途 | 说明 |
|---|---|---|
| `POST /api/lead` | 订阅登记 | 表单 → 推站长 TG + 落盘 |
| `POST /api/verify-member` | 会员验证 | identifier → 匹配 → 解锁 |
| `GET /api/stats` | 站点统计 | PV / 访客 / 页面排行 |
| `POST /api/payment/notify` | 支付通知（预留） | 未来接入 webhook 回调 |
| `GET /api/payment/status` | 支付状态查询 | 用户查询入账状态 |

---

## 4. 数据设计

### 4.1 核心实体

| 实体 | 关键字段 |
|---|---|
| Match | slug, league, game, teams, date, started_at/ended_at, format, status, result_inferred, settlement, ot_rule |
| Node | match_slug, game, phase, window_start/end, slice_path, intel_json, report_path, 四段时间戳 |
| Slice | match_slug, node, window, league_files, rows, gap |
| Intel | meta(总数/活跃/密度), teams/players 提及, gray_signals, density_timeline, samples |
| Entity | team/player 画像：提及量, 正负锚, 灰信号记录, BP 战绩 |
| IntelAsset | 主题, 维度, 洞察, 证据, 置信, 验证状态, 时间 |
| Knowledge | 四维知识：选手/队伍/英雄/联赛 |
| PaymentAddress | user_id, chain, address, address_index, reference |
| Payment | user_id, chain, tx_hash, log_index, amount, status |
| Subscription | user_id, plan, status, started_at, expires_at, payment_id |
| Member | identifier(TG/QQ), plan, expires, source |

### 4.2 存储约定

- 原始弹幕 JSONL：只增不改，按 `platform/date` 落盘
- 切片 JSONL：`match_slug_g{game>_<phase>` 命名
- 规则层 intel.json：与切片一一对应
- 状态文件：幂等（存在即跳过）
- SQLite：结构化库（matches/teams/players/gray/bp/leagues/knowledge/assets/payments/subscriptions）
- JSONL/JSON：切片与状态
- MD：知识库与镜像

### 4.3 目录结构（目标工程）

```
danmu-intel/
├── pyproject.toml / requirements.txt / uv.lock
├── README.md / AGENTS.md / CONTRIBUTING.md
├── config/
│   ├── settings.yaml            # 全局配置（联赛/房间/时间窗/SLA/成本阈值/OT规则）
│   ├── leagues.json             # 联赛默认采集集
│   ├── streamers.json           # 直播间注册表
│   ├── team_names.json          # 队伍命名（唯一权威）
│   ├── lexicon.json             # 词表（黑话/灰信号）
│   └── payment.yaml             # ★ 支付配置（链/价格/梯度/钱包）
├── src/danmu_intel/
│   ├── capture/                 # 采集层
│   ├── schedule/                # 赛程与事件
│   ├── slice/                   # 切片层
│   ├── rules/                   # 规则层
│   ├── refine/                  # 提炼层（LLM 客户端 + 提示词渲染）
│   ├── verify/                  # 校验层
│   ├── storage/                 # 存储层（SQLite + 模型）
│   ├── publish/                 # 发布层
│   ├── payment/                 # ★ 支付模块（地址派生/轮询/匹配/订阅）
│   │   ├── addresses.py         #   xpub 派生 + reference 生成
│   │   ├── watcher.py           #   链上轮询（EVM/Solana）
│   │   ├── matcher.py           #   入账匹配 + 幂等
│   │   ├── subscription.py      #   开通/续期/到期
│   │   └── models.py            #   数据模型
│   ├── scheduler/               # 调度层
│   ├── monitor/                 # 监控层
│   └── api/                     # API
├── prompts/                     # ★ 5 份固定提示词
│   ├── report_full.md
│   ├── report_game.md
│   ├── report_pre.md
│   ├── report_live.md
│   └── intel_asset.md
├── data/                        # 运行时数据（切片/规则 JSON/状态）
├── reports/                     # 输出情报页（HTML+MD）
├── runtime/                     # 状态/日志/成本记录
├── tests/                       # 回归测试
└── deploy/                      # systemd units / 部署脚本 / nginx 配置
```

---

## 5. 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | PRD 定稿 |
| 弹幕采集 | aiohttp + vendor/real-url 虎牙库 | 异步 + vendor 化防平台改版 |
| 官方数据 | urllib/requests 直连 Riot/Liquipedia/HLTV | 无额外依赖 |
| LLM | DeepSeek API（OpenAI 兼容） | PRD 定稿，provider 可替换 |
| 链上交互 | web3.py（Polygon）+ solana-py（Solana） | 项目已有依赖倾向（web3.py） |
| 地址派生 | bip_utils（BIP-44） | 纯 Python，轻量 |
| 存储 | SQLite + JSONL + MD | PRD 定稿 |
| 调度 | systemd service + timer | PRD 定稿 |
| 站点 | 静态页生成 + GitHub Pages / nginx | PRD 定稿 |
| 通知 | Telegram Bot API | PRD 定稿 |
| 测试 | pytest + 回归测试集 | PRD 定稿 |
| 部署 | uv/venv + systemd + git 同步 | PRD 定稿 |

---

## 6. 流程设计（端到端）

### 6.1 比赛生命周期

```
1. 每日清单同步 → 比赛进入今日页「未开始」
2. 开播检测 → 启动采集 session（多直播间同场）
3. BP 锁定事件 → 切片 → 规则统计 → 快报（≤2min）→ 完整版（≤10min）同 URL 升级
4. 局中里程碑 → 局中切片 → 局中快报 → 局中完整版
5. 小局结束 → G{n}_end 完整版
6. CS2 加时检测 → G{n}_ot 节点
7. 整场结束（Polymarket 结算仲裁）→ full 整场复盘（≤15min）
8. 比赛结束 → 该场全部节点页转免费公开
9. 情报库沉淀 → 幂等合并 → 趋势延伸 → 画像页更新
10. 验证回填 → 历史库收录 + 今日页移除（零点刷新）
```

### 6.2 付费闭环流程

```
1. 用户注册 → 写入 data/users.json
2. 选档位 + 选链 → 异步派生专属地址
3. 展示地址 + 二维码 + 金额
4. 用户链上转账
5. 后台轮询检测入账（确认数达标）
6. 匹配成功 → 自动写入 members.json
7. 页面解锁（verify-member 通过）
8. 到期前提醒 → 手动续费
```

---

## 7. 部署设计

### 7.1 服务器组件

| 组件 | 说明 |
|---|---|
| danmu-session.service | 采集常驻（按直播间注册表自动启停） |
| danmu-intel-pipeline.timer | 管线每 10 分钟兜底 + 事件钩子 |
| danmu-publish.timer | 发布每 5 分钟（含审计） |
| danmu-payment.timer | ★ 支付轮询每 30s（Polygon）/ 20s（Solana） |
| danmu-api.service | 对外 API（8080） |
| nginx | 站点反代 / 静态托管 |

### 7.2 环境与密钥

- `DEEPSEEK_API_KEY`（生成端）
- `DANMU_INTEL_XPU`（★ EVM xpub，只读公钥）
- `DANMU_INTEL_SOL_ADDRESS`（★ Solana 收款地址）
- `DANMU_INTEL_STATS_SECRET`（API 认证）
- GitHub Deploy Key（站点推送）
- Telegram Bot Token / Chat ID（订阅提醒与告警）
- 所有密钥仅存服务器，权限 600；不入库不提交

### 7.3 部署剧本

```bash
# 1. 拉取工程
git clone <repo> /opt/danmu-intel && cd /opt/danmu-intel
uv sync

# 2. 配置
cp config/settings.example.yaml config/settings.yaml
cp config/payment.example.yaml config/payment.yaml
# 编辑：联赛/房间/SLA/OT规则/支付价格/梯度

# 3. 初始化
python -m danmu_intel.storage init

# 4. 回归测试
pytest -q

# 5. 启动采集与定时任务
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now danmu-session danmu-intel-pipeline.timer \
    danmu-publish.timer danmu-payment.timer danmu-api

# 6. 试跑一页 full 验证
python -m danmu_intel.cli test-full --match <slug>

# 7. 发布上线
python -m danmu_intel.publish deploy --check
```

---

## 8. 非功能需求

### 8.1 性能与 SLA

| 指标 | 目标 |
|---|---|
| BP 后快报 | ≤2 分钟 |
| 局中快报 | ≤2 分钟 |
| 完整版 | ≤10 分钟 |
| 赛后复盘 | ≤15 分钟 |
| 发布部署 | ≤1 分钟 |
| 采集断线重连 | ≤60 秒 |
| 并发 | 同时处理 ≥4 场比赛 |
| 支付入账检测 | ≤2 分钟（Polygon 3 确认 / Solana finalized） |

### 8.2 成本预算

| 项 | 成本 |
|---|---|
| LLM | 单页 ≈0.05 元；每晚 5-6 场 ≤2.5 元 |
| VPS | 2C4G80G ≈ $18-24/月 |
| 链上 | 仅 gas（用户支付），平台零手续费 |
| 支付钱包 | 新建（一次性），无持续成本 |

### 8.3 安全与合规

- 灰信号只作风险标注，对外永远写「观众质疑 · 非结论」
- 弹幕引用脱敏，不展示用户身份
- API Key 只存服务器，权限 600
- **不持有私钥**（xpub 只读 + watch-only）
- 支付模块：系统被完全攻破，攻击者无法转走资金

---

## 9. 测试与回归

### 9.0 覆盖策略（强制要求）

| 维度 | 要求 |
|---|---|
| **整体覆盖率** | **≥ 90%**（语句覆盖率 + 分支覆盖率双达标） |
| **单元测试（单元层）** | 所有模块业务逻辑 100% 覆盖；含正常路径 + 边界 + 异常路径 |
| **E2E 浏览器测试** | 关键用户旅程端到端覆盖（Playwright / pytest-playwright）；含注册→付款→解锁→到期全链路 |
| **测试分层** | 单元（pytest，<1s）→ 集成（pytest + testcontainers，<30s）→ E2E（Playwright，<5min） |
| **CI 门禁** | 覆盖率 < 90% 时 CI 失败，禁止合并 |
| **工具** | pytest + pytest-cov + coverage.py + playwright |
| **数据** | 单元用 mock / fixture；E2E 用测试网（Polygon Mumbai / Solana devnet）+ 浏览器自动化 |
| **覆盖率报告** | HTML 报告（`htmlcov/`）+ XML（CI 消费）；覆盖率差按模块生成热力图 |

### 9.1 单元测试清单

| 测试 | 锁定行为 | 类型 |
|---|---|---|
| test_scan_regression | 扫描/抓取空结果自检 | 单元 |
| test_match_state_guard | 四道闸：刚开赛误判/仅弹幕定胜负/反讽/比分源滞后 | 单元 |
| test_slice_league_isolate | 跨联赛混源 = 0 | 单元 |
| test_node_window | 节点切片边界与官方 schedule 一致 | 单元 |
| test_intel_schema | 规则层 intel.json 字段固定 | 单元 |
| test_render_sections | A 型 0-10 / B 型 0-11 每段恰好一次 | 单元 |
| test_ot_rules | CS2 加时制参数化 | 单元 |
| test_danmu_premature_end | 弹幕提前喊话不判终局 | 单元 |
| test_official_lag | 官方页滞后快照不阻塞 | 单元 |
| test_paywall_free | 比赛结束后该场节点页自动转免费 | 单元 |
| test_library_idempotent | 沉淀引擎同场不重复入库 | 单元 |
| test_verify_member | 会员验证（陌生拒绝/订阅解锁/过期失效） | 单元 |
| ★ test_payment_address_derivation | 地址派生幂等（同用户同链始终返回同一地址） | 单元 |
| ★ test_payment_watcher | 模拟入账事件 → 检测 → 匹配 → 开通 | 单元 |
| ★ test_payment_idempotent | 同一 tx 不重复开通 | 单元 |
| ★ test_payment_underpaid | 金额不足 → 待补款状态 | 单元 |
| ★ test_subscription_expire | 到期自动降级 | 单元 |

### 9.2 E2E 浏览器测试清单

| 测试 | 锁定行为 | 浏览器 |
|---|---|---|
| ★ e2e_register_select_plan | 注册 → 选档位 → 选链 → 展示专属地址+二维码 | Chromium / Firefox / WebKit |
| ★ e2e_payment_flow | 模拟入账 → 页面自动解锁（Pro 内容可见） | Chromium |
| ★ e2e_subscription_expire | 到期 → 自动降级 → Pro 内容隐藏 + 续费提醒 | Chromium |
| ★ e2e_verify_member_api | /api/verify-member 对已订阅/未订阅/过期返回正确 | Playwright request |
| ★ e2e_responsive_layout | 情报页在手机/平板/桌面三档响应正确 | 三 viewport |
| ★ e2e_paywall_toggle | 比赛结束前后 Pro/免费分层自动切换 | Chromium |
| ★ e2e_idempotent_refresh | 同一用户重复打开订阅页，地址不变 | Chromium |

### 9.3 发布门禁

```
pytest 全绿 → 覆盖率 ≥ 90% → 生成页结构门禁 → 事实层官方校准
→ 终局四信号 → 全站审计 → E2E 通过 → 支付模块测试全绿 → 发布 → 线上抽查
```

---

## 10. 里程碑与交付计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 | 工程骨架：目录、配置、依赖、CI 测试 | pytest 全绿 |
| M1 | 采集 + 切片 + 规则层 | 真实比赛多直播间采集、切片边界正确 |
| M2 | LLM 提炼 + 校验 + 输出 | 一页 full 对照样页逐段一致；成本达标 |
| M3 | ★ 付费闭环（payment 模块） | 测试网入账 → 自动开通 → 页面解锁 |
| M4 | 发布 + 付费墙 + 订阅 | 全链路自动化；Pro/免费分层正确 |
| M5 | 情报资产沉淀 + 四维知识库 + 验证闭环 | 每场提炼资产并幂等合并 |
| M6 | 监控 + SLA 指标 + 告警 | SLA 达标率日报；异常自动告警 |

---

## 11. 验收标准（总）

| # | 标准 | 来源 |
|---|---|---|
| 1 | 项目为标准 Python 工程，可一键部署到 VPS | PRD §16.1 |
| 2 | 全链路自动化：比赛开始到复盘入库**无需人工干预** | PRD §16.2 |
| 3 | 输出结构唯一（旧 10 段框架），样页逐段对照一致 | PRD §16.3 |
| 4 | 事实层只信官方，弹幕结论可溯源，缺数据显式标「无」 | PRD §16.4 |
| 5 | 四类失效模式有门禁与回归测试锁定 | PRD §16.5 |
| 6 | SLA：快报 ≤2min / 完整版 ≤10min / 复盘 ≤15min | PRD §16.6 |
| 7 | 成本：单页 ≈0.05 元，每晚 5-6 场 ≤2.5 元 | PRD §16.7 |
| 8 | ★ 付费闭环：链上入账 → 自动开通 → 页面解锁 → 到期全自动 | 本文档 §3.9.1 |
| 9 | 站点 Pro/免费分层正确，结束即免费开放 | PRD §16.9 |
| 10 | 情报资产：每场自动提炼并沉淀，四维可查、可溯源、可验证 | PRD §16.10 |
| 11 | 监控：SLA/成本/健康度日报 + 异常 TG 告警 | PRD §16.11 |
| 12 | 终局防误：任何"比赛结束"发布必经四信号 + 四道闸 | PRD §16.12 |
| 13 | ★ 支付模块：服务器不持有私钥（xpub 只读） | 本文档 §3.9.1 |
| 14 | ★ 支付模块：支持 Polygon + Solana 两条链 | 本文档 §3.9.1 |
| 15 | ★ **测试覆盖 ≥ 90%**：单元测试 + E2E 浏览器测试双层覆盖；CI 门禁低于 90% 禁止合并 | 本文档 §9 |

---

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| 弹幕源失效（房间改名/平台改版） | 注册表热更新 + 断线重连 + 完整性标注 + 告警 |
| 官方 API 失效 | 多源交叉（Riot/Liquipedia/HLTV）+ 定期校验 |
| LLM 输出幻觉 | 固定提示词 + 门禁 + 无中生有拦截 + 重试 ≤3 次 |
| 成本失控 | usage 记录 + 阈值告警 + 样本量限制 + 重试限制 |
| ★ 链上入账漏检 | 双轨检测（轮询 + 预留 webhook）；确认数达标才开通 |
| ★ 地址派生冲突 | 幂等 + 唯一索引；异步 worker 串行派生 |
| ★ 私钥泄露 | **不持有私钥**（xpub 只读）；系统被攻破也无法转走资金 |
| 多场比赛并发 | 并行生成（≤4 并发）+ 状态幂等 + 队列 |

---

## 13. 待用户决定项

| # | 问题 | 当前状态 |
|---|---|---|
| 1 | ★ 早鸟窗口：原定 2026-09-15 已过（今天 2026-09-21），新窗口如何设定？ | 需用户决定 |
| 2 | ★ 收款钱包：新建钱包的助记词由谁保管？如何备份？ | 需用户决定 |
| 3 | ★ RPC 节点：Polygon/Solana 的 RPC 端点（自建 or 公共）？ | 需用户决定 |
| 4 | ★ 早鸟名额：限 20 名是否保留？新窗口下名额多少？ | 需用户决定 |

---

## 附录 A：与蓝本（danmu-intel-local）的差异清单

| 项 | 蓝本 | 本项目 |
|---|---|---|
| LLM 提炼 | 手动（`USE_LLM=False`） | **自动**（程序直连 DeepSeek） |
| 生成路径 | 三条并存（Codex/DeepSeek/规则直出） | **唯一权威**（程序固化） |
| 付费闭环 | 表单推 TG + 手工维护 members.json | **链上收款自动开通** |
| 支付模块 | 不存在 | **新增 `src/danmu_intel/payment/`** |
| 收款链 | N/A | **Polygon + Solana** |
| 钱包 | 不持有私钥 | **新建收款钱包 + xpub 只读** |
| 会员验证 | TG/QQ 用户名客户端验证 | **链上支付记录 + 会员验证双轨** |
| 价格文案 | 硬编码在 add_paywall.py | **config/payment.yaml 配置化** |

## 附录 B：参考资料

- `danmu-intel-local/docs/task/DANMU_INTEL_CLOUD_PYTHON_PROJECT_PRD.md`（PRD v2.0 定稿）
- `danmu-intel-local/docs/task/DANMU_INTEL_SERVER_PRD_2026-08-31.md`
- `danmu-intel-local/docs/task/INTEL_PRODUCT_FRAMEWORK_2026-08-31.md`
- `danmu-intel-local/docs/task/SUBSCRIPTION_LEDGER.md`
- `danmu-intel-local/docs/task/PRICING_ESPORTS_BUNDLE_2026.md`
- Solana Pay 官方规范：https://docs.solanapay.com/spec
- BTCPay Server 官方 FAQ：唯一充值地址标准做法
