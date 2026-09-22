# ADR-0002 存储方案

## 状态：已接受

## 上下文

需求：会员数据、订单账本、切片/统计/报告、审计日志都需要持久化；原始弹幕在线保留 6 个月。需求写明"单机 + 不做备份恢复演练"。

## 决策

- **数据库**：SQLite（stdlib `sqlite3`，WAL 模式）。零外部进程、单机友好、事务 + 并发读支持。
- **原始弹幕落盘**：仓库外 `~/danmu-intel-data/raw/<platform>/<yyyy-mm-dd>/<room_id>-<hh>.jsonl`（append-only）。
- **6 个月后归档**：压缩为 `.jsonl.zst` 迁至 NAS（`smb://ocean.local/...`），DB 索引行保留 rel_path 改指向归档挂载点。
- **git 边界**：代码 + 站点产物进 git；`~/danmu-intel-data/` 不进（`.gitignore` + 绝对路径）；报告与订单是账本，保留长期在线。
- **不实现**：不实现备份工具、恢复演练（NFR-A-5）。

## 后果

- ✅ SQLite stdlib 不引入新依赖；WAL 支持多读者。
- ✅ JSONL append-only 天然满足"不丢证据"。
- ✅ 仓库保持小（蓝图蓝本 ~400K 文件全部在仓库外）。
- ⚠ 单写者假设（asyncio 单线程 + 串行写）——未来并发升高时需评估。
- ⚠ 浮点数值入库统一保留 6 位小数 + 四舍五入，避免"重算不一致"。
