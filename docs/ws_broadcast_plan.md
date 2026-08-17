# WS 广播模式改造方案（单发布者 + 每连接队列）

状态：已实施（2026-08-13）。`app/stream_hub.py` + `app/main.py` 薄壳改造完成，
`WATCH_STREAM_BROADCASTER=0` 可整体回退旧实现；测试见 `tests/test_stream_hub.py`。
日期：2026-08-13
前置：service 层共享载荷缓存已上线（`_cached_payload` + `_payload_cache_ttl`，盘中 1s TTL + 按视图单飞）

## 1. 背景与目标

当前 `/ws/stream` 是「每连接一个轮询循环」：每个 WS 连接各自
`asyncio.sleep(interval)` → `service.terminal(...)` → `model_dump` → delta 计算 → 发送。

service 层缓存已经把**数据构建**收敛为全局一次，但每个连接仍保留三项 per-client 开销：

1. `model_copy(deep=True)` + `model_dump(mode="json")`：每连接每 tick 对整份载荷做一次深拷贝和序列化（盘中 1s 一次，载荷约 1–3 MB 量级）；
2. JSON 编码：同一份 delta/snapshot 被重复 `json.dumps` N 次；
3. delta 签名校验：`_sig()` 对未变化分区反复做 `json.dumps(sort_keys=True)`。

连接数几十时无所谓；目标场景（对外部署、几百到上千并发）下这三项是纯浪费的线性 CPU。

**目标**：同一「频道」（sector/sort/page/page_size/board_level/fast/view 参数组合）只跑一个发布者循环，构建、序列化、delta 计算全局一次，每连接只剩「从队列取字符串 → send」。

**非目标**：不改数据口径、不改 WS 协议格式（snapshot/delta 消息结构保持现状，前端零改动）、不动 service 层缓存。

## 2. 总体设计

新增 `app/stream_hub.py`，核心是两个类：

```
StreamHub
  └─ channels: dict[channel_key, StreamChannel]     # channel_key = 规范化参数元组

StreamChannel（每个参数组合一个）
  ├─ publisher task        # 唯一的构建/序列化循环
  ├─ subscribers: set[Subscription]   # 每连接一个
  ├─ latest_payload        # 最近一次构建的完整 dict（供新订阅者做快照）
  ├─ tracker: TerminalDeltaTracker   # 频道级 delta 状态机（对全体订阅者共享）
  └─ last_json: (payload_key, str)   # legacy 全量模式共享的序列化结果
```

### 2.1 发布者循环（每频道一个）

```
while 有订阅者:
    payload = await to_thread(service.terminal, **params)   # service 层已共享缓存
    dumped  = payload.model_dump(mode="json")               # 每 tick 全局一次
    message = channel.tracker.next_message(dumped)          # delta 计算全局一次
    if message is not None:
        text = json.dumps(message)                          # 编码全局一次
        channel.broadcast(text)                             # put_nowait 到各订阅者队列
    channel.latest_payload = dumped
    await asyncio.sleep(live/static interval)
```

要点：

- **频道级 delta 状态机可行**，因为 delta 是严格顺序的：所有订阅者从同一个快照出发、按序应用同一串 delta，等价于现在每连接各自维护 tracker 的效果。
- 新订阅者加入时，在频道锁内完成两件事：拷贝 `latest_payload` 作为其个人 snapshot 立即下发；注册队列。此后发布者产生的每一条 delta 都能干净地应用到这份快照上（delta 是相对上一份载荷计算的，而快照就是那一刻的载荷）。
- 频道的第一个订阅者触发创建，最后一个订阅者断开后经一个宽限期（如 30s）回收频道，避免参数抖动导致频道频繁重建。

### 2.2 订阅者与背压

```
class Subscription:
    queue: asyncio.Queue[str | Resync]   # maxsize 例如 8
```

- WS 端点变成薄壳：解析参数 → `hub.subscribe(...)` 拿到 snapshot + queue → 循环 `queue.get()` → `websocket.send_text()`。
- **慢客户端背压**：`put_nowait` 失败（队列满）时不阻塞发布者，给该订阅者投入 `Resync` 哨兵并清空其队列；消费端遇到 Resync 就从频道重新拉最新完整快照重发（协议上就是一条 `type=snapshot` 消息，前端已有处理逻辑）。慢客户端最坏退化为「每秒收一条全量快照」，不会拖垮频道其他人。
- 发布者端不再需要 `payload_key` 签名比较（tracker 已承担变更检测）；legacy 非 delta 模式（`view=terminal` 无 `format=delta`，及 `view=legacy`）按 `last_json` 缓存共享全量文本，只有内容变化才重新编码。

### 2.3 与 main.py 的衔接

- `/ws/stream` 保留全部现有查询参数与默认值，`channel_key` 由规范化后的参数元组构成（注意 `page_size` 规范化逻辑与 service 一致：20–240 夹取等，避免同一视图裂成两个频道）。
- 断连（`WebSocketDisconnect` / 发送异常）→ `hub.unsubscribe()`，从频道移除并触发回收检查。
- 保留一条逃生通道：环境变量关闭时回退到现有「每连接轮询」实现（见 §5）。

## 3. 配置项（新增，均有默认值）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WATCH_STREAM_BROADCASTER` | `1` | 0 时回退到现有每连接轮询实现 |
| `WATCH_STREAM_QUEUE_SIZE` | `8` | 每订阅者队列长度，满则触发 Resync |
| `WATCH_STREAM_CHANNEL_IDLE_SECONDS` | `30` | 频道空转回收宽限期 |

复用现有 `stream_live_interval_seconds` / `stream_static_interval_seconds` 作为发布者节奏，不新增节奏配置。

## 4. 边界与风险

1. **频道数量有界性**：参数组合理论上很多，但实际由前端固定产生（`useTerminalStream` 只发 board_level/sort/page/page_size/sector）。按 `page_size=40` 档和有限板块数，频道数 << 连接数；再加频道总数上限（如 256，超出回退 per-connection 循环）兜底。
2. **一致性语义不变**：所有订阅者收到的字节流与现状逐字节等价（同一快照 + 同一串 delta），区别只是计算从 N 份变成 1 份。盘中 1s 的共享载荷 TTL 语义已在上一轮改动中确立，本次不叠加新的延迟。
3. **连接级个性化**：现状唯一 per-connection 的东西就是 delta tracker 状态，本方案用「共享顺序流 + Resync 兜底」替代，不阻塞广播化。
4. **异常隔离**：发布者循环内捕获构建异常 → 本轮跳过（沿用现有「出错就等下一 tick」语义），不杀死频道；订阅者发送失败只移除自己。
5. **与现有测试的兼容**：`test_terminal_stream_api.py` 走真实 WS 握手，协议不变应直接通过；新增 hub 单测覆盖共享构建与 Resync。

## 5. 实施步骤（建议顺序，可逐步验收）

1. `app/stream_hub.py`：`StreamChannel` / `StreamHub` / `Subscription` / `Resync`，含频道创建、回收、背压逻辑（纯新文件，~150 行）。
2. hub 单测：两个订阅者共享一次构建（计数 `service.terminal` 调用）；慢订阅者触发 Resync 收到新 snapshot；全部断开后频道回收；频道数上限回退。
3. `app/main.py` 的 `/ws/stream` 改为薄壳，加 `WATCH_STREAM_BROADCASTER` 开关，关闭时走原实现（原循环保留为 `_stream_legacy()`）。
4. 回归：全部 WS/terminal 测试 + 手动开两个浏览器页签验证同屏一致、翻页/切板块各自成频道互不影响。
5. （可选）压测：`scripts/` 下加一个 ws 扇入脚本，对比 50/200 连接下 CPU 与 p99 延迟，数据写进 README 部署章节。

## 6. 验收标准

- 同一视图 N 个连接：每 tick `service.terminal` 构建 ≤1 次（service 缓存命中后主要是 1 次 dump + 1 次 delta 计算 + 1 次 JSON 编码）。
- 协议逐字节兼容：前端不改代码，snapshot/delta 行为与现状一致。
- 慢客户端只影响自己（Resync 退化），频道内其他连接不受影响。
- 开关关闭后行为与当前 master 完全一致。
