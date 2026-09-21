# ADR-0002：链上收款采用「唯一充值地址 + watch-only」，服务器不持有私钥

- **状态**：已采纳
- **日期**：2026-09-21
- **决策者**：用户

## 背景

蓝本付费闭环为零：订阅走「登记表单 → 推站长 TG」，会员名单 `members.json` 手工维护。PRD §7.9 只有 4 行，但 §16 验收第 8 条要求「订阅链路：登记 → 名单 → 解锁 → 到期全自动」——**验收标准与设计打架**。

需要选一条自动化的链上收款路线。候选三种：

| 方案 | 机制 | 代价 |
|---|---|---|
| A｜唯一充值地址 | 每用户每链一个专属地址，轮询入账自动开通 | 无私钥风险、仅 gas、复杂度低 |
| B｜自建合约 | approve + 定时扣款 / 流支付 | 需合约权限 + 审计，复杂度高 |
| C｜托管网关 | Coinbase Commerce / Stripe USDC 等 | 1%–1.5% 手续费，依赖第三方 |

关键调研发现：**收款根本不需要私钥**。使用扩展公钥（xpub）即可派生出无限子地址，系统只做「监控入账」，资产直达冷钱包。这与 PRD §12.4「不持有私钥」完全一致。

行业事实佐证（BTCPay Server 官方 FAQ）：

> creating a new invoice with a **unique address**… **Addresses are never reused**

交易所钱包实践同样明确：`unique deposit address per user, per chain`、`Persist the derivation path. Never derive inside a web request. Put allocation behind an idempotent worker`；并明确否掉「共享地址 + memo」方案（理由：`No support agent tracing a deposit by hand`）。

## 决策

**采用方案 A｜唯一充值地址 + watch-only**（v1），架构预留 B。

- 服务器**只持有 xpub（扩展公钥）**，不持有私钥、不持有助记词。
- 地址派生：EVM 走 BIP-44 `m/44'/60'/0'/0/{index}`，持久化 index。
- 派生必须**异步 + 幂等**，**绝不在 web 请求内派生**。
- 入账检测用轮询（EVM `eth_getLogs` 扫 ERC-20 `Transfer` 事件），不引入 indexer 依赖。
- 幂等唯一键：`(chain, tx_hash, log_index)`。

## 后果

- **正向**：安全等级最高 —— 系统被完全攻破，攻击者也无法转走资金；平台零手续费（仅 gas，由付款方承担）；无第三方依赖与托管风险。
- **负向**：无原生自动续费能力（见 ADR-0003 背景中的 Pull Payment Problem），到期续费依赖「提醒 + 手动付款」。
- **约束**：xpub 与助记词必须物理隔离 —— 助记词离线备份、永不触网，服务器只配 xpub。
- **约束**：每笔入账必须落库（user / chain / tx_hash / amount / 开通时长），可回溯审计。
