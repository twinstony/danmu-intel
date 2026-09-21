# ADR-0005：钱包保管、RPC 节点、马斯克打包、早鸟窗口

- **状态**：已采纳
- **日期**：2026-09-21
- **决策者**：用户

## 背景

在 ADR-0002（唯一充值地址 + watch-only）之上，仍有 4 项决策需要用户拍板才能冻结支付模块的设计。4 项互相独立，合并一条 ADR。

## 决策

### 1. 早鸟窗口：先忽略

原定「限 20 名，2026-09-15 截止」（PRD 原档 + `config/payment.yaml` 注释）。今天已是 2026-09-21，窗口已过期。**暂不定义新窗口**，上线时早鸟档位默认隐藏（代码里早鸟开关 `early_bird.enabled: false`），等用户后续启用时再填 `deadline`。

### 2. 收款钱包：用户保管助记词

| 项 | 处置 |
|---|---|
| 助记词 | **用户自己保管**。不入库、不提交、不进入任何配置或日志 |
| 服务器只配 | **xpub（扩展公钥）**+ **Solana 收款地址** 两样 |
| 资产流向 | 充值地址收到的 USDC 留在该地址（watch-only）；用户可定期从地址池提现到冷/热钱包 |
| 提供方式 | 用户按 `docs/payment/wallet-info-checklist.md` 导出 xpub + Solana 地址，填入 `config/payment.yaml` 对应字段 |

### 3. RPC 节点：v1 用公共免费节点

| 链 | 端点 | 说明 |
|---|---|---|
| Polygon | `https://polygon-rpc.com` | 官方公共，免费无 API key，实测延迟 30-40ms |
| Solana | `https://api.mainnet-beta.solana.com` | 社区公共端点。Solana 官方标注"实验用"，但 v1 流量下够用 |

配置中以环境变量注入（`DANMU_INTEL_POLYGON_RPC` / `DANMU_INTEL_SOLANA_RPC`），运行时可覆盖为付费节点。

### 4. 马斯克打包方案：v1 不做

PRD 中有「马斯克订阅用户加购电竞情报 $29/月」打包定价。**v1 专注单卖，不做打包**。产品形态聚焦「单一订阅 = 电竞情报全线解锁」；预留配置位，后续扩展不重构。

## 后果

- 早鸟档位代码默认 `enabled: false`，UI 上不展示早鸟选项，不影响正常定价梯度。
- 助记词不触网 → 即使服务器被完全入侵，攻击者也无法获得助记词。唯一风险是 xpub 泄露导致「地址可被观察」，但不可花费。
- Solana 公共 RPC 在极端流量下可能限流 → 运行时配置切换为付费端点（QuickNode/Helius 等），无需代码改动。
- 马斯克打包不做 → v1 产品边界清晰，上市更快。

## 验证

- [ ] `config/payment.yaml` 中 `early_bird.enabled: false`
- [ ] `config/payment.yaml` 中 USDC 地址与 MEXC/Circle 官方数据一致
- [ ] `config/payment.yaml` 中 RPC 端点为上述公共节点
- [ ] 用户提供的 xpub 只进入服务器环境变量，**不入库**
