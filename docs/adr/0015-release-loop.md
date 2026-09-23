# ADR-0015 发布闭环：站点树、7 项检查、原子发布、秒级回滚与状态机驱动的可见性

## 状态：已接受

## 上下文

需求 FR-C5-1..11 / §6.7 / §6.8 / §6.10、AC-2、AC-8、NFR-A-1、设计 §11 定了发布层要什么：
静态产物走 Vercel、付费段不进静态文件、6 项发布前检查（逐项纯函数）、原子发布、
秒级回滚、比赛结束自动转公开。设计没写到接口级、必须在实现时定死的有：

- 站点产物的**清单与路径**（哪些页面、放在哪）；
- 检查的**输入形态**（检查是「产物树 + 比赛状态 → 结论」的纯函数，那产物树是什么）；
- **「付费段」的边界**：哪一段算付费、页面上留下什么；
- 「原子替换」在**同一个仓库目录内**到底怎么做（`os.replace` 换目录在目标非空时不可行）；
- 发布批次的**账本与版本标识**（回滚后要能指出「线上是哪一版」），以及发布的**幂等**判定；
- 回滚的**两步**（Vercel 即时回滚 + git revert 对齐账本）谁先谁后、失败怎么办；
- 「结束转公开」的**触发点**（谁在什么时刻再发布）；
- Vercel 客户端怎么注入（断网可跑），凭据放哪；
- 画像库里的**选手页**、以及**校验闭环页**的数据从哪来（现有模型里没有预测台账）。

## 决策

1. **站点产物是一棵「站点树」（`SiteTree`）**：页面是纯数据（`path` + 标题 + HTML +
   导航项 + 关联标识 + 可见性 + 报告项），树是这次发布的完整产物。清单：
   `index.html`、`history/index.html`、`leagues/<league>.html`、`matches/<id>/index.html`
   （比赛页）、`matches/<id>/<kind>.html`（报告页，路径沿用 T5）、`profile/index.html` +
   `profile/teams/<slug>.html` + `profile/players/<slug>.html`（画像库）、`gray/index.html`、
   `verification/index.html`、`subscribe.html`、`release.json`（版本标识）。
   导航是固定栏目（首页 / 历史情报库 / 画像库 / 灰信号 / 验证闭环 / 订阅），
   联赛页与比赛页靠索引、历史库、画像库与比赛页互链到达。

2. **发布检查是 7 个纯函数**（`SiteBuild → CheckResult`）：需求 §6.8 的 6 项 + 1 项加固
   （报告来源引用可达，含 SHA256）。第 4 项「报告分段完整」**复用报告层的段集与解读段
   检查**（`report/publish.py` 的同一断言不写第二遍）：产物里的报告内容直接取自
   `reports` 账本，检查对象是账本里的 `content_json`，而不是页面 HTML —— 付费报告页
   按设计**本来就没有**段正文，拿 HTML 数段会得出错误结论。

3. **付费边界只由比赛状态机驱动**（ADR-0009），且是**报告级**的：
   `visibility(state) = public if state == ended else paid`。付费时报告页
   **不写任何段正文**（事实段与解读段都不写），只留标题、元信息、段目与付费说明；
   判定与文件名、路径、报告形态、发布日期**全部无关**。付费正文只经
   `paid_content(content, match_state, credential_verified)` 返回：比赛已结束（所有人可见）
   或凭据校验通过（会员可见）二者之一；其余情况抛 `PaidAccessDenied`（HTTP 层属 T9）。

4. **原子发布 = staging → 检查 → 逐条目 `os.replace` 交换 → 最后写 `release.json`**。
   同分区 `os.replace` 对**单个条目**是瞬时的；目标目录非空时换不了目录，因此不换目录，
   换条目：先在 `site/.staging/` 里生成整棵树，检查全过后按「目录 → 文件 →
   `release.json`」的顺序逐个原子替换，并把线上多出来的条目删掉。`release.json` 最后落，
   它因此是「这一批已完整上线」的标记（版本标识：`version` + `git_ref` + `deployment_id`）。
   任一检查不通过 → **不交换任何条目**，线上保持上一版可用（AC-8）。

5. **发布批次账本 `releases`**（只增不改，像 `reports` 一样）：`version`（递增，即版本
   标识）、`tree_digest`（站点树指纹）、`state`（`live` / `superseded` / `rolled_back` /
   `failed`）、`deployment_id`、`deploy_ref`（git 提交）、`paywalled_matches`（本批付费的
   比赛）、`checks_json`、`payload_json`（页面清单，供回滚与排查）。**幂等**：
   新树指纹与上一批 `live` 相同 → 直接返回「无变化」，不重复提交、不重复部署。

6. **回滚两步，顺序不可换**：① 调 Vercel instant rollback 到目标批次的
   `deployment_id`（秒级，线上立刻回到上一版）；② `git revert` 掉**当前坏版本**的提交，
   让「仓库 = 线上」的账本重新一致。② 失败不回滚①（线上已经好了），只写一条
   `notifications(pending)` 报警。两步都写 `audit_log`。`releases` 的 live 指针跟着换。

7. **结束转公开由状态机派生 + 幂等再发布**：可见性只由 `matches.state` 派生，因此
   「比赛转 ended 后所有页面自动转公开」不需要任何按页面的手工动作。
   `sync_ended()` 检查上一批 live 发布里**曾经付费**的比赛：一旦其状态转为 `ended`
   就自动再发布公开版；没有需要翻转的比赛就**什么都不做**（幂等，不产生新批次）。
   状态写入是唯一的人工动作；`match set-state` 写状态后立即调用它。

8. **Vercel 客户端是注入缝，凭据只在仓库外 `.env`**（`VERCEL_TOKEN` / `VERCEL_PROJECT_ID`，
   0600，绝不入 git —— AC-12 / NFR-S-4）。`VercelClient` 协议只有两个动作：
   `latest(project)`、`rollback(project, deployment_id)`；HTTP 实现走 stdlib
   `urllib.request`（不新增依赖），测试注入假客户端，因此**断网可跑**（AC-14）。
   缺凭据时 `publish --no-deploy` 走本地发布：只在 `site/` 做原子交换 + 记账，
   不推 git、不调 API。

9. **画像库的数据来源**：队伍页由 `matches` 汇总（场次、战绩、联赛）；选手页只在官方数据
   带阵容时生成（`official_result.lineups`，`common/official.py` 的契约扩展）。**没有数据
   就不造页**：原始弹幕只存加盐用户哈希（不落明文身份），因此不从弹幕里发明「选手」。

10. **校验闭环页只做「可复核闭环」**：逐场列出已发布报告、事实层哈希、官方结果与
    **当前重新复核来源的结果**（文件 + 行范围 + SHA256），并如实写明「没有留痕的预测
    不参与对错统计」。**不建预测台账**：段 7「预测验证」的事实正文目前只声明纪律，
    系统里没有任何「预测」的产出方，凭空造一张台账就是造事实。台账留给提出预测产出的票据。

11. **`site/` 产物进 git（设计 §6）**，`.gitignore` 只忽略 `site/.staging/`。产物所有者是
    发布器：`report publish` 写的单页是它的中间态，发布器按同一渲染函数（同一可见性判定）
    重出整棵树，两者不会给出不同答案。

## 后果

- ✅ AC-8 可测：任何一项检查不通过 → 一个条目都不换、`release.json` 不动、站点仍是上一版。
- ✅ AC-2 可测：`state == live` 时页面里没有付费正文（`curl` 拿不到），转 `ended` 后
  `sync_ended()` 自动再发布为公开版，且再跑一次不再发布（幂等）。
- ✅ 回滚有可验证的版本标识：Vercel 当前部署 == 目标批次、`releases` 的 live 指针 == 目标
  批次、`site/release.json` 回到上一版（由 git revert 跟进）。
- ✅ 付费边界与文件名彻底解耦：`paywall.visibility()` 只接受状态，测试逐条守住这一点。
- ⚠ 逐条目替换不是「整棵树一次换完」，理论上有极短的中间态（本地工作目录，非线上：
  线上是 Vercel 构建）。需求 FR-C5-7 关心的「页面与入口不一致」由「同一批树、同一批
  替换、`release.json` 最后落」覆盖；真要做整树原子切换，得把产物放进单独目录再换
  `site` 本身，收益不值这个复杂度。
- ⚠ 选手页依赖官方阵容（设计 §20 O8 未定的数据源）：没有阵容数据时画像库只有队伍页，
  这是如实反映，不是缺功能。
- ⚠ 预测台账缺席使校验闭环页只能公开「可复核性」而非「预测对错」；这是有意的范围边界，
  等有预测产出方时另开票据（届时补 `predictions` 表与对照逻辑）。
