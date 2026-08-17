from __future__ import annotations

from dataclasses import dataclass
import logging
from math import ceil, isfinite, log10
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from app.ai_client import AIAnalysisClient
from app.cloud_persistence import CloudBaseNoSqlStateStore
from app.config import AppSettings, load_yaml
from app.data_sources import (
    BoardContext,
    MarketDataRouter,
    MarketSnapshot,
    china_now,
    is_trading_window,
    market_session,
    normalize_board_level,
)
from app.market_schedule import market_refresh_policy
from app.message_store import MessageStore
from app.webhook_push import SignalPushPool, WebhookSubscriptionStore
from app.dark_pool import DarkPoolMonitor
from app.formula_engine import (
    SIGNAL_VERSION,
    ZuoTDayContext,
    compute_zuot_series,
)
from app.models import (
    AnalysisRecord,
    ConfluenceSnapshot,
    DashboardPayload,
    DetailChartSeries,
    EventItem,
    FormulaOverlay,
    FormulaState,
    IndexReplayDetail,
    IndexSnapshot,
    MarketState,
    MessageDetailPayload,
    MessageEvidenceBundle,
    MessageStoreStatus,
    MiniIntradayMarker,
    MiniIntradaySeries,
    OpeningDecisionPayload,
    PositionRecord,
    Quote,
    SectorFlowPoint,
    SectorFlowSeries,
    ReplayMarker,
    ReplayPoint,
    RiskRewardPlan,
    SectorSnapshot,
    SignalDetailChartPayload,
    SignalDetailExtrasPayload,
    SignalDetailOverlayMarker,
    SignalDetailOverlayPayload,
    SignalReplayDetail,
    SignalType,
    SignalPhase,
    StockBoardItem,
    StockBoardPayload,
    TerminalPayload,
    TransactionFlowObservation,
    TradeSignal,
    TradeAction,
    TradeDirection,
    WatchlistItem,
    ZsxqMessageIngestRequest,
    ZsxqMessageIngestResponse,
)
from app.signal_engine import SignalEngine
from app.opening_strategy import OpeningStrategy
from app.storage import (
    AnalysisStore,
    CloudBackedPositionStore,
    CloudBackedWatchlistStore,
    JsonStateStore,
    PositionStore,
    ThemeStore,
    WatchlistStore,
)
from app.trajectory_store import IntradayWatchtowerStore
from app.trend_line_store import TrendLineStore, near_trend_match


logger = logging.getLogger(__name__)


MINI_CHART_SOURCE_ROWS = 1200
MINI_CHART_REPRESENTATIVE_ROWS = 240


@dataclass
class DashboardContext:
    watchlist: list[WatchlistItem]
    themes: list[dict]
    snapshot: MarketSnapshot
    market: MarketState
    sectors: list[SectorSnapshot]
    sector_flow: list[SectorFlowSeries]
    signals_all: list[TradeSignal]
    core_watch: list[TradeSignal]
    events: list[EventItem]
    source_status: dict[str, Any]


@dataclass
class SignalDetailContext:
    context: DashboardContext
    actual_trade_date: str
    quote: Quote
    signal: TradeSignal
    sector_snapshot: SectorSnapshot | None
    selected_sector: str | None
    position: PositionRecord | None
    watchlist_item: WatchlistItem | None
    live_mode: bool


@dataclass
class SharedMinuteChartRows:
    rows: list[dict[str, Any]]
    error: str | None = None


@dataclass
class StockDetailBundle:
    """详情页共享计算结果：分钟行 + 分时图序列 + 做T公式状态 + 买卖标记。

    chart/overlay/详情三个入口共用，配合短 TTL 单飞缓存保证每股每次只算一次。
    """

    rows: list[dict[str, Any]]
    chart: DetailChartSeries
    formula_result: Any
    formula_state: FormulaState
    formula_overlay: FormulaOverlay
    markers: list[SignalDetailOverlayMarker]
    summary: list[str]
    error: str | None = None


@dataclass
class BoardEntry:
    sort_key: tuple[Any, ...]
    quote: Quote
    sector: SectorSnapshot | None


class DashboardService:
    _DEFAULT_INDEX_MINUTE_SERIES: tuple[tuple[str, str], ...] = (
        ("000001", "上证指数"),
        ("399001", "深证成指"),
        ("399006", "创业板指"),
    )

    def __init__(
        self,
        settings: AppSettings,
        watchlist_store: WatchlistStore | None = None,
        theme_store: ThemeStore | None = None,
        analysis_store: AnalysisStore | None = None,
        ai_client: AIAnalysisClient | None = None,
        message_store: MessageStore | None = None,
        position_store: PositionStore | None = None,
        trajectory_store: IntradayWatchtowerStore | None = None,
        state_store: JsonStateStore | None = None,
    ) -> None:
        self.settings = settings
        self.state_store = state_store or self._build_state_store(settings)
        self.watchlist_store = watchlist_store or self._build_watchlist_store(settings)
        self.theme_store = theme_store or ThemeStore(settings.themes_file)
        self.analysis_store = analysis_store or AnalysisStore(settings.data_dir / "runtime" / "analysis")
        self.ai_client = ai_client or AIAnalysisClient(settings)
        self.message_store = message_store or MessageStore.from_settings(settings)
        self.position_store = position_store or self._build_position_store(settings)
        self.trajectory_store = trajectory_store or IntradayWatchtowerStore(
            settings.intraday_watchtower_db_file,
            enabled=settings.trajectory_enabled,
            state_store=self.state_store,
        )
        self.data_source = MarketDataRouter(settings, state_store=self.state_store)
        # 短期/长期趋势线最新值缓存：榜单「低吸机会」过滤按当天口径复用，后台补算缺失代码
        self.trend_line_store = TrendLineStore(
            settings.data_dir / "runtime" / "trend_lines.json",
            self.data_source.fetch_daily_kline_rows,
        )
        from app.stock_tags import StockTagStore

        self.stock_tag_store = StockTagStore()
        self.engine = SignalEngine(load_yaml(settings.rules_file, {}))
        self.opening_strategy = OpeningStrategy(
            load_yaml(settings.rules_file, {}),
            persist_path=settings.opening_decision_file,
        )
        # 暗盘资金监控：盘中大单推断独立慢循环（默认 120s/24 只），
        # 盘后口径读本地 tushare_eod.sqlite；均不进 5 秒大盘刷新循环。
        self.dark_pool_monitor = DarkPoolMonitor(
            settings,
            context_provider=self._get_context,
            tape_fetcher=self._fetch_transaction_flow,
            sector_mapper=self._stock_board_display_map_for_level,
        )
        self._context_cache: DashboardContext | None = None
        self._context_cache_at: float = 0.0
        self._context_cache_bucket: str = ""
        self._context_lock = threading.Lock()
        # 全量刷新互斥锁：_refresh_context 可能耗时数秒（全市场快照+信号构建），
        # 用它替代 _context_lock 做刷新串行化，避免读请求/写自选被长锁卡住。
        self._refresh_in_progress_lock = threading.Lock()
        self._refresh_trigger_lock = threading.Lock()
        self._refresh_thread: threading.Thread | None = None
        self._sector_flow_cache: list[SectorFlowSeries] | None = None
        self._sector_flow_cache_at: float = 0.0
        self._sector_flow_cache_key: str = ""
        self._sector_flow_names: list[str] = []
        self._sector_flow_names_key: str = ""
        self._sector_flow_cache_by_key: dict[str, tuple[float, list[SectorFlowSeries]]] = {}
        self._sector_flow_names_by_key: dict[str, list[str]] = {}
        self._sector_flow_lock = threading.Lock()
        self._sector_flow_refresh_threads: dict[str, threading.Thread] = {}
        # 个股 → 官方板块（easy_tdx 申万 1/2/3 级）名称映射：机会队列把 X410302 这类内部
        # 行业代码显示成「网络工程施工」；每级独立缓存，300s TTL，板块成员缓存本身也是热数据
        self._stock_board_name_map_by_level: dict[int, dict[str, str]] = {}
        self._stock_board_name_map_at_by_level: dict[int, float] = {}
        # 快照代理曲线状态：cache_key -> {"trade_date", "points", "cum", "offset"}
        self._sector_flow_proxy_by_key: dict[str, dict[str, Any]] = {}
        self._sector_flow_last_build_at: dict[str, float] = {}
        self._sector_flow_cloud_write_at_by_key: dict[str, float] = {}
        # 全市场个股 tick 缓存：code -> {"rows": [{time,price,amount,volume}], "prev"/"last": (price, amount)}
        # 每个刷新周期从全市场快照并入，供各消费方按票/板块筛选聚合，避免逐股额外请求。
        self._quote_tick_cache: dict[str, dict[str, Any]] = {}
        self._quote_tick_cache_date: str = ""
        self._terminal_cache_by_key: dict[str, tuple[float, TerminalPayload]] = {}
        # 载荷共享缓存的并发保护：所有 WS 连接/HTTP 请求读同一份缓存；
        # _terminal_build_locks 按 cache_key 单飞，同一视图同一时刻最多一个线程构建。
        self._terminal_cache_lock = threading.Lock()
        self._terminal_build_locks: dict[str, threading.Lock] = {}
        self._terminal_warmup_lock = threading.Lock()
        self._terminal_warmup_signature: str = ""
        self._terminal_warmup_thread: threading.Thread | None = None
        self._board_members_cache_by_key: dict[tuple[int, str], dict[str, list[str]]] = {}
        self._quote_sector_map_cache: dict[tuple[int, str], dict[str, SectorSnapshot]] = {}
        self._sector_rank_cache: dict[tuple[int, str], dict[str, int]] = {}
        self._stock_mini_chart_cache: dict[tuple[str, str], tuple[float, MiniIntradaySeries]] = {}
        # 分时缩略图后台预热：请求路径立即返回（stale-while-revalidate），
        # 由单 worker 批量填充缓存，下一轮 WS 增量自然带出真实曲线
        self._mini_chart_warm_lock = threading.Lock()
        self._mini_chart_warm_pending: set[tuple[str, str]] = set()
        self._mini_chart_warm_thread: threading.Thread | None = None
        self._sector_mini_chart_cache: dict[tuple[str, str], tuple[float, MiniIntradaySeries]] = {}
        self._fast_board_entries_cache: dict[str, tuple[float, list[BoardEntry]]] = {}
        self._visible_quote_cache: dict[str, tuple[float, Quote]] = {}
        self._visible_quote_refresh_started_at_by_key: dict[str, float] = {}
        self._visible_quote_refresh_threads: dict[str, threading.Thread] = {}
        self._visible_quote_refresh_errors_by_key: dict[str, tuple[float, str]] = {}
        self._visible_quote_lock = threading.Lock()
        self._last_stock_mini_chart_elapsed_ms: float = 0.0
        self._last_stock_mini_chart_missing_count: int = 0
        self._last_stock_mini_chart_loaded_count: int = 0
        self._last_visible_mini_chart_cache: dict[str, MiniIntradaySeries] = {}
        # 当天最近一次做T买/卖点（方向/触发价/时间）：信号发生时被记录，
        # 信号回到「观察」后仍供榜单距买卖点 ±% 与「置顶买点」排序使用。
        self._last_action_lock = threading.Lock()
        self._last_action_by_code: dict[str, dict[str, Any]] = {}
        # 飞书 webhook 信号推送池：订阅注册持久化到 data/webhook_subscriptions.json，
        # 每轮实盘信号构建后按票去重投递（同票 5 分钟内只推一次）。
        self.push_pool = SignalPushPool(
            WebhookSubscriptionStore(settings.webhook_subscriptions_file),
            dedup_seconds=settings.push_signal_dedup_seconds,
            timeout_seconds=settings.push_http_timeout_seconds,
        )
        self._last_trajectory_cleanup: dict[str, Any] = {}
        self._last_trajectory_cleanup_day: str = ""
        self._trajectory_cleanup_lock = threading.Lock()
        self._trajectory_cleanup_thread: threading.Thread | None = None
        # F10 盘前增量预热：每日 08:40 起（周一至周五）一轮，只刷过期缓存。
        self._last_f10_preopen: dict[str, Any] = {}
        self._last_f10_preopen_day: str = ""
        self._f10_preopen_lock = threading.Lock()
        self._f10_preopen_thread: threading.Thread | None = None
        self._previous_sector_ranks: dict[str, int] = {}
        self._historical_context_cache: dict[str, DashboardContext] = {}
        # 详情页基础上下文/分钟源/公式bundle 的短 TTL 单飞缓存。
        # 缓存键只含 trade_date/code 等稳定维度，不绑 context.updated_at——
        # 否则盘中每 5 秒刷新就全量失效，详情轮询等同冷启动。
        self._signal_detail_context_cache: dict[str, tuple[float, SignalDetailContext]] = {}
        self._signal_detail_context_cache_lock = threading.Lock()
        self._signal_detail_context_build_locks: dict[str, threading.Lock] = {}
        self._detail_minute_rows_cache: dict[str, tuple[float, SharedMinuteChartRows]] = {}
        self._detail_minute_rows_cache_lock = threading.Lock()
        self._detail_minute_rows_build_locks: dict[str, threading.Lock] = {}
        # 详情页公式bundle缓存：chart/overlay/monolith 共享同一份
        # 分钟归一化 + 做T公式序列 + 买卖标记，只算一次。
        self._detail_bundle_cache: dict[str, tuple[float, StockDetailBundle]] = {}
        self._detail_bundle_cache_lock = threading.Lock()
        self._detail_bundle_build_locks: dict[str, threading.Lock] = {}
        # 日K详情载荷缓存（公式+筹码+标签）：分钟级轮询削抖
        self._detail_daily_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._detail_daily_cache_lock = threading.Lock()
        self._detail_daily_build_locks: dict[str, threading.Lock] = {}
        # 流通市值快照缓存（板块中军口径用）：按（日期, 库文件 mtime）失效
        self._float_mcap_cache_key: tuple[str, int] | None = None
        self._float_mcap_cache: dict[str, float] = {}
        # 详情页单票公式行缓存：轨迹库文件大、行按时间交错落盘，单票 180 行
        # 实际是随机页读，盘中与后台批量读写叠加时可能卡数秒。数据本身每
        # background_collector_seconds 才更新一次，短 TTL 内存缓存即可。
        self._formula_rows_small_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        # legacy dashboard 视图共享载荷缓存：与 terminal 同一套失效口径
        self._dashboard_cache_by_key: dict[str, tuple[float, DashboardPayload]] = {}
        self._dashboard_cache_lock = threading.Lock()
        self._dashboard_build_locks: dict[str, threading.Lock] = {}

    def _build_state_store(self, settings: AppSettings) -> JsonStateStore | None:
        if settings.persistence_backend != "cloudbase_nosql":
            return None
        if not settings.cloudbase_env_id or not settings.cloudbase_api_token:
            return None
        return CloudBaseNoSqlStateStore(
            env_id=settings.cloudbase_env_id,
            token=settings.cloudbase_api_token,
            collection=settings.cloudbase_state_collection,
            instance=settings.cloudbase_database_instance,
            database=settings.cloudbase_database_name,
            base_url=settings.cloudbase_api_base_url or None,
            timeout=settings.cloudbase_api_timeout_seconds,
        )

    def _build_watchlist_store(self, settings: AppSettings) -> WatchlistStore:
        if self.state_store is not None:
            return CloudBackedWatchlistStore(settings.watchlist_file, self.state_store)
        return WatchlistStore(settings.watchlist_file)

    def _build_position_store(self, settings: AppSettings) -> PositionStore:
        if self.state_store is not None:
            return CloudBackedPositionStore(settings.position_file, self.state_store)
        return PositionStore(settings.position_file)

    def dashboard(
        self,
        sector: str | None = None,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> DashboardPayload:
        context = self._get_context()
        context = self._context_with_client_watchlist(context, client_watchlist)
        cache_key = "|".join(
            [
                "dashboard",
                self._context_signature(context),
                self._watchlist_signature(context.watchlist),
                str(self._normalize_sector(sector) or ""),
            ]
        )
        return self._cached_payload(
            self._dashboard_cache_by_key,
            self._dashboard_cache_lock,
            self._dashboard_build_locks,
            cache_key,
            self._payload_cache_ttl(context),
            lambda: self._payload_for_context(context, sector=sector),
            max_entries=8,
        )

    def dark_pool_payload(self) -> dict[str, Any]:
        """暗盘资金面板数据：只读缓存/本地库，绝不在请求路径发行情请求。"""
        payload = self.dark_pool_monitor.payload()
        policy = self._market_refresh_policy()
        payload["session"] = policy["market_session"]
        payload["is_trading_window"] = policy["is_trading_window"]
        payload["refresh_policy"] = policy
        return payload

    def terminal(
        self,
        sector: str | None = None,
        board_level: int | str = 3,
        sort: str = "activity",
        page: int = 1,
        page_size: int = 80,
        fast: bool = False,
        near_trend: bool = False,
        pin_buy: bool = False,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> TerminalPayload:
        """Build the dense terminal view without the legacy signal-card payload.

        载荷构建在所有 WS 连接/HTTP 请求间共享：冻结/静态期按 context 签名缓存，
        盘中按短 TTL（terminal_payload_live_cache_seconds，默认 1s）缓存，
        同一视图同一时刻最多一个线程在构建，其余连接直接复用结果。
        """
        context = self._get_context()
        context = self._context_with_client_watchlist(context, client_watchlist)
        level = normalize_board_level(board_level)
        cache_key = self._terminal_cache_key(context, sector, level, sort, page, page_size, near_trend, pin_buy)
        ttl = self._payload_cache_ttl(context)
        if (near_trend or pin_buy) and (ttl is None or ttl > 2.0):
            # 低吸线值/最近买点可能由后台或详情页补算：冻结/静态期也不能按
            # context 签名无限缓存，否则补算完成的结果永远不会进入载荷。
            ttl = 2.0
        if fast:
            payload = self._cached_payload(
                self._terminal_cache_by_key,
                self._terminal_cache_lock,
                self._terminal_build_locks,
                f"{cache_key}|fast",
                ttl,
                lambda: self._terminal_fast_payload_for_context(
                    context,
                    sector=sector,
                    board_level=level,
                    sort=sort,
                    page=page,
                    page_size=page_size,
                    near_trend=near_trend,
                    pin_buy=pin_buy,
                ),
                max_entries=24,
            )
        else:
            payload = self._cached_payload(
                self._terminal_cache_by_key,
                self._terminal_cache_lock,
                self._terminal_build_locks,
                cache_key,
                ttl,
                lambda: self._terminal_payload_for_context(
                    context,
                    sector=sector,
                    board_level=level,
                    sort=sort,
                    page=page,
                    page_size=page_size,
                    near_trend=near_trend,
                    pin_buy=pin_buy,
                ),
                max_entries=24,
            )
        return payload

    @staticmethod
    def _context_signature(context: DashboardContext) -> str:
        source_status = context.source_status
        return "|".join(
            [
                str(source_status.get("trade_date") or ""),
                str(source_status.get("updated_at") or source_status.get("clock_label") or context.market.updated_at or ""),
                str(source_status.get("active_source") or ""),
                str(context.snapshot.data_mode or ""),
                str(bool(context.market.frozen)),
                str(source_status.get("quote_count") or len(context.snapshot.quotes)),
                str(source_status.get("sector_count") or len(context.sectors)),
            ]
        )

    @staticmethod
    def _watchlist_signature(watchlist: list[WatchlistItem]) -> str:
        return ",".join(item.code for item in watchlist)

    @staticmethod
    def _normalize_client_watchlist(
        client_watchlist: list[WatchlistItem] | None,
    ) -> list[WatchlistItem] | None:
        if client_watchlist is None:
            return None
        normalized: list[WatchlistItem] = []
        seen: set[str] = set()
        for item in client_watchlist:
            code = str(getattr(item, "code", "") or "").strip().zfill(6)
            if len(code) != 6 or not code.isdigit() or code in seen:
                continue
            seen.add(code)
            normalized.append(
                item.model_copy(
                    update={
                        "code": code,
                        "name": str(getattr(item, "name", "") or "").strip(),
                        "themes": list(getattr(item, "themes", []) or []),
                        "core": bool(getattr(item, "core", False)),
                        "position": False,
                        "notes": str(getattr(item, "notes", "") or ""),
                    }
                )
            )
        return normalized

    def _context_with_client_watchlist(
        self,
        context: DashboardContext,
        client_watchlist: list[WatchlistItem] | None,
    ) -> DashboardContext:
        watchlist = self._normalize_client_watchlist(client_watchlist)
        if watchlist is None:
            return context
        position_by_code = {item.code: item for item in self.position_store.list_items()}
        signals_all = self._decorate_signals(context.signals_all, watchlist, position_by_code)
        return DashboardContext(
            watchlist=watchlist,
            themes=context.themes,
            snapshot=context.snapshot,
            market=context.market,
            sectors=context.sectors,
            sector_flow=context.sector_flow,
            signals_all=signals_all,
            core_watch=self._core_watch(signals_all, context.themes, watchlist),
            events=context.events,
            source_status=dict(context.source_status),
        )

    @staticmethod
    def _cached_payload(
        store: dict[str, tuple[float, Any]],
        store_lock: threading.Lock,
        build_locks: dict[str, threading.Lock],
        cache_key: str,
        ttl: float | None,
        builder: Callable[[], Any],
        max_entries: int,
    ) -> Any:
        """共享载荷缓存 + 按 key 单飞构建。

        ttl=None（冻结/静态期）时缓存随 key 中的 context 签名失效，不做时间
        过期；ttl 为数（盘中）时超过 TTL 即重建。命中返回深拷贝，调用方可以
        自由挂载连接级字段而不污染缓存。
        """
        now = time.time()
        with store_lock:
            cached = store.get(cache_key)
            if cached is not None and (ttl is None or now - cached[0] <= ttl):
                return cached[1].model_copy(deep=True)
            build_lock = build_locks.setdefault(cache_key, threading.Lock())
        with build_lock:
            # 双检：等待构建锁期间可能已有别的线程建好
            with store_lock:
                cached = store.get(cache_key)
                if cached is not None and (ttl is None or time.time() - cached[0] <= ttl):
                    return cached[1].model_copy(deep=True)
            payload = builder()
            with store_lock:
                store[cache_key] = (time.time(), payload.model_copy(deep=True))
                while len(store) > max_entries:
                    oldest = min(store, key=lambda key: store[key][0])
                    store.pop(oldest, None)
                    build_locks.pop(oldest, None)
            return payload

    @staticmethod
    def _cached_value(
        store: dict[str, tuple[float, Any]],
        store_lock: threading.Lock,
        build_locks: dict[str, threading.Lock],
        cache_key: str,
        ttl: float | None,
        builder: Callable[[], Any],
        max_entries: int,
    ) -> Any:
        """Shared short-TTL cache for read-only dataclasses / lists.

        Unlike `_cached_payload`, this helper does not deep-copy the cached
        object. It is only suitable for values treated as immutable by callers.
        """
        now = time.time()
        with store_lock:
            cached = store.get(cache_key)
            if cached is not None and (ttl is None or now - cached[0] <= ttl):
                return cached[1]
            build_lock = build_locks.setdefault(cache_key, threading.Lock())
        with build_lock:
            with store_lock:
                cached = store.get(cache_key)
                if cached is not None and (ttl is None or time.time() - cached[0] <= ttl):
                    return cached[1]
            value = builder()
            with store_lock:
                store[cache_key] = (time.time(), value)
                while len(store) > max_entries:
                    oldest = min(store, key=lambda key: store[key][0])
                    store.pop(oldest, None)
                    build_locks.pop(oldest, None)
            return value

    def _terminal_cache_key(
        self,
        context: DashboardContext,
        sector: str | None,
        board_level: int | str,
        sort: str,
        page: int,
        page_size: int,
        near_trend: bool = False,
        pin_buy: bool = False,
    ) -> str:
        return "|".join(
            [
                self._context_signature(context),
                self._watchlist_signature(context.watchlist),
                str(normalize_board_level(board_level)),
                str(self._normalize_sector(sector) or ""),
                str(sort or "activity"),
                str(max(1, int(page or 1))),
                str(max(1, int(page_size or 80))),
                "near_trend" if near_trend else "",
                "pin_buy" if pin_buy else "",
            ]
        )

    def _payload_cache_ttl(self, context: DashboardContext) -> float | None:
        """载荷共享缓存的过期口径。

        冻结/静态期返回 None：缓存随 key 中的 context 签名失效，不做时间过期。
        盘中 live 返回短 TTL（默认 1s）：所有连接共用同一次构建结果，避免每个
        WS 连接每秒各自重算整份载荷；代价是最多 ~1s 的额外延迟，与 WS 推送
        节奏（stream_live_interval_seconds 默认 1s）一致。
        """
        if context.market.frozen:
            return None
        source_status = {**context.snapshot.source_status, **context.source_status}
        live = bool(
            is_trading_window()
            and (
                context.snapshot.data_mode == "live"
                or context.snapshot.data_mode == "local_trajectory"
                or str(source_status.get("active_source") or "").startswith("easy_tdx")
                or str(source_status.get("active_source") or "").startswith("local_trajectory")
            )
        )
        if live:
            return float(self.settings.terminal_payload_live_cache_seconds)
        return None

    def stock_board(
        self,
        sector: str | None = None,
        board_level: int | str = 3,
        sort: str = "activity",
        page: int = 1,
        page_size: int = 80,
        near_trend: bool = False,
        pin_buy: bool = False,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> StockBoardPayload:
        context = self._get_context()
        context = self._context_with_client_watchlist(context, client_watchlist)
        return self._stock_board_payload_for_context(
            context,
            sector=sector,
            board_level=board_level,
            sort=sort,
            page=page,
            page_size=page_size,
            near_trend=near_trend,
            pin_buy=pin_buy,
        )

    def sector_rank(
        self,
        board_level: int | str = 3,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> list[SectorSnapshot]:
        context = self._get_context()
        context = self._context_with_client_watchlist(context, client_watchlist)
        level = normalize_board_level(board_level)
        board_context = self.data_source.fetch_board_context(level)
        official_boards_available = bool(board_context.available and board_context.sectors)
        if not official_boards_available:
            return context.sectors
        members_by_sector = self._board_members_by_sector(board_context)
        local_board_sectors = self._official_board_sectors_from_snapshot(context, board_context, members_by_sector)
        if local_board_sectors:
            return local_board_sectors
        return self._decorate_sector_ranks(board_context.sectors)

    def opening_decision(self, sector: str | None = None) -> OpeningDecisionPayload:
        """Return the current opening-window decision without limiting the scan.

        A sector parameter only narrows the displayed candidate/defense lists;
        the underlying market evaluation and counts remain full-market.
        """
        context = self._get_context()
        payload = self.opening_strategy.evaluate(
            trade_date=str(
                context.source_status.get("trade_date")
                or context.snapshot.source_status.get("trade_date")
                or china_now().strftime("%Y%m%d")
            ),
            clock_label=str(
                context.source_status.get("clock_label")
                or context.snapshot.source_status.get("clock_label")
                or context.market.updated_at
                or ""
            ),
            data_mode=context.snapshot.data_mode,
            frozen=bool(context.source_status.get("frozen", context.market.frozen)),
            quotes=context.snapshot.quotes,
            indices=context.snapshot.indices,
            market=context.market,
            sectors=context.sectors,
            sector_flow=context.sector_flow,
            signals=context.signals_all,
            watchlist=context.watchlist,
        )
        selected = self._normalize_sector(sector)
        if not selected:
            return payload
        candidates = [item for item in payload.top_candidates if item.sector == selected]
        defense = [item for item in payload.top_defense if item.sector == selected]
        return payload.model_copy(
            update={
                "selected_sector": selected,
                "top_candidates": candidates,
                "top_defense": defense,
            }
        )

    def signal_detail_chart(
        self,
        code: str,
        sector: str | None = None,
        trade_date: str | None = None,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> SignalDetailChartPayload:
        info = self._signal_detail_context(code, sector=sector, trade_date=trade_date, client_watchlist=client_watchlist)
        bundle = self._stock_detail_bundle(info)
        confluence_snapshot = self._confluence_snapshot(
            info,
            chart=bundle.chart,
            formula_state=bundle.formula_state,
        )
        summary = list(bundle.summary)
        if bundle.error:
            summary.append(f"分钟回放不可用：{bundle.error}")
        # 板块展示名统一走 _display_sector_name：内部行业代码映射成官方板块名
        sector_snapshot = info.sector_snapshot
        sector_display = self._display_sector_name(info.quote, sector_snapshot)
        if sector_snapshot is not None and sector_snapshot.name != sector_display:
            sector_snapshot = sector_snapshot.model_copy(update={"name": sector_display})
        signal = info.signal.model_copy(
            update={"risk_reward": self._zuot_risk_reward(info, bundle.formula_state)}
        )
        return SignalDetailChartPayload(
            code=info.quote.code,
            name=info.quote.name,
            sector=sector_display,
            trade_date=info.actual_trade_date,
            selected_sector=info.selected_sector,
            market=info.context.market,
            sector_snapshot=sector_snapshot,
            current_signal=signal,
            summary=summary,
            chart=bundle.chart,
            formula_overlay=bundle.formula_overlay,
            order_flow=info.quote.order_flow,
            watchlisted=info.watchlist_item is not None,
            watchlist_tags=list(info.signal.watchlist_tags),
            position=info.position,
            formula_state=bundle.formula_state,
            confluence_snapshot=confluence_snapshot,
        )

    def signal_detail_overlay(
        self,
        code: str,
        sector: str | None = None,
        trade_date: str | None = None,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> SignalDetailOverlayPayload:
        info = self._signal_detail_context(code, sector=sector, trade_date=trade_date, client_watchlist=client_watchlist)
        bundle = self._stock_detail_bundle(info)
        transaction_flow = self._fetch_transaction_flow(
            info.quote.code,
            info.actual_trade_date,
            full_session=True,
        )
        confluence_snapshot = self._confluence_snapshot(
            info,
            chart=bundle.chart,
            formula_state=bundle.formula_state,
            transaction_flow=transaction_flow,
        )
        return SignalDetailOverlayPayload(
            code=info.quote.code,
            name=info.quote.name,
            sector=self._display_sector_name(info.quote, info.sector_snapshot),
            trade_date=info.actual_trade_date,
            selected_sector=info.selected_sector,
            frozen=bool(info.context.market.frozen),
            markers=bundle.markers,
            transaction_flow=self._transaction_flow_summary(transaction_flow),
            formula_state=bundle.formula_state,
            confluence_snapshot=confluence_snapshot,
        )

    def signal_detail_extras(
        self,
        code: str,
        sector: str | None = None,
        trade_date: str | None = None,
        include_fundamentals: bool = False,
        include_capital_flow: bool = False,
        include_indicators: bool = False,
        include_chanlun: bool = False,
        include_auction_history: bool = True,
        include_messages: bool = True,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> SignalDetailExtrasPayload:
        info = self._signal_detail_context(code, sector=sector, trade_date=trade_date, client_watchlist=client_watchlist)
        loaders: dict[str, Callable[[], Any]] = {
            "analysis": lambda: self.analysis_store.load(info.quote.code, info.actual_trade_date),
        }
        if include_messages:
            # 星球消息证据要查 CloudBase（冷查询约 0.8-1.1s），是 extras 冷启动大头；
            # 详情页默认看筹码 tab，前端切到「星球消息」tab 时才带 include_messages=true。
            sector_terms = self._message_evidence_terms(
                info.quote,
                info.signal,
                info.sector_snapshot,
                info.selected_sector,
            )
            # message_status（大表 count=exact）前端详情页并不消费，不再随 extras 加载；
            # 需要时走 /api/messages/status（独立 300s 缓存）。
            loaders["message_evidence"] = lambda: self.message_store.evidence_for(
                code=info.quote.code,
                sector_terms=sector_terms,
            )
        if include_auction_history:
            loaders["auction_history"] = lambda: self.data_source.auction_history(
                info.quote.code,
                info.actual_trade_date,
            )
        if include_fundamentals:
            loaders["fundamentals"] = lambda: self.data_source.fetch_fundamentals(info.quote.code)
        if include_capital_flow:
            loaders["capital_flow"] = lambda: self.data_source.fetch_capital_flow(info.quote.code)
        if include_indicators:
            loaders["technical_indicators"] = lambda: self.data_source.fetch_technical_indicators(info.quote.code)
        if include_chanlun:
            loaders["chanlun"] = lambda: self.data_source.fetch_chanlun(info.quote.code)

        results: dict[str, Any] = {}
        if len(loaders) == 1:
            key, loader = next(iter(loaders.items()))
            results[key] = self._load_signal_detail_extra(key, loader, info)
        else:
            with ThreadPoolExecutor(max_workers=min(6, len(loaders))) as executor:
                future_by_key = {
                    executor.submit(self._load_signal_detail_extra, key, loader, info): key
                    for key, loader in loaders.items()
                }
                for future in as_completed(future_by_key):
                    key = future_by_key[future]
                    results[key] = future.result()

        analysis = results.get("analysis")
        message_evidence = results.get("message_evidence", MessageEvidenceBundle())
        auction_history = results.get("auction_history", [])
        fundamentals = results.get("fundamentals")
        capital_flow = results.get("capital_flow")
        technical_indicators = results.get("technical_indicators")
        chanlun = results.get("chanlun")
        payload = SignalDetailExtrasPayload(
            code=info.quote.code,
            name=info.quote.name,
            sector=self._display_sector_name(info.quote, info.sector_snapshot),
            trade_date=info.actual_trade_date,
            selected_sector=info.selected_sector,
            watchlisted=info.watchlist_item is not None,
            watchlist_tags=list(info.signal.watchlist_tags),
            position=info.position,
            auction_history=auction_history,
            message_evidence=message_evidence,
            analysis=analysis,
        )
        if fundamentals is not None:
            payload.fundamentals = fundamentals
        if capital_flow is not None:
            payload.capital_flow = capital_flow
        if technical_indicators is not None:
            payload.technical_indicators = technical_indicators
        if chanlun is not None:
            payload.chanlun = chanlun
        return payload

    def signal_detail_f10(self, code: str, refresh: bool = False) -> Any:
        """个股详情页 F10 聚合数据（tushare_pro + easy_tdx）。

        三层缓存：内存(1h) → 持久缓存(18h) → 实时拉取；refresh=True 强制实时拉
        并回写持久缓存。盘前另有定时增量预热（maybe_run_f10_preopen_refresh）。
        """
        return self.data_source.fetch_f10_full(code, force=refresh)

    # -- 日K详情（AI主力狙击公式 + 筹码峰 + 题材概念标签） ----------------------

    def signal_detail_daily(
        self,
        code: str,
        sector: str | None = None,
        trade_date: str | None = None,
        count: int = 240,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> dict[str, Any]:
        """日K详情载荷：日K线 + AI主力狙击三件套公式 + 筹码分布 + 题材概念标签。

        数据边界（AGENTS.md）：日K/板块标签走 easy_tdx；换手率用 tushare 本地
        daily_basic 流通股本快照（个股级数值，不参与板块分类）。
        """
        from app.chip_distribution import (
            compute_chip_distribution,
            compute_intraday_distribution,
        )
        from app.daily_formula_engine import DailyFormulaInput, compute_daily_formulas
        from app.stock_tags import classify_belong_boards

        info = self._signal_detail_context(code, sector=sector, trade_date=trade_date, client_watchlist=client_watchlist)
        quote = info.quote
        row_count = max(120, min(int(count or 240), 800))

        def build() -> dict[str, Any]:
            rows = self.data_source.fetch_daily_kline_rows(quote.code, count=row_count)
            float_shares = self._lookup_float_shares(quote.code)
            # 筹码峰固定取最近 250 根：日K 向前动态加载更多历史时筹码分布保持稳定，
            # 避免可视窗口扩张导致峰形漂移、数字前后不一致。
            chip = compute_chip_distribution(rows[-250:], float_shares=float_shares)

            index_code = self._index_code_for_stock(quote.code)
            index_close_map = (
                self.data_source.fetch_index_daily_close_map(index_code, count=row_count + 60)
                if index_code
                else {}
            )
            formula_input = DailyFormulaInput.from_rows(
                rows,
                float_shares=float_shares,
                index_close_by_date=index_close_map,
                winner_pct=chip.get("winner_pct") if chip.get("available") else None,
            )
            formulas = compute_daily_formulas(formula_input)

            minute_rows = self._shared_stock_chart_rows(info).rows
            chip_intraday = compute_intraday_distribution(
                minute_rows,
                prev_close=float(quote.prev_close or 0),
            )

            tags = self.stock_tag_store.get_or_fetch(
                quote.code,
                fetcher=lambda c: classify_belong_boards(
                    self.data_source.fetch_belong_board_rows(c), c
                ),
            )
            sector_display = self._display_sector_name(quote, info.sector_snapshot)

            return {
                "code": quote.code,
                "name": quote.name,
                "sector": sector_display,
                "trade_date": info.actual_trade_date,
                "prev_close": float(quote.prev_close or 0),
                "count": len(rows),
                "bars": [
                    {
                        "date": str(row.get("date") or ""),
                        "open": round(float(row.get("open") or 0), 3),
                        "high": round(float(row.get("high") or 0), 3),
                        "low": round(float(row.get("low") or 0), 3),
                        "close": round(float(row.get("close") or 0), 3),
                        "vol": round(float(row.get("vol") or 0), 1),
                        "amount": round(float(row.get("amount") or 0), 1),
                    }
                    for row in rows
                ],
                "formulas": formulas,
                "chip": chip,
                "chip_intraday": chip_intraday,
                "tags": {
                    "available": bool(tags.get("industry") or tags.get("concepts")),
                    "industry_official": sector_display if sector_display != "未归类" else "",
                    "industry": tags.get("industry") or "",
                    "concepts": tags.get("concepts") or [],
                    "styles": tags.get("styles") or [],
                    "regions": tags.get("regions") or [],
                    "source": tags.get("source") or "",
                    "stale": bool(tags.get("stale")),
                },
                "generated_at": china_now().isoformat(timespec="seconds"),
            }

        ttl = 20.0 if info.live_mode else 300.0
        cache_key = "|".join([quote.code, info.actual_trade_date, str(row_count)])
        return self._cached_value(
            self._detail_daily_cache,
            self._detail_daily_cache_lock,
            self._detail_daily_build_locks,
            cache_key,
            ttl,
            build,
            max_entries=16,
        )

    def _float_mcap_map(self) -> dict[str, float]:
        """最新一个交易日的流通市值（元），按 6 位代码索引。

        数据源：tushare daily_basic 本地快照（circ_mv，万元；个股级数值，
        不参与板块分类）。按（日期, 库文件 mtime）缓存——ingest 写库后
        mtime 变化即重载；库缺失/查询失败返回空表，调用方回退成交额口径。
        """
        db_file = self.settings.data_dir / "runtime" / "tushare_eod.sqlite"
        try:
            mtime = int(db_file.stat().st_mtime)
        except OSError:
            return {}
        cache_key = (china_now().strftime("%Y%m%d"), mtime)
        if self._float_mcap_cache_key == cache_key:
            return self._float_mcap_cache
        result: dict[str, float] = {}
        try:
            import sqlite3

            with sqlite3.connect(str(db_file), timeout=2) as conn:
                row = conn.execute("SELECT MAX(trade_date) FROM daily_basic").fetchone()
                latest = str(row[0]) if row and row[0] else ""
                if latest:
                    for ts_code, circ_mv in conn.execute(
                        "SELECT ts_code, circ_mv FROM daily_basic WHERE trade_date = ?",
                        (latest,),
                    ):
                        code = str(ts_code or "").split(".")[0].zfill(6)
                        try:
                            value = float(circ_mv) * 10_000.0  # 万元 → 元
                        except (TypeError, ValueError):
                            continue
                        if len(code) == 6 and code.isdigit() and value > 0:
                            result[code] = value
        except Exception:
            return {}
        self._float_mcap_cache_key = cache_key
        self._float_mcap_cache = result
        return result

    @staticmethod
    def _capacity_leader(metric_quotes: list[Quote], float_mcaps: dict[str, float]) -> Quote | None:
        """板块中军（市场"核心容量"口径）：成分股里流通市值第一。

        市值数据缺失时回退成交额第一（盘口代理口径）。
        """
        if not metric_quotes:
            return None
        with_cap = [quote for quote in metric_quotes if (float_mcaps.get(quote.code) or 0) > 0]
        if with_cap:
            return max(with_cap, key=lambda quote: float_mcaps[quote.code])
        return max(
            metric_quotes,
            key=lambda quote: (float(quote.amount or 0), quote.minute_amount_ratio, quote.change_pct),
        )

    def _lookup_float_shares(self, code: str) -> float | None:
        db_file = self.settings.data_dir / "runtime" / "tushare_eod.sqlite"
        if not db_file.exists():
            return None
        ts_code = f"{code}.{'SH' if code.startswith(('6', '5', '9')) else 'BJ' if code.startswith(('4', '8', '92')) else 'SZ'}"
        try:
            import sqlite3

            with sqlite3.connect(str(db_file), timeout=2) as conn:
                row = conn.execute(
                    "SELECT float_share FROM daily_basic WHERE ts_code = ? ORDER BY trade_date DESC LIMIT 1",
                    (ts_code,),
                ).fetchone()
        except Exception:
            return None
        if not row or row[0] in (None, ""):
            return None
        try:
            value = float(row[0])  # 万股
        except (TypeError, ValueError):
            return None
        return value * 10_000.0 if value > 0 else None

    @staticmethod
    def _index_code_for_stock(code: str) -> str | None:
        """个股对应的大盘指数（公式 INDEXC）：沪市→上证指数，深市→深证成指；北交所暂不映射。"""
        if code.startswith(("6", "5", "9")):
            return "000001"
        if code.startswith(("0", "3", "2")):
            return "399001"
        return None

    # -- F10 盘前增量预热 -----------------------------------------------------

    def _f10_preopen_candidates(self, max_codes: int) -> list[str]:
        """候选池：自选 + 持仓 + 持久缓存索引里的近期查看股票，去重限流。"""
        codes: list[str] = []
        seen: set[str] = set()

        def push(raw: Any) -> None:
            text = str(raw or "").strip()
            if not text:
                return
            code = text.zfill(6)
            if len(code) == 6 and code.isdigit() and code not in seen:
                seen.add(code)
                codes.append(code)

        try:
            for item in self.watchlist_store.list_items():
                push(item.code)
            for item in self.position_store.list_items():
                push(item.code)
        except Exception as exc:
            logger.warning("f10 preopen candidates from stores failed: %r", exc)
        index = self.data_source.f10_cache_index()
        for code, _ in sorted(index.items(), key=lambda kv: kv[1], reverse=True):
            push(code)
        return codes[:max_codes]

    def maybe_run_f10_preopen_refresh(self, *, reason: str = "daily_collector") -> dict[str, Any]:
        """每日盘前窗口（默认 08:40 起 25 分钟内，周一至周五）触发一轮 F10 增量预热。

        只刷缓存缺失或超过 f10_preopen_stale_seconds 的股票；每天最多一轮。
        挂在 collect_once 上，与 trajectory 每日清理同一模式，不占 5s 行情循环。
        """
        settings = self.settings
        if not getattr(settings, "f10_preopen_refresh", True):
            return {"scheduled": False, "skipped": "disabled_by_settings", "reason": reason}
        now = china_now()
        if now.weekday() >= 5:
            return {"scheduled": False, "skipped": "weekend", "reason": reason}
        try:
            hour_text, minute_text = str(settings.f10_preopen_time).split(":", 1)
            start_minutes = int(hour_text) * 60 + int(minute_text)
        except (ValueError, AttributeError):
            start_minutes = 8 * 60 + 40
        current_minutes = now.hour * 60 + now.minute
        window = int(getattr(settings, "f10_preopen_window_minutes", 25))
        if not (start_minutes <= current_minutes < start_minutes + window):
            return {"scheduled": False, "skipped": "outside_preopen_window", "reason": reason}
        day_key = now.strftime("%Y%m%d")
        with self._f10_preopen_lock:
            if self._last_f10_preopen_day == day_key:
                return {"scheduled": False, "skipped": "already_refreshed_today", "reason": reason}
            if self._f10_preopen_thread is not None and self._f10_preopen_thread.is_alive():
                return {"scheduled": False, "skipped": "refresh_in_progress", "reason": reason}

            def run() -> None:
                try:
                    candidates = self._f10_preopen_candidates(int(settings.f10_preopen_max_codes))
                    stats = self.data_source.refresh_f10_stale(
                        candidates,
                        max_age_seconds=int(settings.f10_preopen_stale_seconds),
                        limit=int(settings.f10_preopen_max_codes),
                    )
                    # 资金流/技术指标/缠论同为日频数据，随同一轮预热增量刷新；
                    # MacClient 串行拉取较慢，候选收敛到前 30 只（自选+持仓优先）。
                    extras_stats = self.data_source.refresh_detail_extras_stale(
                        candidates[:30],
                        max_age_seconds=int(settings.f10_preopen_stale_seconds),
                        limit=30,
                    )
                    self._last_f10_preopen = {
                        **stats,
                        "extras": extras_stats,
                        "day_key": day_key,
                        "reason": reason,
                    }
                    logger.info("f10 preopen refresh done: %s", self._last_f10_preopen)
                except Exception as exc:  # pragma: no cover - 防御性兜底
                    self._last_f10_preopen = {"error": str(exc)[:200], "day_key": day_key}
                    logger.warning("f10 preopen refresh error: %r", exc)
                finally:
                    with self._f10_preopen_lock:
                        self._last_f10_preopen_day = day_key

            self._f10_preopen_thread = threading.Thread(
                target=run,
                name="f10-preopen-refresh",
                daemon=True,
            )
            self._f10_preopen_thread.start()
        return {"scheduled": True, "reason": reason, "day_key": day_key}

    def _load_signal_detail_extra(
        self,
        key: str,
        loader: Callable[[], Any],
        info: SignalDetailContext,
    ) -> Any:
        try:
            return loader()
        except AssertionError:
            raise
        except Exception as exc:
            logger.warning(
                "signal detail extras loader failed: key=%s code=%s trade_date=%s error=%r",
                key,
                info.quote.code,
                info.actual_trade_date,
                exc,
                exc_info=True,
            )
            return self._signal_detail_extra_fallback(key)

    def _signal_detail_extra_fallback(self, key: str) -> Any:
        if key == "message_evidence":
            return MessageEvidenceBundle()
        if key == "message_status":
            return MessageStoreStatus(
                db_file=str(getattr(self.message_store, "db_file", "")),
                ingest_enabled=bool(self.settings.message_ingest_token),
            )
        if key == "auction_history":
            return []
        return None

    def message_detail(self, event_id: str) -> MessageDetailPayload:
        payload = self.message_store.message_detail(
            event_id,
            ingest_enabled=bool(self.settings.message_ingest_token),
        )
        if payload is None:
            raise ValueError(f"未找到消息事件 {event_id}")
        return payload

    def signal_detail(
        self,
        code: str,
        sector: str | None = None,
        trade_date: str | None = None,
        fast: bool = False,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> SignalReplayDetail:
        current_context = self._get_context()
        current_context = self._context_with_client_watchlist(current_context, client_watchlist)
        current_trade_date = str(current_context.source_status.get("trade_date") or "")
        actual_trade_date = trade_date or current_trade_date or china_now().strftime("%Y%m%d")
        context = self._context_for_trade_date(current_context, actual_trade_date)
        context = self._context_with_client_watchlist(context, client_watchlist)
        quote = self._quote_for_code(context.snapshot.quotes, code)
        if quote is None:
            raise ValueError(f"未找到 {code} 的行情数据")

        signal = self._signal_for_code(context.signals_all, code)
        if signal is None:
            raise ValueError(f"未找到 {code} 的信号数据")

        requested_sector = self._normalize_sector(sector)
        sector_snapshot = self._best_sector_for_quote(
            quote,
            context.sectors,
            preferred_sector_names=self._manual_theme_names(context.themes),
            requested_sector=requested_sector,
        )
        selected_sector = sector_snapshot.name if sector_snapshot else requested_sector
        position = self.position_store.get(code)
        transaction_flow = (
            TransactionFlowObservation(
                trade_date=actual_trade_date,
                note="快速分时响应暂不读取逐笔成交，完整详情随后补充",
            )
            if fast
            else self._fetch_transaction_flow(
                code,
                actual_trade_date,
                full_session=True,
            )
        )
        detail_quote = quote
        if transaction_flow.available:
            detail_quote = quote.model_copy(
                update={
                    "order_flow": self._merge_transaction_order_flow(
                        quote.order_flow,
                        transaction_flow,
                    )
                }
            )
        if sector_snapshot is not None:
            scoped_quotes = [
                detail_quote if item.code == detail_quote.code else item
                for item in context.snapshot.quotes
            ]
            scoped_formula_rows = self._formula_rows_by_code_for_context(
                trade_date=actual_trade_date,
                quotes=[detail_quote],
                watchlist=[
                    WatchlistItem(
                        code=quote.code,
                        name=quote.name,
                        themes=list(quote.themes),
                        core=quote.core,
                    )
                ],
                positions={quote.code: position} if position else {},
            )
            scoped_signals = self.engine.build_signals(
                scoped_quotes,
                [
                    WatchlistItem(
                        code=quote.code,
                        name=quote.name,
                        themes=list(quote.themes),
                        core=quote.core,
                    )
                ],
                context.sectors,
                self._market_for_signals(context.snapshot, context.market),
                clock_label=str(context.source_status.get("clock_label") or context.market.updated_at or ""),
                preferred_sector_names={sector_snapshot.name},
                positions={quote.code: position} if position else {},
                formula_rows_by_code=scoped_formula_rows,
                sector_name_mapper=self._display_sector_name,
            )
            if scoped_signals:
                scoped_signal = scoped_signals[0]
                signal = scoped_signal.model_copy(
                    update={
                        "pinned": signal.pinned,
                        "watchlist_tags": list(signal.watchlist_tags),
                    }
                )
        live_mode = context.snapshot.data_mode == "live" or context.source_status.get("active_source") == "easy_tdx"
        detail_info = SignalDetailContext(
            context=context,
            actual_trade_date=actual_trade_date,
            quote=detail_quote,
            signal=signal,
            sector_snapshot=sector_snapshot,
            selected_sector=selected_sector,
            position=position,
            watchlist_item=None,
            live_mode=live_mode,
        )
        bundle = self._stock_detail_bundle(detail_info)
        minute_error = bundle.error
        replay_points, markers, signal_timeline, summary = self._zuot_replay(
            detail_info,
            bundle,
            transaction_flow=transaction_flow,
        )
        decision_markers = list(markers)
        chart_series = bundle.chart
        formula_state = bundle.formula_state
        confluence_snapshot = self._confluence_snapshot(
            detail_info,
            chart=chart_series,
            formula_state=formula_state,
            transaction_flow=transaction_flow,
        )
        if decision_markers:
            latest_decision = decision_markers[-1]
            signal = signal.model_copy(
                update={
                    "signal": latest_decision.signal,
                    "score": latest_decision.score,
                    "reasons": list(latest_decision.reasons),
                    "risks": list(latest_decision.risks),
                    "factor_flags": list(latest_decision.factor_flags),
                    "signal_grade": latest_decision.signal_grade,
                    "factor_scores": dict(latest_decision.factor_scores),
                    "exit_score": latest_decision.exit_score,
                    "confluence_window_bars": latest_decision.confluence_window_bars,
                    "t_plus_one_restricted": latest_decision.t_plus_one_restricted,
                    "action_size_pct": latest_decision.action_size_pct,
                    "direction": latest_decision.direction,
                    "action": latest_decision.action,
                    "setup": latest_decision.setup,
                    "regime": latest_decision.regime,
                    "executable": latest_decision.executable,
                    "execution_reason": latest_decision.execution_reason,
                    "evidence_sequence": list(latest_decision.evidence_sequence),
                    "validation_status": latest_decision.validation_status,
                    "hypothesis_id": latest_decision.hypothesis_id,
                    "strategy_version": latest_decision.strategy_version,
                    "phase": latest_decision.phase,
                    "invalidation_price": latest_decision.invalidation_price,
                    "source_quality": latest_decision.source_quality,
                    "risk_reward": latest_decision.risk_reward,
                }
            )
            summary.append(
                f"做T公式：{latest_decision.time} {latest_decision.action} · {latest_decision.setup}"
            )
            if not fast and self.settings.trajectory_enabled:
                for decision_marker in decision_markers:
                    try:
                        self.trajectory_store.record_strategy_event(
                            trade_date=actual_trade_date,
                            time=decision_marker.time,
                            code=quote.code,
                            marker=decision_marker,
                        )
                    except Exception:
                        # Replay persistence is observational and must not slow
                        # or fail the detail response.
                        pass
        if minute_error:
            summary.append(f"分钟回放不可用：{minute_error}")
        if transaction_flow.available:
            summary.append(
                f"L1逐笔成交流：{transaction_flow.count}笔，方向成交额差"
                f" {transaction_flow.imbalance_pct:+.1f}%，大额差"
                f" {transaction_flow.large_imbalance_pct:+.1f}%"
            )

        watchlist_item = self._watchlist_item_for_code(context.watchlist, code)
        analysis = None if fast else self.analysis_store.load(code, actual_trade_date)
        message_evidence = MessageEvidenceBundle() if fast else self.message_store.evidence_for(
            code=quote.code,
            sector_terms=self._message_evidence_terms(quote, signal, sector_snapshot, selected_sector),
        )
        message_status = None if fast else self.message_store.status(
            ingest_enabled=bool(self.settings.message_ingest_token)
        )
        auction_history = self.data_source.auction_history(quote.code, actual_trade_date)
        return SignalReplayDetail(
            code=quote.code,
            name=quote.name,
            sector=signal.sector,
            trade_date=actual_trade_date,
            selected_sector=selected_sector,
            market=context.market,
            sector_snapshot=sector_snapshot,
            current_signal=signal,
            replay_points=replay_points,
            signal_timeline=signal_timeline,
            markers=markers,
            summary=summary,
            analysis=analysis,
            message_evidence=message_evidence,
            message_status=message_status,
            auction_history=auction_history,
            watchlisted=watchlist_item is not None,
            watchlist_tags=list(signal.watchlist_tags),
            order_flow=detail_quote.order_flow,
            transaction_flow=transaction_flow,
            position=position,
            decision_markers=decision_markers,
            formula_state=formula_state,
            confluence_snapshot=confluence_snapshot,
        )

    def index_detail(
        self,
        code: str,
        trade_date: str | None = None,
    ) -> IndexReplayDetail:
        current_context = self._get_context()
        current_trade_date = str(current_context.source_status.get("trade_date") or "")
        actual_trade_date = trade_date or current_trade_date or china_now().strftime("%Y%m%d")
        context = self._context_for_trade_date(current_context, actual_trade_date)
        index = self._index_for_code(self._indices_for_minute_series(context), code)
        if index is None:
            raise ValueError(f"未找到 {code} 的大盘指数数据")

        live_mode = context.snapshot.data_mode == "live" or context.source_status.get("active_source") == "easy_tdx"
        minute_error: str | None = None
        try:
            minute_rows = self._merge_live_index_tail(
                self.data_source.fetch_index_minute_series(index.code, actual_trade_date, live=bool(live_mode)),
                index=index,
                market=context.market,
                live=bool(live_mode),
            )
        except Exception as exc:
            minute_rows = []
            minute_error = str(exc)

        replay_points = self._index_replay_points(index, minute_rows)
        summary = self._index_replay_summary(index, replay_points)
        if minute_error:
            summary.append(f"大盘分钟回放不可用：{minute_error}")

        return IndexReplayDetail(
            code=index.code,
            name=index.name,
            trade_date=actual_trade_date,
            market=context.market,
            current_index=index,
            replay_points=replay_points,
            markers=[],
            summary=summary,
        )

    def index_minutes(self, trade_date: str | None = None) -> dict[str, Any]:
        """轻量的多指数分钟涨跌幅序列：用于指数拐头共振图。

        复用缓存的指数分钟线（_fetch_cached_minute_series），盘中 10 秒级轮询
        不会触发额外的全市场抓取。
        """
        current_context = self._get_context()
        current_trade_date = str(current_context.source_status.get("trade_date") or "")
        actual_trade_date = trade_date or current_trade_date or china_now().strftime("%Y%m%d")
        context = self._context_for_trade_date(current_context, actual_trade_date)
        live_mode = context.snapshot.data_mode == "live" or context.source_status.get("active_source") == "easy_tdx"

        indices: list[dict[str, Any]] = []
        for index in self._indices_for_minute_series(context):
            try:
                rows = self._merge_live_index_tail(
                    self.data_source.fetch_index_minute_series(index.code, actual_trade_date, live=bool(live_mode)),
                    index=index,
                    market=context.market,
                    live=bool(live_mode),
                )
            except Exception:
                rows = []
            fallback_times = self.engine._session_times(len(rows))
            prev_close = self._index_prev_close_for_minutes(index, rows)
            points: list[dict[str, Any]] = []
            last_price = float(index.price or 0)
            for idx, row in enumerate(rows):
                price = float(row.get("price") or 0)
                if not isfinite(price) or price <= 0:
                    price = last_price
                if price <= 0:
                    continue
                last_price = price
                change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0
                points.append(
                    {
                        "time": str(row.get("time") or (fallback_times[idx] if idx < len(fallback_times) else ""))[:5],
                        "change_pct": round(change_pct, 2),
                        "vol": round(max(float(row.get("vol") or 0), 0), 0),
                    }
                )
            latest_change_pct = float(index.change_pct or 0)
            if latest_change_pct == 0 and points:
                latest_change_pct = float(points[-1].get("change_pct") or 0)
            indices.append(
                {
                    "code": index.code,
                    "name": index.name,
                    "change_pct": round(latest_change_pct, 2),
                    "rebound_from_low_pct": round(float(index.rebound_from_low_pct or 0), 2),
                    "points": points,
                }
            )

        return {
            "trade_date": actual_trade_date,
            "index_turning": bool(context.market.index_turning),
            "index_turning_mode": str(getattr(context.market, "index_turning_mode", "") or ""),
            "index_slope_pct": float(getattr(context.market, "index_slope_pct", 0.0) or 0.0),
            "amount_expanding": bool(context.market.amount_expanding),
            "indices": indices,
            "market_session": market_session(),
            "is_trading_window": is_trading_window(),
            "refresh_policy": self._market_refresh_policy(),
        }

    def analyze_watchlist_item(
        self,
        code: str,
        sector: str | None = None,
        trade_date: str | None = None,
    ) -> AnalysisRecord:
        detail = self.signal_detail(code, sector=sector, trade_date=trade_date)
        source = self._analysis_source(detail)
        try:
            ai_result = self.ai_client.analyze(source)
            explained_result = self._lock_analysis_decision(
                detail,
                ai_result.get("result") if isinstance(ai_result, dict) else {},
            )
            record = AnalysisRecord(
                code=detail.code,
                name=detail.name,
                trade_date=detail.trade_date,
                generated_at=ai_result["generated_at"],
                provider=ai_result["provider"],
                model=ai_result.get("model"),
                status=str(ai_result.get("status") or "ok"),
                source=source,
                result=explained_result,
                raw_text=ai_result["raw_text"],
            )
        except Exception as exc:
            record = AnalysisRecord(
                code=detail.code,
                name=detail.name,
                trade_date=detail.trade_date,
                generated_at=china_now().isoformat(timespec="seconds"),
                provider="local_rules",
                model=None,
                status="fallback",
                source=source,
                result=self._fallback_analysis(detail),
                raw_text=str(exc),
            )
        self.analysis_store.save(record)
        return record

    def load_analysis(self, code: str, trade_date: str | None = None) -> AnalysisRecord | None:
        return self.analysis_store.load(code, trade_date)

    def cleanup_trajectory_history(self) -> dict[str, Any]:
        if not self.settings.trajectory_cleanup_on_start:
            self._last_trajectory_cleanup = {"skipped": "disabled_by_settings", "deleted_rows": 0}
            return dict(self._last_trajectory_cleanup)
        cleanup = getattr(self.trajectory_store, "cleanup_high_frequency_history", None)
        if cleanup is None:
            self._last_trajectory_cleanup = {"skipped": "store_does_not_support_cleanup", "deleted_rows": 0}
            return dict(self._last_trajectory_cleanup)
        try:
            self._last_trajectory_cleanup = cleanup(
                retain_trade_days=self.settings.trajectory_retention_trade_days,
                truncate_wal=True,
            )
        except Exception as exc:
            self._last_trajectory_cleanup = {"error": str(exc), "deleted_rows": 0}
        return dict(self._last_trajectory_cleanup)

    def cleanup_trajectory_history_once_per_day(self, *, day_key: str | None = None) -> dict[str, Any]:
        key = str(day_key or china_now().strftime("%Y%m%d"))
        with self._trajectory_cleanup_lock:
            if self._last_trajectory_cleanup_day == key:
                return {
                    **dict(self._last_trajectory_cleanup or {}),
                    "skipped": "already_cleaned_today",
                    "deleted_rows": 0,
                    "day_key": key,
                }
        result = self.cleanup_trajectory_history()
        self._last_trajectory_cleanup = {**result, "day_key": key}
        if "error" not in result:
            with self._trajectory_cleanup_lock:
                self._last_trajectory_cleanup_day = key
        return dict(self._last_trajectory_cleanup)

    def start_trajectory_cleanup_thread(self, *, reason: str = "scheduled") -> dict[str, Any]:
        if not self.settings.trajectory_cleanup_on_start:
            return {"scheduled": False, "skipped": "disabled_by_settings", "reason": reason}
        if is_trading_window():
            return {"scheduled": False, "skipped": "trading_window", "reason": reason}
        day_key = china_now().strftime("%Y%m%d")
        with self._trajectory_cleanup_lock:
            if self._last_trajectory_cleanup_day == day_key:
                return {"scheduled": False, "skipped": "already_cleaned_today", "reason": reason}
            if self._trajectory_cleanup_thread is not None and self._trajectory_cleanup_thread.is_alive():
                return {"scheduled": False, "skipped": "cleanup_in_progress", "reason": reason}

            def run() -> None:
                self.cleanup_trajectory_history_once_per_day(day_key=day_key)

            self._trajectory_cleanup_thread = threading.Thread(
                target=run,
                name="intraday-watchtower-cleanup",
                daemon=True,
            )
            self._trajectory_cleanup_thread.start()
        return {"scheduled": True, "reason": reason}

    def ingest_zsxq_messages(self, payload: ZsxqMessageIngestRequest) -> ZsxqMessageIngestResponse:
        response = self.message_store.upsert_messages(payload)
        # 首次同步后自举一次全量预建（物化表为空时），之后每次同步只增量刷新受影响实体。
        self._maybe_prebuild_message_evidence()
        return response

    def _maybe_prebuild_message_evidence(self) -> None:
        if getattr(self, "_message_evidence_prebuild_checked", False):
            return
        self._message_evidence_prebuild_checked = True
        if not self.message_store.available or self.message_store.evidence_cache_has_rows("sector"):
            return
        self.prebuild_message_evidence()

    def prebuild_message_evidence(self) -> dict[str, Any]:
        """后台全量预建物化证据：全部申万一/二/三级板块词 + 自选股/持仓个股。

        一次性把读路径的 like 全表扫描收敛到后台，之后每只股票的首次详情
        加载都是物化表上的 1~2 次索引查询。已在运行时不重复触发。
        """
        lock = getattr(self, "_message_evidence_prebuild_lock", None)
        if lock is None:
            lock = self._message_evidence_prebuild_lock = threading.Lock()
        if not lock.acquire(blocking=False):
            return {"ok": False, "status": "already_running"}

        def run() -> None:
            try:
                sector_terms = sorted(
                    {
                        str(name or "").strip()
                        for level in (1, 2, 3)
                        for name in self._stock_board_display_map_for_level(level).values()
                        if str(name or "").strip()
                    }
                )
                stock_codes = sorted(
                    {
                        str(item.code).zfill(6)
                        for item in [*self.list_watchlist(), *self.list_positions()]
                        if str(item.code or "").strip()
                    }
                )
                result = self.message_store.refresh_entities(
                    stock_codes=stock_codes,
                    sector_terms=sector_terms,
                )
                logger.info(
                    "message evidence prebuild finished: stocks=%d sectors=%d terms=%d",
                    result.get("stock", 0),
                    result.get("sector", 0),
                    len(sector_terms),
                )
            except Exception as exc:
                logger.warning("message evidence prebuild failed: error=%r", exc, exc_info=True)
            finally:
                lock.release()

        threading.Thread(target=run, name="message-evidence-prebuild", daemon=True).start()
        return {"ok": True, "status": "started"}

    def message_status(self) -> dict[str, Any]:
        try:
            status = self.message_store.status(ingest_enabled=bool(self.settings.message_ingest_token))
        except AssertionError:
            raise
        except Exception as exc:
            logger.warning("message status loader failed: error=%r", exc, exc_info=True)
            status = MessageStoreStatus(
                db_file=str(getattr(self.message_store, "db_file", "")),
                ingest_enabled=bool(self.settings.message_ingest_token),
            )
        return status.model_dump(mode="json")

    def market_capabilities(self) -> dict[str, Any]:
        now = china_now()
        return {
            **self.data_source.capabilities(),
            "server_clock": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_session": market_session(now),
            "is_trading_window": is_trading_window(now),
            "refresh_policy": self._market_refresh_policy(now),
        }

    def _market_refresh_policy(self, now: Any | None = None) -> dict[str, Any]:
        return market_refresh_policy(
            now,
            live_interval_seconds=self.settings.stream_live_interval_seconds,
            static_interval_seconds=self.settings.stream_static_interval_seconds,
        )

    def _public_market_capability_status(self) -> dict[str, Any]:
        """Add non-secret source capability labels to dashboard payloads."""
        capabilities = self.data_source.capabilities()
        now = china_now()
        policy = self._market_refresh_policy(now)
        return {
            "market_session": policy["market_session"],
            "is_trading_window": policy["is_trading_window"],
            "refresh_policy": policy,
            "quote_capability": str(capabilities.get("quote_protocol") or ""),
            "quote_depth": bool(capabilities.get("quote_depth")),
            "order_book_capability": (
                "quote_depth"
                if capabilities.get("quote_depth")
                else "unavailable"
            ),
            "transaction_tape": bool(capabilities.get("transaction_tape") or capabilities.get("transaction_data")),
            "transaction_data": bool(capabilities.get("transaction_data") or capabilities.get("transaction_tape")),
            "transaction_data_note": str(
                capabilities.get("transaction_tape_note")
                or capabilities.get("transaction_data_note")
                or ""
            ),
            "level2_available": bool(capabilities.get("level2_available")),
            "level2_note": str(capabilities.get("level2_note") or ""),
            "auction_series": bool(capabilities.get("auction_series")),
            "auction_0925": bool(capabilities.get("auction_0925")),
            "auction_proxy": bool(capabilities.get("auction_proxy")),
            "auction_capability_note": str(capabilities.get("auction_note") or ""),
        }

    def auction_history(self, code: str, trade_date: str | None = None) -> dict[str, Any]:
        return {
            "code": code,
            "trade_date": trade_date or china_now().strftime("%Y%m%d"),
            "source": self.data_source.capabilities(),
            "items": self.data_source.auction_history(code, trade_date=trade_date),
        }

    def transaction_flow(
        self,
        code: str,
        trade_date: str | None = None,
        count: int | None = None,
    ) -> TransactionFlowObservation:
        return self.data_source.fetch_transaction_flow(
            code,
            trade_date=trade_date,
            count=count,
        )

    def list_watchlist(self) -> list[WatchlistItem]:
        return self.watchlist_store.list_items()

    def search_stocks(
        self,
        query: str,
        limit: int = 12,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> list[dict[str, Any]]:
        normalized = str(query or "").strip()
        if not normalized:
            return []
        bounded_limit = max(1, min(int(limit or 12), 50))
        context = self._context_cache or self._get_context()
        context = self._context_with_client_watchlist(context, client_watchlist)
        watchlist_codes = {item.code for item in context.watchlist}
        position_by_code = {item.code: item for item in self.position_store.list_items()}
        signal_by_code = {signal.code: signal for signal in context.signals_all}
        theme_core_codes = {
            str(code).zfill(6)
            for theme in context.themes
            for code in theme.get("core_codes", [])
        }
        preferred_sector_names = self._manual_theme_names(context.themes)

        def match_rank(quote: Quote) -> int | None:
            code = quote.code
            name = quote.name or ""
            if code == normalized:
                return 0
            if code.startswith(normalized):
                return 1
            if name.startswith(normalized):
                return 2
            if normalized in code:
                return 3
            if normalized in name:
                return 4
            return None

        matches: list[tuple[int, Quote, str, SectorSnapshot | None]] = []
        for quote in context.snapshot.quotes:
            rank = match_rank(quote)
            if rank is None:
                continue
            sector = self._best_sector_for_quote(
                quote,
                context.sectors,
                preferred_sector_names=preferred_sector_names,
            )
            matches.append((rank, quote, self._display_sector_name(quote, sector), sector))

        matches.sort(
            key=lambda entry: (
                entry[0],
                1 if entry[1].code in watchlist_codes else 0,
                -float(entry[1].amount or 0),
                entry[1].code,
            )
        )
        results: list[dict[str, Any]] = []
        for _, quote, _, sector_snapshot in matches[:bounded_limit]:
            row = self._stock_board_item(
                quote,
                sector_snapshot,
                context.sectors,
                None,
                theme_core_codes,
                signal=signal_by_code.get(quote.code),
                watch_item=next((item for item in context.watchlist if item.code == quote.code), None),
                position_item=position_by_code.get(quote.code),
            ).model_dump(mode="json")
            row["source"] = "current_context"
            results.append(row)
        return results

    def upsert_watchlist(self, item: WatchlistItem) -> WatchlistItem:
        saved = self.watchlist_store.upsert(item)
        self._patch_cached_watchlist(saved)
        self._invalidate_context()
        return saved

    def delete_watchlist(self, code: str) -> bool:
        deleted = self.watchlist_store.delete(code)
        if deleted:
            normalized = str(code or "").strip().zfill(6)
            cache = self._context_cache
            if cache is not None:
                cache.watchlist = [entry for entry in cache.watchlist if entry.code != normalized]
            self._invalidate_context()
        return deleted

    def _patch_cached_watchlist(self, item: WatchlistItem) -> None:
        """就地更新缓存上下文里的自选列表。

        后台全量刷新一轮要数秒；写自选后先把缓存里的 watchlist 改掉，
        dashboard 下一次读取立即一致，不用等下一轮刷新。信号装饰等
        衍生数据仍由后台刷新补齐。
        """
        cache = self._context_cache
        if cache is None:
            return
        for index, existing in enumerate(cache.watchlist):
            if existing.code == item.code:
                cache.watchlist[index] = item
                return
        cache.watchlist.append(item)

    def list_positions(self) -> list[PositionRecord]:
        return self.position_store.list_items()

    def upsert_position(self, item: PositionRecord) -> PositionRecord:
        saved = self.position_store.upsert(item)
        self._invalidate_context()
        return saved

    def delete_position(self, code: str) -> bool:
        deleted = self.position_store.delete(code)
        if deleted:
            self._invalidate_context()
        return deleted

    def collect_once(self) -> dict[str, Any]:
        """Refresh and persist one backend-owned intraday observation."""
        self.start_trajectory_cleanup_thread(reason="daily_collector")
        self.maybe_run_f10_preopen_refresh(reason="daily_collector")
        if self._context_cache:
            active_source = str(self._context_cache.source_status.get("active_source") or "")
            if active_source == "local_trajectory_bootstrap" or (
                self._context_cache.market.frozen and not is_trading_window()
            ):
                skipped = "local_trajectory_bootstrap" if active_source == "local_trajectory_bootstrap" else "market_frozen"
                return {**self.trajectory_store.status(), "skipped": skipped}
        context = self._get_context()
        active_source = str(context.source_status.get("active_source") or "")
        if active_source == "local_trajectory_bootstrap":
            return {**self.trajectory_store.status(), "skipped": "local_trajectory_bootstrap"}
        self._record_intraday_context(context, self.position_store.list_items())
        return {
            **self.trajectory_store.status(),
            "trade_date": str(context.source_status.get("trade_date") or ""),
            "captured_at": str(context.source_status.get("clock_label") or context.market.updated_at),
            "frozen": context.market.frozen,
        }

    def close(self) -> None:
        self.trajectory_store.close()
        self.message_store.close()
        pool = getattr(self, "push_pool", None)
        if pool is not None:
            pool.close()

    @staticmethod
    def _looks_like_board_code(value: str) -> bool:
        """X410302 / X3006 这类内部行业代码：单字母 + 4~6 位数字。"""
        text = str(value or "").strip()
        return 5 <= len(text) <= 7 and text[0].isalpha() and text[1:].isdigit()

    def _stock_board_display_map_for_level(self, level: int = 3) -> dict[str, str]:
        """个股 → 官方板块名称（easy_tdx 申万 1/2/3 级）。

        复用 easy_tdx 官方板块成员缓存（本就是热数据），每级独立 300s TTL 兜底。
        """
        level = level if level in {1, 2, 3} else 3
        now = time.time()
        cache = self._stock_board_name_map_by_level.get(level)
        if cache and now - self._stock_board_name_map_at_by_level.get(level, 0.0) < 300:
            return cache
        mapping: dict[str, str] = {}
        try:
            board_context = self.data_source.fetch_board_context(level)
        except Exception:
            board_context = None
        if board_context is not None and board_context.available:
            for sector in board_context.sectors:
                name = str(sector.name or "").strip()
                if not name:
                    continue
                members = (board_context.members_by_code or {}).get(sector.board_code) or []
                for code in members:
                    code = str(code).zfill(6)
                    if code.isdigit():
                        mapping.setdefault(code, name)
        if mapping:
            self._stock_board_name_map_by_level[level] = mapping
            self._stock_board_name_map_at_by_level[level] = now
        return self._stock_board_name_map_by_level.get(level, {})

    def _stock_board_display_map(self) -> dict[str, str]:
        """个股 → 官方板块名称（申万三级，如「网络工程施工」）。"""
        return self._stock_board_display_map_for_level(3)

    def _record_intraday_context(
        self,
        context: DashboardContext,
        positions: list[PositionRecord],
    ) -> None:
        if not self.settings.trajectory_enabled:
            return
        priority_codes = {item.code for item in context.watchlist}
        priority_codes.update(item.code for item in positions)
        priority_codes.update(quote.code for quote in context.snapshot.quotes if quote.core)
        for sector in context.sectors[:12]:
            priority_codes.update(sector.core_codes)
            if sector.leader_code:
                priority_codes.add(sector.leader_code)
        priority_codes.update(
            signal.code
            for signal in context.signals_all
            if signal.phase != SignalPhase.OBSERVE.value or signal.score >= self.engine.sector_watch_score
        )
        active_stock_feature_codes = {
            quote.code
            for quote in sorted(
                context.snapshot.quotes,
                key=lambda quote: (
                    quote.code in priority_codes,
                    bool(quote.limit_up or quote.opened_limit),
                    min(max(float(quote.minute_amount_ratio or 0), 0), 9.99),
                    abs(float(quote.change_pct or 0)),
                    max(float(quote.amount or 0), 0),
                ),
                reverse=True,
            )[:160]
        }
        stock_feature_codes = list(dict.fromkeys([*priority_codes, *active_stock_feature_codes]))
        source_name = str(context.source_status.get("active_source") or context.snapshot.data_mode)
        if any(quote.order_flow.available for quote in context.snapshot.quotes):
            source_quality = "live_l1_five_level_proxy"
        elif context.market.frozen:
            source_quality = "frozen_close_snapshot"
        else:
            source_quality = "minute_quote_proxy"
        try:
            self.trajectory_store.record_context(
                trade_date=str(context.source_status.get("trade_date") or china_now().strftime("%Y%m%d")),
                captured_at=str(context.source_status.get("clock_label") or context.market.updated_at),
                updated_at=context.market.updated_at,
                frozen=context.market.frozen,
                source_quality=source_quality,
                market=context.market,
                sectors=context.sectors,
                quotes=context.snapshot.quotes,
                signals=context.signals_all,
                priority_codes=priority_codes,
                stock_feature_codes=stock_feature_codes,
            )
            context.source_status["trajectory_source"] = source_name
            context.source_status["trajectory_quality"] = source_quality
            context.source_status["trajectory_error"] = ""
        except Exception as exc:
            # Trajectory persistence is observational; it must never take the
            # market dashboard down when the local disk is unavailable.
            context.source_status["trajectory_error"] = str(exc)

    def _get_context(self) -> DashboardContext:
        if self._context_is_fresh():
            return self._context_cache  # type: ignore[return-value]
        if self._context_cache is None:
            restored = None
            with self._context_lock:
                if self._context_is_fresh():
                    return self._context_cache  # type: ignore[return-value]
                if self._context_cache is not None:
                    return self._context_cache
                restored = self._restore_context_from_trajectory()
                if restored is not None:
                    self._context_cache = restored
                    self._context_cache_at = time.time()
                    self._context_cache_bucket = self._context_bucket()
                    self._clear_payload_caches()
                    self._fast_board_entries_cache.clear()
                    self._visible_quote_cache.clear()
            if restored is not None:
                if is_trading_window():
                    self._ensure_background_context_refresh()
                return restored
            # 重型刷新放到 _context_lock 外，避免冷启动时卡死所有读请求
            return self._refresh_context_serialized()

        if is_trading_window():
            if self._intraday_close_cache_is_invalid():
                # 后台刷新进行中时先用旧缓存响应，不让请求排队等全量刷新
                if self._refresh_in_progress_lock.locked():
                    return self._context_cache
                return self._refresh_context_serialized()
            self._ensure_background_context_refresh()
            return self._context_cache

        if self._refresh_in_progress_lock.locked():
            return self._context_cache
        return self._refresh_context_serialized()

    def _refresh_context_serialized(self) -> DashboardContext:
        """前台刷新入口：与后台刷新线程互斥，但全程不持有 _context_lock。"""
        with self._refresh_in_progress_lock:
            if self._context_is_fresh():
                return self._context_cache  # type: ignore[return-value]
            return self._refresh_context()

    def _ensure_background_context_refresh(self) -> None:
        with self._refresh_trigger_lock:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return
            self._refresh_thread = threading.Thread(
                target=self._refresh_context_without_blocking_readers,
                name="watchtower-context-refresh",
                daemon=True,
            )
            self._refresh_thread.start()

    def _refresh_context_without_blocking_readers(self) -> None:
        while True:
            # 不再持 _context_lock 跑全量刷新：一轮刷新（全市场快照+信号构建）
            # 可能耗时数秒，持锁会把加自选/删自选/详情等请求整体卡住。
            # 改用 _refresh_in_progress_lock 保证同一时刻只有一个刷新在跑。
            if not self._refresh_in_progress_lock.acquire(blocking=False):
                return
            try:
                context = self._refresh_context()
            except Exception:
                return
            finally:
                self._refresh_in_progress_lock.release()
            if not is_trading_window() or context.market.frozen:
                return
            # 一轮全量刷新本身就要数秒（快照 ~6s + 信号构建 ~1s），按“刷新开始
            # 时间”扣减会把等待算成 0，形成 100% 占空比的紧循环，GIL 被长期
            # 占满，所有请求（连 health check）都被拖慢。改为每轮之间固定休息
            # full_market_refresh_seconds，让出 CPU 给请求线程。
            time.sleep(max(1.0, float(self.settings.full_market_refresh_seconds)))

    def _context_is_fresh(self) -> bool:
        now = time.time()
        cache_bucket = self._context_bucket()
        if self._context_cache and self._context_cache_bucket == cache_bucket:
            if self._intraday_close_cache_is_invalid():
                return False
            if self._context_unavailable(self._context_cache):
                ttl = self.settings.terminal_context_live_cache_seconds
            else:
                ttl = (
                    self.settings.terminal_context_frozen_cache_seconds
                    if self._context_cache.market.frozen
                    else self.settings.terminal_context_live_cache_seconds
                )
            if now - self._context_cache_at < max(1, ttl):
                return True
        return False

    @staticmethod
    def _snapshot_unavailable(snapshot: MarketSnapshot) -> bool:
        active_source = str(snapshot.source_status.get("active_source") or "")
        return bool(
            snapshot.data_mode == "unavailable"
            or active_source == "unavailable"
            or active_source.endswith("_unavailable")
            or not snapshot.quotes
        )

    @classmethod
    def _context_unavailable(cls, context: DashboardContext) -> bool:
        return cls._snapshot_unavailable(context.snapshot) or not context.sectors

    @classmethod
    def _context_has_usable_market_data(cls, context: DashboardContext | None) -> bool:
        return bool(
            context is not None
            and not cls._context_unavailable(context)
            and context.snapshot.quotes
            and context.sectors
        )

    @staticmethod
    def _mark_refresh_fallback(
        context: DashboardContext,
        reason: str,
        snapshot: MarketSnapshot,
        elapsed_ms: float,
    ) -> None:
        context.source_status["refresh_fallback"] = reason
        context.source_status["refresh_unavailable_source"] = str(snapshot.source_status.get("active_source") or snapshot.data_mode)
        context.source_status["refresh_unavailable_note"] = str(snapshot.source_status.get("note") or "")
        context.source_status["refresh_unavailable_elapsed_ms"] = round(elapsed_ms, 1)
        context.source_status["refresh_unavailable_at"] = china_now().isoformat(timespec="seconds")

    def _fallback_context_for_unavailable_snapshot(
        self,
        snapshot: MarketSnapshot,
        cache_bucket: str,
        elapsed_ms: float,
    ) -> DashboardContext | None:
        if self._context_has_usable_market_data(self._context_cache):
            context = self._context_cache
            self._mark_refresh_fallback(context, "previous_context", snapshot, elapsed_ms)
            self._context_cache = context
            self._context_cache_at = time.time()
            self._context_cache_bucket = cache_bucket
            return context
        restored = self._restore_context_from_trajectory()
        if self._context_has_usable_market_data(restored):
            self._mark_refresh_fallback(restored, "latest_context", snapshot, elapsed_ms)
            self._context_cache = restored
            self._context_cache_at = time.time()
            self._context_cache_bucket = cache_bucket
            self._clear_payload_caches()
            self._fast_board_entries_cache.clear()
            self._visible_quote_cache.clear()
            return restored
        return None

    def _intraday_close_cache_is_invalid(self) -> bool:
        if self.settings.data_mode == "replay" or self._context_cache is None or not is_trading_window():
            return False
        snapshot = self._context_cache.snapshot
        source_status = {**snapshot.source_status, **self._context_cache.source_status}
        active_source = str(source_status.get("active_source") or "")
        return snapshot.data_mode == "closed_static" or active_source.endswith("_daily_close")

    def _restore_context_from_trajectory(self) -> DashboardContext | None:
        if self.settings.data_mode == "replay":
            return None
        loader = getattr(self.trajectory_store, "latest_context_payload", None)
        if not callable(loader):
            return None
        started_at = time.perf_counter()
        stage_started_at = started_at
        stage_elapsed: dict[str, float] = {}

        def mark_stage(name: str) -> None:
            nonlocal stage_started_at
            now_perf = time.perf_counter()
            stage_elapsed[name] = round((now_perf - stage_started_at) * 1000, 1)
            stage_started_at = now_perf

        try:
            raw = loader()
        except Exception:
            return None
        if not raw:
            return None
        mark_stage("trajectory_load_ms")

        try:
            watchlist = self.watchlist_store.list_items()
            positions = self.position_store.list_items()
            position_by_code = {item.code: item for item in positions}
            themes = self.theme_store.list_themes()
            mark_stage("local_config_ms")
            market_payload = dict(raw.get("market") or {})
            market = MarketState(**market_payload)
            quotes = [
                Quote(**item)
                for item in list(raw.get("quotes") or [])
                if isinstance(item, dict) and str(item.get("code") or "").strip()
            ]
            sectors = [
                SectorSnapshot(**item)
                for item in list(raw.get("sectors") or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
        except Exception:
            return None
        if not quotes or not sectors:
            return None
        sectors = self._decorate_sector_ranks(sectors)
        trade_date = str(raw.get("trade_date") or "")
        captured_at = str(raw.get("captured_at") or market.updated_at or "")
        frozen = bool(raw.get("frozen", market.frozen))
        source_status = {
            **self.settings.public_source_status,
            "active_source": "local_trajectory_bootstrap",
            "trajectory_source_quality": str(raw.get("source_quality") or ""),
            "trade_date": trade_date,
            "clock_label": captured_at,
            "updated_at": str(raw.get("updated_at") or market.updated_at or captured_at),
            "frozen": frozen,
            "bootstrap": True,
            "bootstrap_note": "先展示本地上一帧，后台刷新实盘",
        }
        snapshot = MarketSnapshot(
            quotes=quotes,
            indices=list(market.indices),
            data_mode="closed_static" if frozen else "local_trajectory",
            source_status=source_status,
        )
        market = market.model_copy(update={"frozen": frozen})
        mark_stage("context_models_ms")

        sector_flow = self._sector_flow_from_trajectory(trade_date, sectors)
        mark_stage("sector_flow_ms")
        scan_items = self._scan_items_from_quotes(quotes)
        formula_rows_by_code = self._formula_rows_by_code_for_context(
            trade_date=trade_date,
            quotes=quotes,
            watchlist=watchlist,
            positions=position_by_code,
        )
        mark_stage("formula_rows_ms")
        signals_all = self.engine.build_signals(
            quotes,
            scan_items,
            sectors,
            self._market_for_signals(snapshot, market),
            clock_label=captured_at or None,
            preferred_sector_names=self._manual_theme_names(themes),
            positions=position_by_code,
            formula_rows_by_code=formula_rows_by_code,
            sector_name_mapper=self._display_sector_name,
        )
        signals_all = self._decorate_signals(signals_all, watchlist, position_by_code)
        mark_stage("signal_build_ms")
        core_watch = self._core_watch(signals_all, themes, watchlist)
        events = self.engine.build_events(market, sectors, signals_all, clock_label=captured_at or None)
        mark_stage("event_build_ms")
        source_status["signal_scope"] = self.settings.scan_scope
        source_status["quote_count"] = len(quotes)
        source_status["signal_count_total"] = len(signals_all)
        source_status["sector_count"] = len(sectors)
        source_status["order_flow_available_count"] = sum(1 for quote in quotes if quote.order_flow.available)
        source_status["level2_available_count"] = sum(1 for quote in quotes if quote.order_flow.level2_available)
        source_status["auction_available_count"] = sum(1 for quote in quotes if quote.auction.available)
        source_status["analysis_available"] = self.ai_client.available
        source_status["context_stage_elapsed_ms"] = stage_elapsed
        source_status["context_refresh_elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
        return DashboardContext(
            watchlist=watchlist,
            themes=themes,
            snapshot=snapshot,
            market=market,
            sectors=sectors,
            sector_flow=sector_flow,
            signals_all=signals_all,
            core_watch=core_watch,
            events=events,
            source_status=source_status,
        )

    def _sector_flow_from_trajectory(
        self,
        trade_date: str,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None = None,
        quotes: list[Quote] | None = None,
        state_key: str = "",
    ) -> list[SectorFlowSeries]:
        """冻结/盘后分支：统一走「全成员个股轨迹聚合」的净流入口径。

        不再使用同名板块热度轨迹（heat_score 0-100）——那是另一个指标，
        混进资金净流入面板会让盘后数值从「亿」跳变成「热度分」，盘中/盘后
        完全对不上。成员来源：官方板块成员加载器，或快照报价的主题归属。
        """
        if not trade_date or not sectors or not getattr(self.trajectory_store, "enabled", False):
            return []
        flow_sectors = self._sector_flow_membership(state_key, sectors)
        names = [sector.name for sector in flow_sectors]
        cache_key = "|".join(["trajectory", trade_date, *names])
        now_ts = time.time()
        with self._sector_flow_lock:
            cached = self._sector_flow_cache_by_key.get(cache_key)
        if cached and now_ts - cached[0] < max(1, self.settings.sector_flow_refresh_seconds):
            return cached[1]
        if is_trading_window():
            return cached[1] if cached else []
        result = self._sector_flow_from_stock_trajectory(
            trade_date,
            flow_sectors,
            member_code_loader,
            quotes,
        )
        # 统一展示排序：今日累计净流入（final_value）降序，同值按热度——与盘中代理/回灌一致
        result.sort(key=lambda series: (series.final_value, series.heat_score), reverse=True)
        with self._sector_flow_lock:
            self._sector_flow_cache_by_key[cache_key] = (time.time(), result)
        return result

    def _sector_flow_from_stock_trajectory(
        self,
        trade_date: str,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None,
        quotes: list[Quote] | None,
    ) -> list[SectorFlowSeries]:
        """按板块全成员筛选个股原始轨迹并聚合「每分钟净流入」曲线。

        与盘中快照代理同一总量口径、同一粒度：全成员 Σ(相邻观测成交额增量 ×
        tick 价格方向，走平沿用上一方向) / 1e8，按分钟归桶。分钟棒的二元方向
        会把整分钟成交额按收盘价涨跌一次性定号（高价股一分钟 ±5 亿来回翻），
        原始 tick（约 5 秒一根）让反向成交在同分钟内自然对冲，噪声消失。
        本地读取零网络，冻结回退与盘中冷启动回灌共用，保证跨分支可比。
        """
        loader = getattr(self.trajectory_store, "stock_feature_ticks_by_code", None)
        if not callable(loader):
            return []
        quote_list = list(quotes or [])
        quotes_by_code = {quote.code: quote for quote in quote_list if getattr(quote, "code", "")}
        result: list[SectorFlowSeries] = []
        for sector in sectors:
            if not sector.name:
                continue
            member_codes = self._sector_flow_member_codes(sector, quote_list, member_code_loader)
            if not member_codes:
                continue
            try:
                ticks_by_code = loader(trade_date, member_codes)
            except Exception:
                continue
            # 每分钟一桶：全成员该分钟净流入求和（亿），不做跨分钟累计。
            step_buckets: dict[str, float] = {}
            for code in member_codes:
                raw_ticks = list(ticks_by_code.get(code) or [])
                # 只保留正常交易时段的观测；盘前（竞价金额 0）的最后一根留作零基线，
                # 否则 09:30 首分钟的真实成交会被当作起点丢掉。
                baseline: dict[str, Any] | None = None
                ticks: list[dict[str, Any]] = []
                for tick in raw_ticks:
                    tick_label = str(tick.get("time") or "")[:5]
                    if self._is_regular_mini_time(tick_label):
                        ticks.append(tick)
                    elif tick_label and tick_label < "09:30":
                        baseline = tick
                if baseline is not None:
                    ticks.insert(0, baseline)
                prev_tick: dict[str, Any] | None = None
                prev_label = ""
                last_direction = 0
                for tick in ticks:
                    price = self._safe_float(tick.get("price"))
                    amount = self._safe_float(tick.get("amount"))
                    label = str(tick.get("time") or "")[:5]
                    if prev_tick is None or price <= 0 or not label:
                        prev_tick = tick
                        prev_label = label
                        continue
                    prev_price = self._safe_float(prev_tick.get("price")) or price
                    delta_amount = amount - self._safe_float(prev_tick.get("amount"))
                    # 相邻观测跨度过大（采集中断）时，成交额增量是整个空窗期的总量，
                    # 按空窗分钟数（封顶 5）摊薄，避免单分钟尖刺；午间休市无成交，天然跳过。
                    gap_minutes = self._minutes_between(prev_label, label)
                    prev_tick = tick
                    prev_label = label
                    if delta_amount <= 0:
                        continue
                    if price > prev_price:
                        direction = 1
                    elif price < prev_price:
                        direction = -1
                    else:
                        direction = last_direction  # 走平沿用上一方向：平推的连续成交不该被丢
                    if direction == 0:
                        continue
                    last_direction = direction
                    step = delta_amount / 100_000_000 * direction
                    if gap_minutes > 2:
                        step /= min(gap_minutes, 5)
                    step_buckets[label] = step_buckets.get(label, 0.0) + step
            if not step_buckets:
                continue
            raw_points: list[dict[str, Any]] = []
            cum_total = 0.0
            for label in sorted(step_buckets):
                value = step_buckets[label]
                cum_total += value
                # change_pct 字段承载曲线值：_sample_mini_rows 的平段压缩按它识别形状
                raw_points.append({"captured_at": label, "change_pct": round(value, 4)})
            # 120 点上限：全交易日约 240 个分钟桶，压缩一半足以保形；
            # 48 点会把分钟级锯齿欠采样成更低频的假波动
            sampled = self._sample_mini_rows(raw_points, max_points=120)
            points = [
                SectorFlowPoint(time=self._mini_time_label(row.get("captured_at")), value=round(self._safe_float(row.get("change_pct")), 2))
                for row in sampled
            ]
            if len(points) < 2:
                continue
            result.append(
                # 总量锚定到当日 L1 主动净额真值：分钟形态保留，成交额口径的
                # 趋势日虚高被收敛；真值不可得或方向矛盾时保持原口径。
                self._anchor_series_to_truth(
                    SectorFlowSeries(
                        name=sector.name,
                        heat_score=sector.heat_score,
                        # final_value 统一为「今日累计动能」（采样压缩前的全量分钟求和），
                        # 供三分支一致排序；points 仍是每分钟净流入，前端自行积分。
                        final_value=round(cum_total, 2),
                        change_pct=sector.avg_change_pct,
                        leader_code=sector.leader_code,
                        leader_name=sector.leader_name,
                        core_codes=list(sector.core_codes),
                        reasons=list(sector.reasons),
                        points=points,
                        flow_basis="每分钟净流入(全成员成交额增量×方向)",
                        sample_codes=sorted(member_codes)[:3],
                    ),
                    self._active_net_truth_total(member_codes, quotes_by_code),
                )
            )
        return result

    def _refresh_context(self) -> DashboardContext:
        refresh_started_wall = time.time()
        refresh_started_at = time.perf_counter()
        stage_started_at = refresh_started_at
        stage_elapsed: dict[str, float] = {}

        def mark_stage(name: str) -> None:
            nonlocal stage_started_at
            now_perf = time.perf_counter()
            stage_elapsed[name] = round((now_perf - stage_started_at) * 1000, 1)
            stage_started_at = now_perf

        cache_bucket = self._context_bucket()

        watchlist = self.watchlist_store.list_items()
        positions = self.position_store.list_items()
        position_by_code = {item.code: item for item in positions}
        themes = self.theme_store.list_themes()
        mark_stage("local_config_ms")
        snapshot = self.data_source.fetch(watchlist, themes)
        mark_stage("snapshot_fetch_ms")
        if self._snapshot_unavailable(snapshot):
            fallback_context = self._fallback_context_for_unavailable_snapshot(
                snapshot,
                cache_bucket,
                round((time.perf_counter() - refresh_started_at) * 1000, 1),
            )
            if fallback_context is not None:
                return fallback_context
        snapshot = self._snapshot_with_index_minute_fallback(snapshot)
        clock_label = str(snapshot.source_status.get("clock_label") or "")
        snapshot_frozen = bool(snapshot.source_status.get("frozen", snapshot.data_mode == "closed_static"))
        market = self.engine.build_market_state(
            snapshot.indices,
            snapshot.quotes,
            clock_label=clock_label or None,
            frozen=snapshot_frozen,
        )
        market = market.model_copy(
            update={
                "frozen": snapshot_frozen,
            }
        )
        mark_stage("market_state_ms")
        sectors = self.engine.rank_sectors(snapshot.quotes, themes, market)
        sectors = self._decorate_sector_ranks(sectors)
        mark_stage("sector_rank_ms")
        sector_flow = self._sector_flow_for_context(
            snapshot,
            sectors,
            prefer_async=bool(snapshot.data_mode == "live" and not snapshot_frozen),
            allow_deferred=True,
        )
        mark_stage("sector_flow_ms")
        scan_items = self._scan_items_from_quotes(snapshot.quotes)
        formula_rows_by_code = self._formula_rows_by_code_for_context(
            trade_date=str(snapshot.source_status.get("trade_date") or china_now().strftime("%Y%m%d")),
            quotes=snapshot.quotes,
            watchlist=watchlist,
            positions=position_by_code,
        )
        mark_stage("formula_rows_ms")
        signals_all = self.engine.build_signals(
            snapshot.quotes,
            scan_items,
            sectors,
            self._market_for_signals(snapshot, market),
            clock_label=clock_label or None,
            preferred_sector_names=self._manual_theme_names(themes),
            positions=position_by_code,
            formula_rows_by_code=formula_rows_by_code,
            sector_name_mapper=self._display_sector_name,
        )
        signals_all = self._decorate_signals(signals_all, watchlist, position_by_code)
        # 实盘刷新路径专属：自选股买卖点飞书推送。「置顶买点/黄金买点」是
        # 现价贴近最近买点的展示态，不在这里触发；本地轨迹恢复/历史回放
        # 路径也不投递，避免重启后把旧信号当新信号推出去。
        if snapshot.data_mode == "live" and not snapshot_frozen:
            self._dispatch_signal_pushes(signals_all)
        mark_stage("signal_build_ms")
        core_watch = self._core_watch(signals_all, themes, watchlist)
        events = self.engine.build_events(market, sectors, signals_all, clock_label=clock_label or None)
        mark_stage("event_build_ms")
        source_status = dict(snapshot.source_status)
        source_status.update(self.settings.public_source_status)
        source_status["signal_scope"] = self.settings.scan_scope
        source_status["quote_count"] = len(snapshot.quotes)
        source_status["signal_count_total"] = len(signals_all)
        source_status["sector_count"] = len(sectors)
        source_status["order_flow_available_count"] = sum(
            1 for quote in snapshot.quotes if quote.order_flow.available
        )
        source_status["l1_flow_available_count"] = sum(
            1 for quote in snapshot.quotes if quote.order_flow.level2_available
        )
        source_status["auction_available_count"] = sum(
            1 for quote in snapshot.quotes if quote.auction.available
        )
        source_status["auction_data_quality"] = (
            "actual"
            if any(quote.auction.data_quality == "actual" for quote in snapshot.quotes)
            else "proxy"
            if any(quote.auction.data_quality == "proxy" for quote in snapshot.quotes)
            else "unavailable"
        )
        source_status.update(self._public_market_capability_status())
        source_status["analysis_available"] = self.ai_client.available
        source_status["frozen"] = market.frozen
        source_status["updated_at"] = market.updated_at
        source_status["position_count"] = len(positions)
        source_status["context_stage_elapsed_ms"] = stage_elapsed
        source_status["context_refresh_elapsed_ms"] = round((time.perf_counter() - refresh_started_at) * 1000, 1)

        context = DashboardContext(
            watchlist=watchlist,
            themes=themes,
            snapshot=snapshot,
            market=market,
            sectors=sectors,
            sector_flow=sector_flow,
            signals_all=signals_all,
            core_watch=core_watch,
            events=events,
            source_status=source_status,
        )
        self._context_cache = context
        self._context_cache_at = refresh_started_wall if not context.market.frozen else time.time()
        self._context_cache_bucket = cache_bucket
        self._fast_board_entries_cache.clear()
        self._visible_quote_cache.clear()
        self._ensure_terminal_warmup(context)
        return context

    def _snapshot_with_index_minute_fallback(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        if snapshot.indices:
            return snapshot
        active_source = str(snapshot.source_status.get("active_source") or "")
        live_mode = snapshot.data_mode == "live" or active_source == "easy_tdx"
        if not live_mode:
            return snapshot
        trade_date = str(snapshot.source_status.get("trade_date") or china_now().strftime("%Y%m%d"))
        indices: list[IndexSnapshot] = []
        errors: dict[str, str] = {}
        for code, name in self._DEFAULT_INDEX_MINUTE_SERIES:
            try:
                rows = self.data_source.fetch_index_minute_series(code, trade_date, live=True)
            except Exception as exc:
                errors[code] = str(exc)
                continue
            index = self._index_snapshot_from_minute_rows(code, name, rows)
            if index is not None:
                indices.append(index)
        if not indices:
            if errors:
                source_status = dict(snapshot.source_status)
                source_status["index_minute_fallback_error"] = "; ".join(
                    f"{code}:{message}" for code, message in list(errors.items())[:3]
                )
                return MarketSnapshot(
                    quotes=list(snapshot.quotes),
                    indices=[],
                    data_mode=snapshot.data_mode,
                    source_status=source_status,
                )
            return snapshot
        source_status = dict(snapshot.source_status)
        source_status["index_minute_fallback"] = True
        source_status["index_minute_fallback_count"] = len(indices)
        if errors:
            source_status["index_minute_fallback_error"] = "; ".join(
                f"{code}:{message}" for code, message in list(errors.items())[:3]
            )
        return MarketSnapshot(
            quotes=list(snapshot.quotes),
            indices=indices,
            data_mode=snapshot.data_mode,
            source_status=source_status,
        )

    @staticmethod
    def _index_snapshot_from_minute_rows(
        code: str,
        name: str,
        rows: list[dict[str, Any]],
    ) -> IndexSnapshot | None:
        prices: list[float] = []
        for row in rows:
            try:
                price = float(row.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if isfinite(price) and price > 0:
                prices.append(price)
        if not prices:
            return None
        prev_close = DashboardService._index_prev_close_for_minutes(
            IndexSnapshot(
                code=code,
                name=name,
                price=0.0,
                prev_close=0.0,
                open=0.0,
                high=0.0,
                low=0.0,
                change_pct=0.0,
                rebound_from_low_pct=0.0,
                minute_amount_ratio=1.0,
                amount=0.0,
            ),
            rows,
        )
        price = prices[-1]
        open_price = prices[0]
        high = max(prices)
        low = min(prices)
        amount_values: list[float] = []
        volume_values: list[float] = []
        for row in rows:
            try:
                amount_values.append(max(float(row.get("amount") or 0), 0))
                volume_values.append(max(float(row.get("vol") or row.get("volume") or 0), 0))
            except (TypeError, ValueError):
                continue
        latest_amount = amount_values[-1] if amount_values else 0.0
        amount = latest_amount if latest_amount >= sum(amount_values[:-1]) else sum(amount_values)
        minute_ratio = 1.0
        if len(volume_values) >= 2:
            previous = [value for value in volume_values[:-1] if value > 0]
            latest_volume = volume_values[-1]
            if previous and latest_volume > 0:
                baseline = sum(previous[-20:]) / len(previous[-20:])
                if baseline > 0:
                    minute_ratio = max(0.1, min(latest_volume / baseline, 5.0))
        return IndexSnapshot(
            code=code,
            name=name,
            price=round(price, 2),
            prev_close=round(prev_close, 2),
            open=round(open_price, 2),
            high=round(high, 2),
            low=round(low, 2),
            change_pct=round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
            rebound_from_low_pct=round((price - low) / low * 100, 2) if low else 0.0,
            minute_amount_ratio=round(minute_ratio, 3),
            amount=round(amount, 2),
        )

    def _context_for_trade_date(
        self,
        current_context: DashboardContext,
        trade_date: str,
    ) -> DashboardContext:
        normalized_date = str(trade_date or "").strip()
        current_date = str(current_context.source_status.get("trade_date") or "").strip()
        if not normalized_date or normalized_date == current_date:
            return current_context

        cache_key = "|".join([normalized_date, self._watchlist_signature(current_context.watchlist)])
        cached = self._historical_context_cache.get(cache_key)
        if cached is not None:
            return cached

        snapshot = self.data_source.fetch_trade_date_snapshot(
            current_context.watchlist,
            current_context.themes,
            normalized_date,
        )
        clock_label = str(snapshot.source_status.get("clock_label") or "15:00:00")
        market = self.engine.build_market_state(
            snapshot.indices,
            snapshot.quotes,
            clock_label=clock_label,
            frozen=True,
        ).model_copy(update={"frozen": True})
        sectors = self.engine.rank_sectors(snapshot.quotes, current_context.themes, market)
        positions = self.position_store.list_items()
        position_by_code = {item.code: item for item in positions}
        scan_items = self._scan_items_from_quotes(snapshot.quotes)
        formula_rows_started_at = time.perf_counter()
        formula_rows_by_code = self._formula_rows_by_code_for_context(
            trade_date=normalized_date,
            quotes=snapshot.quotes,
            watchlist=current_context.watchlist,
            positions=position_by_code,
        )
        formula_rows_finished_at = time.perf_counter()
        signals_all = self.engine.build_signals(
            snapshot.quotes,
            scan_items,
            sectors,
            market,
            clock_label=clock_label,
            preferred_sector_names=self._manual_theme_names(current_context.themes),
            positions=position_by_code,
            formula_rows_by_code=formula_rows_by_code,
            sector_name_mapper=self._display_sector_name,
        )
        signals_all = self._decorate_signals(
            signals_all,
            current_context.watchlist,
            position_by_code,
        )
        source_status_elapsed = {
            "formula_rows_ms": round((formula_rows_finished_at - formula_rows_started_at) * 1000, 1),
            "signal_build_ms": round((time.perf_counter() - formula_rows_finished_at) * 1000, 1),
        }
        source_status = dict(snapshot.source_status)
        source_status.update(self.settings.public_source_status)
        source_status.update(
            {
                "signal_scope": self.settings.scan_scope,
                "quote_count": len(snapshot.quotes),
                "signal_count_total": len(signals_all),
                "sector_count": len(sectors),
                "order_flow_available_count": 0,
                "level2_available_count": 0,
                "auction_available_count": sum(
                    1 for quote in snapshot.quotes if quote.auction.available
                ),
                "auction_data_quality": (
                    "actual"
                    if any(quote.auction.data_quality == "actual" for quote in snapshot.quotes)
                    else "proxy"
                    if any(quote.auction.data_quality == "proxy" for quote in snapshot.quotes)
                    else "unavailable"
                ),
                "analysis_available": self.ai_client.available,
                "frozen": True,
                "updated_at": market.updated_at,
                "position_count": len(positions),
                "context_stage_elapsed_ms": source_status_elapsed,
            }
        )
        source_status.update(self._public_market_capability_status())
        context = DashboardContext(
            watchlist=current_context.watchlist,
            themes=current_context.themes,
            snapshot=snapshot,
            market=market,
            sectors=sectors,
            sector_flow=[],
            signals_all=signals_all,
            core_watch=self._core_watch(signals_all, current_context.themes, current_context.watchlist),
            events=self.engine.build_events(market, sectors, signals_all, clock_label=clock_label),
            source_status=source_status,
        )
        self._historical_context_cache[cache_key] = context
        if len(self._historical_context_cache) > 8:
            self._historical_context_cache.pop(next(iter(self._historical_context_cache)))
        return context

    def _context_bucket(self) -> str:
        """Invalidate a long frozen cache when the market session changes."""
        session = "open" if is_trading_window() else "closed"
        return f"{self.settings.data_mode}:{session}"

    def _decorate_sector_ranks(self, sectors: list[SectorSnapshot]) -> list[SectorSnapshot]:
        return self._decorate_ranks(sectors, self._previous_sector_ranks)

    @staticmethod
    def _decorate_ranks(
        sectors: list[SectorSnapshot],
        previous_ranks: dict[str, int],
    ) -> list[SectorSnapshot]:
        current: dict[str, int] = {}
        decorated: list[SectorSnapshot] = []
        for rank, sector in enumerate(sectors, start=1):
            current[sector.name] = rank
            previous = previous_ranks.get(sector.name)
            rank_change = 0 if previous is None else previous - rank
            decorated.append(sector.model_copy(update={"rank_change": rank_change}))
        previous_ranks.clear()
        previous_ranks.update(current)
        return decorated

    def _selected_sector_codes(
        self,
        context: DashboardContext,
        selected_sector: str | None,
        board_context: BoardContext | None,
    ) -> set[str]:
        if not selected_sector:
            return set()
        if board_context and board_context.available and selected_sector in board_context.name_to_code:
            symbol = board_context.name_to_code.get(selected_sector) or ""
            members_by_code = getattr(board_context, "members_by_code", {}) or {}
            codes = members_by_code.get(symbol) or self.data_source.fetch_board_member_codes(
                selected_sector,
                board_context.board_level,
            )
            return {code for code in codes if self._quote_for_code(context.snapshot.quotes, code)}
        return self._sector_codes(context.snapshot.quotes, selected_sector)

    def _board_members_by_sector(self, board_context: BoardContext | None) -> dict[str, list[str]]:
        if not board_context or not board_context.available:
            return {}
        cache_key = self._board_members_cache_key(board_context)
        cached = self._board_members_cache_by_key.get(cache_key)
        if cached is not None:
            return cached
        members_by_code = getattr(board_context, "members_by_code", {}) or {}
        result: dict[str, list[str]] = {}
        for name, board_code in dict(board_context.name_to_code).items():
            codes = members_by_code.get(board_code) or members_by_code.get(name) or []
            clean = list(
                dict.fromkeys(
                    str(code).zfill(6)
                    for code in codes
                    if str(code).strip().isdigit()
                )
            )
            if clean:
                result[name] = clean
        self._board_members_cache_by_key[cache_key] = result
        if len(self._board_members_cache_by_key) > 8:
            self._board_members_cache_by_key.pop(next(iter(self._board_members_cache_by_key)), None)
        return result

    @staticmethod
    def _board_members_cache_key(board_context: BoardContext) -> tuple[int, str]:
        members_by_code = getattr(board_context, "members_by_code", {}) or {}
        name_to_code = getattr(board_context, "name_to_code", {}) or {}
        member_counts = "/".join(
            f"{key}:{len(value or [])}:{','.join(list(value or [])[:3])}"
            for key, value in list(dict(members_by_code).items())[:20]
        )
        sector_names = "/".join(list(dict(name_to_code).keys())[:32])
        return (
            int(normalize_board_level(getattr(board_context, "board_level", 3) or 3)),
            "|".join(
                [
                    str(getattr(board_context, "source", "") or ""),
                    str(len(members_by_code)),
                    str(len(name_to_code)),
                    sector_names,
                    member_counts,
                ]
            ),
        )

    def _official_board_sectors_from_snapshot(
        self,
        context: DashboardContext,
        board_context: BoardContext,
        members_by_sector: dict[str, list[str]] | None = None,
    ) -> list[SectorSnapshot]:
        members_by_sector = members_by_sector if members_by_sector is not None else self._board_members_by_sector(board_context)
        if not members_by_sector:
            return []
        quote_by_code = {quote.code: quote for quote in context.snapshot.quotes}
        meta_by_name = {sector.name: sector for sector in board_context.sectors}
        float_mcaps = self._float_mcap_map()
        built: list[SectorSnapshot] = []
        for name, member_codes in members_by_sector.items():
            quotes = [quote_by_code[code] for code in member_codes if code in quote_by_code]
            if not quotes:
                continue
            base = meta_by_name.get(name)
            built.append(
                self._aggregate_sector_snapshot(
                    name,
                    quotes,
                    float_mcaps,
                    board_code=base.board_code if base else board_context.name_to_code.get(name, ""),
                    board_level=board_context.board_level,
                    board_source="easy_tdx_cached_members_local_quote_aggregation",
                )
            )
        built.sort(
            key=lambda sector: (
                sector.heat_score,
                sector.avg_change_pct,
                sector.flow_delta,
                sector.amount,
            ),
            reverse=True,
        )
        return self._decorate_sector_ranks(built)

    def _aggregate_sector_snapshot(
        self,
        name: str,
        quotes: list[Quote],
        float_mcaps: dict[str, float],
        *,
        board_code: str,
        board_level: int,
        board_source: str,
    ) -> SectorSnapshot:
        """一组成分股行情 → 强弱指标快照（官方板块与手工主题共用同一口径）。"""
        metric_quotes, excluded_quotes = self.engine.sector_metric_quotes(quotes)
        total = len(metric_quotes)
        raw_total = len(quotes)
        up_count = sum(1 for quote in metric_quotes if quote.change_pct > 0)
        down_count = sum(1 for quote in metric_quotes if quote.change_pct < 0)
        limit_up_count = sum(1 for quote in metric_quotes if quote.limit_up)
        opened_limit_count = sum(1 for quote in metric_quotes if quote.opened_limit)
        avg_change = sum(quote.change_pct for quote in metric_quotes) / total
        amount = sum(max(float(quote.amount or 0), 0) for quote in metric_quotes)
        flow_delta = sum(
            max(float(quote.amount or 0), 0) * max(-10.0, min(10.0, float(quote.change_pct or 0))) / 100
            for quote in metric_quotes
        ) / 100_000_000
        leader = max(
            metric_quotes,
            key=lambda quote: (
                quote.change_pct,
                quote.minute_amount_ratio,
                quote.amount,
            ),
        )
        core_quotes = sorted(
            metric_quotes,
            key=lambda quote: (
                quote.amount,
                quote.minute_amount_ratio,
                quote.change_pct,
            ),
            reverse=True,
        )[:5]
        core_codes = list(dict.fromkeys([leader.code, *(quote.code for quote in core_quotes)]))
        capacity_leader = self._capacity_leader(metric_quotes, float_mcaps)
        breadth = up_count / max(total, 1)
        amount_yi = min(amount / 100_000_000, 180)
        attack_count = sum(
            1
            for quote in metric_quotes
            if quote.limit_up or (quote.change_pct >= 2.0 and quote.minute_amount_ratio >= 1.4)
        )
        heat_score = int(
            max(
                0,
                min(
                    100,
                    round(
                        45
                        + avg_change * 5.0
                        + (breadth - 0.5) * 34
                        + min(18, attack_count * 2.8)
                        + min(8, amount_yi * 0.04)
                    ),
                ),
            )
        )
        reasons = [
            f"成分股{raw_total}只",
            f"均涨{avg_change:+.2f}%",
            f"{up_count}/{total}上涨",
            f"成交额{amount / 100_000_000:.1f}亿",
            f"动能代理{flow_delta:+.1f}亿",
            f"领涨{leader.name}",
        ]
        exclusion_reason = self.engine.sector_exclusion_reason(excluded_quotes)
        if exclusion_reason:
            reasons.insert(1, exclusion_reason)
        return SectorSnapshot(
            name=name,
            heat_score=heat_score,
            avg_change_pct=round(avg_change, 2),
            up_count=up_count,
            down_count=down_count,
            total_count=total,
            limit_up_count=limit_up_count,
            opened_limit_count=opened_limit_count,
            core_attack=attack_count > 0,
            core_codes=core_codes,
            leader_code=leader.code,
            leader_name=leader.name,
            capacity_leader_code=capacity_leader.code if capacity_leader else None,
            capacity_leader_name=capacity_leader.name if capacity_leader else None,
            reasons=reasons,
            flow_delta=round(flow_delta, 2),
            raw_total_count=raw_total,
            new_listing_excluded_count=len(excluded_quotes),
            amount=round(amount, 2),
            main_net_amount=0,
            board_code=board_code,
            board_level=board_level,
            board_source=board_source,
        )

    def _terminal_payload_for_context(
        self,
        context: DashboardContext,
        sector: str | None,
        board_level: int | str,
        sort: str,
        page: int,
        page_size: int,
        near_trend: bool = False,
        pin_buy: bool = False,
    ) -> TerminalPayload:
        payload_started_at = time.perf_counter()
        stage_started_at = payload_started_at
        terminal_stage_elapsed: dict[str, float] = {}

        def mark_stage(name: str) -> None:
            nonlocal stage_started_at
            now_perf = time.perf_counter()
            terminal_stage_elapsed[name] = round((now_perf - stage_started_at) * 1000, 1)
            stage_started_at = now_perf

        level = normalize_board_level(board_level)
        # 载荷级共享缓存/单飞在 terminal() 入口统一处理，这里只做构建。
        board_context = self.data_source.fetch_board_context(level)
        mark_stage("board_context_ms")
        official_boards_available = bool(board_context.available and board_context.sectors)
        board_members_by_sector = self._board_members_by_sector(board_context) if official_boards_available else {}
        official_board_member_ready = bool(board_members_by_sector)
        local_board_sectors = (
            self._official_board_sectors_from_snapshot(context, board_context, board_members_by_sector)
            if official_board_member_ready
            else []
        )
        mark_stage("board_local_grouping_ms")
        display_sectors = (
            local_board_sectors
            if local_board_sectors
            else board_context.sectors
            if official_board_member_ready
            else context.sectors
        )
        requested_sector = self._normalize_sector(sector)
        display_sectors = self._decorate_sector_mini_charts(
            context,
            display_sectors,
            preferred_names={requested_sector} if requested_sector else None,
        )
        board_source = (
            "easy_tdx_cached_members_local_quote_aggregation"
            if local_board_sectors
            else board_context.source
            if official_board_member_ready
            else "signal_engine_theme_rank"
        )
        mark_stage("board_member_map_ms")
        mode = "board"
        display_sector_names = {item.name for item in display_sectors}
        legacy_sector_names = {item.name for item in context.sectors}
        selected_sector = (
            requested_sector
            if requested_sector
            and (
                requested_sector in display_sector_names
                if official_boards_available
                else requested_sector in legacy_sector_names
            )
            else None
        )
        sector_focus = next(
            (item for item in display_sectors if item.name == selected_sector),
            None,
        ) if selected_sector else None
        selected_codes = self._selected_sector_codes(context, selected_sector, board_context)
        mark_stage("selected_sector_codes_ms")
        if official_boards_available:
            terminal_market = context.market.model_copy(
                update={
                    "mainline": (
                        display_sectors[0].name
                        if display_sectors and display_sectors[0].heat_score >= self.engine.sector_watch_score
                        else context.market.mainline
                    )
                }
            )
            if context.source_status.get("bootstrap"):
                # 冷启动本地帧：同样走延迟重建，避免终端构建同步等 80GB 轨迹库冷读
                sector_flow = self._sector_flow_for_context(
                    context.snapshot,
                    display_sectors,
                    member_code_loader=lambda sector: board_members_by_sector.get(sector.name, []),
                    cache_namespace=f"bootstrap_official_board_level_{level}",
                    allow_deferred=True,
                )
            else:
                sector_flow = self._sector_flow_for_context(
                    context.snapshot,
                    display_sectors,
                    member_code_loader=lambda sector: board_members_by_sector.get(sector.name, []),
                    cache_namespace=f"official_board_level_{level}",
                    prefer_async=bool(context.snapshot.data_mode == "live" and not context.market.frozen),
                    allow_deferred=True,
                )
            mark_stage("sector_flow_ms")
        else:
            terminal_market = context.market
            sector_flow = context.sector_flow
            mark_stage("sector_flow_ms")
        quote_overrides: dict[str, Quote] = {}
        visible_refresh_status: dict[str, Any] = {}
        board_preview_codes = self._preview_board_codes(
            context,
            selected_sector=selected_sector,
            sector_codes=selected_codes,
            display_sectors=display_sectors,
            sector_focus=sector_focus,
            board_members_by_sector=board_members_by_sector,
            sort=sort,
            page=page,
            limit=page_size,
        )
        visible_codes = list(
            dict.fromkeys(
                [
                    *board_preview_codes,
                    *[item.code for item in context.watchlist],
                    *[item.code for item in self.position_store.list_items()],
                ]
            )
        )
        quote_overrides, visible_refresh_status = self._visible_quote_overrides(context, visible_codes)
        mark_stage("visible_quote_refresh_ms")
        board_context_for_payload = self._context_with_quote_overrides(context, quote_overrides)
        if quote_overrides and board_context_for_payload.market.updated_at:
            terminal_market = terminal_market.model_copy(update={"updated_at": board_context_for_payload.market.updated_at})
        self._last_visible_mini_chart_cache: dict[str, MiniIntradaySeries] = {}
        board = self._build_stock_board(
            board_context_for_payload,
            selected_sector=selected_sector,
            sector_codes=selected_codes,
            display_sectors=display_sectors,
            sector_focus=sector_focus,
            board_level=level,
            board_source=board_source,
            board_members_by_sector=board_members_by_sector,
            sort=sort,
            page=page,
            page_size=page_size,
            near_trend=near_trend,
            pin_buy=pin_buy,
        )
        mark_stage("stock_board_ms")
        watchlist_preview, positions_preview = self._context_watch_previews(
            board_context_for_payload,
            mini_cache=getattr(self, "_last_visible_mini_chart_cache", None),
        )
        mark_stage("watch_position_preview_ms")
        source_status = dict(board_context_for_payload.source_status)
        source_status.update(
            {
                "selected_sector": selected_sector,
                "sector_mode": mode,
                "board_total": board.total,
                "board_page": board.page,
                "board_page_size": board.page_size,
                "board_level": level,
                "board_source": board_source,
                "board_count": len(display_sectors),
                "official_board_available": official_boards_available,
                "official_board_member_ready": official_board_member_ready,
                "official_board_error": board_context.error,
                "board_member_cached_count": len(board_members_by_sector),
                "board_local_grouping": bool(local_board_sectors),
                "signal_count_total": len(context.signals_all),
                "watchlist_codes": [item.code for item in context.watchlist],
                **visible_refresh_status,
                "stock_mini_chart_elapsed_ms": self._last_stock_mini_chart_elapsed_ms,
                "stock_mini_chart_missing_count": self._last_stock_mini_chart_missing_count,
                "stock_mini_chart_loaded_count": self._last_stock_mini_chart_loaded_count,
                "terminal_stage_elapsed_ms": terminal_stage_elapsed,
                "terminal_payload_elapsed_ms": round((time.perf_counter() - payload_started_at) * 1000, 1),
            }
        )
        payload = TerminalPayload(
            market=terminal_market,
            sectors=display_sectors,
            sector_flow=sector_flow,
            stock_board=board,
            watchlist=context.watchlist,
            watchlist_preview=watchlist_preview,
            positions_preview=positions_preview,
            data_mode=context.snapshot.data_mode,
            source_status=source_status,
            selected_sector=selected_sector,
            sector_focus=sector_focus,
            board_level=level,
            board_source=board_source,
            watchlist_codes=[item.code for item in context.watchlist],
        )
        self._ensure_terminal_warmup(context)
        return payload

    def _terminal_fast_payload_for_context(
        self,
        context: DashboardContext,
        sector: str | None,
        board_level: int | str,
        sort: str,
        page: int,
        page_size: int,
        near_trend: bool = False,
        pin_buy: bool = False,
    ) -> TerminalPayload:
        payload_started_at = time.perf_counter()
        stage_started_at = payload_started_at
        terminal_stage_elapsed: dict[str, float] = {}

        def mark_stage(name: str) -> None:
            nonlocal stage_started_at
            now_perf = time.perf_counter()
            terminal_stage_elapsed[name] = round((now_perf - stage_started_at) * 1000, 1)
            stage_started_at = now_perf

        level = normalize_board_level(board_level)
        requested_sector = self._normalize_sector(sector)
        mode = "board"
        display_sectors = context.sectors
        display_sector_names = {item.name for item in display_sectors}
        selected_sector = requested_sector if requested_sector in display_sector_names else None
        sector_focus = next(
            (item for item in display_sectors if item.name == selected_sector),
            None,
        ) if selected_sector else None
        selected_codes = self._sector_codes(context.snapshot.quotes, selected_sector) if selected_sector else set()
        mark_stage("select_scope_ms")

        normalized_sort = sort if sort in {"activity", "change", "amount", "volume_ratio", "order_flow", "signal"} else "activity"
        normalized_page_size = max(20, min(int(page_size or 80), 240))
        normalized_page = max(1, int(page or 1))
        board_entries = self._fast_stock_board_entries(
            context,
            selected_sector=selected_sector,
            sector_codes=selected_codes,
            display_sectors=display_sectors,
            sector_focus=sector_focus,
            board_members_by_sector={},
            sort=normalized_sort,
        )
        near_trend_ready = 0
        near_trend_pending = 0
        if near_trend:
            board_entries, near_trend_ready, near_trend_pending = self._near_trend_filter_entries(board_entries)
        if pin_buy:
            board_entries = self._pin_buy_entries(board_entries)
        page_count = max(1, (len(board_entries) + normalized_page_size - 1) // normalized_page_size)
        normalized_page = min(normalized_page, page_count)
        board_preview_entries = self._visible_board_entries(
            board_entries,
            page=normalized_page,
            page_size=normalized_page_size,
        )
        board_preview_codes = [entry.quote.code for entry in board_preview_entries]
        visible_codes = list(
            dict.fromkeys(
                [
                    *board_preview_codes,
                    *[item.code for item in context.watchlist],
                    *[item.code for item in self.position_store.list_items()],
                ]
            )
        )
        quote_overrides, visible_refresh_status = self._visible_quote_overrides(context, visible_codes)
        mark_stage("visible_quote_refresh_ms")

        latest_at = self._latest_quote_timestamp(quote_overrides.values())
        terminal_market = context.market.model_copy(update={"updated_at": latest_at}) if latest_at else context.market
        self._last_visible_mini_chart_cache = {}
        board_preview_entries = self._entries_with_quote_overrides(board_preview_entries, quote_overrides)
        board = self._stock_board_from_entries(
            context,
            board_preview_entries,
            total=len(board_entries),
            normalized_sort=normalized_sort,
            normalized_page=normalized_page,
            normalized_page_size=normalized_page_size,
            selected_sector=selected_sector,
            display_sectors=display_sectors,
            board_level=level,
            board_source="signal_engine_theme_rank_fast",
            include_mini_charts=False,
            near_trend=near_trend,
            near_trend_ready=near_trend_ready,
            near_trend_pending=near_trend_pending,
            pin_buy=pin_buy,
        )
        mark_stage("stock_board_ms")
        watchlist_preview, positions_preview = self._context_watch_previews(
            context,
            mini_cache={},
            include_mini_charts=False,
            quote_overrides=quote_overrides,
        )
        mark_stage("watch_position_preview_ms")

        source_status = dict(context.source_status)
        if latest_at:
            source_status["clock_label"] = latest_at
            source_status["updated_at"] = latest_at
        source_status.update(
            {
                "selected_sector": selected_sector,
                "sector_mode": mode,
                "board_total": board.total,
                "board_page": board.page,
                "board_page_size": board.page_size,
                "board_level": level,
                "board_source": "signal_engine_theme_rank_fast",
                "board_count": len(display_sectors),
                "official_board_available": False,
                "official_board_error": "",
                "board_member_cached_count": 0,
                "board_local_grouping": False,
                "signal_count_total": len(context.signals_all),
                "watchlist_codes": [item.code for item in context.watchlist],
                "terminal_fast_mode": True,
                "terminal_omitted_sections": [
                    "official_board_refresh",
                    "sector_flow",
                    "sector_mini_charts",
                    "stock_mini_chart_sqlite_reads",
                    "homepage_l1_transaction_tape",
                ],
                "stock_mini_chart_elapsed_ms": 0.0,
                "stock_mini_chart_missing_count": 0,
                "stock_mini_chart_loaded_count": 0,
                "terminal_stage_elapsed_ms": terminal_stage_elapsed,
                "terminal_payload_elapsed_ms": round((time.perf_counter() - payload_started_at) * 1000, 1),
                **visible_refresh_status,
            }
        )
        return TerminalPayload(
            market=terminal_market,
            sectors=display_sectors,
            sector_flow=[],
            stock_board=board,
            watchlist=context.watchlist,
            watchlist_preview=watchlist_preview,
            positions_preview=positions_preview,
            data_mode=context.snapshot.data_mode,
            source_status=source_status,
            selected_sector=selected_sector,
            sector_focus=sector_focus,
            board_level=level,
            board_source="signal_engine_theme_rank_fast",
            watchlist_codes=[item.code for item in context.watchlist],
        )

    def _stock_board_payload_for_context(
        self,
        context: DashboardContext,
        sector: str | None,
        board_level: int | str,
        sort: str,
        page: int,
        page_size: int,
        near_trend: bool = False,
        pin_buy: bool = False,
    ) -> StockBoardPayload:
        """Build only the paged stock table.

        The board table is requested for pagination, sorting and fast sector
        switches.  It must not force a sector-flow chart rebuild or read
        multiple official board member lists.
        """
        level = normalize_board_level(board_level)
        board_context = self.data_source.fetch_board_context(level)
        official_boards_available = bool(board_context.available and board_context.sectors)
        board_members_by_sector = self._board_members_by_sector(board_context) if official_boards_available else {}
        official_board_member_ready = bool(board_members_by_sector)
        local_board_sectors = (
            self._official_board_sectors_from_snapshot(context, board_context, board_members_by_sector)
            if official_board_member_ready
            else []
        )
        display_sectors = (
            local_board_sectors
            if local_board_sectors
            else board_context.sectors
            if official_board_member_ready
            else context.sectors
        )
        requested_sector = self._normalize_sector(sector)
        display_sectors = self._decorate_sector_mini_charts(
            context,
            display_sectors,
            preferred_names={requested_sector} if requested_sector else None,
        )
        board_source = (
            "easy_tdx_cached_members_local_quote_aggregation"
            if local_board_sectors
            else board_context.source
            if official_board_member_ready
            else "signal_engine_theme_rank"
        )
        display_sector_names = {item.name for item in display_sectors}
        legacy_sector_names = {item.name for item in context.sectors}
        selected_sector = (
            requested_sector
            if requested_sector
            and (
                requested_sector in display_sector_names
                if official_boards_available
                else requested_sector in legacy_sector_names
            )
            else None
        )
        sector_focus = next(
            (item for item in display_sectors if item.name == selected_sector),
            None,
        ) if selected_sector else None
        selected_codes = self._selected_sector_codes(context, selected_sector, board_context)
        return self._build_stock_board(
            context,
            selected_sector=selected_sector,
            sector_codes=selected_codes,
            display_sectors=display_sectors,
            sector_focus=sector_focus,
            board_level=level,
            board_source=board_source,
            board_members_by_sector=board_members_by_sector,
            sort=sort,
            page=page,
            page_size=page_size,
            near_trend=near_trend,
            pin_buy=pin_buy,
        )

    def _preview_board_codes(
        self,
        context: DashboardContext,
        *,
        selected_sector: str | None,
        sector_codes: set[str],
        display_sectors: list[SectorSnapshot],
        sector_focus: SectorSnapshot | None,
        board_members_by_sector: dict[str, list[str]] | None,
        sort: str,
        page: int,
        limit: int,
    ) -> list[str]:
        allowed_sorts = {"activity", "change", "amount", "volume_ratio", "order_flow", "signal"}
        normalized_sort = sort if sort in allowed_sorts else "activity"
        normalized_limit = max(20, min(int(limit or 80), 240))
        normalized_page = max(1, int(page or 1))
        signal_by_code = {signal.code: signal for signal in context.signals_all}
        watch_codes = {item.code for item in context.watchlist}
        position_codes = {item.code for item in self.position_store.list_items()}
        theme_core_codes = {
            str(code).zfill(6)
            for theme in context.themes
            for code in theme.get("core_codes", [])
        }
        preferred_sector_names = self._manual_theme_names(context.themes)
        sector_by_code = self._quote_sector_map(display_sectors, board_members_by_sector or {})

        entries: list[tuple[tuple[Any, ...], str]] = []
        for quote in context.snapshot.quotes:
            if sector_codes and quote.code not in sector_codes:
                continue
            if selected_sector and sector_focus is not None:
                sector_snapshot = sector_focus
            elif sector_by_code:
                sector_snapshot = sector_by_code.get(quote.code)
            else:
                sector_snapshot = self._best_sector_for_quote(
                    quote,
                    context.sectors,
                    preferred_sector_names=preferred_sector_names,
                    requested_sector=selected_sector,
                )
            entries.append(
                (
                    self._board_sort_key_for_quote(
                        quote,
                        sector_snapshot,
                        normalized_sort,
                        theme_core_codes,
                        signal=signal_by_code.get(quote.code),
                        watchlisted=quote.code in watch_codes,
                        position=quote.code in position_codes,
                    ),
                    quote.code,
                )
            )
        entries.sort(key=lambda entry: entry[0])
        start = (normalized_page - 1) * normalized_limit
        return [code for _, code in entries[start:start + normalized_limit]]

    def _visible_quote_overrides(
        self,
        context: DashboardContext,
        visible_codes: list[str],
    ) -> tuple[dict[str, Quote], dict[str, Any]]:
        normalized_codes = list(
            dict.fromkeys(str(code or "").zfill(6) for code in visible_codes if str(code or "").strip())
        )
        status: dict[str, Any] = {
            "visible_quote_refresh_count": 0,
            "visible_quote_refresh_mode": "skipped",
        }
        if not normalized_codes:
            return {}, status
        if (
            context.market.frozen
            or context.snapshot.data_mode == "replay"
            or not is_trading_window()
        ):
            return {}, status
        limited_codes = normalized_codes[: self.settings.visible_quote_max_codes]
        trade_date = self._visible_quote_trade_date(context)
        cache_key = self._visible_quote_refresh_key(trade_date, limited_codes)
        overrides = self._cached_visible_quote_overrides(limited_codes, trade_date)
        had_cached_overrides = bool(overrides)
        refresh_state = self._ensure_visible_quote_refresh(context, limited_codes, cache_key)
        budget_seconds = max(0, self.settings.visible_quote_refresh_budget_ms) / 1000
        waited_for_refresh = False
        if (
            budget_seconds > 0
            and refresh_state.get("started")
            and not refresh_state.get("reused_running")
            and not overrides
        ):
            thread = refresh_state.get("thread")
            if isinstance(thread, threading.Thread):
                thread.join(budget_seconds)
                waited_for_refresh = True
                overrides = self._cached_visible_quote_overrides(limited_codes, trade_date)
        latest_at = self._latest_quote_timestamp(overrides.values())
        mode = "cache"
        if refresh_state.get("error"):
            mode = "error_cache" if overrides else "error"
        elif refresh_state.get("reused_running"):
            mode = "refreshing_cache" if overrides else "refreshing"
        elif refresh_state.get("started"):
            if had_cached_overrides:
                mode = "refreshing_cache"
            elif waited_for_refresh and overrides:
                mode = "subset"
            else:
                mode = "refreshing"
        elif refresh_state.get("throttled"):
            mode = "throttled_cache" if overrides else "throttled"
        status.update(
            {
                "visible_quote_refresh_mode": mode,
                "visible_quote_refresh_count": len(overrides),
                "visible_quote_refresh_requested": len(limited_codes),
                "visible_quote_refresh_at": latest_at,
                "visible_quote_refresh_budget_ms": self.settings.visible_quote_refresh_budget_ms,
                "visible_quote_refresh_cache_seconds": self.settings.visible_quote_cache_seconds,
            }
        )
        if refresh_state.get("error"):
            status["visible_quote_refresh_error"] = str(refresh_state["error"])
        return overrides, status

    @staticmethod
    def _visible_quote_trade_date(context: DashboardContext) -> str:
        return str(
            context.source_status.get("trade_date")
            or context.snapshot.source_status.get("trade_date")
            or ""
        )

    @staticmethod
    def _visible_quote_refresh_key(trade_date: str, codes: list[str]) -> str:
        return "|".join([trade_date, ",".join(codes)])

    @staticmethod
    def _visible_quote_cache_key(trade_date: str, code: str) -> str:
        return f"{trade_date}|{code}"

    def _cached_visible_quote_overrides(self, codes: list[str], trade_date: str) -> dict[str, Quote]:
        if not codes:
            return {}
        now = time.monotonic()
        ttl = self.settings.visible_quote_cache_seconds
        result: dict[str, Quote] = {}
        with self._visible_quote_lock:
            for code in codes:
                cached = self._visible_quote_cache.get(self._visible_quote_cache_key(trade_date, code))
                if cached is None:
                    continue
                if now - cached[0] <= ttl:
                    result[code] = cached[1]
        return result

    def _ensure_visible_quote_refresh(
        self,
        context: DashboardContext,
        codes: list[str],
        cache_key: str,
    ) -> dict[str, Any]:
        if not codes:
            return {"started": False}
        now = time.monotonic()
        with self._visible_quote_lock:
            error = self._visible_quote_refresh_errors_by_key.pop(cache_key, None)
            current = self._visible_quote_refresh_threads.get(cache_key)
            if current is not None and current.is_alive():
                state: dict[str, Any] = {"started": False, "reused_running": True, "thread": current}
                if error:
                    state["error"] = error[1]
                return state
            last_started_at = self._visible_quote_refresh_started_at_by_key.get(cache_key, 0.0)
            if now - last_started_at < self.settings.visible_quote_min_interval_seconds:
                state = {"started": False, "throttled": True}
                if error:
                    state["error"] = error[1]
                return state
            self._visible_quote_refresh_started_at_by_key[cache_key] = now
            thread = threading.Thread(
                target=self._refresh_visible_quotes_worker,
                args=(list(codes), list(context.snapshot.quotes), cache_key, self._visible_quote_trade_date(context)),
                name="watchtower-visible-quote-refresh",
                daemon=True,
            )
            self._visible_quote_refresh_threads[cache_key] = thread
            thread.start()
            state = {"started": True, "thread": thread}
            if error:
                state["error"] = error[1]
            return state

    def _refresh_visible_quotes_worker(
        self,
        codes: list[str],
        base_quotes: list[Quote],
        cache_key: str,
        trade_date: str,
    ) -> None:
        try:
            quotes = self.data_source.fetch_quote_subset(codes, base_quotes=base_quotes)
            overrides = {
                str(code).zfill(6): quote
                for code, quote in dict(quotes or {}).items()
                if isinstance(quote, Quote)
            }
            now = time.monotonic()
            with self._visible_quote_lock:
                for code, quote in overrides.items():
                    self._visible_quote_cache[self._visible_quote_cache_key(trade_date, code)] = (now, quote)
                if len(self._visible_quote_cache) > 512:
                    stale_codes = sorted(
                        self._visible_quote_cache,
                        key=lambda code: self._visible_quote_cache[code][0],
                    )[: len(self._visible_quote_cache) - 512]
                    for code in stale_codes:
                        self._visible_quote_cache.pop(code, None)
        except Exception as exc:
            with self._visible_quote_lock:
                self._visible_quote_refresh_errors_by_key[cache_key] = (time.monotonic(), str(exc))
        finally:
            with self._visible_quote_lock:
                current = self._visible_quote_refresh_threads.get(cache_key)
                if current is threading.current_thread():
                    self._visible_quote_refresh_threads.pop(cache_key, None)

    def _context_with_quote_overrides(
        self,
        context: DashboardContext,
        quote_overrides: dict[str, Quote],
    ) -> DashboardContext:
        if not quote_overrides:
            return context
        overrides = {str(code).zfill(6): quote for code, quote in quote_overrides.items()}
        quotes = [overrides.get(quote.code, quote) for quote in context.snapshot.quotes]
        latest_at = self._latest_quote_timestamp(overrides.values())
        snapshot_status = dict(context.snapshot.source_status)
        source_status = dict(context.source_status)
        if latest_at:
            snapshot_status["clock_label"] = latest_at
            snapshot_status["updated_at"] = latest_at
            source_status["clock_label"] = latest_at
            source_status["updated_at"] = latest_at
        snapshot = MarketSnapshot(
            quotes=quotes,
            indices=context.snapshot.indices,
            data_mode=context.snapshot.data_mode,
            source_status=snapshot_status,
        )
        market = context.market.model_copy(update={"updated_at": latest_at}) if latest_at else context.market
        return DashboardContext(
            watchlist=context.watchlist,
            themes=context.themes,
            snapshot=snapshot,
            market=market,
            sectors=context.sectors,
            sector_flow=context.sector_flow,
            signals_all=context.signals_all,
            core_watch=context.core_watch,
            events=context.events,
            source_status=source_status,
        )

    @staticmethod
    def _latest_quote_timestamp(quotes: Any) -> str:
        latest = ""
        for quote in quotes:
            label = str(getattr(quote, "updated_at", "") or "")
            if label and label > latest:
                latest = label
        return latest

    def _stock_board_item(
        self,
        quote: Quote,
        sector_snapshot: SectorSnapshot | None,
        display_sectors: list[SectorSnapshot],
        selected_sector: str | None,
        theme_core_codes: set[str],
        signal: TradeSignal | None = None,
        watch_item: WatchlistItem | None = None,
        position_item: PositionRecord | None = None,
    ) -> StockBoardItem:
        stock_type, stock_tags, is_leader, is_core = self._classify_stock(
            quote,
            sector_snapshot,
            theme_core_codes,
        )
        if position_item:
            stock_tags = list(dict.fromkeys(["持仓", *stock_tags]))
        sector_name = selected_sector or self._display_sector_name(quote, sector_snapshot)
        signal_value = signal or TradeSignal(
            code=quote.code,
            name=quote.name,
            signal=SignalType.WATCH,
            score=0,
            sector=self._display_sector_name(quote, sector_snapshot),
            price=quote.price,
            change_pct=quote.change_pct,
            rebound_from_low_pct=self._rebound_from_quote(quote),
            minute_amount_ratio=quote.minute_amount_ratio,
            reasons=[],
            risks=[],
            updated_at=quote.updated_at,
            auction=quote.auction,
            order_flow=quote.order_flow,
            signal_source="snapshot",
        )
        activity_score = self._activity_score(quote, sector_snapshot, signal_value, stock_tags)
        zuot_resistance, zuot_support = self._zuot_levels_for_quote(quote)
        last_action = self._last_action_for_code(quote.code)
        return StockBoardItem(
            code=quote.code,
            name=quote.name,
            themes=list(quote.themes),
            sector=sector_name,
            price=quote.price,
            change_pct=quote.change_pct,
            amount=quote.amount,
            minute_amount_ratio=quote.minute_amount_ratio,
            rebound_from_low_pct=round(self._rebound_from_quote(quote), 2),
            pullback_from_high_pct=round(self._pullback_from_quote(quote), 2),
            resistance=zuot_resistance,
            support=zuot_support,
            limit_up=quote.limit_up,
            limit_down=quote.limit_down,
            opened_limit=quote.opened_limit,
            signal=signal_value.signal,
            signal_score=signal_value.score,
            last_action=str(last_action.get("last_action") or ""),
            last_action_price=float(last_action.get("last_action_price") or 0),
            last_action_time=str(last_action.get("last_action_time") or ""),
            stock_type=stock_type,
            stock_tags=stock_tags,
            activity_score=round(activity_score, 1),
            sector_heat_score=sector_snapshot.heat_score if sector_snapshot else 0,
            sector_rank=self._sector_rank(display_sectors, sector_snapshot),
            leader=is_leader,
            core=is_core,
            watchlisted=watch_item is not None,
            position=position_item is not None,
            updated_at=quote.updated_at,
            order_flow=quote.order_flow,
            auction=quote.auction,
            factor_flags=list(signal_value.factor_flags),
            signal_grade=signal_value.signal_grade,
            phase=signal_value.phase,
            signal_time=signal_value.updated_at,
            invalidation_price=signal_value.invalidation_price,
            source_quality=signal_value.source_quality,
            exit_score=signal_value.exit_score,
            t_plus_one_restricted=signal_value.t_plus_one_restricted,
            risk_reward=signal_value.risk_reward,
        )

    def _mini_markers_for_board_item(self, item: StockBoardItem) -> list[MiniIntradayMarker]:
        try:
            signal = item.signal if isinstance(item.signal, SignalType) else SignalType(str(item.signal))
        except ValueError:
            return []
        if signal not in {SignalType.BUY_T, SignalType.SELL_T}:
            return []
        time_label = self._mini_time_label(item.signal_time or item.updated_at)
        if not time_label:
            return []
        base_reason = "公式买入原语" if signal == SignalType.BUY_T else "公式卖出原语"
        factor_reasons = [
            str(flag)
            for flag in item.factor_flags
            if str(flag) in {"公式买入原语", "公式卖出原语"}
        ]
        reasons = list(dict.fromkeys([base_reason, *factor_reasons]))
        return [
            MiniIntradayMarker(
                time=time_label,
                signal=signal,
                price=round(float(item.price or 0), 3),
                change_pct=round(float(item.change_pct or 0), 3),
                reasons=reasons,
            )
        ]

    def _mini_chart_with_board_marker(
        self,
        item: StockBoardItem,
        chart: MiniIntradaySeries | None,
    ) -> MiniIntradaySeries:
        base = chart or MiniIntradaySeries(source_quality="unavailable")
        markers = self._mini_markers_for_board_item(item)
        if not markers:
            return base
        return base.model_copy(update={"markers": markers})

    def _context_watch_previews(
        self,
        context: DashboardContext,
        mini_cache: dict[str, MiniIntradaySeries] | None = None,
        include_mini_charts: bool = True,
        quote_overrides: dict[str, Quote] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        watchlist = list(context.watchlist)
        positions = self.position_store.list_items()
        watch_codes = {item.code for item in watchlist}
        position_by_code = {item.code: item for item in positions}
        if not watch_codes and not position_by_code:
            return [], []
        signal_by_code = {signal.code: signal for signal in context.signals_all}
        theme_core_codes = {
            str(code).zfill(6)
            for theme in context.themes
            for code in theme.get("core_codes", [])
        }
        preferred_sector_names = self._manual_theme_names(context.themes)
        watch_items: list[StockBoardItem] = []
        position_items: list[tuple[StockBoardItem, PositionRecord]] = []
        quote_by_code = {quote.code: quote for quote in context.snapshot.quotes}
        if quote_overrides:
            quote_by_code.update({str(code).zfill(6): quote for code, quote in quote_overrides.items()})
        preview_codes = list(
            dict.fromkeys(
                [
                    *[item.code for item in watchlist],
                    *position_by_code.keys(),
                ]
            )
        )
        for code in preview_codes:
            quote = quote_by_code.get(code)
            if quote is None:
                continue
            sector_snapshot = self._best_sector_for_quote(
                quote,
                context.sectors,
                preferred_sector_names=preferred_sector_names,
            )
            watch_item = next((item for item in watchlist if item.code == quote.code), None)
            position_item = position_by_code.get(quote.code)
            board_item = self._stock_board_item(
                quote,
                sector_snapshot,
                context.sectors,
                None,
                theme_core_codes,
                signal=signal_by_code.get(quote.code),
                watch_item=watch_item,
                position_item=position_item,
            )
            if watch_item:
                watch_items.append(board_item)
            if position_item:
                position_items.append((board_item, position_item))
        watch_items.sort(key=lambda item: self._board_sort_key(item, "activity"))
        position_items.sort(key=lambda entry: self._board_sort_key(entry[0], "activity"))
        if include_mini_charts:
            requested_mini_codes = [item.code for item in watch_items] + [item.code for item, _ in position_items]
            existing_mini_cache = dict(mini_cache or {})
            missing_mini_codes = [code for code in requested_mini_codes if code not in existing_mini_cache]
            if missing_mini_codes:
                existing_mini_cache.update(self._stock_mini_charts_by_code(context, missing_mini_codes))
            mini_cache = existing_mini_cache
        else:
            mini_cache = {
                item.code: MiniIntradaySeries(source_quality="deferred")
                for item in [*watch_items, *[entry[0] for entry in position_items]]
            }
        watch_items = [
            item.model_copy(
                update={
                    "mini_chart": self._mini_chart_with_board_marker(
                        item,
                        mini_cache.get(item.code, MiniIntradaySeries(source_quality="unavailable")),
                    )
                }
            )
            for item in watch_items
        ]
        watch_preview = [item.model_dump(mode="json") for item in watch_items]
        position_preview = []
        for board_item, position_item in position_items:
            board_item = board_item.model_copy(
                update={
                    "mini_chart": self._mini_chart_with_board_marker(
                        board_item,
                        mini_cache.get(board_item.code, MiniIntradaySeries(source_quality="unavailable")),
                    )
                }
            )
            row = board_item.model_dump(mode="json")
            row["name"] = position_item.name or row.get("name") or position_item.code
            row["cost"] = position_item.cost
            row["quantity"] = position_item.quantity
            row["available_quantity"] = position_item.available_quantity
            row["t_allocation_pct"] = position_item.t_allocation_pct
            row["entry_date"] = position_item.entry_date
            row["position_updated_at"] = position_item.updated_at
            row["position_notes"] = position_item.notes
            row["position"] = True
            row["watchlisted"] = bool(row.get("watchlisted"))
            position_preview.append(row)
        return watch_preview, position_preview

    @staticmethod
    def _context_trade_date(context: DashboardContext) -> str:
        return str(
            context.source_status.get("trade_date")
            or context.snapshot.source_status.get("trade_date")
            or china_now().strftime("%Y%m%d")
        )

    @staticmethod
    def _mini_time_label(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        token = text.rsplit(" ", 1)[-1].rsplit("T", 1)[-1]
        return token[:5] if ":" in token else text[:5]

    @staticmethod
    def _minutes_between(earlier: str, later: str) -> int:
        """两个 HH:MM 标签之间相差的分钟数，解析失败按 1 处理。"""
        try:
            a = int(str(earlier)[:2]) * 60 + int(str(earlier)[3:5])
            b = int(str(later)[:2]) * 60 + int(str(later)[3:5])
            return max(1, b - a)
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _mini_row_time_label(row: dict[str, Any]) -> str:
        return DashboardService._mini_time_label(row.get("captured_at") or row.get("updated_at"))

    @staticmethod
    def _is_regular_mini_time(label: str) -> bool:
        if not label or len(label) < 5:
            return False
        return ("09:30" <= label <= "11:30") or ("13:00" <= label <= "15:00")

    @staticmethod
    def _regular_mini_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        regular = [
            row
            for row in rows
            if DashboardService._is_regular_mini_time(DashboardService._mini_row_time_label(row))
        ]
        return regular if len(regular) >= 2 else rows

    @staticmethod
    def _dedupe_mini_rows_by_minute(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        last_label = ""
        for row in rows:
            label = DashboardService._mini_row_time_label(row)
            if label and label == last_label and deduped:
                deduped[-1] = row
                continue
            deduped.append(row)
            last_label = label
        return deduped

    @staticmethod
    def _compress_flat_mini_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(rows) <= 2:
            return rows

        def value_for(row: dict[str, Any]) -> float:
            return round(
                DashboardService._safe_float(
                    row.get("change_pct", row.get("avg_change_pct", row.get("price", 0)))
                ),
                3,
            )

        compressed: list[dict[str, Any]] = []
        run: list[dict[str, Any]] = []
        run_value: float | None = None
        for row in rows:
            value = value_for(row)
            if run and value != run_value:
                compressed.append(run[0])
                if len(run) > 1:
                    compressed.append(run[-1])
                run = []
            run.append(row)
            run_value = value
        if run:
            compressed.append(run[0])
            if len(run) > 1:
                compressed.append(run[-1])

        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in compressed:
            key = f"{DashboardService._mini_row_time_label(row)}|{value_for(row)}"
            if key in seen:
                continue
            seen.add(key)
            output.append(row)
        return output

    @staticmethod
    def _sample_mini_rows(rows: list[dict[str, Any]], max_points: int = 48) -> list[dict[str, Any]]:
        if not rows:
            return []
        rows = DashboardService._compress_flat_mini_rows(
            DashboardService._dedupe_mini_rows_by_minute(DashboardService._regular_mini_rows(rows))
        )
        limit = max(8, min(int(max_points or 48), 48))
        if len(rows) <= limit:
            return rows
        if limit <= 1:
            return [rows[-1]]
        last_index = len(rows) - 1
        values = [
            DashboardService._safe_float(
                row.get("change_pct", row.get("avg_change_pct", row.get("price", 0)))
            )
            for row in rows
        ]
        indexes: set[int] = {0, last_index}
        if values:
            indexes.add(min(range(len(values)), key=lambda index: values[index]))
            indexes.add(max(range(len(values)), key=lambda index: values[index]))

        bucket_count = max(4, min(12, limit // 2))
        for bucket in range(bucket_count):
            start = int(bucket * len(rows) / bucket_count)
            end = int((bucket + 1) * len(rows) / bucket_count)
            if end <= start:
                continue
            bucket_indexes = range(start, end)
            indexes.add(min(bucket_indexes, key=lambda index: values[index]))
            indexes.add(max(bucket_indexes, key=lambda index: values[index]))

        fill_slots = max(limit * 2, limit)
        for index in range(fill_slots):
            if len(indexes) >= limit:
                break
            indexes.add(round(index * last_index / max(fill_slots - 1, 1)))
        if len(indexes) > limit:
            ordered = sorted(indexes)
            keep = {0, last_index}
            if values:
                keep.add(min(range(len(values)), key=lambda index: values[index]))
                keep.add(max(range(len(values)), key=lambda index: values[index]))
            remaining = [index for index in ordered if index not in keep]
            target = max(0, limit - len(keep))
            if target and remaining:
                step = (len(remaining) - 1) / max(target - 1, 1)
                keep.update(remaining[round(pos * step)] for pos in range(target))
            indexes = keep
        return [rows[index] for index in sorted(indexes)]

    @staticmethod
    def _rolling_mean(values: list[float]) -> list[float]:
        output: list[float] = []
        total = 0.0
        for index, value in enumerate(values, start=1):
            total += value
            output.append(round(total / index, 3))
        return output

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if isfinite(number) else default

    def _mini_chart_for_stock(
        self,
        context: DashboardContext,
        code: str,
        cache: dict[str, MiniIntradaySeries] | None = None,
    ) -> MiniIntradaySeries:
        normalized = str(code or "").zfill(6)
        if cache is not None and normalized in cache:
            return cache[normalized]
        loader = getattr(self.trajectory_store, "stock_feature_mini_series", None)
        if callable(loader):
            rows = loader(
                self._context_trade_date(context),
                normalized,
                max_rows=MINI_CHART_REPRESENTATIVE_ROWS,
            )
        else:
            rows = self.trajectory_store.stock_feature_series(
                self._context_trade_date(context),
                normalized,
                max_rows=MINI_CHART_SOURCE_ROWS,
            )
        chart = self._mini_chart_from_stock_rows(rows)
        if cache is not None:
            cache[normalized] = chart
        return chart

    def _stock_mini_charts_by_code(
        self,
        context: DashboardContext,
        codes: list[str],
    ) -> dict[str, MiniIntradaySeries]:
        """Stale-while-revalidate：请求路径零 SQLite 读取。

        80GB 轨迹库上每票约 40ms 的采样查询曾让首屏同步等 1.7s+；
        现在命中缓存（含过期缓存）直接返回，缺失/过期由后台 worker 批量
        预热，下一轮 WS 增量自然带出真实曲线。
        """
        started_at = time.perf_counter()
        self._last_stock_mini_chart_elapsed_ms = 0.0
        self._last_stock_mini_chart_missing_count = 0
        self._last_stock_mini_chart_loaded_count = 0
        normalized_codes = list(dict.fromkeys(str(code or "").zfill(6) for code in codes if str(code or "").strip()))
        if not normalized_codes:
            return {}
        trade_date = self._context_trade_date(context)
        now_ts = time.time()
        ttl = 30.0 if not context.market.frozen else float(self.settings.terminal_context_frozen_cache_seconds)
        result: dict[str, MiniIntradaySeries] = {}
        refresh: list[str] = []
        missing = 0
        loaded = 0
        for code in normalized_codes:
            entry = self._stock_mini_chart_cache.get((trade_date, code))
            if entry and now_ts - entry[0] <= ttl:
                result[code] = entry[1]
                loaded += 1
            elif entry is not None:
                result[code] = entry[1]  # 过期先用着，后台刷新，避免闪烁
                refresh.append(code)
                loaded += 1
            else:
                result[code] = MiniIntradaySeries(source_quality="deferred")
                refresh.append(code)
                missing += 1
        if refresh:
            self._schedule_mini_chart_warm(trade_date, refresh)
        self._last_stock_mini_chart_missing_count = missing
        self._last_stock_mini_chart_loaded_count = loaded
        self._last_stock_mini_chart_elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        return result

    def _schedule_mini_chart_warm(self, trade_date: str, codes: list[str]) -> None:
        if not trade_date or not codes:
            return
        with self._mini_chart_warm_lock:
            new_codes = [code for code in codes if (trade_date, code) not in self._mini_chart_warm_pending]
            if not new_codes:
                return
            self._mini_chart_warm_pending.update((trade_date, code) for code in new_codes)
            thread = self._mini_chart_warm_thread
            if thread is not None and thread.is_alive():
                return
            self._mini_chart_warm_thread = threading.Thread(
                target=self._mini_chart_warm_worker,
                name="mini-chart-warm",
                daemon=True,
            )
            self._mini_chart_warm_thread.start()

    def _mini_rows_need_session_fallback(self, rows: list[dict[str, Any]], trade_date: str) -> bool:
        """轨迹特征只覆盖盘中活跃子集；新点开/新入榜的票可能从盘中才开始有行。

        行数不足、或当前已过开盘而样本仍从盘中才开始时，回退 easy_tdx 全天
        分钟线补出早盘段，避免缩略图只显示「打开之后」的半段。
        """
        regular = [
            label
            for label in (self._mini_row_time_label(row) for row in rows)
            if self._is_regular_mini_time(label)
        ]
        if len(regular) < 2:
            return True
        today = china_now().strftime("%Y%m%d")
        current_label = china_now().strftime("%H:%M") if str(trade_date) == today else "15:00"
        if current_label <= "09:35":
            return False
        if regular[0] > "09:35":
            return True
        return bool(current_label >= "14:55" and regular[-1] < "14:50")

    def _mini_chart_fallback_rows_by_code(
        self,
        trade_date: str,
        codes: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """用 easy_tdx 当日分钟线补轨迹库未覆盖/起步过晚的榜单缩略图。"""
        if not codes:
            return {}
        context = getattr(self, "_context_cache", None)
        quotes = list(getattr(getattr(context, "snapshot", None), "quotes", []) or [])
        quote_by_code = {quote.code: quote for quote in quotes}
        today = china_now().strftime("%Y%m%d")
        live = bool(
            str(trade_date) == today
            and is_trading_window()
            and not bool(getattr(getattr(context, "market", None), "frozen", False))
        )

        def load(code: str) -> tuple[str, list[dict[str, Any]]]:
            try:
                minute_rows = self.data_source.fetch_minute_series(code, trade_date, live=live)
            except Exception:
                return code, []
            quote = quote_by_code.get(code)
            base = float(
                getattr(quote, "prev_close", 0)
                or getattr(quote, "open", 0)
                or next((float(row.get("price") or 0) for row in minute_rows if float(row.get("price") or 0) > 0), 0.0)
            )
            output: list[dict[str, Any]] = []
            for row in minute_rows:
                label = self._mini_time_label(row.get("time") or row.get("captured_at"))
                if not self._is_regular_mini_time(label):
                    continue
                try:
                    price = float(row.get("price") or 0)
                    amount = max(float(row.get("amount") or 0), 0.0)
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                output.append(
                    {
                        "captured_at": label,
                        "price": price,
                        "change_pct": round((price / base - 1.0) * 100.0, 3) if base > 0 else 0.0,
                        "amount": amount,
                        "minute_amount": amount,
                        "minute_amount_ratio": 1.0,
                    }
                )
            return code, output

        result: dict[str, list[dict[str, Any]]] = {}
        workers = min(4, len(codes))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mini-chart-fallback") as executor:
            futures = [executor.submit(load, code) for code in codes]
            for future in as_completed(futures):
                code, rows = future.result()
                if len(rows) >= 2:
                    result[code] = rows
        return result

    def _mini_chart_warm_worker(self) -> None:
        while True:
            with self._mini_chart_warm_lock:
                batch = sorted(self._mini_chart_warm_pending)[:60]
                self._mini_chart_warm_pending.difference_update(batch)
            if not batch:
                return
            codes_by_date: dict[str, list[str]] = {}
            for date, code in batch:
                codes_by_date.setdefault(date, []).append(code)
            for date, codes in codes_by_date.items():
                try:
                    loader = getattr(self.trajectory_store, "stock_feature_mini_series_by_code", None)
                    if callable(loader):
                        rows_by_code = loader(date, codes, max_rows=MINI_CHART_REPRESENTATIVE_ROWS)
                    else:
                        fallback = getattr(self.trajectory_store, "stock_feature_series_by_code", None)
                        rows_by_code = fallback(date, codes, max_rows=MINI_CHART_SOURCE_ROWS) if callable(fallback) else {}
                    rows_by_code = dict(rows_by_code or {})
                    fallback_codes = [
                        code
                        for code in codes
                        if self._mini_rows_need_session_fallback(list(rows_by_code.get(code) or []), date)
                    ]
                    fallback_rows_by_code = self._mini_chart_fallback_rows_by_code(date, fallback_codes)
                    now_ts = time.time()
                    for code in codes:
                        rows = list(rows_by_code.get(code) or [])
                        source_quality = "trajectory_stock_features"
                        if code in fallback_rows_by_code:
                            rows = fallback_rows_by_code[code]
                            source_quality = "easy_tdx_minute_fallback"
                        chart = self._mini_chart_from_stock_rows(rows)
                        if rows and source_quality == "easy_tdx_minute_fallback":
                            chart = chart.model_copy(update={"source_quality": source_quality})
                        # 无数据也写入 unavailable：30s TTL 内不再反复排队扫库/请求上游。
                        self._stock_mini_chart_cache[(date, code)] = (now_ts, chart)
                except Exception:  # pragma: no cover - 预热失败不影响请求路径
                    pass
            self._trim_mini_chart_caches()

    def _mini_chart_for_sector(
        self,
        context: DashboardContext,
        sector_name: str,
        cache: dict[str, MiniIntradaySeries] | None = None,
    ) -> MiniIntradaySeries:
        normalized = str(sector_name or "").strip()
        if cache is not None and normalized in cache:
            return cache[normalized]
        rows = self.trajectory_store.sector_feature_series(
            self._context_trade_date(context),
            normalized,
            max_rows=180,
        )
        chart = self._mini_chart_from_sector_rows(rows)
        if cache is not None:
            cache[normalized] = chart
        return chart

    def _sector_mini_charts_by_name(
        self,
        context: DashboardContext,
        sector_names: list[str],
    ) -> dict[str, MiniIntradaySeries]:
        normalized_names = list(dict.fromkeys(str(name or "").strip() for name in sector_names if str(name or "").strip()))
        if not normalized_names:
            return {}
        trade_date = self._context_trade_date(context)
        now_ts = time.time()
        ttl = 2.0 if not context.market.frozen else float(self.settings.terminal_context_frozen_cache_seconds)
        cached: dict[str, MiniIntradaySeries] = {}
        missing: list[str] = []
        for name in normalized_names:
            entry = self._sector_mini_chart_cache.get((trade_date, name))
            if entry and now_ts - entry[0] <= ttl:
                cached[name] = entry[1]
            else:
                missing.append(name)
        if not missing:
            return cached
        if (
            is_trading_window()
            and not context.market.frozen
            and len(normalized_names) > 1
        ):
            return {
                name: cached.get(name) or MiniIntradaySeries(source_quality="deferred")
                for name in normalized_names
            }
        batch_loader = getattr(self.trajectory_store, "sector_feature_series_by_name", None)
        if callable(batch_loader):
            try:
                rows_by_name = batch_loader(trade_date, missing, max_rows=180)
            except Exception:
                rows_by_name = {}
            loaded = {
                name: self._mini_chart_from_sector_rows(list(rows_by_name.get(name) or []))
                for name in missing
                if rows_by_name.get(name)
            }
            for name, chart in loaded.items():
                self._sector_mini_chart_cache[(trade_date, name)] = (now_ts, chart)
            result = {
                name: loaded.get(name)
                or cached.get(name)
                or self._sector_mini_chart_cache.get((trade_date, name), (0, MiniIntradaySeries(source_quality="unavailable")))[1]
                for name in normalized_names
            }
            self._trim_mini_chart_caches()
            return result
        result = dict(cached)
        for name in missing:
            chart = self._mini_chart_for_sector(context, name)
            result[name] = chart
            if chart.source_quality != "unavailable":
                self._sector_mini_chart_cache[(trade_date, name)] = (now_ts, chart)
        self._trim_mini_chart_caches()
        return result

    def _trim_mini_chart_caches(self) -> None:
        if len(self._stock_mini_chart_cache) > 640:
            for key in list(self._stock_mini_chart_cache)[: len(self._stock_mini_chart_cache) - 480]:
                self._stock_mini_chart_cache.pop(key, None)
        if len(self._sector_mini_chart_cache) > 240:
            for key in list(self._sector_mini_chart_cache)[: len(self._sector_mini_chart_cache) - 180]:
                self._sector_mini_chart_cache.pop(key, None)

    def _decorate_sector_mini_charts(
        self,
        context: DashboardContext,
        sectors: list[SectorSnapshot],
        *,
        preferred_names: set[str | None] | None = None,
    ) -> list[SectorSnapshot]:
        if not sectors:
            return []
        front_names = [sector.name for sector in sectors[:48]]
        preferred = [
            str(name).strip()
            for name in (preferred_names or set())
            if str(name or "").strip()
        ]
        chart_names = list(dict.fromkeys([*front_names, *preferred]))
        cache = self._sector_mini_charts_by_name(context, chart_names)
        return [
            sector.model_copy(
                update={"mini_chart": cache.get(sector.name, MiniIntradaySeries(source_quality="unavailable"))}
            )
            for sector in sectors
        ]

    def _mini_chart_from_stock_rows(self, rows: list[dict[str, Any]]) -> MiniIntradaySeries:
        if not rows:
            return MiniIntradaySeries(source_quality="unavailable")
        sampled = self._sample_mini_rows(rows)
        price_pcts = [round(self._safe_float(row.get("change_pct")), 3) for row in sampled]
        volume_ratios = [
            round(max(0.0, min(9.99, self._safe_float(row.get("minute_amount_ratio"), 1))), 3)
            for row in sampled
        ]
        vwap_pcts: list[float] = []
        cumulative_amount = 0.0
        cumulative_volume = 0.0
        prev_close = next(
            (
                self._safe_float(row.get("prev_close"))
                for row in rows
                if self._safe_float(row.get("prev_close")) > 0
            ),
            0.0,
        )
        for row in sampled:
            price = self._safe_float(row.get("price"))
            amount = max(self._safe_float(row.get("amount")), 0.0)
            if price > 0 and amount > 0:
                cumulative_amount += amount
                cumulative_volume += amount / price
            if prev_close > 0 and cumulative_volume > 0:
                vwap_price = cumulative_amount / cumulative_volume
                vwap_pcts.append(round((vwap_price - prev_close) / prev_close * 100, 3))
            else:
                vwap_pcts.append(round(sum(price_pcts[: len(vwap_pcts) + 1]) / (len(vwap_pcts) + 1), 3))
        return MiniIntradaySeries(
            times=[self._mini_time_label(row.get("captured_at") or row.get("updated_at")) for row in sampled],
            price_pcts=price_pcts,
            vwap_pcts=vwap_pcts,
            volume_ratios=volume_ratios,
            latest_change_pct=price_pcts[-1] if price_pcts else 0,
            source_quality="trajectory_stock_features" if len(rows) >= 2 else "trajectory_sparse",
            point_count=len(sampled),
        )

    def _mini_chart_from_sector_rows(self, rows: list[dict[str, Any]]) -> MiniIntradaySeries:
        if not rows:
            return MiniIntradaySeries(source_quality="unavailable")
        sampled = self._sample_mini_rows(rows)
        price_pcts = [round(self._safe_float(row.get("avg_change_pct")), 3) for row in sampled]
        heat_values = [max(0.0, min(100.0, self._safe_float(row.get("heat_score")))) for row in sampled]
        volume_ratios = [round(max(0.1, value / 50.0), 3) for value in heat_values]
        return MiniIntradaySeries(
            times=[self._mini_time_label(row.get("captured_at")) for row in sampled],
            price_pcts=price_pcts,
            vwap_pcts=self._rolling_mean(price_pcts),
            volume_ratios=volume_ratios,
            latest_change_pct=price_pcts[-1] if price_pcts else 0,
            source_quality="trajectory_sector_aggregation" if len(rows) >= 2 else "trajectory_sparse",
            point_count=len(sampled),
        )

    def _ensure_terminal_warmup(self, context: DashboardContext) -> None:
        return

    def _warm_terminal_views(self, context: DashboardContext) -> None:
        for level in (3, 1, 2):
            try:
                self._terminal_payload_for_context(
                    context,
                    sector=None,
                    board_level=level,
                    sort="activity",
                    page=1,
                    page_size=80,
                )
            except Exception:
                continue

    def _build_stock_board(
        self,
        context: DashboardContext,
        selected_sector: str | None,
        sector_codes: set[str],
        display_sectors: list[SectorSnapshot],
        sector_focus: SectorSnapshot | None,
        board_level: int,
        board_source: str,
        board_members_by_sector: dict[str, list[str]] | None,
        sort: str,
        page: int,
        page_size: int,
        include_mini_charts: bool = True,
        near_trend: bool = False,
        pin_buy: bool = False,
    ) -> StockBoardPayload:
        allowed_sorts = {"activity", "change", "amount", "volume_ratio", "order_flow", "signal"}
        normalized_sort = sort if sort in allowed_sorts else "activity"
        normalized_page_size = max(20, min(int(page_size or 80), 240))
        normalized_page = max(1, int(page or 1))
        entries = self._stock_board_entries(
            context,
            selected_sector=selected_sector,
            sector_codes=sector_codes,
            display_sectors=display_sectors,
            sector_focus=sector_focus,
            board_members_by_sector=board_members_by_sector,
            sort=normalized_sort,
        )
        near_trend_ready = 0
        near_trend_pending = 0
        if near_trend:
            entries, near_trend_ready, near_trend_pending = self._near_trend_filter_entries(entries)
        if pin_buy:
            entries = self._pin_buy_entries(entries)
        total = len(entries)
        page_count = max(1, (total + normalized_page_size - 1) // normalized_page_size)
        normalized_page = min(normalized_page, page_count)
        visible_entries = self._visible_board_entries(
            entries,
            page=normalized_page,
            page_size=normalized_page_size,
        )
        return self._stock_board_from_entries(
            context,
            visible_entries,
            total=total,
            normalized_sort=normalized_sort,
            normalized_page=normalized_page,
            normalized_page_size=normalized_page_size,
            selected_sector=selected_sector,
            display_sectors=display_sectors,
            board_level=board_level,
            board_source=board_source,
            include_mini_charts=include_mini_charts,
            near_trend=near_trend,
            near_trend_ready=near_trend_ready,
            near_trend_pending=near_trend_pending,
            pin_buy=pin_buy,
        )

    def _near_trend_filter_entries(self, entries: list[BoardEntry]) -> tuple[list[BoardEntry], int, int]:
        """低吸区间过滤：短期/长期线低者为底（-1%）、高者为顶（+3%），现价落区间内。

        线值来自 TrendLineStore（当天持久缓存）；缺失代码后台补算，本轮先不命中，
        返回 (过滤后 entries, 已有线值代码数, 待补算代码数)。
        """
        today = china_now().strftime("%Y%m%d")
        fresh = self.trend_line_store.get_fresh((entry.quote.code for entry in entries), today)
        missing = [entry.quote.code for entry in entries if entry.quote.code not in fresh]
        if missing:
            self.trend_line_store.ensure(missing, today)
        matched = [
            entry
            for entry in entries
            if entry.quote.code in fresh
            and near_trend_match(float(entry.quote.price or 0), fresh[entry.quote.code])
        ]
        return matched, len(fresh), len(missing)

    def _pin_buy_distance(self, entry: BoardEntry) -> float | None:
        """最近信号是买T 且现价 ≤ 买点+1% 时返回距离（%，可为负），否则 None。"""
        action = self._last_action_for_code(entry.quote.code)
        if str(action.get("last_action") or "") != SignalType.BUY_T.value:
            return None
        price = float(entry.quote.price or 0)
        action_price = float(action.get("last_action_price") or 0)
        if price <= 0 or action_price <= 0:
            return None
        distance = (price / action_price - 1.0) * 100.0
        return distance if distance <= 1.0 else None

    def _pin_buy_entries(self, entries: list[BoardEntry]) -> list[BoardEntry]:
        """置顶买点过滤：只保留靠近上一个买点的票（现价 ≤ 买点+1%），保持原排序。"""
        return [entry for entry in entries if self._pin_buy_distance(entry) is not None]

    def _fast_stock_board_entries(
        self,
        context: DashboardContext,
        *,
        selected_sector: str | None,
        sector_codes: set[str],
        display_sectors: list[SectorSnapshot],
        sector_focus: SectorSnapshot | None,
        board_members_by_sector: dict[str, list[str]] | None,
        sort: str,
    ) -> list[BoardEntry]:
        source_status = {**context.snapshot.source_status, **context.source_status}
        key = "|".join(
            [
                str(source_status.get("trade_date") or ""),
                str(source_status.get("updated_at") or source_status.get("clock_label") or context.market.updated_at or ""),
                str(source_status.get("active_source") or ""),
                str(context.snapshot.data_mode or ""),
                str(bool(context.market.frozen)),
                str(len(context.snapshot.quotes)),
                str(len(display_sectors)),
                str(selected_sector or ""),
                sort,
                self._watchlist_signature(context.watchlist),
            ]
        )
        now = time.monotonic()
        cached = self._fast_board_entries_cache.get(key)
        if cached is not None and now - cached[0] < 1.25:
            return cached[1]
        entries = self._stock_board_entries(
            context,
            selected_sector=selected_sector,
            sector_codes=sector_codes,
            display_sectors=display_sectors,
            sector_focus=sector_focus,
            board_members_by_sector=board_members_by_sector,
            sort=sort,
        )
        self._fast_board_entries_cache[key] = (now, entries)
        if len(self._fast_board_entries_cache) > 8:
            oldest = min(self._fast_board_entries_cache, key=lambda item: self._fast_board_entries_cache[item][0])
            self._fast_board_entries_cache.pop(oldest, None)
        return entries

    def _stock_board_entries(
        self,
        context: DashboardContext,
        *,
        selected_sector: str | None,
        sector_codes: set[str],
        display_sectors: list[SectorSnapshot],
        sector_focus: SectorSnapshot | None,
        board_members_by_sector: dict[str, list[str]] | None,
        sort: str,
    ) -> list[BoardEntry]:
        signal_by_code = {signal.code: signal for signal in context.signals_all}
        watchlist_by_code = {item.code: item for item in context.watchlist}
        position_by_code = {item.code: item for item in self.position_store.list_items()}
        theme_core_codes = {
            str(code).zfill(6)
            for theme in context.themes
            for code in theme.get("core_codes", [])
        }
        preferred_sector_names = self._manual_theme_names(context.themes)
        sector_by_code = self._quote_sector_map(display_sectors, board_members_by_sector or {})

        entries: list[BoardEntry] = []
        for quote in context.snapshot.quotes:
            if sector_codes and quote.code not in sector_codes:
                continue
            if selected_sector and sector_focus is not None:
                sector_snapshot = sector_focus
            elif sector_by_code:
                sector_snapshot = sector_by_code.get(quote.code)
            else:
                sector_snapshot = self._best_sector_for_quote(
                    quote,
                    context.sectors,
                    preferred_sector_names=preferred_sector_names,
                    requested_sector=selected_sector,
                )
            entries.append(
                BoardEntry(
                    sort_key=
                    self._board_sort_key_for_quote(
                        quote,
                        sector_snapshot,
                        sort,
                        theme_core_codes,
                        signal=signal_by_code.get(quote.code),
                        watchlisted=quote.code in watchlist_by_code,
                        position=quote.code in position_by_code,
                    ),
                    quote=quote,
                    sector=sector_snapshot,
                )
            )

        entries.sort(key=lambda entry: entry.sort_key)
        return entries

    @staticmethod
    def _visible_board_entries(
        entries: list[BoardEntry],
        *,
        page: int,
        page_size: int,
    ) -> list[BoardEntry]:
        start = (page - 1) * page_size
        return entries[start:start + page_size]

    @staticmethod
    def _entries_with_quote_overrides(
        entries: list[BoardEntry],
        quote_overrides: dict[str, Quote],
    ) -> list[BoardEntry]:
        if not quote_overrides:
            return entries
        overrides = {str(code).zfill(6): quote for code, quote in quote_overrides.items()}
        return [
            BoardEntry(
                sort_key=entry.sort_key,
                quote=overrides.get(entry.quote.code, entry.quote),
                sector=entry.sector,
            )
            for entry in entries
        ]

    def _last_action_updates_for_entries(
        self,
        entries: list[BoardEntry],
        trade_date: str,
    ) -> dict[str, dict[str, Any]]:
        """当天最近一次做T买/卖点（方向 + 触发价）。

        优先读内存事件表（信号发生/详情全天公式时写入）；缓存未覆盖的可见页
        再按全天 tick 窗口扫本地特征，避免信号回到「观察」后买卖点消失。
        """
        updates: dict[str, dict[str, Any]] = {}
        if not entries or not trade_date:
            return updates
        trajectory_enabled = bool(getattr(self.trajectory_store, "enabled", False))
        for entry in entries:
            quote = entry.quote
            cached = self._last_action_for_code(quote.code)
            if cached:
                updates[quote.code] = cached
                continue
            if not trajectory_enabled:
                continue
            try:
                rows = self._feature_series_small_cached(trade_date, quote.code, max_rows=2880)
                if len(rows) < 2:
                    continue
                # 全天窗口：找当天最后一次买/卖触发，而非首页提升用的最近几根
                event = self.engine._latest_cached_zuot_event(rows, quote, recent_bars=len(rows) + 1)
            except Exception:
                continue
            if not event:
                continue
            price = float(event.get("price") or 0)
            if price <= 0:
                continue
            self._record_last_action_event(quote.code, event["signal"], price, event.get("time"))
            cached = self._last_action_for_code(quote.code)
            if cached:
                updates[quote.code] = cached
        return updates

    def _stock_board_from_entries(
        self,
        context: DashboardContext,
        visible_entries: list[BoardEntry],
        *,
        total: int,
        normalized_sort: str,
        normalized_page: int,
        normalized_page_size: int,
        selected_sector: str | None,
        display_sectors: list[SectorSnapshot],
        board_level: int,
        board_source: str,
        include_mini_charts: bool,
        near_trend: bool = False,
        near_trend_ready: int = 0,
        near_trend_pending: int = 0,
        pin_buy: bool = False,
    ) -> StockBoardPayload:
        signal_by_code = {signal.code: signal for signal in context.signals_all}
        trade_date = str(
            context.source_status.get("trade_date")
            or context.snapshot.source_status.get("trade_date")
            or china_now().strftime("%Y%m%d")
        )
        last_action_updates = self._last_action_updates_for_entries(visible_entries, trade_date)
        watchlist_by_code = {item.code: item for item in context.watchlist}
        position_by_code = {item.code: item for item in self.position_store.list_items()}
        theme_core_codes = {
            str(code).zfill(6)
            for theme in context.themes
            for code in theme.get("core_codes", [])
        }
        visible = [
            self._stock_board_item(
                entry.quote,
                entry.sector,
                display_sectors,
                selected_sector,
                theme_core_codes,
                signal=signal_by_code.get(entry.quote.code),
                watch_item=watchlist_by_code.get(entry.quote.code),
                position_item=position_by_code.get(entry.quote.code),
            )
            for entry in visible_entries
        ]
        if include_mini_charts:
            mini_cache = self._stock_mini_charts_by_code(context, [item.code for item in visible])
            self._last_visible_mini_chart_cache = dict(mini_cache)
        else:
            mini_cache = {item.code: MiniIntradaySeries(source_quality="deferred") for item in visible}
            self._last_visible_mini_chart_cache = {}
            self._last_stock_mini_chart_elapsed_ms = 0.0
            self._last_stock_mini_chart_missing_count = 0
            self._last_stock_mini_chart_loaded_count = 0
        visible = [
            item.model_copy(
                update={
                    "mini_chart": self._mini_chart_with_board_marker(
                        item,
                        mini_cache.get(item.code, MiniIntradaySeries(source_quality="unavailable")),
                    )
                }
            )
            for item in visible
        ]
        # 低吸区间标记：只针对当前可见页（≤page_size 只）查当天线值缓存，
        # 缺失的后台补算——不开「低吸机会」开关时也能在压/支列提示，且量有界。
        if visible:
            today = china_now().strftime("%Y%m%d")
            visible_codes = [item.code for item in visible]
            fresh_lines = self.trend_line_store.get_fresh(visible_codes, today)
            missing_codes = [code for code in visible_codes if code not in fresh_lines]
            if missing_codes:
                self.trend_line_store.ensure(missing_codes, today)
            visible = [
                item.model_copy(
                    update={
                        "near_zone": near_trend_match(float(item.price or 0), fresh_lines[item.code])
                        if item.code in fresh_lines
                        else False,
                        **last_action_updates.get(item.code, {}),
                    }
                )
                for item in visible
            ]
        updated_at = str(context.source_status.get("clock_label") or context.market.updated_at)
        return StockBoardPayload(
            scope="full_market" if self.settings.scan_scope == "full_market" else self.settings.scan_scope,
            selected_sector=selected_sector,
            board_level=board_level,
            board_source=board_source,
            sort=normalized_sort,
            page=normalized_page,
            page_size=normalized_page_size,
            total=total,
            updated_at=updated_at,
            data_mode=context.snapshot.data_mode,
            frozen=context.market.frozen,
            items=visible,
            near_trend=near_trend,
            near_trend_ready=near_trend_ready,
            near_trend_pending=near_trend_pending,
            pin_buy=pin_buy,
        )

    def _board_sort_key_for_quote(
        self,
        quote: Quote,
        sector: SectorSnapshot | None,
        sort: str,
        theme_core_codes: set[str],
        *,
        signal: TradeSignal | None = None,
        watchlisted: bool = False,
        position: bool = False,
    ) -> tuple[Any, ...]:
        _stock_type, stock_tags, _is_leader, _is_core = self._classify_stock(
            quote,
            sector,
            theme_core_codes,
        )
        signal_type = signal.signal if signal is not None else SignalType.WATCH
        signal_score = signal.score if signal is not None else 0
        activity_score = self._activity_score_for_values(
            quote,
            sector,
            signal_type=signal_type,
            tags=stock_tags,
        )
        signal_priority = {SignalType.BUY_T: 0, SignalType.SELL_T: 1, SignalType.WATCH: 2}
        value_map = {
            "activity": activity_score,
            "change": quote.change_pct,
            "amount": quote.amount,
            "volume_ratio": quote.minute_amount_ratio,
            "order_flow": quote.order_flow.score,
            "signal": -signal_priority.get(signal_type, 2) * 100 + signal_score,
        }
        return (
            0 if watchlisted or position else 1,
            -float(value_map.get(sort, activity_score) or 0),
            -float(activity_score or 0),
            quote.code,
        )

    def _classify_stock(
        self,
        quote: Quote,
        sector: SectorSnapshot | None,
        theme_core_codes: set[str],
    ) -> tuple[str, list[str], bool, bool]:
        """Classify from current market structure; names are intentionally absent."""
        strong_sector = bool(sector and sector.heat_score >= self.engine.sector_watch_score)
        # 标签口径对齐市场通用认知：
        # 板块龙头 = 板块领涨股（涨幅第一，同花顺/东财板块页"领涨"口径；涨幅为负是领跌，不算龙头）；
        # 核心容量 = 板块中军（板块内流通市值第一，无市值数据回退成交额第一），
        # 或手工主题/自选显式标记的 core。不再按"领涨+成交额前5"批发核心标签。
        is_leader = bool(sector and sector.leader_code == quote.code and quote.change_pct > 0)
        explicit_core = bool(quote.core or quote.code in theme_core_codes)
        sector_core = bool(sector and sector.capacity_leader_code and sector.capacity_leader_code == quote.code)
        is_core = explicit_core or sector_core
        flow = quote.order_flow
        attack = quote.minute_amount_ratio >= self.engine.core_attack_volume_ratio and quote.change_pct > 0
        if flow.available and flow.direction in {"买盘增强", "放量承接"} and quote.change_pct >= 0:
            attack = attack or flow.score >= self.engine.order_flow_attack_score
        low_rebound = self._rebound_from_quote(quote)
        pullback = self._pullback_from_quote(quote)
        pressure = quote.change_pct < -1.0 or pullback >= self.engine.pressure_pullback_pct or (
            flow.available and flow.direction in {"卖盘增强", "放量抛压"}
        )
        tags: list[str] = []
        if is_leader:
            tags.append("板块龙头")
        if is_core:
            tags.append("核心容量")
        if quote.limit_up:
            tags.append("涨停")
        if quote.opened_limit:
            tags.append("回封/炸板")
        if attack:
            tags.append("大单进攻")
        if strong_sector and quote.change_pct > 0 and not is_leader and not pressure:
            tags.append("强势跟随")
        if quote.change_pct >= -0.5 and low_rebound >= self.engine.low_support_rebound_min and not pressure:
            tags.append("低位承接")
        if pressure:
            tags.append("掉队/抛压")
        if not tags:
            tags.append("普通成员")

        if is_leader:
            stock_type = "板块龙头"
        elif is_core:
            stock_type = "核心容量"
        elif quote.limit_up or quote.opened_limit:
            stock_type = "涨停/回封"
        elif attack:
            stock_type = "大单进攻"
        elif "强势跟随" in tags:
            stock_type = "强势跟随"
        elif "低位承接" in tags:
            stock_type = "低位承接"
        elif pressure:
            stock_type = "掉队/抛压"
        else:
            stock_type = "普通成员"
        return stock_type, list(dict.fromkeys(tags)), is_leader, is_core

    def _activity_score(
        self,
        quote: Quote,
        sector: SectorSnapshot | None,
        signal: TradeSignal,
        tags: list[str],
    ) -> float:
        return self._activity_score_for_values(
            quote,
            sector,
            signal_type=signal.signal,
            tags=tags,
        )

    def _activity_score_for_values(
        self,
        quote: Quote,
        sector: SectorSnapshot | None,
        *,
        signal_type: SignalType,
        tags: list[str],
    ) -> float:
        amount_score = min(36.0, log10(max(float(quote.amount or 0), 0) / 1_000_000 + 1) * 9.5)
        change_score = min(24.0, abs(float(quote.change_pct or 0)) * 2.4)
        volume_score = min(24.0, max(float(quote.minute_amount_ratio or 0), 0) * 10)
        sector_score = min(14.0, max(float(sector.heat_score if sector else 0), 0) * 0.14)
        flow_score = min(12.0, abs(float(quote.order_flow.score or 0)) * 0.12)
        tag_bonus = 5.0 if any(tag in tags for tag in {"板块龙头", "涨停", "大单进攻"}) else 0
        signal_bonus = 8.0 if signal_type in {SignalType.BUY_T, SignalType.SELL_T} else 0
        return amount_score + change_score + volume_score + sector_score + flow_score + tag_bonus + signal_bonus

    def _board_sort_key(self, item: StockBoardItem, sort: str) -> tuple[Any, ...]:
        signal_priority = {SignalType.BUY_T: 0, SignalType.SELL_T: 1, SignalType.WATCH: 2}
        value_map = {
            "activity": item.activity_score,
            "change": item.change_pct,
            "amount": item.amount,
            "volume_ratio": item.minute_amount_ratio,
            "order_flow": item.order_flow.score,
            "signal": -signal_priority.get(item.signal, 2) * 100 + item.signal_score,
        }
        return (
            0 if item.watchlisted or item.position else 1,
            -float(value_map.get(sort, item.activity_score) or 0),
            -float(item.activity_score or 0),
            item.code,
        )

    def _sector_rank(self, sectors: list[SectorSnapshot], sector: SectorSnapshot | None) -> int:
        if not sector:
            return 0
        ranks = self._sector_rank_by_name(sectors)
        return ranks.get(sector.name, 0)

    def _sector_rank_by_name(self, sectors: list[SectorSnapshot]) -> dict[str, int]:
        cache_key = (
            len(sectors),
            "|".join(
                [
                    str(len(sectors)),
                    "/".join(sector.name for sector in sectors[:16]),
                ]
            ),
        )
        ranks = self._sector_rank_cache.get(cache_key)
        if ranks is not None:
            return ranks
        ranks = {item.name: rank for rank, item in enumerate(sectors, start=1)}
        self._sector_rank_cache[cache_key] = ranks
        if len(self._sector_rank_cache) > 16:
            self._sector_rank_cache.pop(next(iter(self._sector_rank_cache)), None)
        return ranks

    def _quote_sector_map(
        self,
        sectors: list[SectorSnapshot],
        members_by_sector: dict[str, list[str]],
    ) -> dict[str, SectorSnapshot]:
        if not sectors or not members_by_sector:
            return {}
        cache_key = (
            len(sectors),
            "|".join(
                [
                    str(len(sectors)),
                    str(len(members_by_sector)),
                    "/".join(sector.name for sector in sectors[:32]),
                    "/".join(
                        f"{name}:{len(codes)}:{','.join(list(codes or [])[:3])}"
                        for name, codes in list(members_by_sector.items())[:20]
                    ),
                ]
            ),
        )
        cached = self._quote_sector_map_cache.get(cache_key)
        if cached is not None:
            return cached
        sector_by_name = {sector.name: sector for sector in sectors}
        result: dict[str, SectorSnapshot] = {}
        for name, codes in members_by_sector.items():
            sector = sector_by_name.get(name)
            if sector is None:
                continue
            for code in codes:
                normalized = str(code).zfill(6)
                current = result.get(normalized)
                if current is None or sector.heat_score > current.heat_score:
                    result[normalized] = sector
        self._quote_sector_map_cache[cache_key] = result
        if len(self._quote_sector_map_cache) > 16:
            self._quote_sector_map_cache.pop(next(iter(self._quote_sector_map_cache)), None)
        return result

    def _rebound_from_quote(self, quote: Quote) -> float:
        if quote.day_low <= 0:
            return 0
        return (quote.price - quote.day_low) / quote.day_low * 100

    def _pullback_from_quote(self, quote: Quote) -> float:
        if quote.day_high <= 0:
            return 0
        return (quote.day_high - quote.price) / quote.day_high * 100

    @staticmethod
    def _zuot_levels_for_quote(quote: Quote) -> tuple[float, float]:
        """做T当日常量（阻力/支撑）：与详情做T公式同一套 ZuoTDayContext 口径。"""
        from app.formula_engine import ZuoTDayContext

        ctx = ZuoTDayContext(
            prev_close=float(quote.prev_close or 0),
            day_high=float(quote.day_high or quote.high or 0),
            day_low=float(quote.day_low or quote.low or 0),
        )
        if not ctx.levels_available:
            return 0.0, 0.0
        return round(ctx.resistance, 3), round(ctx.support, 3)

    def _payload_for_context(self, context: DashboardContext, sector: str | None = None) -> DashboardPayload:
        selected_sector = self._normalize_sector(sector)
        sector_focus = next((item for item in context.sectors if item.name == selected_sector), None) if selected_sector else None
        sector_codes = self._sector_codes(context.snapshot.quotes, selected_sector) if selected_sector else set()
        signals = self._filter_signals(context.signals_all, sector_codes)
        core_watch = self._filter_signals(context.core_watch, sector_codes)
        if selected_sector:
            signals = [signal.model_copy(update={"sector": selected_sector}) for signal in signals]
            core_watch = [signal.model_copy(update={"sector": selected_sector}) for signal in core_watch]
        events = self._filter_events(context.events, selected_sector)
        rendered_signals = self._limit_signals(signals)
        watchlist_codes = [item.code for item in context.watchlist]

        source_status = dict(context.source_status)
        source_status["selected_sector"] = selected_sector
        source_status["watchlist_codes"] = watchlist_codes
        source_status["signal_count_rendered"] = len(rendered_signals)

        return DashboardPayload(
            market=context.market,
            sectors=context.sectors,
            sector_flow=context.sector_flow,
            signals=rendered_signals,
            core_watch=core_watch[:12],
            events=events,
            watchlist=context.watchlist,
            data_mode=context.snapshot.data_mode,
            source_status=source_status,
            selected_sector=selected_sector,
            sector_focus=sector_focus,
            watchlist_codes=watchlist_codes,
        )

    def _filter_signals(self, signals: list[TradeSignal], sector_codes: set[str]) -> list[TradeSignal]:
        if not sector_codes:
            return list(signals)
        return [signal for signal in signals if signal.code in sector_codes]

    def _filter_events(self, events: list[EventItem], sector: str | None) -> list[EventItem]:
        if not sector:
            return list(events)
        filtered: list[EventItem] = []
        for event in events:
            if event.level == "market":
                filtered.append(event)
                continue
            haystack = f"{event.title} {event.detail}"
            if sector in haystack:
                filtered.append(event)
        return filtered or list(events)

    def _record_last_action_event(
        self,
        code: str,
        signal: SignalType | str,
        price: Any,
        time_label: Any,
    ) -> None:
        """记录当天最近一次做T买/卖点；只按时间向前推进，不回滚到更早事件。"""
        normalized = str(code or "").zfill(6)
        signal_text = signal.value if isinstance(signal, SignalType) else str(signal or "")
        if len(normalized) != 6 or signal_text not in {SignalType.BUY_T.value, SignalType.SELL_T.value}:
            return
        try:
            trigger_price = float(price or 0)
        except (TypeError, ValueError):
            return
        if not isfinite(trigger_price) or trigger_price <= 0:
            return
        label = self._mini_time_label(time_label) or china_now().strftime("%H:%M")
        lock = getattr(self, "_last_action_lock", None)
        store = getattr(self, "_last_action_by_code", None)
        if lock is None or store is None:
            return
        with lock:
            existing = store.get(normalized)
            existing_time = str(existing.get("last_action_time") or "") if existing else ""
            if existing and existing_time and label and existing_time > label:
                return
            store[normalized] = {
                "last_action": signal_text,
                "last_action_price": round(trigger_price, 3),
                "last_action_time": label,
            }

    def _record_last_action_signals(self, signals: list[TradeSignal]) -> None:
        """从每轮首页信号里沉淀最近一次买卖点，信号回到「观察」后仍可排序/展示。"""
        for signal in signals:
            if signal.signal not in {SignalType.BUY_T, SignalType.SELL_T}:
                continue
            self._record_last_action_event(
                signal.code,
                signal.signal,
                signal.trigger_price,
                signal.updated_at,
            )

    def _last_action_for_code(self, code: str) -> dict[str, Any]:
        normalized = str(code or "").zfill(6)
        lock = getattr(self, "_last_action_lock", None)
        store = getattr(self, "_last_action_by_code", None)
        if lock is None or store is None:
            return {}
        with lock:
            return dict(store.get(normalized) or {})

    def _dispatch_signal_pushes(self, signals: list[TradeSignal]) -> None:
        """把本轮实盘信号交给飞书推送池；推送异常绝不能影响行情刷新。"""
        pool = getattr(self, "push_pool", None)
        if pool is None:
            return
        try:
            pool.process_signals(signals)
        except Exception:
            logger.exception("signal push pool failed")

    def _decorate_signals(
        self,
        signals: list[TradeSignal],
        watchlist: list[WatchlistItem],
        positions: dict[str, PositionRecord] | None = None,
    ) -> list[TradeSignal]:
        self._record_last_action_signals(signals)
        watchlist_map = {item.code: item for item in watchlist}
        position_map = positions or {}
        decorated: list[TradeSignal] = []
        for signal in signals:
            item = watchlist_map.get(signal.code)
            tags: list[str] = []
            pinned = False
            if signal.code in position_map:
                pinned = True
                tags.append("持仓")
            if item:
                pinned = True
                if item.core:
                    tags.append("核心")
                if not tags:
                    tags.append("自选")
            decorated.append(
                signal.model_copy(
                    update={"pinned": pinned, "watchlist_tags": list(dict.fromkeys(tags))}
                )
            )
        return decorated

    def _limit_signals(self, signals: list[TradeSignal]) -> list[TradeSignal]:
        buckets = {
            SignalType.BUY_T: [],
            SignalType.WATCH: [],
            SignalType.SELL_T: [],
        }
        for signal in signals:
            buckets[signal.signal].append(signal)
        ordered = []
        for key in [SignalType.BUY_T, SignalType.WATCH, SignalType.SELL_T]:
            bucket = sorted(buckets[key], key=lambda signal: (not signal.pinned, -signal.score))
            limit = self.settings.max_signals_per_group
            ordered.extend(bucket if limit <= 0 else bucket[: max(1, limit)])
        return ordered

    def _core_watch(
        self,
        signals: list[TradeSignal],
        themes: list[dict],
        watchlist: list[WatchlistItem],
    ) -> list[TradeSignal]:
        core_codes = {code for theme in themes for code in theme.get("core_codes", [])}
        if self.settings.include_watchlist_in_scan:
            core_codes |= {item.code for item in watchlist if item.core}
        core = [signal for signal in signals if signal.code in core_codes]
        if not core:
            core = [signal for signal in signals if signal.signal != SignalType.WATCH]
        return sorted(core, key=lambda signal: (not signal.pinned, -signal.score))[:12]

    def _sector_flow_membership(
        self,
        state_key: str,
        sectors: list[SectorSnapshot],
    ) -> list[SectorSnapshot]:
        """板块资金动能成员口径：资金净流入 top-5 保底 ∪ 热度 top-N 补位，带滞回防抖。

        盯盘口径：这是资金动能面板，真金白银（flow_delta > 0 的今日累计净额
        top-5）必须保底入围——旧顺序让热度 top-N 先占席、资金榜补到 cap 即止，
        资金第 4/5 名会被热度榜挤掉（如 2026-08-17 资金第 4 的半导体设备缺席）。
        资金保底后热度按序补到 cap，被截断的是热度榜尾部而非资金榜头部。
        上轮已展示的板块，只要仍在热度 top-(N+3) 或资金 top-8 内就保留，防止
        榜单边界板块随每次快照闪进闪出。总量封顶 N+2。state_key 为空时不读写
        上轮名单（一次性构建）。
        """
        limit = max(1, int(getattr(self.engine, "sector_flow_top_n", 10) or 10))
        money_pick = 5
        cap = limit + 2
        ranked = [sector for sector in sectors if getattr(sector, "name", "")]
        money_ranked = sorted(
            ranked,
            key=lambda sector: float(getattr(sector, "flow_delta", 0.0) or 0.0),
            reverse=True,
        )
        members: dict[str, SectorSnapshot] = {}
        for sector in money_ranked[:money_pick]:
            if float(getattr(sector, "flow_delta", 0.0) or 0.0) > 0:
                members.setdefault(sector.name, sector)
        for sector in ranked[:limit]:
            if len(members) >= cap:
                break
            members.setdefault(sector.name, sector)
        if not state_key:
            return list(members.values())[:cap]
        with self._sector_flow_lock:
            previous_names = list(self._sector_flow_names_by_key.get(state_key) or [])
        if previous_names:
            by_name = {sector.name: sector for sector in ranked}
            heat_extended = {sector.name for sector in ranked[: limit + 3]}
            money_extended = {
                sector.name
                for sector in money_ranked[: money_pick + 3]
                if float(getattr(sector, "flow_delta", 0.0) or 0.0) > 0
            }
            for name in previous_names:
                if len(members) >= cap:
                    break
                if name in members or name not in by_name:
                    continue
                if name in heat_extended or name in money_extended:
                    members[name] = by_name[name]
        selected = list(members.values())[:cap]
        with self._sector_flow_lock:
            self._sector_flow_names_by_key[state_key] = [sector.name for sector in selected]
        return selected

    def _sector_flow_member_codes(
        self,
        sector: SectorSnapshot,
        quotes: list[Quote],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None,
    ) -> list[str]:
        """板块资金动能的全成员代码：官方板块成员 > 快照主题归属 > 代表股回退。

        资金净流入必须是全成员求和才有盯盘意义（与外部板块资金流排名可比），
        只取 3 只代表股的旧口径会把「广谱流入」算成「三只票平均动能」。
        """
        if member_code_loader is not None:
            try:
                codes = [str(code).zfill(6) for code in (member_code_loader(sector) or [])]
            except Exception:
                codes = []
            if codes:
                return codes
        by_code: dict[str, None] = {}
        for quote in quotes:
            if sector.name in (quote.themes or []):
                by_code.setdefault(quote.code)
        if by_code:
            return list(by_code)
        return self.engine.sector_flow_codes(sector, quotes, member_codes=None)

    def _sector_flow_for_context(
        self,
        snapshot: MarketSnapshot,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None = None,
        cache_namespace: str = "",
        prefer_async: bool = False,
        allow_deferred: bool = False,
    ) -> list[SectorFlowSeries]:
        if not sectors:
            return []

        trade_date = str(snapshot.source_status.get("trade_date") or china_now().strftime("%Y%m%d"))
        live_mode = snapshot.data_mode == "live" or snapshot.source_status.get("active_source") == "easy_tdx"
        frozen = bool(snapshot.source_status.get("frozen", snapshot.data_mode == "closed_static"))
        cache_key = "|".join(
            [
                trade_date,
                snapshot.data_mode,
                str(snapshot.source_status.get("active_source") or ""),
                cache_namespace,
            ]
        )
        ttl = self.settings.terminal_context_frozen_cache_seconds if frozen else self.settings.sector_flow_refresh_seconds
        now = time.time()
        with self._sector_flow_lock:
            cached = self._sector_flow_cache_by_key.get(cache_key)
        if cached and cached[1] and now - cached[0] < max(1, ttl):
            if prefer_async and live_mode and not frozen:
                needs_opening_backfill = self._sector_flow_needs_opening_backfill(cached[1], snapshot)
                needs_live_tail = self._sector_flow_needs_live_tail(cached[1], snapshot)
                if needs_opening_backfill:
                    self._ensure_sector_flow_refresh(
                        cache_key,
                        snapshot,
                        sectors,
                        member_code_loader=member_code_loader,
                    )
                if needs_live_tail:
                    self._seed_sector_flow_proxy_state(cache_key, trade_date, cached[1])
                else:
                    return cached[1]
            else:
                return cached[1]

        # 冻结/非实时（收盘后、重启冷启动）：优先用本地板块轨迹重建资金流，
        # 而不是逐票请求 TDX 历史分钟线——后者盘后大量失败会导致只剩个别板块。
        if frozen or not live_mode:
            cloud_flow = self._load_sector_flow_cloud(cache_key, trade_date)
            if cloud_flow:
                cloud_flow = self._anchor_flow_list_to_active_net(
                    cloud_flow, sectors, getattr(snapshot, "quotes", None), member_code_loader
                )
                with self._sector_flow_lock:
                    self._sector_flow_cache_by_key[cache_key] = (now, cloud_flow)
                return cloud_flow
            if allow_deferred:
                # 首屏不等 80GB 轨迹库的冷读：先返回过期缓存/空，
                # 后台重建后清终端缓存，下一轮 WS 增量带出曲线。
                # 快照为空（冷启动、上下文尚未就绪）时不要调度：没有 quotes
                # 的重建无法做 L1 真值锚定，未锚定结果会占住冻结缓存 300s，
                # 把「成交额毛口径」当成定数展示（2026-08-17 本地/生产分歧
                # 的诱因之一）。
                if getattr(snapshot, "quotes", None):
                    self._schedule_sector_flow_trajectory_refresh(
                        cache_key,
                        trade_date,
                        sectors,
                        member_code_loader,
                        snapshot.quotes,
                    )
                if cached and cached[1]:
                    return cached[1]
                if getattr(self, "state_store", None) is not None:
                    fallback_flow = self._build_and_cache_sector_flow(
                        cache_key,
                        snapshot,
                        sectors,
                        member_code_loader=member_code_loader,
                    )
                    if fallback_flow:
                        return fallback_flow
                return []
            trajectory_flow = self._sector_flow_from_trajectory(
                trade_date,
                sectors,
                member_code_loader=member_code_loader,
                quotes=getattr(snapshot, "quotes", None),
                state_key=cache_key,
            )
            if trajectory_flow:
                with self._sector_flow_lock:
                    self._sector_flow_cache_by_key[cache_key] = (now, trajectory_flow)
                self._persist_sector_flow_cloud(cache_key, trade_date, trajectory_flow, force=True)
                return trajectory_flow

        if prefer_async and live_mode and not frozen:
            with self._sector_flow_lock:
                proxy_state = self._sector_flow_proxy_by_key.get(cache_key)
                proxy_has_points = bool(
                    proxy_state
                    and proxy_state.get("trade_date") == trade_date
                    and any(points for points in dict(proxy_state.get("points") or {}).values())
                )
            if not cached and not proxy_has_points:
                cloud_flow = self._load_sector_flow_cloud(cache_key, trade_date)
                if cloud_flow:
                    cloud_flow = self._anchor_flow_list_to_active_net(
                        cloud_flow, sectors, getattr(snapshot, "quotes", None), member_code_loader
                    )
                    self._seed_sector_flow_proxy_state(cache_key, trade_date, cloud_flow)
                    with self._sector_flow_lock:
                        self._sector_flow_cache_by_key[cache_key] = (now, cloud_flow)
                    self._ensure_sector_flow_refresh(
                        cache_key,
                        snapshot,
                        sectors,
                        member_code_loader=member_code_loader,
                    )
                    if not self._sector_flow_needs_live_tail(cloud_flow, snapshot):
                        return cloud_flow
            # 盘中每次刷新都返回快照代理曲线：全成员成交额增量×方向求和，
            # 零网络开销，随主循环逐点累积。
            proxy = self._sector_flow_proxy_tick(
                cache_key,
                snapshot,
                sectors,
                member_code_loader=member_code_loader,
            )
            # 仅冷启动（刚启动/换交易日，曲线还很薄）时后台用本地轨迹按
            # 同一口径回灌一次当日分钟历史；之后曲线完全由快照 tick 驱动。
            proxy_points = min((len(series.points) for series in proxy), default=0)
            if (
                not proxy
                or proxy_points < 3
                or self._sector_flow_needs_opening_backfill(proxy, snapshot)
            ):
                self._ensure_sector_flow_refresh(
                    cache_key,
                    snapshot,
                    sectors,
                    member_code_loader=member_code_loader,
            )
            if proxy:
                self._persist_sector_flow_cloud(cache_key, trade_date, proxy)
                return proxy
            return cached[1] if cached else []

        return self._build_and_cache_sector_flow(
            cache_key,
            snapshot,
            sectors,
            member_code_loader=member_code_loader,
        )

    def _snapshot_time_label(self, snapshot: MarketSnapshot) -> str:
        clock_label = str(snapshot.source_status.get("clock_label") or "")
        normalized = self._minute_label_from_clock(clock_label)
        if normalized:
            return normalized
        return (
            clock_label[:5]
            if len(clock_label) >= 5 and clock_label[2] == ":"
            else china_now().strftime("%H:%M")
        )

    def _update_quote_tick_cache(self, snapshot: MarketSnapshot, trade_date: str) -> str:
        """把全市场实时快照并入个股 tick 缓存：同一分钟原地更新，跨分钟追加。

        每个 code 记录 rows（分钟级价额量序列）以及 prev/last 两次刷新的 (price, amount)，
        消费方（板块资金动能等）据此计算增量，无需逐股额外请求。换交易日自动清空。
        """
        time_label = self._snapshot_time_label(snapshot)
        with self._sector_flow_lock:
            if self._quote_tick_cache_date != trade_date:
                self._quote_tick_cache.clear()
                self._quote_tick_cache_date = trade_date
            for quote in snapshot.quotes:
                if quote.price <= 0:
                    continue
                entry = self._quote_tick_cache.get(quote.code)
                if entry is None:
                    entry = {"rows": [], "prev": None, "last": None}
                    self._quote_tick_cache[quote.code] = entry
                rows = entry["rows"]
                row = {"time": time_label, "price": quote.price, "amount": quote.amount, "volume": quote.volume}
                if rows and rows[-1]["time"] == time_label:
                    rows[-1] = row
                else:
                    rows.append(row)
                    if len(rows) > 240:
                        del rows[: len(rows) - 240]
                entry["prev"] = entry["last"]
                entry["last"] = (quote.price, quote.amount, time_label)
        return time_label

    def _sector_flow_needs_opening_backfill(
        self,
        series: list[SectorFlowSeries],
        snapshot: MarketSnapshot,
    ) -> bool:
        """Return whether an intraday flow curve starts too late for today."""
        if not series:
            return True
        clock_label = str(snapshot.source_status.get("clock_label") or "")
        current_label = self._minute_label_from_clock(clock_label or china_now().strftime("%H:%M:%S"))
        if not current_label:
            return False
        session_order = {time_label: index for index, time_label in enumerate(self.engine._session_times(242))}
        current_index = session_order.get(current_label)
        if current_index is None or current_index < 10:
            return False

        first_indices: list[int] = []
        for item in series:
            for point in item.points:
                point_index = session_order.get(str(point.time or "")[:5])
                if point_index is not None:
                    first_indices.append(point_index)
                    break
        if not first_indices:
            return True
        return min(first_indices) > 3

    def _sector_flow_needs_live_tail(
        self,
        series: list[SectorFlowSeries],
        snapshot: MarketSnapshot,
    ) -> bool:
        """Return whether a live flow curve is behind the current trade minute."""
        if not series:
            return True
        clock_label = str(snapshot.source_status.get("clock_label") or "")
        current_label = self._minute_label_from_clock(clock_label or china_now().strftime("%H:%M:%S"))
        if not current_label:
            return False
        session_order = {time_label: index for index, time_label in enumerate(self.engine._session_times(242))}
        current_index = session_order.get(current_label)
        if current_index is None:
            return False

        last_indices: list[int] = []
        for item in series:
            for point in reversed(item.points):
                point_index = session_order.get(str(point.time or "")[:5])
                if point_index is not None:
                    last_indices.append(point_index)
                    break
        if not last_indices:
            return True
        return max(last_indices) < current_index

    def _sector_flow_proxy_tick(
        self,
        cache_key: str,
        snapshot: MarketSnapshot,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None = None,
    ) -> list[SectorFlowSeries]:
        """从全市场 tick 缓存聚合「板块每分钟净流入」曲线，零网络请求。

        口径：全成员 Σ(本 tick L1 主动买卖量差 × 价格) / 1e8——与外部板块
        资金流排名同口径（计数器单调累计、跨采集中断自对齐）；order_flow 缺失的个股
        回退「成交额增量 × tick 价格方向」并按空窗分钟摊薄。不做代表股抽样、不做平均、不加偏向系数。
        每次刷新先把快照并入 tick 缓存，再把各成员股本 tick 的净额累进当前
        分钟桶；跨分钟自动开新桶，每个点的值就是该分钟的净流入（亿），不做
        跨分钟累计。冷启动由本地轨迹回灌当日分钟历史。
        """
        trade_date = str(snapshot.source_status.get("trade_date") or china_now().strftime("%Y%m%d"))
        # 成员口径与回灌/轨迹分支一致：热度 top-N ∪ 资金净流入 top-5 + 滞回。
        # 必须在锁外调用（内部会短暂拿锁读写上轮名单）。
        flow_sectors = self._sector_flow_membership(cache_key, sectors)
        time_label = self._update_quote_tick_cache(snapshot, trade_date)
        by_code = {quote.code: quote for quote in snapshot.quotes}
        member_codes_by_sector = {
            sector.name: set(self._sector_flow_member_codes(sector, snapshot.quotes, member_code_loader))
            for sector in flow_sectors
            if sector.name
        }

        with self._sector_flow_lock:
            state = self._sector_flow_proxy_by_key.get(cache_key)
            if state is not None and state.get("trade_date") != trade_date:
                state = None
            if state is None:
                # points: sector_name -> {minute_label: 该分钟净流入（亿）}
                state = {"trade_date": trade_date, "points": {}}
                self._sector_flow_proxy_by_key[cache_key] = state
            if len(self._sector_flow_proxy_by_key) > 12:
                oldest = min(
                    (key for key in self._sector_flow_proxy_by_key if key != cache_key),
                    key=lambda key: len(self._sector_flow_proxy_by_key[key]["points"]),
                    default=None,
                )
                if oldest is not None:
                    self._sector_flow_proxy_by_key.pop(oldest, None)

            series_list: list[SectorFlowSeries] = []
            for sector in flow_sectors:
                if not sector.name:
                    continue
                member_codes = member_codes_by_sector.get(sector.name) or set()
                step_total = 0.0
                active_codes = 0
                for code in member_codes:
                    quote = by_code.get(code)
                    if quote is None or quote.price <= 0:
                        continue
                    tick_entry = self._quote_tick_cache.get(code) or {}
                    # 主口径：L1 主动买卖量差（外/内盘累计计数器的相邻增量×价格）。
                    # 计数器随交易日单调累计，跨采集中断自动对齐该缺口内的真实净
                    # 主动量，与外部板块资金流排名同口径。成交额增量×方向只在
                    # order_flow 缺失时回退——趋势日它会把每个采样窗口的毛成交额
                    # 按单一方向定号，累计虚高真值 2 倍以上。
                    active_net_amount = self._active_quote_net_amount(quote, tick_entry, time_label)
                    if active_net_amount is not None:
                        if active_net_amount:
                            step_total += active_net_amount
                            active_codes += 1
                        continue
                    prev_tick = tick_entry.get("prev") or (None, None, "")
                    prev_price, prev_amount = prev_tick[0], prev_tick[1]
                    prev_label = prev_tick[2] if len(prev_tick) > 2 else ""
                    base_prev = prev_price or (quote.open if quote.open > 0 else 0) or quote.prev_close or quote.price
                    if prev_amount is not None and quote.amount >= prev_amount > 0:
                        delta_amount = quote.amount - prev_amount
                    else:
                        delta_amount = max(float(quote.minute_amount or 0), 0)
                    if delta_amount <= 0:
                        continue
                    # 走平沿用上一方向：平推的连续成交占全天大头，丢掉会把曲线
                    #  biased 向反转 tick、放大锯齿（与轨迹回灌同一规则）
                    if quote.price > base_prev:
                        direction = 1
                    elif quote.price < base_prev:
                        direction = -1
                    else:
                        direction = int(tick_entry.get("dir") or 0)
                    if direction == 0:
                        continue
                    tick_entry["dir"] = direction
                    step_amount = delta_amount * direction
                    # 相邻观测跨度过大（采集中断/故障恢复）时，成交额增量是整个
                    # 空窗期的总量，按空窗分钟数（封顶 5）摊薄，与轨迹回灌同一规则，
                    # 避免空窗毛成交额被单一方向一次性定号。
                    gap_minutes = self._minutes_between(prev_label, time_label) if prev_label else 0
                    if gap_minutes > 2:
                        step_amount /= min(gap_minutes, 5)
                    step_total += step_amount
                    active_codes += 1

                points_map = state["points"].setdefault(sector.name, {})
                if active_codes:
                    step = step_total / 100_000_000  # 亿元：全成员求和，不平均、不缩放
                    points_map[time_label] = round(points_map.get(time_label, 0.0) + step, 4)
                ordered = sorted(points_map.items())
                if len(ordered) > 240:
                    ordered = ordered[-240:]
                    state["points"][sector.name] = dict(ordered)
                if not ordered:
                    continue
                series_list.append(
                    SectorFlowSeries(
                        name=sector.name,
                        heat_score=sector.heat_score,
                        # final_value = 今日累计净流入（亿，全成员总量口径），
                        # 供三分支一致排序；points 仍是每分钟净流入，前端自行积分。
                        final_value=round(sum(value for _, value in ordered), 2),
                        change_pct=sector.avg_change_pct,
                        leader_code=sector.leader_code,
                        leader_name=sector.leader_name,
                        core_codes=list(sector.core_codes),
                        reasons=list(sector.reasons),
                        points=[SectorFlowPoint(time=label, value=round(value, 2)) for label, value in ordered],
                        flow_basis="每分钟净流入(全成员L1主动量差，缺省用成交额增量×方向)",
                        sample_codes=sorted(member_codes)[:3],
                    )
                )
            # 统一展示排序：今日累计净流入降序，同值按热度——与回灌/轨迹分支一致
            series_list.sort(key=lambda series: (series.final_value, series.heat_score), reverse=True)
            return series_list

    @staticmethod
    def _active_quote_net_amount(quote: Quote, tick_entry: dict[str, Any], time_label: str) -> float | None:
        """L1 主动买卖量差的本 tick 增量（元）。

        返回 None 表示 order_flow 不可用，调用方回退「成交额增量×方向」口径；
        返回 0 表示口径可用但本 tick 无净增量（含计数器回退跳变：跳过而不是
        把全天累计摊进当前分钟——趋势日那种摊法会单次虚增上亿）。
        """
        flow = getattr(quote, "order_flow", None)
        if flow is None or not getattr(flow, "available", False) or quote.price <= 0:
            return None
        buy_volume = float(getattr(flow, "active_buy_volume", 0.0) or 0.0)
        sell_volume = float(getattr(flow, "active_sell_volume", 0.0) or 0.0)
        if buy_volume + sell_volume <= 0:
            return None
        previous = tick_entry.get("active_last")
        tick_entry["active_last"] = (buy_volume, sell_volume)
        if previous:
            prev_buy, prev_sell = previous
            if buy_volume < prev_buy or sell_volume < prev_sell:
                # 计数器回退（源切换/字段重基）：本 tick 跳过，下一轮恢复增量语义。
                return 0.0
            net_volume = (buy_volume - prev_buy) - (sell_volume - prev_sell)
            return net_volume * quote.price * 100
        # 当日首次观测（冷启动/中途并入成员）：把已错过时段的累计净主动量
        # 按已交易分钟数摊成均速归入当前分钟，量级有界。
        elapsed = DashboardService._elapsed_session_minutes_for_label(time_label)
        net_volume = buy_volume - sell_volume
        return net_volume * quote.price * 100 / max(1, elapsed)

    @staticmethod
    def _elapsed_session_minutes_for_label(time_label: str) -> int:
        try:
            hour, minute = [int(part) for part in str(time_label or "").split(":")[:2]]
        except (TypeError, ValueError):
            return 1
        current = hour * 60 + minute
        if current < 9 * 60 + 30:
            return 1
        if current <= 11 * 60 + 30:
            return max(1, current - (9 * 60 + 30) + 1)
        if current < 13 * 60:
            return 120
        if current <= 15 * 60:
            return 120 + max(1, current - (13 * 60) + 1)
        return 240

    @staticmethod
    def _active_net_truth_total(member_codes: list[str], quotes_by_code: dict[str, Quote]) -> float | None:
        """全成员当日 L1 主动买卖净额真值（亿元）。

        取 order_flow 外/内盘计数器差：Σ((active_buy - active_sell) × price × 100)。
        这是与盘中实时口径一致的真值；分钟线/轨迹的「成交额增量×方向」在趋势日
        会把被动成交也按收盘价方向定号，总量系统性虚高，需要向它收敛。
        覆盖率不足 80% 时返回 None，调用方保留原曲线不定标；
        覆盖不全时按成员数等比外推缺失成员。
        """
        members = [str(code) for code in member_codes if code]
        if not members:
            return None
        covered_sum = 0.0
        covered = 0
        for code in members:
            quote = quotes_by_code.get(code)
            if quote is None or quote.price <= 0:
                continue
            flow = getattr(quote, "order_flow", None)
            if flow is None or not getattr(flow, "available", False):
                continue
            buy_volume = float(getattr(flow, "active_buy_volume", 0.0) or 0.0)
            sell_volume = float(getattr(flow, "active_sell_volume", 0.0) or 0.0)
            if buy_volume + sell_volume <= 0:
                continue
            covered += 1
            covered_sum += (buy_volume - sell_volume) * quote.price * 100
        if covered < max(1, ceil(len(members) * 0.8)):
            return None
        return covered_sum / 100_000_000 * len(members) / covered

    @staticmethod
    def _anchor_series_to_truth(series: SectorFlowSeries, truth_total: float | None) -> SectorFlowSeries:
        """总量锚定：分钟线形态不动，整体缩放到 L1 主动净额真值。

        守卫只挡「方向矛盾/垃圾形态」：比值 <= 0（符号相反，含成交额口径把
        净流入板块算成净流出的情形）或超出 [0.05, 20] 时保留原曲线。
        不能再设 3 倍上限——成交额增量×方向形态在震荡日会正负对冲、系统性
        低估真值（2026-08-17 种子 6.3x、消费电子组件 3.1x 都是真实情形），
        真值本身来自收盘外/内盘计数器、覆盖率 >=80% 才到这里，量级差异不是
        拒绝理由；被守卫静默拦下会让同一面板混排两种口径，本地/生产各漏
        锚不同的板块，数值完全对不上。
        """
        if truth_total is None or not series.points:
            return series
        shape_total = sum(float(point.value) for point in series.points)
        if shape_total == 0:
            return series
        ratio = truth_total / shape_total
        if not 0.05 <= ratio <= 20.0:
            return series
        return series.model_copy(
            update={
                "points": [
                    SectorFlowPoint(time=point.time, value=round(float(point.value) * ratio, 4))
                    for point in series.points
                ],
                "final_value": round(truth_total, 2),
                "flow_basis": "每分钟净流入(分钟形态×L1主动量定标)",
            }
        )

    def _anchor_flow_list_to_active_net(
        self,
        flow_list: list[SectorFlowSeries],
        sectors: list[SectorSnapshot],
        quotes: list[Quote] | None,
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None = None,
    ) -> list[SectorFlowSeries]:
        """云端记录加载后的总量锚定：分钟形态不动，总量收敛到 L1 主动净额真值。

        云端记录可能是上一版未锚定的成交额口径（趋势日虚高），加载时用当前
        快照的外/内盘计数器定标到真值；已锚定（定标 basis）或真值不可得的
        记录原样保留。
        """
        quotes_by_code = {quote.code: quote for quote in quotes or [] if getattr(quote, "code", "")}
        if not flow_list or not quotes_by_code:
            return flow_list
        sectors_by_name = {sector.name: sector for sector in sectors or []}
        anchored: list[SectorFlowSeries] = []
        for series in flow_list:
            if "L1主动量定标" in str(series.flow_basis or ""):
                anchored.append(series)
                continue
            sector = sectors_by_name.get(series.name)
            member_codes: list[str] = []
            if sector is not None and member_code_loader is not None:
                try:
                    member_codes = list(member_code_loader(sector) or [])
                except Exception:
                    member_codes = []
            if not member_codes and sector is not None:
                member_codes = self.engine.sector_flow_codes(sector, list(quotes or []), member_codes=None)
            anchored.append(
                self._anchor_series_to_truth(series, self._active_net_truth_total(member_codes, quotes_by_code))
            )
        return anchored

    def _load_sector_flow_cloud(self, cache_key: str, trade_date: str) -> list[SectorFlowSeries]:
        state_store = getattr(self, "state_store", None)
        if state_store is None:
            return []
        try:
            payload = state_store.get_json("sector_flow", cache_key)
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []
        if str(payload.get("trade_date") or "") != str(trade_date or ""):
            return []
        rows = payload.get("series")
        if not isinstance(rows, list):
            return []
        series: list[SectorFlowSeries] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                item = SectorFlowSeries.model_validate(row)
            except Exception:
                continue
            if "缺省用L1主动量" in str(item.flow_basis or ""):
                # 旧口径（成交额增量×单方向）持久化的历史数据在趋势日虚高 2 倍
                # 以上，拒绝加载，让调用方走分钟线/轨迹重建出新口径曲线。
                continue
            if item.points:
                series.append(item)
        if str(trade_date or "") < china_now().strftime("%Y%m%d"):
            # 历史完整交易日的曲线必须覆盖到尾盘；中途持久化的残缺回灌
            # （如 188/240 点）拒绝加载，让调用方重建完整分钟线。
            series = [
                item
                for item in series
                if str(item.points[-1].time or "") >= "14:50"
            ]
        if not any(len(item.points) >= 2 for item in series):
            return []
        return series

    def _persist_sector_flow_cloud(
        self,
        cache_key: str,
        trade_date: str,
        series: list[SectorFlowSeries],
        *,
        force: bool = False,
    ) -> None:
        state_store = getattr(self, "state_store", None)
        if state_store is None or not series:
            return
        rows = [item.model_dump(mode="json") for item in series if item.points]
        if not rows:
            return
        now = time.time()
        if not force:
            min_interval = max(30.0, float(getattr(self.settings, "sector_flow_minute_refresh_seconds", 60) or 60))
            write_times = getattr(self, "_sector_flow_cloud_write_at_by_key", {})
            last = write_times.get(cache_key, 0.0)
            if now - last < min_interval:
                return
        if not hasattr(self, "_sector_flow_cloud_write_at_by_key"):
            self._sector_flow_cloud_write_at_by_key = {}
        self._sector_flow_cloud_write_at_by_key[cache_key] = now
        try:
            state_store.set_json(
                "sector_flow",
                cache_key,
                {
                    "trade_date": str(trade_date),
                    "cache_key": str(cache_key),
                    "updated_at": int(now),
                    "series": rows,
                },
            )
        except Exception:
            return

    def _seed_sector_flow_proxy_state(
        self,
        cache_key: str,
        trade_date: str,
        series: list[SectorFlowSeries],
    ) -> None:
        if not series:
            return
        with self._sector_flow_lock:
            state = self._sector_flow_proxy_by_key.get(cache_key)
            if state is not None and state.get("trade_date") != trade_date:
                state = None
            if state is None:
                state = {"trade_date": trade_date, "points": {}}
                self._sector_flow_proxy_by_key[cache_key] = state
            points_by_name = state.setdefault("points", {})
            for item in series:
                if not item.name or not item.points:
                    continue
                existing = dict(points_by_name.get(item.name) or {})
                for point in item.points:
                    if point.time:
                        existing[str(point.time)] = float(point.value)
                if existing:
                    points_by_name[item.name] = existing

    def _schedule_sector_flow_trajectory_refresh(
        self,
        cache_key: str,
        trade_date: str,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None,
        quotes: list[Quote] | None,
    ) -> None:
        """冻结分支的后台轨迹重建：与盘中分钟线回灌共用去重线程表。"""
        with self._sector_flow_lock:
            thread = self._sector_flow_refresh_threads.get(cache_key)
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._refresh_sector_flow_trajectory,
                args=(cache_key, trade_date, sectors, member_code_loader, quotes),
                name=f"watchtower-sector-flow-traj-{abs(hash(cache_key))}",
                daemon=True,
            )
            self._sector_flow_refresh_threads[cache_key] = thread
            thread.start()

    def _refresh_sector_flow_trajectory(
        self,
        cache_key: str,
        trade_date: str,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None,
        quotes: list[Quote] | None,
    ) -> None:
        try:
            flow = self._sector_flow_from_trajectory(
                trade_date,
                sectors,
                member_code_loader=member_code_loader,
                quotes=quotes,
                state_key=cache_key,
            )
            if flow:
                with self._sector_flow_lock:
                    self._sector_flow_cache_by_key[cache_key] = (time.time(), flow)
                # 让下一轮终端构建立刻采用新曲线（WS 增量随之带出）
                self._clear_payload_caches()
        except Exception:  # pragma: no cover - 后台重建失败不影响请求路径
            pass
        finally:
            with self._sector_flow_lock:
                current = self._sector_flow_refresh_threads.get(cache_key)
                if current is threading.current_thread():
                    self._sector_flow_refresh_threads.pop(cache_key, None)

    def _ensure_sector_flow_refresh(
        self,
        cache_key: str,
        snapshot: MarketSnapshot,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None = None,
    ) -> None:
        with self._sector_flow_lock:
            thread = self._sector_flow_refresh_threads.get(cache_key)
            if thread is not None and thread.is_alive():
                return
            # 冷启动/晚启动回灌限速：曲线厚度或起点不达标时重试，但失败
            # 后按 sector_flow_minute_refresh_seconds 放慢节奏，避免打满分钟线源。
            last_build = self._sector_flow_last_build_at.get(cache_key, 0.0)
            if time.time() - last_build < max(15, self.settings.sector_flow_minute_refresh_seconds):
                return
            self._sector_flow_last_build_at[cache_key] = time.time()
            thread = threading.Thread(
                target=self._refresh_sector_flow_backfill_from_trajectory,
                args=(cache_key, snapshot, sectors, member_code_loader),
                name=f"watchtower-sector-flow-{abs(hash(cache_key))}",
                daemon=True,
            )
            self._sector_flow_refresh_threads[cache_key] = thread
            thread.start()

    def _refresh_sector_flow_backfill_from_trajectory(
        self,
        cache_key: str,
        snapshot: MarketSnapshot,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None,
    ) -> None:
        """盘中冷启动回灌：优先用本地轨迹按「全成员总量」同一口径重建当日分钟历史。

        CloudRun 容器中途启动导致本地轨迹为空时，回退到 easy_tdx 当日分钟线
        补一次早盘曲线；后续仍由实时快照代理继续增量。
        """
        try:
            trade_date = str(snapshot.source_status.get("trade_date") or china_now().strftime("%Y%m%d"))
            flow_sectors = self._sector_flow_membership(cache_key, sectors)
            flow = self._sector_flow_from_stock_trajectory(
                trade_date,
                flow_sectors,
                member_code_loader,
                getattr(snapshot, "quotes", None),
            )
            minute_backfilled = False
            if not flow or self._sector_flow_needs_opening_backfill(flow, snapshot):
                # CloudRun 容器中途启动时本地轨迹可能从启动时刻才有数据。
                # 这种场景用 easy_tdx 当日分钟线补一次展示板块的早盘资金曲线，
                # 后续仍由实时快照代理继续增量，避免资金面板永久缺上午。
                fallback_flow = self._build_and_cache_sector_flow(
                    cache_key,
                    snapshot,
                    flow_sectors,
                    member_code_loader=member_code_loader,
                )
                if fallback_flow:
                    flow = fallback_flow
                    minute_backfilled = True
            if flow:
                if not minute_backfilled:
                    with self._sector_flow_lock:
                        proxy_state = self._sector_flow_proxy_by_key.get(cache_key)
                        if proxy_state is not None and proxy_state.get("trade_date") == trade_date:
                            for series in flow:
                                if not series.points:
                                    continue
                                live_map = dict(proxy_state["points"].get(series.name) or {})
                                forming_label = max(live_map) if live_map else ""
                                merged = {
                                    point.time: float(point.value)
                                    for point in series.points
                                    if point.time and point.time != forming_label
                                }
                                merged.update(live_map)
                                proxy_state["points"][series.name] = merged
                    self._clear_payload_caches()
                    self._fast_board_entries_cache.clear()
        except Exception:  # pragma: no cover - 后台回灌失败不影响请求路径
            pass
        finally:
            with self._sector_flow_lock:
                current = self._sector_flow_refresh_threads.get(cache_key)
                if current is threading.current_thread():
                    self._sector_flow_refresh_threads.pop(cache_key, None)
                self._clear_payload_caches()
                self._fast_board_entries_cache.clear()

    def _build_and_cache_sector_flow(
        self,
        cache_key: str,
        snapshot: MarketSnapshot,
        sectors: list[SectorSnapshot],
        member_code_loader: Callable[[SectorSnapshot], list[str]] | None = None,
    ) -> list[SectorFlowSeries]:
        trade_date = str(snapshot.source_status.get("trade_date") or china_now().strftime("%Y%m%d"))
        live_mode = snapshot.data_mode == "live" or snapshot.source_status.get("active_source") == "easy_tdx"
        limit = max(1, int(self.engine.sector_flow_top_n))
        # 成员口径与盘中代理/轨迹分支一致：热度 top-N ∪ 资金净流入 top-5 + 滞回
        stable_sectors = self._sector_flow_membership(cache_key, sectors)
        minute_series_map: dict[str, list[dict]] = {}
        sector_member_codes: dict[str, list[str]] = {}

        fetch_codes: list[str] = []
        for sector in stable_sectors:
            member_codes = member_code_loader(sector) if member_code_loader else None
            if member_code_loader:
                sector_member_codes[sector.name] = list(member_codes or [])
            for code in self.engine.sector_flow_codes(sector, snapshot.quotes, member_codes=member_codes):
                if code not in fetch_codes:
                    fetch_codes.append(code)

        workers = max(1, min(int(getattr(self.settings, "sector_flow_workers", 4) or 4), 8))
        if fetch_codes and workers > 1 and len(fetch_codes) > 1:
            # 每次拉取都新建独立 TdxClient（见 EasyTdxMinuteReplaySource._client），线程安全。
            with ThreadPoolExecutor(
                max_workers=min(workers, len(fetch_codes)),
                thread_name_prefix="sector-flow-minute",
            ) as pool:
                futures = {
                    pool.submit(self.data_source.fetch_minute_series, code, trade_date, live=bool(live_mode)): code
                    for code in fetch_codes
                }
                for future in as_completed(futures):
                    try:
                        rows = future.result()
                    except Exception:
                        continue
                    if rows:
                        minute_series_map[futures[future]] = rows
        else:
            for code in fetch_codes:
                try:
                    rows = self.data_source.fetch_minute_series(code, trade_date, live=bool(live_mode))
                except Exception:
                    continue
                if rows:
                    minute_series_map[code] = rows

        built = self.engine.build_sector_flow(
            stable_sectors,
            snapshot.quotes,
            minute_series_map,
            sector_member_codes=sector_member_codes or None,
            # 成员上限即展示上限：滞回保留的板块也要能出曲线，
            # 引擎内部已按累计动能（final_value）降序排序，与统一口径一致。
            limit=max(limit, len(stable_sectors)),
        )
        # 引擎产出的是累计曲线，统一差分成「每分钟净流入」口径后再缓存/返回。
        result = [self._per_minute_flow_series(series) for series in built]
        # 总量锚定到当日 L1 主动净额真值（外/内盘计数器差）：分钟形态保留，
        # 成交额×方向口径在趋势日的系统性虚高被收敛；真值不可得时维持原口径。
        quotes_by_code = {quote.code: quote for quote in snapshot.quotes if quote.code}
        sectors_by_name = {sector.name: sector for sector in stable_sectors}
        anchored: list[SectorFlowSeries] = []
        for series in result:
            member_codes = sector_member_codes.get(series.name)
            if not member_codes:
                sector = sectors_by_name.get(series.name)
                member_codes = (
                    self.engine.sector_flow_codes(sector, snapshot.quotes, member_codes=None)
                    if sector is not None
                    else list(series.core_codes or [])
                )
            truth_total = self._active_net_truth_total(member_codes, quotes_by_code)
            shape_total = sum(float(point.value) for point in series.points)
            next_series = self._anchor_series_to_truth(series, truth_total)
            if next_series is series or next_series.flow_basis != "每分钟净流入(分钟形态×L1主动量定标)":
                logger.info(
                    "sector_flow anchor skipped: name=%s members=%d truth=%s shape=%.2f quotes=%d",
                    series.name,
                    len(member_codes or []),
                    f"{truth_total:.2f}" if truth_total is not None else "none",
                    shape_total,
                    len(quotes_by_code),
                )
            anchored.append(next_series)
        result = anchored
        with self._sector_flow_lock:
            self._sector_flow_cache_by_key[cache_key] = (time.time(), result)
            if len(self._sector_flow_cache_by_key) > 12:
                oldest = min(self._sector_flow_cache_by_key, key=lambda item: self._sector_flow_cache_by_key[item][0])
                self._sector_flow_cache_by_key.pop(oldest, None)
                self._sector_flow_names_by_key.pop(oldest, None)
            # 冷启动回灌合并：把分钟线历史按时间标签写进代理曲线的 points，
            # 但保留 live 正在成形的最新一分钟（它的 tick 增量更及时），
            # 分钟口径下无需累计偏移，天然无跳变。
            proxy_state = self._sector_flow_proxy_by_key.get(cache_key)
            if proxy_state is not None and proxy_state.get("trade_date") == trade_date:
                for series in result:
                    if not series.points:
                        continue
                    live_map = dict(proxy_state["points"].get(series.name) or {})
                    forming_label = max(live_map) if live_map else ""
                    merged = {
                        point.time: float(point.value)
                        for point in series.points
                        if point.time and point.time != forming_label
                    }
                    merged.update(live_map)
                    proxy_state["points"][series.name] = merged
        self._persist_sector_flow_cloud(cache_key, trade_date, result, force=True)
        return result

    @staticmethod
    def _per_minute_flow_series(series: SectorFlowSeries) -> SectorFlowSeries:
        """把累计动能曲线差分为每分钟净流入：value[t] = cum[t] - cum[t-1]。

        final_value 保持「今日累计动能」语义（差分前的累计终值），
        与快照代理/个股轨迹分支一致，供三分支统一排序。
        """
        if not series.points:
            return series
        diffed: list[SectorFlowPoint] = []
        prev = 0.0
        for point in series.points:
            diffed.append(SectorFlowPoint(time=point.time, value=round(point.value - prev, 2)))
            prev = point.value
        return series.model_copy(
            update={
                "points": diffed,
                "flow_basis": "每分钟净流入代理(分钟成交额加权)",
            }
        )

    def _scan_items_from_quotes(self, quotes: list[Quote]) -> list[WatchlistItem]:
        return [
            WatchlistItem(
                code=quote.code,
                name=quote.name,
                themes=quote.themes,
                core=quote.core,
                position=False,
            )
            for quote in quotes
        ]

    def _formula_candidate_codes(
        self,
        quotes: list[Quote],
        watchlist: list[WatchlistItem],
        positions: dict[str, PositionRecord],
    ) -> list[str]:
        watch_codes = {item.code for item in watchlist if item.code}
        position_codes = {code for code in positions if code}
        if not quotes:
            return list(dict.fromkeys([*watch_codes, *position_codes]))
        max_candidates = 120 if self.settings.scan_scope == "full_market" else 80
        ranked_quotes = sorted(
            quotes,
            key=lambda quote: (
                quote.code in position_codes,
                quote.code in watch_codes,
                bool(quote.core),
                bool(quote.limit_up or quote.opened_limit),
                min(max(float(quote.minute_amount_ratio or 0), 0.0), 9.99),
                abs(float(quote.change_pct or 0)),
                max(float(quote.amount or 0), 0.0),
            ),
            reverse=True,
        )
        ranked_codes = [quote.code for quote in ranked_quotes[:max_candidates] if quote.code]
        return list(
            dict.fromkeys(
                [
                    *[code for code in watch_codes if code],
                    *[code for code in position_codes if code],
                    *ranked_codes,
                ]
            )
        )

    def _formula_rows_by_code_for_context(
        self,
        *,
        trade_date: str,
        quotes: list[Quote],
        watchlist: list[WatchlistItem],
        positions: dict[str, PositionRecord],
    ) -> dict[str, list[dict[str, Any]]]:
        normalized_date = str(trade_date or "").strip()
        if not normalized_date or not getattr(self.trajectory_store, "enabled", False):
            return {}
        candidate_codes = self._formula_candidate_codes(quotes, watchlist, positions)
        if not candidate_codes:
            return {}
        if len(candidate_codes) == 1:
            # 详情页单票路径：走短 TTL 内存缓存，避开大库随机页读的磁盘抖动
            return {
                code: rows
                for code in candidate_codes
                if len(rows := self._feature_series_small_cached(normalized_date, code)) >= 2
            }
        batch_loader = getattr(self.trajectory_store, "stock_feature_series_by_code", None)
        if callable(batch_loader):
            return {
                code: rows
                for code, rows in batch_loader(normalized_date, candidate_codes, max_rows=180).items()
                if len(rows) >= 2
            }
        rows_by_code: dict[str, list[dict[str, Any]]] = {}
        for code in candidate_codes:
            rows = self.trajectory_store.stock_feature_series(normalized_date, code, max_rows=180)
            if len(rows) >= 2:
                rows_by_code[code] = rows
        return rows_by_code

    def _feature_series_small_cached(
        self,
        trade_date: str,
        code: str,
        *,
        max_rows: int = 180,
    ) -> list[dict[str, Any]]:
        """单票公式行的短 TTL 缓存；数据更新节奏等于采集间隔。"""
        bounded_rows = max(2, min(int(max_rows or 180), 2880))
        key = (trade_date, code, bounded_rows)
        now = time.time()
        cached = self._formula_rows_small_cache.get(key)
        ttl = max(5.0, float(self.settings.background_collector_seconds))
        if cached and now - cached[0] < ttl:
            return cached[1]
        rows = self.trajectory_store.stock_feature_series(trade_date, code, max_rows=bounded_rows)
        self._formula_rows_small_cache[key] = (now, rows)
        if len(self._formula_rows_small_cache) > 64:
            oldest_key = min(
                self._formula_rows_small_cache,
                key=lambda item: self._formula_rows_small_cache[item][0],
            )
            self._formula_rows_small_cache.pop(oldest_key, None)
        return rows

    def _market_for_signals(
        self,
        snapshot: MarketSnapshot,
        market: MarketState,
    ) -> MarketState:
        """Do not promote a first live snapshot into a formal buy signal."""
        live = bool(
            snapshot.data_mode == "live"
            or snapshot.source_status.get("active_source") == "easy_tdx"
        )
        if live and market.index_turning_mode == "snapshot_rebound_proxy":
            reasons = list(market.reasons)
            reasons.append("实时拐头只有单快照，正式买T等待下一分钟状态确认")
            return market.model_copy(
                update={
                    "index_turning": False,
                    "reasons": list(dict.fromkeys(reasons)),
                }
            )
        return market

    def _fetch_transaction_flow(
        self,
        code: str,
        trade_date: str,
        full_session: bool = False,
    ) -> TransactionFlowObservation:
        """Fetch the optional real transaction tape without blocking details on errors."""
        fetcher = getattr(self.data_source, "fetch_transaction_flow", None)
        if not callable(fetcher):
            return TransactionFlowObservation(
                trade_date=trade_date,
                note="当前数据路由未提供逐笔成交接口",
            )
        try:
            try:
                result = fetcher(
                    code,
                    trade_date=trade_date,
                    full_session=full_session,
                )
            except TypeError as exc:
                # Keep compatibility with lightweight test/custom providers
                # that implemented the older three-argument contract.
                if "full_session" not in str(exc):
                    raise
                result = fetcher(code, trade_date=trade_date)
        except Exception as exc:  # pragma: no cover - provider dependent
            return TransactionFlowObservation(
                source="easy_tdx_transaction_data",
                trade_date=trade_date,
                note=f"逐笔成交读取失败：{exc}",
            )
        return result if isinstance(result, TransactionFlowObservation) else TransactionFlowObservation(
            trade_date=trade_date,
            note="逐笔成交接口返回格式不可识别",
        )

    def _signal_detail_context(
        self,
        code: str,
        sector: str | None = None,
        trade_date: str | None = None,
        client_watchlist: list[WatchlistItem] | None = None,
    ) -> SignalDetailContext:
        normalized_code = str(code or "").strip().zfill(6)
        normalized_watchlist = self._normalize_client_watchlist(client_watchlist) or []
        requested_sector = self._normalize_sector(sector)
        base_context = self._get_context()
        current_context = self._context_with_client_watchlist(base_context, normalized_watchlist or None)
        # 缓存键只用稳定维度：context.updated_at 每次全市场刷新都会变，
        # 绑进键里等于盘中每次轮询都冷启动（分钟行+公式全部重算）。
        # 新鲜度由短 TTL 保证，图表尾部的实时价由 _merge_live_quote_tail 合并。
        cache_key = "|".join(
            [
                str(base_context.source_status.get("trade_date") or ""),
                str(bool(base_context.market.frozen)),
                normalized_code,
                str(requested_sector or ""),
                str(trade_date or ""),
                self._watchlist_signature(normalized_watchlist),
            ]
        )

        def build() -> SignalDetailContext:
            current_trade_date = str(current_context.source_status.get("trade_date") or "")
            actual_trade_date = trade_date or current_trade_date or china_now().strftime("%Y%m%d")
            context = self._context_for_trade_date(current_context, actual_trade_date)
            context = self._context_with_client_watchlist(context, normalized_watchlist or None)
            quote = self._quote_for_code(context.snapshot.quotes, normalized_code)
            if quote is None:
                raise ValueError(f"未找到 {normalized_code} 的行情数据")

            signal = self._signal_for_code(context.signals_all, normalized_code)
            if signal is None:
                raise ValueError(f"未找到 {normalized_code} 的信号数据")

            sector_snapshot = self._best_sector_for_quote(
                quote,
                context.sectors,
                preferred_sector_names=self._manual_theme_names(context.themes),
                requested_sector=requested_sector,
            )
            selected_sector = sector_snapshot.name if sector_snapshot else requested_sector
            position = self.position_store.get(normalized_code)
            watchlist_item = self._watchlist_item_for_code(context.watchlist, normalized_code)
            live_mode = context.snapshot.data_mode == "live" or context.source_status.get("active_source") == "easy_tdx"
            if sector_snapshot is not None:
                scoped_quotes = [
                    quote if item.code == quote.code else item
                    for item in context.snapshot.quotes
                ]
                scoped_formula_rows = self._formula_rows_by_code_for_context(
                    trade_date=actual_trade_date,
                    quotes=[quote],
                    watchlist=[
                        WatchlistItem(
                            code=quote.code,
                            name=quote.name,
                            themes=list(quote.themes),
                            core=quote.core,
                        )
                    ],
                    positions={quote.code: position} if position else {},
                )
                scoped_signals = self.engine.build_signals(
                    scoped_quotes,
                    [
                        WatchlistItem(
                            code=quote.code,
                            name=quote.name,
                            themes=list(quote.themes),
                            core=quote.core,
                        )
                    ],
                    context.sectors,
                    self._market_for_signals(context.snapshot, context.market),
                    clock_label=str(context.source_status.get("clock_label") or context.market.updated_at or ""),
                    preferred_sector_names={sector_snapshot.name},
                    positions={quote.code: position} if position else {},
                    formula_rows_by_code=scoped_formula_rows,
                    sector_name_mapper=self._display_sector_name,
                )
                if scoped_signals:
                    scoped_signal = scoped_signals[0]
                    signal = scoped_signal.model_copy(
                        update={
                            "pinned": signal.pinned,
                            "watchlist_tags": list(signal.watchlist_tags),
                        }
                    )

            return SignalDetailContext(
                context=context,
                actual_trade_date=actual_trade_date,
                quote=quote,
                signal=signal,
                sector_snapshot=sector_snapshot,
                selected_sector=selected_sector,
                position=position,
                watchlist_item=watchlist_item,
                live_mode=live_mode,
            )

        return self._cached_value(
            self._signal_detail_context_cache,
            self._signal_detail_context_cache_lock,
            self._signal_detail_context_build_locks,
            cache_key,
            5.0,
            build,
            max_entries=24,
        )

    def _detail_chart_series(
        self,
        info: SignalDetailContext,
        minute_rows: list[dict[str, Any]],
    ) -> DetailChartSeries:
        if not minute_rows:
            return DetailChartSeries(
                prev_close=info.quote.prev_close,
                source_quality="minute_proxy",
            )

        count = len(minute_rows)
        fallback_times = self.engine._session_times(count)
        times: list[str] = []
        for idx, row in enumerate(minute_rows):
            fallback = fallback_times[idx] if idx < len(fallback_times) else ""
            times.append(str(row.get("time") or fallback)[:5])
        raw_prices = [
            float(row.get("price") or info.quote.prev_close or info.quote.price or 0)
            for row in minute_rows
        ]
        prices = self.engine._normalize_replay_prices(
            raw_prices,
            info.quote.prev_close,
            info.quote.day_low,
            info.quote.day_high,
        )
        volumes = [max(float(row.get("vol") or row.get("volume") or 0), 0) for row in minute_rows]
        amounts = []
        for row, price, volume in zip(minute_rows, prices, volumes):
            raw_amount = max(float(row.get("amount") or 0), 0)
            amounts.append(raw_amount if raw_amount > 0 else volume * price * 100)

        avg_amount = sum(amounts) / len(amounts) if amounts else 1
        if avg_amount <= 0:
            avg_amount = 1

        cumulative_amount = 0.0
        cumulative_volume = 0.0
        vwaps: list[float] = []
        change_pcts: list[float] = []
        amount_ratios: list[float] = []
        flow_scores: list[int] = []
        for idx, (price, volume, amount) in enumerate(zip(prices, volumes, amounts)):
            cumulative_amount += amount
            cumulative_volume += volume * 100
            vwap = cumulative_amount / cumulative_volume if cumulative_volume > 0 else price
            vwaps.append(round(vwap, 4))
            change_pct = ((price - info.quote.prev_close) / info.quote.prev_close * 100) if info.quote.prev_close else 0
            change_pcts.append(round(change_pct, 2))
            amount_ratio = amount / avg_amount if avg_amount else 1
            amount_ratios.append(round(amount_ratio, 2))
            gap_pct = ((price - vwap) / vwap * 100) if vwap > 0 else 0
            volume_push = (amount_ratio - 1) * 14
            direction = 1 if idx == 0 or price >= prices[idx - 1] else -1
            flow_score = int(max(-100, min(100, round(direction * max(-12, min(12, volume_push)) + gap_pct * 5))))
            flow_scores.append(flow_score)

        source_quality = "live_minute_chart" if info.live_mode else "historical_minute_chart"
        return DetailChartSeries(
            times=[str(time_label)[:5] for time_label in times],
            prices=[round(price, 2) for price in prices],
            vwaps=vwaps,
            volumes=[round(volume, 2) for volume in volumes],
            change_pcts=change_pcts,
            amount_ratios=amount_ratios,
            flow_scores=flow_scores,
            prev_close=round(info.quote.prev_close, 2),
            source_quality=source_quality,
            point_count=count,
            start_time=str(times[0])[:5] if times else "",
            end_time=str(times[-1])[:5] if times else "",
            latest_price=round(prices[-1], 2),
            latest_change_pct=change_pcts[-1] if change_pcts else 0,
        )

    @staticmethod
    def _formula_state_raw_value(state: dict[str, Any], *keys: str, default: Any = 0) -> Any:
        for key in keys:
            if key in state:
                return state.get(key)
        return default

    @staticmethod
    def _formula_state_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "__dict__"):
            return dict(value.__dict__)
        try:
            return dict(value)
        except Exception:
            return {}

    @staticmethod
    def _day_context_for_quote(quote: Quote) -> ZuoTDayContext:
        """做T公式日级常量输入（通达信 DYNAINFO 等价物）。

        量比无直接行情字段，沿用现有分钟量比代理；换手率需要流通股本，
        不在分时热路径拉取 F10，缺省时前端显示 --。
        """
        flow = quote.order_flow
        return ZuoTDayContext(
            prev_close=float(quote.prev_close or 0),
            day_high=float(quote.day_high or 0),
            day_low=float(quote.day_low or 0),
            change_pct=float(quote.change_pct or 0),
            volume_ratio=float(quote.minute_amount_ratio or 1) or 1.0,
            turnover_rate=None,
            total_amount=float(quote.amount or 0),
            outer_volume=float(flow.active_buy_volume or 0),
            inner_volume=float(flow.active_sell_volume or 0),
        )

    def _stock_detail_bundle(self, info: SignalDetailContext) -> StockDetailBundle:
        """详情页共享 bundle：分钟行 + 分时序列 + 做T公式 + 买卖标记。

        chart/overlay/详情三个入口共用同一份计算结果；新做T公式为 O(n)
        递推，重建成本毫秒级，缓存只用于削掉并发重复与高频轮询抖动。
        """
        cache_key = "|".join(
            [
                info.actual_trade_date,
                info.quote.code,
                str(bool(info.live_mode)),
            ]
        )

        def build() -> StockDetailBundle:
            shared = self._shared_stock_chart_rows(info)
            rows = shared.rows
            chart = self._detail_chart_series(info, rows)
            formula_rows = self._formula_rows_for_detail(info, rows)
            day = self._day_context_for_quote(info.quote)
            formula_result = None
            if formula_rows:
                try:
                    formula_result = compute_zuot_series(formula_rows, day)
                except Exception:
                    formula_result = None
            formula_state = self._zuot_state_model(formula_result)
            formula_overlay = self._zuot_formula_overlay(formula_result, info.quote.prev_close)
            markers = self._zuot_overlay_markers(info, formula_result)
            if markers:
                # 详情页拿的是全天分钟公式；把最后一个买卖点回填给榜单的全天窗口缓存。
                latest = markers[-1]
                self._record_last_action_event(info.quote.code, latest.signal, latest.price, latest.time)
            summary = self._detail_chart_summary(info, chart)
            return StockDetailBundle(
                rows=rows,
                chart=chart,
                formula_result=formula_result,
                formula_state=formula_state,
                formula_overlay=formula_overlay,
                markers=markers,
                summary=summary,
                error=shared.error,
            )

        ttl = 4.0 if info.live_mode else 30.0
        return self._cached_value(
            self._detail_bundle_cache,
            self._detail_bundle_cache_lock,
            self._detail_bundle_build_locks,
            cache_key,
            ttl,
            build,
            max_entries=24,
        )

    @staticmethod
    def _zuot_state_model(formula_result: Any) -> FormulaState:
        latest = getattr(formula_result, "latest", None) if formula_result is not None else None
        if not latest:
            return FormulaState()
        data = {
            key: value
            for key, value in dict(latest).items()
            if key in FormulaState.model_fields
        }
        data["price"] = float(dict(latest).get("close") or 0)
        data["source_quality"] = str(dict(latest).get("source_quality") or SIGNAL_VERSION)
        return FormulaState(**data)

    @staticmethod
    def _zuot_formula_overlay(formula_result: Any, prev_close: float) -> FormulaOverlay:
        day = getattr(formula_result, "day", None) if formula_result is not None else None
        base = float(prev_close or 0)
        if day is None or base <= 0 or not day.levels_available:
            return FormulaOverlay()

        def pct(value: float) -> float:
            return round((float(value) / base - 1.0) * 100.0, 3)

        return FormulaOverlay(
            available=True,
            resistance_pct=pct(day.resistance),
            support_pct=pct(day.support),
            resistance=round(float(day.resistance), 3),
            support=round(float(day.support), 3),
        )

    def _zuot_overlay_markers(
        self,
        info: SignalDetailContext,
        formula_result: Any,
    ) -> list[SignalDetailOverlayMarker]:
        """做T公式买卖标记：买=LONGCROSS(支撑,现价,2)，卖=LONGCROSS(现价,阻力,2)。"""
        states = list(getattr(formula_result, "states", []) or []) if formula_result is not None else []
        if not states:
            return []
        has_position = bool(info.position and info.position.quantity > 0)
        t_plus_one_restricted = bool(has_position and info.position.available_quantity <= 0)
        prev_close = float(info.quote.prev_close or 0)
        markers: list[SignalDetailOverlayMarker] = []
        for index, state in enumerate(states):
            side = "buy" if state.get("buy_signal") else "sell" if state.get("sell_signal") else ""
            if not side:
                continue
            price = float(state.get("close") or 0)
            change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0
            time_label = str(state.get("time") or "")[:5]
            if side == "buy":
                reasons = ["LONGCROSS(支撑,现价,2) 回踩支撑↖买"]
                risks: list[str] = []
                invalidation = float(state.get("support") or 0)
            else:
                reasons = ["LONGCROSS(现价,阻力,2) 冲高兑现↗卖"]
                risks = []
                if t_plus_one_restricted:
                    risks.append("A股T+1限制：当前持仓可卖数量为0")
                elif not has_position:
                    risks.append("未录入本地持仓，绿色卖T仅作风险提示")
                invalidation = float(state.get("resistance") or 0)
            markers.append(
                SignalDetailOverlayMarker(
                    id=f"zuot|{time_label}|{side}|{index}",
                    time=time_label,
                    signal=SignalType.BUY_T if side == "buy" else SignalType.SELL_T,
                    price=round(price, 2),
                    change_pct=round(change_pct, 2),
                    phase=SignalPhase.CONFIRM.value if side == "buy" else SignalPhase.SELL_CONFIRM.value,
                    reasons=reasons,
                    risks=risks,
                    invalidation_price=round(invalidation, 2),
                    source_quality=SIGNAL_VERSION,
                    direction=TradeDirection.POSITIVE_T.value if side == "buy" else TradeDirection.REVERSE_T.value,
                    action=TradeAction.BUY_T.value if side == "buy" else TradeAction.SELL_BASE.value,
                    setup="zuot_support_rebound" if side == "buy" else "zuot_resistance_fade",
                    regime=SIGNAL_VERSION,
                    executable=False,
                    execution_reason="做T公式信号；执行前需结合持仓与L1逐笔确认",
                    t_plus_one_restricted=t_plus_one_restricted,
                )
            )
        return markers

    def _zuot_risk_reward(self, info: SignalDetailContext, formula_state: FormulaState) -> RiskRewardPlan:
        """用做T公式的阻力/支撑给出当前价位的盈亏比计划（支撑低吸→阻力高抛）。"""
        price = float(info.quote.price or 0)
        support = float(formula_state.support or 0)
        resistance = float(formula_state.resistance or 0)
        if price <= 0 or support <= 0 or resistance <= support:
            return RiskRewardPlan(context="等待阻力/支撑有效")
        risk_pct = (price - support) / price * 100
        reward_pct = (resistance - price) / price * 100
        ratio = reward_pct / risk_pct if risk_pct > 0 else 0.0
        min_required = 1.5
        favorable = bool(risk_pct > 0 and ratio >= min_required)
        return RiskRewardPlan(
            available=True,
            favorable=favorable,
            context="支撑低吸 → 阻力高抛" if favorable else "当前价位盈亏比不足",
            structure="阻力/支撑通道",
            status="盈亏比占优" if favorable else "盈亏比不足",
            direction=TradeDirection.POSITIVE_T.value,
            action=TradeAction.BUY_T.value,
            entry_price=round(price, 2),
            support_price=round(support, 2),
            invalidation_price=round(support, 2),
            target_price=round(resistance, 2),
            risk_pct=round(risk_pct, 2),
            expected_reward_pct=round(reward_pct, 2),
            reward_risk_ratio=round(ratio, 2),
            min_required_ratio=min_required,
            reasons=[f"目标阻力 {resistance:.2f} / 失效支撑 {support:.2f}"],
            risks=[] if favorable else ["现价距阻力过近或已跌破支撑，等待回踩"],
        )

    def _zuot_replay(
        self,
        info: SignalDetailContext,
        bundle: StockDetailBundle,
        *,
        transaction_flow: TransactionFlowObservation | None = None,
    ) -> tuple[list[ReplayPoint], list[ReplayMarker], list[ReplayMarker], list[str]]:
        """分钟回放点 + 做T买卖标记（买卖点的唯一来源：做T公式.md）。"""
        chart = bundle.chart
        if not chart.point_count:
            return [], [], [], ["暂无分钟回放数据"]
        states = list(getattr(bundle.formula_result, "states", []) or [])
        has_position = bool(info.position and info.position.quantity > 0)
        t_plus_one_restricted = bool(has_position and info.position.available_quantity <= 0)
        tx_available = bool(transaction_flow and transaction_flow.available)

        replay_points: list[ReplayPoint] = []
        markers: list[ReplayMarker] = []
        running_low = chart.prices[0] if chart.prices else 0.0
        running_high = running_low
        for index in range(chart.point_count):
            price = chart.prices[index]
            running_low = min(running_low, price)
            running_high = max(running_high, price)
            rebound = (price - running_low) / running_low * 100 if running_low else 0.0
            pullback = (running_high - price) / running_high * 100 if running_high else 0.0
            state = states[index] if index < len(states) else {}
            buy = bool(state.get("buy_signal"))
            sell = bool(state.get("sell_signal"))
            point_signal = SignalType.BUY_T if buy else SignalType.SELL_T if sell else SignalType.WATCH
            if buy:
                point_reasons = ["LONGCROSS(支撑,现价,2) 回踩支撑↖买"]
                point_risks: list[str] = []
                invalidation = float(state.get("support") or 0)
            elif sell:
                point_reasons = ["LONGCROSS(现价,阻力,2) 冲高兑现↗卖"]
                point_risks = []
                if t_plus_one_restricted:
                    point_risks.append("A股T+1限制：当前持仓可卖数量为0")
                elif not has_position:
                    point_risks.append("未录入本地持仓，绿色卖T仅作风险提示")
                invalidation = float(state.get("resistance") or 0)
            else:
                point_reasons = []
                point_risks = []
                invalidation = 0.0
            point_phase = (
                SignalPhase.CONFIRM.value
                if buy
                else SignalPhase.SELL_CONFIRM.value
                if sell
                else SignalPhase.OBSERVE.value
            )
            point_action = (
                TradeAction.BUY_T.value if buy else TradeAction.SELL_BASE.value if sell else TradeAction.OBSERVE.value
            )
            point_direction = (
                TradeDirection.POSITIVE_T.value
                if buy
                else TradeDirection.REVERSE_T.value
                if sell
                else TradeDirection.NONE.value
            )
            factor_scores = {
                "大单净额_万": float(state.get("fund_flow") or 0),
                "大单买_万": float(state.get("big_buy_amount") or 0),
                "大单卖_万": float(state.get("big_sell_amount") or 0),
            }
            replay_points.append(
                ReplayPoint(
                    time=chart.times[index],
                    price=price,
                    change_pct=chart.change_pcts[index],
                    rebound_from_low_pct=round(rebound, 2),
                    pullback_from_high_pct=round(pullback, 2),
                    volume=chart.volumes[index],
                    minute_amount_ratio=chart.amount_ratios[index],
                    signal=point_signal,
                    reasons=point_reasons,
                    factor_flags=["公式买入原语"] if buy else ["公式卖出原语"] if sell else [],
                    vwap=chart.vwaps[index],
                    flow_score=chart.flow_scores[index],
                    signal_grade="公式买T" if buy else "公式卖T" if sell else "观察",
                    phase=point_phase,
                    risks=point_risks,
                    invalidation_price=round(invalidation, 2),
                    source_quality=SIGNAL_VERSION,
                    factor_scores=factor_scores,
                    t_plus_one_restricted=t_plus_one_restricted,
                    direction=point_direction,
                    action=point_action,
                    setup="zuot_support_rebound" if buy else "zuot_resistance_fade" if sell else "",
                    regime=SIGNAL_VERSION,
                    executable=False,
                    execution_reason="做T公式信号；执行前需结合持仓与L1逐笔确认" if point_signal != SignalType.WATCH else "",
                    evidence_sequence=point_reasons,
                )
            )
            if buy or sell:
                markers.append(
                    ReplayMarker(
                        time=chart.times[index],
                        signal=point_signal,
                        price=price,
                        change_pct=chart.change_pcts[index],
                        reasons=point_reasons,
                        factor_flags=["公式买入原语"] if buy else ["公式卖出原语"],
                        signal_grade="公式买T" if buy else "公式卖T",
                        phase=point_phase,
                        risks=point_risks,
                        invalidation_price=round(invalidation, 2),
                        source_quality=SIGNAL_VERSION,
                        factor_scores=factor_scores,
                        t_plus_one_restricted=t_plus_one_restricted,
                        direction=point_direction,
                        action=point_action,
                        setup="zuot_support_rebound" if buy else "zuot_resistance_fade",
                        regime=SIGNAL_VERSION,
                        executable=False,
                        execution_reason="做T公式信号；执行前需结合持仓与L1逐笔确认",
                        evidence_sequence=point_reasons,
                    )
                )

        timeline: list[ReplayMarker] = list(markers)
        if replay_points:
            first_point = replay_points[0]
            timeline.insert(
                0,
                ReplayMarker(
                    time=first_point.time,
                    signal=SignalType.WATCH,
                    price=first_point.price,
                    change_pct=first_point.change_pct,
                    reasons=["开盘观察，等待做T公式买卖信号"],
                    phase=SignalPhase.OBSERVE.value,
                    source_quality=SIGNAL_VERSION,
                ),
            )

        buy_count = sum(1 for marker in markers if marker.signal == SignalType.BUY_T)
        sell_count = sum(1 for marker in markers if marker.signal == SignalType.SELL_T)
        summary = [
            "公式引擎：做T买卖点唯一来源为 做T公式.md（阻力/支撑/均线系统）",
            f"分钟点 {len(replay_points)} 个",
        ]
        if chart.prices:
            summary.append(f"区间 {min(chart.prices):.2f} - {max(chart.prices):.2f}")
        summary.append(f"公式信号 买T {buy_count} / 卖T {sell_count}")
        if tx_available and transaction_flow:
            summary.append(
                f"L1成交流 {transaction_flow.count}笔，方向差{transaction_flow.imbalance_pct:+.1f}%；"
                "只作承接/抛压确认，不单独生成买卖点"
            )
        if info.selected_sector:
            summary.append(f"板块视图 {info.selected_sector}")
        if not markers:
            summary.append("未出现做T公式买卖信号")
        return replay_points, markers, timeline, summary

    def _formula_rows_for_detail(
        self,
        info: SignalDetailContext,
        minute_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not minute_rows:
            return []
        count = len(minute_rows)
        fallback_times = self.engine._session_times(count)
        times = [
            str(row.get("time") or row.get("datetime") or (fallback_times[idx] if idx < len(fallback_times) else ""))[:5]
            for idx, row in enumerate(minute_rows)
        ]
        raw_prices = [
            self._safe_float(row.get("close") or row.get("price") or row.get("last") or info.quote.prev_close)
            for row in minute_rows
        ]
        prices = self.engine._normalize_replay_prices(
            raw_prices,
            info.quote.prev_close,
            info.quote.day_low,
            info.quote.day_high,
        )
        raw_opens = [
            self._safe_float(
                row.get("open"),
                prices[idx - 1] if idx else info.quote.open or info.quote.prev_close or prices[idx],
            )
            for idx, row in enumerate(minute_rows)
        ]
        raw_highs = [
            self._safe_float(row.get("high"), max(prices[idx], raw_opens[idx]))
            for idx, row in enumerate(minute_rows)
        ]
        raw_lows = [
            self._safe_float(row.get("low"), min(prices[idx], raw_opens[idx]))
            for idx, row in enumerate(minute_rows)
        ]
        opens = self.engine._normalize_replay_prices(raw_opens, info.quote.prev_close, info.quote.day_low, info.quote.day_high)
        highs = self.engine._normalize_replay_prices(raw_highs, info.quote.prev_close, info.quote.day_low, info.quote.day_high)
        lows = self.engine._normalize_replay_prices(raw_lows, info.quote.prev_close, info.quote.day_low, info.quote.day_high)
        output: list[dict[str, Any]] = []
        for idx, (row, price) in enumerate(zip(minute_rows, prices)):
            volume = max(self._safe_float(row.get("vol") or row.get("volume")), 0.0)
            amount = max(self._safe_float(row.get("amount")), 0.0)
            if amount <= 0 and volume > 0 and price > 0:
                amount = volume * price * 100
            open_price = opens[idx] if idx < len(opens) else price
            high_price = max(highs[idx] if idx < len(highs) else price, open_price, price)
            low_price = min(lows[idx] if idx < len(lows) else price, open_price, price)
            output.append(
                {
                    "time": times[idx],
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": price,
                    "price": price,
                    "vol": volume,
                    "amount": amount,
                }
            )
        return output

    @staticmethod
    def _l1_sell_pressure(transaction_flow: TransactionFlowObservation | None) -> bool:
        if not transaction_flow or not transaction_flow.available:
            return False
        return bool(
            transaction_flow.score <= -25
            or transaction_flow.imbalance_pct <= -20
            or transaction_flow.large_imbalance_pct <= -25
        )

    @staticmethod
    def _l1_buy_support(transaction_flow: TransactionFlowObservation | None) -> bool:
        if not transaction_flow or not transaction_flow.available:
            return False
        return bool(
            transaction_flow.score >= 18
            or transaction_flow.imbalance_pct >= 18
            or transaction_flow.large_imbalance_pct >= 8
        )

    def _confluence_snapshot(
        self,
        info: SignalDetailContext,
        *,
        chart: DetailChartSeries,
        formula_state: FormulaState,
        transaction_flow: TransactionFlowObservation | None = None,
    ) -> ConfluenceSnapshot:
        latest_ratio = chart.amount_ratios[-1] if chart.amount_ratios else info.quote.minute_amount_ratio
        max_ratio = max(chart.amount_ratios) if chart.amount_ratios else info.quote.minute_amount_ratio
        tx_available = bool(transaction_flow and transaction_flow.available)
        tx_score = int(transaction_flow.score if transaction_flow else 0)
        tx_pressure = self._l1_sell_pressure(transaction_flow)
        tx_support = self._l1_buy_support(transaction_flow)
        sector = info.sector_snapshot
        market = info.context.market
        sector_attack = bool(sector and (sector.core_attack or sector.limit_up_count > 0))
        index_turning = bool(market.index_turning or market.index_slope_pct > 0)
        volume_expanding = bool(latest_ratio >= 1.3 or max_ratio >= 1.6)
        score = 0
        score += 25 if tx_support else -25 if tx_pressure else 0
        score += 20 if volume_expanding else 0
        score += 25 if sector_attack else 0
        score += 20 if index_turning else 0
        score += 10 if formula_state.buy_signal else 0
        summary = []
        summary.append("L1承接增强" if tx_support else "L1抛压" if tx_pressure else "L1成交流待确认")
        summary.append("分时放量" if volume_expanding else "量能平稳")
        summary.append("板块进攻" if sector_attack else "板块未上攻")
        summary.append("指数拐头" if index_turning else "指数未拐头")
        return ConfluenceSnapshot(
            score=max(-100, min(100, score)),
            summary=summary,
            l1_transaction_flow={
                "available": tx_available,
                "score": tx_score,
                "support": tx_support,
                "sell_pressure": tx_pressure,
                "imbalance_pct": round(transaction_flow.imbalance_pct, 2) if transaction_flow else 0,
                "large_imbalance_pct": round(transaction_flow.large_imbalance_pct, 2) if transaction_flow else 0,
                "count": int(transaction_flow.count) if transaction_flow else 0,
                "label": "L1逐笔成交流" if tx_available else "待详情读取",
            },
            intraday_volume={
                "latest_ratio": round(float(latest_ratio or 0), 2),
                "max_ratio": round(float(max_ratio or 0), 2),
                "expanding": volume_expanding,
                "point_count": chart.point_count,
                "label": "分时放量" if volume_expanding else "分钟量价代理",
            },
            sector_attack={
                "available": sector is not None,
                "core_attack": bool(sector.core_attack) if sector else False,
                "limit_up_count": int(sector.limit_up_count) if sector else 0,
                "leader_code": sector.leader_code if sector else "",
                "leader_name": sector.leader_name if sector else "",
                "heat_score": int(sector.heat_score) if sector else 0,
                "label": "核心进攻" if sector_attack else "板块观察",
            },
            index_turning={
                "turning": index_turning,
                "amount_expanding": bool(market.amount_expanding),
                "slope_pct": round(float(market.index_slope_pct or 0), 3),
                "prior_slope_pct": round(float(market.index_prior_slope_pct or 0), 3),
                "label": "指数拐头" if index_turning else "指数观察",
            },
            source_quality="l1_transaction_minute_formula" if tx_available else "minute_formula_proxy",
            updated_at=str(info.quote.updated_at or info.context.market.updated_at or ""),
        )

    def _shared_stock_chart_rows(self, info: SignalDetailContext) -> SharedMinuteChartRows:
        """Return the one minute-row source used by chart and overlay.

        easy_tdx minute bars can lag the L1 quote snapshot by one minute.  During
        live sessions the dashboard already has the latest full-market quote, so
        merge that quote into the tail of the chart rows.  This keeps the main
        tape preview and detail chart aligned without adding full-market minute
        polling.
        """
        cache_key = "|".join(
            [
                info.actual_trade_date,
                info.quote.code,
                str(bool(info.live_mode)),
            ]
        )

        def build() -> SharedMinuteChartRows:
            try:
                rows = self.data_source.fetch_minute_series(
                    info.quote.code,
                    info.actual_trade_date,
                    live=bool(info.live_mode),
                )
                return SharedMinuteChartRows(
                    rows=self._merge_live_quote_tail(
                        rows,
                        quote=info.quote,
                        market=info.context.market,
                        live=bool(info.live_mode),
                    )
                )
            except Exception as exc:
                return SharedMinuteChartRows(rows=[], error=str(exc))

        return self._cached_value(
            self._detail_minute_rows_cache,
            self._detail_minute_rows_cache_lock,
            self._detail_minute_rows_build_locks,
            cache_key,
            5.0,
            build,
            max_entries=24,
        )

    @staticmethod
    def _minute_label_from_clock(clock_label: str) -> str:
        raw = str(clock_label or "").strip()
        if not raw:
            return ""
        try:
            parts = raw.split()
            time_part = parts[-1] if parts else raw
            hour_text, minute_text, *_ = time_part.split(":")
            hour = int(hour_text)
            minute = int(minute_text)
        except Exception:
            return raw[:5] if ":" in raw else ""

        total = hour * 60 + minute
        if total < 9 * 60 + 30:
            return ""
        if total <= 11 * 60 + 30:
            return f"{hour:02d}:{minute:02d}"
        if total < 13 * 60:
            return "11:30"
        if total <= 15 * 60:
            return f"{hour:02d}:{minute:02d}"
        return "15:00"

    def _merge_live_quote_tail(
        self,
        rows: list[dict[str, Any]],
        *,
        quote: Quote,
        market: MarketState,
        live: bool,
    ) -> list[dict[str, Any]]:
        normalized_rows = [dict(row) for row in rows]
        if not live or quote.price <= 0:
            return normalized_rows

        clock_label = quote.updated_at or market.updated_at
        current_minute = self._minute_label_from_clock(clock_label)
        if not current_minute:
            return normalized_rows

        tail_price = float(quote.price or 0)
        tail_amount = max(float(quote.minute_amount or 0), 0)
        tail_volume = tail_amount / tail_price / 100 if tail_amount > 0 and tail_price > 0 else 0
        tail = {
            "time": current_minute,
            "price": tail_price,
            "vol": tail_volume,
            "amount": tail_amount,
            "source": "live_quote_tail",
        }
        if not normalized_rows:
            return [tail]

        last = normalized_rows[-1]
        last_time = str(last.get("time") or "")[:5]
        fallback_times = self.engine._session_times(len(normalized_rows))
        if not last_time and fallback_times:
            last_time = str(fallback_times[-1])[:5]

        if last_time == current_minute:
            merged = dict(last)
            merged["time"] = current_minute
            merged["price"] = tail_price
            if tail_amount > 0:
                merged["amount"] = tail_amount
            if tail_volume > 0:
                merged["vol"] = tail_volume
            merged["source"] = "live_quote_tail"
            normalized_rows[-1] = merged
            return normalized_rows

        session_order = {time_label: idx for idx, time_label in enumerate(self.engine._session_times(242))}
        last_idx = session_order.get(last_time, -1)
        current_idx = session_order.get(current_minute, -1)
        if current_idx > last_idx:
            normalized_rows.append(tail)
        return normalized_rows

    def _merge_live_index_tail(
        self,
        rows: list[dict[str, Any]],
        *,
        index: IndexSnapshot,
        market: MarketState,
        live: bool,
    ) -> list[dict[str, Any]]:
        normalized_rows = [dict(row) for row in rows]
        if not live or index.price <= 0:
            return normalized_rows

        current_minute = self._minute_label_from_clock(market.updated_at)
        if not current_minute:
            return normalized_rows

        tail = {
            "time": current_minute,
            "price": float(index.price or 0),
            "vol": 0,
            "amount": 0,
            "source": "live_index_tail",
        }
        if not normalized_rows:
            return [tail]

        last = normalized_rows[-1]
        last_time = str(last.get("time") or "")[:5]
        fallback_times = self.engine._session_times(len(normalized_rows))
        if not last_time and fallback_times:
            last_time = str(fallback_times[-1])[:5]
        if last_time == current_minute:
            merged = dict(last)
            merged["time"] = current_minute
            merged["price"] = float(index.price or 0)
            merged["source"] = "live_index_tail"
            normalized_rows[-1] = merged
            return normalized_rows

        session_order = {time_label: idx for idx, time_label in enumerate(self.engine._session_times(242))}
        if session_order.get(current_minute, -1) > session_order.get(last_time, -1):
            normalized_rows.append(tail)
        return normalized_rows

    @staticmethod
    def _detail_chart_summary(info: SignalDetailContext, chart: DetailChartSeries) -> list[str]:
        if not chart.point_count:
            return ["暂无分钟回放数据"]
        prices = chart.prices or [info.quote.price]
        summary = [
            f"分钟点 {chart.point_count} 个",
            f"区间 {min(prices):.2f} - {max(prices):.2f}",
        ]
        if info.selected_sector:
            summary.append(f"板块视图 {info.selected_sector}")
        if chart.latest_change_pct:
            summary.append(f"最新涨跌 {chart.latest_change_pct:+.2f}%")
        return summary

    @staticmethod
    def _transaction_flow_summary(transaction_flow: TransactionFlowObservation) -> dict[str, Any]:
        payload = transaction_flow.model_dump(mode="json", exclude={"points"})
        payload["point_count"] = len(transaction_flow.points)
        return payload

    def _merge_transaction_order_flow(
        self,
        order_flow: Any,
        transaction_flow: TransactionFlowObservation,
    ) -> Any:
        """Blend L1 tape evidence into the detail-only flow score.

        The board keeps the inexpensive five-level snapshot score.  A detail
        request may additionally read a bounded transaction tape; blending it
        here gives the user more evidence without making a full-market scan
        issue thousands of transaction requests every refresh.
        """
        base_score = int(getattr(order_flow, "score", 0) or 0)
        tx_score = int(transaction_flow.score or 0)
        blended_score = int(max(-100, min(100, round(base_score * 0.65 + tx_score * 0.35))))
        if blended_score >= self.engine.order_flow_attack_score:
            direction = "买盘增强"
        elif blended_score <= self.engine.order_flow_pressure_score:
            direction = "卖盘增强"
        elif tx_score > 0:
            direction = "放量承接"
        elif tx_score < 0:
            direction = "放量抛压"
        else:
            direction = getattr(order_flow, "direction", "多空拉锯") or "多空拉锯"
        evidence = list(getattr(order_flow, "evidence", []) or [])
        evidence.extend(transaction_flow.evidence[:3])
        has_five_level = bool(getattr(order_flow, "available", False))
        return order_flow.model_copy(
            update={
                "available": bool(has_five_level or transaction_flow.available),
                "source": (
                    "easy_tdx_l1_five_level+transaction"
                    if has_five_level
                    else transaction_flow.source
                ),
                "data_quality": (
                    "l1_five_level_transaction"
                    if has_five_level
                    else "l1_transaction"
                ),
                "direction": direction,
                "score": blended_score,
                "confidence": (
                    "中等：五档 L1 + 逐笔成交方向代理"
                    if has_five_level
                    else "中等：逐笔成交方向代理"
                ),
                "evidence": list(dict.fromkeys(evidence)),
                "disclaimer": "五档/逐笔成交代理，不是队列数据或逐笔委托",
            }
        )

    @staticmethod
    def _analysis_point(point: ReplayPoint | ReplayMarker | None) -> dict[str, Any]:
        if point is None:
            return {}
        plan = getattr(point, "risk_reward", None)
        risk_reward = plan.model_dump(mode="json") if hasattr(plan, "model_dump") else {}
        return {
            "time": getattr(point, "time", ""),
            "price": getattr(point, "price", 0),
            "change_pct": getattr(point, "change_pct", 0),
            "vwap": getattr(point, "vwap", 0),
            "minute_amount_ratio": getattr(point, "minute_amount_ratio", 0),
            "flow_score": getattr(point, "flow_score", 0),
            "signal": getattr(getattr(point, "signal", ""), "value", getattr(point, "signal", "")),
            "phase": getattr(point, "phase", ""),
            "score": getattr(point, "score", getattr(point, "signal_score", 0)),
            "exit_score": getattr(point, "exit_score", 0),
            "direction": getattr(point, "direction", ""),
            "action": getattr(point, "action", ""),
            "setup": getattr(point, "setup", ""),
            "regime": getattr(point, "regime", ""),
            "executable": getattr(point, "executable", False),
            "execution_reason": getattr(point, "execution_reason", ""),
            "invalidation_price": getattr(point, "invalidation_price", 0),
            "risk_reward": {
                key: risk_reward.get(key)
                for key in (
                    "available",
                    "favorable",
                    "direction",
                    "action",
                    "entry_price",
                    "sell_price",
                    "buyback_price",
                    "support_price",
                    "invalidation_price",
                    "target_price",
                    "risk_pct",
                    "expected_reward_pct",
                    "reward_risk_ratio",
                    "min_required_ratio",
                    "status",
                )
                if key in risk_reward
            },
            "events": {
                "market": getattr(point, "market_event", ""),
                "sector": getattr(point, "sector_event", ""),
                "stock": getattr(point, "stock_event", ""),
                "flow": getattr(point, "flow_event", ""),
            },
            "reasons": list(getattr(point, "reasons", []) or [])[:4],
            "risks": list(getattr(point, "risks", []) or [])[:4],
            "evidence_sequence": list(getattr(point, "evidence_sequence", []) or [])[:6],
            "source_quality": getattr(point, "source_quality", ""),
        }

    def _analysis_minute_context(self, detail: SignalReplayDetail) -> dict[str, Any]:
        points = list(detail.replay_points or [])
        if not points:
            return {
                "point_count": 0,
                "note": "无分钟分时数据，AI只能解释当前快照、盘口和消息证据。",
            }

        high_point = max(points, key=lambda item: item.price)
        low_point = min(points, key=lambda item: item.price)
        max_volume_point = max(points, key=lambda item: item.minute_amount_ratio)
        strongest_flow_point = max(points, key=lambda item: item.flow_score)
        weakest_flow_point = min(points, key=lambda item: item.flow_score)
        latest = points[-1]
        latest_vwap = float(latest.vwap or 0)
        latest_price = float(latest.price or 0)
        vwap_distance_pct = (
            (latest_price - latest_vwap) / latest_vwap * 100
            if latest_price > 0 and latest_vwap > 0
            else 0
        )
        marker_times = {
            str(marker.time or "")[:5]
            for marker in list(detail.decision_markers or []) + list(detail.markers or [])
            if str(marker.time or "").strip()
        }
        selected: dict[str, ReplayPoint] = {}
        for point in [points[0], latest, high_point, low_point, max_volume_point, strongest_flow_point, weakest_flow_point]:
            selected[str(point.time)[:5]] = point
        for point in points:
            time_label = str(point.time or "")[:5]
            if time_label in marker_times:
                selected[time_label] = point
        timeline = sorted(selected.values(), key=lambda item: str(item.time or ""))[:20]
        return {
            "point_count": len(points),
            "start_time": points[0].time,
            "end_time": latest.time,
            "latest": self._analysis_point(latest),
            "high_point": self._analysis_point(high_point),
            "low_point": self._analysis_point(low_point),
            "max_volume_point": self._analysis_point(max_volume_point),
            "strongest_flow_point": self._analysis_point(strongest_flow_point),
            "weakest_flow_point": self._analysis_point(weakest_flow_point),
            "latest_vwap_distance_pct": round(vwap_distance_pct, 2),
            "key_timeline": [self._analysis_point(point) for point in timeline],
        }

    @staticmethod
    def _analysis_transaction_context(transaction: TransactionFlowObservation) -> dict[str, Any]:
        data = transaction.model_dump(mode="json")
        points = list(transaction.points or [])
        selected_points = []
        if points:
            strongest = max(points, key=lambda item: item.rolling_score)
            weakest = min(points, key=lambda item: item.rolling_score)
            largest = max(points, key=lambda item: max(item.large_buy_amount, item.large_sell_amount))
            latest = points[-1]
            seen_times: set[str] = set()
            for item in [strongest, weakest, largest, latest]:
                key = str(item.time or "")
                if key in seen_times:
                    continue
                seen_times.add(key)
                selected_points.append(item.model_dump(mode="json"))
        data["point_count"] = len(points)
        data["key_points"] = selected_points
        data.pop("points", None)
        return data

    @staticmethod
    def _analysis_message_context(bundle: MessageEvidenceBundle) -> dict[str, Any]:
        def compact(items: list[Any]) -> list[dict[str, Any]]:
            output = []
            for item in items[:8]:
                row = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                output.append({
                    "event_id": row.get("event_id"),
                    "title": row.get("event_title") or row.get("topic_title"),
                    "summary": (
                        row.get("display_text")
                        or row.get("media_summary")
                        or row.get("event_summary")
                        or row.get("topic_content")
                    ),
                    "direction": row.get("direction"),
                    "event_type": row.get("event_type"),
                    "confidence": row.get("confidence"),
                    "impact_strength": row.get("impact_strength"),
                    "create_time": row.get("create_time"),
                    "entity_type": row.get("entity_type"),
                    "code": row.get("code"),
                    "name": row.get("name"),
                    "role": row.get("role"),
                    "relevance": row.get("relevance"),
                    "impact": row.get("impact"),
                })
            return output

        return {
            "stock": compact(list(bundle.stock or [])),
            "sector": compact(list(bundle.sector or [])),
        }

    @staticmethod
    def _analysis_auction_context(detail: SignalReplayDetail) -> dict[str, Any]:
        history = list(detail.auction_history or [])
        latest = history[-1] if history else {}
        first = history[0] if history else {}
        signal_auction = detail.current_signal.auction.model_dump(mode="json")
        return {
            "current_signal_auction": signal_auction,
            "history_count": len(history),
            "first_snapshot": first,
            "latest_snapshot": latest,
            "key_snapshots": history[:3] + (history[-3:] if len(history) > 3 else []),
            "note": (
                "真实竞价缺失时不能伪造成未匹配委托；easy_tdx盘前快照只能作为低权重先验。"
            ),
        }

    def _analysis_auxiliary_context(self, code: str) -> dict[str, Any]:
        """Fetch optional detail panes for AI, but keep only compact summaries."""

        output: dict[str, Any] = {}

        def safe(label: str, fetcher: Callable[[], Any]) -> None:
            try:
                payload = fetcher()
            except Exception as exc:
                output[label] = {"available": False, "error": str(exc)}
                return
            if hasattr(payload, "model_dump"):
                row = payload.model_dump(mode="json")
            elif isinstance(payload, dict):
                row = dict(payload)
            else:
                output[label] = {"available": False, "error": "返回格式不可识别"}
                return
            if "sections" in row:
                sections = []
                for section in list(row.get("sections") or [])[:6]:
                    sections.append({
                        "title": section.get("title") or section.get("key"),
                        "available": section.get("available"),
                        "fields": list(section.get("fields") or [])[:6],
                        "row_count": section.get("row_count"),
                    })
                output[label] = {
                    "available": row.get("available"),
                    "source": row.get("source"),
                    "fetched_at": row.get("fetched_at"),
                    "section_count": row.get("section_count"),
                    "sections": sections,
                    "note": row.get("note"),
                }
            else:
                output[label] = {
                    "available": row.get("available"),
                    "source": row.get("source"),
                    "fetched_at": row.get("fetched_at"),
                    "summary": row.get("summary") or {},
                    "tables": [
                        {
                            "title": table.get("title"),
                            "columns": list(table.get("columns") or table.get("raw_columns") or [])[:8],
                            "sample_rows": list(table.get("rows") or [])[:3],
                            "row_count": table.get("row_count"),
                        }
                        for table in list(row.get("tables") or [])[:3]
                    ],
                    "note": row.get("note"),
                }

        safe("fundamentals", lambda: self.data_source.fetch_fundamentals(code))
        safe("capital_flow", lambda: self.data_source.fetch_capital_flow(code))
        safe("technical_indicators", lambda: self.data_source.fetch_technical_indicators(code))
        safe("chanlun", lambda: self.data_source.fetch_chanlun(code))
        return output

    def _analysis_source(self, detail: SignalReplayDetail) -> dict[str, Any]:
        decision_markers = [
            marker.model_dump(mode="json")
            for marker in (detail.decision_markers or [])
        ]
        latest_decision = decision_markers[-1] if decision_markers else {}
        compact_markers = [self._analysis_point(marker) for marker in (detail.decision_markers or [])]
        return {
            "source_version": "analysis_context_v2",
            "code": detail.code,
            "name": detail.name,
            "sector": detail.sector,
            "trade_date": detail.trade_date,
            "selected_sector": detail.selected_sector,
            "data_quality": {
                "minute_points": len(detail.replay_points or []),
                "decision_marker_count": len(detail.decision_markers or []),
                "has_order_flow": detail.order_flow.available,
                "has_transaction_flow": detail.transaction_flow.available,
                "has_message_evidence": bool(detail.message_evidence.stock or detail.message_evidence.sector),
                "has_position": detail.position is not None,
            },
            "market": detail.market.model_dump(mode="json"),
            "sector_snapshot": detail.sector_snapshot.model_dump(mode="json") if detail.sector_snapshot else None,
            "current_signal": detail.current_signal.model_dump(mode="json"),
            "summary": detail.summary,
            "minute_context": self._analysis_minute_context(detail),
            "markers": [self._analysis_point(marker) for marker in detail.markers[:8]],
            "signal_timeline": [self._analysis_point(marker) for marker in detail.signal_timeline[:24]],
            # Formula markers are the canonical action evidence.  AI
            # receives them as read-only context and is not allowed to create a
            # new point or change its action.
            "decision_markers": decision_markers,
            "canonical_action_points": compact_markers,
            "strategy_version": detail.current_signal.strategy_version,
            "research_evidence": {
                "direction": latest_decision.get("direction", detail.current_signal.direction),
                "action": latest_decision.get("action", detail.current_signal.action),
                "setup": latest_decision.get("setup", detail.current_signal.setup),
                "regime": latest_decision.get("regime", detail.current_signal.regime),
                "hypothesis_id": latest_decision.get("hypothesis_id", detail.current_signal.hypothesis_id),
                "validation_status": latest_decision.get(
                    "validation_status", detail.current_signal.validation_status
                ),
                "risk_reward": latest_decision.get(
                    "risk_reward", detail.current_signal.risk_reward.model_dump(mode="json")
                ),
            },
            "decision_basis": detail.current_signal.reasons,
            "risks": detail.current_signal.risks,
            "order_flow": detail.order_flow.model_dump(mode="json"),
            "transaction_flow": self._analysis_transaction_context(detail.transaction_flow),
            "opening_auction": self._analysis_auction_context(detail),
            "position": detail.position.model_dump(mode="json") if detail.position else None,
            "message_evidence": self._analysis_message_context(detail.message_evidence),
            "auxiliary_context": self._analysis_auxiliary_context(detail.code),
            "analysis_rules": [
                "AI只能解释结构化证据，不得发明新的买卖点。",
                "真正画在分时图上的点以 canonical_action_points / decision_markers 为准。",
                "easy_tdx逐笔成交是L1 transaction tape，不是委托队列或隐藏主力单。",
                "没有真实竞价时，不得把开盘价或五档快照当成真实未匹配委托。",
                "正T/反T必须结合持仓可卖数量和T+1约束描述可执行性。",
            ],
        }

    def _lock_analysis_decision(
        self,
        detail: SignalReplayDetail,
        ai_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Keep AI explanatory while action points stay engine-owned."""

        result = dict(ai_result or {})
        markers = list(detail.decision_markers or [])
        buy_actions = {"buy_t", "buyback"}
        sell_actions = {"sell_old", "sell_base", "risk_rebuy"}

        def action_point(marker: ReplayMarker) -> dict[str, Any]:
            return {
                "time": marker.time,
                "action": marker.action,
                "reason": " / ".join(marker.reasons),
                "executable": marker.executable,
                "risk": " / ".join(marker.risks[:3]),
            }

        buy_points = [
            action_point(marker)
            for marker in markers
            if marker.action in buy_actions
        ]
        sell_points = [
            action_point(marker)
            for marker in markers
            if marker.action in sell_actions
        ]
        if not isinstance(result.get("decision_basis"), list):
            result["decision_basis"] = list(detail.current_signal.reasons)
        if not isinstance(result.get("risk"), list):
            result["risk"] = list(detail.current_signal.risks)
        if not isinstance(result.get("watch_items"), list):
            result["watch_items"] = [
                "指数是否继续放量上攻或持续新低",
                "板块核心票是否继续主动进攻/回封",
                "个股逐笔成交方向与价格响应是否一致",
                "失效价/VWAP/局部支撑是否被有效跌破",
            ]
        direction_map = {
            TradeDirection.POSITIVE_T.value: "正T",
            TradeDirection.REVERSE_T.value: "反T",
            TradeDirection.NONE.value: "观察",
        }
        market_event = getattr(detail.current_signal, "market_event", "")
        sector_event = getattr(detail.current_signal, "sector_event", "")
        stock_event = getattr(detail.current_signal, "stock_event", "")
        flow_event = getattr(detail.current_signal, "flow_event", "")
        result["decision"] = detail.current_signal.signal.value
        result.setdefault("t_direction", direction_map.get(detail.current_signal.direction, "观察"))
        result.setdefault("confidence", detail.current_signal.score)
        result.setdefault("confidence_reason", detail.current_signal.execution_reason or "做T公式信号需结合盘面确认")
        result.setdefault("market_read", market_event or detail.market.trend)
        result.setdefault(
            "sector_read",
            sector_event
            or (detail.sector_snapshot.reasons[0] if detail.sector_snapshot and detail.sector_snapshot.reasons else detail.sector),
        )
        result.setdefault("stock_read", stock_event or "结合分时结构、VWAP和局部高低点判断")
        result.setdefault("tape_read", flow_event or "结合五档/逐笔成交代理观察买卖力量")
        result.setdefault("opening_read", detail.current_signal.auction.status or "无真实竞价增量")
        result.setdefault("message_read", "；".join(self._message_basis(detail)) or "暂无同步消息证据")
        result.setdefault(
            "position_read",
            (
                f"持仓{detail.position.quantity}，可卖{detail.position.available_quantity}，T仓比例{detail.position.t_allocation_pct}%"
                if detail.position else
                "未录入本地持仓，卖T可执行性只能按波段观察描述"
            ),
        )
        result.setdefault(
            "invalidation",
            (
                f"跌破/突破失效价 {detail.current_signal.invalidation_price:.2f} 后判断失效"
                if detail.current_signal.invalidation_price > 0 else
                "市场、板块或逐笔成交方向与当前判断反向共振时失效"
            ),
        )
        result.setdefault("next_action", self._fallback_analysis(detail)["next_action"])
        result["buy_points"] = buy_points
        result["sell_points"] = sell_points
        result["canonical_decision_markers"] = [marker.model_dump(mode="json") for marker in markers]
        result["strategy_version"] = detail.current_signal.strategy_version
        result["decision_source"] = "zuot_tdx_levels"
        result["ai_role"] = "解释结构化证据；不生成或修改买卖点"
        return result

    def _fallback_analysis(self, detail: SignalReplayDetail) -> dict[str, Any]:
        signal = detail.current_signal.signal.value
        if signal == SignalType.BUY_T.value:
            next_action = "沿着强板块低吸，盯住量能是否继续放大。"
        elif signal == SignalType.SELL_T.value:
            next_action = "优先减仓，确认兑现后等待回落再看。"
        else:
            next_action = "继续观察，等指数和板块都完成拐头。"
        message_basis = self._message_basis(detail)
        markers = detail.decision_markers or detail.signal_timeline
        market_event = getattr(detail.current_signal, "market_event", "")
        sector_event = getattr(detail.current_signal, "sector_event", "")
        stock_event = getattr(detail.current_signal, "stock_event", "")
        flow_event = getattr(detail.current_signal, "flow_event", "")
        return {
            "summary": f"{detail.name} 当前为 {signal}。",
            "decision": signal,
            "t_direction": {
                TradeDirection.POSITIVE_T.value: "正T",
                TradeDirection.REVERSE_T.value: "反T",
                TradeDirection.NONE.value: "观察",
            }.get(detail.current_signal.direction, "观察"),
            "decision_basis": detail.current_signal.reasons + detail.summary + message_basis,
            "buy_points": [
                {
                    "time": marker.time,
                    "action": marker.action,
                    "reason": " / ".join(marker.reasons),
                    "executable": marker.executable,
                    "risk": " / ".join(marker.risks[:3]),
                }
                for marker in markers
                if marker.action in {"buy_t", "buyback"} or marker.signal == SignalType.BUY_T
            ],
            "sell_points": [
                {
                    "time": marker.time,
                    "action": marker.action,
                    "reason": " / ".join(marker.reasons),
                    "executable": marker.executable,
                    "risk": " / ".join(marker.risks[:3]),
                }
                for marker in markers
                if marker.action in {"sell_old", "sell_base", "risk_rebuy"} or marker.signal == SignalType.SELL_T
            ],
            "risk": detail.current_signal.risks,
            "market_read": market_event or detail.market.trend,
            "sector_read": sector_event or detail.sector,
            "stock_read": stock_event or "等待个股结构更清晰",
            "tape_read": flow_event or detail.order_flow.direction,
            "opening_read": detail.current_signal.auction.status or "无真实竞价增量",
            "message_read": "；".join(message_basis) or "暂无同步消息证据",
            "position_read": (
                f"持仓{detail.position.quantity}，可卖{detail.position.available_quantity}，T仓比例{detail.position.t_allocation_pct}%"
                if detail.position else
                "未录入本地持仓"
            ),
            "invalidation": (
                f"跌破/突破失效价 {detail.current_signal.invalidation_price:.2f} 后判断失效"
                if detail.current_signal.invalidation_price > 0 else
                "市场、板块或逐笔成交方向反向共振时失效"
            ),
            "next_action": next_action,
            "confidence": detail.current_signal.score,
            "confidence_reason": detail.current_signal.execution_reason or "做T公式信号需结合盘面确认",
            "watch_items": [
                "指数放量方向",
                "板块核心票强弱",
                "个股逐笔成交方向与价格响应",
                "VWAP/失效价是否被破坏",
            ],
            "message_evidence": message_basis,
            "canonical_decision_markers": [marker.model_dump(mode="json") for marker in detail.decision_markers],
            "strategy_version": detail.current_signal.strategy_version,
            "decision_source": "zuot_tdx_levels",
            "ai_role": "解释结构化证据；不生成或修改买卖点",
        }

    def _message_evidence_terms(
        self,
        quote: Quote,
        signal: TradeSignal,
        sector_snapshot: SectorSnapshot | None,
        selected_sector: str | None,
    ) -> list[str]:
        code = str(quote.code).zfill(6)
        easy_tdx_terms = [
            self._stock_board_display_map_for_level(level).get(code)
            for level in (3, 2, 1)
        ]
        display_sector = self._display_sector_name(quote, sector_snapshot)
        focused_terms = [
            display_sector,
            *easy_tdx_terms,
            selected_sector,
            signal.sector,
            sector_snapshot.name if sector_snapshot else None,
        ]
        terms = focused_terms if any(str(item or "").strip() for item in focused_terms) else list(quote.themes or [])
        output: list[str] = []
        seen: set[str] = set()
        for item in terms:
            value = str(item or "").strip()
            if value and value not in seen:
                seen.add(value)
                output.append(value)
        return output

    def _message_basis(self, detail: SignalReplayDetail) -> list[str]:
        bundle = detail.message_evidence
        evidence = [*(bundle.stock or []), *(bundle.sector or [])]
        basis: list[str] = []
        for item in evidence[:5]:
            title = item.event_title or item.topic_title
            if not title:
                continue
            descriptor = item.event_type or item.direction or item.entity_type
            summary = self._message_evidence_text(item, limit=180)
            if descriptor:
                prefix = f"消息面：{title}（{descriptor}）"
            else:
                prefix = f"消息面：{title}"
            basis.append(f"{prefix}：{summary}" if summary else prefix)
        return basis

    @staticmethod
    def _message_evidence_text(item: Any, limit: int = 180) -> str:
        text = (
            getattr(item, "display_text", "")
            or getattr(item, "media_summary", "")
            or getattr(item, "event_summary", "")
            or getattr(item, "topic_content", "")
        )
        text = "；".join(part.strip() for part in str(text or "").splitlines() if part.strip())
        return text[: max(20, int(limit))]

    def _normalize_sector(self, sector: str | None) -> str | None:
        if not sector:
            return None
        value = str(sector).strip()
        return value or None

    def _sector_codes(self, quotes: list[Quote], sector: str | None) -> set[str]:
        if not sector:
            return set()
        return {quote.code for quote in quotes if sector in quote.themes}

    def _quote_for_code(self, quotes: list[Quote], code: str) -> Quote | None:
        return next((quote for quote in quotes if quote.code == code), None)

    def _index_for_code(self, indices: list[IndexSnapshot], code: str) -> IndexSnapshot | None:
        normalized = str(code).strip()
        return next((index for index in indices if index.code == normalized), None)

    def _indices_for_minute_series(self, context: DashboardContext) -> list[IndexSnapshot]:
        indices = list(context.market.indices or context.snapshot.indices or [])
        if indices:
            return indices
        return [
            IndexSnapshot(
                code=code,
                name=name,
                price=0.0,
                prev_close=0.0,
                open=0.0,
                high=0.0,
                low=0.0,
                change_pct=0.0,
                rebound_from_low_pct=0.0,
                minute_amount_ratio=1.0,
                amount=0.0,
            )
            for code, name in self._DEFAULT_INDEX_MINUTE_SERIES
        ]

    @staticmethod
    def _index_prev_close_for_minutes(index: IndexSnapshot, rows: list[dict[str, Any]]) -> float:
        candidates: list[Any] = [
            index.prev_close,
            rows[0].get("prev_close") if rows else None,
            rows[0].get("pre_close") if rows else None,
            rows[0].get("preClose") if rows else None,
        ]
        for candidate in candidates:
            try:
                value = float(candidate or 0)
            except (TypeError, ValueError):
                continue
            if isfinite(value) and value > 0:
                return value
        for row in rows:
            try:
                value = float(row.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if isfinite(value) and value > 0:
                return value
        return 0.0

    def _signal_for_code(self, signals: list[TradeSignal], code: str) -> TradeSignal | None:
        return next((signal for signal in signals if signal.code == code), None)

    def _watchlist_item_for_code(self, watchlist: list[WatchlistItem], code: str) -> WatchlistItem | None:
        return next((item for item in watchlist if item.code == code), None)

    def _best_sector_for_quote(
        self,
        quote: Quote,
        sectors: list[SectorSnapshot],
        preferred_sector_names: set[str] | None = None,
        requested_sector: str | None = None,
    ) -> SectorSnapshot | None:
        candidates = [sector for sector in sectors if sector.name in quote.themes]
        if not candidates:
            return None
        if requested_sector:
            requested = next((sector for sector in candidates if sector.name == requested_sector), None)
            if requested is not None:
                return requested
        if preferred_sector_names:
            preferred = [sector for sector in candidates if sector.name in preferred_sector_names]
            if preferred:
                return max(preferred, key=lambda sector: sector.heat_score)
        return max(candidates, key=lambda sector: sector.heat_score)

    @staticmethod
    def _manual_theme_names(themes: list[dict]) -> set[str]:
        return {
            str(theme.get("name") or "").strip()
            for theme in themes
            if str(theme.get("name") or "").strip()
        }

    @staticmethod
    def _is_internal_theme_code(value: str | None) -> bool:
        """内部行业代码：单字母 + 4~6 位数字（X430201 / X3006 / T0602 等）。"""
        text = str(value or "").strip()
        return 5 <= len(text) <= 7 and text[0].isalpha() and text[1:].isdigit()

    @classmethod
    def _visible_theme_names(cls, themes: list[str]) -> list[str]:
        return [
            str(theme).strip()
            for theme in themes
            if str(theme).strip() and not cls._is_internal_theme_code(str(theme))
        ]

    def _display_sector_name(self, quote: Quote, sector: SectorSnapshot | None = None) -> str:
        sector_name = str(sector.name if sector else "").strip()
        if sector_name and not self._is_internal_theme_code(sector_name):
            return sector_name
        # 内部行业代码：优先映射官方板块（申万三级）名称，再退可见主题名
        mapped = self._stock_board_display_map().get(str(quote.code).zfill(6))
        if mapped:
            return mapped
        visible_themes = self._visible_theme_names(list(quote.themes))
        return visible_themes[0] if visible_themes else "未归类"

    def _index_replay_points(self, index: IndexSnapshot, rows: list[dict]) -> list[ReplayPoint]:
        if not rows:
            return []

        fallback_times = self.engine._session_times(len(rows))
        times = [
            str(row.get("time") or fallback_times[idx] if idx < len(fallback_times) else "")[:5]
            for idx, row in enumerate(rows)
        ]
        prices: list[float] = []
        prev_close = self._index_prev_close_for_minutes(index, rows)
        fallback = prev_close if prev_close > 0 else index.price
        for row in rows:
            price = float(row.get("price") or fallback or 0)
            if not isfinite(price) or price <= 0:
                price = prices[-1] if prices else fallback
            prices.append(price)

        amounts = [max(float(row.get("vol") or 0), 0) * price for row, price in zip(rows, prices)]
        avg_amount = sum(amounts) / len(amounts) if amounts else 1
        if avg_amount <= 0:
            avg_amount = 1

        running_low = prices[0]
        running_high = prices[0]
        replay_points: list[ReplayPoint] = []
        for time_label, row, price, amount in zip(times, rows, prices, amounts):
            volume = max(float(row.get("vol") or 0), 0)
            running_low = min(running_low, price)
            running_high = max(running_high, price)
            change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
            rebound = (price - running_low) / running_low * 100 if running_low else 0
            pullback = (running_high - price) / running_high * 100 if running_high else 0
            replay_points.append(
                ReplayPoint(
                    time=time_label,
                    price=round(price, 2),
                    change_pct=round(change_pct, 2),
                    rebound_from_low_pct=round(rebound, 2),
                    pullback_from_high_pct=round(pullback, 2),
                    volume=volume,
                    minute_amount_ratio=round(amount / avg_amount, 2) if avg_amount else 1,
                    signal=SignalType.WATCH,
                    reasons=[],
                ),
            )
        return replay_points

    def _index_replay_summary(self, index: IndexSnapshot, points: list[ReplayPoint]) -> list[str]:
        summary = [
            "大盘盘口分时，不生成个股买卖信号",
            f"当前涨幅 {index.change_pct:+.2f}%",
            f"低位反弹 {index.rebound_from_low_pct:.2f}%",
        ]
        if not points:
            summary.append("暂无大盘分钟回放数据")
            return summary

        prices = [point.price for point in points]
        summary.extend(
            [
                f"分钟点 {len(points)} 个",
                f"区间 {min(prices):.2f} - {max(prices):.2f}",
            ],
        )
        latest = points[-1]
        if latest.minute_amount_ratio >= 1.08:
            summary.append(f"当前量能 {latest.minute_amount_ratio:.1f}倍")
        return summary

    def _invalidate_context(self) -> None:
        """标记上下文过期，不等待 _context_lock、不清空缓存对象。

        历史实现用阻塞方式抢 _context_lock 并把 _context_cache 置 None：
        盘中后台全量刷新线程几乎一直持有该锁（一轮全市场刷新数秒），
        导致加自选/删自选等写操作随机卡顿数秒，且下一次 _get_context 会
        走阻塞式全量重建。现在只打过期间隔标记，读请求继续用旧快照快速
        响应，后台刷新线程下一轮重建后自然拿到最新数据。小缓存容器用
        原子替换而不是 clear()，避免与读线程并发迭代冲突。
        """
        self._context_cache_at = 0.0
        self._context_cache_bucket = ""
        self._sector_flow_cache = None
        self._sector_flow_cache_at = 0.0
        self._sector_flow_cache_key = ""
        self._sector_flow_names = []
        self._sector_flow_names_key = ""
        self._sector_flow_cache_by_key = {}
        self._sector_flow_names_by_key = {}
        self._clear_payload_caches()
        self._fast_board_entries_cache = {}
        self._visible_quote_cache = {}
        self._visible_quote_refresh_started_at_by_key = {}
        self._visible_quote_refresh_errors_by_key = {}
        self._terminal_warmup_signature = ""
        self._historical_context_cache = {}
        self._signal_detail_context_cache = {}
        self._signal_detail_context_build_locks = {}
        self._detail_minute_rows_cache = {}
        self._detail_minute_rows_build_locks = {}
        self._signal_detail_context_cache = {}
        self._signal_detail_context_build_locks = {}
        self._detail_minute_rows_cache = {}
        self._detail_minute_rows_build_locks = {}

    def _clear_payload_caches(self) -> None:
        """清空共享载荷缓存（terminal/dashboard）。

        原子替换容器而不是 clear()，避免与持锁读线程并发迭代冲突；构建锁字典
        一并替换，正在构建的线程仍持有旧锁引用，完成后写入新容器即可。
        """
        with self._terminal_cache_lock:
            self._terminal_cache_by_key = {}
            self._terminal_build_locks = {}
        with self._dashboard_cache_lock:
            self._dashboard_cache_by_key = {}
            self._dashboard_build_locks = {}
