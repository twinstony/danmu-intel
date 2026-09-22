# ADR-0011 采集监督与异常事件出口

## 状态：已接受

## 上下文

需求 FR-C1-1/5/7 要求同场多直播间并发采集、断流重连不静默、运行状态对管理员可见；
NFR-A-4 要求进程异常退出后自动拉起；设计 §7.4 定「一房间一子进程」，§7.3 定
`connecting → running → stalled / no_stream` 的状态机，§7.5 定「异常要响」。
Ticket #5（T2）要把这套机制落地，且异常事件最终由 T11 的通知通道投递。

落地时必须回答几个设计文档没写到接口级的问题：心跳放在哪、谁写会话行的哪几列、
子进程与主进程怎么分工、异常往哪落、去重键里的 `msg_hash` 从哪来。

## 决策

1. **两级恢复，各管一层**：
   - **流层**（子进程内，`collect/adapter.py`）：连续 60 秒没有消息 → 主动放弃这条连接
     重连，累计 `reconnects`，状态标 `stalled`；断流抛错同样重连（退避 1s→60s）。
   - **进程层**（主进程，`collect/supervisor.py`）：进程退出 → 按 1s→2s→…→60s 退避重拉；
     心跳老化超过 15 秒 → 判定僵死，杀掉重拉。**子进程不自杀，父进程不干预正常流。**
2. **一房间一子进程**，主进程只做调度（不碰网络）；同一房间 30 分钟内重启超过 5 次
   即停止重试，**停止原因写进库**（`restart_exceeded` 事件的 payload），并以非零退出码
   结束监督进程（不静默）。
3. **心跳契约**：子进程每 5 秒原子覆盖写 `<data>/runtime/heartbeat/<platform>-<room_id>.json`
   （临时文件 + `os.replace`），同时更新自己 `room_sessions` 行的
   `state / last_msg_at / reconnects / severity`。心跳带 `pid`（识破上一轮的残留文件）
   与 `written_at`（算老化）；**首次心跳宽限 30 秒**（房间探测最多耗 15 秒 HTTP 超时）。
4. **会话行按「谁有能力写」分工**：子进程活着时写自己的行；它死掉或被 SIGKILL 时由
   supervisor 收尾（补 `ended_at`、`state='exited'`、严重级别只升不降），幂等。
5. **监督接力棒走环境变量** `DANMU_INTEL_SUPERVISION`（`restart_count` / `reconnects`），
   子进程据此写自己的会话行——重启次数由重拉者记账，跨进程不靠猜。
6. **异常事件出口是 `notifications(state='pending')`**（设计 §5.1 既有的表），payload 带
   `match_id / platform / room_id`。六类：`process_exit` / `process_hung` /
   `restart_exceeded` / `no_stream` / `stalled` / `disk_low`。**投递与抑制归 T11**
   （`alerts` 表），本层只保证「异常一定留下可查的行」。
7. **去重键 `(platform, room_id, msg_hash)`**：`msg_hash` 取落盘记录的稳定字段
   `ts|user_hash|text`（平台不提供全局消息 id）。键里带 `room_id` ⇒ **不同房间的同文
   弹幕各算一条**（两份独立证据），同一房间内重复落盘的记录只算一条。
8. **子进程被 kill 时来不及封存的文件由 supervisor 补封**（按心跳里的最后一条消息时刻
   推出落盘路径）：证据不许因为进程被杀而漏出索引。
9. **轮询等待取小值**：`min(5 秒, 最近一个待拉起房间的退避到点时刻)`——固定睡满 5 秒会把
   「退避 1 秒」拖成 6 秒以上，`kill` 后 10 秒内拉起就没有保证。

## 后果

- ✅ FR-C1-1/5/7 与 AC-15 可测：三房间并发、每房间贡献量（条数/跨度/去重后条数）、
  kill 后 10 秒内拉起、断流重连计数、重启超限原因可查，都有测试与 CLI 出口。
- ✅ 父子共用一个 SQLite（WAL + busy timeout）：写入都是小事务，3 房间实测无争用问题。
- ✅ 新增平台（T3）不需要动这一层：适配器只需把 `on_reconnect` 转交给 `reconnecting`。
- ⚠ supervisor 自身没有心跳（挂了靠 systemd/PM2 的 `Restart=always` 兜底）。
- ⚠ 重启/重连计数的历史只保留在会话行与事件 payload 里，没有独立的指标表；若将来要画
  趋势图需另加聚合（T10 站点统计之前不必要）。
- ⚠ 「收尾判定用比赛状态机」（§7.3 的 `ended`）仍属后续票：本层靠 `--seconds` 或人工
  停止监督进程。
