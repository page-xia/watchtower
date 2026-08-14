from __future__ import annotations

"""Auditable intraday T-strategy event study.

This module is deliberately separate from the online dashboard signal path.
The dashboard may use a frozen daily sector snapshot, while a research run must
only use values available at the candidate minute.  Historical easy_tdx data also
has an important limitation: standard servers provide L1 trade prints and
five displayed levels, but not historical queue data.  The report
keeps that distinction explicit.
"""

import csv
import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.config import AppSettings, load_yaml
from app.data_sources import (
    DataSourceError,
    EasyTdxDailyDataSource,
    StockMeta,
    _records_from_payload,
    easy_tdx_market_for_code,
)
from app.formula_engine import (
    DEFAULT_TREND_NEAR_THRESHOLD_PCT,
    compute_formula_series,
    compute_trend_line_series,
    evaluate_l1_flow,
)
from app.opening_strategy import CHECKPOINTS, OpeningStrategy
from app.risk_reward import RiskRewardEvaluator
from app.research_artifacts import build_compact_research_report
from app.research_protocol import (
    ProtocolConfig,
    ResearchSample,
    compute_daily_regime,
    protocol_study,
)
from app.trajectory_store import IntradayWatchtowerStore


def _session_times(count: int = 240) -> list[str]:
    times: list[str] = []
    hour, minute = 9, 31
    while hour < 11 or (hour == 11 and minute <= 30):
        times.append(f"{hour:02d}:{minute:02d}")
        minute += 1
        if minute >= 60:
            hour += 1
            minute = 0
    hour, minute = 13, 1
    while hour < 15 or (hour == 15 and minute <= 0):
        times.append(f"{hour:02d}:{minute:02d}")
        minute += 1
        if minute >= 60:
            hour += 1
            minute = 0
        if hour > 15:
            break
    return times[:count]


SESSION_TIMES = _session_times(240)
INDEX_CODE = "000001"
CACHE_SCHEMA_VERSION = 4
FORMULA_TREND_THRESHOLDS_PCT = (0.25, 0.35, 0.50, 0.70, 1.00, DEFAULT_TREND_NEAR_THRESHOLD_PCT)
FORMULA_OUTCOME_HORIZONS = (5, 15, 30)


def _finite(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _median(values: Iterable[float], fallback: float = 0.0) -> float:
    clean = [value for value in (_finite(item) for item in values) if value > 0]
    return statistics.median(clean) if clean else fallback


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _pct(current: float, reference: float) -> float:
    return (current - reference) / reference * 100 if reference else 0.0


def _limit_pct(code: str, market: str = "") -> float:
    code = str(code).zfill(6)
    if code.startswith("92") or market == "北交所":
        return 30.0
    if code.startswith(("300", "301", "688")) or market in {"创业板", "科创板"}:
        return 20.0
    return 10.0


def _valid_code(value: Any) -> bool:
    code = str(value or "").strip().zfill(6)
    return len(code) == 6 and code.isdigit() and not code.startswith(("2", "9"))


def _stock_time_index(label: str) -> int | None:
    """Map a trade print to its completed one-minute session bucket.

    TDX minute bars are close-labelled (the first bar is 09:31), while a
    transaction stamped 09:30 belongs to that first bucket.  Returning 240 at
    exactly 15:00 lets the consumer explicitly clamp it to the final bar and
    avoids shifting every afternoon print one minute early.
    """
    text = str(label or "")[:5]
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
    except (TypeError, ValueError):
        return None
    total = hour * 60 + minute
    if 9 * 60 + 30 <= total <= 11 * 60 + 30:
        return min(total - (9 * 60 + 30), 119)
    if 13 * 60 <= total <= 15 * 60:
        return 120 + total - 13 * 60
    return None


@dataclass(frozen=True)
class ResearchConfig:
    dates: tuple[str, ...] = ("20260806", "20260807")
    selection_date: str = "20260805"
    sample_size: int = 100
    sample_pool_multiplier: float = 1.50
    industry_width: int = 4
    flow_threshold: float = 18.0
    large_flow_threshold: float = 8.0
    volume_ratio_threshold: float = 1.25
    same_minute_volume_ratio_threshold: float = 1.35
    cumulative_volume_ratio_threshold: float = 1.15
    index_volume_ratio_threshold: float = 1.08
    index_rebound_threshold: float = 0.15
    price_slope_threshold: float = 0.08
    sector_breadth_threshold: float = 0.50
    sector_change_threshold: float = 0.0
    sector_core_ratio_threshold: float = 1.25
    sector_core_slope_threshold: float = 0.05
    rebound_min: float = 0.5
    rebound_max: float = 7.0
    # Historical minute bars are coarser than the live 3-5 second snapshots.
    # Keep a five-minute evidence window so "core first, index next, follower
    # last" is treated as one sequence instead of requiring one candle.
    confluence_window: int = 5
    candidate_cooldown: int = 8
    flow_window: int = 3
    strategy_flow_pressure_threshold: float = -25.0
    strategy_flow_support_threshold: float = 18.0
    strategy_flow_overheat_threshold: float = 65.0
    volume_window: int = 5
    outcome_horizons: tuple[int, ...] = (5, 15, 30)
    friction_pct: float = 0.20
    good_mfe_30: float = 1.50
    good_mae_15: float = -1.00
    good_net_30: float = 0.20
    stop_loss_pct: float = -2.0
    sell_rebound_pct: float = 8.0
    sell_near_high_pullback_pct: float = 1.5
    sell_min_hold_bars: int = 15
    include_transactions: bool = True
    min_candidate_index: int = 3
    v3_target_ratios: tuple[float, ...] = (1.2, 1.5, 1.8, 2.2)
    v3_dynamic_exit_score: int = 40
    v3_dynamic_exit_factors: int = 2
    v3_rearm_bars: int = 30
    v3_max_trades_per_stock_day: int = 2
    v3_no_progress_bars: int = 10
    v3_no_progress_r: float = 0.45
    v3_late_entry_index: int = 210
    sector_min_members: int = 3
    sector_lead_breadth_threshold: float = 0.30
    sector_ignition_cooldown: int = 8
    protocol: str = "research_first"
    protocol_minimum_days: int = 20
    protocol_oos_days: int = 60
    protocol_minimum_events: int = 30
    daily_history_sessions: int = 60
    formula_trend_thresholds_pct: tuple[float, ...] = FORMULA_TREND_THRESHOLDS_PCT


@dataclass
class HistoricalFeed:
    """Small resumable easy_tdx client used only by the offline study."""

    settings: AppSettings
    cache_dir: Path
    timeout: float = 1.5
    transaction_page_size: int = 1800
    max_transaction_pages: int = 5
    api: Any = None
    preferred_host: tuple[str, int] | None = None
    hosts: tuple[tuple[str, int], ...] = (
        ("119.147.212.81", 7709),
        ("47.103.48.45", 7709),
        ("106.14.95.149", 7709),
        ("115.238.56.198", 7709),
    )

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.minute_dir = self.cache_dir / "minute"
        self.transaction_dir = self.cache_dir / "transactions"
        self.auction_dir = self.cache_dir / "auction"
        self.minute_dir.mkdir(parents=True, exist_ok=True)
        self.transaction_dir.mkdir(parents=True, exist_ok=True)
        self.auction_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        if self.api is not None:
            try:
                close = getattr(self.api, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
        self.api = None

    def _connect(self) -> Any:
        if self.api is not None:
            return self.api
        try:
            from easy_tdx import TdxClient
        except Exception as exc:  # pragma: no cover - optional dependency
            raise DataSourceError("easy_tdx未安装，无法运行真实回放研究") from exc
        last_error: Exception | None = None
        client = None
        for host, port in self.hosts:
            candidate = TdxClient(host=host, port=port, timeout=self.timeout, heartbeat_interval=15.0)
            try:
                candidate.connect()
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
                try:
                    candidate.close()
                except Exception:
                    pass
                continue
            client = candidate
            break
        if client is None:
            raise DataSourceError(f"easy_tdx研究连接失败：{last_error}") from last_error
        self.api = client
        return client

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        client = self._connect()
        try:
            return getattr(client, method)(*args, **kwargs)
        except Exception:
            self.close()
            client = self._connect()
            return getattr(client, method)(*args, **kwargs)

    def minute(self, code: str, trade_date: str, *, index: bool = False) -> list[dict[str, Any]]:
        normalized = str(code).zfill(6)
        path = self.minute_dir / f"{trade_date}_{'index' if index else 'stock'}_{normalized}.json"
        cached = self._read_rows(path, trade_date, normalized)
        if cached is not None:
            return cached
        raw = self._minute_page(normalized, trade_date, index=index)
        points = list(raw if isinstance(raw, list) else getattr(raw, "points", ()) or getattr(raw, "rows", ()) or [])
        rows = [self._normalize_bar(row) for row in points]
        # Align unlabeled historical bars by their returned session position.
        for index, row in enumerate(rows):
            row.setdefault("bar_index", index)
            if not row.get("time") and index < len(SESSION_TIMES):
                row["time"] = SESSION_TIMES[index]
        if not self._sane_bars(rows):
            rows = []
        self._write_rows(path, trade_date, normalized, rows)
        return rows

    def transactions(self, code: str, trade_date: str) -> list[dict[str, Any]]:
        normalized = str(code).zfill(6)
        path = self.transaction_dir / f"{trade_date}_{normalized}.json"
        cached = self._read_rows(path, trade_date, normalized)
        if cached is not None:
            return cached

        rows: list[dict[str, Any]] = []
        for page in range(self.max_transaction_pages):
            offset = page * self.transaction_page_size
            page_data = self._transaction_page(normalized, trade_date, start=offset, count=self.transaction_page_size)
            batch = _records_from_payload(page_data)
            normalized_batch = [
                self._normalize_transaction(
                    row,
                    source_sequence=offset + index,
                    source_page=page,
                )
                for index, row in enumerate(batch)
            ]
            rows.extend(normalized_batch)
            if len(normalized_batch) < self.transaction_page_size:
                break
            # A tiny pause avoids repeatedly hitting one public quote server.
            time.sleep(0.02)

        filtered: list[dict[str, Any]] = []
        for row in rows:
            idx = _stock_time_index(str(row.get("time") or ""))
            if idx is None:
                continue
            # Identical-looking prints can be separate real transactions.
            # Keep them all; removing them by value would bias the flow score.
            filtered.append(row)
        # Keep the provider's page/offset metadata as the audit order while
        # making minute buckets chronological for the feature extractor.  The
        # original sequence is never discarded or deduplicated.
        filtered.sort(
            key=lambda row: (
                str(row.get("time") or ""),
                _finite(row.get("source_sequence"), 10**12),
            )
        )
        self._write_rows(path, trade_date, normalized, filtered)
        return filtered

    def auction_0925(self, code: str, trade_date: str) -> dict[str, Any]:
        """Fetch and cache the 09:25 auction prior from historical transaction tape."""
        normalized = str(code).zfill(6)
        path = self.auction_dir / f"{trade_date}_{normalized}.json"
        cached = self._read_rows(path, trade_date, normalized)
        if cached is not None:
            return dict(cached[0]) if cached else {
                "available": False,
                "source": "unavailable",
                "note": "L1历史成交09:25代理缓存为空",
            }
        row = self._fetch_auction_0925(normalized, trade_date)
        self._write_rows(path, trade_date, normalized, [row])
        return dict(row)

    def _market_arg(self, code: str, *, index: bool = False, api: Any | None = None) -> Any:
        return easy_tdx_market_for_code(code, index=index)

    def _minute_page(self, code: str, trade_date: str, *, index: bool = False) -> Any:
        client = self._connect()
        if hasattr(client, "get_history_minute_time_data"):
            market = self._market_arg(code, index=index, api=client)
            return client.get_history_minute_time_data(market, code, int(trade_date))
        raise DataSourceError("easy_tdx未安装，无法运行真实回放研究")

    def _transaction_page(self, code: str, trade_date: str, *, start: int, count: int) -> Any:
        client = self._connect()
        if hasattr(client, "get_history_transaction_data"):
            market = self._market_arg(code, api=client)
            return client.get_history_transaction_data(market, code, int(trade_date), start=start, count=count)
        raise DataSourceError("easy_tdx未安装，无法运行真实回放研究")

    def _fetch_auction_0925(self, code: str, trade_date: str) -> dict[str, Any]:
        errors: list[str] = []
        try:
            via_tape = self._auction_0925_from_transaction_pages(code, trade_date)
            if via_tape.get("available"):
                return via_tape
            errors.append(str(via_tape.get("note") or "历史逐笔未找到09:25成交"))
        except Exception as exc:  # pragma: no cover - provider dependent
            errors.append(f"历史逐笔09:25扫描失败：{exc}")
            self.close()
        return {
            "available": False,
            "source": "unavailable",
            "note": "；".join(errors) or "L1历史成交未返回09:25竞价代理数据",
        }

    def _auction_0925_from_transaction_pages(self, code: str, trade_date: str) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for page in range(self.max_transaction_pages):
            offset = page * self.transaction_page_size
            page_data = self._transaction_page(code, trade_date, start=offset, count=self.transaction_page_size)
            batch = _records_from_payload(page_data)
            for index, row in enumerate(batch):
                normalized = self._normalize_transaction(
                    row,
                    source_sequence=offset + index,
                    source_page=page,
                )
                if str(normalized.get("raw_time") or normalized.get("time") or "")[:5] == "09:25":
                    rows.append(normalized)
            if len(batch) < self.transaction_page_size:
                break
        if not rows:
            return {
                "available": False,
                "source": "easy_tdx_history_transaction_data",
                "note": "L1历史成交未找到09:25竞价成交",
            }
        price = 0.0
        previous_price = 0.0
        total_volume = 0.0
        buy_amount = 0.0
        sell_amount = 0.0
        for row in rows:
            row_price = _finite(row.get("price"))
            volume = max(0.0, _finite(row.get("vol")))
            total_volume += volume
            side = self._transaction_side(row, previous_price=previous_price)
            amount = row_price * volume * 100 if row_price > 0 else 0.0
            if side == "buy":
                buy_amount += amount
            elif side == "sell":
                sell_amount += amount
            if row_price > 0:
                price = row_price
                previous_price = row_price
        directional = buy_amount + sell_amount
        imbalance = (buy_amount - sell_amount) / directional * 100 if directional else 0.0
        if price <= 0 or total_volume <= 0:
            return {
                "available": False,
                "source": "easy_tdx_history_transaction_data",
                "note": "easy_tdx历史09:25成交缺少有效价格或成交量",
            }
        return {
            "available": True,
            "source": "easy_tdx_history_transaction_data",
            "as_of": "09:25",
            "match_price": price,
            "price": price,
            "volume": total_volume * 100,
            "amount": price * total_volume * 100,
            "order_imbalance_pct": imbalance,
            "data_quality": "proxy",
            "note": "L1历史成交09:25回填；方向只作成交代理",
        }

    @staticmethod
    def _transaction_side(row: Mapping[str, Any], *, previous_price: float = 0.0) -> str:
        raw_direction = row.get("side")
        if isinstance(raw_direction, str):
            lowered = raw_direction.lower()
            if "buy" in lowered or "买" in raw_direction:
                return "buy"
            if "sell" in lowered or "卖" in raw_direction:
                return "sell"
        raw_direction = row.get("buyorsell")
        try:
            direction = int(raw_direction) if raw_direction is not None else None
        except (TypeError, ValueError):
            direction = None
        if direction == 0:
            return "buy"
        if direction == 1:
            return "sell"
        price = _finite(row.get("price"))
        if previous_price and price > previous_price:
            return "buy"
        if previous_price and price < previous_price:
            return "sell"
        return "neutral"

    @staticmethod
    def _normalize_bar(row: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "price": _finite(_row_value(row, "price")),
            "vol": max(0.0, _finite(_row_value(row, "vol", _row_value(row, "volume")))),
            "amount": max(0.0, _finite(_row_value(row, "amount", _row_value(row, "open_interest")))),
        }
        raw_time = str(
            _row_value(row, "time_label")
            or _row_value(row, "time")
            or _row_value(row, "datetime")
            or _row_value(row, "date")
            or ""
        )
        time_label = (raw_time.split()[-1] if " " in raw_time else raw_time)[:5]
        if time_label:
            normalized["time"] = time_label
        if raw_time:
            normalized["raw_time"] = raw_time
        return normalized

    @staticmethod
    def _normalize_transaction(
        row: Any,
        *,
        source_sequence: int | None = None,
        source_page: int | None = None,
    ) -> dict[str, Any]:
        normalized = {
            "time": str(_row_value(row, "time_label") or _row_value(row, "time") or "")[:5],
            "price": _finite(_row_value(row, "price")),
            "vol": max(0.0, _finite(_row_value(row, "vol", _row_value(row, "volume")))),
            "buyorsell": _row_value(row, "buyorsell"),
        }
        if not normalized["time"]:
            hour = _row_value(row, "hour")
            minute = _row_value(row, "minute")
            second = _row_value(row, "second", 0)
            if hour is not None and minute is not None:
                try:
                    normalized["time"] = f"{int(hour):02d}:{int(minute):02d}"
                    normalized["raw_time"] = f"{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"
                except (TypeError, ValueError):
                    pass
        side = _row_value(row, "side")
        if side is not None:
            normalized["side"] = side
        nature = _row_value(row, "nature")
        if normalized["buyorsell"] is None and nature is not None:
            normalized["buyorsell"] = nature
            normalized["nature"] = nature
        status_raw = _row_value(row, "status_raw")
        if status_raw is not None:
            normalized["status_raw"] = status_raw
        if source_sequence is not None:
            normalized.update(
                {
                    "source_sequence": int(source_sequence),
                    "source_page": int(source_page or 0),
                    "source_offset": int(source_sequence),
                    "raw_time": str(_row_value(row, "time_label") or _row_value(row, "time") or ""),
                }
            )
        return normalized

    @staticmethod
    def _sane_bars(rows: list[dict[str, Any]]) -> bool:
        prices = [float(row.get("price") or 0) for row in rows if float(row.get("price") or 0) > 0]
        if len(prices) < 200:
            return False
        median = _median(prices)
        return bool(median and min(prices) >= median * 0.4 and max(prices) <= median * 2.5)

    @staticmethod
    def _read_rows(path: Path, trade_date: str, code: str) -> list[dict[str, Any]] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        try:
            schema_version = int(payload.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        if (
            str(payload.get("trade_date")) != str(trade_date)
            or str(payload.get("code")) != str(code)
            or schema_version < CACHE_SCHEMA_VERSION
        ):
            return None
        rows = payload.get("rows")
        return rows if isinstance(rows, list) else None

    @staticmethod
    def _write_rows(path: Path, trade_date: str, code: str, rows: list[dict[str, Any]]) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "trade_date": trade_date,
                    "code": code,
                    "fetched_at": datetime.now().isoformat(),
                    "rows": rows,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


@dataclass
class StockSeries:
    code: str
    name: str
    industry: str
    market: str
    prev_close: float
    previous_day_amount: float
    previous_bars: list[dict[str, Any]]
    bars: list[dict[str, Any]]
    transactions: list[dict[str, Any]]
    themes: list[str] = field(default_factory=list)
    context_only: bool = False
    metrics: list[dict[str, Any]] = field(default_factory=list)
    trade_date: str = ""
    daily_history: list[dict[str, Any]] = field(default_factory=list)


class StrategyResearcher:
    """Run a cross-sectional, minute-by-minute event study."""

    def __init__(self, settings: AppSettings | None = None, config: ResearchConfig | None = None) -> None:
        self.settings = settings or AppSettings()
        self.config = config or ResearchConfig()
        self.rules = load_yaml(self.settings.rules_file, {}).get("thresholds", {})
        self.themes = load_yaml(self.settings.themes_file, {}).get("themes", [])
        self.manual_theme_members = {
            str(theme.get("name") or "").strip(): {
                str(code).zfill(6)
                for code in [*(theme.get("members", []) or []), *(theme.get("core_codes", []) or [])]
                if _valid_code(code)
            }
            for theme in self.themes
            if str(theme.get("name") or "").strip()
        }
        self.manual_theme_core = {
            str(theme.get("name") or "").strip(): {
                str(code).zfill(6) for code in (theme.get("core_codes", []) or []) if _valid_code(code)
            }
            for theme in self.themes
            if str(theme.get("name") or "").strip()
        }
        self.runtime_dir = self.settings.data_dir / "runtime" / "strategy-research"
        self.cache_dir = self.runtime_dir / "cache"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.feed = HistoricalFeed(self.settings, self.cache_dir, timeout=self.settings.easy_tdx_timeout_seconds)
        self.research_store = IntradayWatchtowerStore(
            getattr(self.settings, "intraday_watchtower_db_file", self.settings.data_dir / "runtime" / "intraday_watchtower.sqlite"),
            enabled=bool(getattr(self.settings, "trajectory_enabled", True)),
        )
        self.opening_strategy = OpeningStrategy(self.rules)
        self.risk_reward = RiskRewardEvaluator(self.rules)
        self.metadata = self._load_metadata()
        self.daily: dict[str, dict[str, dict[str, Any]]] = {}
        self.daily_history_status: dict[str, Any] = {
            "status": "not_loaded",
            "required_sessions": max(20, int(self.config.daily_history_sessions)),
            "loaded_sessions": 0,
            "dates": [],
            "source": "unavailable",
            "error": "",
        }

    def close(self) -> None:
        self.feed.close()
        store = getattr(self, "research_store", None)
        if store is not None:
            store.close()

    def run(self) -> dict[str, Any]:
        run_started_at = datetime.now().isoformat(timespec="seconds")
        try:
            target_dates = sorted({str(value) for value in self.config.dates if str(value)})
            for date in target_dates:
                self._ensure_daily(date)
            self._prepare_daily_history(target_dates)
            for date in target_dates:
                self._ensure_daily(self._previous_date(date))
            selection_pool = self.select_sample()
            selection = self._finalize_non_one_word_sample(selection_pool, target_dates)
            # Keep the legacy V2 comparison universe compatible with the
            # earlier "non-one-word" replay request, but give the research
            # protocol its own ex-ante universe.  A target-day one-word board
            # must remain in the protocol as an explicit ``no_fill`` outcome;
            # removing it after looking at the target day's OHLC would bias
            # the execution study.
            protocol_selections = self._protocol_selection_schedule(target_dates)
            all_candidates: list[dict[str, Any]] = []
            all_formula_events: list[dict[str, Any]] = []
            all_sell_zones: list[dict[str, Any]] = []
            all_trades: list[dict[str, Any]] = []
            all_v3_trades: list[dict[str, Any]] = []
            all_v3_rejections: list[dict[str, Any]] = []
            all_v3_stress: dict[float, list[dict[str, Any]]] = defaultdict(list)
            all_opening: list[dict[str, Any]] = []
            protocol_samples: list[ResearchSample] = []
            date_summaries: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            sample_codes = set(selection["codes"])
            # The legacy comparison keeps its original fixed universe.  The
            # research protocol chooses a fresh ex-ante universe for every
            # target day from that day's immediately preceding session.
            base_context_codes = {
                str(code).zfill(6)
                for code in selection.get("pool_codes", [])
                if _valid_code(code)
            }
            base_context_codes.update(sample_codes)

            index_series: dict[str, list[dict[str, Any]]] = {}
            for date in target_dates:
                previous_date = self._previous_date(date)
                protocol_selection = protocol_selections.get(date, {})
                protocol_sample_codes = {
                    str(code).zfill(6)
                    for code in protocol_selection.get("codes", [])
                    if _valid_code(code)
                }
                protocol_context_codes = {
                    str(code).zfill(6)
                    for code in protocol_selection.get("pool_codes", protocol_sample_codes)
                    if _valid_code(code)
                }
                protocol_context_codes.update(protocol_sample_codes)
                measured_codes = sample_codes | protocol_sample_codes
                # The larger ex-ante pool is context only.  It lets a sampled
                # stock observe independent peers without treating its own move
                # as proof that the whole sector is strong.
                context_codes = set(base_context_codes)
                context_codes.update(protocol_context_codes)
                context_codes.update(protocol_sample_codes)
                for members in self.manual_theme_members.values():
                    context_codes.update(members)
                self._ensure_daily(date)
                self._ensure_daily(previous_date)
                index_rows = self.feed.minute(INDEX_CODE, date, index=True)
                previous_index_rows = self.feed.minute(INDEX_CODE, previous_date, index=True)
                if not index_rows:
                    skipped.append({"date": date, "reason": "缺少上证指数分钟历史"})
                    continue
                market_metrics = self._market_metrics(index_rows, previous_index_rows, date)

                series_by_code: dict[str, StockSeries] = {}
                for code in sorted(context_codes):
                    meta = self.metadata.get(code)
                    row = self.daily[date].get(code)
                    prior = self.daily[previous_date].get(code)
                    if not meta or not row or not prior:
                        skipped.append({"date": date, "code": code, "reason": "缺少日线基准"})
                        continue
                    bars = self.feed.minute(code, date)
                    prior_bars = self.feed.minute(code, previous_date)
                    if not bars:
                        skipped.append({"date": date, "code": code, "reason": "缺少个股分钟历史"})
                        continue
                    context_only = code not in measured_codes
                    transactions = self.feed.transactions(code, date) if self.flow_enabled and not context_only else []
                    series = StockSeries(
                        code=code,
                        name=str(meta.get("name") or code),
                        industry=str(meta.get("industry") or "未分类"),
                        market=str(meta.get("market") or ""),
                        prev_close=_finite(row.get("pre_close")),
                        previous_day_amount=_finite(prior.get("amount")) * 1000,
                        previous_bars=prior_bars,
                        bars=bars,
                        transactions=transactions,
                        themes=[name for name, members in self.manual_theme_members.items() if code in members],
                        context_only=context_only,
                        trade_date=date,
                        daily_history=self._daily_history_for_code(code, date),
                    )
                    series.metrics = self._stock_metrics(series)
                    series_by_code[code] = series

                market_metrics = self._augment_market_context(market_metrics, series_by_code)
                index_series[date] = market_metrics
                protocol_samples.extend(
                    self._protocol_samples_for_day(
                        date,
                        series_by_code,
                        market_rows=index_rows,
                        eligible_codes=protocol_sample_codes,
                        context_codes=protocol_context_codes,
                    )
                )

                candidates, sell_zones, trades = self._study_day(
                    date,
                    previous_date,
                    series_by_code,
                    market_metrics,
                    eligible_codes=sample_codes,
                )
                formula_events = self._formula_events_for_day(
                    date,
                    previous_date,
                    series_by_code,
                    eligible_codes=protocol_sample_codes,
                )
                v3_result = self._simulate_v3_trades(
                    date,
                    series_by_code,
                    market_metrics,
                    candidates,
                )
                opening_records = self._study_opening_day(
                    date,
                    series_by_code,
                    market_metrics,
                    eligible_codes=sample_codes,
                )
                all_candidates.extend(candidates)
                all_formula_events.extend(formula_events)
                all_sell_zones.extend(sell_zones)
                all_trades.extend(trades)
                all_v3_trades.extend(v3_result["trades"])
                all_v3_rejections.extend(v3_result["rejections"])
                for ratio, rows in v3_result["stress_trades"].items():
                    all_v3_stress[ratio].extend(rows)
                all_opening.extend(opening_records)
                date_summaries.append(
                    {
                        "date": date,
                        "previous_date": previous_date,
                        "requested_stocks": len(selection["codes"]),
                        "usable_stocks": sum(1 for code in sample_codes if code in series_by_code),
                        "context_stocks": max(0, len(series_by_code) - sum(1 for code in sample_codes if code in series_by_code)),
                        "index_minutes": len(index_rows),
                        "transaction_coverage": round(
                            sum(1 for code in sample_codes if code in series_by_code and series_by_code[code].transactions)
                            / max(sum(1 for code in sample_codes if code in series_by_code), 1),
                            3,
                        ),
                        "candidate_events": len(candidates),
                        "formula_events": len(formula_events),
                        "strict_four_factor_events": sum(1 for item in candidates if item["strict_four_factor"]),
                        "sell_zone_events": len(sell_zones),
                        "trades": len(trades),
                        "v3_trades": len(v3_result["trades"]),
                        "v3_rr_rejections": len(v3_result["rejections"]),
                        "opening_events": len(opening_records),
                        "opening_candidates": sum(
                            1 for item in opening_records if item.get("action") in {"竞价候选", "初筛候选"}
                        ),
                        "opening_buy_events": sum(1 for item in opening_records if item.get("action") == "确认买T"),
                        "opening_defense_events": sum(
                            1 for item in opening_records if item.get("action") in {"回避", "减T"}
                        ),
                    }
                )

            report = self._build_report(
                selection,
                date_summaries,
                skipped,
                all_candidates,
                all_sell_zones,
                all_trades,
                formula_candidates=all_formula_events,
                opening_records=all_opening,
                v3_trades=all_v3_trades,
                v3_rejections=all_v3_rejections,
                v3_stress_trades=dict(all_v3_stress),
            )
            protocol_report = protocol_study(
                protocol_samples,
                config=ProtocolConfig(
                    minimum_days=max(20, int(self.config.protocol_minimum_days)),
                    out_of_sample_days=max(60, int(self.config.protocol_oos_days)),
                    minimum_events=max(30, int(self.config.protocol_minimum_events)),
                ),
            )
            protocol_report["sample"]["selection"] = self._protocol_selection_summary(
                protocol_selections
            )
            protocol_report["sample"]["one_word_policy"] = (
                "按前一交易日可见流动性和行业分层选取；目标日一字板保留并标记no_fill，不能事后删除"
            )
            protocol_report["data_quality"]["daily_history"] = dict(
                self.daily_history_status
            )
            protocol_report["started_at"] = run_started_at
            protocol_report["finished_at"] = datetime.now().isoformat(timespec="seconds")
            report["research_protocol"] = protocol_report
            report["research_status"] = protocol_report.get("validation", {}).get("status", "research_only")
            persistence_error = ""
            try:
                self.research_store.record_protocol_report(protocol_report)
            except Exception as exc:
                # The JSON/Markdown report remains the source artifact even if a
                # local SQLite projection is temporarily locked or unavailable.
                persistence_error = str(exc)
            report["research_persistence"] = {
                "db_file": str(getattr(self.research_store, "path", "")),
                "persisted": not bool(persistence_error),
                "error": persistence_error,
            }
            self._write_report(report)
            return report
        finally:
            self.close()

    @property
    def flow_enabled(self) -> bool:
        return bool(getattr(self.config, "include_transactions", True))

    def select_sample(self, selection_date: str | None = None) -> dict[str, Any]:
        actual_selection_date = str(selection_date or self.config.selection_date)
        self._ensure_daily(actual_selection_date)
        rows = list(self.daily[actual_selection_date].values())
        eligible: list[dict[str, Any]] = []
        selection_date_one_word: list[dict[str, str]] = []
        for row in rows:
            if not self._eligible_row(row):
                continue
            code = str(row.get("symbol") or "").zfill(6)
            one_word_reason = self._one_word_reason(row, code)
            if one_word_reason:
                selection_date_one_word.append(
                    {
                        "code": code,
                        "date": actual_selection_date,
                        "reason": one_word_reason,
                    }
                )
                continue
            eligible.append(row)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in eligible:
            groups[str(self.metadata.get(str(row.get("symbol")), {}).get("industry") or "未分类")].append(row)
        ranked_industries = sorted(
            groups,
            key=lambda name: sum(_finite(item.get("amount")) for item in groups[name]),
            reverse=True,
        )
        pool_target = max(
            self.config.sample_size,
            math.ceil(self.config.sample_size * max(1.0, self.config.sample_pool_multiplier)),
        )
        target_industries = max(1, math.ceil(pool_target / max(1, self.config.industry_width)))
        selected: list[dict[str, Any]] = []
        selected_industries: list[str] = []
        for industry in ranked_industries:
            members = sorted(groups[industry], key=lambda item: _finite(item.get("amount")), reverse=True)
            if len(members) < 2:
                continue
            selected.extend(members[: self.config.industry_width])
            selected_industries.append(industry)
            if len(selected_industries) >= target_industries:
                break
        selected = selected[:pool_target]

        selected_codes = {str(row.get("symbol")).zfill(6) for row in selected}
        # Configured core membership is ex-ante metadata, not a same-day winner
        # filter. Include it only when it belongs to the selected industries.
        configured_core = {
            str(code).zfill(6)
            for theme in self.themes
            for code in theme.get("core_codes", [])
            if _valid_code(code)
        }
        for code in sorted(configured_core):
            row = self.daily[actual_selection_date].get(code)
            industry = str(self.metadata.get(code, {}).get("industry") or "未分类")
            if (
                row
                and self._eligible_row(row)
                and not self._one_word_reason(row, code)
                and industry in selected_industries
                and code not in selected_codes
            ):
                replacement = next(
                    (item for item in reversed(selected) if str(item.get("symbol")).zfill(6) in selected_codes and str(self.metadata.get(str(item.get("symbol")).zfill(6), {}).get("industry") or "未分类") == industry),
                    None,
                )
                if replacement:
                    selected.remove(replacement)
                    selected_codes.remove(str(replacement.get("symbol")).zfill(6))
                    selected.append(row)
                    selected_codes.add(code)

        selected.sort(key=lambda row: _finite(row.get("amount")), reverse=True)
        return {
            "selection_date": actual_selection_date,
            "method": (
                f"前一交易日成交额排序；按成交额最高的{len(selected_industries)}个行业"
                f"各取前{self.config.industry_width}只构建{len(selected)}只事前候选池；"
                "配置核心票只作事前元数据替换"
            ),
            "requested": self.config.sample_size,
            "count": len(selected),
            "sample_pool_count": len(selected),
            "industries": selected_industries,
            "codes": [str(row.get("symbol")).zfill(6) for row in selected],
            "pool_codes": [str(row.get("symbol")).zfill(6) for row in selected],
            "selection_date_one_word_exclusions": selection_date_one_word,
            "items": [
                {
                    "code": str(row.get("symbol")).zfill(6),
                    "name": str(self.metadata.get(str(row.get("symbol")).zfill(6), {}).get("name") or row.get("symbol")),
                    "industry": str(self.metadata.get(str(row.get("symbol")).zfill(6), {}).get("industry") or "未分类"),
                    "selection_amount": round(_finite(row.get("amount")) * 1000, 2),
                    "selection_change_pct": round(_finite(row.get("pct_chg")), 2),
                }
                for row in selected
            ],
        }

    def _finalize_non_one_word_sample(
        self,
        selection_pool: dict[str, Any],
        target_dates: list[str],
    ) -> dict[str, Any]:
        """Keep the ex-ante liquidity order while removing target-day one-word bars.

        Looking at target-day OHLC is used only to enforce the user's requested
        research universe.  It never changes the ordering of the remaining
        stocks and no future return is consulted.
        """
        selected: list[dict[str, Any]] = []
        one_word_details: list[dict[str, str]] = []
        unavailable_details: list[dict[str, str]] = []
        for item in selection_pool.get("items", []):
            code = str(item.get("code") or "").zfill(6)
            excluded = False
            for trade_date in target_dates:
                row = self.daily.get(trade_date, {}).get(code)
                if not row:
                    unavailable_details.append(
                        {"code": code, "date": trade_date, "reason": "缺少目标交易日日线"}
                    )
                    excluded = True
                    break
                reason = self._one_word_reason(row, code)
                if reason:
                    one_word_details.append(
                        {
                            "code": code,
                            "name": str(item.get("name") or code),
                            "date": trade_date,
                            "reason": reason,
                        }
                    )
                    excluded = True
                    break
            if excluded:
                continue
            selected.append(dict(item))
            if len(selected) >= self.config.sample_size:
                break

        result = dict(selection_pool)
        result.update(
            {
                "count": len(selected),
                "usable_sample_count": len(selected),
                "codes": [str(item.get("code") or "").zfill(6) for item in selected],
                "items": selected,
                "excluded_one_word_count": len(one_word_details),
                "excluded_one_word_codes": sorted({item["code"] for item in one_word_details}),
                "excluded_one_word": one_word_details,
                "excluded_unavailable": unavailable_details,
                "non_one_word_rule": (
                    "目标日开高低收振幅不超过0.08%，且收盘接近对应10%/20%/30%涨跌停幅度；"
                    "先按前一日流动性排好候选池，再剔除，不按目标日收益重排"
                ),
                "method": (
                    str(selection_pool.get("method") or "")
                    + f"；目标日剔除一字板后按原顺序取前{len(selected)}只"
                ),
            }
        )
        return result

    def _protocol_selection(self, selection_pool: dict[str, Any]) -> dict[str, Any]:
        """Return the point-in-time universe used by the research protocol.

        This selection is intentionally made only from the configured
        selection-day snapshot.  Target-day OHLC is not consulted to choose or
        remove names.  The old event-study report can still expose its
        non-one-word comparison universe separately, while the protocol keeps
        one-word/no-fill observations for an honest fill-rate estimate.
        """

        items = [dict(item) for item in selection_pool.get("items", []) or []]
        items = items[: max(0, int(self.config.sample_size))]
        codes = [str(item.get("code") or "").zfill(6) for item in items if _valid_code(item.get("code"))]
        return {
            "selection_date": str(selection_pool.get("selection_date") or self.config.selection_date),
            "method": (
                "只按目标日前可见的流动性和行业分层取样；不使用目标日涨跌筛选；"
                "一字板与无法成交样本保留并标记no_fill"
            ),
            "requested": int(self.config.sample_size),
            "count": len(codes),
            "codes": codes,
            "items": [item for item in items if str(item.get("code") or "").zfill(6) in set(codes)],
            "one_word_retained": True,
            "future_filter_used": False,
        }

    def _protocol_selection_schedule(
        self,
        target_dates: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Build one ex-ante sample for each target session.

        Reusing a single selection date across a long historical range leaks
        future liquidity and industry membership into earlier sessions.  Each
        target date therefore receives its own sample based only on the prior
        trading day.  Target-day OHLC is never consulted here.
        """

        schedule: dict[str, dict[str, Any]] = {}
        for target_date in sorted({str(value) for value in target_dates if str(value)}):
            selection_date = self._previous_date(target_date)
            pool = self.select_sample(selection_date=selection_date)
            selection = self._protocol_selection(pool)
            selection.update(
                {
                    "target_date": target_date,
                    "selection_date": selection_date,
                    "pool_codes": [
                        str(code).zfill(6)
                        for code in pool.get("pool_codes", [])
                        if _valid_code(code)
                    ],
                }
            )
            schedule[target_date] = selection
        return schedule

    @staticmethod
    def _protocol_selection_summary(
        schedule: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        rows = [
            {
                "target_date": str(target_date),
                "selection_date": str(selection.get("selection_date") or ""),
                "count": int(selection.get("count") or 0),
                "codes": [str(code).zfill(6) for code in selection.get("codes", [])],
            }
            for target_date, selection in sorted(schedule.items())
        ]
        distinct_codes = sorted(
            {
                code
                for row in rows
                for code in row["codes"]
                if _valid_code(code)
            }
        )
        return {
            "method": (
                "每个目标日使用其前一交易日可见的成交额和行业分层独立选样；"
                "不使用目标日涨跌筛选；一字板与无法成交样本保留为no_fill"
            ),
            "per_date": rows,
            "date_count": len(rows),
            "stock_day_count": sum(row["count"] for row in rows),
            "distinct_code_count": len(distinct_codes),
            "distinct_codes": distinct_codes,
            "one_word_retained": True,
            "future_filter_used": False,
        }

    def _one_word_reason(self, row: dict[str, Any], code: str) -> str:
        prices = [_finite(row.get(key)) for key in ("open", "high", "low", "close")]
        previous = _finite(row.get("pre_close"))
        if previous <= 0 or any(price <= 0 for price in prices):
            return ""
        open_price, high, low, close = prices
        spread_pct = (max(prices) - min(prices)) / previous * 100
        limit_pct = _limit_pct(code, str(self.metadata.get(code, {}).get("market") or ""))
        close_change = _pct(close, previous)
        at_limit = abs(abs(close_change) - limit_pct) <= 0.45
        open_at_limit = abs(abs(_pct(open_price, previous)) - limit_pct) <= 0.45
        if spread_pct <= 0.08 and at_limit and open_at_limit:
            direction = "涨停" if close_change > 0 else "跌停"
            return f"{direction}一字板：开高低收几乎一致，振幅{spread_pct:.3f}%"
        return ""

    def _study_opening_day(
        self,
        trade_date: str,
        series_by_code: dict[str, StockSeries],
        market_metrics: list[dict[str, Any]],
        *,
        eligible_codes: set[str],
    ) -> list[dict[str, Any]]:
        """Replay the fixed 09:33/09:35/09:37 checkpoints.

        Every feature passed to ``OpeningStrategy`` is sliced through the
        checkpoint index.  In particular, sector breadth and average change
        are rebuilt from the members' observations available at that minute;
        no end-of-day winner information enters the decision.
        """
        by_industry: dict[str, list[StockSeries]] = defaultdict(list)
        for series in series_by_code.values():
            by_industry[series.industry].append(series)
        sector_groups: dict[str, list[StockSeries]] = dict(by_industry)
        for theme_name, members in self.manual_theme_members.items():
            themed = [series for code, series in series_by_code.items() if code in members]
            if themed:
                sector_groups[theme_name] = themed
        sector_metrics = {
            name: self._sector_metrics(
                members,
                len(market_metrics),
                configured_core_codes=self.manual_theme_core.get(name, set()),
            )
            for name, members in sector_groups.items()
        }
        checkpoint_indices: dict[str, int] = {}
        for checkpoint in CHECKPOINTS:
            checkpoint_indices[checkpoint] = next(
                (idx for idx, row in enumerate(market_metrics) if str(row.get("time") or "")[:5] == checkpoint),
                {"09:33": 2, "09:35": 4, "09:37": 6}.get(checkpoint, 0),
            )

        records: list[dict[str, Any]] = []
        for checkpoint in CHECKPOINTS:
            idx = checkpoint_indices[checkpoint]
            if idx < 0 or idx >= len(market_metrics):
                continue
            market_row = dict(market_metrics[idx])
            visible_metrics = [
                series.metrics[idx]
                for series in series_by_code.values()
                if idx < len(series.metrics)
            ]
            changes = [_finite(row.get("change_pct")) for row in visible_metrics]
            market_row.update(
                {
                    "breadth": sum(1 for value in changes if value > 0) / max(1, len(changes)) * 100,
                    "emotion": _clamp(
                        50 + (sum(1 for value in changes if value > 0) - sum(1 for value in changes if value < 0))
                        / max(1, len(changes))
                        * 50,
                        0,
                        100,
                    ),
                    "index_volume_ratio": _finite(market_row.get("amount_ratio"), 1),
                    "index_recovering": bool(
                        _finite(market_row.get("rebound")) >= 0.08
                        or _finite(market_row.get("slope3")) >= 0.01
                    ),
                }
            )
            for series in series_by_code.values():
                if series.code not in eligible_codes or idx >= len(series.metrics):
                    continue
                stock_metric = dict(series.metrics[idx])
                first_open = (
                    _finite(series.bars[0].get("price"), series.prev_close)
                    if series.bars
                    else series.prev_close
                )
                stock_metric.update(
                    {
                        "code": series.code,
                        "name": series.name,
                        "open": first_open,
                        "prev_close": series.prev_close,
                        "vwap": _finite(stock_metric.get("vwap"), first_open),
                    }
                )
                sector_name, sector_row = self._best_sector_context(
                    [series.industry, *series.themes],
                    sector_metrics,
                    idx,
                )
                # ``_sector_metrics`` is an offline research representation:
                # breadth is a fraction and ``score`` is its composite
                # momentum score.  The online opening scorer consumes the
                # terminal contract (percentage breadth + heat_score), so
                # translate the point-in-time row explicitly here.  This is
                # derived only from members through ``idx`` and does not use
                # end-of-day winners.
                sector_row = self._opening_sector_features(sector_row, market_row)
                result = self.opening_strategy.evaluate_historical_point(
                    trade_date=trade_date,
                    checkpoint=checkpoint,
                    stock=stock_metric,
                    market=market_row,
                    sector=sector_row,
                    sector_name=sector_name,
                )
                item = result["item"]
                records.append(
                    {
                        "trade_date": trade_date,
                        "checkpoint": checkpoint,
                        "time": checkpoint,
                        "index": idx,
                        "code": series.code,
                        "name": series.name,
                        "industry": series.industry,
                        "action": item.get("action", "观察"),
                        "can_execute": bool(item.get("can_execute")),
                        "score": int(item.get("score", 0)),
                        "market_gate": bool(item.get("market_gate")),
                        "sector_gate": bool(item.get("sector_gate")),
                        "stock_gate": bool(item.get("stock_gate")),
                        "reasons": list(item.get("reasons") or []),
                        "risks": list(item.get("risks") or []),
                        "metrics": {
                            "market_score": result["market"].get("score", 0),
                            "sector_score": result["sector"].get("score", 0),
                            "sector_breadth": result["sector"].get("breadth", 0),
                            "index_amount_ratio": market_row.get("amount_ratio", 1),
                            "amount_ratio": stock_metric.get("amount_ratio", 1),
                            "same_minute_amount_ratio": stock_metric.get("same_minute_amount_ratio", 1),
                            "flow_score": stock_metric.get("flow_score", 0),
                            "flow_is_l1": bool(stock_metric.get("flow_available")),
                            "rebound": stock_metric.get("rebound", 0),
                        },
                        "outcome": self._outcome(series.metrics, idx),
                    }
                )
        records.sort(key=lambda item: (item["trade_date"], item["checkpoint"], -item["score"], item["code"]))
        return records

    @staticmethod
    def _opening_sector_features(
        row: Mapping[str, Any],
        market_row: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Translate a point-in-time research sector row to opening inputs."""
        breadth = _finite(row.get("breadth"))
        breadth_pct = breadth * 100 if 0 <= breadth <= 1 else breadth
        avg_change = _finite(row.get("avg_change"))
        core_attack = bool(row.get("core_attack"))
        limit_count = int(_finite(row.get("limit_up_count")))
        opened_count = int(_finite(row.get("opened_limit_count")))
        # Keep this in sync with SignalEngine's public heat-score contract.
        heat_score = _clamp(
            18
            + breadth_pct * 0.28
            + max(0, min((avg_change + 1.5) * 5.2, 28))
            + max(0, min(limit_count * 8 + opened_count * 4, 20))
            + (18 if core_attack else 0)
            + (8 if bool(market_row.get("turning")) else 0),
            0,
            100,
        )
        return {
            **dict(row),
            "heat_score": round(heat_score, 2),
            "breadth": breadth_pct,
            "avg_change_pct": avg_change,
            "flow_delta": _finite(row.get("flow_delta"), 0),
        }

    def _study_day(
        self,
        trade_date: str,
        previous_date: str,
        series_by_code: dict[str, StockSeries],
        market_metrics: list[dict[str, Any]],
        *,
        eligible_codes: set[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        by_industry: dict[str, list[StockSeries]] = defaultdict(list)
        for series in series_by_code.values():
            by_industry[series.industry].append(series)
        sector_groups: dict[str, list[StockSeries]] = dict(by_industry)
        for theme_name, members in self.manual_theme_members.items():
            themed = [series for code, series in series_by_code.items() if code in members]
            if themed:
                sector_groups[theme_name] = themed
        sector_metrics = {
            name: self._sector_metrics(
                members,
                len(market_metrics),
                configured_core_codes=self.manual_theme_core.get(name, set()),
            )
            for name, members in sector_groups.items()
        }
        candidates: list[dict[str, Any]] = []
        sell_zones: list[dict[str, Any]] = []
        for series in series_by_code.values():
            if series.code not in eligible_codes:
                continue
            sector_names = [series.industry, *series.themes]
            last_candidate = -999
            last_v2_candidate = -999
            last_sell = -999
            for idx, metric in enumerate(series.metrics):
                market = market_metrics[min(idx, len(market_metrics) - 1)] if market_metrics else {}
                sector_name, sector_at = self._best_sector_context(sector_names, sector_metrics, idx)
                sector_history = sector_metrics.get(sector_name, [])
                factors = self._factor_state(series, idx, metric, market, sector_at)
                factors["sector_name"] = sector_name
                factors.update(self._market_confluence_state(market_metrics, idx))
                sector_window = sector_history[
                    max(0, idx - self.config.confluence_window + 1) : idx + 1
                ]
                independent_confirm_rows = [
                    row
                    for row in sector_window
                    if row.get("confirmed")
                    and (
                        any(code != series.code for code in row.get("attack_codes", []))
                        or any(code != series.code for code in row.get("limit_up_codes", []))
                        or sum(code != series.code for code in row.get("up_codes", [])) >= 2
                    )
                ]
                ignition_rows = [row for row in sector_window if row.get("ignition")]
                self_core_ignition = any(
                    series.code in row.get("attack_codes", [])
                    for row in ignition_rows
                )
                independent_core_attack = any(
                    any(code != series.code for code in row.get("attack_codes", []))
                    for row in sector_window
                )
                independent_limit_up = any(
                    any(code != series.code for code in row.get("limit_up_codes", []))
                    for row in sector_window
                )
                independent_up_count = max(
                    (
                        sum(code != series.code for code in row.get("up_codes", []))
                        for row in sector_window
                    ),
                    default=0,
                )
                recent_sector = bool(independent_confirm_rows)
                factors.update(
                    {
                        "sector_is_manual": sector_name in self.manual_theme_members,
                        "sector_recent": recent_sector,
                        "sector_confirmation_recent": recent_sector,
                        "sector_ignition_recent": bool(ignition_rows),
                        "self_core_ignition": self_core_ignition,
                        "independent_core_attack_recent": independent_core_attack,
                        "independent_limit_up_recent": independent_limit_up,
                        "independent_up_count_recent": independent_up_count,
                        "sector_ignition_age": int(_finite(sector_at.get("ignition_age"), 999)),
                        "sector_impulse_id": max(
                            (int(_finite(row.get("ignition_id"))) for row in sector_window),
                            default=int(_finite(sector_at.get("ignition_id"))),
                        ),
                    }
                )
                factors["strict_four_factor"] = bool(
                    factors["market_recent"]
                    and recent_sector
                    and factors["volume_factor"]
                    and factors["flow_factor"]
                    and factors["stock_setup"]
                )
                factors["factor_count"] = sum(
                    bool(factors.get(name)) for name in ("market_recent", "sector_recent", "volume_factor", "flow_factor")
                )
                if factors["sector_ignition_recent"] and not factors["sector_recent"]:
                    factors["factor_count"] += 1
                candidate_ready = bool(
                    idx >= self.config.min_candidate_index
                    and idx + 1 + max(self.config.outcome_horizons) < len(series.metrics)
                    and factors["stock_setup"]
                    and factors["factor_count"] >= 2
                )
                strategy_v2 = self._strategy_v2_state(factors)
                regular_candidate_due = idx - last_candidate >= self.config.candidate_cooldown
                # A weaker two-factor observation must not hide the first full
                # V2 setup that appears inside its cooldown window.
                v2_candidate_due = bool(
                    strategy_v2["hard_ready"]
                    and idx - last_v2_candidate >= self.config.candidate_cooldown
                )
                if candidate_ready and (regular_candidate_due or v2_candidate_due):
                    event = self._candidate_record(series, trade_date, previous_date, idx, factors)
                    candidates.append(event)
                    last_candidate = idx
                    if strategy_v2["hard_ready"]:
                        last_v2_candidate = idx

                sell = self._sell_state(series, idx, metric, market, sector_at)
                if (
                    sell["trigger"]
                    and idx + 1 + min(self.config.outcome_horizons) < len(series.metrics)
                    and idx - last_sell >= self.config.candidate_cooldown
                ):
                    sell_zones.append(self._sell_record(series, trade_date, idx, sell))
                    last_sell = idx

        candidates.sort(key=lambda item: (item["trade_date"], item["time"], -item["factor_count"], item["code"]))
        sell_zones.sort(key=lambda item: (item["trade_date"], item["time"], item["code"]))
        trades = self._simulate_trades(trade_date, series_by_code, candidates, sell_zones)
        return candidates, sell_zones, trades

    @staticmethod
    def _formula_point(metrics: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize formula/L1 fields without consulting a legacy decision."""

        def first_finite(*keys: str) -> float | None:
            for key in keys:
                if key not in metrics or metrics.get(key) in (None, ""):
                    continue
                try:
                    value = float(metrics.get(key))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    return value
            return None

        def first_bool(*keys: str) -> tuple[bool, bool]:
            for key in keys:
                if key in metrics:
                    return bool(metrics.get(key)), True
            return False, False

        quick_entry, quick_entry_present = first_bool(
            "formula_quick_entry",
            "quick_entry",
            "fast_trigger",
            "赶快出手",
        )
        main_absorption = first_finite(
            "formula_main_absorption",
            "main_absorption",
            "main_accumulation",
            "主力吸筹",
        )
        trend_distance = first_finite(
            "formula_trend_distance_pct",
            "trend_distance_pct",
            "趋势线最近距离_pct",
        )
        if trend_distance is None:
            line_distances = [
                value
                for value in (
                    first_finite("formula_white_distance_pct", "white_distance_pct", "白线距离_pct"),
                    first_finite("formula_yellow_distance_pct", "yellow_distance_pct", "黄线距离_pct"),
                )
                if value is not None
            ]
            trend_distance = min(line_distances) if line_distances else None

        l1_buy_support, support_present = first_bool(
            "l1_buy_support",
            "formula_l1_buy_support",
        )
        l1_sell_pressure, pressure_present = first_bool(
            "l1_sell_pressure",
            "formula_l1_sell_pressure",
        )
        indicators_present = bool(support_present or pressure_present)
        if "l1_indicators_present" in metrics:
            indicators_present = bool(metrics.get("l1_indicators_present"))
        l1_available = bool(
            metrics.get("l1_available")
            or metrics.get("flow_available")
            or l1_buy_support
            or l1_sell_pressure
        )
        quick_absorption = bool(
            quick_entry
            and main_absorption is not None
            and main_absorption > 0
        )
        return {
            "ready": bool(
                (quick_entry_present and main_absorption is not None)
                or trend_distance is not None
            ),
            "quick_entry": quick_entry,
            "main_absorption": main_absorption,
            "quick_absorption": quick_absorption,
            "trend_distance_pct": trend_distance,
            "l1_available": l1_available,
            "l1_indicators_present": indicators_present,
            "l1_buy_support": l1_buy_support,
            "l1_sell_pressure": l1_sell_pressure,
        }

    def _formula_events_for_day(
        self,
        trade_date: str,
        previous_date: str,
        series_by_code: Mapping[str, StockSeries],
        *,
        eligible_codes: set[str],
    ) -> list[dict[str, Any]]:
        """Collect formula observations independently of the legacy V2/V3 universe."""

        events: list[dict[str, Any]] = []
        max_threshold = max(FORMULA_TREND_THRESHOLDS_PCT)
        max_horizon = max(FORMULA_OUTCOME_HORIZONS)
        for series in series_by_code.values():
            if series.code not in eligible_codes:
                continue
            for idx, metric in enumerate(series.metrics):
                if idx + 1 + max_horizon > len(series.metrics):
                    continue
                point = self._formula_point(metric)
                trend_hit = bool(
                    point["trend_distance_pct"] is not None
                    and point["trend_distance_pct"] <= max_threshold
                )
                if not point["ready"] or not (point["quick_absorption"] or trend_hit):
                    continue
                event_metrics = {
                    key: metric.get(key)
                    for key in (
                        "formula_white_line",
                        "formula_yellow_line",
                        "formula_white_distance_pct",
                        "formula_yellow_distance_pct",
                        "formula_near_trend_line_name",
                        "flow_is_l1",
                        "flow_available",
                        "transaction_count",
                    )
                    if key in metric
                }
                event_metrics.update(
                    {
                        "formula_quick_entry": point["quick_entry"],
                        "formula_main_absorption": point["main_absorption"],
                        "l1_available": point["l1_available"],
                        "l1_indicators_present": point["l1_indicators_present"],
                        "l1_buy_support": point["l1_buy_support"],
                        "l1_sell_pressure": point["l1_sell_pressure"],
                    }
                )
                if point["trend_distance_pct"] is not None:
                    event_metrics["formula_trend_distance_pct"] = point["trend_distance_pct"]
                events.append(
                    {
                        "trade_date": trade_date,
                        "previous_date": previous_date,
                        "code": series.code,
                        "name": series.name,
                        "industry": series.industry,
                        "time": str(metric.get("time") or SESSION_TIMES[min(idx, len(SESSION_TIMES) - 1)]),
                        "index": idx,
                        "research_status": "research_only",
                        "metrics": event_metrics,
                        "outcome": self._outcome(series.metrics, idx),
                    }
                )
        events.sort(key=lambda item: (item["trade_date"], item["time"], item["code"]))
        return events

    def _factor_state(
        self,
        series: StockSeries,
        idx: int,
        metric: dict[str, Any],
        market: dict[str, Any],
        sector: dict[str, Any],
    ) -> dict[str, Any]:
        flow_score = _finite(metric.get("flow_score"))
        large_imbalance = _finite(metric.get("large_imbalance"))
        flow_factor = bool(flow_score >= self.config.flow_threshold or large_imbalance >= self.config.large_flow_threshold)
        volume_factor = bool(_finite(metric.get("amount_ratio"), 1) >= self.config.volume_ratio_threshold)
        volume_factor = bool(
            volume_factor
            or _finite(metric.get("same_minute_amount_ratio"), 1) >= self.config.same_minute_volume_ratio_threshold
            or _finite(metric.get("cumulative_amount_ratio"), 1) >= self.config.cumulative_volume_ratio_threshold
        )
        stock_setup = bool(
            self.config.rebound_min <= _finite(metric.get("rebound")) <= self.config.rebound_max
            and _finite(metric.get("slope3")) >= self.config.price_slope_threshold
            and (
                _finite(metric.get("price")) >= _finite(metric.get("vwap"))
                or bool(metric.get("formula_support"))
            )
        )
        core_codes = [str(code).zfill(6) for code in sector.get("core_codes", [])]
        attack_codes = [str(code).zfill(6) for code in sector.get("attack_codes", [])]
        return {
            "market_factor": bool(market.get("turning") and market.get("amount_expanding")),
            "market_turn_factor": bool(market.get("turning")),
            "market_volume_factor": bool(market.get("amount_expanding")),
            "sector_factor": bool(sector.get("confirmed")),
            "volume_factor": volume_factor,
            "flow_factor": flow_factor,
            "stock_setup": stock_setup,
            "flow_score": round(flow_score, 2),
            "large_imbalance": round(large_imbalance, 2),
            "flow_source": str(metric.get("flow_source") or "minute_price_amount_proxy"),
            "flow_is_l1": bool(metric.get("flow_available")),
            "transaction_count": int(_finite(metric.get("transaction_count"))),
            "l1_available": bool(metric.get("l1_available") or metric.get("flow_available")),
            "l1_indicators_present": bool(metric.get("l1_indicators_present")),
            "l1_buy_support": bool(metric.get("l1_buy_support")),
            "l1_sell_pressure": bool(metric.get("l1_sell_pressure")),
            "amount_ratio": round(_finite(metric.get("amount_ratio"), 1), 2),
            "same_minute_amount_ratio": round(_finite(metric.get("same_minute_amount_ratio"), 1), 2),
            "cumulative_amount_ratio": round(_finite(metric.get("cumulative_amount_ratio"), 1), 2),
            "sector_score": round(_finite(sector.get("score")), 2),
            "sector_breadth": round(_finite(sector.get("breadth")), 3),
            "sector_breadth_delta": round(_finite(sector.get("breadth_delta")), 3),
            "sector_avg_change": round(_finite(sector.get("avg_change")), 3),
            "sector_momentum": round(_finite(sector.get("momentum")), 3),
            "sector_member_count": int(_finite(sector.get("member_count"))),
            "is_sector_core": series.code in core_codes,
            "self_core_attack": series.code in attack_codes,
            "index_amount_ratio": round(_finite(market.get("amount_ratio"), 1), 2),
            "index_slope3": round(_finite(market.get("slope3")), 3),
            "context_breadth": round(_finite(market.get("context_breadth")), 3),
            "context_breadth_delta": round(_finite(market.get("context_breadth_delta")), 3),
            "context_strong_breadth": round(_finite(market.get("context_strong_breadth")), 3),
            "context_advancing_amount_share": round(
                _finite(market.get("context_advancing_amount_share")), 3
            ),
            "context_advancing_amount_delta": round(
                _finite(market.get("context_advancing_amount_delta")), 3
            ),
            "rebound": round(_finite(metric.get("rebound")), 3),
            "price_slope3": round(_finite(metric.get("slope3")), 3),
            "formula_support": bool(metric.get("formula_support")),
            "formula_exhaustion": bool(metric.get("formula_exhaustion")),
            "formula_trend_score": round(_finite(metric.get("formula_trend_score"), 50.0), 3),
            "formula_quick_entry": bool(metric.get("formula_quick_entry")),
            "formula_main_absorption": round(_finite(metric.get("formula_main_absorption")), 6),
            "formula_white_line": round(_finite(metric.get("formula_white_line")), 6),
            "formula_yellow_line": round(_finite(metric.get("formula_yellow_line")), 6),
            "formula_white_distance_pct": round(_finite(metric.get("formula_white_distance_pct"), 999.0), 6),
            "formula_yellow_distance_pct": round(_finite(metric.get("formula_yellow_distance_pct"), 999.0), 6),
            "formula_trend_distance_pct": round(_finite(metric.get("formula_trend_distance_pct"), 999.0), 6),
            "formula_near_trend_line": bool(metric.get("formula_near_trend_line")),
            "formula_near_trend_line_name": str(metric.get("formula_near_trend_line_name") or ""),
            "formula_buy_candidate": bool(metric.get("formula_buy_candidate")),
            "formula_sell_candidate": bool(metric.get("formula_sell_candidate")),
            "limit_up": bool(metric.get("limit_up")),
            "core_attack": bool(sector.get("core_attack")),
        }

    def _strategy_v2_state(self, factors: dict[str, Any]) -> dict[str, Any]:
        """Apply the research-backed V2 role assignment.

        The market and sector are context gates.  A current stock volume/setup
        impulse is the execution gate.  L1 flow is intentionally a soft input:
        strong selling can veto a buy, moderate buying can upgrade confidence,
        and an unavailable feed falls back to a clearly labelled minute proxy.
        """
        hard_factors = {
            "market": bool(factors.get("market_recent")),
            "sector": bool(factors.get("sector_recent")),
            "volume": bool(factors.get("volume_factor")),
            "setup": bool(factors.get("stock_setup")),
        }
        flow_is_l1 = bool(factors.get("flow_is_l1"))
        flow_score = _finite(factors.get("flow_score"))
        large_imbalance = _finite(factors.get("large_imbalance"))
        flow_pressure = bool(
            flow_is_l1
            and (
                flow_score <= self.config.strategy_flow_pressure_threshold
                or large_imbalance <= self.config.strategy_flow_pressure_threshold
            )
        )
        flow_support = bool(
            flow_is_l1
            and self.config.strategy_flow_support_threshold <= flow_score < self.config.strategy_flow_overheat_threshold
            and not flow_pressure
        )
        hard_ready = all(hard_factors.values())
        market_ready = hard_factors["market"]
        stock_ready = hard_factors["setup"]
        volume_ready = hard_factors["volume"]
        execution_ready = bool(volume_ready or flow_support)
        leader_ignition = bool(
            market_ready
            and stock_ready
            and execution_ready
            and factors.get("self_core_ignition")
            and (flow_support or not flow_is_l1)
        )
        sector_transfer = bool(
            market_ready
            and stock_ready
            and execution_ready
            and factors.get("sector_ignition_recent")
            and (
                factors.get("independent_core_attack_recent")
                or factors.get("independent_limit_up_recent")
            )
            and _finite(factors.get("sector_breadth"))
            >= self.config.sector_lead_breadth_threshold
        )
        sector_confirmation = bool(
            market_ready
            and stock_ready
            and factors.get("sector_confirmation_recent", hard_factors["sector"])
            and execution_ready
        )
        if leader_ignition:
            entry_archetype = "容量核心先点火"
        elif sector_transfer:
            entry_archetype = "核心带动板块传导"
        elif sector_confirmation:
            entry_archetype = "板块确认后跟随"
        else:
            entry_archetype = "等待盘面"
        eligible = bool(
            not flow_pressure
            and (leader_ignition or sector_transfer or sector_confirmation)
        )
        if eligible and flow_support:
            grade = "A"
        elif eligible:
            grade = "B"
        else:
            grade = "观察"
        if flow_is_l1:
            flow_role = "支持" if flow_support else "抛压" if flow_pressure else "中性"
            flow_mode = "easy_tdx_history_transaction"
        else:
            flow_role = "分钟量价代理"
            flow_mode = "minute_price_amount_proxy"
        return {
            "eligible": eligible,
            "grade": grade,
            "hard_ready": hard_ready,
            "hard_factors": [name for name, enabled in hard_factors.items() if enabled],
            "missing_hard_factors": [name for name, enabled in hard_factors.items() if not enabled],
            "flow_is_l1": flow_is_l1,
            "flow_role": flow_role,
            "flow_mode": flow_mode,
            "flow_pressure": flow_pressure,
            "flow_support": flow_support,
            "entry_archetype": entry_archetype,
            "leader_ignition": leader_ignition,
            "sector_transfer": sector_transfer,
            "sector_confirmation": sector_confirmation,
            "formula_exhaustion_warning": bool(factors.get("formula_exhaustion")),
        }

    def _candidate_record(
        self,
        series: StockSeries,
        trade_date: str,
        previous_date: str,
        idx: int,
        factors: dict[str, Any],
    ) -> dict[str, Any]:
        outcome = self._outcome(series.metrics, idx)
        active_factors = [
            name
            for name, label in (
                ("market", "指数拐头+放量"),
                ("sector", "板块情绪/核心进攻"),
                ("volume", "个股分时放量"),
                ("flow", "历史逐笔成交代理" if factors.get("flow_is_l1") else "分钟量价流向代理"),
            )
            if factors.get(f"{name}_recent" if name in {"market", "sector"} else f"{name}_factor")
        ]
        reasons = []
        if factors.get("market_recent"):
            turn_time = str(factors.get("market_turn_time") or "")
            volume_time = str(factors.get("market_volume_time") or "")
            timing = (
                f"{turn_time}同时确认拐头和放量"
                if turn_time and turn_time == volume_time
                else f"拐头{turn_time or '窗口内'}、放量{volume_time or '窗口内'}"
            )
            reasons.append(
                f"指数{timing}，{self.config.confluence_window}分钟窗口共振，"
                f"窗口峰值量能{factors['index_window_max_amount_ratio']:.2f}倍"
            )
        if factors.get("sector_recent"):
            reasons.append(
                f"{factors.get('sector_name') or series.industry}板块确认，样本上涨{factors['sector_breadth'] * 100:.0f}%，核心进攻{'是' if factors['core_attack'] else '否'}"
            )
        if factors.get("volume_factor"):
            reasons.append(
                f"个股放量：局部{factors['amount_ratio']:.2f}倍/同比分钟{factors['same_minute_amount_ratio']:.2f}倍/累计节奏{factors['cumulative_amount_ratio']:.2f}倍"
            )
        if factors.get("flow_factor"):
            reasons.append(
                f"{('历史逐笔成交' if factors.get('flow_is_l1') else '分钟量价')}方向差{factors['flow_score']:+.1f}%，大额差{factors['large_imbalance']:+.1f}%"
            )
        if factors.get("formula_support"):
            reasons.append("公式思想：低位承接/短趋势转强")
        strategy_v2 = self._strategy_v2_state(factors)
        if strategy_v2["eligible"]:
            reasons.append(
                f"策略V3 {strategy_v2['entry_archetype']} · {strategy_v2['grade']}级："
                f"盘面顺序成立，盘口{strategy_v2['flow_role']}"
            )
        elif strategy_v2["flow_pressure"]:
            reasons.append("策略V2暂缓：历史逐笔成交方向出现明显抛压")
        return {
            "trade_date": trade_date,
            "previous_date": previous_date,
            "code": series.code,
            "name": series.name,
            "industry": series.industry,
            "time": SESSION_TIMES[min(idx, len(SESSION_TIMES) - 1)],
            "index": idx,
            "entry_time": SESSION_TIMES[min(idx + 1, len(SESSION_TIMES) - 1)] if idx + 1 < len(series.metrics) else "",
            "price": round(_finite(series.metrics[idx].get("price")), 4),
            "factor_count": int(factors.get("factor_count", 0)),
            "strict_four_factor": bool(factors.get("strict_four_factor")),
            "factors": active_factors,
            "reasons": reasons,
            "strategy_v2": strategy_v2,
            "metrics": {
                key: factors[key]
                for key in (
                    "flow_score",
                    "large_imbalance",
                    "flow_source",
                    "flow_is_l1",
                    "transaction_count",
                    "l1_available",
                    "l1_indicators_present",
                    "l1_buy_support",
                    "l1_sell_pressure",
                    "amount_ratio",
                    "same_minute_amount_ratio",
                    "cumulative_amount_ratio",
                    "sector_score",
                    "sector_name",
                    "sector_is_manual",
                    "sector_breadth",
                    "sector_breadth_delta",
                    "sector_avg_change",
                    "sector_momentum",
                    "sector_member_count",
                    "sector_confirmation_recent",
                    "sector_ignition_recent",
                    "sector_impulse_id",
                    "sector_ignition_age",
                    "self_core_ignition",
                    "independent_core_attack_recent",
                    "independent_limit_up_recent",
                    "independent_up_count_recent",
                    "is_sector_core",
                    "index_amount_ratio",
                    "index_window_max_amount_ratio",
                    "index_slope3",
                    "context_breadth",
                    "context_breadth_delta",
                    "context_strong_breadth",
                    "context_advancing_amount_share",
                    "context_advancing_amount_delta",
                    "market_turn_recent",
                    "market_volume_recent",
                    "market_turn_time",
                    "market_volume_time",
                    "rebound",
                    "price_slope3",
                    "formula_support",
                    "formula_exhaustion",
                    "formula_trend_score",
                    "formula_quick_entry",
                    "formula_main_absorption",
                    "formula_white_line",
                    "formula_yellow_line",
                    "formula_white_distance_pct",
                    "formula_yellow_distance_pct",
                    "formula_trend_distance_pct",
                    "formula_near_trend_line",
                    "formula_near_trend_line_name",
                    "formula_buy_candidate",
                    "formula_sell_candidate",
                    "limit_up",
                    "core_attack",
                )
            },
            "outcome": outcome,
        }

    def _sell_state(
        self,
        series: StockSeries,
        idx: int,
        metric: dict[str, Any],
        market: dict[str, Any],
        sector: dict[str, Any],
    ) -> dict[str, Any]:
        rebound = _finite(metric.get("rebound"))
        pullback = _finite(metric.get("pullback"))
        fading = bool(
            _finite(metric.get("amount_ratio"), 1) <= 0.95
            or _finite(metric.get("flow_score")) <= -10
            or _finite(metric.get("slope3")) <= 0
            or not bool(market.get("amount_expanding"))
        )
        trigger = bool(
            idx >= self.config.sell_min_hold_bars
            and rebound >= self.config.sell_rebound_pct
            and (
                pullback <= self.config.sell_near_high_pullback_pct
                or fading
                or not bool(sector.get("confirmed"))
            )
        )
        reasons = [f"日内低点反弹{rebound:.1f}%"]
        if pullback <= self.config.sell_near_high_pullback_pct:
            reasons.append(f"距日高{pullback:.1f}%")
        if fading:
            reasons.append("量能/方向/指数至少一项开始衰减")
        return {
            "trigger": trigger,
            "rebound": rebound,
            "pullback": pullback,
            "fading": fading,
            "reasons": reasons,
        }

    def _sell_record(self, series: StockSeries, trade_date: str, idx: int, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "trade_date": trade_date,
            "code": series.code,
            "name": series.name,
            "industry": series.industry,
            "time": SESSION_TIMES[min(idx, len(SESSION_TIMES) - 1)],
            "index": idx,
            "price": round(_finite(series.metrics[idx].get("price")), 4),
            "rebound_pct": round(_finite(state.get("rebound")), 3),
            "pullback_pct": round(_finite(state.get("pullback")), 3),
            "reasons": list(state.get("reasons") or []),
            "outcome": self._outcome(series.metrics, idx),
        }

    def _simulate_trades(
        self,
        trade_date: str,
        series_by_code: dict[str, StockSeries],
        candidates: list[dict[str, Any]],
        sell_zones: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_code_sell: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in sell_zones:
            by_code_sell[item["code"]].append(item)
        output: list[dict[str, Any]] = []
        for code, series in series_by_code.items():
            entries = [item for item in candidates if item["code"] == code and item["strict_four_factor"]]
            entries.sort(key=lambda item: int(item["index"]))
            exits = by_code_sell.get(code, [])
            exit_pos = 0
            next_allowed = -1
            for entry in entries:
                entry_idx = int(entry["index"]) + 1
                if entry_idx >= len(series.metrics) or entry_idx < next_allowed:
                    continue
                chosen = None
                for candidate in exits[exit_pos:]:
                    if int(candidate["index"]) >= entry_idx + self.config.sell_min_hold_bars:
                        chosen = candidate
                        break
                if chosen:
                    exit_pos = exits.index(chosen) + 1
                    exit_idx = int(chosen["index"])
                    exit_price = _finite(chosen.get("price"))
                    exit_reason = list(chosen.get("reasons") or [])
                else:
                    exit_idx = len(series.metrics) - 1
                    exit_price = _finite(series.metrics[exit_idx].get("price"))
                    exit_reason = ["收盘退出"]
                entry_price = _finite(series.metrics[entry_idx].get("price"))
                gross = _pct(exit_price, entry_price)
                net = gross - self.config.friction_pct
                path = [
                    _finite(item.get("price"))
                    for item in series.metrics[entry_idx : exit_idx + 1]
                    if _finite(item.get("price")) > 0
                ]
                max_runup = _pct(max(path), entry_price) if path and entry_price else 0
                max_drawdown = _pct(min(path), entry_price) if path and entry_price else 0
                output.append(
                    {
                        "trade_date": trade_date,
                        "code": code,
                        "name": series.name,
                        "industry": series.industry,
                        "entry_time": entry["entry_time"],
                        "entry_index": entry_idx,
                        "entry_price": round(entry_price, 4),
                        "exit_time": SESSION_TIMES[min(exit_idx, len(SESSION_TIMES) - 1)],
                        "exit_index": exit_idx,
                        "exit_price": round(exit_price, 4),
                        "gross_return_pct": round(gross, 3),
                        "net_return_pct": round(net, 3),
                        "max_runup_pct": round(max_runup, 3),
                        "max_drawdown_pct": round(max_drawdown, 3),
                        "exit_reasons": exit_reason,
                        "entry_factors": entry["factors"],
                    }
                )
                next_allowed = exit_idx + self.config.candidate_cooldown
        return output

    def _simulate_v3_trades(
        self,
        trade_date: str,
        series_by_code: dict[str, StockSeries],
        market_metrics: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Simulate the risk/reward version without replacing the V2 baseline.

        Candidate selection remains point-in-time V2.  V3 adds a trade-location
        plan at the candidate close, an explicit no-chase entry limit for the
        following minute, and exits driven by stop/target or multiple weakening
        observations.  No target-day outcome participates in the entry gate.
        """
        formal = [
            item
            for item in candidates
            if item.get("strategy_v2", {}).get("eligible")
        ]
        formal = self._cluster_events(formal, gap=30)
        count = max((len(series.metrics) for series in series_by_code.values()), default=0)
        sector_rows = self._research_sector_context(series_by_code, count)
        trades, rejections = self._simulate_v3_variant(
            trade_date,
            series_by_code,
            market_metrics,
            sector_rows,
            formal,
            target_r_override=None,
            collect_rejections=True,
        )
        stress_trades: dict[float, list[dict[str, Any]]] = {}
        for target_r in self.config.v3_target_ratios:
            variant, _ = self._simulate_v3_variant(
                trade_date,
                series_by_code,
                market_metrics,
                sector_rows,
                formal,
                target_r_override=float(target_r),
                collect_rejections=False,
            )
            stress_trades[float(target_r)] = variant
        return {
            "trades": trades,
            "rejections": rejections,
            "stress_trades": stress_trades,
        }

    def _research_sector_context(
        self,
        series_by_code: dict[str, StockSeries],
        count: int,
    ) -> dict[str, list[dict[str, Any]]]:
        by_industry: dict[str, list[StockSeries]] = defaultdict(list)
        for series in series_by_code.values():
            by_industry[series.industry].append(series)
        groups: dict[str, list[StockSeries]] = dict(by_industry)
        for theme_name, members in self.manual_theme_members.items():
            themed = [series for code, series in series_by_code.items() if code in members]
            if themed:
                groups[theme_name] = themed
        return {
            name: self._sector_metrics(
                members,
                count,
                configured_core_codes=self.manual_theme_core.get(name, set()),
            )
            for name, members in groups.items()
        }

    def _simulate_v3_variant(
        self,
        trade_date: str,
        series_by_code: dict[str, StockSeries],
        market_metrics: list[dict[str, Any]],
        sector_metrics: dict[str, list[dict[str, Any]]],
        candidates: list[dict[str, Any]],
        *,
        target_r_override: float | None,
        collect_rejections: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in candidates:
            grouped[str(item.get("code") or "")].append(item)

        output: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        for code, entries in grouped.items():
            series = series_by_code.get(code)
            if series is None:
                continue
            entries.sort(key=lambda item: int(item.get("index", 0)))
            next_allowed = -1
            last_impulse_id = -1
            trade_count = 0
            for candidate in entries:
                candidate_idx = int(candidate.get("index", 0))
                entry_idx = candidate_idx + 1
                if entry_idx >= len(series.metrics) or entry_idx < next_allowed:
                    continue
                plan, sector_name = self._v3_entry_plan(
                    series,
                    candidate,
                    market_metrics,
                    sector_metrics,
                )
                rejection_reason = ""
                if not plan.available:
                    rejection_reason = plan.status or "结构样本不足"
                elif not plan.favorable:
                    rejection_reason = plan.status or "赔率不满足"

                decision = candidate.get("strategy_v2", {})
                factor_metrics = candidate.get("metrics", {})
                impulse_id = int(_finite(factor_metrics.get("sector_impulse_id")))
                entry_archetype = str(decision.get("entry_archetype") or "板块确认后跟随")
                if not rejection_reason and trade_count >= self.config.v3_max_trades_per_stock_day:
                    rejection_reason = "当天该股已完成两轮独立T计划"
                elif (
                    not rejection_reason
                    and trade_count > 0
                    and impulse_id <= last_impulse_id
                ):
                    rejection_reason = "同一板块脉冲内的重复信号，不再次追买"
                elif not rejection_reason and entry_idx >= self.config.v3_late_entry_index:
                    rejection_reason = "14:30后剩余兑现时间不足，不新开T计划"

                terms = self._v3_execution_terms(
                    series=series,
                    candidate=candidate,
                    candidate_plan=plan,
                    entry_idx=entry_idx,
                    market_metrics=market_metrics,
                    sector_metrics=sector_metrics,
                )
                entry_price = _finite(terms.get("entry_price"))
                invalidation = _finite(terms.get("invalidation_price"))
                base_target = _finite(terms.get("target_price"))
                minimum_rr = max(_finite(terms.get("required_rr"), plan.min_required_ratio), 1.0)
                max_entry = _finite(terms.get("entry_limit_price"))
                if not rejection_reason and not terms.get("accepted"):
                    rejection_reason = str(terms.get("reason") or "实际成交位置不再满足计划")

                if rejection_reason:
                    if collect_rejections:
                        rejections.append(
                            {
                                "trade_date": trade_date,
                                "code": code,
                                "name": series.name,
                                "industry": series.industry,
                                "candidate_time": str(candidate.get("time") or ""),
                                "candidate_index": candidate_idx,
                                "status": plan.status,
                                "reason": rejection_reason,
                                "structure": plan.structure,
                                "entry_archetype": entry_archetype,
                                "sector_impulse_id": impulse_id,
                                "planned_rr": round(_finite(plan.reward_risk_ratio), 3),
                                "execution_rr": round(_finite(terms.get("execution_rr")), 3),
                                "required_rr": round(minimum_rr, 3),
                                "entry_price": round(entry_price, 4),
                                "entry_limit_price": round(max_entry, 4),
                                "invalidation_price": round(invalidation, 4),
                                "target_price": round(base_target, 4),
                            }
                        )
                    continue

                risk_amount = entry_price - invalidation
                target = (
                    min(base_target, entry_price + risk_amount * target_r_override)
                    if target_r_override is not None
                    else base_target
                )
                trade = self._run_v3_trade(
                    trade_date=trade_date,
                    series=series,
                    market_metrics=market_metrics,
                    sector_rows=sector_metrics.get(sector_name, []),
                    candidate=candidate,
                    entry_idx=entry_idx,
                    entry_price=entry_price,
                    invalidation=invalidation,
                    target=target,
                    entry_limit=max_entry,
                    plan=plan,
                    target_r_override=target_r_override,
                    scene=str(terms.get("scene") or ""),
                    scene_evidence=list(terms.get("scene_evidence") or []),
                )
                output.append(trade)
                trade_count += 1
                last_impulse_id = max(last_impulse_id, impulse_id)
                next_allowed = max(
                    int(trade["exit_index"]) + self.config.candidate_cooldown,
                    candidate_idx + self.config.v3_rearm_bars,
                )
        return output, rejections

    def _v3_execution_terms(
        self,
        *,
        series: StockSeries,
        candidate: dict[str, Any],
        candidate_plan: Any,
        entry_idx: int,
        market_metrics: list[dict[str, Any]],
        sector_metrics: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Reprice a candidate at the next observable minute without fake R multiples."""
        entry_price = _finite(series.metrics[entry_idx].get("price"))
        required_rr = max(_finite(candidate_plan.min_required_ratio), 1.0)
        candidate_stop = _finite(candidate_plan.invalidation_price)
        candidate_target = _finite(candidate_plan.target_price)
        entry_limit = (
            (candidate_target + required_rr * candidate_stop) / (1 + required_rr)
            if candidate_target > candidate_stop > 0
            else 0.0
        )
        result = {
            "accepted": False,
            "reason": "",
            "entry_price": entry_price,
            "entry_limit_price": entry_limit,
            "invalidation_price": candidate_stop,
            "target_price": candidate_target,
            "required_rr": required_rr,
            "execution_rr": 0.0,
        }
        if entry_price <= 0:
            result["reason"] = "下一分钟没有有效成交价格"
            return result
        if entry_limit > 0 and entry_price > entry_limit:
            result["reason"] = f"下一分钟价格{entry_price:.2f}超过不追价上限{entry_limit:.2f}"
            return result

        execution_plan, _ = self._v3_entry_plan(
            series,
            candidate,
            market_metrics,
            sector_metrics,
            idx_override=entry_idx,
        )
        if not execution_plan.available:
            result["reason"] = "成交分钟结构样本不足"
            return result
        if str(execution_plan.structure) in {"结构观察", "追涨风险"}:
            result["reason"] = f"成交分钟{execution_plan.structure}，原计划不再执行"
            return result

        planned_entry = _finite(candidate_plan.entry_price)
        planned_risk_amount = max(
            planned_entry - candidate_stop,
            planned_entry * self.risk_reward.min_risk_pct / 100,
        )
        execution_risk_floor = max(
            planned_risk_amount,
            entry_price * self.risk_reward.min_risk_pct / 100,
        )
        stop_candidates = [
            entry_price - execution_risk_floor,
            candidate_stop,
            _finite(execution_plan.invalidation_price),
        ]
        invalidation = min(value for value in stop_candidates if value > 0)
        targets = [
            value
            for value in (candidate_target, _finite(execution_plan.target_price))
            if value > entry_price
        ]
        target = min(targets) if targets else 0.0
        risk_amount = entry_price - invalidation
        risk_pct = risk_amount / entry_price * 100 if entry_price else 0.0
        reward_amount = target - entry_price
        execution_rr = reward_amount / risk_amount if risk_amount > 0 else 0.0
        result.update(
            {
                "invalidation_price": invalidation,
                "target_price": target,
                "execution_rr": execution_rr,
                "risk_pct": risk_pct,
                "execution_structure": str(execution_plan.structure),
            }
        )
        if entry_price <= invalidation:
            result["reason"] = "下一分钟已跌破重新评估后的失效位"
        elif target <= entry_price:
            result["reason"] = "下一分钟没有保守可达的上行空间"
        elif risk_pct < self.risk_reward.min_risk_pct:
            result["reason"] = f"实际风险{risk_pct:.2f}%低于正常分钟噪声，拒绝虚假高赔率"
        elif risk_pct > self.risk_reward.max_risk_pct:
            result["reason"] = f"实际风险{risk_pct:.2f}%超过风险预算"
        elif execution_rr < required_rr:
            result["reason"] = f"实际成交盈亏比{execution_rr:.2f}R低于{required_rr:.2f}R要求"
        else:
            scene = self._v3_scene_decision(
                series=series,
                candidate=candidate,
                entry_idx=entry_idx,
                execution_structure=str(execution_plan.structure),
            )
            result.update(scene)
            if scene.get("accepted"):
                result["accepted"] = True
            else:
                result["reason"] = str(scene.get("reason") or "盘面场景不支持执行")
        return result

    def _v3_scene_decision(
        self,
        *,
        series: StockSeries,
        candidate: dict[str, Any],
        entry_idx: int,
        execution_structure: str,
    ) -> dict[str, Any]:
        """Apply a setup hierarchy after location/RR has passed.

        This is intentionally a veto chain rather than another additive score:
        a late, saturated breakout is not rescued by unrelated positive points.
        """
        metrics = candidate.get("metrics", {})
        decision = candidate.get("strategy_v2", {})
        actual_idx = min(entry_idx, len(series.metrics) - 1)
        actual = series.metrics[actual_idx]
        archetype = str(decision.get("entry_archetype") or "板块确认后跟随")
        manual_sector = bool(metrics.get("sector_is_manual"))
        flow_is_tape = bool(actual.get("flow_available"))
        flow_score = _finite(actual.get("flow_score"))
        large_imbalance = _finite(actual.get("large_imbalance"))
        amount_ratio = _finite(actual.get("amount_ratio"), 1.0)
        rebound = _finite(actual.get("rebound"), _finite(metrics.get("rebound")))
        breadth = _finite(metrics.get("context_breadth"))
        strong_breadth = _finite(metrics.get("context_strong_breadth"))
        independent_up = int(_finite(metrics.get("independent_up_count_recent")))
        ignition_age = int(_finite(metrics.get("sector_ignition_age"), 999))
        rules = getattr(self, "rules", {}) or {}
        saturation_breadth = _finite(rules.get("v3_market_saturation_breadth"), 0.60)
        saturation_strong = _finite(rules.get("v3_market_saturation_strong_breadth"), 0.45)
        late_rebound = _finite(
            rules.get("strategy_late_rebound_warning_pct"),
            5.0,
        )
        overheat_flow = self.config.strategy_flow_overheat_threshold
        raw_tape_pressure = bool(
            flow_is_tape
            and (
                flow_score <= self.config.strategy_flow_pressure_threshold
                or large_imbalance <= -30
            )
        )
        tape_absorption = bool(
            raw_tape_pressure
            and actual_idx >= 2
            and _finite(series.metrics[actual_idx - 2].get("price"))
            > _finite(series.metrics[actual_idx - 1].get("price"))
            and _finite(actual.get("price"))
            > _finite(series.metrics[actual_idx - 1].get("price"))
            and execution_structure == "回踩承接"
            and _finite(actual.get("slope3")) >= 0
            and (
                _finite(actual.get("vwap")) <= 0
                or (
                    _finite(actual.get("price")) >= _finite(actual.get("vwap")) * 0.995
                    and _finite(actual.get("price")) <= _finite(actual.get("vwap")) * 1.008
                )
            )
            and amount_ratio <= 2.5
        )
        tape_support = bool(
            not flow_is_tape
            or tape_absorption
            or (
                flow_score >= self.config.strategy_flow_support_threshold
                and large_imbalance > -10
            )
        )
        market_saturated = bool(
            breadth >= saturation_breadth
            and strong_breadth >= saturation_strong
        )
        manual_fresh_leader = bool(
            manual_sector
            and archetype == "容量核心先点火"
            and ignition_age <= 2
            and tape_support
        )

        if raw_tape_pressure and not tape_absorption:
            reason = "成交分钟逐笔卖压与价格下行同步，原共振计划降级为观察"
        elif (
            flow_is_tape
            and flow_score >= overheat_flow
            and amount_ratio >= 3.0
            and execution_structure != "回踩承接"
        ):
            reason = "逐笔成交与量能同时过热，突破更像兑现区而非低风险点火"
        elif (
            rebound >= late_rebound
            and execution_structure != "回踩承接"
            and not manual_fresh_leader
        ):
            reason = f"日内低点反弹已达{rebound:.1f}%，没有首次回踩结构，不追剩余空间"
        elif market_saturated and execution_structure != "回踩承接" and not manual_fresh_leader:
            reason = "上涨宽度和强势宽度已处高位，继续追突破的盈亏比不对称"
        elif entry_idx >= 120 and execution_structure != "回踩承接":
            reason = "午后新开T计划只接受首次回踩承接，不追普通突破"
        elif not manual_sector and archetype == "容量核心先点火" and independent_up < 2:
            reason = "非手工主线的容量票缺少至少两只独立成员扩散，不能自证板块"
        elif archetype == "板块确认后跟随" and flow_is_tape and not tape_support:
            reason = "跟随股没有逐笔成交支持，只保留观察"
        else:
            reason = ""

        if archetype == "容量核心先点火":
            scene = "核心先点火"
        elif archetype == "核心带动板块传导":
            scene = "核心向板块传导"
        elif execution_structure == "回踩承接":
            scene = "首次回踩确认"
        else:
            scene = "板块跟随确认"
        evidence = [
            f"{scene} · {execution_structure}",
            f"逐笔方向{flow_score:+.1f}% / 大额差{large_imbalance:+.1f}%",
            "逐笔卖压被价格承接，按回踩吸收处理" if tape_absorption else "逐笔与价格方向一致",
            f"上下文上涨宽度{breadth * 100:.0f}% / 强势宽度{strong_breadth * 100:.0f}%",
            f"日内低点反弹{rebound:.1f}% / 板块点火后{ignition_age}分钟",
        ]
        return {
            "accepted": not reason,
            "reason": reason,
            "scene": scene,
            "scene_evidence": evidence,
        }

    def _v3_entry_plan(
        self,
        series: StockSeries,
        candidate: dict[str, Any],
        market_metrics: list[dict[str, Any]],
        sector_metrics: dict[str, list[dict[str, Any]]],
        *,
        idx_override: int | None = None,
    ) -> tuple[Any, str]:
        idx = min(
            int(candidate.get("index", 0) if idx_override is None else idx_override),
            len(series.metrics) - 1,
        )
        metric = series.metrics[idx]
        prices = [_finite(item.get("price")) for item in series.metrics[: idx + 1]]
        market_at = market_metrics[min(idx, len(market_metrics) - 1)] if market_metrics else {}
        factor_metrics = candidate.get("metrics", {})
        decision = candidate.get("strategy_v2", {})
        entry_archetype = str(decision.get("entry_archetype") or "板块确认后跟随")
        pilot_entry = entry_archetype in {"容量核心先点火", "核心带动板块传导"}
        sector_name = str(factor_metrics.get("sector_name") or series.industry)
        sector_history = sector_metrics.get(sector_name, [])
        sector_at = sector_history[min(idx, len(sector_history) - 1)] if sector_history else {}
        sector_recent = self._recent_true(
            [
                row.get("confirmed") or row.get("ignition")
                for row in sector_history[max(0, idx - self.config.confluence_window + 1) : idx + 1]
            ],
            self.config.confluence_window,
        )
        market_state = self._market_confluence_state(market_metrics, idx)
        sequence_ready = bool(
            decision.get("leader_ignition")
            or decision.get("sector_transfer")
            or decision.get("sector_confirmation")
        )
        flow_score = _finite(metric.get("flow_score"))
        flow_pressure = bool(
            metric.get("flow_available")
            and (
                flow_score <= self.config.strategy_flow_pressure_threshold
                or _finite(metric.get("large_imbalance"))
                <= self.config.strategy_flow_pressure_threshold
            )
        )
        plan = self.risk_reward.evaluate(
            prices=prices,
            idx=idx,
            price=_finite(metric.get("price")),
            vwap=_finite(metric.get("vwap")),
            prev_close=series.prev_close,
            open_price=prices[0] if prices else series.prev_close,
            running_high=max(prices) if prices else series.prev_close,
            rebound_pct=_finite(metric.get("rebound")),
            minute_index=idx,
            market_resonance=bool(
                market_state.get("market_turn_recent")
                and market_state.get("market_volume_recent")
            ),
            market_accelerating=bool(
                market_at.get("turning")
                or _finite(market_at.get("slope3")) > _finite(market_at.get("prior_slope"))
            ),
            sector_accelerating=bool(
                (sector_recent or sequence_ready)
                and (
                    sector_at.get("core_attack")
                    or factor_metrics.get("self_core_ignition")
                    or factor_metrics.get("independent_core_attack_recent")
                    or _finite(sector_at.get("momentum")) >= 0
                )
            ),
            flow_positive=flow_score >= self.config.strategy_flow_support_threshold,
            flow_pressure=flow_pressure,
            limit_pct=_limit_pct(series.code, series.market),
            required_rr_discount=(self.risk_reward.pilot_rr_discount if pilot_entry else 0.0),
            required_rr_floor=self.risk_reward.pilot_rr_floor,
        )
        return plan, sector_name

    def _run_v3_trade(
        self,
        *,
        trade_date: str,
        series: StockSeries,
        market_metrics: list[dict[str, Any]],
        sector_rows: list[dict[str, Any]],
        candidate: dict[str, Any],
        entry_idx: int,
        entry_price: float,
        invalidation: float,
        target: float,
        entry_limit: float,
        plan: Any,
        target_r_override: float | None,
        scene: str = "",
        scene_evidence: list[str] | None = None,
    ) -> dict[str, Any]:
        risk_amount = max(entry_price - invalidation, 0.000001)
        risk_pct = risk_amount / entry_price * 100
        peak = entry_price
        trough = entry_price
        exit_idx = len(series.metrics) - 1
        exit_price = _finite(series.metrics[exit_idx].get("price"), entry_price)
        exit_kind = "收盘退出"
        exit_factors: list[str] = ["收盘退出"]
        exit_score = 0

        for idx in range(entry_idx + 1, len(series.metrics)):
            metric = series.metrics[idx]
            price = _finite(metric.get("price"), entry_price)
            peak = max(peak, price)
            trough = min(trough, price)
            held = idx - entry_idx
            if price <= invalidation:
                exit_idx = idx
                exit_price = min(price, invalidation)
                exit_kind = "失效止损"
                exit_factors = [f"跌破失效价{invalidation:.2f}"]
                exit_score = 100
                break
            if price >= target:
                exit_idx = idx
                exit_price = target
                exit_kind = "目标兑现"
                exit_factors = [f"触及计划目标{target:.2f}"]
                exit_score = 100
                break
            if held < 3:
                continue

            previous = series.metrics[idx - 1]
            recent = series.metrics[max(entry_idx, idx - 3) : idx + 1]
            flow_score = _finite(metric.get("flow_score"))
            max_recent_flow = max((_finite(item.get("flow_score")) for item in recent), default=flow_score)
            slope = _finite(metric.get("slope3"))
            prior_slope = _finite(previous.get("slope3"))
            amount_ratio = _finite(metric.get("amount_ratio"), 1)
            gain = max(peak - entry_price, 0.0)
            giveback = (peak - price) / gain if gain > 0 else 0.0
            max_r = (peak - entry_price) / risk_amount

            factors: list[str] = []
            score = 0
            momentum_decay = bool(
                prior_slope >= 0.08
                and slope <= 0
                and price <= _finite(previous.get("price"), price)
            )
            if momentum_decay:
                factors.append("动能衰减")
                score += 12
            flow_divergence = bool(
                flow_score <= -15
                or (max_recent_flow - flow_score >= 25 and price >= peak * 0.992)
            )
            if flow_divergence:
                factors.append("成交方向背离")
                score += 18
            efficiency_down = bool(
                (amount_ratio >= 1.45 and slope <= 0.03)
                or (max_r >= 1.0 and amount_ratio <= 0.80 and slope <= 0)
            )
            if efficiency_down:
                factors.append("量价效率下降")
                score += 14

            market_at = market_metrics[min(idx, len(market_metrics) - 1)] if market_metrics else {}
            market_weak = bool(
                not market_at.get("turning")
                and _finite(market_at.get("slope3")) <= -0.03
            )
            if market_weak:
                factors.append("指数背离")
                score += 8
            sector_at = sector_rows[min(idx, len(sector_rows) - 1)] if sector_rows else {}
            sector_weak = bool(
                sector_rows
                and not sector_at.get("supportive", sector_at.get("confirmed"))
                and _finite(sector_at.get("momentum")) <= -0.08
            )
            if sector_weak:
                factors.append("板块退潮")
                score += 10

            failed_breakout = bool(
                max_r >= 0.8
                and price <= peak * 0.994
                and amount_ratio >= 1.10
            )
            profit_giveback = bool(max_r >= 1.0 and giveback >= 0.35)
            if failed_breakout or profit_giveback:
                factors.append("突破失败" if failed_breakout else "利润回吐")
                score += 16

            current_r = (price - entry_price) / risk_amount
            no_progress = bool(
                held >= self.config.v3_no_progress_bars
                and max_r < self.config.v3_no_progress_r
                and current_r <= 0.10
                and (flow_score < 0 or slope <= 0)
                and (market_weak or sector_weak)
            )
            if no_progress:
                factors.append("预期未兑现")
                score += 20

            stock_deterioration_count = sum(
                name in factors
                for name in (
                    "动能衰减",
                    "成交方向背离",
                    "量价效率下降",
                    "突破失败",
                    "利润回吐",
                    "预期未兑现",
                )
            )
            dynamic_ready = bool(
                no_progress
                or (
                    score >= self.config.v3_dynamic_exit_score
                    and len(set(factors)) >= self.config.v3_dynamic_exit_factors
                    and (
                        max_r >= 0.8
                        or stock_deterioration_count >= 2
                    )
                )
            )

            if dynamic_ready:
                fill_idx = min(idx + 1, len(series.metrics) - 1)
                exit_idx = fill_idx
                exit_price = _finite(series.metrics[fill_idx].get("price"), price)
                exit_kind = "逻辑/时间失效" if no_progress else "动态衰竭"
                exit_factors = list(dict.fromkeys(factors))
                exit_score = score
                peak = max(peak, exit_price)
                trough = min(trough, exit_price)
                break

        gross = _pct(exit_price, entry_price)
        net = gross - max(self.config.friction_pct, 0.20)
        reward_pct = _pct(target, entry_price)
        decision = candidate.get("strategy_v2", {})
        entry_archetype = str(decision.get("entry_archetype") or "板块确认后跟随")
        planned_size_pct = 25 if entry_archetype in {"容量核心先点火", "核心带动板块传导"} else 75
        return {
            "trade_date": trade_date,
            "code": series.code,
            "name": series.name,
            "industry": series.industry,
            "candidate_time": str(candidate.get("time") or ""),
            "entry_time": SESSION_TIMES[min(entry_idx, len(SESSION_TIMES) - 1)],
            "entry_index": entry_idx,
            "entry_price": round(entry_price, 4),
            "entry_limit_price": round(entry_limit, 4),
            "support_price": round(_finite(plan.support_price), 4),
            "invalidation_price": round(invalidation, 4),
            "target_price": round(target, 4),
            "exit_time": SESSION_TIMES[min(exit_idx, len(SESSION_TIMES) - 1)],
            "exit_index": exit_idx,
            "exit_price": round(exit_price, 4),
            "exit_kind": exit_kind,
            "exit_score": int(exit_score),
            "exit_factors": exit_factors,
            "gross_return_pct": round(gross, 3),
            "net_return_pct": round(net, 3),
            "risk_pct": round(risk_pct, 3),
            "expected_reward_pct": round(reward_pct, 3),
            "planned_rr": round(reward_pct / risk_pct if risk_pct > 0 else 0, 3),
            "required_rr": round(_finite(plan.min_required_ratio), 3),
            "realized_r": round(net / risk_pct if risk_pct > 0 else 0, 3),
            "max_favorable_r": round((peak - entry_price) / risk_amount, 3),
            "max_adverse_r": round((trough - entry_price) / risk_amount, 3),
            "stop_hit": exit_kind == "失效止损",
            "target_hit": exit_kind == "目标兑现",
            "dynamic_exit": exit_kind in {"动态衰竭", "逻辑/时间失效"},
            "logic_exit": exit_kind == "逻辑/时间失效",
            "close_exit": exit_kind == "收盘退出",
            "target_r_override": target_r_override,
            "entry_archetype": entry_archetype,
            "planned_size_pct": planned_size_pct,
            "weighted_net_return_pct": round(net * planned_size_pct / 100, 3),
            "sector_impulse_id": int(_finite(candidate.get("metrics", {}).get("sector_impulse_id"))),
            "sector_name": str(candidate.get("metrics", {}).get("sector_name") or series.industry),
            "sector_is_manual": bool(candidate.get("metrics", {}).get("sector_is_manual")),
            "flow_score": round(_finite(candidate.get("metrics", {}).get("flow_score")), 3),
            "context_breadth": round(_finite(candidate.get("metrics", {}).get("context_breadth")), 3),
            "context_breadth_delta": round(
                _finite(candidate.get("metrics", {}).get("context_breadth_delta")), 3
            ),
            "context_advancing_amount_share": round(
                _finite(candidate.get("metrics", {}).get("context_advancing_amount_share")), 3
            ),
            "entry_context": str(plan.context),
            "entry_structure": str(plan.structure),
            "entry_reasons": list(plan.reasons),
            "entry_scene": scene,
            "scene_evidence": list(scene_evidence or []),
            "flow_source": str(candidate.get("metrics", {}).get("flow_source") or "minute_price_amount_proxy"),
        }

    @staticmethod
    def _v3_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
        returns = [_finite(item.get("net_return_pct")) for item in trades]
        weighted_returns = [
            _finite(item.get("weighted_net_return_pct"), _finite(item.get("net_return_pct")))
            for item in trades
        ]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        realized_r = [_finite(item.get("realized_r")) for item in trades]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        avg_win = statistics.mean(wins) if wins else 0.0
        avg_loss = statistics.mean(losses) if losses else 0.0
        return {
            "trade_count": len(trades),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "avg_win_pct": round(avg_win, 3),
            "avg_loss_pct": round(avg_loss, 3),
            "payoff_ratio": round(avg_win / abs(avg_loss), 3) if avg_loss < 0 else None,
            "expectancy_pct": round(statistics.mean(returns), 3) if returns else 0,
            "size_weighted_expectancy_pct": (
                round(statistics.mean(weighted_returns), 3) if weighted_returns else 0
            ),
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
            "average_r": round(statistics.mean(realized_r), 3) if realized_r else 0,
            "stop_hit_rate_pct": round(sum(bool(item.get("stop_hit")) for item in trades) / len(trades) * 100, 2) if trades else 0,
            "target_hit_rate_pct": round(sum(bool(item.get("target_hit")) for item in trades) / len(trades) * 100, 2) if trades else 0,
            "dynamic_exit_rate_pct": round(sum(bool(item.get("dynamic_exit")) for item in trades) / len(trades) * 100, 2) if trades else 0,
            "logic_exit_rate_pct": round(sum(bool(item.get("logic_exit")) for item in trades) / len(trades) * 100, 2) if trades else 0,
            "close_exit_rate_pct": round(sum(bool(item.get("close_exit")) for item in trades) / len(trades) * 100, 2) if trades else 0,
        }

    @staticmethod
    def _deduplicate_v3_pulses(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep one ex-ante representative for each sector ignition pulse."""
        role_order = {"容量核心先点火": 0, "核心带动板块传导": 1, "板块确认后跟随": 2}
        ordered = sorted(
            trades,
            key=lambda item: (
                str(item.get("trade_date") or ""),
                int(_finite(item.get("entry_index"))),
                role_order.get(str(item.get("entry_archetype") or ""), 9),
                str(item.get("code") or ""),
            ),
        )
        selected: dict[tuple[str, str, int], dict[str, Any]] = {}
        for item in ordered:
            impulse_id = int(_finite(item.get("sector_impulse_id")))
            if impulse_id <= 0:
                impulse_id = int(_finite(item.get("entry_index"))) // 30 + 10_000
            key = (
                str(item.get("trade_date") or ""),
                str(item.get("sector_name") or item.get("industry") or "未分类"),
                impulse_id,
            )
            selected.setdefault(key, item)
        return list(selected.values())

    def _v3_breakdowns(self, trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        definitions = {
            "by_date": lambda item: str(item.get("trade_date") or ""),
            "by_archetype": lambda item: str(item.get("entry_archetype") or "未分类"),
            "by_scene": lambda item: str(item.get("entry_scene") or "未分类"),
            "by_time": lambda item: (
                "09:30-10:00"
                if str(item.get("entry_time") or "") < "10:00"
                else "10:00-11:30"
                if str(item.get("entry_time") or "") < "11:30"
                else "13:00-14:00"
                if str(item.get("entry_time") or "") < "14:00"
                else "14:00-15:00"
            ),
        }
        output: dict[str, list[dict[str, Any]]] = {}
        for label, key_fn in definitions.items():
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in trades:
                grouped[key_fn(item)].append(item)
            output[label] = [
                {"group": group, **self._v3_trade_stats(rows)}
                for group, rows in sorted(grouped.items())
            ]
        return output

    def _outcome(self, metrics: list[dict[str, Any]], idx: int) -> dict[str, Any]:
        entry_idx = idx + 1
        entry_price = _finite(metrics[entry_idx].get("price")) if entry_idx < len(metrics) else 0
        result: dict[str, Any] = {"entry_index": entry_idx}
        if not entry_price:
            for horizon in self.config.outcome_horizons:
                result.update({f"ret_{horizon}m": None, f"mfe_{horizon}m": None, f"mae_{horizon}m": None})
            result["good_30"] = False
            return result
        for horizon in self.config.outcome_horizons:
            future = [
                _finite(item.get("price"))
                for item in metrics[entry_idx : entry_idx + horizon]
                if _finite(item.get("price")) > 0
            ]
            if not future:
                result.update({f"ret_{horizon}m": None, f"mfe_{horizon}m": None, f"mae_{horizon}m": None})
                continue
            result[f"ret_{horizon}m"] = round(_pct(future[-1], entry_price), 3)
            result[f"mfe_{horizon}m"] = round(_pct(max(future), entry_price), 3)
            result[f"mae_{horizon}m"] = round(_pct(min(future), entry_price), 3)
        ret30 = result.get("ret_30m")
        mfe30 = result.get("mfe_30m")
        mae15 = result.get("mae_15m")
        result["net_30m"] = round(ret30 - self.config.friction_pct, 3) if ret30 is not None else None
        result["good_30"] = bool(
            mfe30 is not None
            and mae15 is not None
            and ret30 is not None
            and mfe30 >= self.config.good_mfe_30
            and mae15 >= self.config.good_mae_15
            and result["net_30m"] >= self.config.good_net_30
        )
        result["failed_30"] = bool(
            mae15 is not None
            and ret30 is not None
            and (mae15 <= self.config.stop_loss_pct or result["net_30m"] < 0)
        )
        return result

    def _market_metrics(
        self,
        bars: list[dict[str, Any]],
        previous_bars: list[dict[str, Any]],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        prices, amounts = self._bar_arrays(bars, index=True)
        _, previous_amounts = self._bar_arrays(previous_bars, index=True)
        output: list[dict[str, Any]] = []
        running_low = prices[0] if prices else 0
        for idx, price in enumerate(prices):
            running_low = min(running_low or price, price)
            ratio = self._amount_ratio(amounts, previous_amounts, idx)
            slope3 = self._slope(prices, idx, 3)
            prior_slope = self._slope(prices, idx - 3, 3) if idx >= 3 else 0
            rebound = _pct(price, running_low) if running_low else 0
            turning = bool(
                slope3 >= 0.01
                and (prior_slope <= 0 or rebound >= self.config.index_rebound_threshold)
                and rebound >= self.config.index_rebound_threshold * 0.5
            )
            output.append(
                {
                    "date": trade_date,
                    "time": SESSION_TIMES[min(idx, len(SESSION_TIMES) - 1)],
                    "price": price,
                    "amount_ratio": ratio,
                    "slope3": slope3,
                    "prior_slope": prior_slope,
                    "rebound": rebound,
                    "turning": turning,
                    "amount_expanding": ratio >= self.config.index_volume_ratio_threshold,
                    "market_factor": bool(turning and ratio >= self.config.index_volume_ratio_threshold),
                }
            )
        return output

    @staticmethod
    def _augment_market_context(
        market_metrics: list[dict[str, Any]],
        series_by_code: dict[str, StockSeries],
    ) -> list[dict[str, Any]]:
        """Add point-in-time cross-sectional context from the ex-ante sample pool."""
        output = [dict(row) for row in market_metrics]
        snapshots: list[dict[str, float]] = []
        for idx in range(len(output)):
            rows = [
                series.metrics[idx]
                for series in series_by_code.values()
                if idx < len(series.metrics) and _finite(series.metrics[idx].get("price")) > 0
            ]
            count = len(rows)
            positive = sum(_finite(row.get("change_pct")) > 0 for row in rows)
            strong = sum(_finite(row.get("change_pct")) >= 1.0 for row in rows)
            total_amount = sum(max(0.0, _finite(row.get("amount"))) for row in rows)
            advancing_amount = sum(
                max(0.0, _finite(row.get("amount")))
                for row in rows
                if _finite(row.get("change_pct")) > 0
            )
            snapshots.append(
                {
                    "breadth": positive / count if count else 0.0,
                    "strong_breadth": strong / count if count else 0.0,
                    "advancing_amount_share": (
                        advancing_amount / total_amount if total_amount else 0.0
                    ),
                    "count": float(count),
                }
            )
        for idx, row in enumerate(output):
            current = snapshots[idx]
            prior = snapshots[max(0, idx - 3)]
            row.update(
                {
                    "context_breadth": current["breadth"],
                    "context_breadth_delta": current["breadth"] - prior["breadth"],
                    "context_strong_breadth": current["strong_breadth"],
                    "context_advancing_amount_share": current["advancing_amount_share"],
                    "context_advancing_amount_delta": (
                        current["advancing_amount_share"] - prior["advancing_amount_share"]
                    ),
                    "context_count": int(current["count"]),
                }
            )
        return output

    def _stock_metrics(self, series: StockSeries) -> list[dict[str, Any]]:
        prices, amounts = self._bar_arrays(series.bars, index=False, fallback=series.prev_close)
        _, previous_amounts = self._bar_arrays(series.previous_bars, index=False, fallback=series.prev_close)
        flow = self._transaction_metrics(series.transactions, prices, amounts)
        formula_rows = self._formula_input_rows(series.bars, prices)
        trend_states = self._formula_trend_states(series, formula_rows, prices)
        formula_states = compute_formula_series(
            formula_rows,
            trend_states=trend_states,
        ).states
        output: list[dict[str, Any]] = []
        cumulative_amount = 0.0
        cumulative_volume = 0.0
        previous_cumulative_amount = 0.0
        running_low = prices[0] if prices else series.prev_close
        running_high = running_low
        for idx, price in enumerate(prices):
            amount = amounts[idx]
            volume = _finite(series.bars[idx].get("vol")) if idx < len(series.bars) else 0
            cumulative_amount += amount
            cumulative_volume += volume
            previous_cumulative_amount += previous_amounts[idx] if idx < len(previous_amounts) else 0
            vwap = cumulative_amount / (cumulative_volume * 100) if cumulative_volume else price
            running_low = min(running_low or price, price)
            running_high = max(running_high or price, price)
            amount_ratio = self._amount_ratio(amounts, previous_amounts, idx)
            same_minute_baseline = previous_amounts[idx] if idx < len(previous_amounts) else 0
            same_minute_amount_ratio = amount / same_minute_baseline if same_minute_baseline else 1.0
            cumulative_amount_ratio = (
                cumulative_amount / previous_cumulative_amount if previous_cumulative_amount else 1.0
            )
            slope3 = self._slope(prices, idx, 3)
            rebound = _pct(price, running_low) if running_low else 0
            pullback = _pct(running_high, price) if running_high else 0
            formula_state = formula_states[idx] if idx < len(formula_states) else {}
            formula = self._formula_features(prices, idx, vwap, running_high, formula_state=formula_state)
            flow_at = flow[idx] if idx < len(flow) else {}
            output.append(
                {
                    "time": SESSION_TIMES[min(idx, len(SESSION_TIMES) - 1)],
                    "price": price,
                    "amount": amount,
                    "volume": volume,
                    "amount_ratio": amount_ratio,
                    "same_minute_amount_ratio": _clamp(same_minute_amount_ratio, 0.1, 10.0),
                    "cumulative_amount_ratio": _clamp(cumulative_amount_ratio, 0.1, 10.0),
                    "vwap": vwap,
                    "slope3": slope3,
                    "rebound": rebound,
                    "pullback": pullback,
                    "change_pct": _pct(price, series.prev_close),
                    "limit_up": price >= series.prev_close * (1 + _limit_pct(series.code, series.market) / 100 - 0.003),
                    "formula_support": formula["support"],
                    "formula_exhaustion": formula["exhaustion"],
                    "formula_trend_score": formula["trend_score"],
                    "formula_quick_entry": formula["quick_entry"],
                    "formula_main_absorption": formula["main_absorption"],
                    "formula_white_line": formula["white_line"],
                    "formula_yellow_line": formula["yellow_line"],
                    "formula_white_distance_pct": formula["white_distance_pct"],
                    "formula_yellow_distance_pct": formula["yellow_distance_pct"],
                    "formula_trend_distance_pct": formula["trend_distance_pct"],
                    "formula_near_trend_line": formula["near_trend_line"],
                    "formula_near_trend_line_name": formula["near_trend_line_name"],
                    "formula_buy_candidate": formula["buy_candidate"],
                    "formula_sell_candidate": formula["sell_candidate"],
                    "formula_validation_status": "research_only",
                    **flow_at,
                }
            )
        return output

    def _formula_trend_states(
        self,
        series: StockSeries,
        formula_rows: list[dict[str, Any]],
        prices: list[float],
    ) -> list[dict[str, Any]]:
        daily_history = [
            dict(row)
            for row in (series.daily_history or [])
            if _finite(row.get("close") or row.get("price")) > 0
        ]
        if not daily_history or not formula_rows:
            return []
        base_rows = daily_history[-160:]
        states: list[dict[str, Any]] = []
        for idx, row in enumerate(formula_rows):
            price = prices[idx] if idx < len(prices) else _finite(row.get("close") or row.get("price"))
            if price <= 0:
                continue
            time_label = str(row.get("time") or SESSION_TIMES[min(idx, len(SESSION_TIMES) - 1)])[:5]
            result = compute_trend_line_series(
                [
                    *base_rows,
                    {
                        "date": series.trade_date,
                        "time": time_label,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "price": price,
                    },
                ],
                source_quality="tdx_formula_daily_trend_research",
            )
            state = dict(result.latest)
            if not state:
                continue
            state["time"] = time_label
            state["trend_source_quality"] = "tdx_formula_daily_trend_research"
            states.append(state)
        return states

    @staticmethod
    def _formula_input_rows(bars: list[dict[str, Any]], prices: list[float]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for idx, price in enumerate(prices):
            raw = bars[idx] if idx < len(bars) else {}
            open_price = _finite(raw.get("open"), price) or price
            high = max(_finite(raw.get("high"), price) or price, price)
            low = min(_finite(raw.get("low"), price) or price, price)
            rows.append(
                {
                    "time": str(raw.get("time") or SESSION_TIMES[min(idx, len(SESSION_TIMES) - 1)])[:5],
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": price,
                    "price": price,
                }
            )
        return rows

    def _sector_metrics(
        self,
        members: list[StockSeries],
        count: int,
        *,
        configured_core_codes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not members:
            return []
        core_members = sorted(members, key=lambda item: item.previous_day_amount, reverse=True)[:2]
        # Manual theme core membership is configured before the target day and
        # is therefore valid context metadata rather than a future winner pick.
        if configured_core_codes:
            configured = sorted(
                (item for item in members if item.code in configured_core_codes),
                key=lambda item: item.previous_day_amount,
                reverse=True,
            )
            if configured:
                # Keep every configured capacity/core representative.  Taking
                # the first two from an unordered set previously dropped names
                # such as 300476 from the AI/PCB lead context.
                core_members = configured[:4]
        output: list[dict[str, Any]] = []
        last_ignition = -999
        ignition_id = 0
        previous_core_attack = False
        for idx in range(count):
            current_members = [item for item in members if idx < len(item.metrics)]
            current: list[dict[str, Any]] = [item.metrics[idx] for item in current_members]
            if not current:
                output.append({"confirmed": False})
                continue
            changes = [_finite(row.get("change_pct")) for row in current]
            breadth = sum(1 for value in changes if value > 0) / len(changes)
            avg_change = statistics.mean(changes)
            core_rows = [
                (item.code, item.metrics[idx])
                for item in core_members
                if idx < len(item.metrics)
            ]
            attack_codes = [
                code
                for code, row in core_rows
                if (
                    _finite(row.get("amount_ratio"), 1) >= self.config.sector_core_ratio_threshold
                    and _finite(row.get("slope3")) >= self.config.sector_core_slope_threshold
                    and _finite(row.get("change_pct")) >= 0
                )
            ]
            core_attack = bool(attack_codes)
            limit_up_codes = [
                item.code
                for item in current_members
                if bool(item.metrics[idx].get("limit_up"))
            ]
            up_codes = [
                item.code
                for item in current_members
                if _finite(item.metrics[idx].get("change_pct")) > 0
            ]
            limit_count = len(limit_up_codes)
            prior_index = max(0, idx - 3)
            prior_changes = [
                _finite(item.metrics[prior_index].get("change_pct"))
                for item in current_members
                if prior_index < len(item.metrics)
            ]
            prior_breadth = (
                sum(1 for value in prior_changes if value > 0) / len(prior_changes)
                if prior_changes
                else breadth
            )
            prior_avg_change = statistics.mean(prior_changes) if prior_changes else avg_change
            breadth_delta = breadth - prior_breadth
            momentum = avg_change - prior_avg_change
            score = _clamp(
                breadth * 55
                + _clamp((avg_change + 0.5) * 12, 0, 22)
                + _clamp(momentum * 20, -10, 12)
                + (18 if core_attack else 0)
                + min(10, limit_count * 5),
                0,
                100,
            )
            enough_members = len(current_members) >= self.config.sector_min_members
            confirmed = bool(
                enough_members
                and breadth >= self.config.sector_breadth_threshold
                and avg_change >= self.config.sector_change_threshold
                and (core_attack or limit_count >= 1)
                and score >= 50
            )
            ignition_ready = bool(
                len(current_members) >= 2
                and core_attack
                and score >= 40
                and (
                    not previous_core_attack
                    or breadth_delta >= 0.15
                    or momentum >= 0.08
                )
            )
            ignition = bool(
                ignition_ready
                and idx - last_ignition >= self.config.sector_ignition_cooldown
            )
            if ignition:
                ignition_id += 1
                last_ignition = idx
            previous_core_attack = core_attack
            output.append(
                {
                    "breadth": breadth,
                    "breadth_delta": breadth_delta,
                    "avg_change": avg_change,
                    "momentum": momentum,
                    "core_attack": core_attack,
                    "limit_up_count": limit_count,
                    "score": score,
                    "confirmed": confirmed,
                    "supportive": bool(confirmed and momentum >= -0.08),
                    "ignition": ignition,
                    "ignition_id": ignition_id,
                    "ignition_age": idx - last_ignition if last_ignition >= 0 else 999,
                    "member_count": len(current_members),
                    "core_codes": [item.code for item in core_members],
                    "attack_codes": attack_codes,
                    "limit_up_codes": limit_up_codes,
                    "up_codes": up_codes,
                }
            )
        return output

    def _best_sector_context(
        self,
        names: list[str],
        sector_metrics: dict[str, list[dict[str, Any]]],
        idx: int,
    ) -> tuple[str, dict[str, Any]]:
        candidates: list[tuple[str, dict[str, Any]]] = []
        for name in dict.fromkeys(name for name in names if name):
            rows = sector_metrics.get(name) or []
            if idx < len(rows):
                candidates.append((name, rows[idx]))
        if not candidates:
            return (names[0] if names else "未分类", {})
        # Selecting the strongest currently visible membership mirrors the
        # terminal's sector ranking; it does not inspect any future bar.
        return max(candidates, key=lambda item: (_finite(item[1].get("score")), bool(item[1].get("confirmed"))))

    def _transaction_metrics(
        self,
        rows: list[dict[str, Any]],
        prices: list[float],
        amounts: list[float],
    ) -> list[dict[str, Any]]:
        by_index: dict[int, list[tuple[float, float, str]]] = defaultdict(list)
        trade_amounts: list[float] = []
        previous_price = 0.0
        for row in rows:
            price = _finite(row.get("price"))
            volume = _finite(row.get("vol"))
            index = _stock_time_index(str(row.get("time") or ""))
            if price <= 0 or volume <= 0 or index is None or not prices:
                continue
            index = min(index, len(prices) - 1)
            raw_direction = row.get("buyorsell")
            try:
                direction = int(raw_direction) if raw_direction is not None else None
            except (TypeError, ValueError):
                direction = None
            if direction == 0:
                side = "buy"
            elif direction == 1:
                side = "sell"
            elif raw_direction is not None:
                side = "neutral"
            elif previous_price and price > previous_price:
                side = "buy"
            elif previous_price and price < previous_price:
                side = "sell"
            else:
                side = "neutral"
            amount = price * volume * 100
            trade_amounts.append(amount)
            by_index[index].append((amount, price, side))
            previous_price = price

        threshold = max(500_000.0, _median(trade_amounts, 100_000.0) * 5)
        raw_by_index: list[dict[str, Any]] = []
        for idx in range(len(prices)):
            trades = by_index.get(idx, [])
            buy = sum(amount for amount, _, side in trades if side == "buy")
            sell = sum(amount for amount, _, side in trades if side == "sell")
            neutral = sum(amount for amount, _, side in trades if side == "neutral")
            large_buy = sum(amount for amount, _, side in trades if side == "buy" and amount >= threshold)
            large_sell = sum(amount for amount, _, side in trades if side == "sell" and amount >= threshold)
            raw_by_index.append(
                {
                    "buy": buy,
                    "sell": sell,
                    "neutral": neutral,
                    "large_buy": large_buy,
                    "large_sell": large_sell,
                    "count": len(trades),
                }
            )

        output: list[dict[str, Any]] = []
        for idx in range(len(prices)):
            window = raw_by_index[max(0, idx - self.config.flow_window + 1): idx + 1]
            buy = sum(_finite(item.get("buy")) for item in window)
            sell = sum(_finite(item.get("sell")) for item in window)
            neutral = sum(_finite(item.get("neutral")) for item in window)
            large_buy = sum(_finite(item.get("large_buy")) for item in window)
            large_sell = sum(_finite(item.get("large_sell")) for item in window)
            transaction_count = sum(int(_finite(item.get("count"))) for item in window)
            directional = buy + sell
            large_directional = large_buy + large_sell
            imbalance = (buy - sell) / directional * 100 if directional else 0
            large_imbalance = (large_buy - large_sell) / large_directional * 100 if large_directional else 0
            score = _clamp(imbalance * 0.55 + large_imbalance * 0.45, -100, 100)
            if not transaction_count:
                # Historical five-level snapshots are unavailable. This is a
                # deliberately weaker price/amount proxy for coverage stats.
                ratio = self._amount_ratio(amounts, [], idx)
                score = _clamp(self._slope(prices, idx, 3) * 35 + (ratio - 1) * 18, -100, 100)
            l1_point = evaluate_l1_flow(
                {
                    "available": bool(transaction_count),
                    "rolling_score": score,
                    "rolling_imbalance_pct": imbalance,
                }
            )
            output.append(
                {
                    "flow_score": score,
                    "large_imbalance": large_imbalance,
                    "buy_amount": buy,
                    "sell_amount": sell,
                    "large_buy_amount": large_buy,
                    "large_sell_amount": large_sell,
                    "transaction_count": transaction_count,
                    "flow_source": "easy_tdx_history_transaction" if transaction_count else "minute_price_amount_proxy",
                    "flow_available": bool(transaction_count),
                    "flow_threshold_amount": threshold,
                    "neutral_amount": neutral,
                    "l1_available": bool(l1_point.get("available")),
                    "l1_indicators_present": bool(transaction_count),
                    "l1_buy_support": bool(l1_point.get("l1_buy_support")),
                    "l1_sell_pressure": bool(l1_point.get("l1_sell_pressure")),
                }
            )
        return output

    def _bar_arrays(
        self,
        bars: list[dict[str, Any]],
        *,
        index: bool,
        fallback: float = 0.0,
    ) -> tuple[list[float], list[float]]:
        prices: list[float] = []
        amounts: list[float] = []
        last = fallback
        for row in bars:
            price = _finite(row.get("price"), last)
            if price <= 0:
                price = last
            last = price or last
            volume = max(0.0, _finite(row.get("vol") or row.get("volume")))
            explicit_amount = max(0.0, _finite(row.get("amount")))
            prices.append(price)
            amounts.append(
                explicit_amount
                if explicit_amount > 0
                else volume * price * (1 if index else 100)
            )
        return prices, amounts

    def _amount_ratio(self, amounts: list[float], previous_amounts: list[float], idx: int) -> float:
        current = amounts[idx] if idx < len(amounts) else 0
        prior = [value for value in amounts[max(0, idx - self.config.volume_window):idx] if value > 0]
        baseline = _median(prior)
        if len(prior) < 3:
            if idx < len(previous_amounts) and previous_amounts[idx] > 0:
                baseline = previous_amounts[idx]
            elif not baseline:
                baseline = _median(previous_amounts)
        return _clamp(current / baseline if baseline else 1.0, 0.1, 10.0)

    @staticmethod
    def _slope(values: list[float], idx: int, lookback: int) -> float:
        if not values or idx <= 0 or values[idx] <= 0:
            return 0.0
        previous = values[max(0, idx - max(1, lookback))]
        return _pct(values[idx], previous) if previous else 0.0

    def _formula_features(
        self,
        prices: list[float],
        idx: int,
        vwap: float,
        running_high: float,
        *,
        formula_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if formula_state:
            quick_entry = bool(formula_state.get("quick_entry") or formula_state.get("赶快出手"))
            main_absorption = _finite(
                formula_state.get("main_absorption", formula_state.get("主力吸筹"))
            )
            near_trend = bool(formula_state.get("near_trend_line") or formula_state.get("是否接近趋势线"))
            trend_distance = _finite(
                formula_state.get(
                    "trend_distance_pct",
                    formula_state.get("趋势线最近距离_pct"),
                )
            )
            white_distance = _finite(
                formula_state.get(
                    "white_distance_pct",
                    formula_state.get("白线距离_pct"),
                )
            )
            yellow_distance = _finite(
                formula_state.get(
                    "yellow_distance_pct",
                    formula_state.get("黄线距离_pct"),
                )
            )
            duo_strength = _finite(
                formula_state.get("duo_strength", formula_state.get("多方力度"))
            )
            kong_strength = _finite(
                formula_state.get("kong_strength", formula_state.get("空方力度"))
            )
            buy_candidate = bool(
                formula_state.get("buy_candidate")
                or quick_entry
                or (main_absorption > 0 and near_trend)
            )
            sell_candidate = bool(
                formula_state.get("sell_candidate")
                or formula_state.get("sell_trigger")
            )
            trend_score = _clamp(
                100.0
                - trend_distance * 80.0
                + _clamp(duo_strength - kong_strength, -40.0, 40.0) * 0.25
                + (12.0 if quick_entry else 0.0)
                + (6.0 if main_absorption > 0 else 0.0),
                0.0,
                100.0,
            )
            return {
                "support": buy_candidate,
                "exhaustion": sell_candidate,
                "trend_score": trend_score,
                "quick_entry": quick_entry,
                "main_absorption": main_absorption,
                "white_line": _finite(formula_state.get("white_line", formula_state.get("白线"))),
                "yellow_line": _finite(formula_state.get("yellow_line", formula_state.get("黄线"))),
                "white_distance_pct": white_distance,
                "yellow_distance_pct": yellow_distance,
                "trend_distance_pct": trend_distance,
                "near_trend_line": near_trend,
                "near_trend_line_name": str(formula_state.get("near_trend_line_name") or ""),
                "buy_candidate": buy_candidate,
                "sell_candidate": sell_candidate,
            }
        window = prices[max(0, idx - 26):idx + 1]
        if len(window) < 3:
            return {
                "support": False,
                "exhaustion": False,
                "trend_score": 50.0,
                "quick_entry": False,
                "main_absorption": 0.0,
                "white_line": 0.0,
                "yellow_line": 0.0,
                "white_distance_pct": 999.0,
                "yellow_distance_pct": 999.0,
                "trend_distance_pct": 999.0,
                "near_trend_line": False,
                "near_trend_line_name": "",
                "buy_candidate": False,
                "sell_candidate": False,
            }
        short = prices[max(0, idx - 8):idx + 1]
        changes = [short[pos] - short[pos - 1] for pos in range(1, len(short))]
        gains = sum(value for value in changes if value > 0)
        losses = sum(-value for value in changes if value < 0)
        rsi = 100.0 if losses == 0 and gains > 0 else 50.0 if gains == losses == 0 else 100 - 100 / (1 + gains / losses)
        def stochastic(length: int) -> float:
            part = prices[max(0, idx - length + 1):idx + 1]
            low, high = min(part), max(part)
            return (part[-1] - low) / (high - low) * 100 if high > low else 50.0
        stoch9 = stochastic(9)
        stoch27 = stochastic(27)
        slope4 = self._slope(prices, idx, 4)
        trend_score = stoch27 * 0.55 + stoch9 * 0.25 + _clamp(slope4 * 30 + 50, 0, 100) * 0.20
        support = bool(
            slope4 >= 0.05
            and (prices[idx] >= vwap or stoch9 <= 65)
            and trend_score >= 42
        )
        exhaustion = bool(
            rsi >= 82
            or (stoch9 >= 88 and running_high > 0 and prices[idx] >= running_high * 0.995)
        )
        return {
            "support": support,
            "exhaustion": exhaustion,
            "trend_score": trend_score,
            "rsi": rsi,
            "quick_entry": False,
            "main_absorption": 0.0,
            "white_line": 0.0,
            "yellow_line": 0.0,
            "white_distance_pct": 999.0,
            "yellow_distance_pct": 999.0,
            "trend_distance_pct": 999.0,
            "near_trend_line": False,
            "near_trend_line_name": "",
            "buy_candidate": support,
            "sell_candidate": exhaustion,
        }

    @staticmethod
    def _recent_true(values: list[Any], window: int) -> bool:
        return any(bool(value) for value in values[-max(1, window):])

    def _market_confluence_state(
        self,
        metrics: list[dict[str, Any]],
        idx: int,
    ) -> dict[str, Any]:
        """Combine index turn and volume evidence within one session window."""
        if not metrics or idx < 0:
            return {
                "market_recent": False,
                "market_turn_recent": False,
                "market_volume_recent": False,
                "market_turn_time": "",
                "market_volume_time": "",
                "index_window_max_amount_ratio": 1.0,
            }
        current = min(idx, len(metrics) - 1)
        session_start = 120 if current >= 120 else 0
        start = max(session_start, current - max(1, self.config.confluence_window) + 1)
        window = metrics[start : current + 1]
        turn_rows = [row for row in window if row.get("turning")]
        volume_rows = [row for row in window if row.get("amount_expanding")]
        return {
            "market_recent": bool(turn_rows and volume_rows),
            "market_turn_recent": bool(turn_rows),
            "market_volume_recent": bool(volume_rows),
            "market_turn_time": str(turn_rows[-1].get("time") or "") if turn_rows else "",
            "market_volume_time": str(volume_rows[-1].get("time") or "") if volume_rows else "",
            "index_window_max_amount_ratio": round(
                max((_finite(row.get("amount_ratio"), 1) for row in window), default=1.0),
                2,
            ),
            "context_breadth": round(_finite(metrics[current].get("context_breadth")), 3),
            "context_breadth_delta": round(
                max((_finite(row.get("context_breadth_delta")) for row in window), default=0.0),
                3,
            ),
            "context_strong_breadth": round(
                _finite(metrics[current].get("context_strong_breadth")), 3
            ),
            "context_advancing_amount_share": round(
                _finite(metrics[current].get("context_advancing_amount_share")), 3
            ),
            "context_advancing_amount_delta": round(
                max(
                    (_finite(row.get("context_advancing_amount_delta")) for row in window),
                    default=0.0,
                ),
                3,
            ),
        }

    @staticmethod
    def _aligned_factor_history(metrics: list[dict[str, Any]], idx: int) -> list[dict[str, Any]]:
        return metrics[: idx + 1]

    def _load_metadata(self) -> dict[str, dict[str, Any]]:
        path = self.settings.data_dir / "runtime" / "stock_basic_cache.json"
        if not path.exists():
            raise DataSourceError("缺少 stock_basic_cache.json，请先运行一次全市场行情初始化")
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows", [])
        return {
            str(row.get("symbol") or str(row.get("ts_code") or "")[:6]).zfill(6): dict(row)
            for row in rows
            if isinstance(row, dict) and _valid_code(row.get("symbol") or str(row.get("ts_code") or "")[:6])
        }

    def _protocol_samples_for_day(
        self,
        trade_date: str,
        series_by_code: Mapping[str, StockSeries],
        *,
        market_rows: Sequence[Mapping[str, Any]],
        eligible_codes: set[str],
        context_codes: set[str] | None = None,
    ) -> list[ResearchSample]:
        """Build protocol samples from the ex-ante universe for one day.

        The old V3 report may remove one-word bars from its measured trade
        list.  The research-first protocol keeps them as explicit ``no_fill``
        samples, so this helper deliberately records the flag instead of
        filtering it away.
        """

        result: list[ResearchSample] = []
        peer_universe = context_codes or set(series_by_code)
        for code in sorted(eligible_codes):
            series = series_by_code.get(code)
            if series is None or not series.bars:
                continue
            peers = [
                peer.bars
                for peer_code, peer in series_by_code.items()
                if (
                    peer_code in peer_universe
                    and peer_code != code
                    and peer.industry == series.industry
                    and peer.bars
                )
            ][:5]
            daily_row = self.daily.get(trade_date, {}).get(code, {})
            one_word = bool(self._one_word_reason(daily_row, code)) if daily_row else False
            daily_history = self._daily_history_for_code(code, trade_date)
            daily_regime = compute_daily_regime(daily_history)
            daily_regime["history_dates"] = [
                str(row.get("trade_date") or row.get("date") or "")
                for row in daily_history
            ]
            auction_prior = self._auction_prior_for_code(
                trade_date,
                code,
                _finite(series.prev_close),
            )
            result.append(
                ResearchSample(
                    code=series.code,
                    name=series.name,
                    trade_date=trade_date,
                    bars=[dict(row) for row in series.bars],
                    transactions=[dict(row) for row in series.transactions],
                    market_bars=[dict(row) for row in market_rows],
                    sector_bars=[[dict(row) for row in rows] for rows in peers],
                    sector_name=series.industry,
                    metadata={
                        "prev_close": series.prev_close,
                        "industry": series.industry,
                        "market": series.market,
                        "daily_regime": daily_regime,
                        "daily_history_count": len(daily_history),
                    },
                    one_word=one_word,
                    auction_prior=auction_prior,
                    source_quality=("l1_transaction" if series.transactions else "minute_proxy"),
                )
            )
        return result

    def _daily_history_for_code(self, code: str, before_date: str) -> list[dict[str, Any]]:
        """Load return-adjusted daily rows strictly before a target day."""

        normalized_code = str(code).zfill(6)
        target = str(before_date or "")
        available_dates: set[str] = {
            str(value)
            for value in self.daily
            if str(value).isdigit() and len(str(value)) == 8
        }
        for path in (self.settings.data_dir / "runtime").glob("daily_*.json"):
            value = path.stem.removeprefix("daily_")
            if len(value) == 8 and value.isdigit():
                available_dates.add(value)
        history: list[dict[str, Any]] = []
        for trade_date in sorted(value for value in available_dates if value < target):
            try:
                self._ensure_daily(trade_date)
            except Exception:
                # A missing/denied remote day is a data-quality limitation,
                # not a reason to invent a daily indicator.
                continue
            row = self.daily.get(trade_date, {}).get(normalized_code)
            if not row:
                continue
            enriched = dict(row)
            enriched.setdefault("trade_date", trade_date)
            history.append(enriched)
        return self._return_adjusted_daily_history(
            history[-max(20, int(self.config.daily_history_sessions)):]
        )

    def _prepare_daily_history(self, target_dates: Sequence[str]) -> dict[str, Any]:
        """Materialize enough prior sessions for point-in-time daily context."""

        targets = sorted({str(value) for value in target_dates if str(value)})
        required = max(20, int(self.config.daily_history_sessions))
        if not targets:
            self.daily_history_status = {
                "status": "unavailable",
                "required_sessions": required,
                "loaded_sessions": 0,
                "dates": [],
                "source": "unavailable",
                "error": "没有目标交易日",
            }
            return self.daily_history_status
        earliest = targets[0]
        runtime = self.settings.data_dir / "runtime"
        local_dates = {
            value
            for pattern in ("daily_*.json", "easy_tdx_daily_*.json")
            for path in runtime.glob(pattern)
            for value in [path.stem.removeprefix("easy_tdx_daily_").removeprefix("daily_")]
            if value.isdigit()
            and len(value) == 8
            and value < earliest
        }
        local_dates.update(
            str(value)
            for value, rows in self.daily.items()
            if str(value).isdigit()
            and len(str(value)) == 8
            and str(value) < earliest
            and rows
        )
        source = "local_daily_cache"
        error = ""
        requested_dates: set[str] = set(local_dates)
        if len(local_dates) < required:
            try:
                calendar_dates = EasyTdxDailyDataSource(self.settings).trade_dates_before(
                    earliest,
                    required,
                )
                requested_dates.update(calendar_dates)
                source = "easy_tdx_index_calendar_and_daily"
            except Exception as exc:
                error = str(exc)
        selected_dates = sorted(value for value in requested_dates if value < earliest)[-required:]
        loaded_dates: list[str] = []
        failures: list[str] = []
        for trade_date in selected_dates:
            try:
                self._ensure_daily(trade_date)
            except Exception as exc:
                failures.append(f"{trade_date}: {exc}")
                continue
            if self.daily.get(trade_date):
                loaded_dates.append(trade_date)
            else:
                failures.append(f"{trade_date}: 日线为空")
        if failures:
            error = "；".join(([error] if error else []) + failures[:5])
        self.daily_history_status = {
            "status": "available" if len(loaded_dates) >= required else "insufficient_history",
            "required_sessions": required,
            "loaded_sessions": len(loaded_dates),
            "dates": loaded_dates,
            "source": source,
            "error": error,
            "strictly_before": earliest,
            "price_basis": "official_pct_chg_return_chain",
        }
        return self.daily_history_status

    @staticmethod
    def _return_adjusted_daily_history(
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build a point-in-time return index from official daily pct_chg.

        easy_tdx unadjusted OHLC can jump on an ex-right/ex-dividend date.
        Chaining the daily return keeps MA/MACD/ADX on one continuous scale
        without requesting a future adjustment factor.
        """

        ordered = sorted(
            (dict(row) for row in rows if isinstance(row, Mapping)),
            key=lambda row: str(row.get("trade_date") or row.get("date") or ""),
        )
        result: list[dict[str, Any]] = []
        adjusted_close = 100.0
        for row in ordered:
            close = _finite(row.get("close") or row.get("price"))
            if close <= 0:
                continue
            if result:
                change_pct = row.get("pct_chg")
                if change_pct is None:
                    previous = _finite(row.get("pre_close"))
                    change_pct = _pct(close, previous) if previous > 0 else 0.0
                adjusted_close *= max(0.01, 1.0 + _finite(change_pct) / 100.0)
            scale = adjusted_close / close
            enriched = dict(row)
            enriched.update(
                {
                    "adj_close": adjusted_close,
                    "adj_open": _finite(row.get("open"), close) * scale,
                    "adj_high": _finite(row.get("high"), close) * scale,
                    "adj_low": _finite(row.get("low"), close) * scale,
                    "adjustment_method": "official_pct_chg_return_chain",
                }
            )
            result.append(enriched)
        return result

    def _auction_prior_for_code(
        self,
        trade_date: str,
        code: str,
        prev_close: float,
    ) -> dict[str, Any]:
        """Read the historical 09:25 auction proxy from easy_tdx transaction tape."""

        row = self.feed.auction_0925(code, trade_date)
        if not row.get("available"):
            return {
                "available": False,
                "source": str(row.get("source") or "unavailable"),
                "note": str(row.get("note") or "L1历史成交未返回09:25竞价代理"),
            }
        match_price = _finite(
            row.get("match_price") or row.get("auction_price") or row.get("price")
        )
        change_pct = _pct(match_price, prev_close) if match_price > 0 and prev_close > 0 else None
        return {
            "available": bool(match_price > 0),
            "source": str(row.get("source") or "easy_tdx_history_transaction_data"),
            "match_price": match_price or None,
            "change_pct": change_pct,
            "order_imbalance_pct": row.get("order_imbalance_pct"),
            "volume": row.get("volume") or row.get("auction_vol") or row.get("vol"),
            "raw_fields": {
                key: row.get(key)
                for key in ("match_price", "price", "volume", "amount", "side", "as_of")
                if key in row
            },
        }

    def _ensure_daily(self, trade_date: str) -> None:
        if trade_date in self.daily:
            return
        runtime = self.settings.data_dir / "runtime"
        path = runtime / f"easy_tdx_daily_{trade_date}.json"
        legacy_path = runtime / f"daily_{trade_date}.json"
        rows: list[dict[str, Any]] = []
        read_path = path if path.exists() else legacy_path
        if read_path.exists():
            try:
                payload = json.loads(read_path.read_text(encoding="utf-8"))
                if str(payload.get("trade_date")) == trade_date and isinstance(payload.get("rows"), list):
                    rows = payload["rows"]
            except Exception:
                rows = []
        if not rows:
            universe_path = self.settings.data_dir / "runtime" / "easy_tdx_stock_basic_cache.json"
            universe_rows: list[dict[str, Any]] = []
            if universe_path.exists():
                try:
                    payload = json.loads(universe_path.read_text(encoding="utf-8"))
                    if isinstance(payload.get("rows"), list):
                        universe_rows = payload["rows"]
                except Exception:
                    universe_rows = []
            universe = {
                str(row.get("symbol") or "").zfill(6): StockMeta(
                    code=str(row.get("symbol") or "").zfill(6),
                    ts_code=str(row.get("ts_code") or ""),
                    name=str(row.get("name") or row.get("symbol") or ""),
                    industry=str(row.get("industry") or ""),
                    market=str(row.get("market") or ""),
                    exchange=str(row.get("exchange") or ""),
                )
                for row in universe_rows
                if str(row.get("symbol") or "").strip().isdigit()
            }
            rows = EasyTdxDailyDataSource(self.settings)._fetch_daily_rows(universe, trade_date)
        self.daily[trade_date] = {
            str(row.get("symbol") or str(row.get("ts_code") or "")[:6]).zfill(6): dict(row)
            for row in rows
            if isinstance(row, dict)
        }

    def _previous_date(self, date: str) -> str:
        """Resolve the prior trading day from local caches/calendar.

        A research run must work for arbitrary date ranges.  Prefer dates
        already materialized locally, then ask easy_tdx's index calendar, and
        only use a weekday fallback when no provider/cache is available.  The
        fallback is marked by the caller's data-quality failures if the day
        is actually a holiday.
        """

        normalized = str(date or "").strip()
        try:
            target = datetime.strptime(normalized, "%Y%m%d").date()
        except ValueError as exc:
            raise DataSourceError(f"交易日格式无效：{date}") from exc
        cache_dates: set[str] = set()
        cache_dates.update(
            str(value)
            for value in self.daily
            if len(str(value)) == 8 and str(value).isdigit() and str(value) < normalized
        )
        for pattern in ("daily_*.json", "easy_tdx_daily_*.json"):
            for path in (self.settings.data_dir / "runtime").glob(pattern):
                value = path.stem.removeprefix("easy_tdx_daily_").removeprefix("daily_")
                if len(value) == 8 and value.isdigit() and value < normalized:
                    cache_dates.add(value)
        for path in self.feed.minute_dir.glob("*_index_000001.json"):
            value = path.name.split("_", 1)[0]
            if len(value) == 8 and value.isdigit() and value < normalized:
                cache_dates.add(value)
        if cache_dates:
            return max(cache_dates)
        try:
            calendar = EasyTdxDailyDataSource(self.settings)._recent_trade_dates()
            prior = sorted(
                str(item) for item in calendar
                if str(item).isdigit() and str(item) < normalized
            )
            if prior:
                return prior[-1]
        except Exception:
            pass
        from datetime import timedelta

        cursor = target - timedelta(days=1)
        while cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
        return cursor.strftime("%Y%m%d")

    @staticmethod
    def _eligible_row(row: dict[str, Any]) -> bool:
        code = str(row.get("symbol") or "").zfill(6)
        name = str(row.get("name") or "")
        return bool(
            _valid_code(code)
            and not any(token in name.upper() for token in ("ST", "退", "N"))
            and _finite(row.get("pre_close")) > 0
            and _finite(row.get("amount")) > 0
        )

    def _build_report(
        self,
        selection: dict[str, Any],
        date_summaries: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        sell_zones: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        formula_candidates: list[dict[str, Any]] | None = None,
        opening_records: list[dict[str, Any]] | None = None,
        v3_trades: list[dict[str, Any]] | None = None,
        v3_rejections: list[dict[str, Any]] | None = None,
        v3_stress_trades: dict[float, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        formula_candidates = list(candidates if formula_candidates is None else formula_candidates)
        opening_records = list(opening_records or [])
        v3_trades = list(v3_trades or [])
        v3_rejections = list(v3_rejections or [])
        v3_stress_trades = dict(v3_stress_trades or {})
        factor_lift = self._factor_lift(candidates)
        sensitivity = self._sensitivity(candidates)
        formula_grid = self._formula_grid(formula_candidates)
        independent = self._independent_stats(candidates)
        strategy_v2 = self._strategy_v2_stats(candidates)
        strategy_diagnostics = self._strategy_diagnostics(candidates)
        good = [item for item in candidates if item.get("outcome", {}).get("good_30")]
        failures = [item for item in candidates if item.get("outcome", {}).get("failed_30")]
        good.sort(key=lambda item: (item["trade_date"], item["time"], -item["factor_count"]))
        failures.sort(key=lambda item: (item["trade_date"], item["time"], -item["factor_count"]))
        data_quality = self._data_quality_contract()
        rejection_counts: dict[str, int] = defaultdict(int)
        for item in v3_rejections:
            rejection_counts[str(item.get("status") or item.get("reason") or "其他")] += 1
        rejection_reason_counts: dict[str, int] = defaultdict(int)
        for item in v3_rejections:
            rejection_reason_counts[str(item.get("reason") or "其他")] += 1
        pulse_trades = self._deduplicate_v3_pulses(v3_trades)
        study_dates = sorted(
            {
                str(item.get("date") or item.get("trade_date") or "")
                for item in date_summaries
                if str(item.get("date") or item.get("trade_date") or "")
            }
        )
        required_screening_days = max(20, int(self.config.protocol_minimum_days))
        required_validation_days = max(60, int(self.config.protocol_oos_days))
        minimum_events = max(30, int(self.config.protocol_minimum_events))
        split_at = (
            min(len(study_dates) - 1, max(required_screening_days, int(len(study_dates) * 0.70)))
            if len(study_dates) > 1
            else len(study_dates)
        )
        training_dates = set(study_dates[:split_at])
        oos_dates = set(study_dates[split_at:])
        oos_trades = [
            item for item in v3_trades if str(item.get("trade_date") or "") in oos_dates
        ]
        base_stats = self._v3_trade_stats(v3_trades)
        oos_stats = self._v3_trade_stats(oos_trades)
        stress_tests = [
            {
                "target_r": ratio,
                **self._v3_trade_stats(rows),
                "oos": self._v3_trade_stats(
                    [item for item in rows if str(item.get("trade_date") or "") in oos_dates]
                ),
            }
            for ratio, rows in sorted(v3_stress_trades.items())
        ]
        stable_stress_pairs = sum(
            bool(
                _finite(current.get("oos", {}).get("expectancy_pct")) > 0
                and _finite(current.get("oos", {}).get("average_r")) > 0
                and _finite(following.get("oos", {}).get("expectancy_pct")) > 0
                and _finite(following.get("oos", {}).get("average_r")) > 0
            )
            for current, following in zip(stress_tests, stress_tests[1:])
        )
        validation_reasons: list[str] = []
        if len(study_dates) < required_screening_days:
            validation_reasons.append(
                f"仅覆盖{len(study_dates)}个交易日，低于{required_screening_days}日假设筛选和"
                f"{required_validation_days}日滚动样本外门槛"
            )
        elif len(study_dates) < required_validation_days:
            validation_reasons.append(
                f"仅覆盖{len(study_dates)}个交易日，低于{required_validation_days}日滚动样本外门槛"
            )
        if len(v3_trades) < minimum_events:
            validation_reasons.append(
                f"V3仅形成{len(v3_trades)}笔交易，低于最低{minimum_events}笔验证样本"
            )
        prerequisites_ready = bool(
            len(study_dates) >= required_validation_days
            and len(v3_trades) >= minimum_events
        )
        if prerequisites_ready and len(oos_trades) < minimum_events:
            validation_reasons.append(
                f"独立区间仅形成{len(oos_trades)}笔交易，低于最低{minimum_events}笔样本"
            )
        if prerequisites_ready and oos_trades and (
            _finite(oos_stats.get("expectancy_pct")) <= 0
            or _finite(oos_stats.get("average_r")) <= 0
        ):
            validation_reasons.append("独立区间净期望或平均R未保持为正")
        if prerequisites_ready and stable_stress_pairs <= 0:
            validation_reasons.append("相邻压力情景尚未形成稳定正期望平台")
        deployable = not validation_reasons
        v3_report = {
            "schema_version": "t_strategy_v3_risk_reward",
            "definition": (
                "V2盘面共振只负责发现机会；V3在候选分钟建立支撑、失效价、目标价和不追价上限，"
                "下一分钟保守成交，赔率不足直接放弃。持有后优先执行失效止损/目标兑现，"
                "成交分钟再做位置重估，并通过过热、追涨、逐笔承接和板块扩散的场景否决链。"
                "其余卖点必须由个股恶化参与的至少两项衰竭共同触发。"
            ),
            "validation_status": "deployable" if deployable else "sample_insufficient",
            "deployable": deployable,
            "validation_reasons": validation_reasons,
            "entry_assumption": "候选分钟收盘制定计划；下一分钟收盘价不高于不追价上限才视为成交。",
            "exit_assumption": (
                "分钟收盘触发失效或目标时按更保守价格成交；动态衰竭在下一分钟收盘成交；"
                f"每笔扣除至少{max(self.config.friction_pct, 0.20):.2f}%双边摩擦。"
            ),
            "stats": base_stats,
            "validation_window": {
                "required_screening_days": required_screening_days,
                "required_validation_days": required_validation_days,
                "minimum_events": minimum_events,
                "training_dates": sorted(training_dates),
                "oos_dates": sorted(oos_dates),
                "oos_stats": oos_stats,
                "stable_stress_pair_count": stable_stress_pairs,
            },
            "pulse_deduplicated_count": len(pulse_trades),
            "pulse_deduplicated_stats": self._v3_trade_stats(pulse_trades),
            "breakdowns": self._v3_breakdowns(v3_trades),
            "rejected_count": len(v3_rejections),
            "rejection_status_counts": dict(sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))),
            "rejection_reason_counts": dict(
                sorted(rejection_reason_counts.items(), key=lambda item: (-item[1], item[0]))
            ),
            "stress_tests": stress_tests,
            "scene_threshold_sensitivity": self._scene_threshold_sensitivity(candidates),
        }
        return {
            "schema_version": "t_strategy_event_study_v3_risk_reward",
            "generated_at": datetime.now().isoformat(),
            "data_quality": data_quality,
            "methodology": {
                "hypothesis": (
                    "指数拐头与放量可在相邻分钟窗口分别确认，并与板块核心进攻构成硬环境；"
                    "个股低位承接和当前分钟放量构成执行条件；"
                    "L1成交方向只负责升级置信度或在明显抛压时否决，缺失时降级为B级分钟量价代理；"
                    "是否出手先看失效位和上方空间，卖出则追踪入场后的目标、R倍数和动态衰竭。"
                ),
                "information_boundary": "候选因子只使用当前分钟及之前数据；前一交易日仅用于样本选择和分钟量能基准。",
                "entry_assumption": "候选分钟收盘后，下一根分钟收盘价作为保守入场代理；不模拟涨停排队成交。",
                "outcome_definition": {
                    "good_30": f"30分钟MFE >= {self.config.good_mfe_30:.2f}%，15分钟MAE >= {self.config.good_mae_15:.2f}%，扣{self.config.friction_pct:.2f}%摩擦后30分钟收益 >= {self.config.good_net_30:.2f}%",
                    "failed_30": f"15分钟MAE <= {self.config.stop_loss_pct:.2f}% 或扣摩擦后30分钟收益为负",
                },
                "flow_definition": data_quality["flow_definition"],
                "strategy_v2_definition": (
                    "指数拐头与量能在同一短窗口内分别出现、板块确认是硬门槛；"
                    "个股低位承接和当前分时放量是执行门槛；"
                    "L1成交方向只做支持/中性/抛压分级，强抛压否决，缺失时降级为分钟量价代理。"
                ),
                "strategy_v3_definition": v3_report["definition"],
                "sector_definition": "按前一交易日元数据分组的行业；板块情绪只用样本成员截至当前分钟的上涨宽度、平均涨跌、核心容量票进攻和上板代理。",
                "formula_inspiration": "按做T公式.md和趋势公式.md落地分时公式字段；赶快出手源码常量0，研究触发使用CROSS(多方力度,6.78)。",
                "formula_grid_definition": (
                    "做T公式网格的唯一候选口径是赶快出手+主力吸筹，或接近趋势公式.md的白线/黄线；"
                    "不读取旧V2/V3决策。L1逐笔层只提供时点买盘支持/明显抛压布尔量，"
                    "所有阈值和L1组合输出保持research_only。"
                ),
                "opening_window": (
                    "09:33只初筛，09:35通过市场+板块+个股三层门槛才允许确认买T，"
                    "09:37复核延续性；集合竞价只加分，不替代连续竞价确认。"
                ),
                "limitations": [
                    f"当前仅有{len(study_dates)}个研究交易日；未达到60日滚动样本外要求时只能用于探索。",
                    data_quality["limitation"],
                    "行业情绪使用研究样本成员，不等同于全行业实时涨停家数。",
                    "分钟收盘价不能保证真实成交，涨停和快速跳价的结果需人工复核。",
                ],
            },
            "config": self.config.__dict__,
            "sample": selection,
            "date_summaries": date_summaries,
            "skipped": skipped,
            "summary": {
                "candidate_count": len(candidates),
                "formula_observation_count": len(formula_candidates),
                "formula_candidate_count": formula_grid.get("formula_candidate_union_count", 0),
                "strict_four_factor_count": sum(1 for item in candidates if item.get("strict_four_factor")),
                "good_30_count": len(good),
                "good_30_rate_all_pct": round(len(good) / len(candidates) * 100, 2) if candidates else 0,
                "failure_30_count": len(failures),
                "sell_zone_count": len(sell_zones),
                "simulated_trade_count": len(trades),
                "v3_trade_count": len(v3_trades),
                "v3_rejected_count": len(v3_rejections),
                "v3_expectancy_pct": v3_report["stats"]["expectancy_pct"],
                "v3_average_r": v3_report["stats"]["average_r"],
                "strategy_v2_formal_count": sum(
                    1 for item in candidates if item.get("strategy_v2", {}).get("eligible")
                ),
                "strategy_v2_a_count": sum(
                    1 for item in candidates if item.get("strategy_v2", {}).get("grade") == "A"
                ),
                "strategy_v2_b_count": sum(
                    1 for item in candidates if item.get("strategy_v2", {}).get("grade") == "B"
                ),
                "transaction_coverage_pct": round(
                    sum(1 for item in candidates if item.get("metrics", {}).get("flow_is_l1")) / max(len(candidates), 1) * 100,
                    2,
                ),
            },
            "factor_lift": factor_lift,
            "strategy_v2": strategy_v2,
            "strategy_diagnostics": strategy_diagnostics,
            "strategy_v3": v3_report,
            "threshold_sensitivity": sensitivity,
            "formula_grid": formula_grid,
            "independent_event_stats": independent,
            "opening": self._opening_report(opening_records),
            "opening_events": opening_records[:1200],
            "all_candidates": candidates,
            "good_candidates": good[:40],
            "failure_candidates": failures[:40],
            "sell_zones": sell_zones[:200],
            "trades": trades,
            "v3_trades": v3_trades,
            "v3_rejections": v3_rejections,
        }

    def _opening_report(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Summarize fixed opening checkpoints without optimizing thresholds."""
        action_counts = {
            action: sum(1 for item in records if item.get("action") == action)
            for action in ("竞价候选", "初筛候选", "确认买T", "观察", "回避", "减T")
        }
        by_checkpoint: list[dict[str, Any]] = []
        for checkpoint in CHECKPOINTS:
            selected = [item for item in records if item.get("checkpoint") == checkpoint]
            buys = [item for item in selected if item.get("action") == "确认买T"]
            candidates = [
                item
                for item in selected
                if item.get("action") in {"竞价候选", "初筛候选", "确认买T"}
            ]
            by_checkpoint.append(
                {
                    "checkpoint": checkpoint,
                    "all": self._stats(selected),
                    "candidates": self._stats(candidates),
                    "confirmed_buy": self._stats(buys),
                    "buy_count": len(buys),
                    "candidate_count": len(candidates),
                    "market_gate_count": sum(1 for item in selected if item.get("market_gate")),
                    "sector_gate_count": sum(1 for item in selected if item.get("sector_gate")),
                }
            )
        dates = sorted({str(item.get("trade_date") or "") for item in records})
        daily_variation = [
            {
                "trade_date": trade_date,
                "checkpoints": [
                    {
                        "checkpoint": checkpoint,
                        "buy_count": sum(
                            1
                            for item in records
                            if item.get("trade_date") == trade_date
                            and item.get("checkpoint") == checkpoint
                            and item.get("action") == "确认买T"
                        ),
                        "candidate_count": sum(
                            1
                            for item in records
                            if item.get("trade_date") == trade_date
                            and item.get("checkpoint") == checkpoint
                            and item.get("action") in {"竞价候选", "初筛候选", "确认买T"}
                        ),
                        "stats": self._stats(
                            [
                                item
                                for item in records
                                if item.get("trade_date") == trade_date
                                and item.get("checkpoint") == checkpoint
                                and item.get("action") in {"竞价候选", "初筛候选", "确认买T"}
                            ]
                        ),
                    }
                    for checkpoint in CHECKPOINTS
                ],
            }
            for trade_date in dates
        ]
        return {
            "schema_version": "opening_window_study_v1",
            "checkpoints": list(CHECKPOINTS),
            "records_count": len(records),
            "action_counts": action_counts,
            "by_checkpoint": by_checkpoint,
            "daily_variation": daily_variation,
            "data_boundary": "只使用09:31至检查时刻的历史分钟量价和L1成交方向；没有历史队列数据；竞价缺失不当作负因子。",
            "threshold_note": "阈值为逻辑预设，当前两交易日仅用于探索性复盘，至少扩展到20个交易日后再评估稳定性。",
        }

    def _data_quality_contract(self) -> dict[str, Any]:
        """Describe the active flow mode without implying unavailable data."""
        if self.flow_enabled:
            return {
                "flow_mode": "easy_tdx_history_transaction",
                "historical_transactions_requested": True,
                "level2_available": False,
                "decision_role": "confidence_bonus_or_strong_pressure_veto",
                "note": "本次读取L1历史逐笔成交明细；side/buyorsell和大额成交差用于成交方向代理，不是队列数据。",
                "flow_definition": "L1历史逐笔成交明细的side/buyorsell与大额成交差，只作A/B分级和明显抛压否决；不是队列数据。",
                "limitation": "历史无五档轨迹和队列数据；buyorsell不能还原委托队列、撤单和真实主力订单。",
            }
        return {
            "flow_mode": "minute_price_amount_proxy",
            "historical_transactions_requested": False,
            "level2_available": False,
            "decision_role": "b_grade_price_amount_proxy_only",
            "note": "本次未读取历史逐笔成交；流向只由分钟价格方向与成交额加速估算，统一降级为B级代理。",
            "flow_definition": "仅使用分钟价格方向、分时均价和成交额加速构造流向代理；没有逐笔成交、五档轨迹或队列数据。",
            "limitation": "本次运行没有历史逐笔成交、五档轨迹或队列数据；所有盘口流向结论均为B级分钟量价代理。",
        }

    def _strategy_v2_stats(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Summarize the deployable V2 grades without hiding proxy quality."""
        groups = [
            ("V2正式候选（硬门槛+无明显抛压）", lambda item: bool(item.get("strategy_v2", {}).get("eligible"))),
            ("V2 A级（L1方向支持）", lambda item: item.get("strategy_v2", {}).get("grade") == "A"),
            ("V2 B级（分钟代理/盘口中性）", lambda item: item.get("strategy_v2", {}).get("grade") == "B"),
            ("硬门槛基线（不做盘口否决）", lambda item: bool(item.get("strategy_v2", {}).get("hard_ready"))),
            (
                "V2暂缓（硬门槛+明显L1抛压）",
                lambda item: bool(
                    item.get("strategy_v2", {}).get("hard_ready")
                    and item.get("strategy_v2", {}).get("flow_pressure")
                ),
            ),
        ]
        output: list[dict[str, Any]] = []
        for name, predicate in groups:
            selected = [item for item in candidates if predicate(item)]
            clustered = self._cluster_events(selected, gap=30)
            output.append(
                {
                    "group": name,
                    **self._stats(selected),
                    "clustered_count": len(clustered),
                    "clustered": self._stats(clustered),
                }
            )
        return output

    def _strategy_diagnostics(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """Describe where the V2 edge appears without selecting a best threshold."""
        formal = [item for item in candidates if item.get("strategy_v2", {}).get("eligible")]
        clustered = self._cluster_events(formal, gap=30)

        time_groups = [
            ("09:33-10:00", lambda value: "09:33" <= value < "10:00"),
            ("10:00-11:30", lambda value: "10:00" <= value <= "11:30"),
            ("13:00-14:00", lambda value: "13:00" <= value < "14:00"),
            ("14:00-15:00", lambda value: "14:00" <= value <= "15:00"),
        ]
        time_buckets = [
            {"bucket": label, **self._stats([item for item in clustered if predicate(str(item.get("time") or ""))])}
            for label, predicate in time_groups
        ]
        date_time_buckets = [
            {
                "trade_date": trade_date,
                "bucket": label,
                **self._stats(
                    [
                        item
                        for item in clustered
                        if str(item.get("trade_date") or "") == trade_date
                        and predicate(str(item.get("time") or ""))
                    ]
                ),
            }
            for trade_date in sorted({str(item.get("trade_date") or "") for item in clustered})
            for label, predicate in time_groups
        ]

        feature_specs = [
            (
                "个股局部量能",
                lambda item: _finite(item.get("metrics", {}).get("amount_ratio"), 1),
                (("<1.5", None, 1.5), ("1.5-2.5", 1.5, 2.5), ("2.5-4", 2.5, 4.0), (">=4", 4.0, None)),
            ),
            (
                "指数窗口峰值量能",
                lambda item: _finite(item.get("metrics", {}).get("index_window_max_amount_ratio"), 1),
                (("<1.08", None, 1.08), ("1.08-1.15", 1.08, 1.15), ("1.15-1.4", 1.15, 1.4), ("1.4-2", 1.4, 2.0), (">=2", 2.0, None)),
            ),
            (
                "板块强度",
                lambda item: _finite(item.get("metrics", {}).get("sector_score")),
                (("<50", None, 50.0), ("50-75", 50.0, 75.0), ("75-90", 75.0, 90.0), ("90-100", 90.0, None)),
            ),
            (
                "日内低点反弹",
                lambda item: _finite(item.get("metrics", {}).get("rebound")),
                (("<1.5%", None, 1.5), ("1.5-3%", 1.5, 3.0), ("3-5%", 3.0, 5.0), (">=5%", 5.0, None)),
            ),
        ]
        feature_buckets: list[dict[str, Any]] = []
        for feature, getter, buckets in feature_specs:
            for label, low, high in buckets:
                selected = [
                    item
                    for item in clustered
                    if (low is None or getter(item) >= low) and (high is None or getter(item) < high)
                ]
                feature_buckets.append({"feature": feature, "bucket": label, **self._stats(selected)})
        variant_specs = [
            ("上午V2", lambda item: str(item.get("time") or "") <= "11:30"),
            (
                "上午+反弹1.5-5%",
                lambda item: str(item.get("time") or "") <= "11:30"
                and 1.5 <= _finite(item.get("metrics", {}).get("rebound")) < 5.0,
            ),
            (
                "上午+板块>=75",
                lambda item: str(item.get("time") or "") <= "11:30"
                and _finite(item.get("metrics", {}).get("sector_score")) >= 75,
            ),
            (
                "上午核心执行区组合",
                lambda item: str(item.get("time") or "") <= "11:30"
                and 1.5 <= _finite(item.get("metrics", {}).get("rebound")) < 5.0
                and _finite(item.get("metrics", {}).get("sector_score")) >= 75
                and _finite(item.get("metrics", {}).get("index_window_max_amount_ratio"), 1) < 1.4
                and _finite(item.get("metrics", {}).get("amount_ratio"), 1) < 4.0,
            ),
        ]
        rule_variants = [
            {"variant": label, **self._stats([item for item in clustered if predicate(item)])}
            for label, predicate in variant_specs
        ]
        core_predicate = variant_specs[-1][1]
        core_by_date: list[dict[str, Any]] = []
        for trade_date in sorted({str(item.get("trade_date") or "") for item in clustered}):
            selected = [
                item
                for item in clustered
                if str(item.get("trade_date") or "") == trade_date and core_predicate(item)
            ]
            core_by_date.append({"trade_date": trade_date, **self._stats(selected)})
        return {
            "scope": "V2正式候选按同股同日30分钟去重；仅用于观察稳健区间，不据此挑选最优阈值。",
            "clustered_count": len(clustered),
            "time_buckets": time_buckets,
            "date_time_buckets": date_time_buckets,
            "feature_buckets": feature_buckets,
            "rule_variants": rule_variants,
            "core_variant_by_date": core_by_date,
        }

    @staticmethod
    def _cluster_events(items: list[dict[str, Any]], gap: int = 30) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            grouped[(str(item.get("trade_date")), str(item.get("code")))].append(item)
        output: list[dict[str, Any]] = []
        for values in grouped.values():
            values.sort(key=lambda item: int(item.get("index", 0)))
            last_index = -999999
            for item in values:
                current = int(item.get("index", 0))
                if current - last_index >= gap:
                    output.append(item)
                    last_index = current
        return output

    def _factor_lift(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        definitions = [
            ("全部候选（至少2因子）", lambda item: item["factor_count"] >= 2),
            ("指数拐头+板块情绪", lambda item: "market" in item["factors"] and "sector" in item["factors"]),
            ("板块情绪+个股放量", lambda item: "sector" in item["factors"] and "volume" in item["factors"]),
            ("个股放量+成交方向代理", lambda item: "volume" in item["factors"] and "flow" in item["factors"]),
            ("指数+个股放量", lambda item: "market" in item["factors"] and "volume" in item["factors"]),
            ("四因子共振", lambda item: bool(item.get("strict_four_factor"))),
            (
                "L1逐笔方向因子",
                lambda item: bool(item.get("metrics", {}).get("flow_is_l1"))
                and "flow" in item.get("factors", []),
            ),
            (
                "分钟量价代理因子",
                lambda item: not bool(item.get("metrics", {}).get("flow_is_l1"))
                and "flow" in item.get("factors", []),
            ),
        ]
        output: list[dict[str, Any]] = []
        for name, predicate in definitions:
            selected = [item for item in candidates if predicate(item)]
            output.append({"group": name, **self._stats(selected)})
        return output

    def _formula_grid(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """Evaluate formula/T-line thresholds without legacy entry gates."""

        thresholds = FORMULA_TREND_THRESHOLDS_PCT
        prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        missing_formula_fields = 0
        for item in candidates:
            point = self._formula_point(item.get("metrics") or {})
            if not point["ready"]:
                missing_formula_fields += 1
                continue
            prepared.append((item, point))

        def formula_hit(point: Mapping[str, Any], threshold: float) -> bool:
            trend_distance = point.get("trend_distance_pct")
            return bool(
                point.get("quick_absorption")
                or (
                    trend_distance is not None
                    and float(trend_distance) <= threshold
                )
            )

        l1_rules: tuple[tuple[str, str, Any, str | None], ...] = (
            (
                "ignore_l1",
                "忽略L1，仅看公式候选",
                lambda point: True,
                None,
            ),
            (
                "veto_l1_pressure",
                "公式候选+明显L1抛压否决",
                lambda point: not point["l1_sell_pressure"],
                "l1_sell_pressure",
            ),
            (
                "require_l1_buy_support",
                "公式候选+L1买盘支持增强",
                lambda point: point["l1_buy_support"] and not point["l1_sell_pressure"],
                "l1_buy_support",
            ),
        )
        minimum_independent_events = max(30, int(self.config.protocol_minimum_events))
        minimum_trading_days = max(20, int(self.config.protocol_minimum_days))
        rows: list[dict[str, Any]] = []
        clustered_event_sets: dict[tuple[str, float], frozenset[tuple[str, str, int]]] = {}
        for threshold in thresholds:
            threshold_hits = [pair for pair in prepared if formula_hit(pair[1], threshold)]
            for key, label, predicate, _ in l1_rules:
                selected = [item for item, point in threshold_hits if predicate(point)]
                clustered = self._cluster_events(selected, gap=30)
                stats = self._formula_grid_stats(selected)
                clustered_stats = self._formula_grid_stats(clustered)
                independent_outcome_count = min(
                    clustered_stats[f"outcome_{horizon}m_count"]
                    for horizon in FORMULA_OUTCOME_HORIZONS
                )
                independent_date_count = len(
                    {
                        str(item.get("trade_date") or "")
                        for item in clustered
                        if str(item.get("trade_date") or "")
                    }
                )
                sample_sufficient = bool(
                    independent_outcome_count >= minimum_independent_events
                    and independent_date_count >= minimum_trading_days
                )
                rows.append(
                    {
                        "status": "research_only",
                        "selected": False,
                        "trend_near_threshold_pct": threshold,
                        "l1_rule": key,
                        "l1_rule_label": label,
                        "formula_candidate_count": len(threshold_hits),
                        "quick_absorption_count": sum(
                            1 for _, point in threshold_hits if point["quick_absorption"]
                        ),
                        "trend_near_count": sum(
                            1
                            for _, point in threshold_hits
                            if point["trend_distance_pct"] is not None
                            and point["trend_distance_pct"] <= threshold
                        ),
                        **stats,
                        "clustered_count": len(clustered),
                        "independent_outcome_count": independent_outcome_count,
                        "independent_date_count": independent_date_count,
                        "sample_sufficient": sample_sufficient,
                        "clustered": clustered_stats,
                    }
                )
                clustered_event_sets[(key, threshold)] = frozenset(
                    (
                        str(item.get("trade_date") or ""),
                        str(item.get("code") or ""),
                        int(_finite(item.get("index"))),
                    )
                    for item in clustered
                )

        plateaus: list[dict[str, Any]] = []
        for key, label, _, _ in l1_rules:
            event_sets = [clustered_event_sets[(key, threshold)] for threshold in thresholds]
            start = 0
            while start < len(thresholds):
                end = start + 1
                while end < len(thresholds) and event_sets[end] == event_sets[start]:
                    end += 1
                if end - start >= 2 and event_sets[start]:
                    matching_rows = [
                        row
                        for row in rows
                        if row["l1_rule"] == key
                        and row["trend_near_threshold_pct"] in thresholds[start:end]
                    ]
                    independent_outcome_count = min(
                        row["independent_outcome_count"] for row in matching_rows
                    )
                    independent_date_count = min(
                        row["independent_date_count"] for row in matching_rows
                    )
                    plateaus.append(
                        {
                            "status": "research_only",
                            "l1_rule": key,
                            "l1_rule_label": label,
                            "thresholds_pct": list(thresholds[start:end]),
                            "independent_event_count": len(event_sets[start]),
                            "independent_outcome_count": independent_outcome_count,
                            "independent_date_count": independent_date_count,
                            "sample_sufficient": (
                                independent_outcome_count >= minimum_independent_events
                                and independent_date_count >= minimum_trading_days
                            ),
                            "selected": False,
                        }
                    )
                start = end

        sufficient_rows = [row for row in rows if row["sample_sufficient"]]
        max_independent_outcomes = max(
            (row["independent_outcome_count"] for row in rows),
            default=0,
        )
        max_independent_dates = max(
            (row["independent_date_count"] for row in rows),
            default=0,
        )
        observed_dates = sorted(
            {
                str(item.get("trade_date") or "")
                for item, _ in prepared
                if str(item.get("trade_date") or "")
            }
        )
        sample_status = "sample_sufficient" if sufficient_rows else "sample_insufficient"
        selection_reason = (
            "样本达到描述性比较门槛；research_only网格只报告平台，不自动选优。"
            if sufficient_rows
            else (
                f"当前{len(observed_dates)}个交易日、最大独立有效样本{max_independent_outcomes}；"
                f"最低要求{minimum_trading_days}日且{minimum_independent_events}个事件，"
                "不选择最优阈值。"
            )
        )
        return {
            "schema_version": "formula_trend_l1_grid_v2",
            "status": "research_only",
            "validation_status": sample_status,
            "definition": (
                "公式候选唯一口径为赶快出手+主力吸筹，或价格接近白/黄趋势线；"
                "不使用strategy_v2、hard_ready、strict_four_factor或V3决策作前置门槛。"
                "L1层只提供时点买盘支持/明显抛压布尔量，网格不重判buyorsell或流量阈值。"
            ),
            "thresholds_pct": list(thresholds),
            "outcome_horizons_minutes": list(FORMULA_OUTCOME_HORIZONS),
            "l1_rules": [
                {"key": key, "label": label, "point_in_time_field": field}
                for key, label, _, field in l1_rules
            ],
            "formula_ready_count": len(prepared),
            "missing_formula_fields": missing_formula_fields,
            "formula_candidate_union_count": sum(
                1
                for _, point in prepared
                if formula_hit(point, max(thresholds))
            ),
            "l1_indicator_ready_count": sum(
                1 for _, point in prepared if point["l1_indicators_present"]
            ),
            "l1_available_count": sum(1 for _, point in prepared if point["l1_available"]),
            "sample_sufficiency": {
                "status": sample_status,
                "observed_trading_days": len(observed_dates),
                "observed_dates": observed_dates,
                "minimum_trading_days": minimum_trading_days,
                "minimum_independent_events": minimum_independent_events,
                "max_independent_date_count": max_independent_dates,
                "max_independent_outcome_count": max_independent_outcomes,
                "sufficient_row_count": len(sufficient_rows),
            },
            "selected_threshold_pct": None,
            "selected_l1_rule": None,
            "selection": {
                "selected": False,
                "trend_near_threshold_pct": None,
                "l1_rule": None,
                "reason": selection_reason,
            },
            "plateau_definition": "相邻阈值命中完全相同的30分钟去重事件集合；不按收益选择平台。",
            "plateaus": plateaus,
            "rows": rows,
        }

    def _formula_grid_stats(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {"count": len(items)}

        def finite_outcome(outcome: Mapping[str, Any], key: str) -> float | None:
            value = outcome.get(key)
            if not isinstance(value, (int, float)):
                return None
            number = float(value)
            return number if math.isfinite(number) else None

        for horizon in FORMULA_OUTCOME_HORIZONS:
            observations: list[tuple[float, float, float]] = []
            for item in items:
                outcome = item.get("outcome") or {}
                ret = finite_outcome(outcome, f"ret_{horizon}m")
                mfe = finite_outcome(outcome, f"mfe_{horizon}m")
                mae = finite_outcome(outcome, f"mae_{horizon}m")
                if ret is None or mfe is None or mae is None:
                    continue
                net = finite_outcome(outcome, f"net_{horizon}m")
                observations.append(
                    (mfe, mae, net if net is not None else ret - self.config.friction_pct)
                )
            net_values = [value[2] for value in observations]
            output[f"outcome_{horizon}m_count"] = len(observations)
            output[f"avg_mfe_{horizon}m_pct"] = (
                round(statistics.mean(value[0] for value in observations), 3)
                if observations
                else None
            )
            output[f"avg_mae_{horizon}m_pct"] = (
                round(statistics.mean(value[1] for value in observations), 3)
                if observations
                else None
            )
            output[f"avg_net_{horizon}m_pct"] = (
                round(statistics.mean(net_values), 3) if net_values else None
            )
            output[f"positive_net_{horizon}m_rate_pct"] = (
                round(
                    sum(1 for value in net_values if value >= 0)
                    / len(net_values)
                    * 100,
                    2,
                )
                if net_values
                else 0
            )
        valid_30 = [
            item
            for item in items
            if all(
                finite_outcome(item.get("outcome") or {}, key) is not None
                for key in ("ret_30m", "mfe_30m", "mae_30m")
            )
        ]
        good = sum(1 for item in valid_30 if item.get("outcome", {}).get("good_30"))
        output["good_30_rate_pct"] = (
            round(good / len(valid_30) * 100, 2) if valid_30 else 0
        )
        return output

    def _independent_stats(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Show clustered-event results separately from the raw event count."""
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in candidates:
            grouped[(str(item.get("trade_date")), str(item.get("code")))].append(item)
        for values in grouped.values():
            values.sort(key=lambda item: int(item.get("index", 0)))

        first_any = [values[0] for values in grouped.values() if values]
        first_strict = [
            next((item for item in values if item.get("strict_four_factor")), None)
            for values in grouped.values()
            if values
        ]
        first_strict = [item for item in first_strict if item is not None]

        non_overlapping: list[dict[str, Any]] = []
        for values in grouped.values():
            last_index = -999
            for item in values:
                if not item.get("strict_four_factor"):
                    continue
                current = int(item.get("index", 0))
                if current - last_index >= 30:
                    non_overlapping.append(item)
                    last_index = current

        return [
            {"group": "每只股票每天首个候选", **self._stats(first_any)},
            {"group": "每只股票每天首个四因子候选", **self._stats(first_strict)},
            {"group": "四因子间隔至少30分钟", **self._stats(non_overlapping)},
        ]

    def _sensitivity(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for volume_threshold in (1.15, 1.25, 1.40, 1.60):
            for flow_threshold in (10, 18, 25, 35):
                selected = [
                    item
                    for item in candidates
                    if item.get("metrics", {}).get("amount_ratio", 0) >= volume_threshold
                    and item.get("metrics", {}).get("flow_score", -999) >= flow_threshold
                    and "market" in item.get("factors", [])
                    and "sector" in item.get("factors", [])
                ]
                output.append(
                    {
                        "volume_ratio_min": volume_threshold,
                        "flow_score_min": flow_threshold,
                        **self._stats(selected),
                    }
                )
        return output

    def _scene_threshold_sensitivity(
        self,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Audit broad veto plateaus; this does not optimize a winning threshold."""
        formal = [
            item
            for item in candidates
            if item.get("strategy_v2", {}).get("eligible")
        ]
        output: list[dict[str, Any]] = []
        for breadth, strong_breadth in zip(
            (0.55, 0.60, 0.65),
            (0.40, 0.45, 0.50),
        ):
            selected = [
                item
                for item in formal
                if _finite(item.get("metrics", {}).get("context_breadth")) >= breadth
                and _finite(item.get("metrics", {}).get("context_strong_breadth"))
                >= strong_breadth
            ]
            output.append(
                {
                    "kind": "market_saturation_veto",
                    "breadth_min": breadth,
                    "strong_breadth_min": strong_breadth,
                    **self._stats(selected),
                }
            )
        for rebound in (4.5, 5.0, 5.5):
            selected = [
                item
                for item in formal
                if _finite(item.get("metrics", {}).get("rebound")) >= rebound
            ]
            output.append(
                {
                    "kind": "late_extension_veto",
                    "rebound_min_pct": rebound,
                    **self._stats(selected),
                }
            )
        return output

    @staticmethod
    def _stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        def values(key: str) -> list[float]:
            output: list[float] = []
            for item in items:
                value = item.get("outcome", {}).get(key)
                if isinstance(value, (int, float)) and math.isfinite(float(value)):
                    output.append(float(value))
            return output

        ret15 = values("ret_15m")
        ret30 = values("ret_30m")
        mfe30 = values("mfe_30m")
        mae15 = values("mae_15m")
        net30 = values("net_30m")
        good = sum(1 for item in items if item.get("outcome", {}).get("good_30"))
        return {
            "count": len(items),
            "outcome_count": len(ret30),
            "good_30_count": good,
            "good_30_rate_pct": round(good / len(items) * 100, 2) if items else 0,
            "positive_net_30_rate_pct": round(sum(1 for value in net30 if value >= 0) / len(net30) * 100, 2) if net30 else 0,
            "avg_ret_15_pct": round(statistics.mean(ret15), 3) if ret15 else None,
            "avg_ret_30_pct": round(statistics.mean(ret30), 3) if ret30 else None,
            "median_ret_30_pct": round(statistics.median(ret30), 3) if ret30 else None,
            "avg_mfe_30_pct": round(statistics.mean(mfe30), 3) if mfe30 else None,
            "avg_mae_15_pct": round(statistics.mean(mae15), 3) if mae15 else None,
            "avg_net_30_pct": round(statistics.mean(net30), 3) if net30 else None,
        }

    def _write_report(self, report: dict[str, Any]) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        mode_suffix = "transactions" if self.flow_enabled else "proxy"
        json_path = self.runtime_dir / f"t_strategy_study_{stamp}_{mode_suffix}.json"
        latest_path = self.runtime_dir / "latest.json"
        mode_latest_path = self.runtime_dir / f"latest_{mode_suffix}.json"
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        json_path.write_text(serialized, encoding="utf-8")
        latest_path.write_text(serialized, encoding="utf-8")
        mode_latest_path.write_text(serialized, encoding="utf-8")
        # Keep the names consumed by the dashboard/API in sync with the most
        # recent run.  ``latest.json`` remains the canonical artifact; these
        # aliases make it impossible for the UI to fall back to an older L1 or
        # transaction report merely because the mode-specific name is stale.
        compatibility_latest = (
            self.runtime_dir / "latest_transactions.json"
            if self.flow_enabled
            else self.runtime_dir / "latest_proxy.json"
        )
        compatibility_latest.write_text(serialized, encoding="utf-8")
        compact_serialized = json.dumps(
            build_compact_research_report(report),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        summary_paths = {
            self.runtime_dir / f"t_strategy_study_{stamp}_{mode_suffix}_summary.json",
            self.runtime_dir / "latest_summary.json",
            self.runtime_dir / f"latest_{mode_suffix}_summary.json",
        }
        for summary_path in summary_paths:
            summary_path.write_text(compact_serialized, encoding="utf-8")
        candidates = report.get("all_candidates", [])
        self._write_csv(self.runtime_dir / f"t_strategy_candidates_{stamp}_{mode_suffix}.csv", candidates)
        self._write_csv(self.runtime_dir / f"t_strategy_trades_{stamp}_{mode_suffix}.csv", report.get("trades", []))
        self._write_csv(self.runtime_dir / f"t_strategy_v3_trades_{stamp}_{mode_suffix}.csv", report.get("v3_trades", []))
        self._write_csv(self.runtime_dir / f"t_strategy_v3_rejections_{stamp}_{mode_suffix}.csv", report.get("v3_rejections", []))
        self._write_csv(self.runtime_dir / f"opening_window_{stamp}_{mode_suffix}.csv", report.get("opening_events", []))
        self._write_markdown(self.runtime_dir / f"t_strategy_study_{stamp}_{mode_suffix}.md", report)
        self._write_markdown(self.runtime_dir / "latest.md", report)
        self._write_markdown(self.runtime_dir / f"latest_{mode_suffix}.md", report)

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        flattened: list[dict[str, Any]] = []
        keys: set[str] = set()
        for row in rows:
            item: dict[str, Any] = {}
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    item[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                else:
                    item[key] = value
                keys.add(key)
            flattened.append(item)
        ordered = sorted(keys)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ordered, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flattened)

    @staticmethod
    def _write_markdown(path: Path, report: dict[str, Any]) -> None:
        protocol = report.get("research_protocol") or {}
        validation = protocol.get("validation") or {}
        protocol_sample = protocol.get("sample") or {}
        selection = protocol_sample.get("selection") or {}
        quality = protocol.get("data_quality") or {}
        daily_history = quality.get("daily_history") or {}
        walk_forward = protocol.get("walk_forward") or {}
        execution = protocol.get("execution_model") or {}
        parameter_discovery = protocol.get("parameter_discovery") or {}
        feature_performance = parameter_discovery.get("feature_performance") or {}
        formula_grid = report.get("formula_grid") or {}
        formula_sample = formula_grid.get("sample_sufficiency") or {}
        formula_selection = formula_grid.get("selection") or {}

        def metric(value: Any, suffix: str = "") -> str:
            if value is None:
                return "--"
            if isinstance(value, float):
                return f"{value:.3f}{suffix}"
            return f"{value}{suffix}"

        reasons = [str(item) for item in validation.get("reasons", []) if str(item)]
        status = str(validation.get("status") or "research_only")
        lines = [
            "# 日内正T/反T研究协议报告",
            "",
            f"生成时间：{protocol.get('generated_at') or report.get('generated_at', '')}",
            "",
            "## 结论边界",
            "",
            f"**研究状态：{status}；不可自动执行。** " + ("；".join(reasons) or "尚未形成可部署结论。"),
            "",
            "本报告用于提出和证伪假设，不是收益承诺。所有候选只使用当时及之前的数据；easy_tdx 数据按 TDX L1 成交明细处理，不称为队列数据。",
            "",
            "## 数据与样本",
            "",
            f"- 交易日：{', '.join(protocol_sample.get('dates', [])) or '无'}；共 {protocol_sample.get('date_count', 0)} 日",
            f"- 样本：{protocol_sample.get('stock_day_count', 0)} 个股票-交易日；逐日事前选样：{selection.get('stock_day_count', 0)} 个",
            f"- 选样规则：{selection.get('method') or '每个目标日前一交易日按流动性和行业分层'}",
            f"- L1逐笔覆盖样本：{protocol_sample.get('transaction_sample_count', 0)}；分钟覆盖均值：{metric(quality.get('minute_coverage_mean'))}",
            f"- 逐笔分钟覆盖均值：{metric(quality.get('transaction_minute_coverage_mean'))}；元数据覆盖均值：{metric(quality.get('transaction_metadata_coverage_mean'))}",
            f"- 日线历史：{daily_history.get('loaded_sessions', 0)}/{daily_history.get('required_sessions', 60)} 个目标日前交易日；状态：{daily_history.get('status', 'unavailable')}；价格口径：{daily_history.get('price_basis', 'unavailable')}",
            f"- 竞价真实数据样本：{protocol_sample.get('auction_available_count', 0)}；缺失时不补造代理",
            f"- 一字板样本：{protocol_sample.get('one_word_count', 0)}；保留为 no_fill：{protocol_sample.get('no_fill_retained', True)}",
            f"- 基础往返成本：{metric(execution.get('base_round_trip_cost_pct'), '%')}；额外悲观成本：{metric(execution.get('extra_pessimistic_round_trip_cost_pct'), '%')}",
            "",
            "## 验证状态",
            "",
            f"- 原始标签：{validation.get('raw_label_count', 0)}；独立候选事件：{validation.get('independent_event_count', 0)}；可成交独立事件：{validation.get('filled_event_count', 0)}",
            f"- Walk-forward：{walk_forward.get('method', 'expanding_window_one_day_ahead')}；折数 {walk_forward.get('fold_count', 0)}；完整：{walk_forward.get('complete', False)}",
            f"- 初始训练：{walk_forward.get('minimum_training_days', 20)} 日；完整研究范围要求：{walk_forward.get('required_total_days', 60)} 日",
            f"- OOS 日期：{', '.join(walk_forward.get('oos_dates', [])) or '尚无合格滚动样本外日期'}",
            "",
            "| 方向 | 状态 | 独立事件 | OOS事件 | 基础OOS平均R | 悲观OOS平均R | 目标先触达 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for direction, label in (("positive_t", "正T"), ("reverse_t", "反T")):
            item = (validation.get("direction") or {}).get(direction, {})
            base = item.get("base_oos_metrics") or {}
            pessimistic = item.get("pessimistic_oos_metrics") or {}
            lines.append(
                f"| {label} | {item.get('status', 'research_only')} | {item.get('independent_event_count', 0)} | "
                f"{item.get('oos_filled_event_count', 0)} | {metric(base.get('mean_net_r'))} | "
                f"{metric(pessimistic.get('mean_net_r'))} | {metric(base.get('target_first_probability_pct'), '%')} |"
            )
        lines.extend(
            [
                "",
                "## 研究假设",
                "",
                "| 假设 | 机制 | 主要对照 |",
                "|---|---|---|",
            ]
        )
        for item in protocol.get("hypotheses", []):
            lines.append(
                f"| {item.get('hypothesis_id', '')} {item.get('title', '')} | "
                f"{item.get('mechanism', '')} | {', '.join(item.get('counterfactual', []))} |"
            )
        lines.extend(
            [
                "",
                "## 反事实对照",
                "",
                "固定事件时点估计量回答同一位置的标签变化；重建候选集估计量回答删除因子后会选到哪些位置。两者不能混为一个结论。",
                "",
                "| 对照 | 固定事件数 | 固定事件平均R | 重建候选数 | 重建候选平均R |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for name, item in (protocol.get("counterfactuals") or {}).items():
            same = item.get("same_event") or {}
            regenerated = item.get("regenerated") or {}
            lines.append(
                f"| {name} | {same.get('independent_outcomes', 0)} | "
                f"{metric((same.get('metrics') or {}).get('mean_net_r'))} | "
                f"{regenerated.get('independent_outcomes', 0)} | "
                f"{metric((regenerated.get('metrics') or {}).get('mean_net_r'))} |"
            )
        stable_platforms = feature_performance.get("stable_positive_platforms") or []
        lines.extend(
            [
                "",
                "## 参数发现",
                "",
                f"- 状态：{parameter_discovery.get('status', 'exploratory_only')}；只用训练日期：{', '.join(parameter_discovery.get('training_dates', [])) or '无'}",
                f"- 已分析连续特征：{feature_performance.get('feature_count', 0)}；训练集相邻正平台：{len(stable_platforms)}",
                f"- OOS平台评估：{len(parameter_discovery.get('oos_platform_evaluation', []))}；所有平台 selected=false，不自动固化阈值",
                f"- 规则：{parameter_discovery.get('plateau_rule', '')}",
            ]
        )
        if stable_platforms:
            lines.extend(["", "| 特征 | 相邻分箱 | 范围 | 状态 |", "|---|---|---|---|"])
        for item in stable_platforms[:30]:
            lines.append(
                f"| {item.get('feature', '')} | {item.get('bins', [])} | "
                f"{metric(item.get('lower'))} ~ {metric(item.get('upper'))} | 仅训练集探索 |"
            )
        grid_rows = formula_grid.get("rows") or []
        lines.extend(
            [
                "",
                "## 做T公式阈值网格",
                "",
                f"- 状态：{formula_grid.get('status', 'research_only')} / {formula_grid.get('validation_status', 'sample_insufficient')}；公式字段可用观测 {formula_grid.get('formula_ready_count', 0)} 个；缺字段 {formula_grid.get('missing_formula_fields', 0)} 个",
                "- 唯一候选口径：赶快出手+主力吸筹，或接近白/黄趋势线；不读取旧V2/V3状态。",
                "- L1组合：忽略L1、明显抛压否决、买盘支持增强；研究层只消费时点布尔指标。",
                f"- 样本：{formula_sample.get('observed_trading_days', 0)} 个交易日、最大独立有效事件 {formula_sample.get('max_independent_outcome_count', 0)}；最低要求 {formula_sample.get('minimum_trading_days', 20)} 日且 {formula_sample.get('minimum_independent_events', 30)} 个事件；充分行 {formula_sample.get('sufficient_row_count', 0)}。",
                f"- 阈值选择：selected=false；{formula_selection.get('reason', 'research_only网格不自动选优。')}",
                f"- 事件集合平台：{len(formula_grid.get('plateaus') or [])} 个；只表示相邻阈值命中相同事件，不按收益选优。",
                "",
                "| 白/黄线接近阈值 | L1规则 | 事件 | 去重事件 | 5m净均值 | 15m净均值 | 30m净均值 | 5/15/30 MFE均值 | 5/15/30 MAE均值 | 30m正净率 |",
                "|---:|---|---:|---:|---:|---:|---:|---|---|---:|",
            ]
        )
        for item in grid_rows:
            clustered = item.get("clustered") or {}
            lines.append(
                f"| {metric(item.get('trend_near_threshold_pct'), '%')} | {item.get('l1_rule_label', item.get('l1_rule', ''))} | "
                f"{item.get('count', 0)} | {item.get('clustered_count', 0)} | "
                f"{metric(clustered.get('avg_net_5m_pct'), '%')} | {metric(clustered.get('avg_net_15m_pct'), '%')} | {metric(clustered.get('avg_net_30m_pct'), '%')} | "
                f"{metric(clustered.get('avg_mfe_5m_pct'), '%')}/{metric(clustered.get('avg_mfe_15m_pct'), '%')}/{metric(clustered.get('avg_mfe_30m_pct'), '%')} | "
                f"{metric(clustered.get('avg_mae_5m_pct'), '%')}/{metric(clustered.get('avg_mae_15m_pct'), '%')}/{metric(clustered.get('avg_mae_30m_pct'), '%')} | "
                f"{metric(clustered.get('positive_net_30m_rate_pct'), '%')} |"
            )
        legacy = report.get("strategy_v3") or {}
        legacy_stats = legacy.get("stats") or {}
        lines.extend(
            [
                "",
                "## 旧规则基线",
                "",
                "旧 V2/V3 结果只用于对照新研究协议，不是当前买卖条件，也不参与可执行状态。",
                f"- V2候选：{(report.get('summary') or {}).get('strategy_v2_formal_count', 0)}",
                f"- V3样本：{legacy_stats.get('trade_count', 0)}；平均R：{metric(legacy_stats.get('average_r'))}；旧状态：{legacy.get('validation_status', 'sample_insufficient')}",
                f"- 旧版验证原因：{'；'.join(legacy.get('validation_reasons', [])) or '无'}",
            ]
        )
        lines.extend(
            [
                "",
                "## 偏差登记",
                "",
                "| 偏差 | 控制 | 状态 |",
                "|---|---|---|",
            ]
        )
        for item in protocol.get("bias_register", []):
            lines.append(
                f"| {item.get('bias', '')} | {item.get('control', '')} | {item.get('status', '')} |"
            )
        lines.extend(["", "## 已知限制", ""])
        for limitation in protocol.get("limitations", []):
            lines.append(f"- {limitation}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_study(
    *,
    dates: list[str] | None = None,
    selection_date: str = "20260805",
    sample_size: int = 100,
    include_transactions: bool = True,
) -> dict[str, Any]:
    """Convenience entry point for tests and the CLI."""
    config = ResearchConfig(
        dates=tuple(dates or ("20260806", "20260807")),
        selection_date=selection_date,
        sample_size=max(20, min(int(sample_size), 100)),
        include_transactions=include_transactions,
    )
    return StrategyResearcher(config=config).run()
