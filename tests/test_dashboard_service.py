import threading
import time
from types import SimpleNamespace

from app.config import AppSettings
from app.data_sources import MarketSnapshot
from app.models import (
    EventItem,
    FundamentalField,
    FundamentalPayload,
    FundamentalSection,
    FundamentalTable,
    IndexSnapshot,
    MarketState,
    MessageEvent,
    MessageEventLink,
    MessageTopic,
    MiniIntradaySeries,
    OrderFlowObservation,
    PositionRecord,
    DetailDataPayload,
    Quote,
    ReplayMarker,
    SectorFlowSeries,
    SectorSnapshot,
    SignalPhase,
    SignalReplayDetail,
    SignalType,
    StockBoardItem,
    TradeSignal,
    TransactionFlowObservation,
    TrendState,
    WatchlistItem,
    ZsxqMessageIngestRequest,
)
from app.message_store import MessageStore
from app.services import DashboardContext, DashboardService
from app.storage import AnalysisStore
from app.trajectory_store import IntradayWatchtowerStore


class MemoryWatchlistStore:
    def __init__(self, items=None) -> None:
        self.items = list(items or [])

    def list_items(self):
        return list(self.items)

    def upsert(self, item):
        self.items = [existing for existing in self.items if existing.code != item.code] + [item]
        return item

    def delete(self, code):
        before = len(self.items)
        self.items = [item for item in self.items if item.code != code]
        return len(self.items) != before


class MemoryThemeStore:
    def list_themes(self):
        return []


class MemoryStateStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def get_json(self, namespace: str, key: str, default=None):
        return self.values.get((namespace, key), default)

    def set_json(self, namespace: str, key: str, value) -> None:
        self.values[(namespace, key)] = value


class FakeAIClient:
    available = True

    def analyze(self, source):
        assert source["code"] == "300476"
        assert source["selected_sector"] == "PCB"
        assert source["source_version"] == "analysis_context_v2"
        assert "minute_context" in source
        assert "transaction_flow" in source
        assert "opening_auction" in source
        assert "auxiliary_context" in source
        assert "canonical_action_points" in source
        assert "message_evidence" in source
        return {
            "generated_at": "2026-08-06T10:30:00",
            "provider": "fake",
            "model": "unit-model",
            "status": "ok",
            "result": {
                "summary": "PCB共振后低位拐头，适合小仓买T。",
                "decision": "买T",
                "decision_basis": ["指数拐头", "PCB强确认", "分钟量能放大"],
                "buy_points": [{"time": "09:44", "reason": "低位拐头且量能放大"}],
                "sell_points": [],
                "risk": ["反弹后量能衰减"],
                "next_action": "盯住核心票量能，冲高放缓则减T。",
                "confidence": 88,
            },
            "raw_text": "{}",
        }


def make_service(tmp_path, max_signals_per_group=10):
    settings = AppSettings()
    settings.max_signals_per_group = max_signals_per_group
    return DashboardService(
        settings,
        watchlist_store=MemoryWatchlistStore(),
        theme_store=MemoryThemeStore(),
        analysis_store=AnalysisStore(tmp_path),
        ai_client=FakeAIClient(),
        message_store=MessageStore(tmp_path / "messages.sqlite"),
        trajectory_store=IntradayWatchtowerStore(tmp_path / "intraday.sqlite"),
    )


def test_trajectory_cleanup_once_per_day_is_throttled(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    service.settings.trajectory_retention_trade_days = 3
    calls = []

    def cleanup(*, retain_trade_days, truncate_wal):
        calls.append((retain_trade_days, truncate_wal))
        return {"deleted_rows": len(calls)}

    monkeypatch.setattr(service.trajectory_store, "cleanup_high_frequency_history", cleanup)

    first = service.cleanup_trajectory_history_once_per_day(day_key="20260812")
    second = service.cleanup_trajectory_history_once_per_day(day_key="20260812")
    third = service.cleanup_trajectory_history_once_per_day(day_key="20260813")

    assert first["deleted_rows"] == 1
    assert second["skipped"] == "already_cleaned_today"
    assert third["deleted_rows"] == 2
    assert calls == [(3, True), (3, True)]


def test_trajectory_cleanup_background_skips_when_existing_run_is_active(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr("app.services.is_trading_window", lambda: False)

    def cleanup(*, retain_trade_days, truncate_wal):
        calls.append((retain_trade_days, truncate_wal))
        started.set()
        assert release.wait(2)
        return {"deleted_rows": 7}

    monkeypatch.setattr(service.trajectory_store, "cleanup_high_frequency_history", cleanup)

    first = service.start_trajectory_cleanup_thread(reason="startup")
    assert first["scheduled"] is True
    assert started.wait(2)
    second = service.start_trajectory_cleanup_thread(reason="startup")
    release.set()
    service._trajectory_cleanup_thread.join(2)

    assert second["skipped"] == "cleanup_in_progress"
    assert service._last_trajectory_cleanup["deleted_rows"] == 7
    assert len(calls) == 1


def test_trajectory_cleanup_background_skips_during_trading_window(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)

    result = service.start_trajectory_cleanup_thread(reason="startup")

    assert result["scheduled"] is False
    assert result["skipped"] == "trading_window"


def test_stock_mini_chart_uses_regular_session_not_after_close_tail(tmp_path) -> None:
    service = make_service(tmp_path)
    rows = [
        {
            "captured_at": f"2026-08-07 09:{30 + index:02d}:15",
            "price": 10 + index * 0.01,
            "prev_close": 10,
            "change_pct": index * 0.1,
            "amount": 1_000_000 + index * 10_000,
            "minute_amount_ratio": 1 + index * 0.01,
        }
        for index in range(30)
    ]
    rows.extend(
        {
            "captured_at": f"2026-08-07 15:{1 + index:02d}:00",
            "price": 10.29,
            "prev_close": 10,
            "change_pct": 2.9,
            "amount": 1_290_000,
            "minute_amount_ratio": 1.2,
        }
        for index in range(8)
    )

    chart = service._mini_chart_from_stock_rows(rows)

    assert chart.times[0] == "09:30"
    assert chart.times[-1] == "09:59"
    assert chart.point_count == 30
    assert len(set(chart.price_pcts)) == 30


def test_stock_mini_chart_preserves_early_limit_up_ramp(tmp_path) -> None:
    service = make_service(tmp_path)
    rows = []
    for index in range(30):
        change_pct = min(10.0, index * 0.55)
        rows.append(
            {
                "captured_at": f"2026-08-07 09:{30 + index:02d}:15",
                "price": round(10 * (1 + change_pct / 100), 3),
                "prev_close": 10,
                "change_pct": change_pct,
                "amount": 1_000_000 + index * 40_000,
                "minute_amount_ratio": 1 + index * 0.02,
            }
        )
    for index in range(90):
        rows.append(
            {
                "captured_at": f"2026-08-07 13:{index:02d}:15" if index < 60 else f"2026-08-07 14:{index - 60:02d}:15",
                "price": 11.0,
                "prev_close": 10,
                "change_pct": 10.0,
                "amount": 2_500_000,
                "minute_amount_ratio": 2.0,
            }
        )

    chart = service._mini_chart_from_stock_rows(rows)

    assert chart.times[0] == "09:30"
    assert chart.times[-1] == "14:29"
    assert chart.point_count <= 48
    assert max(chart.price_pcts) == 10.0
    assert any(time < "10:00" and pct >= 5 for time, pct in zip(chart.times, chart.price_pcts))
    assert len(set(chart.price_pcts)) > 1
    assert chart.price_pcts.count(10.0) <= 2


def test_mini_chart_adds_lightweight_current_signal_marker(tmp_path) -> None:
    service = make_service(tmp_path)
    item = StockBoardItem(
        code="600667",
        name="太极实业",
        signal=SignalType.BUY_T,
        price=21.2,
        change_pct=10.02,
        signal_time="09:45",
        signal_grade="金色共振买T",
        factor_flags=["公式买入原语", "金色共振"],
    )
    chart = MiniIntradaySeries(
        times=["09:30", "09:45"],
        price_pcts=[0.0, 10.02],
        vwap_pcts=[0.0, 5.0],
        volume_ratios=[1.0, 2.0],
        point_count=2,
    )

    marked = service._mini_chart_with_board_marker(item, chart)

    assert marked.markers
    assert marked.markers[0].time == "09:45"
    assert marked.markers[0].signal == SignalType.BUY_T
    assert marked.markers[0].gold_resonance is True


def test_formula_rows_for_context_prefers_watchlist_and_positions(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    watchlist = [WatchlistItem(code="300308", name="中际旭创", themes=["CPO"], core=True)]
    positions = {
        "000001": PositionRecord(code="000001", name="平安银行", quantity=100, available_quantity=100)
    }
    rows_by_code = {
        "300476": [
            {"captured_at": "09:30", "price": 10.0, "change_pct": 1.0, "amount": 1_000_000, "minute_amount_ratio": 1.5},
            {"captured_at": "09:31", "price": 10.2, "change_pct": 2.0, "amount": 1_100_000, "minute_amount_ratio": 1.6},
        ],
        "300308": [
            {"captured_at": "09:30", "price": 20.0, "change_pct": 0.5, "amount": 900_000, "minute_amount_ratio": 1.2},
            {"captured_at": "09:31", "price": 20.1, "change_pct": 1.0, "amount": 950_000, "minute_amount_ratio": 1.3},
        ],
        "000001": [
            {"captured_at": "09:30", "price": 12.0, "change_pct": -0.2, "amount": 800_000, "minute_amount_ratio": 0.9},
            {"captured_at": "09:31", "price": 12.1, "change_pct": 0.1, "amount": 820_000, "minute_amount_ratio": 1.0},
        ],
    }
    batch_calls: list[tuple[str, tuple[str, ...], int]] = []

    def fake_batch_series(trade_date, codes, max_rows=180):
        batch_calls.append((trade_date, tuple(codes), max_rows))
        return {code: rows_by_code.get(code, []) for code in codes}

    monkeypatch.setattr(service.trajectory_store, "stock_feature_series_by_code", fake_batch_series)

    rows = service._formula_rows_by_code_for_context(
        trade_date="20260807",
        quotes=[
            quote("300476", "胜宏科技", ["PCB"]),
            quote("300308", "中际旭创", ["CPO"]),
        ],
        watchlist=watchlist,
        positions=positions,
    )

    assert batch_calls == [("20260807", ("300308", "000001", "300476"), 180)]
    assert set(rows) == {"300476", "300308", "000001"}
    assert all(len(series) == 2 for series in rows.values())


def test_formula_rows_for_context_falls_back_to_single_series(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    rows_by_code = {
        "300476": [
            {"captured_at": "09:30", "price": 10.0, "change_pct": 1.0, "amount": 1_000_000, "minute_amount_ratio": 1.5},
            {"captured_at": "09:31", "price": 10.2, "change_pct": 2.0, "amount": 1_100_000, "minute_amount_ratio": 1.6},
        ]
    }
    calls: list[tuple[str, str, int]] = []

    def fake_series(trade_date, code, max_rows=720):
        calls.append((trade_date, code, max_rows))
        return rows_by_code.get(code, [])

    monkeypatch.setattr(service.trajectory_store, "stock_feature_series_by_code", None)
    monkeypatch.setattr(service.trajectory_store, "stock_feature_series", fake_series)

    rows = service._formula_rows_by_code_for_context(
        trade_date="20260807",
        quotes=[quote("300476", "胜宏科技", ["PCB"])],
        watchlist=[],
        positions={},
    )

    assert calls == [("20260807", "300476", 180)]
    assert set(rows) == {"300476"}


def quote(code: str, name: str, themes: list[str]) -> Quote:
    return Quote(
        code=code,
        name=name,
        themes=themes,
        price=10.5,
        prev_close=10,
        open=10,
        high=10.8,
        low=9.8,
        day_high=10.8,
        day_low=9.8,
        change_pct=5,
        volume=1_000_000,
        amount=100_000_000,
        minute_amount=3_000_000,
        minute_amount_ratio=2.0,
        updated_at="10:10:00",
    )


def sector(name: str, leader_code: str) -> SectorSnapshot:
    return SectorSnapshot(
        name=name,
        heat_score=88,
        avg_change_pct=3.2,
        up_count=2,
        total_count=3,
        limit_up_count=1,
        opened_limit_count=0,
        core_attack=True,
        core_codes=[leader_code],
        leader_code=leader_code,
        leader_name="核心票",
        reasons=[f"{name}强确认"],
    )


def signal(
    code: str,
    name: str,
    sector_name: str,
    signal_type: SignalType,
    score: int,
    pinned: bool = False,
) -> TradeSignal:
    return TradeSignal(
        code=code,
        name=name,
        signal=signal_type,
        score=score,
        sector=sector_name,
        price=10.5,
        change_pct=5,
        rebound_from_low_pct=7,
        minute_amount_ratio=2,
        reasons=[f"{sector_name}强度88分"],
        risks=[],
        updated_at="10:10:00",
        pinned=pinned,
        watchlist_tags=["持仓"] if pinned else [],
    )


def market() -> MarketState:
    return MarketState(
        trend=TrendState.TURNING_UP,
        emotion_score=72,
        breadth_pct=56.5,
        index_turning=True,
        amount_expanding=True,
        mainline="PCB",
        indices=[],
        reasons=["指数从日内低位拐头"],
        updated_at="10:10:00",
    )


def test_intraday_context_does_not_serve_cached_close_snapshot(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)

    closed_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "easy_tdx_daily_close", "frozen": True},
        ),
        market=market().model_copy(update={"frozen": True, "updated_at": "15:00:00"}),
        sectors=[],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "easy_tdx_daily_close", "frozen": True},
    )
    live_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[],
            indices=[],
            data_mode="live",
            source_status={
                "active_source": "easy_tdx",
                "clock_label": "12:15:00",
                "frozen": False,
                "market_session": "lunch_break",
            },
        ),
        market=market().model_copy(update={"frozen": False, "updated_at": "12:15:00"}),
        sectors=[],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "easy_tdx", "market_session": "lunch_break"},
    )
    calls = {"refresh": 0, "background": 0}

    def fake_refresh() -> DashboardContext:
        calls["refresh"] += 1
        service._context_cache = live_context
        return live_context

    def fake_background() -> None:
        calls["background"] += 1

    service._context_cache = closed_context
    service._context_cache_at = 10**12
    service._context_cache_bucket = service._context_bucket()
    monkeypatch.setattr(service, "_refresh_context", fake_refresh)
    monkeypatch.setattr(service, "_ensure_background_context_refresh", fake_background)

    result = service._get_context()

    assert result is live_context
    assert calls == {"refresh": 1, "background": 0}


def test_context_bootstraps_from_local_trajectory_before_live_fetch(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    service.trajectory_store.record_context(
        trade_date="20260807",
        captured_at="10:10:00",
        updated_at="10:10:00",
        frozen=False,
        source_quality="live_l1_five_level_proxy",
        market=market(),
        sectors=[sector("PCB", "300476")],
        quotes=[quote("300476", "胜宏科技", ["PCB"])],
        signals=[],
        priority_codes=["300476"],
    )
    calls = {"fetch": 0, "background": 0}

    def fail_fetch(watchlist, themes):  # noqa: ARG001
        calls["fetch"] += 1
        raise AssertionError("cold bootstrap should not block on live fetch")

    service.data_source.fetch = fail_fetch
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service, "_ensure_background_context_refresh", lambda: calls.__setitem__("background", calls["background"] + 1))

    context = service._get_context()

    assert calls == {"fetch": 0, "background": 1}
    assert context.source_status["active_source"] == "local_trajectory_bootstrap"
    assert context.source_status["bootstrap"] is True
    assert context.source_status["quote_count"] == 1
    assert context.snapshot.quotes[0].code == "300476"
    assert context.signals_all


def test_get_context_returns_stale_cache_when_refresh_lock_is_busy(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    stale_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote("300476", "胜宏科技", ["PCB"])],
            indices=[],
            data_mode="live",
            source_status={"active_source": "easy_tdx", "trade_date": "20260807", "clock_label": "10:00:00"},
        ),
        market=market().model_copy(update={"frozen": False, "updated_at": "10:00:00"}),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "easy_tdx", "trade_date": "20260807", "clock_label": "10:00:00"},
    )
    service._context_cache = stale_context
    service._context_cache_at = 0
    service._context_cache_bucket = service._context_bucket()
    service._refresh_in_progress_lock.acquire()
    monkeypatch.setattr("app.services.is_trading_window", lambda: False)
    monkeypatch.setattr(service, "_refresh_context", lambda: (_ for _ in ()).throw(AssertionError("must not block on refresh")))

    try:
        context = service._get_context()
    finally:
        service._refresh_in_progress_lock.release()

    assert context is stale_context
    assert context.source_status["clock_label"] == "10:00:00"


def test_refresh_context_keeps_existing_cache_when_snapshot_unavailable(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    stale_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote("300476", "胜宏科技", ["PCB"])],
            indices=[],
            data_mode="live",
            source_status={"active_source": "easy_tdx", "trade_date": "20260807", "clock_label": "10:00:00"},
        ),
        market=market().model_copy(update={"frozen": False, "updated_at": "10:00:00"}),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "easy_tdx", "trade_date": "20260807", "clock_label": "10:00:00"},
    )
    service._context_cache = stale_context
    service._context_cache_at = 0
    service._context_cache_bucket = service._context_bucket()
    unavailable = MarketSnapshot(
        quotes=[],
        indices=[],
        data_mode="unavailable",
        source_status={
            "active_source": "unavailable",
            "frozen": True,
            "clock_label": "--",
            "note": "easy_tdx unavailable",
        },
    )
    monkeypatch.setattr(service.data_source, "fetch", lambda watchlist, themes: unavailable)
    monkeypatch.setattr(service, "_ensure_terminal_warmup", lambda context: None)

    context = service._refresh_context()

    assert context is stale_context
    assert service._context_cache is stale_context
    assert context.source_status["refresh_fallback"] == "previous_context"
    assert context.source_status["refresh_unavailable_note"] == "easy_tdx unavailable"


def test_get_context_starts_background_refresh_after_trajectory_bootstrap(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    restored_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote("300476", "胜宏科技", ["PCB"])],
            indices=[],
            data_mode="local_trajectory",
            source_status={"active_source": "local_trajectory_bootstrap", "trade_date": "20260807", "clock_label": "10:00:00"},
        ),
        market=market().model_copy(update={"frozen": False, "updated_at": "10:00:00"}),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "local_trajectory_bootstrap", "trade_date": "20260807", "clock_label": "10:00:00"},
    )
    calls = {"background": 0}
    monkeypatch.setattr(service, "_restore_context_from_trajectory", lambda: restored_context)
    monkeypatch.setattr(service, "_refresh_context", lambda: (_ for _ in ()).throw(AssertionError("bootstrap should not block on full refresh")))
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service, "_ensure_background_context_refresh", lambda: calls.__setitem__("background", calls["background"] + 1))

    context = service._get_context()

    assert context is restored_context
    assert calls["background"] == 1


def test_collect_once_skips_bootstrapped_context_outside_trading_window(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    restored_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote("300476", "胜宏科技", ["PCB"])],
            indices=[],
            data_mode="local_trajectory",
            source_status={"active_source": "local_trajectory_bootstrap", "trade_date": "20260807"},
        ),
        market=market().model_copy(update={"frozen": False}),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "local_trajectory_bootstrap", "trade_date": "20260807"},
    )
    service._context_cache = restored_context
    monkeypatch.setattr("app.services.is_trading_window", lambda: False)
    monkeypatch.setattr(service, "_get_context", lambda: (_ for _ in ()).throw(AssertionError("should skip collection")))

    result = service.collect_once()

    assert result["skipped"] == "local_trajectory_bootstrap"


def test_collect_once_skips_bootstrapped_context_during_trading_window(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    restored_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote("300476", "胜宏科技", ["PCB"])],
            indices=[],
            data_mode="local_trajectory",
            source_status={"active_source": "local_trajectory_bootstrap", "trade_date": "20260807"},
        ),
        market=market().model_copy(update={"frozen": False}),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "local_trajectory_bootstrap", "trade_date": "20260807"},
    )
    service._context_cache = restored_context
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service, "_get_context", lambda: (_ for _ in ()).throw(AssertionError("bootstrap should not be collected")))

    result = service.collect_once()

    assert result["skipped"] == "local_trajectory_bootstrap"


def test_collect_once_skips_bootstrapped_context_returned_from_loader(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    restored_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote("300476", "胜宏科技", ["PCB"])],
            indices=[],
            data_mode="local_trajectory",
            source_status={"active_source": "local_trajectory_bootstrap", "trade_date": "20260807"},
        ),
        market=market().model_copy(update={"frozen": False}),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "local_trajectory_bootstrap", "trade_date": "20260807"},
    )
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service, "_get_context", lambda: restored_context)
    monkeypatch.setattr(service, "_record_intraday_context", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bootstrap should not be persisted")))

    result = service.collect_once()

    assert result["skipped"] == "local_trajectory_bootstrap"


def index_snapshot(code: str, name: str) -> IndexSnapshot:
    return IndexSnapshot(
        code=code,
        name=name,
        price=3310.0,
        prev_close=3300.0,
        open=3295.0,
        high=3320.0,
        low=3288.0,
        change_pct=0.30,
        rebound_from_low_pct=0.67,
        minute_amount_ratio=1.18,
        amount=1_200_000_000_000,
    )


def test_dashboard_sector_filter_keeps_only_selected_sector(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[
                quote("300476", "胜宏科技", ["PCB"]),
                quote("300308", "中际旭创", ["CPO"]),
            ],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "test", "signal_scope": "full_market"},
        ),
        market=market(),
        sectors=[sector("PCB", "300476"), sector("CPO", "300308")],
        sector_flow=[],
        signals_all=[
            signal("300476", "胜宏科技", "PCB", SignalType.SELL_T, 91),
            signal("300308", "中际旭创", "CPO", SignalType.WATCH, 72),
        ],
        core_watch=[
            signal("300476", "胜宏科技", "PCB", SignalType.SELL_T, 91),
            signal("300308", "中际旭创", "CPO", SignalType.WATCH, 72),
        ],
        events=[
            EventItem(time="10:10", level="market", title="指数拐头", detail="市场共振"),
            EventItem(time="10:11", level="sector", title="PCB板块点火", detail="PCB强确认"),
            EventItem(time="10:12", level="sector", title="CPO板块点火", detail="CPO强确认"),
        ],
        source_status={"signal_scope": "full_market", "quote_count": 2, "signal_count_total": 2},
    )
    monkeypatch.setattr(service, "_get_context", lambda: context)

    payload = service.dashboard(sector="PCB")

    assert payload.selected_sector == "PCB"


def test_signal_detail_overlay_uses_formula_replay_markers(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    context = SimpleNamespace(
        context=DashboardContext(
            watchlist=[],
            themes=[],
            snapshot=MarketSnapshot(
                quotes=[quote("300476", "胜宏科技", ["PCB"])],
                indices=[],
                data_mode="closed_static",
                source_status={"active_source": "test", "trade_date": "20260807"},
            ),
            market=market(),
            sectors=[sector("PCB", "300476")],
            sector_flow=[],
            signals_all=[signal("300476", "胜宏科技", "PCB", SignalType.BUY_T, 78)],
            core_watch=[],
            events=[],
            source_status={"active_source": "test", "trade_date": "20260807"},
        ),
        actual_trade_date="20260807",
        quote=quote("300476", "胜宏科技", ["PCB"]),
        signal=signal("300476", "胜宏科技", "PCB", SignalType.BUY_T, 78),
        sector_snapshot=sector("PCB", "300476"),
        selected_sector="PCB",
        position=None,
        watchlist_item=None,
        live_mode=False,
    )
    monkeypatch.setattr(service, "_signal_detail_context", lambda *args, **kwargs: context)

    captured = {}

    def capture_replay(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["bars"] = args[1]
        captured["transaction_flow"] = kwargs["transaction_flow"]
        return (
            [
                {"time": "09:44", "price": 10.4, "change_pct": 4.0},
                {"time": "09:45", "price": 10.45, "change_pct": 4.2},
                {"time": "09:46", "price": 10.5, "change_pct": 4.5},
            ],
            [
                ReplayMarker(
                    time="09:44",
                    signal=SignalType.BUY_T,
                    price=10.4,
                    change_pct=4.0,
                    phase=SignalPhase.CONFIRM.value,
                    action="buy_t",
                    direction="positive_t",
                ),
                ReplayMarker(
                    time="09:45",
                    signal=SignalType.BUY_T,
                    price=10.45,
                    change_pct=4.2,
                    phase=SignalPhase.CONFIRM.value,
                    action="buy_t",
                    direction="positive_t",
                ),
                ReplayMarker(
                    time="09:46",
                    signal=SignalType.BUY_T,
                    price=10.5,
                    change_pct=4.5,
                    phase=SignalPhase.CONFIRM.value,
                    action="buy_t",
                    direction="positive_t",
                ),
                ReplayMarker(
                    time="09:46",
                    signal=SignalType.SELL_T,
                    price=10.5,
                    change_pct=4.5,
                    phase=SignalPhase.SELL_CONFIRM.value,
                    action="sell_base",
                    direction="reverse_t",
                ),
            ],
            [],
            ["公式引擎：做T买卖点唯一来源"],
        )

    monkeypatch.setattr(service.engine, "build_replay_detail", capture_replay)
    monkeypatch.setattr(
        service,
        "_fetch_transaction_flow",
        lambda *args, **kwargs: TransactionFlowObservation(
            available=True,
            source="easy_tdx_history_transaction_data",
            trade_date="20260807",
            count=2,
            score=12,
            evidence=["买盘增强"],
        ),
    )
    monkeypatch.setattr(
        service.data_source,
        "fetch_minute_series",
        lambda *args, **kwargs: [
            {"time": "09:44", "price": 10.4, "change_pct": 4.0, "vol": 100},
            {"time": "09:45", "price": 10.45, "change_pct": 4.2, "vol": 120},
            {"time": "09:46", "price": 10.5, "change_pct": 4.5, "vol": 130},
        ],
    )

    payload = service.signal_detail_overlay("300476", sector="PCB", trade_date="20260807")

    assert payload.code == "300476"
    assert payload.markers
    assert [(marker.time, marker.signal) for marker in payload.markers] == [
        ("09:44", SignalType.BUY_T),
        ("09:45", SignalType.BUY_T),
    ]
    assert captured["bars"][0]["time"] == "09:44"
    assert captured["transaction_flow"].available is True
    dumped = payload.model_dump(mode="json")
    assert "replay_points" not in dumped
    assert "sector_focus" not in dumped
    assert "signals" not in dumped
    assert "core_watch" not in dumped
    assert payload.selected_sector == "PCB"
    assert payload.transaction_flow["available"] is True


def test_live_detail_chart_and_overlay_share_tail_merged_minute_rows(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    live_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={
            "price": 10.86,
            "change_pct": 8.6,
            "minute_amount": 5_000_000,
            "updated_at": "10:10:22",
        }
    )
    context = SimpleNamespace(
        context=DashboardContext(
            watchlist=[],
            themes=[],
            snapshot=MarketSnapshot(
                quotes=[live_quote],
                indices=[],
                data_mode="live",
                source_status={"active_source": "easy_tdx", "trade_date": "20260810"},
            ),
            market=market().model_copy(update={"updated_at": "10:10:22"}),
            sectors=[sector("PCB", "300476")],
            sector_flow=[],
            signals_all=[signal("300476", "胜宏科技", "PCB", SignalType.BUY_T, 78)],
            core_watch=[],
            events=[],
            source_status={"active_source": "easy_tdx", "trade_date": "20260810"},
        ),
        actual_trade_date="20260810",
        quote=live_quote,
        signal=signal("300476", "胜宏科技", "PCB", SignalType.BUY_T, 78),
        sector_snapshot=sector("PCB", "300476"),
        selected_sector="PCB",
        position=None,
        watchlist_item=None,
        live_mode=True,
    )
    monkeypatch.setattr(service, "_signal_detail_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(
        service.data_source,
        "fetch_minute_series",
        lambda *args, **kwargs: [
            {"time": "10:08", "price": 10.30, "vol": 100, "amount": 1_030_000},
            {"time": "10:09", "price": 10.42, "vol": 120, "amount": 1_250_400},
        ],
    )
    monkeypatch.setattr(
        service,
        "_fetch_transaction_flow",
        lambda *args, **kwargs: TransactionFlowObservation(
            available=True,
            source="easy_tdx_transaction_data",
            trade_date="20260810",
            count=1,
        ),
    )
    captured: dict[str, list[dict]] = {}

    def capture_replay(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        captured["bars"] = args[1]
        return [], [], [], []

    monkeypatch.setattr(service.engine, "build_replay_detail", capture_replay)

    chart = service.signal_detail_chart("300476", sector="PCB", trade_date="20260810")
    service.signal_detail_overlay("300476", sector="PCB", trade_date="20260810")

    assert chart.chart.times == ["10:08", "10:09", "10:10"]
    assert chart.chart.prices[-1] == 10.86
    assert chart.chart.latest_price == 10.86
    assert captured["bars"][-1]["time"] == "10:10"
    assert captured["bars"][-1]["price"] == 10.86


def test_pinned_signals_sort_before_unpinned_within_bucket(tmp_path) -> None:
    service = make_service(tmp_path, max_signals_per_group=2)
    ordered = service._limit_signals(
        [
            signal("000001", "高分未置顶", "PCB", SignalType.BUY_T, 95),
            signal("000002", "自选置顶", "PCB", SignalType.BUY_T, 60, pinned=True),
            signal("000003", "次高分未置顶", "PCB", SignalType.BUY_T, 90),
        ]
    )

    assert [item.code for item in ordered] == ["000002", "000001"]


def test_signal_detail_includes_todays_signal_timeline(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    quote_item = quote("300476", "胜宏科技", ["PCB"])
    current_signal = signal("300476", "胜宏科技", "PCB", SignalType.WATCH, 70)
    context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote_item],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "test", "trade_date": "20260806"},
        ),
        market=market(),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[current_signal],
        core_watch=[current_signal],
        events=[],
        source_status={"signal_scope": "full_market", "quote_count": 1, "signal_count_total": 1, "trade_date": "20260806"},
    )
    prices = [
        9.4,
        9.4,
        9.42,
        9.45,
        9.5,
        9.55,
        9.6,
        9.66,
        9.72,
        9.82,
        9.9,
        9.98,
        10.06,
        10.12,
        10.18,
        10.24,
        10.3,
        10.36,
        10.42,
        10.48,
        10.52,
        10.56,
        10.6,
        10.65,
        10.7,
        10.76,
    ]
    minute_rows = [{"price": price, "vol": 600 if idx in {12, 25} else 100} for idx, price in enumerate(prices)]
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "fetch_minute_series", lambda code, trade_date, live=False: minute_rows)
    monkeypatch.setattr(
        service,
        "_fetch_transaction_flow",
        lambda code, trade_date, full_session=False: TransactionFlowObservation(
            trade_date=trade_date,
            note="deterministic minute-only test",
        ),
    )

    detail = service.signal_detail("300476", sector="PCB", trade_date="20260806")

    assert detail.signal_timeline
    assert detail.signal_timeline[0].time == "09:31"
    assert any(
        item.phase == SignalPhase.CONFIRM.value and item.signal == SignalType.BUY_T
        for item in detail.signal_timeline
    )
    assert all(item.time for item in detail.signal_timeline)


def test_signal_detail_prefers_manual_theme_and_honors_requested_sector(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    quote_item = quote("300308", "中际旭创", ["通信设备", "AI硬件", "CPO"])
    current_signal = signal("300308", "中际旭创", "通信设备", SignalType.WATCH, 70)
    official = sector("通信设备", "300308").model_copy(update={"heat_score": 99})
    ai_hardware = sector("AI硬件", "300308").model_copy(update={"heat_score": 83})
    cpo = sector("CPO", "300308").model_copy(update={"heat_score": 67})
    context = DashboardContext(
        watchlist=[],
        themes=[{"name": "AI硬件"}, {"name": "CPO"}],
        snapshot=MarketSnapshot(
            quotes=[quote_item],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "test", "trade_date": "20260806"},
        ),
        market=market(),
        sectors=[official, ai_hardware, cpo],
        sector_flow=[],
        signals_all=[current_signal],
        core_watch=[],
        events=[],
        source_status={"active_source": "test", "trade_date": "20260806"},
    )
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "fetch_minute_series", lambda code, trade_date, live=False: [])

    default_detail = service.signal_detail("300308", trade_date="20260806")
    cpo_detail = service.signal_detail("300308", sector="CPO", trade_date="20260806")

    assert default_detail.sector_snapshot.name == "AI硬件"
    assert default_detail.selected_sector == "AI硬件"
    assert default_detail.current_signal.sector == "AI硬件"
    assert cpo_detail.sector_snapshot.name == "CPO"
    assert cpo_detail.selected_sector == "CPO"
    assert cpo_detail.current_signal.sector == "CPO"


def test_signal_detail_uses_snapshot_from_requested_trade_date(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    current_quote = quote("300476", "胜宏科技", ["PCB"])
    current_context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[current_quote],
            indices=[index_snapshot("000001", "上证指数")],
            data_mode="closed_static",
            source_status={"active_source": "test", "trade_date": "20260807", "frozen": True},
        ),
        market=market(),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[signal("300476", "胜宏科技", "PCB", SignalType.WATCH, 70)],
        core_watch=[],
        events=[],
        source_status={"active_source": "test", "trade_date": "20260807", "frozen": True},
    )
    historical_quote = current_quote.model_copy(
        update={
            "price": 8.8,
            "prev_close": 10.0,
            "open": 9.4,
            "high": 9.5,
            "low": 8.5,
            "day_high": 9.5,
            "day_low": 8.5,
            "change_pct": -12.0,
            "updated_at": "15:00:00",
        }
    )
    historical_snapshot = MarketSnapshot(
        quotes=[historical_quote],
        indices=[index_snapshot("000001", "上证指数")],
        data_mode="closed_static",
        source_status={
            "active_source": "easy_tdx_daily_close",
            "trade_date": "20260806",
            "clock_label": "15:00:00",
            "frozen": True,
        },
    )
    monkeypatch.setattr(service, "_get_context", lambda: current_context)
    monkeypatch.setattr(
        service.data_source,
        "fetch_trade_date_snapshot",
        lambda watchlist, themes, trade_date: historical_snapshot,
    )
    monkeypatch.setattr(service.data_source, "fetch_minute_series", lambda code, trade_date, live=False: [])
    monkeypatch.setattr(service.data_source, "fetch_index_minute_series", lambda code, trade_date, live=False: [])

    detail = service.signal_detail("300476", trade_date="20260806")

    assert detail.trade_date == "20260806"
    assert detail.current_signal.price == 8.8
    assert detail.current_signal.updated_at == "15:00:00"


def test_signal_detail_includes_message_evidence_and_ai_source(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    service.message_store.upsert_messages(
        ZsxqMessageIngestRequest(
            run_id="detail-message-run",
            topics=[
                MessageTopic(
                    topic_id="topic-300476",
                    title="胜宏科技消息",
                    content="服务器PCB订单改善。",
                    create_time="2026-08-06T09:45:00+08:00",
                )
            ],
            events=[
                MessageEvent(
                    event_id="event-300476",
                    topic_id="topic-300476",
                    title="PCB订单改善",
                    summary="胜宏科技订单改善，板块有催化。",
                    event_type="订单",
                    direction=1,
                    confidence=0.9,
                    impact_strength=0.8,
                    valid_from="2026-08-06T09:45:00+08:00",
                    keywords=["PCB", "胜宏科技"],
                )
            ],
            links=[
                MessageEventLink(
                    event_id="event-300476",
                    entity_type="stock",
                    code="300476",
                    name="胜宏科技",
                    role="核心容量票",
                    relevance=0.95,
                    impact=0.9,
                ),
                MessageEventLink(
                    event_id="event-300476",
                    entity_type="sector",
                    code="pcb_ccl_eglass",
                    name="PCB/CCL/电子布",
                    role="所属主题",
                    relevance=0.9,
                    impact=0.8,
                ),
            ],
        )
    )
    quote_item = quote("300476", "胜宏科技", ["PCB"])
    current_signal = signal("300476", "胜宏科技", "PCB", SignalType.BUY_T, 88)
    context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote_item],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "test", "trade_date": "20260806"},
        ),
        market=market(),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[current_signal],
        core_watch=[current_signal],
        events=[],
        source_status={"signal_scope": "full_market", "quote_count": 1, "signal_count_total": 1, "trade_date": "20260806"},
    )
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "fetch_minute_series", lambda code, trade_date, live=False: [])

    detail = service.signal_detail("300476", sector="PCB", trade_date="20260806")
    source = service._analysis_source(detail)

    assert detail.message_evidence.stock
    assert detail.message_evidence.sector
    assert detail.message_evidence.stock[0].event_title == "PCB订单改善"
    assert source["message_evidence"]["stock"][0]["code"] == "300476"
    assert source["message_evidence"]["sector"][0]["name"] == "PCB/CCL/电子布"


def test_signal_detail_extras_fetches_f10_only_when_requested(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    quote_item = quote("300476", "胜宏科技", ["PCB"])
    current_signal = signal("300476", "胜宏科技", "PCB", SignalType.BUY_T, 88)
    context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote_item],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "test", "trade_date": "20260806"},
        ),
        market=market(),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[current_signal],
        core_watch=[current_signal],
        events=[],
        source_status={"signal_scope": "full_market", "quote_count": 1, "signal_count_total": 1, "trade_date": "20260806"},
    )
    calls: dict[str, list[str]] = {
        "fundamentals": [],
        "capital_flow": [],
        "indicators": [],
        "chanlun": [],
    }
    fundamentals = FundamentalPayload(
        available=True,
        code="300476",
        section_count=1,
        expected_section_count=21,
        sections=[
            FundamentalSection(
                key="valuation",
                title="估值指标",
                available=True,
                status="ok",
                row_count=1,
                fields=[FundamentalField(label="PE TTM", value="58.84", raw_key="PETTM")],
                tables=[
                    FundamentalTable(
                        title="估值明细",
                        columns=["日期", "PE TTM"],
                        raw_columns=["DATE", "PETTM"],
                        rows=[{"日期": "20260807", "PE TTM": "58.84"}],
                        row_count=1,
                    )
                ],
            )
        ],
    )
    capital_flow = DetailDataPayload(
        available=True,
        source="easy_tdx_mac_capital_flow",
        code="300476",
        fetched_at="2026-08-06T10:30:00",
        summary={"latest_date": "20260806", "main_net": 12.3},
        note="资金流按需读取",
    )
    indicators = DetailDataPayload(
        available=True,
        source="easy_tdx_mac_indicators",
        code="300476",
        fetched_at="2026-08-06T10:30:00",
        summary={"MACD_DIF": 0.12, "MACD_DEA": 0.08},
        note="技术指标按需读取",
    )
    chanlun = DetailDataPayload(
        available=True,
        source="easy_tdx_chanlun",
        code="300476",
        fetched_at="2026-08-06T10:30:00",
        summary={"bi_count": 8, "zs_count": 2},
        note="缠论按需读取",
    )
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "auction_history", lambda code, trade_date=None: [])

    def fake_fetch_fundamentals(code: str) -> FundamentalPayload:
        calls["fundamentals"].append(code)
        return fundamentals

    def fake_fetch_capital_flow(code: str) -> DetailDataPayload:
        calls["capital_flow"].append(code)
        return capital_flow

    def fake_fetch_indicators(code: str) -> DetailDataPayload:
        calls["indicators"].append(code)
        return indicators

    def fake_fetch_chanlun(code: str) -> DetailDataPayload:
        calls["chanlun"].append(code)
        return chanlun

    monkeypatch.setattr(service.data_source, "fetch_fundamentals", fake_fetch_fundamentals)
    monkeypatch.setattr(service.data_source, "fetch_capital_flow", fake_fetch_capital_flow)
    monkeypatch.setattr(service.data_source, "fetch_technical_indicators", fake_fetch_indicators)
    monkeypatch.setattr(service.data_source, "fetch_chanlun", fake_fetch_chanlun)

    default_payload = service.signal_detail_extras("300476", sector="PCB", trade_date="20260806")
    f10_payload = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_fundamentals=True,
    )
    capital_payload = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_capital_flow=True,
    )
    indicators_payload = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_indicators=True,
    )
    chanlun_payload = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_chanlun=True,
    )

    assert calls == {
        "fundamentals": ["300476"],
        "capital_flow": ["300476"],
        "indicators": ["300476"],
        "chanlun": ["300476"],
    }
    assert default_payload.fundamentals.available is False
    assert default_payload.fundamentals.expected_section_count == 21
    assert default_payload.capital_flow.available is False
    assert default_payload.technical_indicators.available is False
    assert default_payload.chanlun.available is False
    assert f10_payload.fundamentals.available is True
    assert f10_payload.fundamentals.source == "easy_tdx_f10_7615"
    assert f10_payload.fundamentals.sections[0].tables[0].rows[0]["PE TTM"] == "58.84"
    assert capital_payload.capital_flow.available is True
    assert capital_payload.capital_flow.source == "easy_tdx_mac_capital_flow"
    assert indicators_payload.technical_indicators.available is True
    assert indicators_payload.technical_indicators.source == "easy_tdx_mac_indicators"
    assert chanlun_payload.chanlun.available is True
    assert chanlun_payload.chanlun.source == "easy_tdx_chanlun"


def test_signal_detail_extras_does_not_load_chart_or_transaction_data(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    quote_item = quote("300476", "胜宏科技", ["PCB"])
    current_signal = signal("300476", "胜宏科技", "PCB", SignalType.BUY_T, 88)
    context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote_item],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "test", "trade_date": "20260806"},
        ),
        market=market(),
        sectors=[sector("PCB", "300476")],
        sector_flow=[],
        signals_all=[current_signal],
        core_watch=[current_signal],
        events=[],
        source_status={
            "signal_scope": "full_market",
            "quote_count": 1,
            "signal_count_total": 1,
            "trade_date": "20260806",
        },
    )
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "auction_history", lambda code, trade_date=None: [])

    def forbidden(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("extras must not load chart, replay or transaction data")

    monkeypatch.setattr(service.data_source, "fetch_minute_series", forbidden)
    monkeypatch.setattr(service, "_fetch_transaction_flow", forbidden)
    monkeypatch.setattr(service.engine, "build_replay_detail", forbidden)

    payload = service.signal_detail_extras("300476", sector="PCB", trade_date="20260806")
    dumped = payload.model_dump(mode="json")

    assert payload.code == "300476"
    assert "replay_points" not in dumped
    assert "markers" not in dumped
    assert "transaction_flow" not in dumped


def test_index_detail_uses_index_minute_series(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[],
            indices=[index_snapshot("000001", "上证指数")],
            data_mode="closed_static",
            source_status={"active_source": "test", "trade_date": "20260806"},
        ),
        market=MarketState(
            trend=TrendState.TURNING_UP,
            emotion_score=70,
            breadth_pct=55.0,
            index_turning=True,
            amount_expanding=True,
            mainline="PCB",
            indices=[index_snapshot("000001", "上证指数")],
            reasons=["指数拐头"],
            updated_at="10:20:00",
        ),
        sectors=[],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"signal_scope": "full_market", "trade_date": "20260806"},
    )
    minute_rows = [
        {"price": 3298.0, "vol": 1000},
        {"price": 3302.0, "vol": 1200},
        {"price": 3307.0, "vol": 1800},
        {"price": 3311.0, "vol": 2200},
    ]
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "fetch_index_minute_series", lambda code, trade_date, live=False: minute_rows)

    detail = service.index_detail("000001", trade_date="20260806")

    assert detail.code == "000001"
    assert detail.current_index.name == "上证指数"
    assert detail.replay_points
    assert detail.replay_points[0].time == "09:31"
    assert detail.replay_points[-1].signal == SignalType.WATCH
    assert "大盘盘口分时" in detail.summary[0]


def test_index_minutes_uses_default_indices_when_context_indices_empty(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[],
            indices=[],
            data_mode="live",
            source_status={"active_source": "easy_tdx", "trade_date": "20260814"},
        ),
        market=market().model_copy(update={"indices": [], "updated_at": "11:05:00"}),
        sectors=[],
        sector_flow=[],
        signals_all=[],
        core_watch=[],
        events=[],
        source_status={"active_source": "easy_tdx", "trade_date": "20260814"},
    )
    calls: list[tuple[str, str, bool]] = []

    def fake_index_minutes(code: str, trade_date: str, live: bool = False) -> list[dict[str, float | str]]:
        calls.append((code, trade_date, live))
        return [
            {"time": "09:30", "price": 3300.0, "vol": 1000},
            {"time": "09:31", "price": 3303.0, "vol": 1200},
        ]

    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "fetch_index_minute_series", fake_index_minutes)

    payload = service.index_minutes()

    assert [item["code"] for item in payload["indices"]] == ["000001", "399001", "399006"]
    assert [item["name"] for item in payload["indices"]] == ["上证指数", "深证成指", "创业板指"]
    assert payload["indices"][0]["points"][0]["time"] == "09:30"
    assert payload["indices"][0]["points"][-1]["change_pct"] == 0.09
    assert calls == [
        ("000001", "20260814", True),
        ("399001", "20260814", True),
        ("399006", "20260814", True),
    ]


def test_live_dashboard_snapshot_uses_index_minute_fallback_when_indices_empty(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    snapshot = MarketSnapshot(
        quotes=[quote("300476", "胜宏科技", ["PCB"])],
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "trade_date": "20260814"},
    )
    rows_by_code = {
        "000001": [
            {"time": "09:30", "price": 3300.0, "vol": 1000, "amount": 10_000},
            {"time": "11:05", "price": 3309.9, "vol": 1500, "amount": 20_000},
        ],
        "399001": [
            {"time": "09:30", "price": 10100.0, "vol": 1000, "amount": 10_000},
            {"time": "11:05", "price": 10069.7, "vol": 800, "amount": 18_000},
        ],
        "399006": [
            {"time": "09:30", "price": 2200.0, "vol": 1000, "amount": 10_000},
            {"time": "11:05", "price": 2206.6, "vol": 1200, "amount": 19_000},
        ],
    }
    calls: list[tuple[str, str, bool]] = []

    def fake_index_minutes(code: str, trade_date: str, live: bool = False) -> list[dict[str, float | str]]:
        calls.append((code, trade_date, live))
        return rows_by_code[code]

    monkeypatch.setattr(service.data_source, "fetch_index_minute_series", fake_index_minutes)

    fallback = service._snapshot_with_index_minute_fallback(snapshot)
    market_state = service.engine.build_market_state(
        fallback.indices,
        fallback.quotes,
        clock_label="11:05:00",
        frozen=False,
    )

    assert [item.code for item in fallback.indices] == ["000001", "399001", "399006"]
    assert fallback.indices[0].name == "上证指数"
    assert fallback.indices[0].price == 3309.9
    assert fallback.indices[0].change_pct == 0.3
    assert fallback.source_status["index_minute_fallback"] is True
    assert fallback.source_status["index_minute_fallback_count"] == 3
    assert len(market_state.indices) == 3
    assert calls == [
        ("000001", "20260814", True),
        ("399001", "20260814", True),
        ("399006", "20260814", True),
    ]


def test_sector_flow_uses_minute_series_for_curve_shape(tmp_path) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476"), sector("CPO", "300308")]
    quotes = [
        quote("300476", "胜宏科技", ["PCB"]),
        quote("300308", "中际旭创", ["CPO"]),
    ]
    minute_series_map = {
        "300476": [
            {"price": 10.0, "vol": 100},
            {"price": 10.2, "vol": 200},
            {"price": 10.4, "vol": 300},
            {"price": 10.6, "vol": 400},
        ],
        "300308": [
            {"price": 10.0, "vol": 100},
            {"price": 9.98, "vol": 100},
            {"price": 9.96, "vol": 100},
            {"price": 9.94, "vol": 100},
        ],
    }

    series = service.engine.build_sector_flow(sectors, quotes, minute_series_map)

    assert [item.name for item in series] == ["PCB", "CPO"]
    assert len(series[0].points) == 4
    assert series[0].final_value > series[1].final_value


def test_sector_flow_context_fetches_easy_tdx_minute_series(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    quotes = [quote("300476", "胜宏科技", ["PCB"])]
    snapshot = MarketSnapshot(
        quotes=quotes,
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "frozen": True,
        },
    )
    calls = []

    def fake_minute_series(code: str, trade_date: str, live: bool = False):
        calls.append((code, trade_date, live))
        return [
            {"price": 10.0, "vol": 100},
            {"price": 10.2, "vol": 200},
            {"price": 10.4, "vol": 300},
        ]

    monkeypatch.setattr(service.data_source, "fetch_minute_series", fake_minute_series)

    series = service._sector_flow_for_context(snapshot, sectors, cache_namespace="unit-test-flow")

    assert calls == [("300476", "20260810", True)]
    assert len(series) == 1
    assert series[0].name == "PCB"
    assert len(series[0].points) == 3


def test_sector_flow_empty_cache_is_rebuilt(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    quotes = [quote("300476", "胜宏科技", ["PCB"])]
    snapshot = MarketSnapshot(
        quotes=quotes,
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "frozen": True,
        },
    )
    cache_key = "20260810|live|easy_tdx|unit-test-empty-cache"
    service._sector_flow_cache_by_key[cache_key] = (9999999999, [])
    calls = []

    def fake_minute_series(code: str, trade_date: str, live: bool = False):
        calls.append((code, trade_date, live))
        return [{"price": 10.0, "vol": 100}, {"price": 10.2, "vol": 200}]

    monkeypatch.setattr(service.data_source, "fetch_minute_series", fake_minute_series)

    series = service._sector_flow_for_context(
        snapshot,
        sectors,
        cache_namespace="unit-test-empty-cache",
    )

    assert calls == [("300476", "20260810", True)]
    assert len(series) == 1


def test_sector_flow_deferred_frozen_restores_cloud_state(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    cloud = MemoryStateStore()
    service.state_store = cloud
    sectors = [sector("PCB", "300476")]
    quotes = [quote("300476", "胜宏科技", ["PCB"])]
    snapshot = MarketSnapshot(
        quotes=quotes,
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "frozen": True,
        },
    )
    cache_key = "20260810|live|easy_tdx|unit-test-cloud-flow"
    cloud.set_json(
        "sector_flow",
        cache_key,
        {
            "trade_date": "20260810",
            "series": [
                {
                    "name": "PCB",
                    "heat_score": 80,
                    "final_value": 1.2,
                    "change_pct": 2.2,
                    "points": [{"time": "09:31", "value": 0.4}, {"time": "09:32", "value": 0.8}],
                }
            ],
        },
    )

    def forbidden_schedule(*args, **kwargs):
        raise AssertionError("cloud sector flow should avoid deferred empty fallback")

    monkeypatch.setattr(service, "_schedule_sector_flow_trajectory_refresh", forbidden_schedule)

    series = service._sector_flow_for_context(
        snapshot,
        sectors,
        cache_namespace="unit-test-cloud-flow",
        allow_deferred=True,
    )

    assert [item.name for item in series] == ["PCB"]
    assert [point.time for point in series[0].points] == ["09:31", "09:32"]


def test_sector_flow_deferred_frozen_builds_once_when_cloud_state_empty(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    service.state_store = MemoryStateStore()
    sectors = [sector("PCB", "300476")]
    quotes = [quote("300476", "胜宏科技", ["PCB"])]
    snapshot = MarketSnapshot(
        quotes=quotes,
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "frozen": True,
        },
    )
    expected = [
        {
            "name": "PCB",
            "heat_score": 80,
            "final_value": 1.2,
            "change_pct": 2.2,
            "points": [{"time": "09:31", "value": 0.4}, {"time": "09:32", "value": 0.8}],
        }
    ]
    calls = []

    def fake_build(cache_key, snapshot_arg, sectors_arg, member_code_loader=None):
        calls.append(cache_key)
        return [SectorFlowSeries.model_validate(item) for item in expected]

    monkeypatch.setattr(service, "_build_and_cache_sector_flow", fake_build)

    series = service._sector_flow_for_context(
        snapshot,
        sectors,
        cache_namespace="unit-test-empty-cloud-flow",
        allow_deferred=True,
    )

    assert calls == ["20260810|live|easy_tdx|unit-test-empty-cloud-flow"]
    assert [item.name for item in series] == ["PCB"]


def test_sector_flow_build_persists_cloud_state(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    cloud = MemoryStateStore()
    service.state_store = cloud
    sectors = [sector("PCB", "300476")]
    quotes = [quote("300476", "胜宏科技", ["PCB"])]
    snapshot = MarketSnapshot(
        quotes=quotes,
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "frozen": True,
        },
    )
    cache_key = "20260810|live|easy_tdx|unit-test-persist-cloud-flow"

    monkeypatch.setattr(
        service.data_source,
        "fetch_minute_series",
        lambda code, trade_date, live=False: [
            {"price": 10.0, "vol": 100},
            {"price": 10.2, "vol": 200},
            {"price": 10.4, "vol": 300},
        ],
    )

    result = service._build_and_cache_sector_flow(cache_key, snapshot, sectors)

    saved = cloud.values[("sector_flow", cache_key)]
    assert result
    assert saved["trade_date"] == "20260810"
    assert saved["series"][0]["name"] == "PCB"
    assert len(saved["series"][0]["points"]) == 3


def test_live_sector_flow_proxy_uses_quote_amount_deltas(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    first_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={"price": 10.2, "open": 10.0, "prev_close": 10.0, "amount": 100_000_000, "minute_amount": 1_000_000}
    )
    second_quote = first_quote.model_copy(update={"price": 10.4, "amount": 160_000_000})

    def snapshot_with(quote_item: Quote, clock_label: str) -> MarketSnapshot:
        return MarketSnapshot(
            quotes=[quote_item],
            indices=[],
            data_mode="live",
            source_status={
                "active_source": "easy_tdx",
                "trade_date": "20260810",
                "clock_label": clock_label,
                "frozen": False,
            },
        )

    monkeypatch.setattr(service, "_ensure_sector_flow_refresh", lambda *args, **kwargs: None)

    first = service._sector_flow_for_context(
        snapshot_with(first_quote, "09:31:00"),
        sectors,
        cache_namespace="unit-test-live-proxy",
        prefer_async=True,
    )
    second = service._sector_flow_for_context(
        snapshot_with(second_quote, "09:32:00"),
        sectors,
        cache_namespace="unit-test-live-proxy",
        prefer_async=True,
    )

    assert [item.name for item in first] == ["PCB"]
    assert [point.value for point in first[0].points] == [0.01]
    assert [item.name for item in second] == ["PCB"]
    assert [point.value for point in second[0].points] == [0.01, 0.6]


def test_live_sector_flow_proxy_falls_back_to_l1_active_volume(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    live_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={
            "price": 10.0,
            "open": 10.0,
            "prev_close": 10.0,
            "amount": 100_000_000,
            "minute_amount": 0,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=620_000,
                active_sell_volume=20_000,
            ),
        }
    )
    snapshot = MarketSnapshot(
        quotes=[live_quote],
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "clock_label": "09:31:00",
            "frozen": False,
        },
    )
    monkeypatch.setattr(service, "_ensure_sector_flow_refresh", lambda *args, **kwargs: None)

    series = service._sector_flow_for_context(
        snapshot,
        sectors,
        cache_namespace="unit-test-live-active-volume",
        prefer_async=True,
    )

    assert [item.name for item in series] == ["PCB"]
    assert series[0].flow_basis == "每分钟净流入(全成员成交额增量×方向，缺省用L1主动量)"
    assert [point.value for point in series[0].points] == [3.0]


def test_live_sector_flow_proxy_restores_cloud_state_on_cold_start(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    cloud = MemoryStateStore()
    service.state_store = cloud
    sectors = [sector("PCB", "300476")]
    live_quote = quote("300476", "胜宏科技", ["PCB"])
    snapshot = MarketSnapshot(
        quotes=[live_quote],
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "clock_label": "09:33:00",
            "frozen": False,
        },
    )
    cache_key = "20260810|live|easy_tdx|unit-test-live-cloud-flow"
    cloud.set_json(
        "sector_flow",
        cache_key,
        {
            "trade_date": "20260810",
            "series": [
                {
                    "name": "PCB",
                    "heat_score": 80,
                    "final_value": 1.2,
                    "change_pct": 2.2,
                    "points": [{"time": "09:31", "value": 0.4}, {"time": "09:32", "value": 0.8}],
                }
            ],
        },
    )
    backfill_calls = {"count": 0}

    monkeypatch.setattr(service, "_ensure_sector_flow_refresh", lambda *args, **kwargs: backfill_calls.__setitem__("count", backfill_calls["count"] + 1))
    monkeypatch.setattr(
        service.data_source,
        "fetch_minute_series",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live cold start should use cloud state")),
    )

    series = service._sector_flow_for_context(
        snapshot,
        sectors,
        cache_namespace="unit-test-live-cloud-flow",
        prefer_async=True,
    )

    assert [item.name for item in series] == ["PCB"]
    assert [point.time for point in series[0].points] == ["09:31", "09:32", "09:33"]
    assert series[0].points[-1].value == 0.03
    assert backfill_calls["count"] == 1
    seeded = service._sector_flow_proxy_by_key[cache_key]["points"]["PCB"]
    assert seeded == {"09:31": 0.4, "09:32": 0.8, "09:33": 0.03}


def test_live_sector_flow_backfill_uses_minute_series_when_trajectory_is_empty(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    live_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={"price": 10.8, "open": 10.0, "prev_close": 10.0, "amount": 180_000_000}
    )
    snapshot = MarketSnapshot(
        quotes=[live_quote],
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "clock_label": "11:05:00",
            "frozen": False,
        },
    )
    cache_key = "20260810|live|easy_tdx|unit-test-live-minute-backfill"
    service._sector_flow_proxy_by_key[cache_key] = {
        "trade_date": "20260810",
        "points": {"PCB": {"11:05": 0.3}},
    }
    calls: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(service.trajectory_store, "stock_feature_ticks_by_code", lambda *args, **kwargs: {})

    def fake_minute_series(code: str, trade_date: str, live: bool = False):
        calls.append((code, trade_date, live))
        return [
            {"time": "09:30", "price": 10.0, "vol": 1000},
            {"time": "09:31", "price": 10.2, "vol": 1500},
            {"time": "09:32", "price": 10.4, "vol": 2000},
        ]

    monkeypatch.setattr(service.data_source, "fetch_minute_series", fake_minute_series)

    service._refresh_sector_flow_backfill_from_trajectory(cache_key, snapshot, sectors, None)

    assert calls == [("300476", "20260810", True)]
    seeded = service._sector_flow_proxy_by_key[cache_key]["points"]["PCB"]
    assert "09:31" in seeded
    assert seeded["11:05"] == 0.3
    assert service._sector_flow_cache_by_key[cache_key][1][0].points[0].time == "09:31"


def test_live_sector_flow_backfill_uses_minute_series_when_trajectory_starts_late(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    live_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={"price": 10.8, "open": 10.0, "prev_close": 10.0, "amount": 180_000_000}
    )
    snapshot = MarketSnapshot(
        quotes=[live_quote],
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "clock_label": "11:05:00",
            "frozen": False,
        },
    )
    cache_key = "20260810|live|easy_tdx|unit-test-live-late-trajectory-backfill"
    service._sector_flow_proxy_by_key[cache_key] = {
        "trade_date": "20260810",
        "points": {"PCB": {"11:05": 0.3}},
    }
    calls: list[tuple[str, str, bool]] = []
    late_flow = [
        SectorFlowSeries.model_validate(
            {
                "name": "PCB",
                "heat_score": 80,
                "final_value": 0.4,
                "change_pct": 2.2,
                "points": [{"time": "11:00", "value": 0.1}, {"time": "11:01", "value": 0.2}],
            }
        )
    ]

    monkeypatch.setattr(service, "_sector_flow_from_stock_trajectory", lambda *args, **kwargs: late_flow)

    def fake_minute_series(code: str, trade_date: str, live: bool = False):
        calls.append((code, trade_date, live))
        return [
            {"time": "09:30", "price": 10.0, "vol": 1000},
            {"time": "09:31", "price": 10.2, "vol": 1500},
            {"time": "09:32", "price": 10.4, "vol": 2000},
        ]

    monkeypatch.setattr(service.data_source, "fetch_minute_series", fake_minute_series)

    service._refresh_sector_flow_backfill_from_trajectory(cache_key, snapshot, sectors, None)

    assert calls == [("300476", "20260810", True)]
    seeded = service._sector_flow_proxy_by_key[cache_key]["points"]["PCB"]
    assert "09:31" in seeded
    assert seeded["11:05"] == 0.3
    assert service._sector_flow_cache_by_key[cache_key][1][0].points[0].time == "09:31"


def test_live_sector_flow_schedules_backfill_when_existing_points_start_late(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    live_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={"price": 10.8, "open": 10.0, "prev_close": 10.0, "amount": 180_000_000}
    )
    snapshot = MarketSnapshot(
        quotes=[live_quote],
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "clock_label": "11:05:00",
            "frozen": False,
        },
    )
    cache_namespace = "unit-test-live-late-start"
    cache_key = f"20260810|live|easy_tdx|{cache_namespace}"
    service._sector_flow_proxy_by_key[cache_key] = {
        "trade_date": "20260810",
        "points": {"PCB": {"11:00": 0.1, "11:01": 0.2, "11:02": 0.3, "11:03": 0.4}},
    }
    calls = {"count": 0}

    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(
        service,
        "_ensure_sector_flow_refresh",
        lambda *args, **kwargs: calls.__setitem__("count", calls["count"] + 1),
    )

    service._sector_flow_for_context(
        snapshot,
        sectors,
        cache_namespace=cache_namespace,
        prefer_async=True,
    )

    assert calls["count"] == 1


def test_sector_flow_snapshot_time_label_clamps_lunch_break_to_last_trade_minute(tmp_path) -> None:
    service = make_service(tmp_path)
    snapshot = MarketSnapshot(
        quotes=[],
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "clock_label": "12:18:22"},
    )

    assert service._snapshot_time_label(snapshot) == "11:30"


def test_live_sector_flow_proxy_continues_after_cloud_seed(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    cloud = MemoryStateStore()
    service.state_store = cloud
    sectors = [sector("PCB", "300476")]
    first_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={"price": 10.5, "open": 10.0, "prev_close": 10.0, "amount": 100_000_000, "minute_amount": 3_000_000}
    )
    second_quote = first_quote.model_copy(update={"price": 10.6, "amount": 130_000_000, "minute_amount": 4_000_000})
    cache_key = "20260810|live|easy_tdx|unit-test-live-cloud-continues"
    cloud.set_json(
        "sector_flow",
        cache_key,
        {
            "trade_date": "20260810",
            "series": [
                {
                    "name": "PCB",
                    "heat_score": 80,
                    "final_value": 1.2,
                    "change_pct": 2.2,
                    "points": [{"time": "09:31", "value": 0.4}, {"time": "09:32", "value": 0.8}],
                }
            ],
        },
    )

    def snapshot_with(quote_item: Quote, clock_label: str) -> MarketSnapshot:
        return MarketSnapshot(
            quotes=[quote_item],
            indices=[],
            data_mode="live",
            source_status={
                "active_source": "easy_tdx",
                "trade_date": "20260810",
                "clock_label": clock_label,
                "frozen": False,
            },
        )

    monkeypatch.setattr(service, "_ensure_sector_flow_refresh", lambda *args, **kwargs: None)
    first = service._sector_flow_for_context(
        snapshot_with(first_quote, "09:32:00"),
        sectors,
        cache_namespace="unit-test-live-cloud-continues",
        prefer_async=True,
    )
    service._sector_flow_cache_by_key[cache_key] = (0, first)

    second = service._sector_flow_for_context(
        snapshot_with(second_quote, "09:33:00"),
        sectors,
        cache_namespace="unit-test-live-cloud-continues",
        prefer_async=True,
    )

    assert [point.time for point in second[0].points] == ["09:31", "09:32", "09:33"]
    assert second[0].points[-1].value == 0.04


def test_live_sector_flow_proxy_extends_fresh_cached_tail_when_current_minute_moves(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    live_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={
            "price": 10.6,
            "open": 10.0,
            "prev_close": 10.0,
            "amount": 200_000_000,
            "minute_amount": 5_000_000,
        }
    )
    snapshot = MarketSnapshot(
        quotes=[live_quote],
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260810",
            "clock_label": "13:12:00",
            "frozen": False,
        },
    )
    cache_namespace = "unit-test-live-tail"
    cache_key = f"20260810|live|easy_tdx|{cache_namespace}"
    service._sector_flow_cache_by_key[cache_key] = (
        time.time(),
        [
            SectorFlowSeries.model_validate(
                {
                    "name": "PCB",
                    "heat_score": 80,
                    "final_value": 0.2,
                    "change_pct": 2.2,
                    "points": [{"time": "09:31", "value": 0.1}, {"time": "11:30", "value": 0.2}],
                }
            )
        ],
    )

    monkeypatch.setattr(service, "_ensure_sector_flow_refresh", lambda *args, **kwargs: None)

    series = service._sector_flow_for_context(
        snapshot,
        sectors,
        cache_namespace=cache_namespace,
        prefer_async=True,
    )

    assert [point.time for point in series[0].points] == ["09:31", "11:30", "13:12"]
    assert series[0].points[-1].value == 0.05


def test_sector_flow_fixed_set_admits_new_top_ranked_board(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    service.engine.sector_flow_top_n = 3
    old_one = sector("旧强一", "300001").model_copy(update={"heat_score": 95, "avg_change_pct": 3.0})
    old_two = sector("旧强二", "300002").model_copy(update={"heat_score": 94, "avg_change_pct": 2.9})
    old_three = sector("旧强三", "300003").model_copy(update={"heat_score": 93, "avg_change_pct": 2.8})
    network = sector("网络接配及塔设", "300004").model_copy(update={"heat_score": 99, "avg_change_pct": 4.2})
    quotes = [
        quote("300001", "旧强一核心", ["旧强一"]),
        quote("300002", "旧强二核心", ["旧强二"]),
        quote("300003", "旧强三核心", ["旧强三"]),
        quote("300004", "灿勤科技", ["网络接配及塔设"]),
    ]
    snapshot = MarketSnapshot(
        quotes=quotes,
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260812",
            "frozen": True,
        },
    )

    def fake_minute_series(code: str, trade_date: str, live: bool = False):
        return [
            {"price": 10.0, "vol": 100},
            {"price": 10.2, "vol": 200},
            {"price": 10.4, "vol": 300},
        ]

    monkeypatch.setattr(service.data_source, "fetch_minute_series", fake_minute_series)

    first = service._sector_flow_for_context(
        snapshot,
        [old_one, old_two, old_three, network],
        cache_namespace="unit-test-flow-rotation",
    )
    second = service._build_and_cache_sector_flow(
        "20260812|live|easy_tdx|unit-test-flow-rotation",
        snapshot,
        [network, old_one, old_two, old_three],
    )

    assert [item.name for item in first] == ["旧强一", "旧强二", "旧强三"]
    assert "网络接配及塔设" in [item.name for item in second]
    assert [item.name for item in second][:3] == ["网络接配及塔设", "旧强一", "旧强二"]


def test_sector_flow_codes_prefer_leader_and_liquid_strong_names(tmp_path) -> None:
    service = make_service(tmp_path)
    bio_sector = SectorSnapshot(
        name="生物制药",
        heat_score=100,
        avg_change_pct=6.1,
        up_count=75,
        total_count=80,
        limit_up_count=6,
        opened_limit_count=1,
        core_attack=False,
        core_codes=[],
        leader_code="688137",
        leader_name="近岸蛋白",
        reasons=["75/80上涨", "6只涨停确认"],
    )
    quotes = [
        quote("688137", "近岸蛋白", ["生物制药"]).model_copy(update={"change_pct": 20.01, "amount": 169_000_000, "limit_up": True, "minute_amount_ratio": 0.3}),
        quote("688806", "泰诺麦博", ["生物制药"]).model_copy(update={"change_pct": -2.06, "amount": 259_000_000, "minute_amount_ratio": 1.0}),
        quote("688796", "百奥赛图", ["生物制药"]).model_copy(update={"change_pct": -1.48, "amount": 212_000_000, "minute_amount_ratio": 1.0}),
        quote("688235", "百济神州", ["生物制药"]).model_copy(update={"change_pct": 11.0, "amount": 3_143_000_000, "minute_amount_ratio": 0.3}),
        quote("301047", "义翘神州", ["生物制药"]).model_copy(update={"change_pct": 20.0, "amount": 1_352_000_000, "limit_up": True, "minute_amount_ratio": 0.3}),
    ]

    codes = service.engine.sector_flow_codes(bio_sector, quotes)

    assert codes == ["688137", "688235", "301047"]


def test_ai_analysis_is_saved_for_later_loading(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    current_signal = signal("300476", "胜宏科技", "PCB", SignalType.BUY_T, 88, pinned=True)
    detail = SignalReplayDetail(
        code="300476",
        name="胜宏科技",
        sector="PCB",
        trade_date="20260806",
        selected_sector="PCB",
        market=market(),
        sector_snapshot=sector("PCB", "300476"),
        current_signal=current_signal,
        replay_points=[],
        markers=[
            ReplayMarker(
                time="09:44",
                signal=SignalType.BUY_T,
                price=10.5,
                change_pct=5,
                reasons=["低位拐头且量能放大"],
            )
        ],
        summary=["首个买T 09:44"],
        watchlisted=True,
        watchlist_tags=["持仓"],
    )
    monkeypatch.setattr(service, "signal_detail", lambda code, sector=None, trade_date=None: detail)

    record = service.analyze_watchlist_item("300476", sector="PCB", trade_date="20260806")
    loaded = service.load_analysis("300476", "20260806")

    assert record.provider == "fake"
    assert loaded is not None
    assert loaded.result["decision"] == "买T"
    assert loaded.result["buy_points"] == []
    assert loaded.result["ai_role"] == "解释结构化证据；不生成或修改买卖点"
    assert loaded.source["selected_sector"] == "PCB"
    assert loaded.source["source_version"] == "analysis_context_v2"


def test_research_status_prefers_protocol_quality_and_exposes_walk_forward(
    tmp_path,
    monkeypatch,
) -> None:
    service = make_service(tmp_path)
    report = {
        "data_quality": {
            "flow_mode": "easy_tdx_history_transaction",
            "minute_coverage_mean": 0.25,
            "note": "legacy source label",
        },
        "research_protocol": {
            "protocol_version": "research_protocol_v1",
            "run_id": "run-test",
            "generated_at": "2026-08-09T21:00:00",
            "sample": {
                "sample_count": 200,
                "stock_day_count": 200,
                "date_count": 2,
                "dates": ["20260806", "20260807"],
                "transaction_sample_count": 200,
                "selection": {
                    "method": "逐日事前选样",
                    "date_count": 2,
                    "stock_day_count": 200,
                    "distinct_code_count": 120,
                    "future_filter_used": False,
                    "per_date": [
                        {
                            "target_date": "20260806",
                            "selection_date": "20260805",
                            "count": 100,
                            "codes": ["300476"],
                        },
                        {
                            "target_date": "20260807",
                            "selection_date": "20260806",
                            "count": 100,
                            "codes": ["300308"],
                        },
                    ],
                },
            },
            "data_quality": {
                "minute_coverage_mean": 1.0,
                "transaction_minute_coverage_mean": 0.98,
                "transaction_metadata_coverage_mean": 1.0,
                "transaction_source": "get_history_transaction_data",
                "level2_available": False,
                "status": "usable_with_limitations",
            },
            "validation": {
                "status": "sample_insufficient",
                "raw_label_count": 300,
                "independent_event_count": 100,
                "filled_event_count": 95,
                "reasons": ["样本不足"],
                "base_oos_metrics": {"mean_net_r": 0.12, "private_raw": "drop"},
                "pessimistic_oos_metrics": {"mean_net_r": 0.03},
                "direction": {
                    "positive_t": {
                        "status": "sample_insufficient",
                        "independent_event_count": 44,
                        "base_oos_metrics": {"mean_net_r": 0.2},
                        "pessimistic_oos_metrics": {"mean_net_r": 0.08},
                    },
                    "reverse_t": {
                        "status": "sample_insufficient",
                        "independent_event_count": 56,
                    },
                },
            },
            "execution_model": {
                "base_round_trip_cost_pct": 0.18,
                "extra_pessimistic_round_trip_cost_pct": 0.08,
            },
            "walk_forward": {
                "method": "expanding_window_one_day_ahead",
                "minimum_training_days": 20,
                "required_total_days": 60,
                "date_count": 2,
                "fold_count": 0,
                "oos_dates": [],
                "available": False,
                "complete": False,
                "base": {"mean_net_r": None},
                "pessimistic": {"mean_net_r": None},
                "folds": [{"raw": "must not be returned"}],
            },
            "limitations": ["样本不足"],
        },
    }
    monkeypatch.setattr(service, "_latest_research_report", lambda: (report, "latest.json"))
    monkeypatch.setattr(service.trajectory_store, "status", lambda: {"research_runs": 1})

    payload = service.research_status()

    assert payload["data_quality"]["flow_mode"] == "easy_tdx_history_transaction"
    assert payload["data_quality"]["minute_coverage_mean"] == 1.0
    assert payload["data_quality"]["transaction_source"] == "get_history_transaction_data"
    assert payload["data_quality"]["level2_available"] is False
    assert payload["validation"]["raw_label_count"] == 300
    assert payload["validation"]["independent_event_count"] == 100
    positive = payload["validation"]["direction"]["positive_t"]
    assert positive["base_oos_metrics"]["mean_net_r"] == 0.2
    assert "private_raw" not in payload["validation"]["base_oos_metrics"]
    assert payload["walk_forward"]["fold_count"] == 0
    assert "folds" not in payload["walk_forward"]
    assert payload["sample"]["selection"]["per_date"] == [
        {"target_date": "20260806", "selection_date": "20260805", "count": 100},
        {"target_date": "20260807", "selection_date": "20260806", "count": 100},
    ]


def test_research_protocol_keeps_both_counterfactual_estimands_compact(
    tmp_path,
    monkeypatch,
) -> None:
    service = make_service(tmp_path)
    report = {
        "research_protocol": {
            "validation": {"status": "sample_insufficient"},
            "counterfactuals": {
                "direction_only": {
                    "note": "成交方向不单独作为买点",
                    "comparison_basis": "same_observed_event_times",
                    "available_estimands": [
                        "same_observed_event_times",
                        "regenerated_candidate_set",
                    ],
                    "same_event": {
                        "outcomes": 30,
                        "independent_outcomes": 10,
                        "metrics": {"mean_net_r": 0.1, "raw_rows": [1, 2, 3]},
                        "estimand": "固定观察事件时点",
                    },
                    "regenerated": {
                        "outcomes": 12,
                        "independent_outcomes": 4,
                        "metrics": {"mean_net_r": -0.2},
                        "estimand": "删除因子后重建候选集",
                    },
                    "candidate_comparison": {
                        "same_event_count": 10,
                        "regenerated_event_count": 4,
                        "raw_candidates": [{"code": "300476"}],
                    },
                }
            },
            "parameter_discovery": {
                "status": "exploratory_only",
                "feature_performance": {
                    "feature_count": 17,
                    "features": {
                        "price_efficiency": {
                            "bins": [
                                {
                                    "bin": 1,
                                    "independent_event_count": 10,
                                    "base": {"mean_net_r": 0.1},
                                }
                            ],
                            "adjacent_pairs": [],
                        }
                    },
                },
            },
            "matched_control": {
                "matched_record_count": 1,
                "records": [{"code": "300476", "time": "09:45"}],
            },
        }
    }
    monkeypatch.setattr(service, "_latest_research_report", lambda: (report, "latest.json"))
    monkeypatch.setattr(service.trajectory_store, "status", lambda: {})

    payload = service.research_protocol()
    control = payload["counterfactuals"]["direction_only"]

    assert control["same_event"]["independent_outcomes"] == 10
    assert control["regenerated"]["independent_outcomes"] == 4
    assert control["same_event"]["metrics"]["mean_net_r"] == 0.1
    assert "raw_rows" not in control["same_event"]["metrics"]
    assert control["candidate_comparison"] == {
        "same_event_count": 10,
        "regenerated_event_count": 4,
    }
    assert payload["parameter_discovery"]["status"] == "exploratory_only"
    assert "features" not in payload["parameter_discovery"]["feature_performance"]
    assert payload["parameter_discovery"]["feature_performance"]["feature_summaries"] == {
        "price_efficiency": {
            "bin_count": 1,
            "adjacent_pair_count": 0,
            "stable_positive_pair_count": 0,
            "independent_event_count": 10,
        }
    }
    assert "records" not in payload["matched_control"]
    assert payload["matched_control"]["records_omitted"] is True




def test_opening_markers_sector_code_mapped_to_board_name(tmp_path, monkeypatch) -> None:
    """机会队列里 X410302 这类内部行业代码显示为官方板块名；中文主题名不动。"""
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_stock_board_display_map", lambda: {"300058": "互联网广告"})
    items = [
        {"code": "300058", "sector": "X430201"},
        {"code": "300476", "sector": "PCB"},
        {"code": "000001", "sector": "X9999"},
    ]
    out = service._opening_markers_with_sector_names(items)
    assert out[0]["sector"] == "互联网广告"
    assert out[1]["sector"] == "PCB"  # 手动主题名原样保留
    assert out[2]["sector"] == "X9999"  # 映射不到保留原代码，便于发现未覆盖板块


def test_opening_markers_live_quote_overlay(tmp_path) -> None:
    """队列标记回填实时行情：live_* 挂上、信号时刻 price/change_pct 不动；
    无快照的票和历史日原样保留，前端自行退回信号时刻快照。"""
    service = make_service(tmp_path)

    class _Quote:
        price = 26.0123
        change_pct = 5.078
        amount = 123456789.0

    items = [
        {"code": "300058", "trade_date": "20260813", "price": 16.54, "change_pct": 4.09},
        {"code": "300476", "trade_date": "20260813", "price": 100.0, "change_pct": 2.0},
        {"code": "301052", "trade_date": "20260812", "price": 25.55, "change_pct": 3.19},
    ]
    quotes = {"300058": _Quote()}
    out = service._opening_markers_with_live_quotes(items, quotes, requested_date="20260813")
    assert out[0]["live_price"] == 26.012
    assert out[0]["live_change_pct"] == 5.08
    assert out[0]["live_amount"] == 123456789.0
    assert out[0]["price"] == 16.54  # 信号时刻快照语义不动
    assert "live_price" not in out[1]  # 快照里没有的票原样
    assert "live_price" not in out[2]  # 历史日不回填实时行情
    assert service._opening_markers_with_live_quotes(items, None, requested_date="20260813") is items
