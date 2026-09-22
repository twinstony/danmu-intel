# ADR-0005 链上数据监听

## 状态：已接受

## 上下文

需求 FR-C6-11：额度受限时报警；需求 NFR-C-2：成本随场次线性增长、不随历史累积。

## 决策

| 链 | 数据源 | 免费额度（实测） | 月用量 | 补扫方式 |
|---|---|---|---|---|
| Polygon | Polygonscan API | 5 calls/s、10 万 calls/天、1000 条/次 | ~4.3 万次/月 | 按地址查 txlist + 区块范围 |
| Solana | Helius | 1M credits/月、10 req/s | ~8.6 万次/月 | `getSignaturesForAddress` 翻页 |

- 每次调用记 `quota_usage`。
- **用量 >80%** 或 **收到限速响应** → 报警（FR-C6-11）。
- `chain_cursors` 记录每个 scope 的最后游标（polygon: 最后扫描区块；solana: 最后签名）。
- **双重路径兜底**：watcher 日常用游标增量；启动/异常恢复/手动补扫走"按地址查历史"（不依赖游标）。

## 后果

- ✅ 免费额度已覆盖实测用量，无预期支出。
- ✅ 双路径确保"即使游标损坏/跳跃，补扫仍能发现所有付款"。
- ⚠ Polygonscan 的免费 API 有 5 calls/s 突发限制，需令牌桶限速。
- ⚠ Helius 免费档 10 req/s，Solana 查询较重（`getSignaturesForAddress` 每次 1 credit），令牌桶单独配置。
