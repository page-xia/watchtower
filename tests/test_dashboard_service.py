import threading
import time
from types import SimpleNamespace

import pytest

from app.config import AppSettings
from app.data_sources import BoardContext, MarketSnapshot, china_now
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
    MessageEvidence,
    MessageEvidenceBundle,
    MessageStoreStatus,
    MessageSyncRunStatus,
    MessageTopic,
    MiniIntradaySeries,
    OrderFlowObservation,
    PositionRecord,
    DetailDataPayload,
    Quote,
    ReplayMarker,
    SectorFlowPoint,
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
    ZsxqMessageIngestResponse,
)
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


class MemoryMessageStore:
    def __init__(self) -> None:
        self.topics: dict[str, MessageTopic] = {}
        self.events: dict[str, MessageEvent] = {}
        self.links: list[MessageEventLink] = []
        self.latest_run: MessageSyncRunStatus | None = None

    def upsert_messages(self, payload: ZsxqMessageIngestRequest) -> ZsxqMessageIngestResponse:
        for topic in payload.topics:
            self.topics[topic.topic_id] = topic
        for event in payload.events:
            self.events[event.event_id] = event
        link_keys = {(link.event_id, link.entity_type, link.code, link.name): link for link in self.links}
        for link in payload.links:
            link_keys[(link.event_id, link.entity_type, link.code, link.name)] = link
        self.links = list(link_keys.values())
        topic_count = len(payload.topics) if payload.reported_topic_count is None else payload.reported_topic_count
        event_count = len(payload.events) if payload.reported_event_count is None else payload.reported_event_count
        link_count = len(payload.links) if payload.reported_link_count is None else payload.reported_link_count
        self.latest_run = MessageSyncRunStatus(
            run_id=payload.run_id or "memory-run",
            source=payload.source,
            start=payload.start or "",
            end=payload.end or "",
            topic_count=topic_count,
            event_count=event_count,
            link_count=link_count,
            status=payload.status,
        )
        return ZsxqMessageIngestResponse(
            ok=True,
            source=payload.source,
            run_id=self.latest_run.run_id,
            topic_count=topic_count,
            event_count=event_count,
            link_count=link_count,
        )

    def status(self, ingest_enabled: bool = False) -> MessageStoreStatus:
        return MessageStoreStatus(
            db_file="memory://messages",
            ingest_enabled=ingest_enabled,
            topic_count=len(self.topics),
            event_count=len(self.events),
            link_count=len(self.links),
            latest_run=self.latest_run,
        )

    def evidence_for(
        self,
        code: str,
        sector_terms: list[str] | None = None,
        stock_limit: int = 8,
        sector_limit: int = 8,
    ) -> MessageEvidenceBundle:
        stock: list[MessageEvidence] = []
        sector: list[MessageEvidence] = []
        terms = [str(term or "").strip() for term in (sector_terms or []) if str(term or "").strip()]
        for link in self.links:
            event = self.events.get(link.event_id)
            topic = self.topics.get(event.topic_id) if event else None
            if event is None or topic is None:
                continue
            evidence = MessageEvidence(
                topic_id=topic.topic_id,
                topic_title=topic.title,
                topic_content=topic.content,
                display_text=topic.media_summary or event.summary or topic.content,
                create_time=topic.create_time,
                owner_name=topic.owner_name,
                has_files=topic.has_files,
                has_images=topic.has_images,
                media_summary=topic.media_summary,
                event_id=event.event_id,
                event_title=event.title,
                event_summary=event.summary,
                event_type=event.event_type,
                direction=str(event.direction or ""),
                confidence=event.confidence,
                impact_strength=event.impact_strength,
                valid_from=event.valid_from,
                expires_at=event.expires_at,
                keywords=event.keywords,
                entity_type=link.entity_type,
                code=link.code,
                name=link.name,
                role=link.role,
                relevance=link.relevance,
                impact=link.impact,
            )
            if link.entity_type == "stock" and link.code == str(code).zfill(6):
                stock.append(evidence.model_copy(update={"match_scope": "stock"}))
            if link.entity_type in {"sector", "theme"} and any(term in link.name for term in terms):
                sector.append(evidence.model_copy(update={"match_scope": "sector"}))
        return MessageEvidenceBundle(stock=stock[:stock_limit], sector=sector[:sector_limit])


class FailingMessageStore(MemoryMessageStore):
    db_file = "cloudbase_mysql://server-d2g7x597t019f5cb0/default/server-d2g7x597t019f5cb0"

    def status(self, ingest_enabled: bool = False) -> MessageStoreStatus:
        raise RuntimeError("mysql connection refused")


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
        message_store=MemoryMessageStore(),
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


def test_message_status_returns_fallback_when_message_store_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WATCH_INGEST_TOKEN", "unit-token")
    service = make_service(tmp_path)
    service.message_store = FailingMessageStore()

    status = service.message_status()

    assert status["db_file"] == FailingMessageStore.db_file
    assert status["ingest_enabled"] is True
    assert status["topic_count"] == 0
    assert status["event_count"] == 0
    assert status["link_count"] == 0


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
        signal_grade="做T公式买T",
        factor_flags=["公式买入原语"],
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
    assert "公式买入原语" in marked.markers[0].reasons


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
    """详情叠加层买卖标记唯一来源：做T公式 LONGCROSS 信号（chart/overlay 共享 bundle）。"""
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
    # quote(): prev_close=10, day_high=10.8, day_low=9.8 → 支撑=9.8625, 阻力=10.675
    closes = [10.0, 10.05, 10.02, 9.80, 10.0, 10.3, 10.5, 10.70]
    monkeypatch.setattr(
        service.data_source,
        "fetch_minute_series",
        lambda *args, **kwargs: [
            {"time": f"09:{44 + i:02d}", "price": c, "vol": 100 + i * 10}
            for i, c in enumerate(closes)
        ],
    )

    payload = service.signal_detail_overlay("300476", sector="PCB", trade_date="20260807")

    assert payload.code == "300476"
    # 买：LONGCROSS(支撑,现价,2) —— 第 4 根跌破支撑 9.8625
    # 卖：LONGCROSS(现价,阻力,2) —— 第 8 根突破阻力 10.675
    assert [(marker.time, marker.signal) for marker in payload.markers] == [
        ("09:47", SignalType.BUY_T),
        ("09:51", SignalType.SELL_T),
    ]
    buy_marker, sell_marker = payload.markers
    assert buy_marker.setup == "zuot_support_rebound"
    assert "LONGCROSS(支撑,现价,2)" in buy_marker.reasons[0]
    assert abs(buy_marker.invalidation_price - 9.8625) < 0.01
    assert sell_marker.setup == "zuot_resistance_fade"
    assert "LONGCROSS(现价,阻力,2)" in sell_marker.reasons[0]
    assert abs(sell_marker.invalidation_price - 10.675) < 0.01
    assert buy_marker.source_quality == "zuot_tdx_levels_v1"
    # 公式状态来自同一批分钟行
    assert payload.formula_state.point_count == len(closes)
    assert payload.formula_state.support > 0
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

    calls = {"count": 0}
    original = DashboardService._shared_stock_chart_rows

    def counting(self, info):  # noqa: ANN001, ANN202
        calls["count"] += 1
        return original(self, info)

    monkeypatch.setattr(DashboardService, "_shared_stock_chart_rows", counting)

    chart = service.signal_detail_chart("300476", sector="PCB", trade_date="20260810")
    overlay = service.signal_detail_overlay("300476", sector="PCB", trade_date="20260810")

    # chart 与 overlay 共用同一份尾盘合并分钟行（bundle 级缓存，只建一次）
    assert calls["count"] == 1
    assert chart.chart.times == ["10:08", "10:09", "10:10"]
    assert chart.chart.prices[-1] == 10.86
    assert chart.chart.latest_price == 10.86
    # overlay 的公式状态也来自含实时尾行（10.86）的同一批数据
    assert overlay.formula_state.price == 10.86
    assert overlay.formula_state.point_count == 3


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
                    media_summary=(
                        "【附件投研要点】\n"
                        "- 涉及个股 胜宏科技(300476，核心直接标的，权重0.92)。\n"
                        "- 核心逻辑：AI服务器PCB订单继续改善，产能利用率提升。"
                    ),
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
    assert "核心逻辑：AI服务器PCB订单继续改善" in source["message_evidence"]["stock"][0]["summary"]
    assert source["message_evidence"]["sector"][0]["name"] == "PCB/CCL/电子布"
    assert any("核心逻辑：AI服务器PCB订单继续改善" in item for item in service._message_basis(detail))


def test_signal_detail_extras_matches_messages_with_easy_tdx_board_names(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    service.message_store.upsert_messages(
        ZsxqMessageIngestRequest(
            run_id="detail-message-tdx-board-run",
            topics=[
                MessageTopic(
                    topic_id="topic-688549",
                    title="中巨芯消息",
                    content="电子化学品国产替代推进。",
                    create_time="2026-08-07T09:45:00+08:00",
                )
            ],
            events=[
                MessageEvent(
                    event_id="event-688549",
                    topic_id="topic-688549",
                    title="电子化学品催化",
                    summary="中巨芯受益电子化学品板块催化。",
                    event_type="板块催化",
                    direction=1,
                    confidence=0.88,
                    impact_strength=0.74,
                    valid_from="2026-08-07T09:45:00+08:00",
                    keywords=["电子化学品", "中巨芯"],
                )
            ],
            links=[
                MessageEventLink(
                    event_id="event-688549",
                    entity_type="sector",
                    code="X4006",
                    name="电子化学品",
                    role="easy_tdx申万三级",
                    relevance=0.9,
                    impact=0.8,
                )
            ],
        )
    )
    quote_item = quote("688549", "中巨芯-U", ["X4006"])
    current_signal = signal("688549", "中巨芯-U", "X4006", SignalType.WATCH, 66)
    context = DashboardContext(
        watchlist=[],
        themes=[],
        snapshot=MarketSnapshot(
            quotes=[quote_item],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "test", "trade_date": "20260807"},
        ),
        market=market(),
        sectors=[],
        sector_flow=[],
        signals_all=[current_signal],
        core_watch=[current_signal],
        events=[],
        source_status={
            "signal_scope": "full_market",
            "quote_count": 1,
            "signal_count_total": 1,
            "trade_date": "20260807",
        },
    )

    def tdx_sector(name: str, board_code: str, board_level: int) -> SectorSnapshot:
        return sector(name, "688549").model_copy(
            update={
                "board_code": board_code,
                "board_level": board_level,
                "board_source": "easy_tdx_mac_board_ranking",
            }
        )

    board_contexts = {
        1: BoardContext(
            board_level=1,
            source="easy_tdx_mac_board_ranking",
            available=True,
            fetched_at="2026-08-07T09:45:00",
            sectors=[tdx_sector("电子", "X1000", 1)],
            name_to_code={"电子": "X1000"},
            code_to_name={"X1000": "电子"},
            members_by_code={"X1000": ["688549"]},
        ),
        2: BoardContext(
            board_level=2,
            source="easy_tdx_mac_board_ranking",
            available=True,
            fetched_at="2026-08-07T09:45:00",
            sectors=[tdx_sector("半导体", "X3006", 2)],
            name_to_code={"半导体": "X3006"},
            code_to_name={"X3006": "半导体"},
            members_by_code={"X3006": ["688549"]},
        ),
        3: BoardContext(
            board_level=3,
            source="easy_tdx_mac_board_ranking",
            available=True,
            fetched_at="2026-08-07T09:45:00",
            sectors=[tdx_sector("电子化学品", "X4006", 3)],
            name_to_code={"电子化学品": "X4006"},
            code_to_name={"X4006": "电子化学品"},
            members_by_code={"X4006": ["688549"]},
        ),
    }
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_contexts[int(board_level)])

    payload = service.signal_detail_extras(
        "688549",
        sector="X4006",
        trade_date="20260807",
        include_auction_history=False,
    )

    assert payload.sector == "电子化学品"
    assert payload.selected_sector == "X4006"
    assert [item.name for item in payload.message_evidence.sector] == ["电子化学品"]


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
    monkeypatch.setattr(service, "_stock_detail_bundle", forbidden)

    payload = service.signal_detail_extras("300476", sector="PCB", trade_date="20260806")
    dumped = payload.model_dump(mode="json")

    assert payload.code == "300476"
    assert "replay_points" not in dumped
    assert "markers" not in dumped
    assert "transaction_flow" not in dumped


def test_signal_detail_extras_can_skip_auction_history(tmp_path, monkeypatch) -> None:
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

    def forbidden(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("auction history should be skippable for the initial extras payload")

    monkeypatch.setattr(service.data_source, "auction_history", forbidden)

    payload = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_auction_history=False,
    )

    assert payload.auction_history == []


def test_signal_detail_extras_keeps_response_when_message_evidence_fails(tmp_path, monkeypatch) -> None:
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

    def fail_evidence(**kwargs):  # noqa: ARG001
        raise RuntimeError("cloudbase mysql timeout")

    monkeypatch.setattr(service.message_store, "evidence_for", fail_evidence)

    payload = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_auction_history=False,
    )

    assert payload.code == "300476"
    assert payload.message_evidence == MessageEvidenceBundle()
    # 详情 extras 不再加载 message_status（大表 count=exact，前端不消费），
    # 同步状态走 /api/messages/status。
    assert payload.message_status is None


def test_signal_detail_extras_skips_message_evidence_when_not_requested(tmp_path, monkeypatch) -> None:
    """include_messages=False 时不查 CloudBase 消息证据（详情页默认不加载星球消息）。"""
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

    calls: list[str] = []

    def spy_evidence(**kwargs):  # noqa: ARG001
        calls.append("300476")
        return MessageEvidenceBundle()

    monkeypatch.setattr(service.message_store, "evidence_for", spy_evidence)

    no_messages = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_auction_history=False,
        include_messages=False,
    )
    assert calls == []
    assert no_messages.message_evidence == MessageEvidenceBundle()

    with_messages = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_auction_history=False,
    )
    assert calls == ["300476"]
    assert with_messages.message_evidence == MessageEvidenceBundle()


def test_signal_detail_extras_keeps_response_when_message_status_fails(tmp_path, monkeypatch) -> None:
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

    def fail_status(*, ingest_enabled: bool = False):  # noqa: ARG001
        raise RuntimeError("cloudbase mysql timeout")

    monkeypatch.setattr(service.message_store, "status", fail_status)

    payload = service.signal_detail_extras(
        "300476",
        sector="PCB",
        trade_date="20260806",
        include_auction_history=False,
    )

    assert payload.code == "300476"
    assert payload.message_evidence == MessageEvidenceBundle()
    # extras 已不加载 message_status，status 故障不再影响详情扩展接口。
    assert payload.message_status is None


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
                    "points": [{"time": "14:59", "value": 0.4}, {"time": "15:00", "value": 0.8}],
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
    assert [point.time for point in series[0].points] == ["14:59", "15:00"]


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
    assert series[0].flow_basis == "每分钟净流入(全成员L1主动量差，缺省用成交额增量×方向)"
    assert [point.value for point in series[0].points] == [3.0]


def test_live_sector_flow_proxy_active_volume_is_primary_not_additive(tmp_path, monkeypatch) -> None:
    """order_flow 可用时以 L1 主动量差为准，不再叠加成交额增量×方向（趋势日虚高根因）。"""
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    first_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={
            "price": 10.0,
            "open": 10.0,
            "prev_close": 10.0,
            "amount": 100_000_000,
            "minute_amount": 0,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=100_000,
                active_sell_volume=50_000,
            ),
        }
    )
    second_quote = first_quote.model_copy(
        update={
            "price": 10.2,
            "amount": 300_000_000,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=160_000,
                active_sell_volume=60_000,
            ),
        }
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
        snapshot_with(first_quote, "09:31:00"),
        sectors,
        cache_namespace="unit-test-active-primary",
        prefer_async=True,
    )
    second = service._sector_flow_for_context(
        snapshot_with(second_quote, "09:32:00"),
        sectors,
        cache_namespace="unit-test-active-primary",
        prefer_async=True,
    )

    # 首 tick：冷启动摊速 (100000-50000)×10×100/2 = 0.25 亿
    assert [point.value for point in first[0].points] == [0.25]
    # 次 tick：主动量差增量 (60000-10000)×10.2×100 = 0.51 亿；
    # 若错误叠加成交额增量×方向（+2 亿）该点会变成 2.51。
    assert [point.value for point in second[0].points] == [0.25, 0.51]


def test_live_sector_flow_proxy_active_counter_reset_skips_tick(tmp_path, monkeypatch) -> None:
    """外/内盘计数器回退时跳过该 tick，不得把全天累计净额摊进当前分钟。"""
    service = make_service(tmp_path)
    sectors = [sector("PCB", "300476")]
    base_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={
            "price": 10.0,
            "open": 10.0,
            "prev_close": 10.0,
            "amount": 100_000_000,
            "minute_amount": 0,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=1_000_000,
                active_sell_volume=500_000,
            ),
        }
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
        snapshot_with(base_quote, "10:00:00"),
        sectors,
        cache_namespace="unit-test-active-reset",
        prefer_async=True,
    )
    reset_quote = base_quote.model_copy(
        update={
            "price": 10.1,
            "amount": 160_000_000,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=100_000,  # 计数器回退：远小于上一轮的 1_000_000
                active_sell_volume=50_000,
            ),
        }
    )
    second = service._sector_flow_for_context(
        snapshot_with(reset_quote, "10:01:00"),
        sectors,
        cache_namespace="unit-test-active-reset",
        prefer_async=True,
    )
    resumed_quote = reset_quote.model_copy(
        update={
            "price": 10.1,
            "amount": 200_000_000,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=130_000,
                active_sell_volume=55_000,
            ),
        }
    )
    third = service._sector_flow_for_context(
        snapshot_with(resumed_quote, "10:02:00"),
        sectors,
        cache_namespace="unit-test-active-reset",
        prefer_async=True,
    )

    # 首 tick：冷启动摊速 (1e6-5e5)×10×100/31 ≈ 0.16 亿
    assert [point.value for point in first[0].points] == [0.16]
    # 回退 tick：跳过，不新增分钟桶，也不摊入全天累计（旧逻辑会写入 ≈1.45 亿）
    assert [point.value for point in second[0].points] == [0.16]
    # 恢复 tick：以回退后的新基线取增量 (30000-5000)×10.1×100 = 0.25 亿
    assert [point.value for point in third[0].points] == [0.16, 0.25]


def test_sector_flow_cloud_legacy_basis_rejected(tmp_path, monkeypatch) -> None:
    """旧口径（成交额增量×方向）持久化的云端记录拒绝加载，避免历史虚高数据回潮。"""
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
    cache_key = "20260810|live|easy_tdx|unit-test-legacy-cloud"
    cloud.set_json(
        "sector_flow",
        cache_key,
        {
            "trade_date": "20260810",
            "series": [
                {
                    "name": "PCB",
                    "heat_score": 80,
                    "final_value": 109.0,
                    "change_pct": 2.2,
                    "flow_basis": "每分钟净流入(全成员成交额增量×方向，缺省用L1主动量)",
                    "points": [{"time": "09:31", "value": 50.0}, {"time": "09:32", "value": 59.0}],
                }
            ],
        },
    )
    monkeypatch.setattr(service, "_ensure_sector_flow_refresh", lambda *args, **kwargs: None)

    series = service._sector_flow_for_context(
        snapshot,
        sectors,
        cache_namespace="unit-test-legacy-cloud",
        prefer_async=True,
    )

    assert [item.name for item in series] == ["PCB"]
    values = [point.value for point in series[0].points]
    assert 50.0 not in values and 59.0 not in values


def test_active_net_truth_total_sums_member_order_flow(tmp_path) -> None:
    """真值 = 全成员 (外盘-内盘)×价格×100，单位亿元；覆盖不全时按成员数外推。"""
    service = make_service(tmp_path)
    members = ["300476", "300308", "000001", "000002", "000003"]
    covered_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={
            "price": 10.0,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=70_000,
                active_sell_volume=20_000,
            ),
        }
    )
    other_covered = quote("300308", "中际旭创", ["PCB"]).model_copy(
        update={
            "price": 20.0,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=30_000,
                active_sell_volume=50_000,
            ),
        }
    )
    no_flow = quote("000001", "平安银行", ["PCB"])
    quotes_by_code = {"300476": covered_quote, "300308": other_covered, "000001": no_flow}

    # 覆盖 2/5 < 80%：不定标
    assert service._active_net_truth_total(members, quotes_by_code) is None

    # 覆盖 4/5 = 80%：两只 +0.5 亿、两只 -0.4 亿，合计 0.2 亿，外推 ×5/4 = 0.25 亿
    more = {
        "000002": covered_quote.model_copy(update={"code": "000002"}),
        "000003": other_covered.model_copy(update={"code": "000003"}),
    }
    truth = service._active_net_truth_total(members, {**quotes_by_code, **more})
    assert truth == pytest.approx(0.25)


def test_anchor_series_to_truth_scales_shape_and_final_value(tmp_path) -> None:
    """收盘后定标：分钟形态按比例缩放，final_value 收敛到 L1 主动净额真值。"""
    service = make_service(tmp_path)
    series = SectorFlowSeries(
        name="光纤光缆",
        heat_score=80,
        final_value=70.11,
        change_pct=3.0,
        points=[
            SectorFlowPoint(time="09:31", value=30.0),
            SectorFlowPoint(time="09:32", value=40.11),
        ],
        flow_basis="每分钟净流入(全成员成交额增量×方向)",
    )

    anchored = service._anchor_series_to_truth(series, 46.12)

    assert anchored.final_value == 46.12
    assert anchored.flow_basis == "每分钟净流入(分钟形态×L1主动量定标)"
    ratio = 46.12 / 70.11
    assert [point.value for point in anchored.points] == [
        round(30.0 * ratio, 4),
        round(40.11 * ratio, 4),
    ]
    # 形状不变：各分钟占比与符号保持一致
    assert anchored.points[0].value / anchored.points[1].value == pytest.approx(30.0 / 40.11)


def test_anchor_series_to_truth_skips_conflict_and_missing_truth(tmp_path) -> None:
    """方向矛盾或真值缺失时保留原曲线，不强行定标。"""
    service = make_service(tmp_path)
    series = SectorFlowSeries(
        name="光纤光缆",
        heat_score=80,
        final_value=70.11,
        change_pct=3.0,
        points=[
            SectorFlowPoint(time="09:31", value=30.0),
            SectorFlowPoint(time="09:32", value=40.11),
        ],
        flow_basis="每分钟净流入(全成员成交额增量×方向)",
    )

    conflict = service._anchor_series_to_truth(series, -46.12)
    assert conflict is series or conflict.final_value == 70.11
    assert conflict.flow_basis == "每分钟净流入(全成员成交额增量×方向)"

    missing = service._anchor_series_to_truth(series, None)
    assert missing.final_value == 70.11

    # 真值远超形态（震荡日成交额口径正负对冲、低估真值）必须定标：
    # 2026-08-17 种子 ratio≈6.3、消费电子组件 ratio≈3.1 都是真实情形。
    underestimated = service._anchor_series_to_truth(series, 70.11 * 6.3)
    assert underestimated.final_value == round(70.11 * 6.3, 2)
    assert underestimated.flow_basis == "每分钟净流入(分钟形态×L1主动量定标)"

    # 仅挡垃圾形态：比值 >20 说明形态与真值完全不像同一个板块，保留原曲线。
    out_of_range = service._anchor_series_to_truth(series, 70.11 * 30)
    assert out_of_range.final_value == 70.11


def test_sector_flow_cloud_rejects_incomplete_history_for_past_date(tmp_path) -> None:
    """历史交易日的云端记录若未覆盖到尾盘（残缺回灌），拒绝加载并触发重建。"""
    service = make_service(tmp_path)
    cloud = MemoryStateStore()
    service.state_store = cloud
    cache_key = "20260814|live|easy_tdx|unit-test-incomplete-cloud"

    def record(last_time: str) -> dict:
        return {
            "trade_date": "20260814",
            "series": [
                {
                    "name": "PCB",
                    "heat_score": 80,
                    "final_value": 56.79,
                    "change_pct": 2.2,
                    "flow_basis": "每分钟净流入代理(分钟成交额加权)",
                    "points": [
                        {"time": "09:31", "value": 10.0},
                        {"time": last_time, "value": 46.79},
                    ],
                }
            ],
        }

    cloud.set_json("sector_flow", cache_key, record("10:38"))
    assert service._load_sector_flow_cloud(cache_key, "20260814") == []

    cloud.set_json("sector_flow", cache_key, record("15:00"))
    loaded = service._load_sector_flow_cloud(cache_key, "20260814")
    assert [item.name for item in loaded] == ["PCB"]


def test_anchor_flow_list_to_active_net_anchors_loaded_cloud_record(tmp_path) -> None:
    """云端旧口径记录加载时按快照外/内盘计数器定标到 L1 主动净额真值。"""
    service = make_service(tmp_path)
    sector_item = sector("PCB", "300476")
    loaded = SectorFlowSeries(
        name="PCB",
        heat_score=80,
        final_value=70.11,
        change_pct=2.2,
        points=[
            SectorFlowPoint(time="14:59", value=30.0),
            SectorFlowPoint(time="15:00", value=40.11),
        ],
        flow_basis="每分钟净流入代理(分钟成交额加权)",
    )
    live_quote = quote("300476", "胜宏科技", ["PCB"]).model_copy(
        update={
            "price": 10.0,
            "order_flow": OrderFlowObservation(
                available=True,
                active_buy_volume=4_712_000,
                active_sell_volume=100_000,
            ),
        }
    )

    anchored = service._anchor_flow_list_to_active_net([loaded], [sector_item], [live_quote])

    # 单成员板块真值：(4712000-100000)×10×100/1e8 = 46.12 亿
    assert anchored[0].final_value == 46.12
    assert anchored[0].flow_basis == "每分钟净流入(分钟形态×L1主动量定标)"
    assert anchored[0].points[-1].time == "15:00"

    # 已锚定记录重复加载不二次缩放
    again = service._anchor_flow_list_to_active_net(anchored, [sector_item], [live_quote])
    assert again[0].final_value == 46.12
    assert [point.value for point in again[0].points] == [point.value for point in anchored[0].points]


def test_live_sector_flow_proxy_restores_cloud_state_on_cold_start(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    cloud = MemoryStateStore()
    service.state_store = cloud
    sectors = [sector("PCB", "300476")]
    live_quote = quote("300476", "胜宏科技", ["PCB"])
    # 盘中冷启动播种只发生在当日：完整性闸门对当日不完整曲线放行
    today = china_now().strftime("%Y%m%d")
    snapshot = MarketSnapshot(
        quotes=[live_quote],
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": today,
            "clock_label": "09:33:00",
            "frozen": False,
        },
    )
    cache_key = f"{today}|live|easy_tdx|unit-test-live-cloud-flow"
    cloud.set_json(
        "sector_flow",
        cache_key,
        {
            "trade_date": today,
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
    today = china_now().strftime("%Y%m%d")
    cache_key = f"{today}|live|easy_tdx|unit-test-live-cloud-continues"
    cloud.set_json(
        "sector_flow",
        cache_key,
        {
            "trade_date": today,
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
                "trade_date": today,
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
