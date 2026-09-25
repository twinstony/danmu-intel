# 弹幕情报库

把公开弹幕变成可复核的情报报告页。

- 需求：[`docs/requirements/DANMU_INTEL_REQUIREMENTS.md`](docs/requirements/DANMU_INTEL_REQUIREMENTS.md)
- 设计：[`docs/design/ENGINEERING_DESIGN_v2.md`](docs/design/ENGINEERING_DESIGN_v2.md)
- 领域术语：[`CONTEXT.md`](CONTEXT.md)｜架构决策：[`docs/adr/`](docs/adr/)

## 当前能力（T1+T2+T3+T4+T5+T6+T7+T8+T9）

**T1**：虎牙**单直播间**真实弹幕 → append-only JSONL → 人工指定小局起止 → 基础统计 →
规则直出**十一段**报告页。

**T2**：同场比赛**多直播间并发采集**（一房间一子进程）+ 采集监督：心跳 5 秒、无消息
60 秒重连、无首条消息 120 秒 `no_stream`、进程被杀/僵死自动拉起（退避 1s→60s，
30 分钟内重启超 5 次停止重试并留因）、磁盘可用 < 5GB 报警、每房间贡献量可查。

**T3**：**SOOP 平台适配器**接入（ADR-0008 首发第二平台）。适配器只做
「平台原始 payload → `DanmuEvent`」；接入新平台 = 新增一个模块 + 注册表加一行，
同一套契约测试同时覆盖两个平台（`tests/contract/test_adapter_contract.py`）。

**T4**：**切片引擎**（官方 > 弹幕信号 > 报告窗口，冲突必记录；弹幕候选需 **≥2 类独立
信号**复核；人工修正必带操作者与理由、落审计、算法版本递增）+ **统计全集**（密度曲线 /
峰值 / 低谷 / 比分交叉校验 / 击杀时间轴 / 中立指标）+ **终局判定**（≥3 类独立信号同时
成立且 2 分钟无反转）+ **灰信号**（多人多时段门槛、必附样本、渲染层零身份、不提供导出）。

**T5**：报告**三形态**（赛中快报 ≤2 分钟 / 完整版 ≤10 分钟 / 复盘版 ≤15 分钟）+
**事实·解读分层**（解读段明确标注、解读层输入只有事实层，`fact_layer_hash` 留指纹）+
**SHA256 溯源**（每项事实带文件 + 行范围 + 封存哈希）；同一形态换版即新增版本，
缺解读段或来源对不上时**拒绝发布**。报告正文消费 T4 的统计产物（终局判定、比分交叉校验、
边界来源、灰信号样本）。

**T6**：**解读层 LLM**（DeepSeek，OpenAI 兼容）——受约束输入（提示词版本化于 `prompts/`，
输入只有事实层 JSON，输出受 JSON Schema 约束：段号 → 文本）+ **反幻觉后置校验**（解读里的
数字/比分/百分比/名称逐个比对事实层，出现新事实即丢弃重试 1 次，再失败降级）+ **成本硬闸**
（单次 25 秒超时；单场 > ¥0.3 或当日 > ¥10 立即降级并报警）+ **降级不静默**
（`llm_state='rule_fallback'`，命令输出、报告第 10 段与页面横幅都写明原因）。
凭据只从仓库外 `.env`（0600）读，不入库、不入 git。

**T7**：**发布闭环** —— 整棵**站点树**（索引 / 历史情报库 / 联赛页 / 比赛页 / 报告页 /
画像库 / 灰信号页 / 验证闭环页 / 订阅页）经 **7 项纯函数检查**（需求 §6.8 的 6 项 +
来源引用可达加固）后**原子发布**（`site/.staging` → 检查 → 逐条目 `os.replace` → 提交推送 →
Vercel 构建；任一项不过就一个条目都不换，线上保持上一版）；**秒级回滚**（Vercel 即时回滚 +
`git revert` 跟进对齐账本）；比赛结束**自动**再发布公开版（幂等）；**付费正文不进静态产物**
（付费页只有标题、段目与付费说明，正文只经凭据 API 返回）。

**T8**：**链上监听** —— Polygon 走 **Polygonscan**（Etherscan V2 多链 API，原生币 + ERC20）、
Solana 走 **Helius**（`getSignaturesForAddress` + `getTransaction`，带每单唯一 `memo`）：
**一个收款地址一个游标**（`chain_cursors`，Polygon 区块号只前进、Solana 签名只在处理完
新记录时推），**启动补扫 + 每 60 秒增量轮询**，两条路径互为兜底（**故意漏掉一笔，事后补扫照样能
发现它**）；**每次调用都记 `quota_usage`**（成功与失败都记），用量 **>80%** 或撞**限速**一律写
`critical` 待投递报警而不是静默重试（投递属 T11）；`danmu-intel chain-usage` 随时看当日/当月
用量、上限、限速与「扫到哪了」。链上数据里只有地址、交易、金额与 memo —— **没有任何可动用
资产的凭据**（不持有私钥/助记词，API key 只从仓库外 `.env` 读，不进日志与异常消息）。

**T9**：**会员付费全自助闭环** —— 用户选档 → 填既有通讯账号（Telegram @name / QQ 号，**不注册本站账号**）
→ 拿到**专属收款要求**（Polygon 从 xpub 按 BIP44 `m/44'/60'/0'/0/i` 派生的 watch-only 地址；
Solana 单一收款地址 + **每单唯一 memo**；金额含唯一小额尾数）→ 链上入账被自动对账
（`chain-watch --orders` 从订单派生监听目标）→ **到账即自动开通**（**无需人工**）→ 用户凭
「账号 + 订单引用 + **领取令牌**」自助领取**凭据**（32 字节随机码，**库里只存哈希**）→ 凭据读到
**付费正文**（`GET /api/report/<id>/<kind>/paid`，正文从不进静态产物）。会员有效期可见、到期有
**宽限期**（默认 24 小时）内仍可访问、**续费从原到期日顺延**；开通/续费/降级/撤权全部写 `audit_log`。
**防枚举**：校验/领取接口对「账号不存在」「未开通」「已过期」「凭据不对」「被限流」返回**逐字节相同**
的响应，账号字段只用于限流分桶、不参与判定 —— 任何人都问不出「某人是不是会员」。
**幂等**：逐笔入账（`tx_ref` 唯一）+ 开通（按 `tx_ref`）两层幂等键，重复检测只开通一次；
不足额转「待补款」并告知差额，对不上账 / 多付 / 开通失败都会写报警，**绝不静默**。
**零资金风险面**：系统只有 xpub、派生地址、memo 与收款地址，**没有任何签名能力**；
`tools/check_no_secrets.py` 新增「扩展私钥」模式，xprv 一出现就拦。

不含通知投递、后台、站点统计、归档（见设计 §19 实施分层；T9 的到期提醒与凭据重发走通讯渠道，
属 T11）。

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

# ③ 切片引擎：按优先级裁决小局边界（官方 > 弹幕信号 > 报告窗口），冲突必记录
danmu-intel boundaries --match-id 1                       # 用官方数据 + 弹幕信号复核
danmu-intel boundaries --match-id 1 --dry-run \
  --report-window 1:1790064000123:1790064300123           # 回填已发布报告窗口（只试算）

# ③' 人工修正（覆盖已有边界必须给操作者与理由；落审计、算法版本递增）
danmu-intel slice --match-id 1 --game-no 1 \
  --start-ms 1790064000123 --end-ms 1790064300123 \
  --override-by 管理员 --override-reason 对齐官方开赛时间

# ④ 规则统计 → ⑤ 报告三形态（发布即上线；缺解读段 / 来源对不上会被拒绝）
danmu-intel stats  --match-id 1          # 统计全集 + 终局判定 + 灰信号落库
danmu-intel final  --match-id 1          # 终局判定明细（信号、首次满足时刻、是否反转）
danmu-intel gray   --match-id 1          # 灰信号（只作风险提示，输出里没有任何身份）
danmu-intel report --match-id 1 --kind live_brief --completed-game 1 --trigger-game 1
danmu-intel report --match-id 1 --kind full      # 完整版 → site/matches/1/full.html
danmu-intel report --match-id 1 --kind review    # 复盘版 → site/matches/1/review.html
danmu-intel reports --match-id 1                 # 已发布的形态 × 版本（含事实层哈希）

# ⑦ 出整棵站点树并发布（7 项检查 → 原子替换 → 提交/部署；默认推 git + 调 Vercel）
danmu-intel publish --dry-run        # 只跑检查：看哪一项会拦下（不动产物）
danmu-intel publish --no-deploy      # 只落本地产物（不推 git、不调 Vercel）
danmu-intel publish                  # 正式发布（凭据与项目在仓库外 .env）
danmu-intel releases                 # 发布批次账本：版本/指纹/提交/部署/本批付费的比赛

# ⑧ 比赛结束 → 自动转公开；出错 → 秒级回滚
danmu-intel match set-state --match-id 1 --state ended   # 状态机写入即触发公开版再发布
danmu-intel rollback                 # 回滚到上一批（Vercel 即时回滚 + git revert 跟进）

# ⑨ 链上监听（Polygonscan + Helius；长驻模式 = 启动补扫 → 每 60 秒增量轮询）
danmu-intel chain-watch --polygon-address 0x… --solana-address 5x…   # 长驻监听（Ctrl-C 停）
danmu-intel chain-watch --polygon-address 0x… --once    # 只跑一轮增量（按游标；cron 友好）
danmu-intel chain-watch --solana-address 5x… --rescan   # 补扫一次（按地址查全历史，不依赖游标）
danmu-intel chain-usage                                 # 额度：当日/当月用量、上限、限速 + 监听游标

# ⑩ 自检
danmu-intel verify-sources --match-id 1 --kind full  # 逐项复核来源（文件 + 行范围 + SHA256）
danmu-intel rebuild        --match-id 1  # AC-13：删统计后重算，结果必须逐字节相同
python3 tools/check_no_secrets.py        # AC-12：全库零命中可动用资产凭据
```

### 报告三形态怎么看（T5）

- **段集固定**：同一形态每次发布的段号集合一致（结构稳定）。赛中快报是完整十一段的
  真子集（去掉需要终局对照的「预测验证」），完整版与复盘版都是全十一段。
- **完成节点才进正文**：`--completed-game N` 声明已完成的小局（可重复）；进行中的节点
  既不出现在统计里，也不出现在取材范围的条数里，只在「未纳入本报告的节点」这句里出现。
  比赛级的结论（终局判定、灰信号）也按覆盖范围重算 —— 正文不得引用取材范围之外的证据。
- **事实与解读分层**：段性质只有「事实」与「解读」两种标记（「事实 + 解读」两者并存），
  解读段一律带「（解读，非事实）」标注；解读层只拿事实层当输入，其指纹记在
  `reports.fact_layer_hash` 与页面上。
- **发布钩子**：段集齐备、**解读段齐备**（AC-16）、来源文件与采集时封存的 SHA256 一致，
  任一项不通过即拒绝发布（`reports` 留一行 `state='failed'`，页面不落盘）。
- **时限**：2 / 10 / 15 分钟是形态常量；实测耗时记进 `reports.timing_json`，
  超时**不阻断**发布（NFR-T：准确性优先），但会显示在 `report` 的输出里。
- 解读层调用点是注入缝（`Interpreter` 协议）：T6 的真 LLM 实现了同一个协议，
  组装与渲染层一行没改。

### 解读层 LLM（T6）

**开箱即用**：配了凭据就走 LLM，没配就规则直出并**标注原因**（不会静默降级）。

```bash
# ① 凭据只放仓库外，权限必须 600（权限不对会拒绝读取，宁可不发不可泄露）
install -m 600 /dev/null ~/danmu-intel-data/.env
printf 'DEEPSEEK_API_KEY=%s\n' '你的密钥' >> ~/danmu-intel-data/.env
# 可选：DEEPSEEK_MODEL=deepseek-v4-pro / DEEPSEEK_BASE_URL=https://api.deepseek.com

danmu-intel report --match-id 1 --kind full
# 输出里会写：解读层 llm；调用 7 次，本次 ¥0.0xxx｜单场累计 …｜当日累计 …（硬闸 ¥0.3 / ¥10）
```

- **受约束输出**：提示词版本化在 `prompts/interpretation/<版本>/`（`PROMPT_VERSION` 是
  唯一版本来源，段集变了必须改提示词，否则启动即报错）；输入**只有**事实层 JSON，
  输出必须是 `{"segments": {"<段号>": "<正文>"}}`（键多了少了都算不合格）。
- **反幻觉后置校验**：抽解读里的数字 / 比分 / 百分比 / 名称，逐个比对事实层
  （允许集 = 模型看到的那份 JSON + 时间戳的确定性改写，结构编号先屏蔽）；
  出现新事实 → 丢弃该段重试 1 次（把违规项回灌给模型）→ 仍失败该段规则直出。
- **失败处理**：单次调用 25 秒超时；超时/断网/报错**不重试**（快报的 2 分钟优先），
  直接该段降级；解读阶段总预算 25 秒，剩余不足就不再调用。
- **成本硬闸**：`llm_calls` 逐次记账；单场 ≥ ¥0.3 或当日 ≥ ¥10 → 下次调用前即降级 +
  写一条 `llm_cost_gate` 报警（`danmu-intel events` 可查，投递属 T11）。
- **全局降级**：账本里连续 ≥3 次失败（超时/报错/校验不过）→ 后续段落不再调用 LLM，
  出现一次成功调用自动恢复；页面横幅与第 10 段写明原因。
- **可回溯**：每次调用记 `model / prompt_version / tokens / cost_cny / latency_ms / outcome`，
  报告行的 `fact_layer_hash` 指向当时那份事实层。

### 发布闭环怎么看（T7）

- **产物**：`danmu-intel publish` 按库里的比赛、报告账本、灰信号与官方数据汇总出整棵树，
  写进 `site/`（索引 / 历史情报库 / 联赛页 / 比赛页 / 报告页 / 画像库 / 灰信号页 /
  验证闭环页 / 订阅页），外加一份 `site/release.json`（版本标识：版本号 + 树指纹 + 页面清单，
  **最后**写，它是「这一批已完整上线」的标记）。
- **7 项检查**（`publish/checks.py`，逐项纯函数，输入是产物树 + 比赛状态）：
  ① 全站导航唯一（无重复条目、无孤儿链接、无孤儿页面，且导航项必须真的渲染在页面上）
  ② 无旧模板残留 ③ 付费墙正确（逐页对照**比赛状态机**）④ 报告分段完整（对照需求 §6.6 十一段，
  与报告层用同一断言）⑤ 页面 × 联赛 × 标识一致 ⑥ 无「速览卡」类残留物
  ⑦ 来源引用可达（加固项：文件 + 行范围 + 封存 SHA256）。
  **任一项不过都不发布**，并写一条 `critical` 待投递报警（`danmu-intel events` 可查）。
- **原子发布**：先在 `site/.staging/` 生成，检查全过后逐条目 `os.replace` 换进 `site/`，
  删掉上一批有、这一批没有的页面（**运维手工放的文件如 `vercel.json` 不动**），
  最后写 `release.json`。同一棵树再发一次是**幂等**的（指纹没变就不重复提交、不重复部署）。
- **秒级回滚**：`rollback` 先调 Vercel 即时回滚到上一批的部署，再 `git revert` 掉坏版本的
  提交让「仓库 = 线上」一致；② 失败不回滚①（线上已经好了），只写一条报警。
- **结束转公开**：可见性只由 `matches.state` 派生（`state == ended` → 公开）。比赛转
  `ended` 后 `match set-state` 会自动再发布公开版（`sync_ended`，幂等）；**永不**按文件名
  或路径判定（ADR-0009）。
- **付费正文隔离**：比赛结束前，该场报告页只有标题、段目与付费说明 —— 正文与来源一个字节
  都不进静态文件，`curl` 到页面也拿不到；正文只经 `publish/access.py` 的出口返回（会员凭凭据，
  比赛结束后所有人可见），HTTP 层属 T9。
- **选手页**：画像库只由官方数据汇总（队伍页来自比赛登记与官方比分）。**没有官方阵容就不生成
  选手页** —— 原始弹幕只存加盐用户哈希，不从弹幕里推断「选手」是谁（ADR-0015 决策 9）。

```bash
# 仓库外 .env 里放发布凭据（0600；绝不入 git）
printf 'VERCEL_TOKEN=%s\nVERCEL_PROJECT_ID=%s\n' '你的 token' '你的项目 id' \
  >> ~/danmu-intel-data/.env      # 可选：VERCEL_TEAM_ID、VERCEL_API_BASE
```

### 链上监听怎么看（T8）

- **两条独立路径互为兜底**（ADR-0005 / ADR-0016）：`poll` 从该地址的游标往后扫（日常）；
  `rescan` **不看游标**、直接按地址查全历史（启动补扫、异常恢复、手动补扫）。所以「游标坏了、
  跳了、被别的地址的入账越过了」都还能把那笔重新摆到桌面上 —— AC-5 的「故意漏掉一笔，事后补扫
  能发现它」就是这么验的（`tests/e2e/test_chain_watch.py`）。
- **游标管到地址一级**（`chain_cursors(network, scope=地址)`），不按链存一个全局游标：多地址
  共用一个游标时，一个地址的入账会被另一个地址推进的游标越过去。Polygon 的游标是「已处理到的
  最新入账区块」且**只前进不回退**；Solana 游标是「已处理的最新签名」（签名没有可用的大小序，
  夹不住，只能靠「真的处理完才写」保证单调）。**没扫到东西就不推游标** —— 不假装进度。
- **额度与限速**（FR-C6-11）：账本按供应商按日累加（`quota_usage`），**每一次打出去的请求都
  记**（成功与失败、包括被限速的那次）。上限窗口不同就如实不同：Polygonscan 日 10 万 calls
  （5 calls/s）、Helius 月 100 万 credits（10 req/s）。**用量 >80%** 或**撞限速**都写一条
  `critical` 待投递报警（`danmu-intel events` 可查，投递属 T11），**不静默退避重试** ——
  「这段时间看不见链上」本身就是必须让人知道的事实。同一窗口同类告警只报一次（去重），
  恢复正常后再出问题会重新报（重启后也会再报一次，那正是「这件事还在吗」的答案）。
- **翻页有上限，撞上限是报警而不是给半截**：一次扫描最多 1 万条记录（`MAX_PAGES × PAGE_SIZE`）。
  翻满上限且还有剩就报错 → 报警且**游标不动**，下轮重来；宁可停下，也不把半截历史当成全部
  （把没取回的那段越过去就是静默漏检）。
- **金额一律是最小单位整数**（wei / lamports / 代币最小单位），与 `orders.amount_due_units`
  同一口径；一笔入账是统一的 `Transfer`（`network/address/tx_ref/asset/units/at_ms/memo/block`），
  订单匹配（T9）只看这一个形状。Solana 的 `memo` 是每单唯一标识（ADR-0004）。
- **零资金风险面**（AC-12 / FR-C6-17..19）：监听层只**看见**入账 —— 代码里没有私钥、没有助记词、
  没有任何签名能力；API key 只从仓库外 `.env`（0600）读，只进查询参数，异常与日志里只有状态码
  与供应商原文。

```bash
# 仓库外 .env 里放供应商凭据（0600；绝不入 git；也可放进同一个文件）
printf 'POLYGONSCAN_API_KEY=%s\nHELIUS_API_KEY=%s\n' '你的 key' '你的 key' \
  >> ~/danmu-intel-data/.env
```

### 会员付费怎么跑（T9）

先把收款配置写进去（**只给 xpub，绝不给私钥/助记词**；xpub 是 watch-only 公开信息）：

```bash
danmu-intel billing                                       # 看档位/价格/宽限期/收款配置
danmu-intel billing --set polygon_xpub=xpub6… \
                    --set solana_address=9xQeWv… \
                    --set api_base=https://<你的>.ts.net:8443 \
                    --actor 管理员                        # 改动写 config.update 审计
danmu-intel billing --set 'tiers=[{"key":"standard","label":"标准档","amount_units":5000000,"days":30}]'
```

档位与价格在 `config` 表（标准档默认 5.00 USDT / 30 天，试用档 0.50 USDT / 3 天；**改价格不影响
已生效的会员**）。金额一律是最小单位整数（USDT 6 位小数），钱不用浮点算。

**一条命令跑通「下单 → 收钱 → 开通 → 领凭据」**：

```bash
# ① 用户下单（页面走 POST /api/orders，这里是命令行等价物）
danmu-intel subscribe --platform telegram --username @reader --tier standard --network polygon
#    → 订单 DM1A2B3C4D｜应付 5.000001 USDT｜收款地址 0x022b…d6407｜领取令牌（只显示这一次）

# ② 监听 + 对账 + 自动开通（监听目标从待付订单派生，不用手工填地址）
danmu-intel chain-watch --orders                          # 启动补扫 → 每 60 秒增量轮询 → 对账开通
danmu-intel chain-watch --orders --once                   # cron 友好：只跑一轮增量

# ③ 运营者视角
danmu-intel orders                                        # 订单：状态/金额/差额/地址/到期
danmu-intel members                                       # 会员：状态/到期；--sweep 执行到期降级

# ④ 漏检兜底（AC-5 后半段）：凭交易凭证人工补开通，必填理由，操作留痕且同样幂等
danmu-intel grant --order-ref DM1A2B3C4D --tx-ref 0x… --reason "用户提供了区块浏览器链接"

# ⑤ 公网接口（Funnel 转发到本进程；下单/领取/校验/付费正文）
danmu-intel serve --host 127.0.0.1 --port 8080
```

四个接口：

| 接口 | 作用 |
|---|---|
| `POST /api/orders` | 下单：档位 + 通讯账号 + 网络 → 收款要求（地址 / memo / 金额 / 到期）+ 领取令牌 |
| `POST /api/claim` | 领取凭据：账号 + 订单引用 + 领取令牌 → `Set-Cookie`（HttpOnly + Secure + SameSite=Lax） |
| `POST /api/verify` | 校验凭据 → 会员状态与有效期（有效期对用户可见） |
| `GET /api/report/<比赛>/<形态>/paid` | 凭据读取**付费正文**（比赛结束后自动转公开，无需凭据） |

纪律三条：**失败一律同一份响应**（含被限流，逐字节相同，AC-10）；**凭据只存哈希**、明文只在领取
那一刻出现一次；**领取必须同时持有领取令牌**（只在下单的那个浏览器里，链上 memo 用的是公开引用，
所以光知道账号领不走别人的会员 —— 理由与偏离说明见 ADR-0017）。

到期降级交给 cron（不引入调度器依赖；哪怕不跑，判定也按时间正确）：

```bash
*/10 * * * * danmu-intel members --sweep      # active → grace → expired（每次转换写审计）
* * * * *   danmu-intel chain-watch --orders --once
```

### 统计门槛怎么调（T4）

门槛（灰信号 N/M/K、终局信号阈值、边界复核门槛）都在 `config` 表，改动留审计：

```bash
danmu-intel config                                          # 看当前门槛
danmu-intel config --set gray_min_users=5 --actor 管理员     # 改门槛（写 config.update 审计）
```

默认值取自需求原文：灰信号命中 ≥5 次 / 独立发言者 ≥3 人 / 覆盖 ≥2 个时段（§6.5 第 4 条
「多人、多时段」）；终局判定 ≥3 类独立信号 + 2 分钟反转窗口、终结类弹幕持续 ≥2 分钟、
流量降至峰值一成以下 ≥5 分钟（§6.4）。

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
| **凭据**（`DEEPSEEK_API_KEY` 等） | `~/danmu-intel-data/.env`（**0600，权限不对就拒读**） | 否 |
| LLM 调用账本 | `db.sqlite3` 的 `llm_calls` 表（成本硬闸的数据源） | 否 |
| 链上监听游标 | `db.sqlite3` 的 `chain_cursors` 表（一地址一行：扫到哪了） | 否 |
| 订单与逐笔入账 | `db.sqlite3` 的 `orders` / `order_payments` 表（账本：谁、多少钱、哪笔交易） | 否 |
| 会员与凭据哈希 | `db.sqlite3` 的 `members` / `member_credentials` 表（**凭据只存 sha256**） | 否 |
| 档位价格与收款配置 | `db.sqlite3` 的 `config` 表的 `billing` 键（xpub / Solana 地址 / 价格 / 宽限期） | 否 |
| 供应商额度账本 | `db.sqlite3` 的 `quota_usage` 表（按供应商按日累加；报警阈值的唯一数据源） | 否 |
| 提示词模板 | `prompts/interpretation/<版本>/` | 是 |
| 站点产物 | `site/**`（整棵站点树 + `release.json`；暂存目录 `site/.staging/` 不进 git） | 是 |

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

解读层的测试缝在**调用点**（`ChatClient`）：假 LLM 提供正常 / 幻觉 / 超时 / 报错四种返回值
（`tests/unit/test_llm_interpreter.py`），端到端验收在 `tests/e2e/test_llm_interpretation.py`
（断网/报错/超时仍按时发布且标注降级、幻觉被拦下且页面里不出现编造的数字与名字、
成本闸触及即降级并报警、解读段里的数字都能溯源、凭据零泄漏到页面/库/仓库）。
凭据扫面还包含 `git log -p --all`：删掉的密钥也算泄漏（AC-12）。

发布闭环的测试缝在**产物树 + 注入的假 Vercel 客户端/假提交器**：
`tests/unit/test_publish_checks.py` 对 6 项需求检查各有一组「注入缺陷即拦截」的红绿用例，
`tests/unit/test_release.py` 覆盖原子替换、幂等、失败保留上一版、回滚（含账本对齐失败报警）、
结束转公开，端到端在 `tests/e2e/test_publish_loop.py`（CLI 全流程，全程不联网）。

会员付费的测试缝有三条：**密码学向量**（BIP-0032 官方向量、keccak 已知常量、参考实现算出的
xpub → 地址链、EIP-55 向量，`tests/unit/test_billing_xpub.py`；xprv 必被拒绝）、
**状态机与对账**（`tests/unit/test_billing_{members,orders,settle,verify}.py`：顺延/宽限/撤销、
派生索引只前进、金额尾数、领取令牌轮换、六种失败逐字节相同、限流窗口、重复与不足额）、
**HTTP 边界**（`tests/e2e/test_billing_loop.py`：真 aiohttp 服务 + 假链，下单 → 入账 → 自动开通 →
领取凭据 → 读付费正文；含「比赛结束自动转公开」与全库零凭据扫面）。

链上监听的测试缝在**注入的假供应商（假链 + 假时钟）**：`tests/unit/test_chain_polygonscan.py` /
`test_chain_helius.py` 验纯函数解析（原生/ERC20/SPL 代币、memo、失败交易跳过、翻页与上限、
限速与错误措辞、凭据不进异常消息），`tests/unit/test_chain_quota.py` 验游标单调性与额度窗口，
`tests/unit/test_chain_watcher.py` 验轮询/补扫/告警去重，端到端在 `tests/e2e/test_chain_watch.py`
（一轮轮询看见入账 → 限速那轮不静默且游标不动 → 恢复后补扫找回那一笔 → 账本逐次可对）。
全程不连外网，也没有任何真实密钥（测试里的 key 是拼接出来的假串，扫面工具自己也要能扫过）。

## 边界

- **会员付费永不触碰钱**（AC-12 / FR-C6-17..19）：系统只持有 xpub（watch-only）、派生地址、
  Solana 收款地址、memo 与订单账本；**没有任何签名能力**，也没有私钥、助记词、keystore、
  交易所 key。xprv 一出现就被扫描器拦下（`tools/check_no_secrets.py` 的「扩展私钥」模式 +
  `tests/unit/test_billing_xpub.py` 的「扩展私钥必被拒绝」用例）。
- **会员身份对外零泄露**（NFR-P-1/P-2）：用户名只用于联系与运营者核对，不进任何公开页面、
  统计与对外响应；校验接口的失败响应逐字节相同，且账号字段**不参与判定** —— 接口回答不了
  「某人是不是会员」。
- 公开弹幕是唯一数据来源，不使用任何需要突破访问限制的手段。
- 原始记录**不落明文身份**：只存平台用户 ID 的加盐哈希（`user_hash`）。
- 灰信号只作风险提示，不指控、不点名、必须附样本与门槛（需求 §6.5）：产出物结构上装不下
  身份字段，渲染层出口前逐一比对全部 `user_hash`（出现即抛错），样本非空是落库前置校验，
  且**不提供任何对外导出接口**。
- 切片边界的来源与冲突都留档：`slices.boundary_source` + `conflict_note`；人工修正进
  `audit_log`，且自动来源永不覆盖人工修正过的切片（需求 FR-C2-5）。
- **链上监听只看见、不动用**（AC-12 / FR-C6-17..19）：系统不持有私钥、助记词、keystore、
  交易所 API key，也没有任何签名能力；只存地址、交易标识、金额、memo 与游标。供应商 API key
  只从仓库外 `.env`（0600）读，不入库、不进 stdout/stderr 与异常消息（`pre-commit` 与测试
  双重扫面，`git log -p --all` 也算）。
- 解读层的纪律由**代码**兜底而不是提示词自觉：模型只看到事实层 JSON，输出受 JSON Schema
  约束，写完再由反幻觉校验逐项比对事实层；校验不过就丢弃重试，再不过就规则直出并标注。
  拦不住的部分（中文昵称、"指控性结论"）在本版是靠提示词纪律 + 人工抽检，见 ADR-0014。
