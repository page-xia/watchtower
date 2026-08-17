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
- Special `buyorsell` values are neutral unless explicitly understood. Prefer `buyorsell` direction when present; only fall back to adjacent price-tick direction when the field is missing.
- 个股结构标签口径（`_classify_stock`，对齐市场通用认知，勿加自造阈值）：板块龙头 = 板块领涨股（涨幅第一且为正，涨幅为负是领跌不打标）；核心容量 = 板块中军，即板块内流通市值第一（tushare daily_basic `circ_mv` 本地快照，见 `_float_mcap_map`；无市值数据回退成交额第一）；手工主题 core_codes / 自选 core 是显式配置，不受板块口径影响。`sector.core_codes`（领涨+成交额前5）仅供板块进攻强度等内部策略使用，不再用于标签。

## Verification Commands

```powershell
.\.venv\Scripts\python.exe scripts\probe_easy_tdx_capabilities.py --codes 300476,300308,000001
.\.venv\Scripts\python.exe scripts\probe_easy_tdx_capabilities.py --codes 300476,300308,000001 --date 20260807
```

The first command checks current transaction access and may be unhelpful outside trading hours. The second command forces historical transaction access for a known trading day.
