# ADR-0003：收款链限定 Polygon + Solana；Solana 用 reference 字段而非地址派生

- **状态**：已采纳
- **日期**：2026-09-21
- **决策者**：用户

## 背景

用户定稿：收款链**只要 Polygon 和 Solana，明确排除 Base**。

原始诉求是「两条链都用唯一地址，统一方案」。调研后发现这是**密码学硬约束**，不是设计偏好：

| 链 | 曲线 | 派生标准 | 能否从公钥派生子公钥 |
|---|---|---|---|
| Polygon（EVM） | secp256k1 | BIP-32/44 | **能**（支持 non-hardened）→ xpub 可派生 |
| Solana | ed25519 | SLIP-0010 | **不能**（仅支持 hardened）→ xpub 派生不可行 |

SLIP-0010 对 ed25519 只定义 hardened 派生（`m/44'/501'/0'/0'`），而 hardened 派生必须持有私钥。**Solana 无法做到「服务器只有公钥却还能派生新地址」。**

Solana 生态的官方替代是 **Solana Pay `reference` 字段**：一个 32 字节公钥，钱包在转账时把它作为**只读非签名账户**塞进转账指令；后台用 `getSignaturesForAddress(reference)` 反查即可定位这笔付款。reference 在用户付款前就已确定，零歧义、零解析失败。

## 决策

1. **收款链限定两条**：Polygon（USDC）+ Solana（USDC）。Base 不做。
2. **Polygon**：xpub BIP-44 派生**每用户唯一地址**（见 ADR-0002）。
3. **Solana**：**单一收款地址 + 每用户唯一 `reference`**，不做地址派生。
4. **用户视角统一，底层机制不同**：两条链对用户都是「选档位 → 拿专属收款信息 + 二维码 + 金额 → 转账 → 自动开通」。代码层抽象统一 `PaymentProcessor` 接口，底层各自实现。

## 后果

- **正向**：两条链都以 watch-only 方式运行，零私钥风险一致；用户体验一致，不需要教育差异。
- **负向**：Solana 侧需要一个 `reference → user_id` 映射表，且依赖用户钱包正确写入 reference（主流钱包与 Solana Pay 规范均支持，但需在付款页显式引导）。
- **约束**：入账检测实现必须分链 —— Polygon 用 `eth_getLogs` 扫 `Transfer` 事件；Solana 用 `getSignaturesForAddress(reference)`。抽象接口 `PaymentProcessor` 统一上层语义。
- **约束**：确认数分链 —— Polygon 3 个区块；Solana `finalized`。
