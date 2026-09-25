"""会员付费全自助闭环（T9；设计 §12、ADR-0004/0005/0006/0017）。

从「打开订阅页」到「拿到访问权」全程无人工（AC-3），模块划分：

| 模块 | 管什么 |
|---|---|
| `pricing` | 档位、价格、宽限期、订单时效、收款配置（`config` 表的 `billing` 键） |
| `members` | 会员身份（既有通讯账号标识）与状态机（pending/active/grace/expired/revoked） |
| `crypto` | keccak256 + secp256k1 点运算 + base58check —— **只有公开运算，没有签名能力** |
| `xpub` | 从 xpub 按 BIP44 `m/44'/60'/0'/0/i` 派生 watch-only 收款地址（索引只前进） |
| `orders` | 订单状态机（pending/short/paid/expired）与收款要求（地址 / memo / 金额尾数） |
| `verify` | 凭据发放与校验（只存哈希、防枚举、限流） |
| `settle` | 链上入账 → 订单匹配 → 幂等开通（AC-3/4/5/7） |

对外 HTTP 面（下单 / 领取 / 校验 / 付费正文 / 统计上报）在 `danmu_intel/api.py`：
它横跨收款与统计两个领域，因此不挂在任何一个领域包下面。三条贯穿全包的硬规矩：

1. **手上没有任何可动用资产的凭据**：只有 xpub、派生地址、Solana 收款地址与 memo；
   没有私钥、助记词、keystore、交易所 key（FR-C6-17..19 / AC-12）。
2. **凭据（会员访问凭证）只存哈希**，明文只在发放的那一刻出现一次（ADR-0006）。
3. **钱不用浮点算**：金额一律是最小单位整数（`units`），与 `chain.transfer.Transfer` 同口径。
"""
