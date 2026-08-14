# 日内宏观盯盘

本地运行的 A 股日内盯盘网页。后端读取行情和本地配置，前端只展示结果，不暴露 `ts2db_config.yaml` 里的密钥。

## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts\dev_server.py
```

如果系统 `python` 不可用，可以使用 Codex 捆绑 Python 创建环境：

```powershell
& "C:\Users\xpc07\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts\dev_server.py
```

开发脚本启动前会先结束当前项目里残留的 `dev_server.py` / `uvicorn app.main:app` 进程，避免端口和 SQLite 被多个后端同时占用。
默认启用热更新（`--reload`，只监视 `app/` 下的 Python 源码），改后端代码自动生效；前端为 `web/` 下的 React 应用，开发时用 `web/` 里 `npm run dev`（端口 7100，代理 `/api` 和 `/ws` 到 8788）获得 HMR，改前端代码无需重启后端。需要关闭后端热更新时显式设 `WATCH_DEV_RELOAD=0`：

```powershell
$env:WATCH_DEV_RELOAD="0"
.\.venv\Scripts\python.exe scripts\dev_server.py
```

打开浏览器访问：

```text
http://127.0.0.1:8788
```

## 数据模式

默认 `WATCH_DATA_MODE=auto`：交易时段优先 `easy_tdx` 实时行情，收盘后自动切到 `easy_tdx` 的最近交易日收盘快照。真实行情不可用时页面会明确显示“无真实数据”，默认不会自动使用内置回放。

可手动指定：

```powershell
$env:WATCH_DATA_MODE="replay"  # 强制回放
$env:WATCH_DATA_MODE="live"    # 强制easy_tdx实时行情
$env:WATCH_ALLOW_REPLAY_FALLBACK="1"  # 仅测试时允许自动回放兜底
```

默认股票板扫描全市场，不把网页自选股当过滤条件。网页自选只负责置顶、标色和详情分析，不参与默认扫描。需要临时把网页自选也纳入扫描时，可以设置：

```powershell
$env:WATCH_INCLUDE_WATCHLIST="1"
```

终端股票表按综合活跃度展示，并支持板块切换、排序和分页；页面底部会同时显示后端扫描总数，分页不代表过滤。个股详情中保留 `买T / 观察 / 减T或卖T` 的日内回放时间轴。

实时刷新默认约 5 秒，板块资金动能默认约 20 秒；可按数据源限频情况调整：

```powershell
$env:WATCH_FULL_MARKET_REFRESH_SECONDS="5"
$env:WATCH_TERMINAL_CONTEXT_LIVE_CACHE_SECONDS="5"
$env:WATCH_SECTOR_FLOW_REFRESH_SECONDS="20"
```

React 首页（`web/`，构建产物 `web/dist` 由后端直接挂载在 `/` 与 `/assets`）走 **WebSocket 增量协议**：连接 `/ws/stream?view=terminal&format=delta` 时首帧全量快照，之后只推变化分区（榜单按 code upsert/remove/order，未变行不重绘）；大响应体经 GZip 压缩。旧版 `static/` 原生 JS 界面已删除，`format=delta` 是唯一协议。

收盘后使用最近交易日快照，页面显示 `15:00:00 · 数据冻结`，不会因为 WebSocket 空转反复重绘。

## CloudBase 云托管持久化

CloudBase CloudRun 容器文件系统不是持久化存储，实例重启、扩缩容或重新部署后，本地 `/tmp`、SQLite 和 JSON 文件都可能回到镜像初始状态。生产最优方案是保持服务无状态：高频行情轨迹仍写本地 SQLite 做运行期缓存，跨重启必须保留的小状态写入 CloudBase NoSQL。

当前镜像默认开启：

```text
WATCH_BACKGROUND_COLLECTOR=1
WATCH_OPENING_WINDOW_ENGINE=1
WATCH_PERSISTENCE_BACKEND=cloudbase_nosql
WATCH_CLOUDBASE_ENV_ID=server-d2g7x597t019f5cb0
WATCH_CLOUDBASE_STATE_COLLECTION=watchtower_state
WATCH_DARK_POOL=1
```

需要在 CloudBase 控制台准备 NoSQL 集合 `watchtower_state`，并在云托管服务环境变量里配置服务端 token：

```text
WATCH_CLOUDBASE_API_TOKEN=<CloudBase API Key>
```

`WATCH_CLOUDBASE_API_TOKEN` 只能放在云托管环境变量或本机临时环境变量里，不要写进 Dockerfile、README、`ts2db_config.yaml` 或前端代码。默认实例和数据库都是 `(default)`；如使用非默认库，再设置：

```text
WATCH_CLOUDBASE_DATABASE_INSTANCE=(default)
WATCH_CLOUDBASE_DATABASE_NAME=(default)
```

云端会保存自选股、持仓、最新 dashboard 快照、官方板块成员缓存和开盘窗口菱形标记。容器重启后，服务优先从 NoSQL 恢复这些状态；服务保持活跃时，后台采集器会继续按行情源重新构建本地轨迹缓存。暗盘资金开启后只在独立慢循环里读取有界股票池，不进入 5 秒全市场刷新链路。若希望减少冷启动和空档，CloudRun 建议设置最小实例数 `MinNum=1`；如果为了省成本设为 `0`，冷启动后仍能从 NoSQL 恢复轻量状态，但运行期 SQLite 缓存需要重新采集。

## 竞价与盘口能力

标准 `easy_tdx` A 股行情协议可以返回五档委买委卖、`b_vol/s_vol` 汇总主动成交量、当前分钟量、累计成交额和逐笔成交回报，但不返回委托队列、逐笔委托或隐藏队列事实。后续分析以逐笔成交回报作为重要成交事实输入，用来确认买盘承接、识别放量抛压和辅助买 T / 减 T 判断；五档深度和分钟成交额作为辅助代理，不把队列数据作为统一口径。

标准协议另外提供 `get_transaction_data` 和
`get_history_transaction_data`。系统只在点开个股详情或调用交易明细接口时按需读取有限条数的真实成交回报；先用 `is_trading_window()` 判断当前是否处在盘中交易窗口，且请求日期为当天时使用 `get_transaction_data` 读取当前逐笔，非盘中、周末、节假日、收盘后或请求历史交易日时使用 `get_history_transaction_data`。非盘中调试应显式传入最近有效交易日，例如 `trade_date=20260807`，避免用周末日期请求空数据。返回数据优先使用 `buyorsell`（`0` 买、`1` 卖），特殊值按中性，字段缺失时才按相邻成交价计算方向成交额和大额成交差。这属于逐笔成交 L1 代理，是当前策略的重要分析数据；它不是 委托队列，也不会进入全市场每 5 秒的批量请求。

集合竞价分两种质量：

- `真实快照`：`easy_tdx` 明确返回集合竞价明细或历史 09:25 竞价/成交回填。
- `TDX L1竞价代理`：仅在 `09:15-09:29` 采集指示价、`cur_vol` 和五档轨迹，不能当作未匹配委托量或队列事实。

竞价阶段只生成开盘候选。`09:33` 只做初筛，不给可执行买点；`09:35` 是市场、板块、个股三层闸门同时通过后的最早 `确认买T` 时间，`09:37` 再复核延续性、抛压和追涨风险。指数拐头和量能可在当前及前一分钟分别出现，并要求板块核心进攻/上板、个股分时放量和低位拐头，以及五档买盘或透明的成交额代理。能力、轨迹和开盘决策接口：

```text
GET /api/market/capabilities
GET /api/auction/{code}?trade_date=YYYYMMDD
GET /api/transactions/{code}?trade_date=YYYYMMDD&count=240
GET /api/opening/decision
GET /api/opening/research
```

部署前可用探测脚本确认当前行情服务器真实返回的字段：

```powershell
.\.venv\Scripts\python.exe scripts\probe_easy_tdx_capabilities.py --codes 300476,300308,000001
.\.venv\Scripts\python.exe scripts\probe_easy_tdx_capabilities.py --codes 300476,300308,000001 --date 20260807
```

探测结果会分别列出五档、显式竞价字段、逐笔成交字段和队列相关能力。不带 `--date` 时探测当前逐笔，开盘外可能只返回尾盘/盘后残留或空数据；带 `--date` 时调用历史逐笔接口。当前标准服务器的预期结论是：五档可用、竞价只能在 `09:15-09:29` 采集标记为 `proxy` 的指示价/五档轨迹、逐笔成交 L1 可按需读取并作为重要分析数据，队列数据不可用且不作为当前策略必需输入。

## 开盘7分钟决策标记（opening7）

基于 `docs/opening7_research.md` 的事件研究（自选池 15 只 × 62 交易日），分时图上叠加开盘 7 分钟制度分层研究标记：

- `09:31` 卖出闸门：高开 ≥2% 且分笔净买比 ≤-10%（L1 成交明细，非委托队列）。持仓票 → 深绿菱形「开盘卖出」；**无持仓票同样触发** → 深绿菱形「回避追高」（避免在开盘高点买入，引擎参数 `sell_gate_for_all`，详情分时图与机会队列一致生效）。
- `09:33` 买入决策：按指数开盘制度分层——强低开 ≤-0.8% 抢反弹；低开 -0.8~-0.3% 要求无重抛压；平稳 -0.3~0% 要求分笔净买 ≥10% 且站上分笔 VWAP；高开不追。命中 → 深红菱形「开盘买入」。追高排除：缺口 ≥5% 或开盘已涨 ≥6.5%。

标记由 `GET /api/signals/{code}/detail/overlay` 的 `opening_markers` 字段返回，`validation_status=research_only`、`executable=false`，仅为研究信号不自动执行；引擎在 `app/opening7.py`，盘中实时与历史复盘走同一纯函数路径。公式买卖点（浅色圆点）与开盘决策（深色菱形）可同时显示。

## 开盘窗口菱形引擎（机会队列信号流）

`app/opening_window_engine.py` 在 09:30–10:00 以 6 秒一轮为机会队列实时产出菱形买卖点，10:00 后自动停转，不进入 5 秒看板刷新循环：

- **观察池（~40 只有界）**：板块热度前 5 × 成员前 3 + 活跃股前 20 + 自选，去重，涨停票动态剔除。
- **分笔边界**：tape 只读池内票、只在窗口内读（盘内 `get_transaction_data`，历史 `get_history_transaction_data`），价格侧全部来自本地行情快照，零额外抓取。
- **规则**：09:31 卖出/回避 + 09:33 买入（opening7 已验证口径的行情代理回放）；09:35–10:00 三条扩展规则（高点回避 / 回踩VWAP / 低开修复）为 `research_only` 新假设，阈值由 `scripts/opening_window_research.py` 的轨迹事件研究决定。
- **两态确认**：扩展规则首次命中进队列为空心「确认中」（warn），连续 2 轮（12s）成立转实心确认（confirmed）；预警消失未确认则移出。同票同规则当天只出一颗。
- **输出**：终端 payload 新增 `opening_markers` 分区（WS 增量随变化推送，最新 20 条）；分页端点 `GET /api/opening/markers?trade_date=&offset=&limit=20`（加载更多/历史日）；按日持久化到 `data/runtime/opening-markers/<YYYYMMDD>.json`。
- 可用环境变量调参：`WATCH_OPENING_WINDOW_ENGINE`（开关）、`WATCH_OPENING_WINDOW_TICK_SECONDS`、`WATCH_OPENING_WINDOW_TAPE_COUNT`、`WATCH_OPENING_WINDOW_POOL_*`、`WATCH_OPENING_WINDOW_WARN_CONFIRM_TICKS`。

历史事件研究（随轨迹库积累样本后重跑）：

```powershell
.\.venv\Scripts\python.exe scripts\opening_window_research.py --days 20 --top 30
```

## 做 T 策略研究

当前采用研究优先协议：正 T、反 T 分开标注，先验证 H1-H7 的市场机制、反事实和可实现盈亏比，再决定是否固化规则。旧 V2/V3 只保留为历史基线，不参与可执行状态。每个目标日使用前一交易日可见的流动性和行业分层独立选择样本；一字板和无法成交事件保留为 `no_fill`。收盘后、周末和历史日期的逐笔成交统一来自 easy_tdx `get_history_transaction_data`，口径为 L1 transaction tape，不称为队列数据。

两日真实数据只用于链路冒烟和数据质量检查：

```powershell
.\.venv\Scripts\python.exe scripts\research_t_strategy.py --dates 20260806,20260807 --sample-size 100
.\.venv\Scripts\python.exe scripts\research_t_strategy.py --dates 20260806,20260807 --sample-size 100 --no-transactions
```

正式筛选先扩展到 20 个连续交易日，再扩展到 60 日滚动样本外验证：

```powershell
.\.venv\Scripts\python.exe scripts\research_t_strategy.py --end 20260807 --lookback-days 20 --sample-size 100
.\.venv\Scripts\python.exe scripts\research_t_strategy.py --end 20260807 --lookback-days 60 --sample-size 100
```

完整报告写入 `data/runtime/strategy-research/latest.json` 和 `latest.md`；页面/API 只读不含原始候选、标签和结果的 `latest_summary.json`。旧报告缺少摘要时可运行：

```powershell
.\.venv\Scripts\python.exe scripts\build_research_summary.py
```

研究状态通过 `GET /api/research/status` 和 `GET /api/research/protocol` 查看。未达到 20/60 日门槛、无滚动样本外折或悲观摩擦下未通过时，接口必须保持 `sample_insufficient` / `deployable=false`。

## 知识星球消息同步

盯盘系统运行时只读自己的消息库 `data/runtime/watchtower_messages.sqlite`，不直接读取 `G:\ai\lh\zsxq`。本地知识星球工程同步和归类完成后，用推送脚本把处理好的消息写入盯盘系统：

```powershell
$env:WATCH_INGEST_TOKEN="your-local-secret"
.\.venv\Scripts\python.exe scripts\dev_server.py
.\.venv\Scripts\python.exe scripts\push_zsxq_messages.py --lookback-days 3 --target-url http://127.0.0.1:8788 --token $env:WATCH_INGEST_TOKEN
```

生产部署时同样只开放 `POST /api/ingest/zsxq/messages` 接收推送，token 放在生产后端环境变量 `WATCH_INGEST_TOKEN`。查看同步状态：

```text
GET /api/messages/status
```

`G:\\ai\\lh\\zsxq` 的每日 08:00 `message-cache-sync-daemon` 已支持在上游同步成功后自动调用这个推送脚本。调度器从 `WATCH_INGEST_TOKEN`、`WATCH_TARGET_URL`、`WATCH_PUSH_SCRIPT` 和 `WATCH_PUSH_PYTHON` 读取连接信息；token 不写入 Windows 计划任务命令或同步日志。若未配置 token，调度记录会明确显示“未配置 WATCH_INGEST_TOKEN”，不会误报已经推送。

## `ts2db_config.yaml` 说明

建议先复制 `ts2db_config.example.yaml` 再按需填写。这个文件不是核心看板的必需项；不配置时，网页看板和行情读取仍可运行，只是 AI 分析、Tushare 收盘落库和消息 ingest 相关能力不可用。

| 字段 | 必要性 | 说明 |
| --- | --- | --- |
| `tushare_token` | 必需（仅 `scripts/ingest_eod_tushare.py`） | 拉取 Tushare 收盘 EOD 数据。 |
| `cf_base_url` + `cf_key` | 需要成组配置，二选一即可 | CloudBase AI 代理。`cf_model_id` 可选。 |
| `deepseek-key` / `zhipu_key` / `bailian_key` / `huoshan_key` | 至少填一组即可 | 选择一个直连 AI 提供方。 |
| `message_ingest_token` / `watch_ingest_token` | 可选 | 消息推送备用配置；生产更建议用 `WATCH_INGEST_TOKEN` 环境变量。 |
| `news_api_key` | 可选 / 预留 | 当前只用于状态展示，主功能不依赖。 |

如果这些 AI 提供方都不填，AI 分析接口会保持不可用，但主看板不受影响。

## 关键文件

- `data/watchlist.json`：网页自选股持久化文件；默认不参与全市场股票板扫描。
- `data/themes.yaml`：手工交易主题和核心票映射。
- `data/trading_rules.yaml`：买T、卖T、板块强度等阈值。
- `data/runtime/watchtower_messages.sqlite`：盯盘系统自己的消息库，由 ingest API 写入。
- `data/runtime/auction_snapshots.jsonl`：交易日内可行动竞价候选的采样轨迹。
- `data/runtime/opening_decisions.jsonl`：盘中保存的真实开盘检查点快照，供收盘后复盘。
- `ts2db_config.example.yaml`：空模板，复制成 `ts2db_config.yaml` 后再填写。
- `ts2db_config.yaml`：本地密钥配置，仅在对应功能启用时需要；具体必需/可选项见上文。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest
```

终端接口：

```text
GET /api/dashboard?view=terminal&sector=&sort=activity&page=1&page_size=80
GET /api/stocks/board?sector=&sort=activity&page=1&page_size=80
WS /ws/stream?view=terminal&sector=&sort=activity&page_size=80
WS /ws/stream?view=terminal&format=delta&...   # React 首页：快照 + 分区增量
GET /api/signals/{code}/detail
GET /api/opening/decision
GET /api/opening/markers?trade_date=&offset=0&limit=20
GET /api/opening/research
```

盘口字段以逐笔成交回报、成交额和五档深度代理为主，不统一包装成队列结论；easy_tdx 收盘和回放数据会明确显示无实时盘口。



## 新版 React 盯盘前端（web/）

`web/` 是重构后的专业盯盘界面：React + TypeScript + Vite + Tailwind + shadcn/ui + ECharts，深色终端主题，红涨绿跌，通过 Vite 代理对接本仓库 FastAPI 后端（默认 `127.0.0.1:8788`）。

```powershell
cd web
npm install
npm run dev        # 默认 http://localhost:7100，代理 /api 与 /ws 到 8788
```

后端不在 8788 时可用环境变量覆盖：

```powershell
$env:WATCH_BACKEND="http://127.0.0.1:8790"
npm run dev
```

布局与交互：

- 顶栏：连接状态、本地时钟、行情时间、数据模式 / 决策阶段 / 冻结标记、手动刷新。
- 市场概览条：情绪分半环仪表、市场节奏与主线、三大指数卡（现价 / 涨幅 / 日内区间位置条）、涨跌宽度堆叠条、涨停炸板跌停、两市成交额。
- 左栏板块强弱：热度分 + 渐变热度条 + 上涨占比 + 龙头 / 涨停数，点击过滤榜单，支持 1/2/3 级板块切换。
- 中栏活跃股榜单：SVG 迷你分时线（含 VWAP）、信号徽章（买T 红 / 减T卖T 绿 / 观察 金）、量比、反弹 / 回撤、板块热度、成交额，支持 6 种排序与分页，行首星标加 / 减自选。
- 右栏：机会队列（默认「◇菱形」信号流——开盘窗口红/绿菱形买卖点，空心为确认中预警、实心为确认，最新在前默认 20 条、手动加载更多；另有买T / 观察 / 减T 分组）、事件流时间线、自选预览；顶部代码 / 名称搜索直达个股详情。
- 个股详情弹窗：ECharts 分时图（价格 + VWAP + 成交量 + 公式买卖点 / 开盘决策菱形标记，悬浮查看触发条件与风险）、逐笔成交 L1 面板（主动买卖 / 大单失衡条 + 最近成交磁带）、做T公式状态（多方 / 空方力度、保护价、趋势线距离）、盘面量化共振、盈亏比评估、星球消息 / AI 分析 / 集合竞价 / 资金流 / F10 / 缠论标签页。

主接口每 5 秒轮询（`/api/dashboard?view=terminal`），详情图每 10 秒轮询；逐笔成交保持按需读取，不进入全市场轮询。暗盘资金面板独立 60s 轮询 `/api/dark-pool`，后端只读缓存与本地 SQLite，不进主刷新链路。
