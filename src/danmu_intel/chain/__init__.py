"""链上监听层（ADR-0005）：Polygonscan + Helius 两条链的入账监听。

- `transfer.py`：一条**入账事实**（`Transfer`）——两条链统一的形状
- `cursor.py`：监听游标（`chain_cursors`），断点续扫与补扫的依据
- `quota.py`：供应商额度记账（`quota_usage`）+ 阈值判定 + 最小间隔限速
- `alerts.py`：链上异常的报警出口（额度告警 / 限速 / 拉取失败）
- `polygonscan.py` / `helius.py`：两个供应商客户端（传输层可注入 → 断网可跑）
- `watcher.py`：60s 轮询 + 启动补扫 + 双重路径兜底

监听层只**看见**入账，不动用任何资产：没有私钥、没有助记词、没有签名能力
（ADR-0004 / FR-C6-17..19 / AC-12）。
"""
