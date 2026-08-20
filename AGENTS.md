# Agent Notes

## Frontend Theming

- 前端（`web/`）支持白天/夜晚双主题，配色规范见 `docs/theme.md`。新增面板、图表或调整配色前先读它：一律走 CSS 变量 + `chartPalette(theme)`，不要在组件里写死 `hsl()` / hex 色值。

## Market Data Boundaries

- 板块口径统一以 easy_tdx 官方板块（申万三级，`fetch_board_context` / `_stock_board_display_map`）为唯一标准，盘中与收盘后一致。Tushare 只提供个股级资金数值（moneyflow / block_trade），不参与板块分类；板块汇总一律用 easy_tdx 映射对个股数值归组。禁止混入东财/同花顺等第三方板块 taxonomy——这条约束的是星球消息归类等数据归组场景；面板查看口径不受此限。通达信概念/风格/地区板块（easy_tdx `BoardType.GN/FG/DQ`）已接入为并列查看口径：内部编码 `board_level=4/5/6`，面板「板块扫描」头部菜单切换（板块 1/2/3 级 / 概念 / 风格 / 地区），与申万聚合互不混合，一只票可同时属于多个概念/风格。手工主题面板模式（sector_mode=theme、theme_sectors）已于 2026-08-17 移除；`data/themes.yaml` 仍作为信号装饰（preferred_sector_names / core_watch）与个股题材标签的配置输入保留，不再是独立榜单口径。
- Treat easy_tdx L1 transaction tape as an important analysis input for this project. Future strategy work should use it to confirm real buying support, detect selling pressure, and support buy-T / reduce-T decisions.
- Keep easy_tdx data quality labels strict. A-share quotes can provide five displayed levels, aggregate active volume, minute price/amount and L1 transaction tape, but not order queues, order-by-order entrusts, ten-level queues or hidden main-order truth.
- Do not use Level-2 as the unified project vocabulary. `get_transaction_data` and `get_history_transaction_data` return L1 transaction prints, usually with `hour`, `minute`, `price`, `vol` and `buyorsell`; describe them as transaction tape or L1 transaction flow.
- Before reading easy_tdx transaction tape, decide whether the request is intraday:
  - If `is_trading_window()` is true and the requested trade date is today, use `get_transaction_data`.
  - If the market is not in the trading window, the date is historical, it is after close, weekend or a holiday, use `get_history_transaction_data`.
  - For non-intraday debugging, pass an explicit valid `trade_date=YYYYMMDD`; do not rely on today's date when today is not a trading day.
- Keep transaction reads on demand for stock details or transaction endpoints. Do not add full-market transaction polling to the 5-second dashboard refresh loop. The opening-window diamond engine was removed (2026-08-17, performance); the detail-page opening7 diamond overlay (`opening_markers` / `app/opening7.py` wiring in `services.py`) was removed the same day — do not reintroduce watch-set tape polling or the diamond overlay without an explicit owner request. `app/opening7.py` remains as an offline research-replay tool for `scripts/backtest_opening7_sector.py` only. 暗盘面板原「24 只磁带大单推断慢循环」已于 2026-08-18 下线（样本太小、与详情页磁带重复），盘中资金地图改用东财快照，不要再加回磁带轮询。
- 暗盘资金（2026-08-18 重构）数据源分层：暗吸/暗派 = Tushare moneyflow 多日窗口 × daily_basic 收盘价（推断口径）；大手场外 = hsgt_top10（北向十大成交，2024-08 起交易所只披露成交额，无买卖方向，禁止展示成净买入）+ block_trade + top_inst 机构席位；盘中资金地图 = 东财 push2 免费接口（`app/em_moneyflow.py`，按单笔金额分桶的推断口径，与 Tushare moneyflow 同族，非隐藏单真值）。东财只提供个股级资金数值，板块归组仍走 easy_tdx 申万映射；UI 一律标注「东财推断口径」。hk_hold 是南向（港股通持港股）口径，不是北向持股，勿用于 A 股暗盘。
- EOD 存储（2026-08-19 起迁到阿里云 RDS，见 `app/eod_store.py`）：统一 pymysql 直连阿里云 RDS MySQL `watchtower_eod` 库（`eod_*` 表，连接配置取 `ts2db_config.yaml` 的 `db_config` 或 `WATCH_EOD_MYSQL_*`；RDS 外网地址见 `.deploy/rds.env`，白名单只放行服务器 47.116.20.229 和本机出口 IP——本机宽带 IP 变了要去 RDS 控制台改白名单）。收盘后跑 `scripts/ingest_eod_tushare.py` 直接落 RDS（`--days N` 回填，`--only` 选数据集），生产容器 `WATCH_EOD_STORE_BACKEND=mysql` 同库直读，不再需要 `--push-prod` 快照推送和 `fulfill_dark_pool_requests.py` 按需补数（历史遗留链路，代码保留但不再使用）。
- 生产部署（2026-08-19 起在阿里云轻量服务器）：47.116.20.229 上 Docker 跑 `watchtower:local` 镜像（`docker run -d --name watchtower --restart unless-stopped -p 127.0.0.1:8788:8788 -v /root/watchtower/data:/data --env-file /root/watchtower/watchtower.env`），域名 omnisource.xin / www.omnisource.xin A 记录直连服务器。HTTPS（2026-08-19 起）：宿主机 nginx（Alibaba Cloud Linux 3，dnf 安装）终结 TLS，证书在 `/etc/nginx/ssl/omnisource.xin.{pem,key}`（key 权限 600），站点配置 `/etc/nginx/conf.d/omnisource.conf`（本地副本 `.deploy/omnisource-nginx.conf`），80 端口 301 跳 https，443 反代 `http://127.0.0.1:8788`（含 WebSocket 头）；原 nginx.conf 默认 80 server 块已删（备份 `nginx.conf.bak`）。证书为 DigiCert DV 免费证书，有效期 2026-08-18 ~ 2026-11-15，到期需重新申请并替换 ssl 目录下两个文件后 `nginx -s reload`。重建容器务必用 127.0.0.1:8788:8788 映射，不要再用 `-p 80:8788`（会和 nginx 抢 80 端口）。容器内 `/data` 挂到宿主机 `/root/watchtower/data`（自选股/持仓/运行态 sqlite 落盘持久化）。重新部署 = 本地打包 `Dockerfile pyproject.toml app web/dist data/themes.yaml data/trading_rules.yaml` → scp 到服务器 `/root/watchtower` → `docker build -t watchtower:local .`（服务器上 Dockerfile 副本要加 `ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`，docker 已配 1ms.run/daocloud 加速器）→ 重建容器。日常部署用 `scripts/deploy_aliyun.ps1` 一键完成这套流程（含回归测试、web 构建、白名单打包、HTTPS 冒烟检查；`-SkipTests` / `-SkipBuild` / `-SkipVerify` 可选），脚本打包时会自动给 Dockerfile 注入阿里云 pip 镜像行。
- 消息存储（星球消息，见 `app/message_store.py`）：2026-08-19 起走 `WATCH_MESSAGE_STORE_BACKEND=mysql` 直连 RDS `watchtower_msg` 库（`WATCH_MSG_MYSQL_*` 环境变量，直连传输把原 PostgREST 参数翻译成参数化 SQL，集中改在 `_query_rows`/`_count`/`_upsert_many` 三处）；历史 `cloudbase_mysql` REST 后端代码保留作参考，不再用于生产。消息数据已全量迁到 RDS（topics 24073 / events 19554 / links 386847）。
- Special `buyorsell` values are neutral unless explicitly understood. Prefer `buyorsell` direction when present; only fall back to adjacent price-tick direction when the field is missing.
- 个股结构标签口径（`_classify_stock`，对齐市场通用认知，勿加自造阈值）：板块龙头 = 板块领涨股（涨幅第一且为正，涨幅为负是领跌不打标）；核心容量 = 板块中军，即板块内流通市值第一（tushare daily_basic `circ_mv` EOD 存储快照，见 `_float_mcap_map`；无市值数据回退成交额第一）；手工主题 core_codes / 自选 core 是显式配置，不受板块口径影响。`sector.core_codes`（领涨+成交额前5）仅供板块进攻强度等内部策略使用，不再用于标签。

## Verification Commands

```powershell
.\.venv\Scripts\python.exe scripts\probe_easy_tdx_capabilities.py --codes 300476,300308,000001
.\.venv\Scripts\python.exe scripts\probe_easy_tdx_capabilities.py --codes 300476,300308,000001 --date 20260807
```

The first command checks current transaction access and may be unhelpful outside trading hours. The second command forces historical transaction access for a known trading day.
