from __future__ import annotations

import itertools
import json
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config import AppSettings
from app.models import (
    AuctionSnapshot,
    DetailDataPayload,
    DetailDataTable,
    FundamentalField,
    FundamentalPayload,
    FundamentalSection,
    FundamentalTable,
    IndexSnapshot,
    OrderBookLevel,
    OrderFlowObservation,
    Quote,
    SectorSnapshot,
    TransactionFlowPoint,
    TransactionFlowObservation,
    TransactionTapePrint,
    WatchlistItem,
)


SECURITY_NAMES = {
    "300308": "中际旭创",
    "300476": "胜宏科技",
    "002428": "云南锗业",
    "002463": "沪电股份",
    "600183": "生益科技",
    "300502": "新易盛",
    "300394": "天孚通信",
    "300620": "光库科技",
    "002916": "深南电路",
    "603228": "景旺电子",
    "688981": "中芯国际",
    "603986": "兆易创新",
    "688041": "海光信息",
}

CHINA_TZ = timezone(timedelta(hours=8))


def _as_china_time(value: datetime) -> datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(CHINA_TZ).replace(tzinfo=None)
    return value


def china_now() -> datetime:
    return _as_china_time(datetime.now(CHINA_TZ))


@dataclass
class MarketSnapshot:
    quotes: list[Quote]
    indices: list[IndexSnapshot]
    data_mode: str
    source_status: dict[str, Any]


@dataclass
class BoardContext:
    board_level: int
    source: str
    available: bool
    fetched_at: str
    sectors: list[SectorSnapshot]
    name_to_code: dict[str, str]
    code_to_name: dict[str, str]
    error: str = ""
    members_by_code: dict[str, list[str]] = field(default_factory=dict)


def normalize_board_level(value: Any) -> int:
    try:
        level = int(str(value or "").strip())
    except Exception:
        level = 3
    return level if level in {1, 2, 3} else 3


class AuctionSnapshotTracker:
    """Keep a bounded intraday trail of pre-open snapshots.

    easy_tdx can expose current-day call-auction points for a single security.
    The tracker stores only the bounded trail needed by opening decisions and
    never promotes five-level quote proxies into hidden-order truth.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.max_points = max(8, int(settings.auction_history_max_points))
        self.path = settings.auction_history_file
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._latest: dict[str, AuctionSnapshot] = {}
        self._session_date = ""
        self._last_persist_at: dict[str, float] = {}

    def observe(self, code: str, snapshot: AuctionSnapshot) -> AuctionSnapshot:
        if not snapshot.available or not snapshot.trade_date:
            return snapshot
        self._start_session(snapshot.trade_date)
        key = f"{snapshot.trade_date}:{code}"
        point = {
            "as_of": snapshot.as_of,
            "price": float(snapshot.price or 0),
            "volume": float(snapshot.volume or 0),
            "amount": float(snapshot.amount or 0),
            "imbalance": float(snapshot.order_imbalance_pct or 0),
        }
        history = self._history.setdefault(key, deque(maxlen=self.max_points))
        if history and str(history[-1].get("as_of")) == str(point["as_of"]):
            history[-1] = point
        else:
            history.append(point)

        first = history[0]
        previous = history[-2] if len(history) > 1 else first
        first_price = float(first.get("price") or snapshot.price or 0)
        current_price = float(point.get("price") or 0)
        price_slope = ((current_price - first_price) / first_price * 100) if first_price else 0
        previous_volume = float(previous.get("volume") or 0)
        current_volume = float(point.get("volume") or 0)
        volume_delta = current_volume - previous_volume if current_volume >= previous_volume else 0
        previous_imbalance = float(previous.get("imbalance") or 0)
        current_imbalance = float(point.get("imbalance") or 0)
        imbalance_delta = current_imbalance - previous_imbalance
        if len(history) < 2:
            trajectory = "首个竞价快照"
        elif price_slope >= 0.05 or (price_slope >= 0 and current_imbalance >= 8):
            trajectory = "竞价上修"
        elif price_slope <= -0.05 or (price_slope <= 0 and current_imbalance <= -8):
            trajectory = "竞价下修"
        else:
            trajectory = "竞价分歧"

        enriched = snapshot.model_copy(
            update={
                "snapshot_count": len(history),
                "price_slope_pct": round(price_slope, 3),
                "price_change_from_first_pct": round(price_slope, 3),
                "volume_delta": round(volume_delta, 2),
                "imbalance_delta_pct": round(imbalance_delta, 2),
                "trajectory": trajectory,
            }
        )
        self._latest[key] = enriched
        self._persist_candidate(code, enriched)
        return enriched

    def latest(self, code: str, trade_date: str) -> AuctionSnapshot | None:
        key = f"{trade_date}:{code}"
        return self._latest.get(key)

    def history(self, code: str, trade_date: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history.get(f"{trade_date}:{code}", ())]

    def _start_session(self, trade_date: str) -> None:
        if self._session_date == trade_date:
            return
        self._session_date = trade_date
        self._history.clear()
        self._latest.clear()
        self._last_persist_at.clear()

    def _persist_candidate(self, code: str, snapshot: AuctionSnapshot) -> None:
        # Persist only actionable candidates or actual rows.  Persisting all
        # 5,000+ symbols every few seconds would create a large log without
        # improving the decision score.
        candidate = (
            snapshot.data_quality == "actual"
            or snapshot.change_pct >= 0.5
            or abs(snapshot.order_imbalance_pct) >= 8
        )
        if not candidate:
            return
        now = time.time()
        key = f"{snapshot.trade_date}:{code}"
        if now - self._last_persist_at.get(key, 0) < 15:
            return
        self._last_persist_at[key] = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "code": code,
                            **snapshot.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            # A persistence problem must not stop real-time scanning.
            return


class DataSourceError(RuntimeError):
    pass


@dataclass
class StockMeta:
    code: str
    ts_code: str
    name: str
    industry: str = ""
    market: str = ""
    exchange: str = ""
    themes: list[str] | None = None
    core: bool = False

    def normalized_themes(self) -> list[str]:
        values = [self.industry, *(self.themes or [])]
        return list(dict.fromkeys([value for value in values if value]))


def market_id_for_code(code: str) -> int:
    if code.startswith("92"):
        return 2
    if code.startswith(("6", "5", "9")):
        return 1
    if code.startswith(("4", "8")):
        return 2
    return 0


INDEX_MARKET_IDS = {
    "000001": 1,
    "399001": 0,
    "399006": 0,
}

INDEX_NAMES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
}


def market_id_for_index_code(code: str) -> int:
    if code not in INDEX_MARKET_IDS:
        raise DataSourceError(f"不支持的大盘指数代码：{code}")
    return INDEX_MARKET_IDS[code]


def full_tdx_code(code: str, *, index: bool = False) -> str:
    normalized = str(code or "").strip().zfill(6)
    if index:
        market = market_id_for_index_code(normalized)
        exchange = "sh" if market == 1 else "bj" if market == 2 else "sz"
        return f"{exchange}{normalized}"
    if normalized.startswith("92"):
        return f"bj{normalized}"
    if normalized.startswith(("6", "5", "9")):
        return f"sh{normalized}"
    if normalized.startswith(("4", "8")):
        return f"bj{normalized}"
    return f"sz{normalized}"


def easy_tdx_market_for_code(code: str, *, index: bool = False) -> Any:
    """Return easy_tdx Market enum without importing easy_tdx at module import."""
    from easy_tdx import Market

    market_id = market_id_for_index_code(code) if index else market_id_for_code(code)
    return Market(market_id)


def dataframe_records(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if hasattr(raw, "empty") and bool(getattr(raw, "empty")):
        return []
    if hasattr(raw, "to_dict"):
        try:
            return [dict(row) for row in raw.to_dict("records")]
        except TypeError:
            pass
    if isinstance(raw, dict):
        return [dict(raw)]
    if isinstance(raw, list):
        return [dict(item) if isinstance(item, dict) else {"value": item} for item in raw]
    try:
        return [dict(item) if isinstance(item, dict) else item.__dict__ for item in raw]
    except Exception:
        return []


def jsonable_market_value(value: Any, max_text: int = 180) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:
            return None
        return round(value, 4)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_text:
        return f"{text[:max_text - 3]}..."
    return text


def market_time_label(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%H:%M")
        except Exception:
            pass
    text = str(value).strip()
    if not text:
        return ""
    if " " in text:
        text = text.rsplit(" ", 1)[-1]
    if "T" in text:
        text = text.rsplit("T", 1)[-1]
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    if len(text) >= 4 and text[:4].isdigit():
        return f"{text[:2]}:{text[2:4]}"
    return text[:5]


def is_regular_transaction_time(value: Any) -> bool:
    """Return whether a transaction print belongs to the continuous session."""
    label = str(value or "")[:5]
    try:
        hour, minute = (int(part) for part in label.split(":", 1))
    except (TypeError, ValueError):
        return False
    total = hour * 60 + minute
    return (
        9 * 60 + 30 <= total <= 11 * 60 + 30
        or 13 * 60 <= total <= 15 * 60
    )


def calc_change_pct(price: float, prev_close: float) -> float:
    if prev_close <= 0:
        return 0
    return round((price - prev_close) / prev_close * 100, 2)


def guess_ts_code(code: str) -> str:
    suffix = "BJ" if code.startswith(("4", "8")) else ("SH" if code.startswith(("6", "9")) else "SZ")
    return f"{code}.{suffix}"


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if is_dataclass(row):
        return {field.name: getattr(row, field.name) for field in fields(row)}
    if hasattr(row, "_asdict"):
        try:
            return dict(row._asdict())
        except Exception:
            pass
    if hasattr(row, "to_dict") and hasattr(row, "columns"):
        try:
            payload = row.to_dict(orient="records")
        except Exception:
            payload = None
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                return dict(first)
    if hasattr(row, "to_dict"):
        try:
            payload = row.to_dict()
            if isinstance(payload, dict):
                return dict(payload)
        except Exception:
            pass
    if hasattr(row, "__dict__"):
        return {key: value for key, value in vars(row).items() if not key.startswith("_")}
    return {}


def _records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if hasattr(payload, "to_dict") and hasattr(payload, "columns"):
        try:
            records = payload.to_dict(orient="records")
        except Exception:
            records = None
        if isinstance(records, list):
            return [dict(row) for row in records if isinstance(row, dict)]
    if isinstance(payload, (list, tuple)):
        return [_row_dict(row) for row in payload if row is not None]
    for key in ("rows", "records", "points", "ticks"):
        value = getattr(payload, key, None)
        if value is not None:
            if isinstance(value, (list, tuple)):
                return [_row_dict(row) for row in value if row is not None]
            try:
                return [_row_dict(row) for row in list(value) if row is not None]
            except Exception:
                pass
    row = _row_dict(payload)
    return [row] if row else []


def _market_id_for_tdx_code(code: str, *, index: bool = False) -> int:
    if index:
        return market_id_for_index_code(code)
    return market_id_for_code(code)


class UniverseProvider:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.cache_path = settings.data_dir / "runtime" / "easy_tdx_stock_basic_cache.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, watchlist: list[WatchlistItem], themes: list[dict]) -> tuple[dict[str, StockMeta], dict[str, Any]]:
        status: dict[str, Any] = {"signal_scope": self.settings.scan_scope}
        if self.settings.scan_scope == "theme_pool":
            universe = self._theme_pool_universe(watchlist, themes)
            status["universe_source"] = "theme_pool"
            status["universe_size"] = len(universe)
            return universe, status

        rows = self._load_cached_rows()
        cache_used = bool(rows)
        if not rows:
            rows = self._fetch_stock_basic_rows()
        if not rows:
            universe = self._theme_pool_universe(watchlist, themes)
            status.update({
                "universe_source": "theme_pool_fallback",
                "universe_size": len(universe),
                "universe_cache_used": cache_used,
            })
            return universe, status

        universe = self._rows_to_universe(rows, themes, watchlist)
        status.update({
            "universe_source": "easy_tdx_security_list",
            "universe_size": len(universe),
            "universe_cache_used": cache_used,
        })
        return universe, status

    def _load_cached_rows(self) -> list[dict[str, Any]]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        fetched_at = str(payload.get("fetched_at", ""))
        try:
            fetched_dt = datetime.fromisoformat(fetched_at)
        except Exception:
            return []
        if china_now() - fetched_dt > timedelta(hours=24):
            return []
        rows = payload.get("rows", [])
        return rows if isinstance(rows, list) else []

    def _fetch_stock_basic_rows(self) -> list[dict[str, Any]]:
        try:
            from easy_tdx import TdxClient
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise DataSourceError("easy_tdx未安装，无法加载全市场股票池。") from exc

        try:
            with TdxClient(timeout=float(self.settings.easy_tdx_timeout_seconds)) as client:
                raw_rows = _records_from_payload(client.get_security_list_all(pages="all"))
        except Exception as exc:  # pragma: no cover - network dependent
            raise DataSourceError(f"拉取 easy_tdx 全市场证券列表失败：{exc}") from exc

        rows = [
            self._normalize_stock_basic_row(row)
            for row in raw_rows
            if self._is_a_share_security(row)
        ]
        if not rows:
            raise DataSourceError("easy_tdx证券列表未返回A股股票池。")
        self.cache_path.write_text(
            json.dumps({"fetched_at": china_now().isoformat(), "rows": rows}, ensure_ascii=False),
            encoding="utf-8",
        )
        return rows

    def _normalize_stock_basic_row(self, row: dict[str, Any]) -> dict[str, Any]:
        symbol = str(row.get("symbol") or row.get("code") or row.get("ts_code", "")[:6]).zfill(6)
        ts_code = str(row.get("ts_code") or guess_ts_code(symbol))
        market_id = int(float(row.get("market") or market_id_for_code(symbol)))
        return {
            "ts_code": ts_code,
            "symbol": symbol,
            "name": str(row.get("name") or SECURITY_NAMES.get(symbol, symbol)),
            "industry": str(row.get("industry_sw") or row.get("industry_tdx") or ""),
            "market": self._market_label(symbol, market_id),
            "exchange": "SSE" if market_id == 1 else "BSE" if market_id == 2 else "SZSE",
            "industry_tdx": str(row.get("industry_tdx") or ""),
            "industry_sw": str(row.get("industry_sw") or ""),
        }

    @staticmethod
    def _is_a_share_security(row: dict[str, Any]) -> bool:
        code = str(row.get("code") or row.get("symbol") or "").strip().zfill(6)
        name = str(row.get("name") or "").strip()
        if not code.isdigit() or not name:
            return False
        if name.endswith(("指数", "债", "债券")) or any(key in name for key in ("ETF", "LOF", "基金", "转债")):
            return False
        return code.startswith(
            (
                "000",
                "001",
                "002",
                "003",
                "300",
                "301",
                "600",
                "601",
                "603",
                "605",
                "688",
                "689",
                "430",
                "830",
                "831",
                "832",
                "833",
                "834",
                "835",
                "836",
                "837",
                "838",
                "839",
                "870",
                "871",
                "872",
                "873",
                "920",
            )
        )

    @staticmethod
    def _market_label(code: str, market_id: int) -> str:
        if market_id == 2 or code.startswith(("4", "8", "92")):
            return "北交所"
        if code.startswith(("300", "301")):
            return "创业板"
        if code.startswith(("688", "689")):
            return "科创板"
        if market_id == 1:
            return "沪主板"
        return "深主板"

    def _rows_to_universe(
        self,
        rows: list[dict[str, Any]],
        themes: list[dict],
        watchlist: list[WatchlistItem],
    ) -> dict[str, StockMeta]:
        universe: dict[str, StockMeta] = {}
        for row in rows:
            code = str(row.get("symbol") or "").zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            universe[code] = StockMeta(
                code=code,
                ts_code=str(row.get("ts_code") or guess_ts_code(code)),
                name=str(row.get("name") or code),
                industry=str(row.get("industry") or ""),
                market=str(row.get("market") or ""),
                exchange=str(row.get("exchange") or ""),
                themes=[],
                core=False,
            )

        self._overlay_themes(universe, themes)
        self._overlay_watchlist(universe, watchlist)
        return universe

    def _theme_pool_universe(self, watchlist: list[WatchlistItem], themes: list[dict]) -> dict[str, StockMeta]:
        universe: dict[str, StockMeta] = {}
        for theme in themes:
            name = str(theme.get("name") or "").strip()
            if not name:
                continue
            for code in itertools.chain(theme.get("members", []), theme.get("core_codes", [])):
                code = str(code).zfill(6)
                meta = universe.setdefault(
                    code,
                    StockMeta(
                        code=code,
                        ts_code=guess_ts_code(code),
                        name=SECURITY_NAMES.get(code, code),
                        industry="",
                        market="",
                        exchange="",
                        themes=[],
                        core=False,
                    ),
                )
                if name not in meta.themes:
                    meta.themes.append(name)
                if code in set(theme.get("core_codes", [])):
                    meta.core = True
        self._overlay_watchlist(universe, watchlist)
        return universe

    def _overlay_themes(self, universe: dict[str, StockMeta], themes: list[dict]) -> None:
        for theme in themes:
            name = str(theme.get("name") or "").strip()
            if not name:
                continue
            core_codes = {str(code).zfill(6) for code in theme.get("core_codes", [])}
            members = {str(code).zfill(6) for code in itertools.chain(theme.get("members", []), theme.get("core_codes", []))}
            for code in members:
                meta = universe.get(code)
                if meta is None:
                    meta = StockMeta(
                        code=code,
                        ts_code=guess_ts_code(code),
                        name=SECURITY_NAMES.get(code, code),
                        industry="",
                        market="",
                        exchange="",
                        themes=[],
                        core=False,
                    )
                    universe[code] = meta
                if name not in meta.themes:
                    meta.themes.append(name)
                if code in core_codes:
                    meta.core = True

    def _overlay_watchlist(self, universe: dict[str, StockMeta], watchlist: list[WatchlistItem]) -> None:
        for item in watchlist:
            meta = universe.get(item.code)
            if meta is None:
                if not self.settings.include_watchlist_in_scan:
                    continue
                meta = StockMeta(
                    code=item.code,
                    ts_code=guess_ts_code(item.code),
                    name=item.name,
                    industry="",
                    market="",
                    exchange="",
                    themes=[],
                    core=False,
                )
                universe[item.code] = meta
            if item.name:
                meta.name = item.name
            for theme in item.themes:
                if theme and theme not in meta.themes:
                    meta.themes.append(theme)
            if item.core and self.settings.include_watchlist_in_scan:
                meta.core = True

class EasyTdxDailyDataSource:
    """Daily and frozen snapshots sourced only from easy_tdx.

    The live dashboard should prefer the L1 quote snapshot even after close.
    This source exists for index seeds, explicit historical replay and offline
    research caches, so it intentionally stays outside the fast refresh path.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.cache_dir = settings.data_dir / "runtime"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._recent_trade_dates_cache: tuple[float, str, list[str]] | None = None
        self._index_seed_cache: tuple[float, str, MarketSnapshot] | None = None

    def fetch_seed(self, universe: dict[str, StockMeta] | None = None) -> MarketSnapshot:  # noqa: ARG002
        today = china_now().strftime("%Y%m%d")
        now_ts = time.time()
        if self._index_seed_cache is not None:
            cached_at, cached_today, cached = self._index_seed_cache
            if cached_today == today and now_ts - cached_at < 300:
                return cached
        trade_date = self._latest_trade_date()
        rows = self._fetch_index_rows(trade_date)
        snapshot = MarketSnapshot(
            quotes=[],
            indices=self._indices_from_rows(rows, trade_date),
            data_mode="closed_static",
            source_status={
                "active_source": "easy_tdx_daily_seed",
                "trade_date": trade_date,
                "clock_label": "15:00:00",
                "frozen": True,
                "note": "easy_tdx 日K指数 seed，仅用于指数前收和比例校准。",
            },
        )
        self._index_seed_cache = (time.time(), today, snapshot)
        return snapshot

    def fetch(self, universe: dict[str, StockMeta]) -> MarketSnapshot:
        empty_dates: list[str] = []
        for trade_date in self._recent_trade_dates():
            try:
                snapshot = self.fetch_for_date(universe, trade_date)
            except Exception:
                empty_dates.append(trade_date)
                continue
            snapshot.source_status["historical_request"] = False
            snapshot.source_status["empty_trade_dates_skipped"] = empty_dates
            snapshot.source_status["note"] = f"非盘中使用 easy_tdx {trade_date} 日K冻结快照。"
            return snapshot
        skipped = ", ".join(empty_dates) if empty_dates else "无"
        raise DataSourceError(f"easy_tdx最近交易日日K为空，无法生成冻结快照；已跳过：{skipped}。")

    def fetch_for_date(self, universe: dict[str, StockMeta], trade_date: str) -> MarketSnapshot:
        normalized_date = str(trade_date or "").strip()
        if len(normalized_date) != 8 or not normalized_date.isdigit():
            raise DataSourceError(f"无效交易日：{trade_date}")

        daily_rows = self._load_cached_daily(normalized_date)
        cache_used = bool(daily_rows)
        if not daily_rows:
            daily_rows = self._fetch_daily_rows(universe, normalized_date)
        if not daily_rows:
            raise DataSourceError(f"{normalized_date} easy_tdx日K为空，无法构建历史快照。")

        index_rows = self._load_cached_indices(normalized_date)
        if not index_rows:
            index_rows = self._fetch_index_rows(normalized_date)

        row_map = {
            str(row.get("symbol") or str(row.get("ts_code", ""))[:6]).zfill(6): row
            for row in daily_rows
        }
        quotes = [
            self._quote_from_daily(code, meta, row_map.get(code), normalized_date)
            for code, meta in universe.items()
            if row_map.get(code)
        ]
        return MarketSnapshot(
            quotes=quotes,
            indices=self._indices_from_rows(index_rows, normalized_date),
            data_mode="closed_static",
            source_status={
                "active_source": "easy_tdx_daily_close",
                "trade_date": normalized_date,
                "clock_label": "15:00:00",
                "frozen": True,
                "historical_request": True,
                "note": f"历史回放使用 easy_tdx {normalized_date} 日K快照。",
                "daily_cache_used": cache_used,
                "auction_source": "unavailable",
                "auction_available_count": 0,
                "auction_note": "日K快照不包含集合竞价；竞价复盘走 easy_tdx 逐笔/竞价按需接口。",
            },
        )

    def trade_dates_before(self, before_date: str, sessions: int = 60) -> list[str]:
        try:
            target = datetime.strptime(str(before_date), "%Y%m%d")
        except ValueError as exc:
            raise DataSourceError(f"交易日格式无效：{before_date}") from exc
        required = max(20, int(sessions))
        rows = self._index_history_rows("000001", count=min(800, max(160, required * 4)))
        dates = [
            self._date_from_bar(row)
            for row in rows
            if self._date_from_bar(row) and self._date_from_bar(row) < target.strftime("%Y%m%d")
        ]
        return dates[-required:]

    def _latest_trade_date(self) -> str:
        dates = self._recent_trade_dates()
        if not dates:
            raise DataSourceError("easy_tdx指数日K没有可用交易日。")
        return dates[0]

    def _recent_trade_dates(self) -> list[str]:
        today = china_now().strftime("%Y%m%d")
        now_ts = time.time()
        if self._recent_trade_dates_cache is not None:
            cached_at, cached_today, cached_dates = self._recent_trade_dates_cache
            if cached_today == today and now_ts - cached_at < 300:
                return list(cached_dates)
        rows = self._index_history_rows("000001", count=20)
        dates = [self._date_from_bar(row) for row in rows]
        result = list(reversed([date for date in dates if date][-10:]))
        self._recent_trade_dates_cache = (time.time(), today, result)
        return list(result)

    def _fetch_daily_rows(self, universe: dict[str, StockMeta], trade_date: str) -> list[dict[str, Any]]:
        cached_by_code: dict[str, dict[str, Any]] = {}
        codes = list(universe.keys())
        if not codes:
            return []
        workers = max(1, min(int(getattr(self.settings, "easy_tdx_quote_workers", 4) or 4), 8))
        chunks = [codes[start:start + 80] for start in range(0, len(codes), 80)]

        def fetch_chunk(chunk_codes: list[str]) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            try:
                from easy_tdx import TdxClient
            except Exception as exc:  # pragma: no cover
                raise DataSourceError("easy_tdx未安装，无法读取日K。") from exc
            with TdxClient(timeout=float(self.settings.easy_tdx_timeout_seconds)) as client:
                for code in chunk_codes:
                    meta = universe.get(code)
                    if meta is None:
                        continue
                    row = self._daily_row_for_code(client, code, meta, trade_date)
                    if row:
                        rows.append(row)
            return rows

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_chunk, chunk) for chunk in chunks]
            for future in as_completed(futures):
                try:
                    for row in future.result():
                        cached_by_code[str(row.get("symbol") or "").zfill(6)] = row
                except Exception:
                    continue

        rows = [cached_by_code[code] for code in codes if code in cached_by_code]
        if rows:
            self._write_cache(self.cache_dir / f"easy_tdx_daily_{trade_date}.json", {"trade_date": trade_date, "rows": rows})
        return rows

    def _daily_row_for_code(
        self,
        client: Any,
        code: str,
        meta: StockMeta,
        trade_date: str,
    ) -> dict[str, Any] | None:
        try:
            from easy_tdx import KlineCategory, Market
            rows = _records_from_payload(
                client.get_security_bars(
                    Market(market_id_for_code(code)),
                    code,
                    KlineCategory.DAY,
                    0,
                    260,
                )
            )
        except Exception:
            return None
        return self._daily_row_from_bars(code, meta, rows, trade_date)

    def _fetch_index_rows(self, trade_date: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for code, name in [("000001", "上证指数"), ("399001", "深证成指"), ("399006", "创业板指")]:
            bar = self._index_daily_row(code, name, trade_date)
            if bar:
                rows.append(bar)
        if rows:
            self._write_cache(self.cache_dir / f"easy_tdx_indices_{trade_date}.json", {"trade_date": trade_date, "rows": rows})
        return rows

    def _index_history_rows(self, code: str, count: int = 120) -> list[dict[str, Any]]:
        try:
            from easy_tdx import KlineCategory, Market, TdxClient
        except Exception as exc:  # pragma: no cover
            raise DataSourceError("easy_tdx未安装，无法读取指数日K。") from exc
        try:
            with TdxClient(timeout=float(self.settings.easy_tdx_timeout_seconds)) as client:
                return _records_from_payload(
                    client.get_index_bars(
                        Market(market_id_for_index_code(code)),
                        code,
                        KlineCategory.DAY,
                        0,
                        count,
                    )
                )
        except Exception as exc:
            raise DataSourceError(f"easy_tdx指数日K读取失败：{exc}") from exc

    def _index_daily_row(self, code: str, name: str, trade_date: str) -> dict[str, Any] | None:  # noqa: ARG002
        rows = self._index_history_rows(code, count=260)
        meta = StockMeta(code=code, ts_code=guess_ts_code(code), name=name)
        return self._daily_row_from_bars(code, meta, rows, trade_date, index=True)

    def _daily_row_from_bars(
        self,
        code: str,
        meta: StockMeta,
        rows: list[dict[str, Any]],
        trade_date: str,
        *,
        index: bool = False,
    ) -> dict[str, Any] | None:
        normalized: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            date = self._date_from_bar(row)
            if date:
                normalized.append((date, row))
        normalized.sort(key=lambda item: item[0])
        for idx, (date, row) in enumerate(normalized):
            if date != trade_date:
                continue
            close = self._float(row.get("close"))
            prev_close = self._float(normalized[idx - 1][1].get("close")) if idx > 0 else self._float(row.get("pre_close") or row.get("open") or close)
            pct_chg = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
            return {
                "ts_code": guess_ts_code(code),
                "symbol": code,
                "trade_date": date,
                "name": meta.name,
                "open": self._float(row.get("open")),
                "high": self._float(row.get("high")),
                "low": self._float(row.get("low")),
                "close": close,
                "pre_close": prev_close,
                "pct_chg": pct_chg,
                "vol": self._float(row.get("vol") or row.get("volume")),
                "amount": self._float(row.get("amount")),
                "index": index,
            }
        return None

    @staticmethod
    def _date_from_bar(row: dict[str, Any]) -> str:
        value = row.get("date") or row.get("datetime") or row.get("time")
        if hasattr(value, "strftime"):
            try:
                return value.strftime("%Y%m%d")
            except Exception:
                return ""
        text = str(value or "").strip()
        if not text:
            return ""
        digits = "".join(ch for ch in text[:10] if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else ""

    def _load_cached_daily(self, trade_date: str) -> list[dict[str, Any]]:
        return self._load_cached_payload(self.cache_dir / f"easy_tdx_daily_{trade_date}.json", trade_date)

    def _load_cached_indices(self, trade_date: str) -> list[dict[str, Any]]:
        return self._load_cached_payload(self.cache_dir / f"easy_tdx_indices_{trade_date}.json", trade_date)

    def _load_cached_payload(self, path: Path, trade_date: str) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if str(payload.get("trade_date")) != trade_date:
            return []
        rows = payload.get("rows", [])
        return rows if isinstance(rows, list) else []

    def _write_cache(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _quote_from_daily(
        self,
        code: str,
        meta: StockMeta,
        row: dict[str, Any] | None,
        trade_date: str,
    ) -> Quote:
        if not row:
            raise DataSourceError(f"缺少 {code} 的 easy_tdx 日K数据。")
        price = float(row["close"])
        prev_close = float(row["pre_close"])
        high = float(row["high"])
        low = float(row["low"])
        change_pct = float(row["pct_chg"])
        amount = float(row["amount"])
        volume = float(row["vol"])
        themes = meta.normalized_themes()
        return Quote(
            code=code,
            name=meta.name or SECURITY_NAMES.get(code, code),
            themes=themes,
            price=round(price, 2),
            prev_close=round(prev_close, 2),
            open=round(float(row["open"]), 2),
            high=round(high, 2),
            low=round(low, 2),
            day_high=round(high, 2),
            day_low=round(low, 2),
            change_pct=round(change_pct, 2),
            volume=volume,
            amount=amount,
            minute_amount=amount / 240 if amount else 0,
            minute_amount_ratio=1.0,
            limit_up=self._limit_up(change_pct, meta),
            limit_down=self._limit_down(change_pct, meta),
            opened_limit=self._opened_limit(high, prev_close, change_pct, meta),
            core=meta.core,
            updated_at="15:00:00",
            auction=AuctionSnapshot(trade_date=trade_date),
        )

    def _indices_from_rows(self, rows: list[dict[str, Any]], trade_date: str) -> list[IndexSnapshot]:
        by_code = {str(row.get("symbol") or str(row.get("ts_code", ""))[:6]).zfill(6): row for row in rows}
        mapping = [
            ("000001", "上证指数"),
            ("399001", "深证成指"),
            ("399006", "创业板指"),
        ]
        indices: list[IndexSnapshot] = []
        for code, name in mapping:
            row = by_code.get(code)
            if not row:
                continue
            price = float(row["close"])
            prev_close = float(row["pre_close"])
            low = float(row["low"])
            high = float(row["high"])
            indices.append(
                IndexSnapshot(
                    code=code,
                    name=name,
                    price=round(price, 2),
                    prev_close=round(prev_close, 2),
                    open=round(float(row["open"]), 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    change_pct=round(float(row["pct_chg"]), 2),
                    rebound_from_low_pct=round((price - low) / low * 100, 2) if low else 0,
                    minute_amount_ratio=1.0,
                    amount=float(row["amount"]),
                )
            )
        return indices

    def _limit_up(self, pct_chg: float, meta: StockMeta) -> bool:
        threshold = self._limit_threshold(meta)
        return pct_chg >= threshold - 0.2

    def _limit_down(self, pct_chg: float, meta: StockMeta) -> bool:
        threshold = self._limit_threshold(meta)
        return pct_chg <= -threshold + 0.2

    def _opened_limit(self, high: float, prev_close: float, pct_chg: float, meta: StockMeta) -> bool:
        threshold = self._limit_threshold(meta)
        return bool(prev_close and high >= prev_close * (1 + threshold / 100 * 0.95) and pct_chg < threshold - 0.7)

    def _limit_threshold(self, meta: StockMeta) -> float:
        code = meta.code
        if code.startswith(("300", "301", "688", "92")) or meta.market in {"创业板", "科创板", "北交所"}:
            if code.startswith("92") or meta.market == "北交所":
                return 30.0
            return 20.0
        return 10.0

    @staticmethod
    def _float(value: Any) -> float:
        try:
            number = float(value)
        except Exception:
            return 0.0
        if number != number:
            return 0.0
        return number

class ReplayMarketDataSource:
    """Deterministic intraday replay that mirrors the 2026-08-06 logic chain."""

    def fetch(
        self,
        watchlist: list[WatchlistItem],
        themes: list[dict],
        universe: dict[str, StockMeta] | None = None,
    ) -> MarketSnapshot:
        phase, data_mode, active_source, note, clock_label, frozen = self._resolve_replay_state()
        all_codes = self._codes_from_watchlist_and_themes(watchlist, themes, universe)
        metadata = self._metadata(watchlist, themes, universe)
        now = clock_label

        quotes = [self._quote_for_code(code, metadata.get(code, {}), phase, now) for code in all_codes]
        indices = self._indices_for_phase(phase)
        return MarketSnapshot(
            quotes=quotes,
            indices=indices,
            data_mode=data_mode,
            source_status={
                "active_source": active_source,
                "phase": phase,
                "note": note,
                "clock_label": clock_label,
                "frozen": frozen,
            },
        )

    def _resolve_replay_state(self) -> tuple[int, str, str, str, str, bool]:
        if is_trading_window():
            phase = int(time.time() / 12) % 4
            return (
                phase,
                "replay",
                "replay",
                "当前使用内置回放数据，用于非交易时段和依赖不可用时验证信号链路。",
                china_now().strftime("%H:%M:%S"),
                False,
            )

        return (
            3,
            "closed_static",
            "replay",
            "真实行情不可用，展示静态回放样例。",
            "15:00:00",
            True,
        )

    def _codes_from_watchlist_and_themes(
        self,
        watchlist: list[WatchlistItem],
        themes: list[dict],
        universe: dict[str, StockMeta] | None = None,
    ) -> list[str]:
        if universe:
            return list(universe.keys())
        codes = [item.code for item in watchlist]
        for theme in themes:
            codes.extend(theme.get("members", []))
            codes.extend(theme.get("core_codes", []))
        return list(dict.fromkeys(codes))

    def _metadata(
        self,
        watchlist: list[WatchlistItem],
        themes: list[dict],
        universe: dict[str, StockMeta] | None = None,
    ) -> dict[str, dict[str, Any]]:
        metadata: dict[str, dict[str, Any]] = {}
        if universe:
            for code, meta in universe.items():
                metadata[code] = {
                    "name": meta.name,
                    "themes": list(meta.normalized_themes()),
                    "core": meta.core,
                }
        for item in watchlist:
            metadata[item.code] = {"name": item.name, "themes": list(item.themes), "core": item.core}
        for theme in themes:
            name = theme.get("name")
            for code in itertools.chain(theme.get("members", []), theme.get("core_codes", [])):
                entry = metadata.setdefault(code, {"name": SECURITY_NAMES.get(code, code), "themes": [], "core": False})
                if name and name not in entry["themes"]:
                    entry["themes"].append(name)
                if code in set(theme.get("core_codes", [])):
                    entry["core"] = True
        return metadata

    def _quote_for_code(self, code: str, meta: dict[str, Any], phase: int, now: str) -> Quote:
        base = {
            "300308": (100.0, [-5.8, -1.8, 4.2, 4.0], [94.2, 93.7, 93.7, 93.7], [96.5, 99.8, 104.5, 105.2], [1.1, 2.4, 2.1, 1.0]),
            "300476": (60.0, [1.2, 6.4, 10.1, 7.7], [60.2, 59.8, 59.8, 59.8], [61.8, 66.1, 66.1, 66.1], [1.6, 3.6, 2.2, 0.9]),
            "002428": (18.0, [3.0, 9.8, 7.2, 4.8], [18.1, 17.9, 17.9, 17.9], [19.8, 19.8, 19.8, 19.8], [1.4, 2.8, 1.8, 0.8]),
            "002463": (42.0, [-0.5, 3.6, 5.9, 3.7], [41.4, 41.4, 41.4, 41.4], [42.8, 44.7, 45.1, 45.1], [1.0, 2.1, 1.6, 0.8]),
            "600183": (28.0, [0.2, 2.7, 4.1, 2.5], [27.8, 27.8, 27.8, 27.8], [28.4, 29.4, 29.6, 29.6], [1.0, 1.9, 1.4, 0.7]),
            "300502": (82.0, [-3.4, 1.8, 6.2, 5.5], [78.8, 78.8, 78.8, 78.8], [81.0, 87.5, 87.9, 87.9], [1.2, 2.2, 2.0, 1.0]),
        }
        default = (24.0, [-0.8, 1.1, 2.4, 0.9], [23.6, 23.6, 23.6, 23.6], [24.3, 24.9, 25.1, 25.1], [0.9, 1.2, 1.1, 0.7])
        prev_close, changes, lows, highs, volume_ratios = base.get(code, default)
        change_pct = changes[phase]
        price = round(prev_close * (1 + change_pct / 100), 2)
        open_price = round(prev_close * (1 + (changes[0] - 0.3) / 100), 2)
        low = min(lows[phase], price, open_price)
        high = max(highs[phase], price, open_price)
        amount = round(prev_close * 10_000_000 * (1 + max(change_pct, 0) / 10), 2)
        limit_threshold = self._replay_limit_threshold(code, meta)
        limit_up = change_pct >= limit_threshold - 0.2
        limit_down = change_pct <= -limit_threshold + 0.2
        opened_limit = high >= prev_close * (1 + limit_threshold / 100 * 0.95) and change_pct < limit_threshold - 0.7

        return Quote(
            code=code,
            name=meta.get("name") or SECURITY_NAMES.get(code, code),
            themes=meta.get("themes", []),
            price=price,
            prev_close=prev_close,
            open=open_price,
            high=high,
            low=low,
            day_high=high,
            day_low=low,
            change_pct=change_pct,
            volume=amount / max(price, 1),
            amount=amount,
            minute_amount=amount / 120,
            minute_amount_ratio=volume_ratios[phase],
            limit_up=limit_up,
            limit_down=limit_down,
            opened_limit=opened_limit,
            core=bool(meta.get("core", False)),
            updated_at=now,
        )

    def _replay_limit_threshold(self, code: str, meta: dict[str, Any]) -> float:
        market = str(meta.get("market") or "")
        if code.startswith(("300", "301", "688", "92")) or market in {"创业板", "科创板", "北交所"}:
            return 30.0 if code.startswith("92") or market == "北交所" else 20.0
        return 10.0

    def _indices_for_phase(self, phase: int) -> list[IndexSnapshot]:
        now_data = [
            ("000001", "上证指数", 3300.0, [-0.8, -0.1, 0.5, 0.25], [3268, 3268, 3268, 3268], [3298, 3318, 3324, 3324], [0.9, 1.2, 1.35, 0.95]),
            ("399001", "深证成指", 10400.0, [-1.1, 0.2, 0.9, 0.45], [10260, 10260, 10260, 10260], [10420, 10540, 10565, 10565], [0.9, 1.25, 1.45, 0.92]),
            ("399006", "创业板指", 2200.0, [-1.6, 0.4, 1.2, 0.65], [2158, 2158, 2158, 2158], [2210, 2232, 2240, 2240], [1.0, 1.35, 1.55, 0.96]),
        ]
        indices: list[IndexSnapshot] = []
        for code, name, prev_close, changes, lows, highs, ratios in now_data:
            change_pct = changes[phase]
            price = round(prev_close * (1 + change_pct / 100), 2)
            low = lows[phase]
            high = highs[phase]
            rebound = (price - low) / low * 100 if low else 0
            indices.append(
                IndexSnapshot(
                    code=code,
                    name=name,
                    price=price,
                    prev_close=prev_close,
                    open=round(prev_close * (1 + (changes[0] - 0.1) / 100), 2),
                    high=high,
                    low=low,
                    change_pct=change_pct,
                    rebound_from_low_pct=round(rebound, 2),
                    minute_amount_ratio=ratios[phase],
                    amount=prev_close * 100_000_000,
                )
            )
        return indices


class EasyTdxMarketDataSource:
    def __init__(self, settings: AppSettings, close_source: Any) -> None:
        self.settings = settings
        self.close_source = close_source
        self.hosts = [
            "115.238.56.198:7709",
            "180.153.18.170:7709",
            "119.147.212.81:7709",
            "47.103.48.45:7709",
            "106.14.95.149:7709",
        ]
        self.chunk_size = 80
        self.quote_workers = max(1, int(getattr(settings, "easy_tdx_quote_workers", 1) or 1))
        self.auction_tracker = AuctionSnapshotTracker(settings)
        self._latest_auction: dict[str, AuctionSnapshot] = {}
        self._auction_session_date = ""
        self._transaction_cache: dict[
            tuple[str, str, int, bool],
            tuple[float, TransactionFlowObservation],
        ] = {}
        self._trade_date_lookup_cache: tuple[float, str, set[str]] | None = None
        self._seed_snapshot_cache: tuple[str, int, MarketSnapshot] | None = None

    def _client(self) -> Any:
        try:
            from easy_tdx import TdxClient
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise DataSourceError("easy_tdx未安装，无法连接TDX L1行情。") from exc
        host, port = self._preferred_tdx_host()
        return TdxClient(host=host, port=port, timeout=float(self.settings.easy_tdx_timeout_seconds))

    def _history_client(self) -> Any:
        return self._client()

    def _mac_client(self) -> Any:
        try:
            from easy_tdx import MacClient
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise DataSourceError("easy_tdx未安装，无法连接TDX扩展行情。") from exc
        return MacClient(timeout=float(self.settings.easy_tdx_timeout_seconds))

    def _preferred_tdx_host(self) -> tuple[str | None, int | None]:
        if not self.hosts:
            return None, None
        host = str(self.hosts[0] or "").strip()
        if not host:
            return None, None
        if ":" not in host:
            return host, None
        ip, port = host.rsplit(":", 1)
        try:
            return ip, int(port)
        except ValueError:
            return ip, None

    def fetch(self, universe: dict[str, StockMeta]) -> MarketSnapshot:
        now = china_now()
        seed_snapshot, seed_cache_hit = self._seed_snapshot(universe, now)
        seed_by_code = {quote.code: quote for quote in seed_snapshot.quotes}

        with self._client() as client:
            raw_quotes, live_meta = self._fetch_quotes(client, universe)
            live_by_code = {quote.code: quote for quote in raw_quotes}
            merged_quotes = []
            today = now.strftime("%Y%m%d")
            if self._auction_session_date != today:
                self._latest_auction.clear()
                self._auction_session_date = today
            easy_tdx_auction_by_code: dict[str, AuctionSnapshot] = {}
            should_fetch_easy_tdx_auction = self._should_fetch_current_easy_tdx_auction(now, len(universe))
            if should_fetch_easy_tdx_auction:
                try:
                    with self._mac_client() as auction_client:
                        for auction_code in universe:
                            base_quote = live_by_code.get(auction_code) or seed_by_code.get(auction_code)
                            snapshot = self._fetch_current_auction_snapshot(
                                auction_client,
                                auction_code,
                                base_quote.prev_close if base_quote else 0.0,
                                today,
                            )
                            if snapshot.available:
                                easy_tdx_auction_by_code[auction_code] = snapshot
                except Exception:
                    easy_tdx_auction_by_code = {}
            for code, meta in universe.items():
                quote = live_by_code.get(code) or seed_by_code.get(code)
                if quote is None:
                    continue
                seed_quote = seed_by_code.get(code)
                auction = quote.auction
                if code in easy_tdx_auction_by_code:
                    auction = easy_tdx_auction_by_code[code]
                if auction.available and auction.trade_date == today:
                    auction = self.auction_tracker.observe(code, auction)
                    self._latest_auction[code] = auction
                elif not auction.available:
                    # Keep the last pre-open plan visible during the first
                    # minutes after 09:30.  It is still labelled proxy/actual
                    # and is never reconstructed from the opening price.
                    auction = self._latest_auction.get(code, AuctionSnapshot())
                if (
                    not auction.available
                    and seed_quote
                    and seed_quote.auction.available
                    and seed_quote.auction.trade_date == today
                ):
                    auction = seed_quote.auction
                quote = quote.model_copy(update={"auction": auction})
                merged_quotes.append(quote)
            indices = self._fetch_indices(client, seed_snapshot.indices)
            auction_available_count = sum(1 for quote in merged_quotes if quote.auction.available)
            order_book_count = sum(1 for quote in merged_quotes if quote.order_flow.available)
            level2_count = sum(1 for quote in merged_quotes if quote.order_flow.level2_available)
            auction_quality = self._auction_quality(merged_quotes)
            if easy_tdx_auction_by_code:
                auction_source = "easy_tdx_auction"
                auction_note = "easy_tdx 返回真实集合竞价记录。"
            elif auction_quality == "actual":
                auction_source = "auction_snapshot"
                auction_note = "当前返回真实集合竞价记录。"
            elif auction_quality == "proxy":
                auction_source = "tdx_l1_preopen_quote_proxy"
                auction_note = "未读取到集合竞价明细，使用竞价时段 L1 指示价/五档轨迹代理。"
            else:
                auction_source = "unavailable"
                auction_note = "当前未收到竞价快照；easy_tdx 集合竞价明细可在个股竞价接口按需读取。"
            session = market_session(now)
            snapshot_frozen = session in {"pre_market", "post_close", "closed_day"}
            trade_date = (
                str(seed_snapshot.source_status.get("trade_date") or today)
                if snapshot_frozen
                else today
            )
            clock_label = "15:00:00" if snapshot_frozen else now.strftime("%H:%M:%S")
            session_note = (
                "午间休市：保留 easy_tdx 当天 L1 快照，等待 13:00 后继续刷新。"
                if session == "lunch_break"
                else "非盘中使用 easy_tdx TDX L1 最新快照并标记冻结。"
                if snapshot_frozen
                else "交易时段使用 easy_tdx TDX L1 实时行情。"
            )
            return MarketSnapshot(
                quotes=merged_quotes,
                indices=indices,
                data_mode="live",
                source_status={
                    "active_source": "easy_tdx",
                    "quote_count": len(merged_quotes),
                    "raw_quote_count": live_meta.get("raw_quote_count", len(raw_quotes)),
                    "universe_size": len(universe),
                    "live_chunks": live_meta["chunks"],
                    "quote_workers": live_meta.get("quote_workers", 1),
                    "quote_fetch_elapsed_ms": live_meta.get("quote_fetch_elapsed_ms", 0),
                    "tdx_host": self.hosts[0] if self.hosts else "package_default",
                    "seed_source": seed_snapshot.source_status.get("active_source"),
                    "seed_cache_hit": seed_cache_hit,
                    "seed_error": seed_snapshot.source_status.get("seed_error"),
                    "trade_date": trade_date,
                    "clock_label": clock_label,
                    "frozen": snapshot_frozen,
                    "market_session": session,
                    "lunch_break": session == "lunch_break",
                    "auction_available_count": auction_available_count,
                    "auction_source": auction_source,
                    "auction_data_quality": auction_quality,
                    "auction_snapshot_count": max(
                        (quote.auction.snapshot_count for quote in merged_quotes),
                        default=0,
                    ),
                    "order_book_available_count": order_book_count,
                    "level2_available_count": level2_count,
                    "quote_capability": "easy_tdx_quote_snapshot",
                    "order_book_capability": "quote_depth",
                    "quote_depth": True,
                    "quote_depth_levels": 5,
                    "ten_level_quote_depth": False,
                    "transaction_tape": True,
                    "level2_available": False,
                    "level2_note": (
                        "当前 easy_tdx TDX L1 行情未返回委托队列或逐笔委托；五档仅作 L1 盘口代理。"
                    ),
                    "auction_note": auction_note,
                    "note": session_note,
                },
            )

    def _seed_snapshot(self, universe: dict[str, StockMeta], now: datetime) -> tuple[MarketSnapshot, bool]:
        cache_key = f"{now.strftime('%Y%m%d')}:{len(universe)}"
        if self._seed_snapshot_cache is not None:
            cached_key, _, snapshot = self._seed_snapshot_cache
            if cached_key == cache_key:
                return snapshot, True
        if hasattr(self.close_source, "fetch_seed"):
            try:
                snapshot = self.close_source.fetch_seed(universe)
            except Exception as exc:
                return self._unavailable_seed_snapshot(now, exc), False
        else:
            try:
                snapshot = self.close_source.fetch(universe)
            except Exception as exc:
                return self._unavailable_seed_snapshot(now, exc), False
        self._seed_snapshot_cache = (cache_key, int(time.time()), snapshot)
        return snapshot, False

    @staticmethod
    def _unavailable_seed_snapshot(now: datetime, exc: Exception) -> MarketSnapshot:
        return MarketSnapshot(
            quotes=[],
            indices=[],
            data_mode="unavailable",
            source_status={
                "active_source": "unavailable",
                "trade_date": now.strftime("%Y%m%d"),
                "clock_label": now.strftime("%H:%M:%S"),
                "frozen": False,
                "seed_error": jsonable_market_value(exc, max_text=240),
                "note": "easy_tdx 日K seed 不可用；实时行情继续使用 L1 quote 快照。",
            },
        )

    def _should_fetch_current_auction(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        current = now.hour * 60 + now.minute
        return bool(
            9 * 60 + 15 <= current < 9 * 60 + 40
            or (9 * 60 + 40 <= current <= 15 * 60 and not self._latest_auction)
        )

    def _should_fetch_current_easy_tdx_auction(self, now: datetime, universe_size: int) -> bool:
        if universe_size > 200:
            return False
        return is_preopen_window(now)

    def _fetch_quotes(self, api: Any, universe: dict[str, StockMeta]) -> tuple[list[Quote], dict[str, Any]]:
        started_at = time.perf_counter()
        raw_quotes: list[Any] = []
        failed_chunks: list[dict[str, Any]] = []
        skipped_codes: list[str] = []
        codes = [str(code).zfill(6) for code in universe.keys()]
        chunks = [
            (start, codes[start:start + self.chunk_size])
            for start in range(0, len(codes), self.chunk_size)
            if codes[start:start + self.chunk_size]
        ]
        if not hasattr(api, "get_security_quotes"):
            raise DataSourceError("easy_tdx行情客户端不支持股票快照接口。")

        if self.quote_workers > 1 and len(chunks) > 2:
            raw_quotes, failed_chunks, skipped_codes = self._fetch_quote_chunks_parallel(chunks)
        else:
            for start, chunk_codes in chunks:
                recovered_rows, failed, failure = self._fetch_quote_chunk_with_recovery(api, chunk_codes)
                raw_quotes.extend(recovered_rows)
                if failure is not None:
                    skipped_codes.extend(failed)
                    failed_chunks.append(
                        self._quote_chunk_failure_meta(
                            start,
                            chunk_codes,
                            recovered=len(recovered_rows),
                            failed=failed,
                            exc=failure,
                        )
                    )
        if not raw_quotes:
            first_error = failed_chunks[0].get("error") if failed_chunks else "quote接口返回0条"
            raise DataSourceError(f"easy_tdx实时行情快照无有效返回：{first_error}")
        quotes = []
        for raw in raw_quotes:
            code = str(self._raw_value(raw, "code") or "").zfill(6)
            meta = universe.get(code)
            if meta is None:
                meta = StockMeta(code=code, ts_code=guess_ts_code(code), name=SECURITY_NAMES.get(code, code))
            quotes.append(self._convert_quote(raw, meta))
        meta: dict[str, Any] = {
            "chunks": len(chunks),
            "raw_quote_count": len(raw_quotes),
            "quote_workers": self.quote_workers if len(chunks) > 2 else 1,
            "quote_fetch_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
        }
        if failed_chunks:
            meta["failed_chunks"] = failed_chunks
            meta["skipped_codes"] = skipped_codes[:40]
            meta["skipped_count"] = len(skipped_codes)
        return quotes, meta

    def fetch_quote_subset(
        self,
        codes: list[str],
        base_quotes: list[Quote] | None = None,
    ) -> dict[str, Quote]:
        normalized_codes = list(dict.fromkeys(str(code or "").zfill(6) for code in codes if str(code or "").strip()))
        if not normalized_codes:
            return {}
        base_by_code = {quote.code: quote for quote in (base_quotes or [])}
        universe = {
            code: StockMeta(
                code=code,
                ts_code=guess_ts_code(code),
                name=base_by_code.get(code).name if code in base_by_code else SECURITY_NAMES.get(code, code),
                themes=list(base_by_code.get(code).themes or []) if code in base_by_code else [],
                core=bool(base_by_code.get(code).core) if code in base_by_code else False,
            )
            for code in normalized_codes
        }
        with self._client() as client:
            raw_quotes, _meta = self._fetch_quotes(client, universe)
        merged: dict[str, Quote] = {}
        today = china_now().strftime("%Y%m%d")
        for quote in raw_quotes:
            base = base_by_code.get(quote.code)
            if base is not None:
                auction = quote.auction if quote.auction.available else base.auction
                if auction.available and auction.trade_date == today:
                    auction = self.auction_tracker.observe(quote.code, auction)
                quote = quote.model_copy(
                    update={
                        "themes": list(base.themes),
                        "core": base.core,
                        "auction": auction,
                    }
                )
            merged[quote.code] = quote
        return merged

    def _fetch_quote_chunks_parallel(
        self,
        chunks: list[tuple[int, list[str]]],
    ) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
        raw_by_start: dict[int, list[Any]] = {}
        failed_chunks: list[dict[str, Any]] = []
        skipped_codes: list[str] = []
        workers = min(self.quote_workers, len(chunks))

        def fetch_chunk(start: int, chunk_codes: list[str]) -> tuple[int, list[Any], list[str], Exception | None]:
            with self._client() as client:
                rows, failed, failure = self._fetch_quote_chunk_with_recovery(client, chunk_codes)
                return start, rows, failed, failure

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(fetch_chunk, start, chunk_codes): (start, chunk_codes)
                for start, chunk_codes in chunks
            }
            for future in as_completed(futures):
                start, chunk_codes = futures[future]
                try:
                    result_start, rows, failed, failure = future.result()
                except Exception as exc:  # pragma: no cover - provider dependent
                    result_start, rows, failed, failure = start, [], chunk_codes, exc
                raw_by_start[result_start] = rows
                if failure is not None:
                    skipped_codes.extend(failed)
                    failed_chunks.append(
                        self._quote_chunk_failure_meta(
                            result_start,
                            chunk_codes,
                            recovered=len(rows),
                            failed=failed,
                            exc=failure,
                        )
                    )

        raw_quotes: list[Any] = []
        for start, _chunk_codes in chunks:
            raw_quotes.extend(raw_by_start.get(start, []))
        failed_chunks.sort(key=lambda item: int(item.get("start") or 0))
        return raw_quotes, failed_chunks, skipped_codes

    def _fetch_quote_chunk_with_recovery(
        self,
        api: Any,
        chunk_codes: list[str],
    ) -> tuple[list[Any], list[str], Exception | None]:
        request = [(easy_tdx_market_for_code(code), code) for code in chunk_codes]
        try:
            result = api.get_security_quotes(request)
            rows = _records_from_payload(result)
            if not rows:
                if len(chunk_codes) > 1:
                    recovered, failed = self._fetch_quotes_individually(api, chunk_codes)
                    return recovered, failed, DataSourceError("quote批次返回0条，已尝试逐只补拉")
                return rows, [], None
            returned_codes = {
                str(self._raw_value(row, "code") or "").zfill(6)
                for row in rows
                if str(self._raw_value(row, "code") or "").strip()
            }
            missing_codes = [code for code in chunk_codes if code not in returned_codes]
            if missing_codes:
                recovered, failed = self._fetch_quotes_individually(api, missing_codes)
                rows.extend(recovered)
                return rows, failed, DataSourceError(
                    f"quote批次少返回{len(missing_codes)}只，已尝试逐只补拉"
                )
            return rows, [], None
        except Exception as exc:
            if self._quote_chunk_error_can_retry_individually(exc):
                recovered, failed = self._fetch_quotes_individually(api, chunk_codes)
            else:
                recovered, failed = [], chunk_codes
            return recovered, failed, exc

    @staticmethod
    def _quote_chunk_failure_meta(
        start: int,
        chunk_codes: list[str],
        *,
        recovered: int,
        failed: list[str],
        exc: Exception,
    ) -> dict[str, Any]:
        return {
            "start": start,
            "size": len(chunk_codes),
            "recovered": recovered,
            "skipped": len(failed),
            "sample_codes": chunk_codes[:5],
            "error": jsonable_market_value(exc, max_text=180),
        }

    def _fetch_quotes_individually(self, api: Any, codes: list[str]) -> tuple[list[Any], list[str]]:
        recovered: list[Any] = []
        failed: list[str] = []
        for code in codes:
            try:
                result = api.get_security_quotes([(easy_tdx_market_for_code(code), code)])
                rows = _records_from_payload(result)
            except Exception:
                failed.append(code)
                continue
            if rows:
                recovered.extend(rows)
            else:
                failed.append(code)
        return recovered, failed

    @staticmethod
    def _quote_chunk_error_can_retry_individually(exc: Exception) -> bool:
        text = str(exc).lower()
        recoverable_fragments = (
            "snapshot record marker",
            "bad response",
            "decode",
            "unpack",
            "security",
        )
        timeout_fragments = ("timeout", "timed out", "connect", "network", "refused")
        return any(fragment in text for fragment in recoverable_fragments) and not any(
            fragment in text for fragment in timeout_fragments
        )

    def _convert_quote(self, raw: Any, meta: StockMeta) -> Quote:
        code = str(self._raw_value(raw, "code") or meta.code).zfill(6)
        prev_close = self._first_raw_number(raw, "pre_close", "pre_close_price", "last_close", "last_close_price")
        price = self._first_raw_number(raw, "price", "last_price") or prev_close
        preopen = is_preopen_window()
        open_price = self._first_raw_number(raw, "open", "open_price") or (prev_close if preopen else price)
        high = self._first_raw_number(raw, "high", "high_price") or (prev_close if preopen else price)
        low = self._first_raw_number(raw, "low", "low_price") or (prev_close if preopen else price)
        amount = self._first_raw_number(raw, "amount", "turnover")
        volume = self._first_raw_number(raw, "vol", "total_hand", "volume")
        cur_vol = self._first_raw_number(raw, "cur_vol", "current_hand")
        minute_amount = cur_vol * price * 100 if cur_vol else 0
        minute_amount_ratio = self._minute_amount_ratio(
            minute_amount=minute_amount,
            cumulative_amount=amount,
            now=china_now(),
        )
        seed_themes = meta.normalized_themes()
        change_pct = calc_change_pct(price, prev_close)
        order_flow = self._order_flow_from_raw(raw, price, open_price, minute_amount_ratio)
        auction = self._auction_from_raw(raw, prev_close)
        return Quote(
            code=code,
            name=meta.name or SECURITY_NAMES.get(code, code),
            themes=seed_themes,
            price=round(price, 2),
            prev_close=prev_close,
            open=open_price,
            high=high,
            low=low,
            day_high=high,
            day_low=low,
            change_pct=change_pct,
            volume=volume,
            amount=amount,
            minute_amount=minute_amount,
            minute_amount_ratio=round(minute_amount_ratio, 2),
            limit_up=change_pct >= self._limit_threshold(meta) - 0.2,
            limit_down=change_pct <= -self._limit_threshold(meta) + 0.2,
            opened_limit=bool(prev_close and high >= prev_close * (1 + self._limit_threshold(meta) / 100 * 0.95) and change_pct < self._limit_threshold(meta) - 0.7),
            core=meta.core,
            updated_at=china_now().strftime("%H:%M:%S"),
            order_flow=order_flow,
            auction=auction,
        )

    def _minute_amount_ratio(
        self,
        minute_amount: float,
        cumulative_amount: float,
        now: datetime,
    ) -> float:
        """Compare the current minute with the elapsed-session average.

        easy_tdx 的 ``current_hand`` 是当前成交量字段，不是委托队列式成交流。
        Using elapsed minutes avoids the old fixed 180-minute divisor
        making the 09:30-09:37 decision window systematically too weak.
        """
        if minute_amount <= 0 or cumulative_amount <= 0:
            return 1.0
        elapsed = self._elapsed_session_minutes(now)
        baseline = cumulative_amount / max(1, elapsed)
        return round(max(0.2, min(8.0, minute_amount / baseline if baseline else 1.0)), 2)

    @staticmethod
    def _elapsed_session_minutes(now: datetime) -> int:
        current = now.hour * 60 + now.minute
        if current < 9 * 60 + 30:
            return 1
        if current <= 11 * 60 + 30:
            return max(1, current - (9 * 60 + 30) + 1)
        if current < 13 * 60:
            return 120
        if current <= 15 * 60:
            return 120 + max(1, current - (13 * 60) + 1)
        return 240

    def _auction_from_raw(self, raw: Any, prev_close: float) -> AuctionSnapshot:
        """Normalize explicit auction fields, then fall back to a labelled proxy.

        easy_tdx exposes real current-day call-auction records through a separate
        endpoint. Quote fields are accepted here only inside the pre-open
        window and stay labelled as an L1 proxy.
        """
        price = self._first_raw_number(raw, "auction_price", "match_price", "auction_match_price")
        reference = prev_close or self._first_raw_number(raw, "pre_close", "pre_close_price", "last_close", "last_close_price")
        now = china_now()
        if price > 0 and reference > 0:
            raw_volume = self._first_raw_number(
                raw,
                "matched",
                "matched_volume",
                "auction_vol",
                "auction_volume",
                "match_vol",
            )
            volume = raw_volume * 100
            amount = self._first_raw_number(raw, "auction_amount", "match_amount") or price * volume
            unmatched_signed = (
                self._object_number(raw, "unmatched_signed_raw", signed=True)
                or self._object_number(raw, "unmatched", signed=True)
            )
            unmatched_buy = self._first_raw_number(
                raw,
                "unmatched_buy",
                "auction_unmatched_buy",
                "bid_unmatched",
                "unmatched_bid",
            )
            unmatched_sell = self._first_raw_number(
                raw,
                "unmatched_sell",
                "auction_unmatched_sell",
                "ask_unmatched",
                "unmatched_ask",
            )
            if not unmatched_buy and not unmatched_sell and unmatched_signed:
                if unmatched_signed > 0:
                    unmatched_buy = unmatched_signed
                else:
                    unmatched_sell = abs(unmatched_signed)
            unmatched_total = unmatched_buy + unmatched_sell
            imbalance = (
                (unmatched_buy - unmatched_sell) / unmatched_total * 100
                if unmatched_total
                else 0
            )
            as_of = str(self._raw_value(raw, "time_label") or self._raw_value(raw, "time") or now.strftime("%H:%M:%S"))
            return AuctionSnapshot(
                available=True,
                source="easy_tdx_auction",
                data_quality="actual",
                trade_date=now.strftime("%Y%m%d"),
                as_of=as_of,
                price=round(price, 2),
                prev_close=round(reference, 2),
                change_pct=calc_change_pct(price, reference),
                volume=round(volume, 2),
                amount=round(amount, 2),
                volume_ratio=round(self._first_raw_number(raw, "auction_volume_ratio"), 2),
                unmatched_buy_volume=round(unmatched_buy * 100, 2),
                unmatched_sell_volume=round(unmatched_sell * 100, 2),
                order_imbalance_pct=round(imbalance, 2),
                status="实时集合竞价",
                phase=self._auction_phase(now),
                indicative=now.hour == 9 and now.minute < 25,
                confidence="较高：行情服务器显式竞价字段",
                note="开盘前候选因子，不能替代开盘后的分时确认",
            )

        if not is_preopen_window(now):
            return AuctionSnapshot()

        # Quote packets may expose an indicative price and five-level depth
        # during call auction. This is still an L1 quote snapshot, so keep the
        # weaker proxy label unless the auction endpoint returned a series.
        indicative_price = self._first_raw_number(raw, "price", "last_price")
        if indicative_price <= 0 or reference <= 0:
            return AuctionSnapshot()
        bid_volume = sum(level.volume for level in self._quote_levels(raw, "bid"))
        ask_volume = sum(level.volume for level in self._quote_levels(raw, "ask"))
        matched_hands = self._first_raw_number(raw, "cur_vol", "current_hand", "auction_vol", "match_vol")
        if matched_hands <= 0 and bid_volume + ask_volume <= 0:
            return AuctionSnapshot()
        depth_total = bid_volume + ask_volume
        imbalance_pct = (bid_volume - ask_volume) / depth_total * 100 if depth_total else 0
        matched_volume = matched_hands * 100
        status = "竞价结果待开盘" if now.hour == 9 and now.minute >= 25 else "实时竞价预估"
        return AuctionSnapshot(
            available=True,
            source="tdx_l1_preopen_quote",
            data_quality="proxy",
            trade_date=now.strftime("%Y%m%d"),
            as_of=now.strftime("%H:%M:%S"),
            price=round(indicative_price, 2),
            prev_close=round(reference, 2),
            change_pct=calc_change_pct(indicative_price, reference),
            volume=round(matched_volume, 2),
            amount=round(indicative_price * matched_volume, 2),
            volume_ratio=0,
            order_imbalance_pct=round(imbalance_pct, 2),
            bid_depth_volume=round(bid_volume, 2),
            ask_depth_volume=round(ask_volume, 2),
            status=status,
            phase=self._auction_phase(now),
            indicative=True,
            confidence="中等：TDX L1竞价时段价格/五档快照代理",
            note="仅生成开盘候选预案；09:30后仍需指数、板块与分时量能确认",
        )

    def _fetch_current_auction_snapshot(
        self,
        client: Any,
        code: str,
        prev_close: float,
        trade_date: str,
    ) -> AuctionSnapshot:
        try:
            series = client.get_auction(_market_id_for_tdx_code(code), str(code).zfill(6))
        except Exception:
            return AuctionSnapshot()
        return self._auction_from_series(series, prev_close=prev_close, trade_date=trade_date)

    def _auction_from_series(self, series: Any, prev_close: float, trade_date: str) -> AuctionSnapshot:
        return self._auction_from_rows(_records_from_payload(series), prev_close=prev_close, trade_date=trade_date)

    def _auction_from_rows(self, rows: list[dict[str, Any]], prev_close: float, trade_date: str) -> AuctionSnapshot:
        if not rows:
            return AuctionSnapshot()
        latest = rows[-1]
        price = self._first_raw_number(latest, "price", "auction_price", "match_price")
        matched_hands = self._first_raw_number(latest, "matched", "matched_volume", "auction_vol", "match_vol")
        if price <= 0:
            return AuctionSnapshot()
        reference = prev_close
        matched_volume = matched_hands * 100
        unmatched_hands = self._object_number(latest, "unmatched_volume")
        unmatched_direction = self._raw_value(latest, "unmatched_direction_raw")
        unmatched_signed = (
            self._object_number(latest, "unmatched_signed_raw", signed=True)
            or self._object_number(latest, "unmatched", signed=True)
        )
        if not unmatched_hands and unmatched_signed:
            unmatched_hands = abs(unmatched_signed)
        unmatched_buy = 0.0
        unmatched_sell = 0.0
        if unmatched_signed > 0:
            unmatched_buy = unmatched_hands * 100
        elif unmatched_signed < 0:
            unmatched_sell = unmatched_hands * 100
        imbalance = 0.0
        unmatched_total = unmatched_buy + unmatched_sell
        if unmatched_total:
            imbalance = (unmatched_buy - unmatched_sell) / unmatched_total * 100
        time_label = market_time_label(self._raw_value(latest, "time_label") or self._raw_value(latest, "time"))
        return AuctionSnapshot(
            available=True,
            source="easy_tdx_auction",
            data_quality="actual",
            trade_date=trade_date,
            as_of=time_label,
            price=round(price, 2),
            prev_close=round(reference, 2),
            change_pct=calc_change_pct(price, reference),
            volume=round(matched_volume, 2),
            amount=round(price * matched_volume, 2),
            order_imbalance_pct=round(imbalance, 2),
            unmatched_buy_volume=round(unmatched_buy, 2),
            unmatched_sell_volume=round(unmatched_sell, 2),
            snapshot_count=len(rows),
            status=f"实时集合竞价明细 direction_raw={unmatched_direction}",
            phase=self._auction_phase(china_now()),
            indicative=True,
            confidence="较高：easy_tdx 集合竞价明细",
            note="开盘前候选因子，不能替代开盘后的分时确认",
        )

    @staticmethod
    def _auction_phase(now: datetime) -> str:
        current = now.hour * 60 + now.minute
        if 9 * 60 + 15 <= current < 9 * 60 + 25:
            return "call_auction"
        if 9 * 60 + 25 <= current < 9 * 60 + 30:
            return "preopen_result"
        if 9 * 60 + 30 <= current:
            return "opened"
        return "unavailable"

    @staticmethod
    def _auction_quality(quotes: list[Quote]) -> str:
        qualities = {
            quote.auction.data_quality
            for quote in quotes
            if quote.auction.available
        }
        if "actual" in qualities:
            return "actual"
        if "proxy" in qualities:
            return "proxy"
        return "unavailable"

    def auction_history(self, code: str, trade_date: str | None = None) -> list[dict[str, Any]]:
        normalized_code = str(code or "").strip().zfill(6)
        date = str(trade_date or china_now().strftime("%Y%m%d")).strip()
        current_date = china_now().strftime("%Y%m%d")
        if date == current_date:
            try:
                with self._mac_client() as client:
                    series = client.get_auction(_market_id_for_tdx_code(normalized_code), normalized_code)
                rows = self._auction_series_rows(series, date)
                if rows:
                    return rows
            except Exception:
                pass
        tracked = self.auction_tracker.history(normalized_code, date)
        if tracked:
            return tracked
        if len(date) == 8 and date.isdigit():
            try:
                with self._history_client() as client:
                    rows = self._auction_history_rows_from_transactions(client, normalized_code, date)
                    if rows:
                        return rows
            except Exception:
                pass
        return []

    def _auction_series_rows(self, series: Any, trade_date: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for point in _records_from_payload(series):
            price = self._object_number(point, "price")
            matched_hands = self._first_raw_number(point, "matched", "matched_volume", "auction_vol", "match_vol")
            unmatched_hands = self._object_number(point, "unmatched_volume")
            unmatched_signed = (
                self._object_number(point, "unmatched_signed_raw", signed=True)
                or self._object_number(point, "unmatched", signed=True)
            )
            if not unmatched_hands and unmatched_signed:
                unmatched_hands = abs(unmatched_signed)
            unmatched_buy = unmatched_hands * 100 if unmatched_signed > 0 else 0.0
            unmatched_sell = unmatched_hands * 100 if unmatched_signed < 0 else 0.0
            unmatched_total = unmatched_buy + unmatched_sell
            imbalance = (unmatched_buy - unmatched_sell) / unmatched_total * 100 if unmatched_total else 0.0
            rows.append(
                {
                    "trade_date": trade_date,
                    "as_of": market_time_label(self._raw_value(point, "time_label") or self._raw_value(point, "time")),
                    "price": round(price, 2),
                    "volume": round(matched_hands * 100, 2),
                    "amount": round(price * matched_hands * 100, 2),
                    "imbalance": round(imbalance, 2),
                    "unmatched_buy_volume": round(unmatched_buy, 2),
                    "unmatched_sell_volume": round(unmatched_sell, 2),
                    "unmatched_direction_raw": self._raw_value(point, "unmatched_direction_raw"),
                    "source": "easy_tdx_auction",
                    "data_quality": "actual",
                }
            )
        return rows

    def _auction_history_rows_from_transactions(
        self,
        client: Any,
        code: str,
        trade_date: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_size = 1800
        if hasattr(client, "get_history_transaction_data") or getattr(client, "trades", None) is not None:
            for page_index in range(5):
                start = page_index * page_size
                raw = self._transaction_page(
                    client,
                    code,
                    trade_date,
                    intraday=False,
                    start=start,
                    count=page_size,
                )
                batch = [self._transaction_row_from_tick(row) for row in _records_from_payload(raw)]
                rows.extend(batch)
                if len(batch) < page_size:
                    break
        auction_rows = [
            row
            for row in rows
            if str(row.get("raw_time") or row.get("time") or "")[:5] == "09:25"
            or str(row.get("time") or "")[:5] == "09:25"
        ]
        if not auction_rows:
            return []
        price = 0.0
        total_volume = 0.0
        buy_amount = 0.0
        sell_amount = 0.0
        for row in auction_rows:
            row_price = self._object_number(row, "price")
            volume = self._first_raw_number(row, "vol", "volume")
            if row_price > 0:
                price = row_price
            total_volume += volume
            amount = row_price * volume * 100 if row_price > 0 else 0.0
            side = self._transaction_side_from_row(row)
            if side == "buy":
                buy_amount += amount
            elif side == "sell":
                sell_amount += amount
        directional = buy_amount + sell_amount
        imbalance = (buy_amount - sell_amount) / directional * 100 if directional else 0.0
        if price <= 0 or total_volume <= 0:
            return []
        return [
            {
                "trade_date": trade_date,
                "as_of": "09:25",
                "price": round(price, 2),
                "volume": round(total_volume * 100, 2),
                "amount": round(price * total_volume * 100, 2),
                "imbalance": round(imbalance, 2),
                "source": "easy_tdx_history_transaction_data",
                "data_quality": "proxy",
                "status": "09:25历史逐笔成交回填；未包含未匹配委托量",
            }
        ]

    def capabilities(self) -> dict[str, Any]:
        return {
            "source": "easy_tdx / TDX L1",
            "provider": "easy_tdx",
            "quote_protocol": "TdxClient.get_security_quotes",
            "five_level_order_book": True,
            "quote_depth": True,
            "quote_depth_levels": 5,
            "ten_level_quote_depth": False,
            "transaction_tape": True,
            "transaction_data": True,
            "transaction_data_protocol": "TdxClient.get_transaction_data/get_history_transaction_data",
            "transaction_tape_protocol": "TdxClient.get_transaction_data/get_history_transaction_data",
            "transaction_tape_note": "可按需读取逐笔成交回报；这是 TDX L1 成交明细，不是委托队列。",
            "transaction_data_note": "可按需读取逐笔成交回报；这是 TDX L1 成交明细，不是委托队列。",
            "level2_available": False,
            "level2_fields": [],
            "level2_note": "easy_tdx 当前接入 TDX L1 行情、五档、成交明细和集合竞价，不返回委托队列或逐笔委托。",
            "auction_series": True,
            "auction_0925": True,
            "auction_0925_direct": False,
            "auction_0925_source": "easy_tdx history transaction 09:25 proxy when available",
            "auction_actual_fields": True,
            "auction_proxy": True,
            "auction_note": "当前日可按个股读取集合竞价明细；历史日使用 easy_tdx 历史成交明细回填09:25代理。五档指示价代理仍标记 proxy。",
            "license_note": "easy_tdx 使用边界为个人非商业研究，不能用于商业、付费或生产服务。",
        }

    def fetch_transaction_flow(
        self,
        code: str,
        trade_date: str | None = None,
        count: int | None = None,
        full_session: bool = False,
    ) -> TransactionFlowObservation:
        """Read a bounded real TDX L1 transaction tape for one stock."""
        normalized_code = str(code or "").strip().zfill(6)
        normalized_date = str(trade_date or china_now().strftime("%Y%m%d")).strip()
        if len(normalized_date) != 8 or not normalized_date.isdigit():
            return TransactionFlowObservation(
                source="easy_tdx_transaction_data",
                trade_date=normalized_date,
                note="交易日格式无效，未请求逐笔成交数据",
            )
        rows_count = max(80, min(int(count or self.settings.transaction_rows), 2000))
        cache_key = (normalized_code, normalized_date, rows_count, bool(full_session))
        now_ts = time.time()
        cached = self._transaction_cache.get(cache_key)
        if cached and now_ts - cached[0] < max(1, self.settings.transaction_cache_seconds):
            return cached[1]

        try:
            current_date = china_now().strftime("%Y%m%d")
            intraday_request = bool(
                normalized_date == current_date
                and is_trading_window()
                and self._is_known_trade_date(normalized_date)
            )
            page_size = 1800 if full_session else min(1800, max(rows_count, 600))
            ticks: list[dict[str, Any]] = []
            with self._history_client() as client:
                for page_index in range(8 if full_session else 5):
                    start = page_index * page_size
                    raw = self._transaction_page(
                        client,
                        normalized_code,
                        normalized_date,
                        intraday=intraday_request,
                        start=start,
                        count=page_size,
                    )
                    batch = _records_from_payload(raw)
                    ticks.extend(batch)
                    regular_count = sum(
                        is_regular_transaction_time(self._transaction_row_from_tick(tick).get("time"))
                        for tick in ticks
                    )
                    if (not full_session and regular_count >= rows_count) or len(batch) < page_size:
                        break
            rows = [self._transaction_row_from_tick(tick) for tick in ticks]
            rows = [row for row in rows if is_regular_transaction_time(row.get("time"))]
            rows.sort(key=lambda row: str(row.get("time") or ""))
            rows = rows if full_session else rows[-rows_count:]
            source = "easy_tdx_transaction_data" if intraday_request else "easy_tdx_history_transaction_data"
            observation = self._transaction_flow_from_rows(
                normalized_code,
                normalized_date,
                rows,
                source,
                full_session=full_session,
            )
        except Exception as exc:  # pragma: no cover - network/server dependent
            observation = TransactionFlowObservation(
                source="easy_tdx_transaction_data",
                trade_date=normalized_date,
                note=f"行情服务器未返回逐笔成交：{exc}",
            )

        self._transaction_cache[cache_key] = (now_ts, observation)
        if len(self._transaction_cache) > 128:
            oldest = min(self._transaction_cache, key=lambda key: self._transaction_cache[key][0])
            self._transaction_cache.pop(oldest, None)
        return observation

    def _is_known_trade_date(self, trade_date: str) -> bool:
        today = china_now().strftime("%Y%m%d")
        now_ts = time.time()
        if self._trade_date_lookup_cache is not None:
            cached_at, cached_today, cached_dates = self._trade_date_lookup_cache
            if cached_today == today and now_ts - cached_at < 300:
                return trade_date in cached_dates
        try:
            loader = getattr(self.close_source, "_recent_trade_dates", None)
            dates = set(loader()) if callable(loader) else set()
        except Exception:
            return False
        self._trade_date_lookup_cache = (now_ts, today, dates)
        return trade_date in dates

    def _transaction_page(
        self,
        client: Any,
        code: str,
        trade_date: str,
        *,
        intraday: bool,
        start: int,
        count: int,
    ) -> Any:
        if hasattr(client, "get_transaction_data") and hasattr(client, "get_history_transaction_data"):
            market = easy_tdx_market_for_code(code)
            method = client.get_transaction_data if intraday else client.get_history_transaction_data
            if intraday:
                return method(market, code, start=start, count=count)
            return method(market, code, int(trade_date), start=start, count=count)
        trades = getattr(client, "trades", None)
        if trades is not None:
            if intraday and hasattr(trades, "today"):
                return trades.today(full_tdx_code(code), start=start, count=count)
            if not intraday and hasattr(trades, "history"):
                return trades.history(full_tdx_code(code), trade_date, start=start, count=count)
        raise DataSourceError("easy_tdx客户端不支持逐笔成交接口。")

    def _transaction_row_from_tick(self, tick: Any) -> dict[str, Any]:
        if isinstance(tick, dict):
            row = dict(tick)
        else:
            row = _row_dict(tick)
        hour = self._raw_value(row, "hour")
        minute = self._raw_value(row, "minute")
        second = self._raw_value(row, "second", 0)
        raw_time_value = (
            self._raw_value(row, "time_label")
            or self._raw_value(row, "time")
            or self._raw_value(row, "datetime")
            or self._raw_value(row, "date_time")
        )
        time_label = market_time_label(raw_time_value)
        raw_time = time_label
        if not time_label and hour is not None and minute is not None:
            try:
                time_label = f"{int(hour):02d}:{int(minute):02d}"
                raw_time = f"{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"
            except (TypeError, ValueError):
                time_label = ""
                raw_time = ""
        normalized = {
            "time": time_label[:5],
            "price": float(self._raw_value(row, "price", 0) or 0),
            "vol": max(
                float(self._raw_value(row, "vol", self._raw_value(row, "volume", 0)) or 0),
                0,
            ),
            "status_raw": self._raw_value(row, "status_raw"),
            "order_count": self._raw_value(row, "order_count"),
        }
        buyorsell = self._raw_value(row, "buyorsell")
        nature = self._raw_value(row, "nature")
        if buyorsell is None and nature is not None:
            buyorsell = nature
            normalized["nature"] = nature
        if buyorsell is not None:
            normalized["buyorsell"] = buyorsell
        side = self._raw_value(row, "side")
        if side is not None:
            normalized["side"] = side
        if raw_time_value or raw_time:
            raw_time_text = str(raw_time_value or raw_time)
            raw_time_match = re.search(r"(\d{2}:\d{2}(?::\d{2})?)", raw_time_text)
            normalized["raw_time"] = raw_time_match.group(1) if raw_time_match else str(raw_time or time_label)
        return normalized

    @staticmethod
    def _transaction_side_from_row(row: dict[str, Any]) -> str:
        side_field = str(row.get("side") or "").lower()
        if side_field in {"buy", "sell"}:
            return side_field
        raw_direction = row.get("buyorsell")
        if raw_direction is None:
            raw_direction = row.get("nature")
        try:
            direction = int(raw_direction) if raw_direction is not None else None
        except (TypeError, ValueError):
            direction = None
        if direction == 0:
            return "buy"
        if direction == 1:
            return "sell"
        return "neutral"

    def _transaction_flow_from_rows(
        self,
        code: str,
        trade_date: str,
        rows: list[dict[str, Any]],
        source: str,
        full_session: bool = False,
    ) -> TransactionFlowObservation:
        records: list[tuple[str, float, float, str, str]] = []
        valid_times: list[str] = []
        explicit_direction_count = 0
        special_direction_count = 0
        excluded_session_count = 0
        tick_rule_count = 0
        previous_price = 0.0
        ordered_rows = [
            row
            for _, row in sorted(
                enumerate(rows),
                key=lambda item: (str(item[1].get("time") or ""), item[0]),
            )
        ]
        for row in ordered_rows:
            if not is_regular_transaction_time(row.get("time")):
                excluded_session_count += 1
                continue
            try:
                price = float(row.get("price") or 0)
                volume = float(row.get("vol") or row.get("volume") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or volume <= 0:
                continue
            side_field = str(row.get("side") or "").lower()
            raw_direction = row.get("buyorsell")
            if raw_direction is None:
                raw_direction = row.get("nature")
            direction: int | None
            try:
                direction = int(raw_direction) if raw_direction is not None else None
            except (TypeError, ValueError):
                direction = None
            if side_field in {"buy", "sell"}:
                side = side_field
                explicit_direction_count += 1
            elif side_field:
                side = "neutral"
                special_direction_count += 1
            elif direction == 0:
                side = "buy"
                explicit_direction_count += 1
            elif direction == 1:
                side = "sell"
                explicit_direction_count += 1
            elif raw_direction is not None:
                # Values such as 2/5/8 occur in special or after-hours prints.
                # They are not safely interpretable as an aggressor side.
                side = "neutral"
                special_direction_count += 1
            else:
                tick_rule_count += 1
                if previous_price <= 0:
                    side = "neutral"
                elif price > previous_price:
                    side = "buy"
                elif price < previous_price:
                    side = "sell"
                else:
                    side = "neutral"
            time_label = str(row.get("time") or "")
            raw_time = str(row.get("raw_time") or time_label or "")[:8]
            records.append((time_label[:5], price, volume, side, raw_time))
            valid_times.append(time_label)
            previous_price = price

        if not records:
            return TransactionFlowObservation(
                source=source,
                data_quality="unavailable",
                trade_date=trade_date,
                note=f"{code} 没有可用逐笔成交回报",
            )

        amounts = [price * volume * 100 for _, price, volume, _, _ in records]
        ordered_amounts = sorted(amounts)
        median_amount = ordered_amounts[len(ordered_amounts) // 2]
        threshold = max(500_000.0, median_amount * 5)
        buy_volume = sell_volume = neutral_volume = 0.0
        buy_amount = sell_amount = neutral_amount = 0.0
        large_buy_count = large_sell_count = 0
        large_buy_amount = large_sell_amount = 0.0
        for (_, price, volume, side, _), amount in zip(records, amounts):
            if side == "buy":
                buy_volume += volume
                buy_amount += amount
                if amount >= threshold:
                    large_buy_count += 1
                    large_buy_amount += amount
            elif side == "sell":
                sell_volume += volume
                sell_amount += amount
                if amount >= threshold:
                    large_sell_count += 1
                    large_sell_amount += amount
            else:
                neutral_volume += volume
                neutral_amount += amount

        directional_amount = buy_amount + sell_amount
        imbalance = (
            (buy_amount - sell_amount) / directional_amount * 100
            if directional_amount
            else 0
        )
        large_directional_amount = large_buy_amount + large_sell_amount
        large_imbalance = (
            (large_buy_amount - large_sell_amount) / large_directional_amount * 100
            if large_directional_amount
            else 0
        )
        score = int(max(-100, min(100, imbalance * 0.55 + large_imbalance * 0.45)))
        points = self._transaction_minute_points(
            [(time_label, price, volume, side) for time_label, price, volume, side, _ in records]
        )
        side_labels = {"buy": "买", "sell": "卖", "neutral": "中性"}
        recent_trades = [
            TransactionTapePrint(
                time=str(raw_time or time_label)[:8],
                price=round(price, 3),
                volume=round(volume, 2),
                amount=round(amount, 2),
                side=side,
                side_label=side_labels.get(side, "中性"),
                large=bool(amount >= threshold),
            )
            for (time_label, price, volume, side, raw_time), amount in reversed(list(zip(records, amounts)))
        ][:5]
        latest_time = valid_times[-1] if valid_times else ""
        first_time = valid_times[0] if valid_times else ""
        evidence = [
            f"逐笔成交 {len(records)} 笔（{source}）",
            f"买卖方向成交额差 {imbalance:+.1f}%",
            f"大额成交阈值 {threshold / 10000:.1f}万元，买{large_buy_count}笔/卖{large_sell_count}笔",
        ]
        if explicit_direction_count:
            evidence.append(f"TDX L1买卖方向字段 {explicit_direction_count}笔")
        if special_direction_count:
            evidence.append(f"未解释方向值 {special_direction_count}笔按中性处理")
        if excluded_session_count:
            evidence.append(f"盘前/盘后成交 {excluded_session_count}笔已排除")
        if tick_rule_count:
            evidence.append(f"方向字段缺失 {tick_rule_count}笔使用价格跳动代理")
        if large_directional_amount:
            evidence.append(f"大额成交额差 {large_imbalance:+.1f}%")
        if points:
            evidence.append(
                f"覆盖 {points[0].time}-{points[-1].time}，按分钟和3分钟滚动聚合"
            )
        return TransactionFlowObservation(
            available=True,
            source=source,
            data_quality="l1_transaction",
            trade_date=trade_date,
            first_time=first_time,
            as_of=latest_time,
            full_session=bool(full_session),
            count=len(records),
            buy_volume=round(buy_volume, 2),
            sell_volume=round(sell_volume, 2),
            neutral_volume=round(neutral_volume, 2),
            buy_amount=round(buy_amount, 2),
            sell_amount=round(sell_amount, 2),
            neutral_amount=round(neutral_amount, 2),
            imbalance_pct=round(imbalance, 2),
            large_trade_threshold_amount=round(threshold, 2),
            large_buy_count=large_buy_count,
            large_sell_count=large_sell_count,
            large_buy_amount=round(large_buy_amount, 2),
            large_sell_amount=round(large_sell_amount, 2),
            large_imbalance_pct=round(large_imbalance, 2),
            score=score,
            confidence=(
                "中等：TDX L1成交方向字段"
                if explicit_direction_count
                else "中低：逐笔价格跳动代理"
            ),
            evidence=evidence,
            recent_trades=recent_trades,
            points=points,
            note=(
                "easy_tdx TDX L1成交明细；优先使用 side/buyorsell 方向字段，特殊值按中性，"
                "字段缺失时才使用价格跳动；不是委托队列或逐笔委托"
            ),
        )

    @staticmethod
    def _transaction_minute_points(
        records: list[tuple[str, float, float, str]],
    ) -> list[TransactionFlowPoint]:
        """Aggregate prints without using future transactions at an earlier minute."""
        if not records:
            return []

        minute_rows: dict[str, dict[str, float]] = {}
        recent_amounts: deque[float] = deque(maxlen=240)
        for time_label, price, volume, side in records:
            amount = price * volume * 100
            recent_amounts.append(amount)
            ordered = sorted(recent_amounts)
            median_amount = ordered[len(ordered) // 2]
            large_threshold = max(500_000.0, median_amount * 5)
            row = minute_rows.setdefault(
                time_label,
                {
                    "count": 0.0,
                    "buy_amount": 0.0,
                    "sell_amount": 0.0,
                    "neutral_amount": 0.0,
                    "large_buy_amount": 0.0,
                    "large_sell_amount": 0.0,
                },
            )
            row["count"] += 1
            row[f"{side}_amount"] += amount
            if amount >= large_threshold and side in {"buy", "sell"}:
                row[f"large_{side}_amount"] += amount

        points: list[TransactionFlowPoint] = []
        rolling: deque[dict[str, float]] = deque(maxlen=3)
        prior_minute_amounts: deque[float] = deque(maxlen=5)
        for time_label, row in sorted(minute_rows.items()):
            directional = row["buy_amount"] + row["sell_amount"]
            imbalance = (
                (row["buy_amount"] - row["sell_amount"]) / directional * 100
                if directional
                else 0.0
            )
            large_directional = row["large_buy_amount"] + row["large_sell_amount"]
            large_imbalance = (
                (row["large_buy_amount"] - row["large_sell_amount"])
                / large_directional
                * 100
                if large_directional
                else 0.0
            )
            minute_amount = directional + row["neutral_amount"]
            baseline = (
                sum(prior_minute_amounts) / len(prior_minute_amounts)
                if prior_minute_amounts
                else minute_amount
            )
            amount_ratio = minute_amount / baseline if baseline > 0 else 1.0
            prior_minute_amounts.append(minute_amount)
            rolling.append(row)
            rolling_buy = sum(item["buy_amount"] for item in rolling)
            rolling_sell = sum(item["sell_amount"] for item in rolling)
            rolling_large_buy = sum(item["large_buy_amount"] for item in rolling)
            rolling_large_sell = sum(item["large_sell_amount"] for item in rolling)
            rolling_directional = rolling_buy + rolling_sell
            rolling_large_directional = rolling_large_buy + rolling_large_sell
            rolling_imbalance = (
                (rolling_buy - rolling_sell) / rolling_directional * 100
                if rolling_directional
                else 0.0
            )
            rolling_large_imbalance = (
                (rolling_large_buy - rolling_large_sell)
                / rolling_large_directional
                * 100
                if rolling_large_directional
                else 0.0
            )
            score = int(max(-100, min(100, imbalance * 0.6 + large_imbalance * 0.4)))
            rolling_score = int(
                max(
                    -100,
                    min(100, rolling_imbalance * 0.6 + rolling_large_imbalance * 0.4),
                )
            )
            points.append(
                TransactionFlowPoint(
                    time=time_label,
                    count=int(row["count"]),
                    buy_amount=round(row["buy_amount"], 2),
                    sell_amount=round(row["sell_amount"], 2),
                    neutral_amount=round(row["neutral_amount"], 2),
                    imbalance_pct=round(imbalance, 2),
                    large_buy_amount=round(row["large_buy_amount"], 2),
                    large_sell_amount=round(row["large_sell_amount"], 2),
                    large_imbalance_pct=round(large_imbalance, 2),
                    amount_ratio=round(amount_ratio, 2),
                    score=score,
                    rolling_count=sum(int(item["count"]) for item in rolling),
                    rolling_buy_amount=round(rolling_buy, 2),
                    rolling_sell_amount=round(rolling_sell, 2),
                    rolling_large_buy_amount=round(rolling_large_buy, 2),
                    rolling_large_sell_amount=round(rolling_large_sell, 2),
                    rolling_imbalance_pct=round(rolling_imbalance, 2),
                    rolling_large_imbalance_pct=round(rolling_large_imbalance, 2),
                    rolling_score=rolling_score,
                )
            )
        return points

    def _order_flow_from_raw(
        self,
        raw: Any,
        price: float,
        open_price: float,
        minute_amount_ratio: float,
    ) -> OrderFlowObservation:
        levels: list[OrderBookLevel] = []
        bid_depth = 0.0
        ask_depth = 0.0
        active_buy_volume = self._first_raw_number(raw, "outer_disc", "b_vol", "buy_vol", "active_buy_volume")
        active_sell_volume = self._first_raw_number(raw, "inside_dish", "s_vol", "sell_vol", "active_sell_volume")
        active_total = active_buy_volume + active_sell_volume
        active_imbalance = (
            (active_buy_volume - active_sell_volume) / active_total * 100
            if active_total
            else 0
        )
        for side in ("bid", "ask"):
            for level, quote_level in enumerate(self._quote_levels(raw, side), start=1):
                level_price = float(getattr(quote_level, "price", 0) or 0)
                level_volume = float(getattr(quote_level, "volume", 0) or 0)
                if level_price <= 0 or level_volume <= 0:
                    continue
                amount = level_price * level_volume * 100
                levels.append(
                    OrderBookLevel(
                        side=side,
                        level=level,
                        price=round(level_price, 2),
                        volume=round(level_volume, 2),
                        amount=round(amount, 2),
                    )
                )
                if side == "bid":
                    bid_depth += amount
                else:
                    ask_depth += amount

        level2_available = False
        if not levels and not active_total:
            return OrderFlowObservation(
                available=False,
                source="easy_tdx_l1_five_level",
                data_quality="unavailable",
                level2_available=level2_available,
                as_of=china_now().strftime("%H:%M:%S"),
                confidence="不可用",
                minute_amount_ratio=round(minute_amount_ratio, 2),
                evidence=["当前行情没有返回五档盘口"],
            )

        total_depth = bid_depth + ask_depth
        depth_imbalance = (bid_depth - ask_depth) / total_depth * 100 if total_depth else 0
        score = int(
            max(
                -100,
                min(
                    100,
                    depth_imbalance * 0.65
                    + active_imbalance * 0.25
                    + (minute_amount_ratio - 1) * 18,
                ),
            )
        )
        if score >= 25 and price >= open_price:
            direction = "买盘增强"
        elif score <= -25 and price <= open_price:
            direction = "卖盘增强"
        elif price >= open_price and minute_amount_ratio >= 1.25:
            direction = "放量承接"
        elif price <= open_price and minute_amount_ratio >= 1.25:
            direction = "放量抛压"
        else:
            direction = "多空拉锯"
        evidence = [
            f"五档买卖深度差 {depth_imbalance:+.1f}%",
            f"当前分钟量能 {minute_amount_ratio:.1f}倍",
        ]
        if active_total:
            evidence.append(f"主动成交量差 {active_imbalance:+.1f}%（TDX L1汇总字段）")
        if price >= open_price:
            evidence.append("价格位于开盘价上方")
        else:
            evidence.append("价格位于开盘价下方")
        return OrderFlowObservation(
            available=True,
            source="easy_tdx_l1_five_level",
            data_quality="l1_five_level",
            level2_available=level2_available,
            as_of=china_now().strftime("%H:%M:%S"),
            direction=direction,
            score=score,
            confidence="中等：五档快照/主动成交量与分钟成交额代理",
            bid_depth_amount=round(bid_depth, 2),
            ask_depth_amount=round(ask_depth, 2),
            imbalance_pct=round(depth_imbalance, 2),
            active_buy_volume=round(active_buy_volume, 2),
            active_sell_volume=round(active_sell_volume, 2),
            active_imbalance_pct=round(active_imbalance, 2),
            minute_amount_ratio=round(minute_amount_ratio, 2),
            evidence=evidence,
            levels=levels,
            disclaimer="成交额/五档/主动成交量代理，不是委托队列或逐笔委托",
        )

    @staticmethod
    def _raw_value(raw: Any, key: str, default: Any = None) -> Any:
        if isinstance(raw, dict):
            return raw.get(key, default)
        return getattr(raw, key, default)

    @classmethod
    def _object_number(cls, raw: Any, key: str, *, signed: bool = False) -> float:
        value = cls._raw_value(raw, key)
        try:
            number = float(value if value is not None else 0)
        except (TypeError, ValueError):
            return 0.0
        if number != number:
            return 0.0
        if signed:
            return number
        return number if number > 0 else 0.0

    @classmethod
    def _raw_number(cls, raw: Any, key: str) -> float:
        return cls._object_number(raw, key)

    @classmethod
    def _first_raw_number(cls, raw: Any, *keys: str) -> float:
        for key in keys:
            value = cls._raw_number(raw, key)
            if value > 0:
                return value
        return 0.0

    def _quote_levels(self, raw: Any, side: str) -> list[Any]:
        attr = "buy_levels" if side == "bid" else "sell_levels"
        levels = list(self._raw_value(raw, attr, ()) or ())
        if levels:
            return levels[:5]

        output: list[Any] = []
        volume_prefix = "bid" if side == "bid" else "ask"

        @dataclass
        class RawLevel:
            price: float
            volume: float

        for level in range(1, 6):
            price = self._raw_number(raw, f"{side}{level}")
            volume = self._first_raw_number(
                raw,
                f"{volume_prefix}_vol{level}",
                f"{volume_prefix}{level}_volume",
                f"{volume_prefix}{level}_vol",
                f"{volume_prefix}_volume{level}",
            )
            if volume > 0:
                output.append(RawLevel(price=price, volume=volume))
        return output

    def _fetch_indices(self, api: Any, seed_indices: list[IndexSnapshot]) -> list[IndexSnapshot]:
        request_codes = ["000001", "399001", "399006"]
        if not hasattr(api, "get_security_quotes"):
            return seed_indices
        result = api.get_security_quotes(
            [(easy_tdx_market_for_code(code, index=True), code) for code in request_codes]
        )
        raw_indices = _records_from_payload(result)
        by_code = {index.code: index for index in seed_indices}
        output: list[IndexSnapshot] = []
        for raw in raw_indices:
            code = str(self._raw_value(raw, "code") or "")
            seed = by_code.get(code) or self._default_index_seed(code, raw)
            if not seed:
                continue
            prev_close = self._first_raw_number(raw, "pre_close", "pre_close_price", "last_close", "last_close_price") or seed.prev_close
            price = self._first_raw_number(raw, "price", "last_price") or seed.price
            open_price = self._first_raw_number(raw, "open", "open_price") or seed.open
            low = self._first_raw_number(raw, "low", "low_price") or seed.low
            high = self._first_raw_number(raw, "high", "high_price") or seed.high
            price_factor = self._index_price_factor(raw, seed, prev_close, price)
            if price_factor != 1.0:
                prev_close *= price_factor
                price *= price_factor
                open_price *= price_factor
                low *= price_factor
                high *= price_factor
            current_volume = self._first_raw_number(raw, "cur_vol", "current_hand")
            current_amount = current_volume * price * 100 if current_volume else 0
            cumulative_amount = self._first_raw_number(raw, "amount") or seed.amount
            output.append(
                IndexSnapshot(
                    code=code,
                    name=seed.name,
                    price=round(price, 2),
                    prev_close=round(prev_close, 2),
                    open=round(open_price, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    change_pct=calc_change_pct(price, prev_close),
                    rebound_from_low_pct=round((price - low) / low * 100, 2) if low else 0,
                    minute_amount_ratio=self._minute_amount_ratio(
                        minute_amount=current_amount,
                        cumulative_amount=cumulative_amount,
                        now=china_now(),
                    ),
                    amount=cumulative_amount,
                )
            )
        return output or seed_indices

    def _default_index_seed(self, code: str, raw: Any) -> IndexSnapshot | None:
        name = INDEX_NAMES.get(str(code or "").zfill(6))
        if not name:
            return None
        raw_prev_close = self._first_raw_number(raw, "pre_close", "pre_close_price", "last_close", "last_close_price")
        raw_price = self._first_raw_number(raw, "price", "last_price")
        reference = raw_prev_close or raw_price
        if reference <= 0:
            return None
        factor = self._fallback_index_price_factor(str(code).zfill(6), reference)
        prev_close = (raw_prev_close or raw_price) * factor
        price = (raw_price or raw_prev_close) * factor
        open_price = (self._first_raw_number(raw, "open", "open_price") or raw_prev_close or raw_price) * factor
        raw_high = self._first_raw_number(raw, "high", "high_price")
        raw_low = self._first_raw_number(raw, "low", "low_price")
        high = raw_high * factor if raw_high else max(price, open_price, prev_close)
        low = raw_low * factor if raw_low else min(price, open_price, prev_close)
        return IndexSnapshot(
            code=str(code).zfill(6),
            name=name,
            price=round(price, 2),
            prev_close=round(prev_close, 2),
            open=round(open_price, 2),
            high=round(high, 2),
            low=round(low, 2),
            change_pct=calc_change_pct(price, prev_close),
            rebound_from_low_pct=round((price - low) / low * 100, 2) if low else 0,
            minute_amount_ratio=1.0,
            amount=self._first_raw_number(raw, "amount"),
        )

    @staticmethod
    def _fallback_index_price_factor(code: str, reference: float) -> float:
        # Some TDX hosts return Shanghai Composite quotes divided by 10 when
        # daily seed data is unavailable. Keep the heuristic code-scoped.
        if code == "000001" and 100 <= reference < 1000:
            return 10.0
        return 1.0

    def _index_price_factor(
        self,
        raw: Any,
        seed: IndexSnapshot,
        raw_prev_close: float,
        raw_price: float,
    ) -> float:
        """Correct provider-specific index price scaling.

        Some easy_tdx hosts return Shanghai Composite quotes with
        decimal_point=3 and price/pre_close divided by 10, while Shenzhen
        indices are already in normal point units.  The daily seed close is the
        safest intraday anchor because it is known before the current tick and
        should match the quote's pre_close after scaling.
        """
        seed_prev = float(seed.price or seed.prev_close or 0)
        anchor = raw_prev_close if raw_prev_close > 0 else raw_price
        if seed_prev <= 0 or anchor <= 0:
            return 1.0
        ratio = seed_prev / anchor
        if 8 <= ratio <= 12:
            return 10.0
        if 80 <= ratio <= 120:
            return 100.0
        if 0.08 <= ratio <= 0.12:
            return 0.1

        decimal_point = int(self._first_raw_number(raw, "decimal_point") or 0)
        if seed.code == "000001" and decimal_point >= 3 and raw_price < 1000 <= seed_prev:
            return 10.0
        return 1.0

    def _limit_threshold(self, meta: StockMeta) -> float:
        code = meta.code
        if code.startswith(("300", "301", "688", "92")) or meta.market in {"创业板", "科创板", "北交所"}:
            if code.startswith("92") or meta.market == "北交所":
                return 30.0
            return 20.0
        return 10.0


class EasyTdxMinuteReplaySource:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.hosts = [
            "119.147.212.81:7709",
            "47.103.48.45:7709",
            "106.14.95.149:7709",
            "115.238.56.198:7709",
        ]

    def _client(self) -> Any:
        try:
            from easy_tdx import TdxClient
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise DataSourceError("easy_tdx未安装，无法拉取分钟回放。") from exc
        return TdxClient(timeout=min(1.5, self.settings.easy_tdx_timeout_seconds))

    def fetch(self, code: str, trade_date: str, live: bool = False) -> list[dict[str, Any]]:
        return self._fetch_with_code(str(code or "").strip().zfill(6), trade_date, live=live)

    def fetch_index(self, code: str, trade_date: str, live: bool = False) -> list[dict[str, Any]]:
        return self._fetch_with_code(str(code or "").strip().zfill(6), trade_date, live=live, index=True)

    def _fetch_with_code(self, code: str, trade_date: str, live: bool = False, index: bool = False) -> list[dict[str, Any]]:
        with self._client() as client:
            rows: list[dict[str, Any]] = []
            if live and is_trading_window():
                raw = self._minute_page(client, code, trade_date, live=True, index=index)
                live_rows = self._normalize_minute_series(raw)
                if self._minute_rows_look_sane(live_rows):
                    rows = live_rows
            if not rows:
                raw = self._minute_page(client, code, trade_date, live=False, index=index)
                rows = self._normalize_minute_series(raw)
            return rows if self._minute_rows_look_sane(rows) else []

    def _minute_page(self, client: Any, code: str, trade_date: str, *, live: bool, index: bool) -> Any:
        if hasattr(client, "get_minute_time_data") and hasattr(client, "get_history_minute_time_data"):
            market = easy_tdx_market_for_code(code, index=index)
            if live:
                return client.get_minute_time_data(market, code)
            return client.get_history_minute_time_data(market, code, int(trade_date))
        minutes = getattr(client, "minutes", None)
        if minutes is not None:
            if live and hasattr(minutes, "today"):
                return minutes.today(full_tdx_code(code, index=index))
            if not live and hasattr(minutes, "history"):
                return minutes.history(full_tdx_code(code, index=index), trade_date)
        raise DataSourceError("easy_tdx客户端不支持分钟分时接口。")

    def _normalize_minute_series(self, raw: Any) -> list[dict[str, Any]]:
        return [self._normalize_minute_row(row) for row in _records_from_payload(raw) if row is not None]

    def _normalize_minute_row(self, row: Any) -> dict[str, Any]:
        price = float(self._raw_value(row, "price") or 0)
        volume = max(float(self._raw_value(row, "vol", self._raw_value(row, "volume")) or 0), 0)
        amount = max(float(self._raw_value(row, "amount", self._raw_value(row, "open_interest")) or 0), 0)
        normalized = {
            "price": price,
            "vol": volume,
            "amount": amount,
        }
        time_label = market_time_label(
            self._raw_value(row, "time_label")
            or self._raw_value(row, "time")
            or self._raw_value(row, "datetime")
        )
        if time_label:
            normalized["time"] = time_label
        return normalized

    def _minute_rows_look_sane(self, rows: list[dict[str, Any]]) -> bool:
        if not rows:
            return False
        prices = [float(row.get("price") or 0) for row in rows]
        positive_prices = [price for price in prices if price > 0]
        if len(positive_prices) < max(3, int(len(rows) * 0.85)):
            return False

        ordered = sorted(positive_prices)
        median = ordered[len(ordered) // 2]
        if median <= 0:
            return False

        min_price = min(positive_prices)
        max_price = max(positive_prices)
        if min_price <= 0:
            return False

        # TDX live minute data can occasionally return decoded deltas instead of prices
        # (0.x, thousands, or negative-like drift). Intraday A-share price ranges should
        # stay compact, so a wide spread means this live response is not usable.
        return max_price / min_price <= 2.5 and min_price >= median * 0.4 and max_price <= median * 2.5

    @staticmethod
    def _raw_value(raw: Any, key: str, default: Any = None) -> Any:
        if isinstance(raw, dict):
            return raw.get(key, default)
        return getattr(raw, key, default)


class EasyTdxF10DataSource:
    """On-demand easy_tdx F10/finance reader for stock detail pages."""

    SOURCE = "easy_tdx_f10_7615"
    SECTION_SPECS: tuple[tuple[str, str, Any], ...] = (
        ("finance_info", "最新财务信息", "_finance_info_response"),
        ("company_info_directory", "公司资料目录", "_company_directory_response"),
        ("company_info_text", "F10文本预览", "_company_text_response"),
        ("financial_file_list", "专业财报文件", "_financial_file_list_response"),
        ("financial_record_latest", "最新财报原始记录", "_financial_record_response"),
    )
    GLOBAL_FIELD_LABELS = {
        "rq": "报告期",
        "DATE": "日期",
        "num": "页内数量",
        "total_num": "总数",
        "ans": "请求ID",
        "date": "日期",
        "time": "时间",
        "pm": "排名",
        "prepm": "排名变化",
        "zqdm": "代码",
        "zqjc": "名称",
        "sc": "市场",
        "modtime": "更新时间",
        "issue_date": "发布日期",
        "title": "标题",
        "tableid": "表ID",
        "rec_id": "记录ID",
        "typecode": "类型代码",
        "typename": "类型",
        "url": "链接",
        "redistime": "入库时间",
        "source": "来源",
        "relatecolumn": "相关栏目",
        "start_date": "开始日期",
        "start_time": "开始时间",
        "end_time": "结束时间",
        "roadshow_type": "路演类型",
        "summary": "摘要",
        "PETTM": "PE TTM",
        "PEBFW": "PE分位",
        "PBMRQ": "PB MRQ",
        "PBBFW": "PB分位",
        "PCFOCFTTM": "PCF TTM",
        "PCFBFW": "PCF分位",
        "PSTTM": "PS TTM",
        "PSBFW": "PS分位",
        "PEG": "PEG",
        "AVGMVM": "平均市值",
        "ALIQMV": "流通市值",
        "zdf": "涨跌幅",
        "zdf_3d": "3日涨跌",
        "zdf_5d": "5日涨跌",
        "zdf_20d": "20日涨跌",
        "zdf_60d": "60日涨跌",
        "zdf_ys": "月涨跌",
        "tjdate": "统计日期",
        "nyear": "年份",
        "flag": "标记",
    }
    SECTION_FIELD_LABELS = {
        "stock_info": {"T003": "名称", "T002": "代码", "sc": "市场"},
        "company_profile": {
            "T035": "证券类型",
            "T031": "上市日期",
            "T051": "发行方式",
            "ssbk": "上市板块",
            "fxzd": "发行制度",
            "mgmz": "每股面值",
        },
        "business_composition": {
            "N000": "分类",
            "N001": "序号",
            "N002": "项目",
            "N003": "营业收入",
            "N004": "收入占比",
            "N005": "营业成本",
            "N006": "成本占比",
            "N007": "毛利",
            "N008": "毛利占比",
            "N009": "毛利率",
        },
        "shareholder_change_plans": {
            "N001": "公告日",
            "N002": "方向",
            "N003": "股东",
            "N004": "身份",
            "N005": "计划股数",
            "N006": "占总股本",
            "N009": "起始日",
            "N010": "截止日",
        },
        "dividend_financing": {
            "rq": "报告期",
            "T003": "公告日",
            "T004": "方案",
            "T006": "股息率",
            "T021": "股权登记日",
            "T023": "除权除息日",
            "T036": "进度",
            "glzfl": "分红率",
        },
        "allotment_dates": {"rq": "获配日期"},
        "finance_report": {"rq": "报告期", "rtype": "报表类型", "nhytype": "行业类型", "zqname": "证券名称"},
        "stock_score": {
            "N001": "综合评分",
            "N002": "市场排名",
            "N003": "行业排名",
            "N004": "样本数",
            "N006": "超过比例",
            "N007": "评分日期",
            "N008": "评分",
            "N009": "上期评分",
        },
        "theme_market": {
            "N001": "序号",
            "N002": "板块代码",
            "N003": "板块名称",
            "N004": "涨跌幅",
            "N005": "成分数量",
        },
        "ranking_detail": {"pm": "排名", "prepm": "排名变化", "zqdm": "代码", "zqjc": "名称"},
        "governance": {
            "T003": "标题",
            "T004": "处理结果",
            "T006": "事由",
            "T007": "内容",
            "T008": "日期",
            "T009": "类型",
            "rec_id": "记录ID",
        },
        "hot_topics": {
            "bflag": "标记",
            "ztrq": "题材日期",
            "ztmc": "题材名",
            "gld": "关联度",
            "rxsj": "入选日期",
            "ztnr": "题材内容",
            "arec": "记录",
            "id": "ID",
            "sslb": "类别",
        },
        "company_news": {
            "T004": "评级",
            "T009": "作者",
            "T012": "日期",
            "T011": "机构",
            "T039": "标题",
            "nflag": "标记",
            "ybdz": "研报地址",
            "zs": "总数",
        },
        "northbound_holding": {
            "N001": "日期",
            "N002": "持股占比",
            "N003": "持股数量",
            "N004": "变动股数",
            "N005": "变动比例",
            "N006": "收盘价",
        },
    }

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[float, FundamentalPayload]] = {}

    def _client(self) -> Any:
        try:
            from easy_tdx import TdxClient
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise DataSourceError("easy_tdx不可用，无法读取7615基本面。") from exc
        return TdxClient(timeout=max(1.0, float(self.settings.easy_tdx_f10_timeout_seconds)))

    def _finance_info_response(self, client: Any, code: str) -> Any:
        rows = _records_from_payload(client.get_finance_info(easy_tdx_market_for_code(code), code))
        return self._response_from_rows("finance_info", rows, "TdxClient.get_finance_info")

    def _company_directory_response(self, client: Any, code: str) -> Any:
        rows = _records_from_payload(client.get_company_info_category(easy_tdx_market_for_code(code), code))
        return self._response_from_rows("company_info_category", rows, "TdxClient.get_company_info_category")

    def _company_text_response(self, client: Any, code: str) -> Any:
        market = easy_tdx_market_for_code(code)
        categories = _records_from_payload(client.get_company_info_category(market, code))
        rows: list[dict[str, Any]] = []
        for category in categories[:5]:
            filename = str(category.get("filename") or "").strip()
            if not filename:
                continue
            start = int(float(category.get("start") or 0))
            length = int(float(category.get("length") or 0))
            read_length = max(0, min(length, 24000))
            text = ""
            if read_length > 0:
                text = client.get_company_info_content(market, code, filename, start, read_length)
            rows.append(
                {
                    "name": category.get("name") or filename,
                    "filename": filename,
                    "start": start,
                    "length": length,
                    "preview": self._compact_value(text, max_text=700),
                    "content": self._compact_value(text, max_text=24000),
                    "truncated": bool(length > read_length),
                }
            )
        return self._response_from_rows(
            "company_info_text",
            rows,
            "TdxClient.get_company_info_content",
            max_text=24000,
        )

    def _financial_file_list_response(self, client: Any, code: str) -> Any:
        rows = _records_from_payload(client.get_financial_file_list())
        return self._response_from_rows(
            "financial_file_list",
            rows[:12],
            "TdxClient.get_financial_file_list",
            row_count=len(rows),
        )

    def _financial_record_response(self, client: Any, code: str) -> Any:
        files = _records_from_payload(client.get_financial_file_list())
        filename = str((files[0] if files else {}).get("filename") or "").strip()
        if not filename:
            return self._response_from_rows("financial_record_latest", [], "TdxClient.get_financial_records")
        remote_filename = filename if filename.startswith("tdxfin/") else f"tdxfin/{filename}"
        records = _records_from_payload(client.get_financial_records(remote_filename))
        filtered = [
            self._financial_record_row(row, filename)
            for row in records
            if str(row.get("code") or "").zfill(6) == code
        ]
        return self._response_from_rows(
            "financial_record_latest",
            filtered[:3],
            f"TdxClient.get_financial_records({filename})",
            row_count=len(filtered),
        )

    def _financial_record_row(self, row: dict[str, Any], filename: str) -> dict[str, Any]:
        output = {
            "filename": filename,
            "code": str(row.get("code") or ""),
            "market": jsonable_market_value(row.get("market")),
            "report_date": jsonable_market_value(row.get("report_date")),
        }
        fields = row.get("fields")
        if isinstance(fields, (list, tuple)):
            for index, value in enumerate(fields[:24], start=1):
                output[f"F{index:03d}"] = jsonable_market_value(value)
            output["field_count"] = len(fields)
        else:
            output["fields"] = jsonable_market_value(fields, max_text=800)
        return output

    def _response_from_rows(
        self,
        key: str,
        rows: list[dict[str, Any]],
        entry: str,
        *,
        row_count: int | None = None,
        max_text: int = 1000,
    ) -> Any:
        clean_rows = [
            {
                str(column): jsonable_market_value(value, max_text=max_text)
                for column, value in dict(row).items()
            }
            for row in rows
        ]
        columns = list(clean_rows[0].keys()) if clean_rows else []
        tables = [
            SimpleNamespace(
                key=key,
                columns=columns,
                rows=clean_rows,
                count=row_count if row_count is not None else len(clean_rows),
            )
        ] if clean_rows else []
        return SimpleNamespace(ok=True, entry=entry, tables=tables, error_code=None)

    def fetch(self, code: str) -> FundamentalPayload:
        normalized_code = str(code or "").strip().zfill(6)
        if len(normalized_code) != 6 or not normalized_code.isdigit():
            return FundamentalPayload(
                code=normalized_code,
                expected_section_count=len(self.SECTION_SPECS),
                note="股票代码格式无效，未请求 easy_tdx F10/财务数据",
            )

        now_ts = time.time()
        ttl = max(0, int(self.settings.fundamentals_cache_seconds))
        cached = self._cache.get(normalized_code)
        if ttl and cached and now_ts - cached[0] <= ttl:
            return cached[1].model_copy(deep=True)

        try:
            client = self._client()
        except DataSourceError as exc:
            return FundamentalPayload(
                code=normalized_code,
                expected_section_count=len(self.SECTION_SPECS),
                note=str(exc),
            )

        try:
            with client:
                sections = [
                    self._fetch_section(client, normalized_code, key, title, getattr(self, method_name))
                    for key, title, method_name in self.SECTION_SPECS
                ]
        except Exception as exc:  # pragma: no cover - network/server dependent
            return FundamentalPayload(
                code=normalized_code,
                expected_section_count=len(self.SECTION_SPECS),
                note=f"easy_tdx F10/财务连接失败：{self._compact_value(exc, max_text=180)}",
            )
        available_count = sum(1 for section in sections if section.available)
        payload = FundamentalPayload(
            available=available_count > 0,
            source=self.SOURCE,
            code=normalized_code,
            fetched_at=china_now().isoformat(timespec="seconds"),
            section_count=available_count,
            expected_section_count=len(self.SECTION_SPECS),
            sections=sections,
        )
        self._cache[normalized_code] = (now_ts, payload)
        if len(self._cache) > 128:
            oldest = min(self._cache, key=lambda key: self._cache[key][0])
            self._cache.pop(oldest, None)
        return payload.model_copy(deep=True)

    def _fetch_section(self, client: Any, code: str, key: str, title: str, caller: Any) -> FundamentalSection:
        try:
            response = caller(client, code)
            row_count = sum(int(getattr(table, "count", 0) or 0) for table in getattr(response, "tables", ()) or ())
            ok = bool(getattr(response, "ok", False))
            status = "ok" if ok and row_count else "empty" if ok else f"error_code={getattr(response, 'error_code', None)}"
            tables = self._tables_from_response(key, response)
            fields, field_count = self._fields_from_response(key, response)
            return FundamentalSection(
                key=key,
                title=title,
                available=ok and row_count > 0,
                status=status,
                entry=str(getattr(response, "entry", "") or ""),
                field_count=field_count,
                row_count=row_count,
                fields=fields,
                tables=tables,
            )
        except Exception as exc:  # pragma: no cover - server-specific failures are section-local
            return FundamentalSection(
                key=key,
                title=title,
                available=False,
                status="error",
                error=self._compact_value(exc, max_text=180) or "F10调用失败",
            )

    def _fields_from_response(self, section_key: str, response: Any) -> tuple[list[FundamentalField], int]:
        for table in getattr(response, "tables", ()) or ():
            rows = list(getattr(table, "rows", ()) or ())
            if not rows:
                continue
            first_row = dict(rows[0])
            raw_columns = self._raw_columns(table, first_row)[:10]
            display_columns = self._display_columns(section_key, raw_columns)
            fields = [
                FundamentalField(
                    label=display_columns[index],
                    value=self._compact_value(
                        first_row.get(raw_key),
                        max_text=self._value_max_text(section_key, raw_key, default=220),
                    ),
                    raw_key=raw_key,
                )
                for index, raw_key in enumerate(raw_columns)
            ]
            return fields, len(first_row)
        return [], 0

    def _tables_from_response(self, section_key: str, response: Any) -> list[FundamentalTable]:
        tables: list[FundamentalTable] = []
        for index, table in enumerate((getattr(response, "tables", ()) or ())[:3]):
            rows = list(getattr(table, "rows", ()) or ())
            sample_row = dict(rows[0]) if rows else {}
            raw_columns = self._raw_columns(table, sample_row)[:12]
            display_columns = self._display_columns(section_key, raw_columns)
            display_rows: list[dict[str, Any]] = []
            for row in rows[:10]:
                raw_row = dict(row)
                display_rows.append(
                    {
                        display_columns[column_index]: self._compact_value(
                            raw_row.get(raw_key),
                            max_text=self._value_max_text(section_key, raw_key),
                        )
                        for column_index, raw_key in enumerate(raw_columns)
                    }
                )
            table_title = self._table_title(section_key, index, getattr(table, "key", None))
            tables.append(
                FundamentalTable(
                    title=table_title,
                    columns=display_columns,
                    raw_columns=raw_columns,
                    rows=display_rows,
                    row_count=int(getattr(table, "count", 0) or 0),
                )
            )
        return tables

    def _raw_columns(self, table: Any, sample_row: dict[str, Any]) -> list[str]:
        columns = [str(column) for column in getattr(table, "columns", ()) or ()]
        if not columns:
            return list(sample_row)
        raw_keys: list[str] = []
        seen: dict[str, int] = {}
        for column in columns:
            count = seen.get(column, 0)
            seen[column] = count + 1
            raw_keys.append(column if count == 0 else f"{column}__{count + 1}")
        return raw_keys

    def _field_label(self, section_key: str, raw_key: str) -> str:
        base_key = str(raw_key).split("__", 1)[0]
        section_labels = self.SECTION_FIELD_LABELS.get(section_key, {})
        return section_labels.get(base_key) or self.GLOBAL_FIELD_LABELS.get(base_key) or base_key

    def _display_columns(self, section_key: str, raw_columns: list[str]) -> list[str]:
        labels: list[str] = []
        seen: dict[str, int] = {}
        for raw_key in raw_columns:
            base_label = self._field_label(section_key, raw_key)
            count = seen.get(base_label, 0)
            seen[base_label] = count + 1
            labels.append(base_label if count == 0 else f"{base_label}#{count + 1}")
        return labels

    def _table_title(self, section_key: str, index: int, table_key: Any) -> str:
        if section_key == "business_composition":
            return ["按行业", "按产品", "按地区"][index] if index < 3 else f"表{index + 1}"
        if section_key == "valuation" and index == 1:
            return "估值明细"
        if section_key == "profit_forecast":
            return ["预测年份", "预测统计", "评级分布", "机构预测", "历史预测"][index] if index < 5 else f"表{index + 1}"
        if section_key == "ranking_detail":
            return "个股排名" if index == 0 else "排名列表"
        if section_key == "topic_compare_first":
            return "题材成分对比" if index == 0 else "当前个股"
        return str(table_key or f"表{index + 1}")

    @staticmethod
    def _value_max_text(section_key: str, raw_key: str, default: int = 160) -> int:
        base_key = str(raw_key).split("__", 1)[0]
        if section_key == "company_info_text" and base_key == "content":
            return 24000
        if section_key == "company_info_text" and base_key == "preview":
            return 700
        return default

    @staticmethod
    def _compact_value(value: Any, max_text: int = 160) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value != value:
                return None
            return round(value, 4)
        text = str(value).strip()
        if not text:
            return None
        if len(text) > max_text:
            return f"{text[:max_text - 3]}..."
        return text


class EasyTdxDetailDataSource:
    CAPITAL_FLOW_SOURCE = "easy_tdx_mac_capital_flow"
    INDICATORS_SOURCE = "easy_tdx_mac_indicators"
    CHANLUN_SOURCE = "easy_tdx_chanlun"
    INDICATORS = ["MACD", "KDJ", "RSI", "BOLL", "OBV", "ATR"]

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._cache: dict[tuple[str, str], tuple[float, DetailDataPayload]] = {}
        self._daily_kline_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}

    def _mac_client(self) -> Any:
        try:
            from easy_tdx import MacClient
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise DataSourceError("easy_tdx未安装，无法读取详情扩展数据。") from exc
        return MacClient(timeout=max(1.0, float(self.settings.easy_tdx_f10_timeout_seconds)))

    def fetch_capital_flow(self, code: str) -> DetailDataPayload:
        return self._cached("capital_flow", code, self._fetch_capital_flow)

    def fetch_technical_indicators(self, code: str) -> DetailDataPayload:
        return self._cached("technical_indicators", code, self._fetch_technical_indicators)

    def fetch_chanlun(self, code: str) -> DetailDataPayload:
        return self._cached("chanlun", code, self._fetch_chanlun)

    def fetch_daily_kline_rows(self, code: str, count: int = 180) -> list[dict[str, Any]]:
        normalized_code = str(code or "").strip().zfill(6)
        if len(normalized_code) != 6 or not normalized_code.isdigit():
            return []
        row_count = max(120, min(int(count or 180), 400))
        now_ts = time.time()
        ttl = max(0, int(self.settings.fundamentals_cache_seconds))
        key = (normalized_code, row_count)
        cached = self._daily_kline_cache.get(key)
        if ttl and cached and now_ts - cached[0] <= ttl:
            return [dict(row) for row in cached[1]]
        try:
            rows = self._fetch_daily_kline_rows(normalized_code, row_count)
        except Exception:
            rows = []
        self._daily_kline_cache[key] = (now_ts, [dict(row) for row in rows])
        if len(self._daily_kline_cache) > 128:
            oldest = min(self._daily_kline_cache, key=lambda item: self._daily_kline_cache[item][0])
            self._daily_kline_cache.pop(oldest, None)
        return [dict(row) for row in rows]

    def _cached(self, kind: str, code: str, loader: Any) -> DetailDataPayload:
        normalized_code = str(code or "").strip().zfill(6)
        if len(normalized_code) != 6 or not normalized_code.isdigit():
            return DetailDataPayload(
                source=kind,
                code=normalized_code,
                note="股票代码格式无效，未请求详情扩展数据",
            )
        now_ts = time.time()
        ttl = max(0, int(self.settings.fundamentals_cache_seconds))
        key = (kind, normalized_code)
        cached = self._cache.get(key)
        if ttl and cached and now_ts - cached[0] <= ttl:
            return cached[1].model_copy(deep=True)
        try:
            payload = loader(normalized_code)
        except Exception as exc:  # pragma: no cover - network/server dependent
            payload = DetailDataPayload(
                source=kind,
                code=normalized_code,
                note=f"easy_tdx详情扩展数据不可用：{jsonable_market_value(exc)}",
            )
        self._cache[key] = (now_ts, payload)
        if len(self._cache) > 256:
            oldest = min(self._cache, key=lambda item: self._cache[item][0])
            self._cache.pop(oldest, None)
        return payload.model_copy(deep=True)

    def _fetch_capital_flow(self, code: str) -> DetailDataPayload:
        with self._mac_client() as client:
            rows = self._clean_rows(
                _records_from_payload(client.get_capital_flow(_market_id_for_tdx_code(code), code)),
                max_rows=80,
            )
        latest = rows[-1] if rows else {}
        tables = [self._table("资金流历史", rows, row_count=len(rows))] if rows else []
        return DetailDataPayload(
            available=bool(rows),
            source=self.CAPITAL_FLOW_SOURCE,
            code=code,
            fetched_at=china_now().isoformat(timespec="seconds"),
            summary={
                "latest_date": latest.get("date"),
                "main_net": latest.get("main_net"),
                "large_net": latest.get("large_net"),
                "mid_net": latest.get("mid_net"),
                "small_net": latest.get("small_net"),
            },
            tables=tables,
            note="easy_tdx MacClient.get_capital_flow 原始资金流字段；仅按个股详情页按需读取。",
        )

    def _fetch_technical_indicators(self, code: str) -> DetailDataPayload:
        from easy_tdx import Adjust, Period

        with self._mac_client() as client:
            rows = self._clean_rows(
                _records_from_payload(
                    client.get_stock_kline_with_indicators(
                        _market_id_for_tdx_code(code),
                        code,
                        self.INDICATORS,
                        period=Period.DAILY,
                        count=120,
                        adjust=Adjust.QFQ,
                    )
                ),
                max_rows=120,
            )
        latest = rows[-1] if rows else {}
        tables = [self._table("日线技术指标", rows, row_count=len(rows))] if rows else []
        return DetailDataPayload(
            available=bool(rows),
            source=self.INDICATORS_SOURCE,
            code=code,
            fetched_at=china_now().isoformat(timespec="seconds"),
            summary={
                "latest_datetime": latest.get("datetime") or latest.get("date"),
                "close": latest.get("close"),
                "MACD_DIF": latest.get("MACD_DIF"),
                "MACD_DEA": latest.get("MACD_DEA"),
                "MACD_HIST": latest.get("MACD_HIST"),
                "RSI": latest.get("RSI"),
                "BOLL_UPPER": latest.get("BOLL_UPPER"),
                "BOLL_MID": latest.get("BOLL_MID"),
                "BOLL_LOWER": latest.get("BOLL_LOWER"),
            },
            tables=tables,
            note="MACD/KDJ/RSI/BOLL/OBV/ATR 基于 easy_tdx 日线K线计算；仅作研究展示。",
        )

    def _fetch_daily_kline_rows(self, code: str, count: int) -> list[dict[str, Any]]:
        from easy_tdx import Adjust, Period

        with self._mac_client() as client:
            raw_rows = _records_from_payload(
                client.get_stock_kline(
                    _market_id_for_tdx_code(code),
                    code,
                    period=Period.DAILY,
                    count=count,
                    adjust=Adjust.QFQ,
                )
            )
        rows = [self._daily_kline_row(row) for row in raw_rows]
        rows = [row for row in rows if row.get("date") and row.get("close", 0) > 0]
        rows.sort(key=lambda row: str(row.get("date") or ""))
        return rows[-count:]

    def _fetch_chanlun(self, code: str) -> DetailDataPayload:
        from easy_tdx import Adjust, Period
        from easy_tdx.chanlun.analyser import ChanlunAnalyser

        market_id = _market_id_for_tdx_code(code)
        prefix = "SH" if market_id == 1 else "BJ" if market_id == 2 else "SZ"
        with self._mac_client() as client:
            frame = client.get_stock_kline(
                market_id,
                code,
                period=Period.DAILY,
                count=800,
                adjust=Adjust.QFQ,
            )
        if getattr(frame, "empty", True):
            return DetailDataPayload(
                source=self.CHANLUN_SOURCE,
                code=code,
                fetched_at=china_now().isoformat(timespec="seconds"),
                note="easy_tdx 未返回可用于缠论分析的日线K线。",
            )
        kline_rows = [self._daily_kline_row(row) for row in _records_from_payload(frame)]
        kline_rows = [row for row in kline_rows if row.get("date") and row.get("close", 0) > 0]
        kline_latest = max(kline_rows, key=lambda row: str(row.get("date") or ""), default={})
        result = ChanlunAnalyser(f"{prefix}{code}", "DAILY").process_klines(frame)
        result_dict = result.to_dict() if hasattr(result, "to_dict") else {}
        table_specs = [
            ("笔", "bis"),
            ("中枢", "zss"),
            ("线段", "xds"),
            ("买卖点", "mmds"),
            ("背驰", "bcs"),
        ]
        tables: list[DetailDataTable] = []
        for title, key in table_specs:
            rows = result_dict.get(key)
            if isinstance(rows, list) and rows:
                clean_rows = self._clean_rows(rows[-30:], max_rows=30)
                tables.append(self._table(title, clean_rows, row_count=len(rows)))
        summary_keys = [
            "kline_count",
            "ckline_count",
            "fractal_count",
            "bi_count",
            "zs_count",
            "xd_count",
            "mmd_count",
            "bc_count",
        ]
        def _row_date(row: Any) -> str:
            if not isinstance(row, dict):
                row = _row_dict(row)
            for key in ("date", "curr_date", "end_date", "end_dt", "datetime", "time", "start_date"):
                digits = self._date_from_detail_row({"date": row.get(key)})
                if digits:
                    return digits
            return ""

        structure_dates = [
            date
            for key in ("bis", "zss", "xds", "mmds", "bcs")
            for row in (result_dict.get(key) if isinstance(result_dict.get(key), list) else [])
            for date in [_row_date(row)]
            if date
        ]
        structure_latest_date = max(structure_dates, default="")
        unconfirmed_kline_count = (
            sum(1 for row in kline_rows if str(row.get("date") or "") > structure_latest_date)
            if structure_latest_date
            else 0
        )
        summary = {key: jsonable_market_value(result_dict.get(key)) for key in summary_keys}
        summary.update(
            {
                "kline_latest_date": kline_latest.get("date") or "",
                "kline_latest_close": kline_latest.get("close"),
                "structure_latest_date": structure_latest_date,
                "unconfirmed_kline_count": unconfirmed_kline_count,
            }
        )
        return DetailDataPayload(
            available=bool(tables or result_dict),
            source=self.CHANLUN_SOURCE,
            code=code,
            fetched_at=china_now().isoformat(timespec="seconds"),
            summary=summary,
            tables=tables,
            note="缠论结构由 easy_tdx 日线K线计算，默认使用最近800根；结构日期表示已确认笔/线段/买卖点日期，可能晚于最新K线后才更新。",
        )

    def _clean_rows(self, rows: list[dict[str, Any]], max_rows: int) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        for row in rows[-max_rows:]:
            if not isinstance(row, dict):
                row = _row_dict(row)
            clean.append(
                {
                    str(column): jsonable_market_value(value, max_text=600)
                    for column, value in dict(row).items()
                }
            )
        return clean

    @classmethod
    def _daily_kline_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        source = dict(row or {})
        close = cls._float(source.get("close") or source.get("price") or source.get("last"))
        open_price = cls._float(source.get("open"), close)
        high = cls._float(source.get("high"), max(open_price, close))
        low = cls._float(source.get("low"), min(open_price, close))
        return {
            "date": cls._date_from_detail_row(source),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "price": close,
            "vol": cls._float(source.get("vol") or source.get("volume")),
            "amount": cls._float(source.get("amount")),
        }

    @staticmethod
    def _date_from_detail_row(row: dict[str, Any]) -> str:
        value = row.get("date") or row.get("datetime") or row.get("time")
        if hasattr(value, "strftime"):
            try:
                return value.strftime("%Y%m%d")
            except Exception:
                return ""
        text = str(value or "").strip()
        if not text:
            return ""
        digits = "".join(ch for ch in text[:10] if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else ""

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except Exception:
            return default
        return number if number == number else default

    @staticmethod
    def _table(title: str, rows: list[dict[str, Any]], row_count: int) -> DetailDataTable:
        columns = list(rows[0].keys()) if rows else []
        return DetailDataTable(
            title=title,
            columns=columns,
            raw_columns=columns,
            rows=rows,
            row_count=row_count,
        )


class EasyTdxBoardDataSource:
    SOURCE = "easy_tdx_mac_board_ranking"

    def __init__(self, settings: AppSettings, state_store: Any | None = None) -> None:
        self.settings = settings
        self.state_store = state_store
        self._context_cache: dict[int, tuple[float, BoardContext]] = {}
        self._member_cache: dict[tuple[int, str], tuple[float, list[str]]] = {}
        self._context_cache_lock = threading.Lock()
        self._context_refresh_threads: dict[int, threading.Thread] = {}
        self._member_warmup_threads: dict[int, threading.Thread] = {}
        self._context_disk_persist_at: dict[int, float] = {}

    def _mac_client(self) -> Any:
        try:
            from easy_tdx import MacClient
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise DataSourceError("easy_tdx未安装，无法读取官方板块。") from exc
        return MacClient(timeout=max(2.0, float(self.settings.easy_tdx_f10_timeout_seconds)))

    def fetch_context(self, board_level: Any = 3) -> BoardContext:
        level = normalize_board_level(board_level)
        now_ts = time.time()
        ttl = max(300, int(self.settings.board_static_cache_seconds))
        with self._context_cache_lock:
            cached = self._context_cache.get(level)
        if cached:
            context = cached[1]
            if now_ts - cached[0] > ttl:
                self._ensure_context_refresh(level)
            self._ensure_member_warmup(level)
            return self._copy_context(context)
        stored_context = self._best_stored_context(
            self._load_context_from_disk(level),
            self._load_context_from_cloud(level),
        )
        if stored_context is not None:
            with self._context_cache_lock:
                self._context_cache[level] = (now_ts, stored_context)
            self._ensure_member_warmup(level)
            return self._copy_context(stored_context)
        try:
            context = self._fetch_context(level)
        except Exception as exc:  # pragma: no cover - network/server dependent
            context = BoardContext(
                board_level=level,
                source=self.SOURCE,
                available=False,
                fetched_at=china_now().isoformat(timespec="seconds"),
                sectors=[],
                name_to_code={},
                code_to_name={},
                error=f"easy_tdx官方板块不可用：{jsonable_market_value(exc)}",
            )
        with self._context_cache_lock:
            self._context_cache[level] = (now_ts, context)
        self._persist_context_to_disk(level, context)
        self._ensure_member_warmup(level)
        return self._copy_context(context)

    def _ensure_context_refresh(self, level: int) -> None:
        with self._context_cache_lock:
            thread = self._context_refresh_threads.get(level)
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._refresh_context_in_background,
                args=(level,),
                name=f"easy-tdx-board-refresh-{level}",
                daemon=True,
            )
            self._context_refresh_threads[level] = thread
            thread.start()

    def _refresh_context_in_background(self, level: int) -> None:
        try:
            context = self._fetch_context(level)
        except Exception:
            context = None
        with self._context_cache_lock:
            current = self._context_refresh_threads.get(level)
            if current is threading.current_thread():
                self._context_refresh_threads.pop(level, None)
            if context is not None:
                self._context_cache[level] = (time.time(), context)
        if context is not None:
            self._persist_context_to_disk(level, context)
            self._ensure_member_warmup(level)

    def _ensure_member_warmup(self, level: int) -> None:
        if not self.settings.board_member_warmup_enabled:
            return
        with self._context_cache_lock:
            cached = self._context_cache.get(level)
            if not cached:
                return
            context = cached[1]
            if not context.available or not context.sectors:
                return
            cached_count = sum(
                1
                for sector in context.sectors
                if sector.board_code and context.members_by_code.get(sector.board_code)
            )
            if cached_count >= len(context.sectors):
                return
            thread = self._member_warmup_threads.get(level)
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._warm_members_in_background,
                args=(level,),
                name=f"easy-tdx-board-member-warmup-{level}",
                daemon=True,
            )
            self._member_warmup_threads[level] = thread
            thread.start()

    def _warm_members_in_background(self, level: int) -> None:
        with self._context_cache_lock:
            cached = self._context_cache.get(level)
            context = cached[1] if cached else None
        if context is None or not context.available:
            return
        warmed = self._copy_context(context)
        changed = False
        try:
            with self._mac_client() as client:
                for sector in warmed.sectors:
                    symbol = str(sector.board_code or "").strip()
                    if not symbol or warmed.members_by_code.get(symbol):
                        continue
                    try:
                        codes = self._fetch_member_codes(client, symbol)
                    except Exception:
                        codes = []
                    if codes:
                        warmed.members_by_code[symbol] = codes
                        self._member_cache[(level, symbol)] = (time.time(), list(codes))
                        changed = True
        finally:
            with self._context_cache_lock:
                current = self._member_warmup_threads.get(level)
                if current is threading.current_thread():
                    self._member_warmup_threads.pop(level, None)
                if changed:
                    self._context_cache[level] = (time.time(), warmed)
            if changed:
                self._persist_context_to_disk(level, warmed, force=True)

    def _context_disk_file(self, level: int) -> Path:
        return self.settings.data_dir / "runtime" / f"easy_tdx_board_level_{level}.json"

    def _load_context_from_disk(self, level: int) -> BoardContext | None:
        path = self._context_disk_file(level)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return None
        return self._context_from_payload(level, payload)

    def _load_context_from_cloud(self, level: int) -> BoardContext | None:
        if self.state_store is None:
            return None
        try:
            payload = self.state_store.get_json("board_context", f"level_{level}")
        except Exception:
            return None
        return self._context_from_payload(level, payload)

    def _context_from_payload(self, level: int, payload: Any) -> BoardContext | None:
        if not isinstance(payload, dict):
            return None
        try:
            sectors = [
                SectorSnapshot.model_validate(item)
                for item in payload.get("sectors", [])
                if isinstance(item, dict)
            ]
            if not sectors:
                return None
            return BoardContext(
                board_level=normalize_board_level(payload.get("board_level") or level),
                source=str(payload.get("source") or self.SOURCE),
                available=bool(payload.get("available", True)),
                fetched_at=str(payload.get("fetched_at") or ""),
                sectors=sectors,
                name_to_code={str(k): str(v) for k, v in dict(payload.get("name_to_code") or {}).items()},
                code_to_name={str(k): str(v) for k, v in dict(payload.get("code_to_name") or {}).items()},
                error=str(payload.get("error") or ""),
                members_by_code={
                    str(key): list(dict.fromkeys(str(code).zfill(6) for code in value if str(code).strip().isdigit()))
                    for key, value in dict(payload.get("members_by_code") or {}).items()
                    if isinstance(value, list)
                },
            )
        except Exception:
            return None

    @staticmethod
    def _stored_member_count(context: BoardContext | None) -> int:
        if context is None:
            return -1
        return sum(
            1
            for sector in context.sectors
            if sector.board_code and context.members_by_code.get(sector.board_code)
        )

    def _best_stored_context(
        self,
        disk_context: BoardContext | None,
        cloud_context: BoardContext | None,
    ) -> BoardContext | None:
        if disk_context is None:
            return cloud_context
        if cloud_context is None:
            return disk_context
        if self._stored_member_count(cloud_context) > self._stored_member_count(disk_context):
            return cloud_context
        return disk_context

    @staticmethod
    def _context_payload(context: BoardContext) -> dict[str, Any]:
        return {
            "board_level": context.board_level,
            "source": context.source,
            "available": context.available,
            "fetched_at": context.fetched_at,
            "sectors": [sector.model_dump(mode="json") for sector in context.sectors],
            "name_to_code": context.name_to_code,
            "code_to_name": context.code_to_name,
            "members_by_code": context.members_by_code,
            "error": context.error,
        }

    def _persist_context_to_cloud(self, level: int, payload: dict[str, Any]) -> None:
        if self.state_store is None:
            return
        try:
            self.state_store.set_json("board_context", f"level_{level}", payload)
        except Exception:
            return

    def _persist_context_to_disk(self, level: int, context: BoardContext, *, force: bool = False) -> None:
        if not context.available or not context.sectors:
            return
        now_ts = time.time()
        if not force and now_ts - self._context_disk_persist_at.get(level, 0) < 60:
            return
        self._context_disk_persist_at[level] = now_ts
        path = self._context_disk_file(level)
        payload = self._context_payload(context)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            tmp_path.replace(path)
        except Exception:
            pass
        self._persist_context_to_cloud(level, payload)

    def member_codes(self, board_name_or_code: str, board_level: Any = 3) -> list[str]:
        level = normalize_board_level(board_level)
        raw = str(board_name_or_code or "").strip()
        if not raw:
            return []
        context = self.fetch_context(level)
        symbol = context.name_to_code.get(raw) or raw
        if not symbol.isdigit():
            return []
        now_ts = time.time()
        if context.members_by_code.get(symbol):
            return list(context.members_by_code[symbol])
        ttl = max(300, int(self.settings.board_member_cache_seconds))
        key = (level, symbol)
        cached = self._member_cache.get(key)
        if cached and now_ts - cached[0] <= ttl:
            return list(cached[1])
        try:
            with self._mac_client() as client:
                codes = self._fetch_member_codes(client, symbol)
        except Exception:
            codes = []
        self._member_cache[key] = (now_ts, codes)
        if codes:
            with self._context_cache_lock:
                cached_context = self._context_cache.get(level)
                if cached_context:
                    cached_context[1].members_by_code[symbol] = list(codes)
                    context = cached_context[1]
            self._persist_context_to_disk(level, context, force=True)
        return list(codes)

    def _fetch_member_codes(self, client: Any, symbol: str) -> list[str]:
        rows = dataframe_records(client.get_board_members(symbol))
        return list(
            dict.fromkeys(
                str(row.get("code") or "").strip().zfill(6)
                for row in rows
                if str(row.get("code") or "").strip().isdigit()
            )
        )

    def _fetch_context(self, level: int) -> BoardContext:
        from easy_tdx import BoardType

        board_type = getattr(BoardType, f"YJ_LEVEL{level}")
        with self._mac_client() as client:
            list_rows = dataframe_records(client.get_board_list(board_type, count=10000))
            try:
                ranking_rows = dataframe_records(
                    client.get_board_ranking(
                        board_type,
                        top_n=max(1000, len(list_rows) or 1000),
                    )
                )
            except Exception:
                ranking_rows = []
        by_code: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for row in list_rows:
            code = str(row.get("code") or row.get("board_code") or "").strip()
            if not code:
                continue
            by_code[code] = dict(row)
            order.append(code)
        for row in ranking_rows:
            code = str(row.get("code") or row.get("board_code") or "").strip()
            if not code:
                continue
            merged = {**by_code.get(code, {}), **dict(row)}
            by_code[code] = merged
            if code not in order:
                order.append(code)

        sectors = [
            sector
            for code in order
            if (sector := self._sector_from_board_row(by_code[code], level)) is not None
        ]
        if not sectors:
            raise DataSourceError(f"easy_tdx未返回{level}级官方板块。")
        sectors.sort(
            key=lambda sector: (
                sector.heat_score,
                sector.avg_change_pct,
                sector.main_net_amount,
                sector.amount,
            ),
            reverse=True,
        )
        name_to_code = {sector.name: sector.board_code for sector in sectors if sector.board_code}
        code_to_name = {sector.board_code: sector.name for sector in sectors if sector.board_code}
        return BoardContext(
            board_level=level,
            source=self.SOURCE,
            available=True,
            fetched_at=china_now().isoformat(timespec="seconds"),
            sectors=sectors,
            name_to_code=name_to_code,
            code_to_name=code_to_name,
            members_by_code={},
        )

    def _sector_from_board_row(self, row: dict[str, Any], level: int) -> SectorSnapshot | None:
        code = str(row.get("code") or row.get("board_code") or "").strip()
        name = str(row.get("name") or row.get("board_name") or "").strip()
        if not code or not name:
            return None
        price = self._float(row.get("price") or row.get("close"))
        pre_close = self._float(row.get("pre_close"))
        change_pct = self._float(row.get("change_pct"))
        if change_pct == 0 and price > 0 and pre_close > 0:
            change_pct = (price - pre_close) / pre_close * 100
        amount = self._float(row.get("amount"))
        main_net_amount = self._float(row.get("main_net_amount"))
        up_count = self._int(row.get("up_count"))
        down_count = self._int(row.get("down_count"))
        total_count = self._int(row.get("member_count")) or up_count + down_count
        leader_code = str(row.get("symbol_code") or "").strip().zfill(6)
        leader_code = leader_code if leader_code.isdigit() and leader_code != "000000" else None
        leader_name = str(row.get("symbol_name") or "").strip() or None
        main_net_yi = main_net_amount / 100_000_000 if main_net_amount else 0
        flow_delta = main_net_yi if main_net_amount else (amount / 100_000_000) * (1 if change_pct > 0 else -1 if change_pct < 0 else 0)
        heat_score = self._heat_score(change_pct, up_count, down_count, total_count, amount, main_net_amount)
        reasons = [
            f"官方{level}级板块涨幅{change_pct:+.2f}%",
            f"{up_count}/{total_count or up_count + down_count}上涨",
        ]
        if main_net_amount:
            reasons.append(f"主净额{main_net_yi:+.1f}亿")
        if amount:
            reasons.append(f"成交额{amount / 100_000_000:.1f}亿")
        if leader_name:
            reasons.append(f"领涨{leader_name}")
        return SectorSnapshot(
            name=name,
            heat_score=heat_score,
            avg_change_pct=round(change_pct, 2),
            up_count=up_count,
            down_count=down_count,
            total_count=max(total_count, up_count + down_count, 1),
            limit_up_count=0,
            opened_limit_count=0,
            core_attack=bool(change_pct > 0 and (main_net_amount > 0 or leader_code)),
            core_codes=[leader_code] if leader_code else [],
            leader_code=leader_code,
            leader_name=leader_name,
            reasons=list(dict.fromkeys(reasons)),
            flow_delta=round(flow_delta, 2),
            amount=round(amount, 2),
            main_net_amount=round(main_net_amount, 2),
            board_code=code,
            board_level=level,
            board_source=self.SOURCE,
        )

    def _heat_score(
        self,
        change_pct: float,
        up_count: int,
        down_count: int,
        total_count: int,
        amount: float,
        main_net_amount: float,
    ) -> int:
        total = max(total_count, up_count + down_count, 1)
        breadth = up_count / total
        amount_yi = min(max(amount, 0) / 100_000_000, 120)
        main_net_yi = max(-20, min(20, main_net_amount / 100_000_000 if main_net_amount else 0))
        score = 46 + change_pct * 4.5 + (breadth - 0.5) * 28 + main_net_yi * 1.6 + amount_yi * 0.05
        return int(max(0, min(100, round(score))))

    @staticmethod
    def _float(value: Any) -> float:
        try:
            number = float(value)
        except Exception:
            return 0.0
        if number != number:
            return 0.0
        return number

    @classmethod
    def _int(cls, value: Any) -> int:
        return int(round(cls._float(value)))

    @staticmethod
    def _copy_context(context: BoardContext) -> BoardContext:
        return BoardContext(
            board_level=context.board_level,
            source=context.source,
            available=context.available,
            fetched_at=context.fetched_at,
            sectors=[sector.model_copy(deep=True) for sector in context.sectors],
            name_to_code=dict(context.name_to_code),
            code_to_name=dict(context.code_to_name),
            error=context.error,
            members_by_code={key: list(value) for key, value in context.members_by_code.items()},
        )


class MarketDataRouter:
    def __init__(self, settings: AppSettings, state_store: Any | None = None) -> None:
        self.settings = settings
        self.universe = UniverseProvider(settings)
        self.close_source = EasyTdxDailyDataSource(settings)
        self.replay = ReplayMarketDataSource()
        self.live = EasyTdxMarketDataSource(settings, self.close_source)
        self.minute_replay = EasyTdxMinuteReplaySource(settings)
        self.f10 = EasyTdxF10DataSource(settings)
        self.detail_data = EasyTdxDetailDataSource(settings)
        self.boards = EasyTdxBoardDataSource(settings, state_store=state_store)
        self._live_cache: MarketSnapshot | None = None
        self._live_cache_at: float = 0.0
        self._minute_cache: dict[tuple[str, str, str, bool], tuple[float, list[dict[str, Any]]]] = {}

    def fetch(self, watchlist: list[WatchlistItem], themes: list[dict]) -> MarketSnapshot:
        universe, universe_status = self.universe.load(watchlist, themes)
        mode = self.settings.data_mode
        if mode == "replay":
            snapshot = self.replay.fetch(watchlist, themes, universe)
            snapshot.source_status.update(universe_status)
            snapshot.source_status["signal_scope"] = self.settings.scan_scope
            snapshot.source_status["synthetic"] = True
            return snapshot
        if mode == "live":
            return self._fetch_live_snapshot(universe, universe_status, watchlist, themes)
        try:
            return self._fetch_live_snapshot(universe, universe_status, watchlist, themes)
        except Exception as exc:
            if is_trading_window():
                if self._live_cache is not None:
                    snapshot = self._stale_live_snapshot(
                        self._live_cache,
                        universe_status,
                        live_error=str(exc),
                    )
                    return snapshot
                return self._intraday_unavailable_snapshot(
                    universe_status,
                    f"交易日内实时行情不可用：{exc}",
                )
            try:
                snapshot = self.close_source.fetch(universe)
                snapshot.source_status.update(universe_status)
                snapshot.source_status["live_error"] = str(exc)
                snapshot.source_status["signal_scope"] = self.settings.scan_scope
                return snapshot
            except Exception as close_exc:
                if self.settings.allow_replay_fallback:
                    snapshot = self.replay.fetch(watchlist, themes, universe)
                    snapshot.source_status.update(universe_status)
                    snapshot.source_status["live_error"] = f"{exc}; fallback_close_error={close_exc}"
                    snapshot.source_status["signal_scope"] = self.settings.scan_scope
                    snapshot.source_status["synthetic"] = True
                    return snapshot
                return self._unavailable_snapshot(
                    universe_status,
                    f"easy_tdx实时与easy_tdx日K均不可用：{exc}; daily_error={close_exc}",
                )

    def fetch_trade_date_snapshot(
        self,
        watchlist: list[WatchlistItem],
        themes: list[dict],
        trade_date: str,
    ) -> MarketSnapshot:
        universe, universe_status = self.universe.load(watchlist, themes)
        snapshot = self.close_source.fetch_for_date(universe, trade_date)
        snapshot.source_status.update(universe_status)
        snapshot.source_status["signal_scope"] = self.settings.scan_scope
        return snapshot

    def _fetch_live_snapshot(
        self,
        universe: dict[str, StockMeta],
        universe_status: dict[str, Any],
        watchlist: list[WatchlistItem],
        themes: list[dict],
    ) -> MarketSnapshot:
        now = time.time()
        if self._live_cache and now - self._live_cache_at < self.settings.full_market_refresh_seconds:
            snapshot = self._live_cache
            snapshot.source_status.update(universe_status)
            snapshot.source_status["signal_scope"] = self.settings.scan_scope
            snapshot.source_status["cache_hit"] = True
            return snapshot
        snapshot = self.live.fetch(universe)
        snapshot.source_status.update(universe_status)
        snapshot.source_status["signal_scope"] = self.settings.scan_scope
        snapshot.source_status["cache_hit"] = False
        self._live_cache = snapshot
        self._live_cache_at = now
        return snapshot

    def _unavailable_snapshot(self, universe_status: dict[str, Any], note: str) -> MarketSnapshot:
        return MarketSnapshot(
            quotes=[],
            indices=[],
            data_mode="unavailable",
            source_status={
                **universe_status,
                "active_source": "unavailable",
                "frozen": True,
                "synthetic": False,
                "clock_label": "--",
                "note": note,
                "data_quality": "no_real_data",
                "quote_count": 0,
            },
        )

    def _intraday_unavailable_snapshot(self, universe_status: dict[str, Any], note: str) -> MarketSnapshot:
        now = china_now()
        session = market_session(now)
        return MarketSnapshot(
            quotes=[],
            indices=[],
            data_mode="unavailable",
            source_status={
                **universe_status,
                "active_source": "easy_tdx_unavailable",
                "frozen": False,
                "synthetic": False,
                "clock_label": now.strftime("%H:%M:%S"),
                "trade_date": now.strftime("%Y%m%d"),
                "market_session": session,
                "lunch_break": session == "lunch_break",
                "note": note,
                "data_quality": "intraday_live_unavailable",
                "quote_count": 0,
            },
        )

    def _stale_live_snapshot(
        self,
        snapshot: MarketSnapshot,
        universe_status: dict[str, Any],
        *,
        live_error: str,
    ) -> MarketSnapshot:
        now = china_now()
        session = market_session(now)
        source_status = dict(snapshot.source_status)
        source_status.update(universe_status)
        source_status.update(
            {
                "signal_scope": self.settings.scan_scope,
                "cache_hit": True,
                "stale_live_cache": True,
                "live_error": live_error,
                "active_source": source_status.get("active_source") or "easy_tdx",
                "frozen": False,
                "clock_label": now.strftime("%H:%M:%S"),
                "market_session": session,
                "lunch_break": session == "lunch_break",
                "note": (
                    "实时行情临时不可用，保留上一份 easy_tdx 当天快照；"
                    "不切换到其他日线收盘快照。"
                ),
            }
        )
        return MarketSnapshot(
            quotes=list(snapshot.quotes),
            indices=list(snapshot.indices),
            data_mode="live",
            source_status=source_status,
        )

    def fetch_minute_series(self, code: str, trade_date: str, live: bool = False) -> list[dict[str, Any]]:
        return self._fetch_cached_minute_series("stock", code, trade_date, live=live)

    def fetch_index_minute_series(self, code: str, trade_date: str, live: bool = False) -> list[dict[str, Any]]:
        return self._fetch_cached_minute_series("index", code, trade_date, live=live)

    def auction_history(self, code: str, trade_date: str | None = None) -> list[dict[str, Any]]:
        return self.live.auction_history(code, trade_date=trade_date)

    def fetch_transaction_flow(
        self,
        code: str,
        trade_date: str | None = None,
        count: int | None = None,
        full_session: bool = False,
    ) -> TransactionFlowObservation:
        """Fetch one stock's real TDX L1 transaction tape on demand."""
        return self.live.fetch_transaction_flow(
            code,
            trade_date=trade_date,
            count=count,
            full_session=full_session,
        )

    def fetch_quote_subset(
        self,
        codes: list[str],
        base_quotes: list[Quote] | None = None,
    ) -> dict[str, Quote]:
        """Fetch a small visible quote set without scanning the whole market."""
        if self.settings.data_mode == "replay":
            return {}
        return self.live.fetch_quote_subset(codes, base_quotes=base_quotes)

    def fetch_fundamentals(self, code: str) -> FundamentalPayload:
        return self.f10.fetch(code)

    def fetch_capital_flow(self, code: str) -> DetailDataPayload:
        return self.detail_data.fetch_capital_flow(code)

    def fetch_technical_indicators(self, code: str) -> DetailDataPayload:
        return self.detail_data.fetch_technical_indicators(code)

    def fetch_chanlun(self, code: str) -> DetailDataPayload:
        return self.detail_data.fetch_chanlun(code)

    def fetch_daily_kline_rows(self, code: str, count: int = 180) -> list[dict[str, Any]]:
        return self.detail_data.fetch_daily_kline_rows(code, count=count)

    def fetch_board_context(self, board_level: Any = 3) -> BoardContext:
        return self.boards.fetch_context(board_level)

    def fetch_board_member_codes(self, board_name_or_code: str, board_level: Any = 3) -> list[str]:
        return self.boards.member_codes(board_name_or_code, board_level)

    def capabilities(self) -> dict[str, Any]:
        capabilities = self.live.capabilities()
        capabilities.update(
            {
                "fundamentals_f10": True,
                "fundamentals_f10_protocol": "TdxClient.get_finance_info/get_company_info_category/get_financial_records",
                "fundamentals_f10_source": EasyTdxF10DataSource.SOURCE,
                "fundamentals_f10_sections": len(EasyTdxF10DataSource.SECTION_SPECS),
                "fundamentals_note": "easy_tdx F10/财报数据仅按个股详情页按需读取，不进入实时刷新循环。",
                "capital_flow": True,
                "capital_flow_protocol": "MacClient.get_capital_flow",
                "technical_indicators": True,
                "technical_indicators_protocol": "MacClient.get_stock_kline_with_indicators",
                "chanlun": True,
                "chanlun_protocol": "MacClient.get_stock_kline + ChanlunAnalyser",
                "daily_trend_kline": True,
                "daily_trend_kline_protocol": "MacClient.get_stock_kline(Period.DAILY, Adjust.QFQ)",
                "official_board_levels": [1, 2, 3],
                "official_board_protocol": "MacClient.get_board_list/get_board_ranking/get_board_members",
                "official_board_source": EasyTdxBoardDataSource.SOURCE,
            }
        )
        return capabilities

    def _fetch_cached_minute_series(self, scope: str, code: str, trade_date: str, live: bool = False) -> list[dict[str, Any]]:
        cache_live = bool(live and is_trading_window())
        key = (scope, code, trade_date, cache_live)
        now = time.time()
        ttl = (
            self.settings.minute_series_live_cache_seconds
            if cache_live
            else self.settings.minute_series_static_cache_seconds
        )
        cached = self._minute_cache.get(key)
        if cached and now - cached[0] <= ttl:
            return [dict(row) for row in cached[1]]

        if scope == "index":
            rows = self.minute_replay.fetch_index(code, trade_date, live=cache_live)
        else:
            rows = self.minute_replay.fetch(code, trade_date, live=cache_live)
        cached_rows = [dict(row) for row in rows]
        self._minute_cache[key] = (now, cached_rows)
        if len(self._minute_cache) > 512:
            oldest_key = min(self._minute_cache, key=lambda item: self._minute_cache[item][0])
            self._minute_cache.pop(oldest_key, None)
        return [dict(row) for row in cached_rows]


def market_session(now: datetime | None = None) -> str:
    current_time = _as_china_time(now) if now is not None else china_now()
    if current_time.weekday() >= 5:
        return "closed_day"
    current = current_time.hour * 60 + current_time.minute
    if current < 9 * 60 + 15:
        return "pre_market"
    if current < 9 * 60 + 30:
        return "preopen"
    if current <= 11 * 60 + 30:
        return "morning"
    if current < 13 * 60:
        return "lunch_break"
    if current < 15 * 60:
        return "afternoon"
    if current <= 15 * 60 + 5:
        return "closing_buffer"
    return "post_close"


def is_trading_window(now: datetime | None = None) -> bool:
    current_time = _as_china_time(now) if now is not None else china_now()
    if current_time.weekday() >= 5:
        return False
    current = current_time.hour * 60 + current_time.minute
    return 9 * 60 + 15 <= current <= 15 * 60 + 5


def is_preopen_window(now: datetime | None = None) -> bool:
    current_time = _as_china_time(now) if now is not None else china_now()
    if current_time.weekday() >= 5:
        return False
    current = current_time.hour * 60 + current_time.minute
    return 9 * 60 + 15 <= current < 9 * 60 + 30


