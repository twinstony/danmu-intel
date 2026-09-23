# 弹幕情报库

把公开弹幕变成可复核的情报报告页。

- 需求：[`docs/requirements/DANMU_INTEL_REQUIREMENTS.md`](docs/requirements/DANMU_INTEL_REQUIREMENTS.md)
- 设计：[`docs/design/ENGINEERING_DESIGN_v2.md`](docs/design/ENGINEERING_DESIGN_v2.md)
- 领域术语：[`CONTEXT.md`](CONTEXT.md)｜架构决策：[`docs/adr/`](docs/adr/)

## 当前能力（T1+T2+T3+T5）

**T1**：虎牙**单直播间**真实弹幕 → append-only JSONL → 人工指定小局起止 → 基础统计 →
规则直出**十一段**报告页。

**T2**：同场比赛**多直播间并发采集**（一房间一子进程）+ 采集监督：心跳 5 秒、无消息
60 秒重连、无首条消息 120 秒 `no_stream`、进程被杀/僵死自动拉起（退避 1s→60s，
30 分钟内重启超 5 次停止重试并留因）、磁盘可用 < 5GB 报警、每房间贡献量可查。

**T3**：**SOOP 平台适配器**接入（ADR-0008 首发第二平台）。适配器只做
「平台原始 payload → `DanmuEvent`」；接入新平台 = 新增一个模块 + 注册表加一行，
同一套契约测试同时覆盖两个平台（`tests/contract/test_adapter_contract.py`）。

**T5**：报告**三形态**（赛中快报 ≤2 分钟 / 完整版 ≤10 分钟 / 复盘版 ≤15 分钟）+
**事实·解读分层**（解读段明确标注、解读层输入只有事实层，`fact_layer_hash` 留指纹）+
**SHA256 溯源**（每项事实带文件 + 行范围 + 封存哈希）；同一形态换版即新增版本，
缺解读段或来源对不上时**拒绝发布**。

不含真 LLM、付费墙、公网发布、Twitch/KICK（注册表留位）、后台（见设计 §19 实施分层）。

## 安装

```bash
pip install -e '.[dev]'
bash deploy/hooks/install.sh     # 装上「提交前凭据检查」钩子
```

## 跑通一遍

```bash
# ① 先登记比赛（采集必须挂在一场比赛上）
danmu-intel match add --league LPL --team-a iG --team-b LNG --state ended \
  --official-result '{"score":"2:0"}'

# ② 采集真实弹幕（seconds 省略则持续采集到 Ctrl-C）
danmu-intel collect --url https://www.huya.com/660000 --seconds 300 --match-id 1

# ②-2 SOOP 也一样，只把平台换成 soop（房间标识 = 主播频道）
danmu-intel collect --platform soop --url https://play.sooplive.com/afchall --seconds 300 \
  --match-id 1

# ②' 多房间并发采集 + 监督（一房间一子进程；seconds 省略则跑到所有房间停下）
danmu-intel supervise --match-id 1 --seconds 1800 \
  --room https://www.huya.com/660000 \
  --room https://www.huya.com/323444 \
  --room https://www.huya.com/11342412

# ③ 人工指定这一局的起止（毫秒时间戳，可从落盘记录里取）
danmu-intel slice --match-id 1 --game-no 1 --start-ms 1790064000123 --end-ms 1790064300123

# ④ 规则统计
danmu-intel stats --match-id 1

# ⑤ 三形态报告（发布即上线；缺解读段 / 来源对不上会被拒绝）
danmu-intel report --match-id 1 --kind live_brief --completed-game 1 --trigger-game 1
danmu-intel report --match-id 1 --kind full      # → site/matches/1/full.html
danmu-intel report --match-id 1 --kind review    # → site/matches/1/review.html
danmu-intel reports --match-id 1                 # 已发布的形态 × 版本（含事实层哈希）

# ⑥ 自检
danmu-intel verify-sources --match-id 1 --kind full  # 逐项复核来源（文件 + 行范围 + SHA256）
danmu-intel rebuild        --match-id 1  # AC-13：删统计后重算，结果必须逐字节相同
python3 tools/check_no_secrets.py        # AC-12：全库零命中可动用资产凭据
```

### 报告三形态怎么看（T5）

- **段集固定**：同一形态每次发布的段号集合一致（结构稳定）。赛中快报是完整十一段的
  真子集（去掉需要终局对照的「预测验证」），完整版与复盘版都是全十一段。
- **完成节点才进正文**：`--completed-game N` 声明已完成的小局（可重复）；进行中的节点
  既不出现在统计里，也不出现在取材范围的条数里，只在「未纳入本报告的节点」这句里出现。
- **事实与解读分层**：段性质只有「事实」与「解读」两种标记（「事实 + 解读」两者并存），
  解读段一律带「（解读，非事实）」标注；解读层只拿事实层当输入，其指纹记在
  `reports.fact_layer_hash` 与页面上。
- **发布钩子**：段集齐备、**解读段齐备**（AC-16）、来源文件与采集时封存的 SHA256 一致，
  任一项不通过即拒绝发布（`reports` 留一行 `state='failed'`，页面不落盘）。
- **时限**：2 / 10 / 15 分钟是形态常量；实测耗时记进 `reports.timing_json`，
  超时**不阻断**发布（NFR-T：准确性优先），但会显示在 `report` 的输出里。
- 解读层调用点是注入缝（`Interpreter` 协议）；本票只有规则直出兜底，
  `llm_state='rule_fallback'` 如实标注，真 LLM 属 T6。

### 采集状态怎么看（T2）

```bash
danmu-intel health       --match-id 1   # 每房间：状态/PID/最后一条消息/重连/重启/严重级别/最近异常
danmu-intel contribution --match-id 1   # 每房间贡献量：条数 / 时间跨度 / 去重后条数（AC-15）
danmu-intel events       --match-id 1   # 采集异常事件（待 T11 通知通道投递）
```

- 一房间一子进程；主进程只调度（不碰网络），5 秒轮询一次。
- 子进程每 5 秒写心跳（库里的 `room_sessions` 行 + `runtime/heartbeat/<平台>-<房间>.json`）。
- 进程退出/僵死 → 退避 1s→2s→…→60s 重拉；**同一房间 30 分钟内重启超过 5 次**则停止重试
  并以非零退出码结束，停止原因写进事件库（`danmu-intel events` 可查）。
- 断流 60 秒触发重连（累计 `reconnects`）；120 秒没有首条消息判 `no_stream`（不退出，
  房间可能只是还没开播）；数据盘可用 < 5GB 产生 `disk_low`（不静默）。
- **AC-15 的 30 分钟真实验收**：上面 `supervise` 那条命令跑满 `--seconds 1800`（建议选
  三个确实在解说同一场比赛的直播间），然后用 `health`/`contribution` 逐房间核对条数、
  时间跨度与去重后条数——三房间互不为子集，合计不等于任一房间的条数。

### 平台适配器（T3）

平台差异全在各自的适配器模块里（`collect/huya.py` / `collect/soop.py`）：

| 平台 | 房间标识 | 弹幕链路 | 探测 |
|---|---|---|---|
| 虎牙 | 房间号（`lProfileRoom`） | `wss://cdnws.api.huya.com/`（Tars 帧） | 公开移动页字段 |
| SOOP | 主播频道 `bj_id`（`broad_no` 每场都变，不能当房间主键） | `wss://chat-<IP 十六进制>.sooplive.com:<CHPT+1>`（自研二进制包：登录 → 进频道 → 弹幕） | `player_live_api` 的 `CHANNEL` 报价 |

两个适配器共用同一套重连、落盘、建库逻辑：适配器只管「平台原始 payload → `DanmuEvent`」，
`socket` 级断流/报错/静默 60 秒都由 `collect/adapter.py` 的 `reconnecting` 接手重连，
并把原因回调给会话层（累计 `reconnects`、标 `stalled`）。

## 数据落点

| 内容 | 位置 | 进 git 吗 |
|---|---|---|
| 原始弹幕 JSONL | `~/danmu-intel-data/raw/<platform>/<yyyy-mm-dd>/<room_id>-<hh>.jsonl` | 否 |
| SQLite | `~/danmu-intel-data/db.sqlite3`（WAL） | 否 |
| 心跳文件 | `~/danmu-intel-data/runtime/heartbeat/<platform>-<room_id>.json` | 否 |
| 用户哈希盐值 | `~/danmu-intel-data/salt`（0600） | 否 |
| 站点产物 | `site/matches/<match_id>/<kind>.html`（每场每形态一份） | 是 |

`DANMU_INTEL_DATA` / `DANMU_INTEL_SITE` 可覆盖上面两个位置（测试用它指向临时目录）。

T1 曾提交过一份样例产物 `site/matches/1.html`；T5 把页面路径改成
`site/matches/<match_id>/<kind>.html`（每场每形态一份），旧的单文件路径已删除
（AGENTS.md：不留兼容层）。换一台机器跑 `danmu-intel report --kind full` 会用当地数据
重新生成。

`~/danmu-intel-data/raw/soop/2026-09-23/seokwngud-00.jsonl` 是 T3 验收的真实采集：
SOOP `seokwngud` 频道连采 5.5 分钟（会话 #5，329 条弹幕，落盘 437 条），
只存 `user_hash`，不落明文身份。

## 测试

```bash
pytest          # 覆盖率门禁 90%；全程不连外网（平台数据用录制帧回放）
```

平台数据的回放 fixture 由 `tools/record_fixtures.py` 生成（`--platform huya|soop`）：先
`record` 连真实直播间录原始帧，再 `sanitize` 把身份与原文替换成样例值后写入
`tests/fixtures/<平台>/frames.jsonl`（录制帧文件本身不进仓库）：

```bash
python3 tools/record_fixtures.py record   --platform soop \
  --url https://play.sooplive.com/seokwngud --seconds 120 --dump .frames-dump/seokwngud.jsonl
python3 tools/record_fixtures.py sanitize --platform soop \
  --dump .frames-dump/seokwngud.jsonl --out tests/fixtures/soop/frames.jsonl
```

适配器契约测试（`tests/contract/test_adapter_contract.py`）**对注册表里的每个平台**
跑同一套断言（字段齐备、时间单调、非法 payload 不崩、断流触发重连）；
新增平台只需加一行注册 + 每个平台一行接线（回放工厂/适配器工厂/探测桩/样例链接）。

采集监督的测试缝在 `tests/unit/test_supervisor.py`（假时钟 + 假子进程 + 真心跳）；
`tests/e2e/test_supervisor_processes.py` 另外用**真进程**跑三个房间，验证
「不重不漏」与「kill 后 10 秒内拉起」，子进程仍是录制帧回放（`tests/e2e/replay_child.py`）。

## 边界

- 公开弹幕是唯一数据来源，不使用任何需要突破访问限制的手段。
- 原始记录**不落明文身份**：只存平台用户 ID 的加盐哈希（`user_hash`）。
- 灰信号只作风险提示，不指控、不点名、必须附样本与门槛（需求 §6.5）。
