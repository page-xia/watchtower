"""暗盘资金盯盘模块（大单推断 + Tushare 收盘口径）。

两层口径，严格分开标注（见 AGENTS.md 数据边界）：

- 盘中「大单推断」：easy_tdx L1 成交磁带的大单印花拆出
  （单笔成交额 ≥ max(50万, 5×中位数)），小单噪声不进净买比。
  这是推断信号，不是隐藏主力单真值——L1 看不到委托队列/冰山单。
- 盘后「官方口径」：Tushare moneyflow（主力净额）+ block_trade（大宗交易，
  字面意义的暗盘成交）+ 龙虎榜命中，收盘后由 scripts/ingest_eod_tushare.py
  落库到 data/runtime/tushare_eod.sqlite，本模块只读本地库。

性能边界（首页不能被拖慢）：

- HTTP 端点只读内存缓存 / 本地 SQLite（带 TTL），永远不在请求路径上发
  行情网络请求；
- 盘中磁带读取在独立守护线程里按慢节奏跑（默认 120s 一轮），股票池有界
  （默认 24 只：自选股 + 成交额 top），与 5 秒大盘刷新循环完全隔离；
- 非交易时段 / replay 模式下磁带线程自动休眠，只出官方口径数据。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable

from app.config import AppSettings
from app.data_sources import china_now, is_trading_window, market_session
from app.models import Quote, TransactionFlowObservation

logger = logging.getLogger(__name__)

EOD_DB_FILE = "tushare_eod.sqlite"

# 大单推断打标阈值
ABSORB_RATIO_PCT = 20.0   # 大单净流入占比 ≥ 20% 且价格滞涨 → 疑似暗吸
ABSORB_PRICE_PCT = 1.0    # 「滞涨」判定：|涨幅| ≤ 1%
MIN_LARGE_TOTAL = 1_000_000.0  # 大单成交总额低于 100 万不参与打标（噪声）


def _now_hhmm() -> int:
    now = china_now()
    return now.hour * 100 + now.minute


def _tape_window_open() -> bool:
    """盘中磁带读取窗口：工作日 09:30-15:00。"""
    if china_now().weekday() >= 5:
        return False
    hhmm = _now_hhmm()
    return 930 <= hhmm <= 1500


class DarkPoolMonitor:
    """暗盘资金监控：盘中大单推断慢循环 + 官方口径本地库读取。"""

    def __init__(
        self,
        settings: AppSettings,
        context_provider: Callable[[], Any],
        tape_fetcher: Callable[..., TransactionFlowObservation],
        sector_mapper: Callable[[int], dict[str, str]] | None = None,
    ) -> None:
        self.settings = settings
        self._context_provider = context_provider
        self._tape_fetcher = tape_fetcher
        self._sector_mapper = sector_mapper
        self._db_path = Path(settings.data_dir) / "runtime" / EOD_DB_FILE

        self._lock = threading.Lock()
        self._intraday_cache: dict[str, Any] = {"available": False, "rows": [], "note": "等待首个盘中周期"}
        self._intraday_cache_at: float = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._eod_cache: tuple[float, dict[str, Any]] | None = None
        self._eod_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 对外入口：只读缓存，绝不阻塞
    # ------------------------------------------------------------------
    def payload(self) -> dict[str, Any]:
        self._ensure_thread()
        with self._lock:
            intraday = dict(self._intraday_cache)
        return {
            "as_of": china_now().strftime("%H:%M:%S"),
            "session": market_session(),
            "enabled": bool(self.settings.dark_pool_enabled),
            "intraday": intraday,
            "eod": self._eod_payload(),
        }

    # ------------------------------------------------------------------
    # 盘中：独立慢循环读 L1 磁带大单
    # ------------------------------------------------------------------
    def _ensure_thread(self) -> None:
        if not self.settings.dark_pool_enabled:
            return
        if self.settings.data_mode == "replay":
            return
        if self._thread and self._thread.is_alive():
            return
        if not _tape_window_open() and self._intraday_cache_at:
            return  # 盘后已有冻结结果，不再起线程
        self._thread = threading.Thread(
            target=self._run_loop,
            name="dark-pool-monitor",
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if _tape_window_open() and is_trading_window():
                age = time.monotonic() - self._intraday_cache_at
                if age >= self.settings.dark_pool_refresh_seconds:
                    started = time.monotonic()
                    try:
                        snapshot = self._collect_intraday()
                        with self._lock:
                            self._intraday_cache = snapshot
                            self._intraday_cache_at = time.monotonic()
                    except Exception as exc:  # noqa: BLE001 - 周期失败不拖垮线程
                        logger.warning("dark pool intraday cycle failed: %s", exc)
                        with self._lock:
                            self._intraday_cache_at = time.monotonic()
                    elapsed = time.monotonic() - started
                    if elapsed > 20:
                        logger.warning("dark pool cycle slow: %.1fs", elapsed)
            self._stop.wait(2.0)

    def _pool_quotes(self) -> list[Quote]:
        """有界股票池：自选 + 全市场成交额 top，涨停一字板除外。"""
        try:
            context = self._context_provider()
        except Exception:  # noqa: BLE001
            return []
        quotes = list(getattr(getattr(context, "snapshot", None), "quotes", []) or [])
        watchlist = {str(item.code) for item in getattr(context, "watchlist", []) or []}
        pool_size = self.settings.dark_pool_pool_size

        def eligible(q: Quote) -> bool:
            return bool(q.code) and not q.limit_up and not q.limit_down and float(q.amount or 0) > 0

        picked: dict[str, Quote] = {}
        for q in quotes:  # 自选股优先入池
            if q.code in watchlist and eligible(q):
                picked[q.code] = q
        by_amount = sorted((q for q in quotes if eligible(q)), key=lambda q: q.amount, reverse=True)
        for q in by_amount:
            if len(picked) >= pool_size:
                break
            picked.setdefault(q.code, q)
        return list(picked.values())

    def _collect_intraday(self) -> dict[str, Any]:
        pool = self._pool_quotes()
        quotes_by_code = {q.code: q for q in pool}
        sector_maps = self._sector_maps_all_levels()
        sectors_l3 = sector_maps.get(3) or {}
        rows: list[dict[str, Any]] = []
        errors = 0

        def fetch(code: str) -> TransactionFlowObservation:
            return self._tape_fetcher(code, None, True)

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="dark-pool-tape") as executor:
            futures = {executor.submit(fetch, q.code): q.code for q in pool}
            for future in as_completed(futures):
                code = futures[future]
                quote = quotes_by_code.get(code)
                try:
                    obs = future.result()
                except Exception:  # noqa: BLE001
                    errors += 1
                    continue
                if not obs.available or not quote:
                    continue
                large_buy = float(obs.large_buy_amount or 0)
                large_sell = float(obs.large_sell_amount or 0)
                total = large_buy + large_sell
                if total < MIN_LARGE_TOTAL:
                    continue
                net = large_buy - large_sell
                ratio = net / total * 100
                change_pct = float(quote.change_pct or 0)
                rows.append(
                    {
                        "code": code,
                        "name": quote.name,
                        "sector": sectors_l3.get(code, ""),
                        "change_pct": round(change_pct, 2),
                        "large_buy_amount": round(large_buy, 0),
                        "large_sell_amount": round(large_sell, 0),
                        "net_amount": round(net, 0),
                        "net_ratio_pct": round(ratio, 1),
                        "tag": self._tag(net, ratio, change_pct),
                    }
                )
        rows.sort(key=lambda r: abs(float(r["net_amount"])), reverse=True)
        rows = rows[:15]
        return {
            "available": bool(rows),
            "refreshed_at": china_now().strftime("%H:%M:%S"),
            "pool_size": len(pool),
            "errors": errors,
            "source": "easy_tdx L1 成交磁带大单拆出（推断，非隐藏单真值）",
            "refresh_seconds": self.settings.dark_pool_refresh_seconds,
            "rows": rows,
            "sector_rollup": self._sector_rollup(rows, "net_amount"),
            "sector_rollup_by_level": {
                f"l{level}": self._sector_rollup(
                    [dict(r, sector=(sector_maps.get(level) or {}).get(str(r["code"]), "")) for r in rows],
                    "net_amount",
                )
                for level in (1, 2, 3)
            },
        }

    def _sector_map(self, level: int = 3) -> dict[str, str]:
        if not callable(self._sector_mapper):
            return {}
        try:
            return self._sector_mapper(level) or {}
        except TypeError:
            # 兼容旧的无参 mapper（固定三级）
            try:
                return self._sector_mapper() or {}  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                return {}
        except Exception:  # noqa: BLE001
            return {}

    def _sector_maps_all_levels(self) -> dict[int, dict[str, str]]:
        return {level: self._sector_map(level) for level in (1, 2, 3)}

    @staticmethod
    def _sector_rollup(rows: list[dict[str, Any]], amount_key: str) -> list[dict[str, Any]]:
        """按板块汇总大单净额：板块内个股求和，按 |净额| 排序。"""
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            sector = str(row.get("sector") or "").strip() or "未分类"
            bucket = grouped.setdefault(sector, {"sector": sector, "net_amount": 0.0, "stock_count": 0, "top_name": "", "top_net": 0.0})
            net = float(row.get(amount_key) or 0)
            bucket["net_amount"] += net
            bucket["stock_count"] += 1
            if abs(net) > abs(bucket["top_net"]):
                bucket["top_net"] = net
                bucket["top_name"] = str(row.get("name") or row.get("code") or "")
        result = sorted(grouped.values(), key=lambda b: abs(b["net_amount"]), reverse=True)
        for bucket in result:
            bucket["net_amount"] = round(bucket["net_amount"], 0)
            bucket["top_net"] = round(bucket["top_net"], 0)
        return result

    @staticmethod
    def _tag(net: float, ratio: float, change_pct: float) -> str:
        if ratio >= ABSORB_RATIO_PCT and change_pct <= ABSORB_PRICE_PCT:
            return "疑似暗吸"
        if ratio <= -ABSORB_RATIO_PCT and change_pct >= -ABSORB_PRICE_PCT:
            return "疑似派发"
        if net > 0:
            return "大单净买"
        return "大单净卖"

    # ------------------------------------------------------------------
    # 盘后：Tushare 官方口径（本地 SQLite，TTL 缓存）
    # ------------------------------------------------------------------
    def _eod_payload(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._eod_lock:
            if self._eod_cache and now - self._eod_cache[0] < 300:
                return dict(self._eod_cache[1])
        payload = self._load_eod()
        with self._eod_lock:
            self._eod_cache = (now, payload)
        return dict(payload)

    def _load_eod(self) -> dict[str, Any]:
        if not self._db_path.exists():
            return {"available": False, "note": "尚未跑收盘管线 scripts/ingest_eod_tushare.py"}
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                trade_date_row = conn.execute(
                    "SELECT MAX(trade_date) AS d FROM moneyflow"
                ).fetchone()
                trade_date = str(trade_date_row["d"] or "") if trade_date_row else ""
                if not trade_date:
                    return {"available": False, "note": "tushare_eod.sqlite 中暂无 moneyflow 数据"}

                inflow = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT ts_code, net_mf_amount FROM moneyflow"
                        " WHERE trade_date = ? ORDER BY net_mf_amount DESC LIMIT 10",
                        (trade_date,),
                    )
                ]
                outflow = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT ts_code, net_mf_amount FROM moneyflow"
                        " WHERE trade_date = ? ORDER BY net_mf_amount ASC LIMIT 5",
                        (trade_date,),
                    )
                ]
                blocks = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT b.ts_code, b.price, b.vol, b.amount, d.close,"
                        "       ROUND((b.price / d.close - 1) * 100, 2) AS premium_pct"
                        " FROM block_trade b JOIN daily_basic d"
                        "   ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date"
                        " WHERE b.trade_date = ? ORDER BY b.amount DESC LIMIT 10",
                        (trade_date,),
                    )
                ]
                top_list_codes = {
                    str(r["ts_code"])
                    for r in conn.execute(
                        "SELECT DISTINCT ts_code FROM top_list WHERE trade_date = ?",
                        (trade_date,),
                    )
                }
                # 全市场主力净额（板块汇总用，单次约 5500 行，300s TTL 摊薄成本）
                all_flow = [
                    (str(r["ts_code"]), float(r["net_mf_amount"] or 0))
                    for r in conn.execute(
                        "SELECT ts_code, net_mf_amount FROM moneyflow WHERE trade_date = ?",
                        (trade_date,),
                    )
                ]
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "note": f"读取本地库失败：{exc}"}

        names = self._name_map()
        sector_maps = self._sector_maps_all_levels()
        sectors = sector_maps.get(3) or {}
        for row in inflow + outflow:
            row["code"] = str(row.pop("ts_code", ""))[:6]
            row["name"] = names.get(row["code"], row["code"])
            row["sector"] = sectors.get(row["code"], "")
            # moneyflow 金额单位为万元，统一转成元供前端 fmtAmount 复用。
            row["net_mf_amount"] = round(float(row["net_mf_amount"] or 0) * 10000, 0)
            row["on_top_list"] = self._to_ts_code(row["code"]) in top_list_codes
        for row in blocks:
            row["code"] = str(row.pop("ts_code", ""))[:6]
            row["name"] = names.get(row["code"], row["code"])
            row["sector"] = sectors.get(row["code"], "")
            row["amount"] = round(float(row["amount"] or 0) * 10000, 0)
            row["on_top_list"] = self._to_ts_code(row["code"]) in top_list_codes

        # 官方口径全市场板块汇总（1/2/3 级）：每级净流入 top8 + 净流出 top4
        rollup_by_level: dict[str, list[dict[str, Any]]] = {}
        for level in (1, 2, 3):
            level_map = sector_maps.get(level) or {}
            sector_rows = [
                {"sector": level_map.get(code[:6], "") or "未分类", "name": names.get(code[:6], code[:6]), "net_amount": amount * 10000}
                for code, amount in all_flow
            ]
            rollup = self._sector_rollup(sector_rows, "net_amount")
            rollup_by_level[f"l{level}"] = (
                [b for b in rollup if b["net_amount"] > 0][:8]
                + [b for b in reversed(rollup) if b["net_amount"] < 0][:4]
            )

        return {
            "available": True,
            "trade_date": trade_date,
            "source": "Tushare 官方口径（moneyflow 主力净额 / block_trade 大宗交易）",
            "main_inflow": inflow,
            "main_outflow": outflow,
            "block_trades": blocks,
            "sector_rollup": rollup_by_level["l3"],
            "sector_rollup_by_level": rollup_by_level,
        }

    @staticmethod
    def _to_ts_code(code: str) -> str:
        if code.startswith(("6", "9")):
            return f"{code}.SH"
        if code.startswith(("4", "8")):
            return f"{code}.BJ"
        return f"{code}.SZ"

    def _name_map(self) -> dict[str, str]:
        try:
            context = self._context_provider()
        except Exception:  # noqa: BLE001
            return {}
        quotes = getattr(getattr(context, "snapshot", None), "quotes", []) or []
        return {q.code: q.name for q in quotes if q.code}
