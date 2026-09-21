# 收款钱包信息清单（你提供，我填）

> 本文档列出**你需要准备/提供**的具体字段。拿到后我填入配置并移除占位符。

## 说明

- **助记词你保管**，我只需要**扩展公钥（xpub）** 和 **Solana 收款地址** 两样。
- 助记词**永不触网**，不入库、不提交、不进入任何配置文件。

---

## 需要你提供的字段

| # | 字段 | 链 | 格式 | 说明 | 示例（Mock） |
|---|---|---|---|---|---|
| 1 | **xpub（扩展公钥）** | Polygon | 字符串 | 由助记词派生出的根扩展公钥（BIP-44 `m/44'/60'/0'`） | `xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKrhko4egpiMZbpiaQL2jkwSB1icqYh2cfDfVxdx4df189oLKnC5fSwqPiCSQQzmNiYmak17Sg4fcQ` |
| 2 | **Solana 收款地址** | Solana | Base58 字符串 | 单个钱包地址（所有 Solana 用户都转这个地址，靠 `reference` 区分付款人） | `7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV` |

---

## 如何导出 xpub

如果你用的是兼容 BIP-44 的钱包（MetaMask / Ledger / Trezor / Trust Wallet / Phantom 等）：

1. 从你的**新收款钱包**助记词导入软件钱包
2. 进入「账户详情」→「导出账户公钥」或「导出 xpub」
3. 复制公钥字符串（以 `xpub` 开头）
4. 发给我

**注意**：不要发私钥、不要发助记词、不要发 keystore。只发**公钥（xpub）**。

---

## 安全边界

| 我拿到后 | 我不做 |
|---|---|
| ✅ 填入 `config/payment.yaml` 的 `evm_xpub` 字段 | ❌ 从不复制到其他地方 |
| ✅ 写入服务器环境变量 `DANMU_INTEL_XPU` | ❌ 从不提交到 git |
| ✅ 用于派生充值地址（只读） | ❌ 从不尝试反向推导私钥 |

---

## 配置现状

```yaml
# config/payment.yaml（当前占位）
wallet:
  evm_xpub: "${DANMU_INTEL_XPU}"        # ← 填入你的 xpub
  solana_address: "${DANMU_INTEL_SOL_ADDRESS}"  # ← 填入你的 Solana 地址
```

填入后占位符消失，变为实际公钥（仅限配置，**不入库**）。
