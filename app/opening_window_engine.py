"""Opening-window diamond engine (09:30-10:00, 6s ticks).

Produces the red/green diamond buy/sell markers for the opportunity queue:

- 09:31 sell gate / avoid-chase (opening7, validated) — quote-proxy replay of
  ``app.opening7.opening_decision_markers`` with ``sell_gate_for_all``;
- 09:33 regime buy (opening7, validated);
- 09:35-10:00 extended rules (``app.opening_window_rules``, research_only).

Design boundaries (see AGENTS.md):

- the watch pool is bounded (~40 codes: top-5 heat sectors x top-3 members
  + activity board top-20 + watchlist, deduped, limit-up excluded) — this is
  NOT full-market transaction polling;
- L1 transaction tape is read only for pool codes and only during the
  09:30-10:00 window, intraday via ``get_transaction_data`` and historical
  via ``get_history_transaction_data`` (routing lives in the data source);
- price-side state is built from the local quote snapshot, zero extra fetches;
- the engine stops after 10:00 and never joins the dashboard refresh loop.

Marker states: ``warn`` (hollow diamond, first hit) upgrades to ``confirmed``
(solid) after the condition persists N consecutive ticks.  opening7 minute
events are born ``confirmed`` (minute-close semantics).  A warn that stops
being true before confirmation is removed.

Markers persist to ``data/runtime/opening-markers/<YYYYMMDD>.json`` so the
queue survives restarts and stays readable after the window closes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Protocol

from app.data_sources import china_now, is_trading_window
from app.opening7 import RULES as OPENING7_RULES, classify_regime
from app.opening_window_rules import (
    EXTENDED_END,
    EXTENDED_START,
    RULE_LABELS,
    RuleInput,
    evaluate_high_avoid,
    evaluate_low_open_recovery,
    evaluate_vwap_pullback_buy,
)

logger = logging.getLogger(__name__)

WINDOW_OPEN = "09:29"     # engine wakes, builds pool, starts tape accumulation
SELL_GATE_CLOCK = "09:31"
BUY_CLOCK = "09:33"
WINDOW_CLOSE = "10:00"
ENGINE_IDLE_AFTER = "10:01"


class JsonStateStore(Protocol):
    def get_json(self, namespace: str, key: str, default: Any = None) -> Any:
        ...

    def set_json(self, namespace: str, key: str, value: Any) -> None:
        ...


@dataclass
class TapeAccumulator:
    """Cumulative L1 tape stats rebuilt each tick from per-minute aggregates.

    Closed minutes are accumulated once; the latest (open) minute is tracked
    as a partial.  ``gap`` marks window truncation on ultra-hot stocks.

    只统计大单印花：上游按分钟聚合时已用「单笔成交额 ≥ max(50万, 5×中位数)」
    拆出 large_buy_amount / large_sell_amount，散户小单噪声不进净买比。
    """

    processed_minutes: set[str] = field(default_factory=set)
    cum_buy: float = 0.0
    cum_sell: float = 0.0
    cur_minute: str = ""
    cur_buy: float = 0.0
    cur_sell: float = 0.0
    gap: bool = False

    def update(self, points: list[Any]) -> None:
        minutes: dict[str, tuple[float, float]] = {}
        for point in points or []:
            label = str(getattr(point, "time", "") or "")[:5]
            if len(label) != 5 or label == "09:25" or label < "09:30":
                continue  # 集合竞价印花方向中性，不进净买比
            large_buy = getattr(point, "large_buy_amount", None)
            large_sell = getattr(point, "large_sell_amount", None)
            buy = float(large_buy if large_buy is not None else (getattr(point, "buy_amount", 0) or 0))
            sell = float(large_sell if large_sell is not None else (getattr(point, "sell_amount", 0) or 0))
            minutes[label] = (buy, sell)
        if not minutes:
            return
        latest = max(minutes)
        for label in sorted(minutes):
            if label >= latest:
                continue
            if label not in self.processed_minutes:
                if self.processed_minutes and label < max(self.processed_minutes):
                    continue  # 已滑出窗口的旧分钟，不重复累计
                self.processed_minutes.add(label)
                buy, sell = minutes[label]
                self.cum_buy += buy
                self.cum_sell += sell
        expected = self._expected_minutes(latest)
        if expected and not expected.issubset(self.processed_minutes | {latest}):
            self.gap = True
        buy, sell = minutes[latest]
        self.cur_minute = latest
        self.cur_buy = buy
        self.cur_sell = sell

    @staticmethod
    def _expected_minutes(latest: str) -> set[str]:
        try:
            hour, minute = int(latest[:2]), int(latest[3:])
        except (TypeError, ValueError):
            return set()
        out: set[str] = set()
        total = 9 * 60 + 30
        end = hour * 60 + minute
        while total < end:
            out.add(f"{total // 60:02d}:{total % 60:02d}")
            total += 1
        return out

    @property
    def ready(self) -> bool:
        return bool(self.processed_minutes) or bool(self.cur_minute)

    def net_ratio(self, upto: str | None = None) -> float | None:
        buy = sell = 0.0
        # 无法按分钟回放闭分钟明细（累计值不记分钟），upto 过滤只在
        # 开盘早期（09:31/09:33）使用——彼时累计分钟本就 <= upto。
        buy = self.cum_buy
        sell = self.cum_sell
        if upto is None or (self.cur_minute and self.cur_minute <= upto):
            buy += self.cur_buy
            sell += self.cur_sell
        if buy + sell <= 0:
            return None
        return (buy - sell) / (buy + sell) * 100


@dataclass
class PriceState:
    prev_session_high: float = 0.0
    session_high: float = 0.0
    max_vwap_excess_pct: float = 0.0


class OpeningWindowEngine:
    def __init__(
        self,
        settings: Any,
        *,
        context_provider: Callable[[], Any],
        tape_fetcher: Callable[..., Any],
        position_checker: Callable[[str], bool],
        data_dir: Path,
        state_store: JsonStateStore | None = None,
    ) -> None:
        self.settings = settings
        self._context_provider = context_provider
        self._tape_fetcher = tape_fetcher
        self._position_checker = position_checker
        self.state_store = state_store
        self._store_dir = Path(data_dir) / "runtime" / "opening-markers"
        self._store_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._markers: dict[str, dict[str, Any]] = {}
        self._warn_hits: dict[str, int] = {}
        self._tape: dict[str, TapeAccumulator] = {}
        self._price: dict[str, PriceState] = {}
        self._trade_date = ""
        self._pool: list[dict[str, Any]] = []
        self._limit_ups: set[str] = set()  # 本 tick 快照里涨停的票（读路径过滤买入菱形用）
        self._sell_gate_done = False
        self._buy_done = False
        self._dirty = False

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="opening-window-engine",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            now = china_now()
            clock = now.strftime("%H:%M")
            if not is_trading_window(now):
                self._stop.wait(20)
                continue
            if not (WINDOW_OPEN <= clock <= ENGINE_IDLE_AFTER):
                # 窗口外（10:01 后或盘中重启）只做低频涨停缓存刷新：
                # 不拉分笔、不评估规则，仅让读路径的「涨停票买入菱形过滤」
                # 全天跟得上最新快照。
                try:
                    self._refresh_limit_up_cache(now)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("opening window limit-up cache refresh failed")
                self._stop.wait(60)
                continue
            try:
                self._tick(now)
            except Exception:  # pragma: no cover - defensive, engine must not die
                logger.exception("opening window engine tick failed")
            self._stop.wait(max(1, int(self.settings.opening_window_tick_seconds)))

    def _cache_limit_ups(self, quotes: Any) -> None:
        values = quotes.values() if isinstance(quotes, dict) else (quotes or [])
        with self._lock:
            self._limit_ups = {
                str(getattr(q, "code", "") or "")
                for q in values
                if getattr(q, "limit_up", False)
            }

    def _refresh_limit_up_cache(self, now: datetime) -> None:
        context = self._context_provider()
        if context is None or getattr(context.market, "frozen", False):
            return
        snapshot = getattr(context, "snapshot", None)
        if getattr(snapshot, "data_mode", "") not in {"live", "local_trajectory"}:
            return
        trade_date = str(context.source_status.get("trade_date") or now.strftime("%Y%m%d"))
        if trade_date != self._trade_date:
            self._roll_day(trade_date)  # 窗口外重启后补载当天持久化菱形
        self._cache_limit_ups(getattr(snapshot, "quotes", None) or [])

    # ------------------------------------------------------------- query
    def latest(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            markers = list(self._markers.values())
            trade_date = self._trade_date
        markers = self._drop_limit_up_buys(markers, trade_date)
        markers.sort(key=lambda m: str(m.get("first_seen") or ""), reverse=True)
        return markers[: max(1, limit)]

    def query(self, trade_date: str | None, offset: int = 0, limit: int = 20, side: str | None = None) -> dict[str, Any]:
        with self._lock:
            current_date = self._trade_date
            markers = list(self._markers.values())
        requested = str(trade_date or "").strip() or current_date or self._latest_persisted_date()
        if requested and requested != current_date:
            markers = self._load_day(requested)
        side_filter = str(side or "").strip().lower()
        if side_filter in {"buy", "sell"}:
            markers = [m for m in markers if str(m.get("side") or "") == side_filter]
        markers = self._drop_limit_up_buys(markers, requested)
        markers.sort(key=lambda m: str(m.get("first_seen") or ""), reverse=True)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        return {
            "trade_date": requested,
            "total": len(markers),
            "offset": offset,
            "limit": limit,
            "items": markers[offset : offset + limit],
        }

    def _latest_persisted_date(self) -> str:
        """窗口外/重启后引擎尚未 tick 时，回退到最近一个持久化交易日。"""
        latest_cloud = self._latest_cloud_date()
        if latest_cloud:
            return latest_cloud
        try:
            files = sorted(self._store_dir.glob("*.json"), reverse=True)
            for path in files:
                stem = path.stem
                if len(stem) == 8 and stem.isdigit():
                    return stem
        except Exception:
            pass
        return ""

    # -------------------------------------------------- limit-up filtering
    def _live_limit_up_codes(self, trade_date: str) -> set[str]:
        """本 tick 快照里涨停的票（tick 时缓存，读路径零额外抓取）。

        查询历史日、或引擎尚未 tick（窗口外重启、 ``_trade_date`` 为空）
        时返回空集合，不按涨停过滤——避免读路径去构建 DashboardContext。
        """
        if not self._trade_date or (trade_date and trade_date != self._trade_date):
            return set()
        with self._lock:
            return set(self._limit_ups)

    def _drop_limit_up_buys(
        self,
        markers: list[dict[str, Any]],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        """涨停票的买入菱形不进队列：买不进，信号无意义。

        监控侧池子构建已按 tick 剔除涨停票，这里兜底展示侧——
        先出菱形、后封涨停的票，其买入标记也不再出现在买T里。
        """
        limit_ups = self._live_limit_up_codes(trade_date)
        if not limit_ups:
            return markers
        return [
            m
            for m in markers
            if not (
                str(m.get("side") or "") == "buy"
                and str(m.get("code") or "") in limit_ups
            )
        ]

    # ---------------------------------------------------------- per-day io
    def _day_file(self, trade_date: str) -> Path:
        return self._store_dir / f"{trade_date}.json"

    def _load_day(self, trade_date: str) -> list[dict[str, Any]]:
        cloud_items = self._load_day_cloud(trade_date)
        if cloud_items:
            return cloud_items
        path = self._day_file(trade_date)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload.get("markers") if isinstance(payload, dict) else None
            return [m for m in items or [] if isinstance(m, dict)]
        except Exception:
            return []

    def _persist(self) -> None:
        if not self._trade_date:
            return
        with self._lock:
            markers = sorted(
                self._markers.values(),
                key=lambda m: str(m.get("first_seen") or ""),
                reverse=True,
            )
        payload = {"trade_date": self._trade_date, "markers": markers}
        path = self._day_file(self._trade_date)
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._store_dir, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                tmp = Path(handle.name)
            tmp.replace(path)
        except Exception:  # pragma: no cover - disk io
            logger.exception("failed to persist opening markers")
        self._persist_cloud(payload)

    def _persist_cloud(self, payload: dict[str, Any]) -> None:
        if self.state_store is None:
            return
        try:
            trade_date = str(payload.get("trade_date") or "").strip()
            if trade_date:
                self.state_store.set_json("opening_markers", trade_date, payload)
            self.state_store.set_json("opening_markers", "latest", payload)
        except Exception:  # pragma: no cover - cloud io
            logger.exception("failed to persist opening markers to cloud state")

    def _load_day_cloud(self, trade_date: str) -> list[dict[str, Any]]:
        if self.state_store is None or not trade_date:
            return []
        try:
            payload = self.state_store.get_json("opening_markers", trade_date)
        except Exception:
            return []
        items = payload.get("markers") if isinstance(payload, dict) else None
        return [m for m in items or [] if isinstance(m, dict)]

    def _latest_cloud_date(self) -> str:
        if self.state_store is None:
            return ""
        try:
            payload = self.state_store.get_json("opening_markers", "latest")
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        value = str(payload.get("trade_date") or "").strip()
        return value if len(value) == 8 and value.isdigit() else ""

    # -------------------------------------------------------------- tick
    def _tick(self, now: datetime) -> None:
        context = self._context_provider()
        if context is None or getattr(context.market, "frozen", False):
            return
        snapshot = context.snapshot
        if getattr(snapshot, "data_mode", "") not in {"live", "local_trajectory"}:
            return
        trade_date = str(context.source_status.get("trade_date") or now.strftime("%Y%m%d"))
        if trade_date != self._trade_date:
            self._roll_day(trade_date)

        clock = now.strftime("%H:%M")
        quotes = {q.code: q for q in snapshot.quotes}
        pool = self._build_pool(context, quotes)
        # 缓存本 tick 的涨停集合：latest/query 读路径过滤买入菱形用，
        # 不回头构建 DashboardContext，零额外抓取
        self._cache_limit_ups(quotes)
        with self._lock:
            self._pool = pool
            # 已出确认态菱形的票停止监听：不再拉分笔、不再评估扩展规则，
            # 当天信号已定格，省下每 tick 的分笔抓取与规则计算
            confirmed_codes = {
                str(m.get("code") or "")
                for m in self._markers.values()
                if m.get("state") == "confirmed"
            }
        if not pool:
            return

        for entry in pool:
            quote = quotes.get(entry["code"])
            if quote is not None:
                self._update_price_state(entry["code"], quote)

        active_pool = [entry for entry in pool if entry["code"] not in confirmed_codes]
        self._accumulate_tape(active_pool, trade_date)

        if clock >= SELL_GATE_CLOCK and not self._sell_gate_done:
            self._eval_sell_gate(now, active_pool, quotes)
        if clock >= BUY_CLOCK and not self._buy_done:
            self._eval_opening_buy(now, context, active_pool, quotes)
        if EXTENDED_START <= clock <= EXTENDED_END:
            self._eval_extended(clock, active_pool, quotes)

        if self._dirty:
            self._dirty = False
            self._persist()

    def _roll_day(self, trade_date: str) -> None:
        with self._lock:
            self._trade_date = trade_date
            self._markers = {str(m.get("id")): m for m in self._load_day(trade_date) if m.get("id")}
            # 重启恢复的历史标记一律按确认态处理，避免重复预警
            for marker in self._markers.values():
                marker["state"] = "confirmed"
            self._warn_hits = {}
            self._tape = {}
            self._price = {}
            self._pool = []
            self._limit_ups = set()
            self._sell_gate_done = any(
                str(m.get("rule", "")).startswith("opening7_sell_gate")
                or str(m.get("rule", "")) == "opening7_avoid_chase"
                for m in self._markers.values()
            )
            self._buy_done = any(
                str(m.get("rule", "")).startswith("opening7_buy") for m in self._markers.values()
            )

    # ----------------------------------------------------------- pool
    def _build_pool(self, context: Any, quotes: dict[str, Any]) -> list[dict[str, Any]]:
        sector_by_code = {signal.code: signal.sector for signal in getattr(context, "signals_all", [])}
        sector_of = {}
        for sector in getattr(context, "sectors", []) or []:
            sector_of[sector.name] = sector

        pool: dict[str, dict[str, Any]] = {}

        def add(code: str, origin: str) -> None:
            quote = quotes.get(code)
            if quote is None or getattr(quote, "limit_up", False):
                return  # 涨停票动态剔除：买不进，信号无意义
            if code in pool:
                pool[code]["origins"].append(origin)
                return
            pool[code] = {
                "code": code,
                "name": getattr(quote, "name", ""),
                "sector": sector_by_code.get(code, ""),
                "origins": [origin],
            }

        sectors = sorted(
            getattr(context, "sectors", []) or [],
            key=lambda s: float(getattr(s, "heat_score", 0) or 0),
            reverse=True,
        )[: max(1, int(self.settings.opening_window_pool_sector_top))]
        member_cap = max(1, int(self.settings.opening_window_pool_sector_members))
        for sector in sectors:
            members = [
                quote
                for quote in quotes.values()
                if sector_by_code.get(quote.code) == sector.name and not getattr(quote, "limit_up", False)
            ]
            members.sort(key=lambda q: float(getattr(q, "amount", 0) or 0), reverse=True)
            for quote in members[:member_cap]:
                add(quote.code, f"sector:{sector.name}")

        actives = sorted(
            (q for q in quotes.values() if not getattr(q, "limit_up", False)),
            key=lambda q: float(getattr(q, "amount", 0) or 0),
            reverse=True,
        )[: max(5, int(self.settings.opening_window_pool_board_top))]
        for quote in actives:
            add(quote.code, "board_top")

        for item in getattr(context, "watchlist", []) or []:
            add(str(getattr(item, "code", "") or ""), "watchlist")

        return list(pool.values())

    # ---------------------------------------------------- state updates
    def _update_price_state(self, code: str, quote: Any) -> None:
        state = self._price.setdefault(code, PriceState())
        session_high = float(getattr(quote, "high", 0) or 0)
        state.prev_session_high = state.session_high or session_high
        state.session_high = max(state.session_high, session_high)
        price = float(getattr(quote, "price", 0) or 0)
        vwap = self._quote_vwap(quote)
        if price > 0 and vwap > 0:
            state.max_vwap_excess_pct = max(state.max_vwap_excess_pct, (price / vwap - 1) * 100)

    @staticmethod
    def _quote_vwap(quote: Any) -> float:
        amount = float(getattr(quote, "amount", 0) or 0)
        volume = float(getattr(quote, "volume", 0) or 0)
        if amount <= 0 or volume <= 0:
            return 0.0
        return amount / (volume * 100)

    def _accumulate_tape(self, pool: list[dict[str, Any]], trade_date: str) -> None:
        count = max(400, int(self.settings.opening_window_tape_count))
        workers = max(2, int(self.settings.opening_window_tape_workers))

        def fetch(code: str) -> Any:
            try:
                return self._tape_fetcher(code, trade_date, count=count)
            except TypeError:
                return self._tape_fetcher(code, trade_date)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="opening-tape") as executor:
            results = list(executor.map(fetch, [entry["code"] for entry in pool]))
        for entry, observation in zip(pool, results):
            if observation is None or not getattr(observation, "available", False):
                continue
            acc = self._tape.setdefault(entry["code"], TapeAccumulator())
            acc.update(getattr(observation, "points", []) or [])

    # ------------------------------------------------------ marker utils
    def _emit(self, marker: dict[str, Any]) -> None:
        with self._lock:
            existing = self._markers.get(marker["id"])
            if existing is not None and existing.get("state") == "confirmed":
                return
            self._markers[marker["id"]] = marker
            self._dirty = True

    def _remove_warn(self, marker_id: str) -> None:
        with self._lock:
            marker = self._markers.get(marker_id)
            if marker is not None and marker.get("state") == "warn":
                self._markers.pop(marker_id, None)
                self._dirty = True

    def _tape_for(self, code: str) -> TapeAccumulator | None:
        return self._tape.get(code)

    def _base_marker(
        self,
        *,
        code: str,
        name: str,
        sector: str,
        time_label: str,
        now: datetime,
        side: str,
        rule: str,
        label: str,
        price: float,
        change_pct: float,
        reasons: list[str],
        tape_net_ratio: float | None,
        state: str,
        regime: str = "",
    ) -> dict[str, Any]:
        acc = self._tape.get(code)
        quality = "live_l1" if acc is not None and acc.ready else "quote_proxy"
        if acc is not None and acc.gap:
            quality = "live_l1_partial"
        return {
            "id": f"{self._trade_date}|{code}|{rule}",
            "trade_date": self._trade_date,
            "code": code,
            "name": name,
            "sector": sector,
            "time": time_label,
            "first_seen": now.strftime("%H:%M:%S"),
            "side": side,
            "rule": rule,
            "label": label,
            "price": round(float(price or 0), 3),
            "change_pct": round(float(change_pct or 0), 2),
            "regime": regime,
            "reasons": reasons,
            "tape_net_ratio": round(tape_net_ratio, 1) if tape_net_ratio is not None else None,
            "state": state,
            "source_quality": quality,
            "validation_status": "research_only",
            "executable": False,
        }

    # ---------------------------------------------------- opening7 events
    def _eval_sell_gate(self, now: datetime, pool: list[dict[str, Any]], quotes: dict[str, Any]) -> None:
        emitted = 0
        for entry in pool:
            quote = quotes.get(entry["code"])
            if quote is None:
                continue
            prev_close = float(getattr(quote, "prev_close", 0) or 0)
            open_price = float(getattr(quote, "open", 0) or 0)
            price = float(getattr(quote, "price", 0) or 0)
            if prev_close <= 0 or open_price <= 0 or price <= 0:
                continue
            gap_pct = (open_price / prev_close - 1) * 100
            if gap_pct < OPENING7_RULES["sell_gate_gap_min_pct"]:
                continue  # 免费预筛：只有高开票才查分笔
            acc = self._tape_for(entry["code"])
            if acc is None or not acc.ready:
                continue  # 分笔未就绪，下一 tick 再试
            net = acc.net_ratio(upto="09:30")
            if net is None or net > OPENING7_RULES["sell_gate_net_max_pct"]:
                continue
            position = bool(self._position_checker(entry["code"]))
            rule = "opening7_sell_gate" if position else "opening7_avoid_chase"
            label = "开盘卖出" if position else "回避追高"
            if position:
                reasons = [
                    f"开盘卖出闸门：高开 {gap_pct:+.2f}% 且大单净买比 {net:+.1f}%（09:31）",
                    "研究样本：该组合全天收阴率约6成、收盘均值-1.2~-1.7%（震荡市样本）",
                ]
            else:
                reasons = [
                    f"开盘高点回避：高开 {gap_pct:+.2f}% 且大单净买比 {net:+.1f}%（09:31），无持仓不追，等回落再看",
                    "研究样本：该组合全天收阴率约6成、收盘均值-1.2~-1.7%（震荡市样本）",
                ]
            self._emit(self._base_marker(
                code=entry["code"], name=entry["name"], sector=entry["sector"],
                time_label=SELL_GATE_CLOCK, now=now, side="sell", rule=rule, label=label,
                price=price, change_pct=(price / prev_close - 1) * 100,
                reasons=reasons, tape_net_ratio=net, state="confirmed",
            ))
            emitted += 1
        if emitted or now.strftime("%H:%M") >= BUY_CLOCK:
            # 09:33 后不再重试卖出闸门（彼时 09:30 分笔已滑出窗口的票放弃）
            self._sell_gate_done = True

    def _eval_opening_buy(self, now: datetime, context: Any, pool: list[dict[str, Any]], quotes: dict[str, Any]) -> None:
        indices = list(getattr(context.market, "indices", []) or [])
        index_snapshot = next((i for i in indices if str(getattr(i, "code", "")) == "000001"), indices[0] if indices else None)
        if index_snapshot is None:
            self._buy_done = True
            return
        idx_change = float(getattr(index_snapshot, "change_pct", 0) or 0)
        regime = classify_regime(idx_change)
        emitted = 0
        for entry in pool:
            quote = quotes.get(entry["code"])
            if quote is None:
                continue
            prev_close = float(getattr(quote, "prev_close", 0) or 0)
            open_price = float(getattr(quote, "open", 0) or 0)
            price = float(getattr(quote, "price", 0) or 0)
            if prev_close <= 0 or open_price <= 0 or price <= 0:
                continue
            gap_pct = (open_price / prev_close - 1) * 100
            from_open = (price / open_price - 1) * 100
            if gap_pct >= OPENING7_RULES["buy_chase_gap_max_pct"] or from_open >= OPENING7_RULES["buy_chase_from_open_max_pct"]:
                continue  # 追高排除
            acc = self._tape_for(entry["code"])
            net = acc.net_ratio(upto="09:32") if acc is not None and acc.ready else None
            buy = False
            reason = ""
            if regime == "strong_low_open":
                buy = True
                reason = f"强低开抢反弹：指数 {idx_change:+.2f}%（研究样本该制度池票30分钟均值+3.1%）"
            elif regime == "low_open":
                if net is not None:
                    buy = net > OPENING7_RULES["buy_low_open_net_min_pct"]
                    reason = f"低开修复：指数 {idx_change:+.2f}%，大单净买比 {net:+.1f}% 无重抛压" if buy else ""
                else:
                    buy = from_open > OPENING7_RULES["low_open_fallback_from_open_min_pct"]
                    reason = f"低开修复（无分笔，用价格承接代理）：指数 {idx_change:+.2f}%，开盘跌幅 {from_open:+.2f}%" if buy else ""
            elif regime == "flat_open":
                vwap = self._quote_vwap(quote)
                if net is not None and vwap > 0:
                    buy = net >= OPENING7_RULES["buy_flat_net_min_pct"] and price >= vwap
                    reason = f"平稳开精选：大单净买比 {net:+.1f}% 且站上日内VWAP" if buy else ""
            # high_open: never chase
            if not buy:
                continue
            reasons = [f"09:33买入决策：{reason}"]
            if net is not None:
                reasons.append(f"大单净买比 {net:+.1f}%（L1成交明细，非委托队列）")
            self._emit(self._base_marker(
                code=entry["code"], name=entry["name"], sector=entry["sector"],
                time_label=BUY_CLOCK, now=now, side="buy",
                rule=f"opening7_buy_{regime}", label="开盘买入",
                price=price, change_pct=(price / prev_close - 1) * 100,
                reasons=reasons, tape_net_ratio=net, state="confirmed", regime=regime,
            ))
            emitted += 1
        if emitted or now.strftime("%H:%M") >= "09:36":
            self._buy_done = True

    # ---------------------------------------------------- extended rules
    def _eval_extended(self, clock: str, pool: list[dict[str, Any]], quotes: dict[str, Any]) -> None:
        confirm_ticks = max(1, int(self.settings.opening_window_warn_confirm_ticks))
        active_warns: set[str] = set()
        for entry in pool:
            quote = quotes.get(entry["code"])
            if quote is None:
                continue
            code = entry["code"]
            prev_close = float(getattr(quote, "prev_close", 0) or 0)
            open_price = float(getattr(quote, "open", 0) or 0)
            price = float(getattr(quote, "price", 0) or 0)
            if prev_close <= 0 or price <= 0:
                continue
            state = self._price.get(code) or PriceState()
            acc = self._tape_for(code)
            inp = RuleInput(
                code=code,
                clock=clock,
                price=price,
                open_price=open_price,
                prev_close=prev_close,
                session_high=state.session_high or float(getattr(quote, "high", 0) or 0),
                prev_session_high=state.prev_session_high,
                vwap=self._quote_vwap(quote),
                tape_net_ratio=acc.net_ratio() if acc is not None and acc.ready else None,
                tape_ready=bool(acc is not None and acc.ready),
            )
            candidates = [
                evaluate_high_avoid(inp),
                evaluate_vwap_pullback_buy(inp, max_vwap_excess_pct=state.max_vwap_excess_pct),
                evaluate_low_open_recovery(inp),
            ]
            for candidate in candidates:
                if candidate is None:
                    continue
                marker_id = f"{self._trade_date}|{code}|{candidate.rule}"
                active_warns.add(marker_id)
                with self._lock:
                    existing = self._markers.get(marker_id)
                    if existing is not None and existing.get("state") == "confirmed":
                        continue
                    hits = self._warn_hits.get(marker_id, 0) + 1
                    self._warn_hits[marker_id] = hits
                confirmed = hits >= confirm_ticks
                if existing is None:
                    self._emit(self._base_marker(
                        code=code, name=entry["name"], sector=entry["sector"],
                        time_label=clock, now=china_now(), side=candidate.side,
                        rule=candidate.rule, label=RULE_LABELS.get(candidate.rule, candidate.rule),
                        price=candidate.price, change_pct=candidate.change_pct,
                        reasons=candidate.reasons, tape_net_ratio=candidate.tape_net_ratio,
                        state="confirmed" if confirmed else "warn",
                    ))
                elif confirmed and existing.get("state") == "warn":
                    with self._lock:
                        existing["state"] = "confirmed"
                        existing["confirmed_at"] = china_now().strftime("%H:%M:%S")
                        existing["price"] = round(float(candidate.price or 0), 3)
                        existing["change_pct"] = round(float(candidate.change_pct or 0), 2)
                        existing["reasons"] = candidate.reasons
                        if candidate.tape_net_ratio is not None:
                            existing["tape_net_ratio"] = round(candidate.tape_net_ratio, 1)
                        self._dirty = True

        # 预警条件消失且尚未确认的标记移出队列
        with self._lock:
            stale = [
                marker_id
                for marker_id, marker in self._markers.items()
                if marker.get("state") == "warn" and marker_id not in active_warns
            ]
        for marker_id in stale:
            self._remove_warn(marker_id)


__all__ = ["OpeningWindowEngine", "TapeAccumulator"]
