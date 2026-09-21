# ADR-0007：数据统一入库策略（分层：SQLite 为档案与统计权威，JSONL 只留原始层）

- **状态**：已采纳
- **日期**：2026-09-22
- **决策者**：用户

## 背景

用户 2026-09-22 提问：

> 「扫描还有哪些数据是以 json 或 jsonl 来保存数据的？能否统一存入数据库」

**实测取证**（命令可复现，见设计文档 §6.4）：

```bash
# ① 仓库内实存文件
find . \( -name '*.json' -o -name '*.jsonl' \) -not -path './.git/*' -not -path './vendor/*' | wc -l
#   → 217 个
# ② 代码与文档中引用的路径（含未入库的运行时产物）
#   grep 于 tools/*.py + knowledge/*.md + docs/*.md → 277 处引用
# ③ 蓝本是否有数据库？
grep -rlni 'sqlite3\|psycopg\|SQLAlchemy\|CREATE TABLE' tools/ src/ 2>/dev/null | wc -l
#   → 0（蓝本全程无数据库）
```

结论：**蓝本零数据库，全部数据以 JSONL + JSON 散落在文件系统上**，且存在
「同一事实在多份 JSON 中重复定义」的漂移风险（例：某联赛的默认采集集在多处出现）。

## 决策

**统一入 SQLite，但采用分层策略 —— 不是「全部塞进一个库」。**

### 1. 分层归属（铁律）

| 层 | 载体 | 内容 | 理由 |
|---|---|---|---|
| 原始证据 | **JSONL 文件（保留）** | 原始弹幕 `data/danmu/<platform>/<date>_<source>.jsonl`<br>切片 `data/danmu/slices/<match>/{all,game_N}.jsonl` | 只增不改、一次采集机会、行级隔离（坏行不毁全量）、可 grep/diff、与落盘目录对齐 |
| 结构化档案 | **SQLite** | `matches`/`teams`/`players`/`gray`/`bp`/`leagues`/`knowledge`/`assets` | 需按 id upsert、需跨场聚合查询、需 schema 契约 |
| 状态幂等 | **SQLite** | 原 `runtime/events/`、`runtime/vps_intel/` 语义 → `job_state` 表 | 需原子判重、需并发安全 |
| 统计与商业 | **SQLite** | `site_events`/`leads`/`price_snapshots`/`payments`/`subscriptions`/`payment_addresses`/`sources`/`users` | 需聚合、需索引、需事务 |
| 无 schema 散 JSON | **SQLite** | 统一进 `intel_asset(kind, id, doc, updated_at)` | 消灭散落文件，同时不强迫立即建模 |
| 配置 | **文件（不入库）** | `config/*.yaml`（`sources` 表除外） | 人工编辑 + git diff 是审计能力，入库反而失去 review |
| JSON Schema | **文件（不入库）** | `schemas/*.schema.json` | 属代码资产，不是数据 |
| 临时产物 | **丢弃** | `/tmp/*.json`、`resp.json`、`r.json` | 一次性产物，`.gitignore` 覆盖 |

### 2. DB 是派生视图，不是 ground truth

**原始 JSONL + 配置永远是可重放的真源；删掉 DB 只降速、不丢数据。**
必须提供 `rebuild-db` 命令与配套测试（删库后全量重建成功、行数与关键字段一致）。

### 3. 一表一实体，无 schema 者进 `intel_asset`

有稳定字段的实体（match/team/player/league…）一表一实体、显式列；
字段不固定或仍在演进的散 JSON 进 `intel_asset(kind, id, doc JSON, updated_at)`，
`kind` 取值即原文件名语义（如 `bp_signals`、`streamer_profiles`、`node_data`）。

### 4. 方案对比（为何不选另两条路）

| 方案 | 做法 | 判定 |
|---|---|---|
| A 全量入库 | 原始弹幕也进 DB | ❌ 丢失行级隔离与 grep/diff；DB 膨胀使备份/迁移变重；append-only 语义被事务包裹反而更重 |
| **B 分层统一** | ✅ **采纳**：档案/状态/统计全入库；原始层留 JSONL | 统一约 90% 的散落 JSON，同时保留原始证据的不可变与可审计性 |
| C 只入统计 | 仅统计层入库 | ❌ 没解决用户提出的「散落 JSON」问题 |

## 后果

- 仓库内除「原始弹幕 / 切片 / 配置 / Schema」外**零散落 JSON**。
- 单文件 SQLite（`runtime/danmu.db`）零运维、易备份、易搬运，符合 AGENTS.md「最简实现」。
- 配置收敛为单一来源（`settings.yaml` + `sources` 表），消除「同一联赛采集集多处定义」
  的漂移风险（对应 §5.2.5）。
- 引入 schema 演进成本 → 由 `rebuild-db` + 迁移脚本承担，且因「DB 可重建」而风险可控。
- 原始弹幕体量增长不影响 DB 大小（分层带来的直接收益）。

## 验证

- [ ] 除「原始弹幕 / 切片 / 配置 / Schema」外，仓库内 `find . -name '*.json'` 仅剩允许清单
- [ ] `intel_asset(kind, id, doc, updated_at)` 表存在，可容纳无 schema 散 JSON，有测试
- [ ] `rebuild-db` 脚本存在：删库 → 从 JSONL + JSON 全量重建 → 行数与关键字段一致（含测试）
- [ ] 状态幂等语义由 `job_state` 表承载，重复执行不产生重复副作用（含测试）
- [ ] 临时产物不入库，`.gitignore` 覆盖 `/tmp` 类产物名
- [ ] §6.4 数据资产清单与实际文件一一对应（清单过期视为文档缺陷）
