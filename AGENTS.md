# Agent Notes

## Frontend Theming

- 前端（`web/`）支持白天/夜晚双主题，配色规范见 `docs/theme.md`。新增面板、图表或调整配色前先读它：一律走 CSS 变量 + `chartPalette(theme)`，不要在组件里写死 `hsl()` / hex 色值。

## Market Data Boundaries

- 板块口径统一以 easy_tdx 官方板块（申万三级，`fetch_board_context` / `_stock_board_display_map`）为唯一标准，盘中与收盘后一致。Tushare 只提供个股级资金数值（moneyflow / block_trade），不参与板块分类；板块汇总一律用 easy_tdx 映射对个股数值归组，禁止混入东财/同花顺等第三方板块 taxonomy。手动主题（themes.yaml）是独立功能，不算板块口径。
- Treat easy_tdx L1 transaction tape as an important analysis input for this project. Future strategy work should use it to confirm real buying support, detect selling pressure, and support buy-T / reduce-T decisions.
- Keep easy_tdx data quality labels strict. A-share quotes can provide five displayed levels, aggregate active volume, minute price/amount and L1 transaction tape, but not order queues, order-by-order entrusts, ten-level queues or hidden main-order truth.
- Do not use Level-2 as the unified project vocabulary. `get_transaction_data` and `get_history_transaction_data` return L1 transaction prints, usually with `hour`, `minute`, `price`, `vol` and `buyorsell`; describe them as transaction tape or L1 transaction flow.
- Before reading easy_tdx transaction tape, decide whether the request is intraday:
  - If `is_trading_window()` is true and the requested trade date is today, use `get_transaction_data`.
  - If the market is not in the trading window, the date is historical, it is after close, weekend or a holiday, use `get_history_transaction_data`.
  - For non-intraday debugging, pass an explicit valid `trade_date=YYYYMMDD`; do not rely on today's date when today is not a trading day.
- Keep transaction reads on demand for stock details or transaction endpoints. Do not add full-market transaction polling to the 5-second dashboard refresh loop.
- The opening-window diamond engine (`app/opening_window_engine.py`) is the one sanctioned exception to watch-set tape reads: a bounded ~40-code pool (top-5 heat sectors x top-3 members + activity top-20 + watchlist, deduped, limit-up excluded), 6s ticks, only 09:30-10:00, intraday/historical routing unchanged. It must never expand to full-market tape polling or join the dashboard refresh loop. Its tape stats are large-print only (`large_buy_amount`/`large_sell_amount` per minute, threshold max(50万, 5×median print amount)); small prints are excluded from the net-buy ratio. Codes with a confirmed marker join the dedup set and are no longer monitored that day.
- Special `buyorsell` values are neutral unless explicitly understood. Prefer `buyorsell` direction when present; only fall back to adjacent price-tick direction when the field is missing.

## Verification Commands

```powershell
.\.venv\Scripts\python.exe scripts\probe_easy_tdx_capabilities.py --codes 300476,300308,000001
.\.venv\Scripts\python.exe scripts\probe_easy_tdx_capabilities.py --codes 300476,300308,000001 --date 20260807
```

The first command checks current transaction access and may be unhelpful outside trading hours. The second command forces historical transaction access for a known trading day.
