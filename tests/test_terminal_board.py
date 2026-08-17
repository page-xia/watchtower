from types import SimpleNamespace
from datetime import datetime as RealDateTime

import app.data_sources as data_sources
from app.config import AppSettings
from app.data_sources import (
    AuctionSnapshotTracker,
    BoardContext,
    EasyTdxMarketDataSource,
    MarketSnapshot,
    StockMeta,
    EasyTdxDailyDataSource,
)
from app.message_store import MessageStore
from app.models import AuctionSnapshot, IndexSnapshot, MarketState, Quote, SectorSnapshot, SignalType, TradeSignal, WatchlistItem
from app.services import DashboardContext, DashboardService
from app.storage import AnalysisStore
from app.trajectory_store import IntradayWatchtowerStore


def test_tdx_code_mapping_routes_920_to_beijing_exchange():
    assert data_sources.market_id_for_code("920000") == 2
    assert data_sources.full_tdx_code("920000") == "bj920000"
    assert data_sources.full_tdx_code("900901") == "sh900901"


def test_easy_tdx_quote_chunk_error_recovers_valid_symbols(monkeypatch):
    monkeypatch.setattr(data_sources, "easy_tdx_market_for_code", lambda code, index=False: code)
    source = EasyTdxMarketDataSource(AppSettings(), EasyTdxDailyDataSource(AppSettings()))
    source.chunk_size = 2

    class FakeApi:
        def get_security_quotes(self, request):
            if len(request) > 1:
                raise ValueError("snapshot record marker not found: bj920000")
            code = request[0][1]
            if code == "920000":
                raise ValueError("snapshot record marker not found: bj920000")
            return [
                {
                    "code": code,
                    "last_close": 10.0,
                    "price": 10.5,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 10.0,
                    "amount": 1_000_000,
                    "vol": 10_000,
                    "cur_vol": 100,
                }
            ]

    universe = {
        "300476": StockMeta(code="300476", ts_code="300476.SZ", name="胜宏科技"),
        "920000": StockMeta(code="920000", ts_code="920000.BJ", name="北交所样本", market="北交所"),
    }

    quotes, meta = source._fetch_quotes(FakeApi(), universe)

    assert [quote.code for quote in quotes] == ["300476"]
    assert meta["skipped_codes"] == ["920000"]
    assert meta["skipped_count"] == 1


def test_easy_tdx_quote_empty_response_is_not_marked_live(monkeypatch):
    monkeypatch.setattr(data_sources, "easy_tdx_market_for_code", lambda code, index=False: code)
    source = EasyTdxMarketDataSource(AppSettings(), EasyTdxDailyDataSource(AppSettings()))

    class EmptyApi:
        def get_security_quotes(self, request):
            return []

    universe = {
        "300476": StockMeta(code="300476", ts_code="300476.SZ", name="胜宏科技"),
    }

    try:
        source._fetch_quotes(EmptyApi(), universe)
    except data_sources.DataSourceError as exc:
        assert "quote接口返回0条" in str(exc)
    else:
        raise AssertionError("empty quote response should not be treated as live data")


class MemoryWatchlistStore:
    def __init__(self, items=None) -> None:
        self.items = list(items or [])

    def list_items(self):
        return list(self.items)

    def upsert(self, item):
        self.items = [current for current in self.items if current.code != item.code] + [item]
        return item

    def delete(self, code):
        before = len(self.items)
        self.items = [item for item in self.items if item.code != code]
        return len(self.items) != before


class MemoryThemeStore:
    def __init__(self, themes=None) -> None:
        self.themes = list(themes or [])

    def list_themes(self):
        return list(self.themes)


def make_service(tmp_path, watchlist=None, themes=None):
    settings = AppSettings()
    return DashboardService(
        settings,
        watchlist_store=MemoryWatchlistStore(watchlist),
        theme_store=MemoryThemeStore(themes),
        analysis_store=AnalysisStore(tmp_path / "analysis"),
        message_store=MessageStore(),
        trajectory_store=IntradayWatchtowerStore(tmp_path / "intraday.sqlite"),
    )


def make_quote(code: str, change_pct: float, amount: float = 100_000_000, **updates) -> Quote:
    price = 10 * (1 + change_pct / 100)
    values = {
        "code": code,
        "name": f"测试{code}",
        "themes": ["测试板块"],
        "price": price,
        "prev_close": 10,
        "open": 10,
        "high": max(10.5, price),
        "low": min(9.5, price),
        "day_high": max(10.5, price),
        "day_low": min(9.5, price),
        "change_pct": change_pct,
        "amount": amount,
        "minute_amount": amount / 100,
        "minute_amount_ratio": 1.0,
        "updated_at": "15:00:00",
    }
    values.update(updates)
    return Quote(
        **values,
    )


def make_sector(total_count: int, leader_code: str = "000001") -> SectorSnapshot:
    return SectorSnapshot(
        name="测试板块",
        heat_score=88,
        avg_change_pct=3.1,
        up_count=total_count,
        total_count=total_count,
        limit_up_count=1,
        opened_limit_count=0,
        core_attack=True,
        core_codes=[leader_code],
        leader_code=leader_code,
        leader_name=f"测试{leader_code}",
        reasons=["板块强确认"],
    )


def make_context(quotes, watchlist=None, themes=None):
    sector_snapshot = make_sector(len(quotes))
    signals = [
        TradeSignal(
            code=quote.code,
            name=quote.name,
            signal=SignalType.WATCH,
            score=20,
            sector="测试板块",
            price=quote.price,
            change_pct=quote.change_pct,
            rebound_from_low_pct=0,
            minute_amount_ratio=quote.minute_amount_ratio,
            reasons=[],
            updated_at=quote.updated_at,
        )
        for quote in quotes
    ]
    return DashboardContext(
        watchlist=list(watchlist or []),
        themes=list(themes or []),
        snapshot=MarketSnapshot(
            quotes=list(quotes),
            indices=[],
            data_mode="closed_static",
            source_status={
                "active_source": "easy_tdx_daily_close",
                "trade_date": "20260806",
                "clock_label": "15:00:00",
                "frozen": True,
            },
        ),
        market=MarketState(
            trend="震荡偏强",
            emotion_score=68,
            breadth_pct=60,
            index_turning=True,
            amount_expanding=True,
            mainline="测试板块",
            indices=[],
            reasons=["指数拐头"],
            updated_at="15:00:00",
            frozen=True,
        ),
        sectors=[sector_snapshot],
        sector_flow=[],
        signals_all=signals,
        core_watch=[],
        events=[],
        source_status={
            "active_source": "easy_tdx_daily_close",
            "trade_date": "20260806",
            "clock_label": "15:00:00",
            "frozen": True,
            "quote_count": len(quotes),
        },
    )


def test_stock_board_is_full_market_sorted_pinned_and_paginated(tmp_path, monkeypatch):
    quotes = [make_quote(f"000{i:03d}", i / 10, amount=10_000_000 + i * 100_000) for i in range(1, 26)]
    watchlist = [WatchlistItem(code="000020", name="测试000020", themes=["测试板块"])]
    service = make_service(tmp_path, watchlist=watchlist, themes=[{"name": "测试板块", "core_codes": ["000001"]}])
    context = make_context(quotes, watchlist, service.theme_store.list_themes())
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(
        service.data_source,
        "fetch_board_context",
        lambda board_level=3: SimpleNamespace(
            available=True,
            board_level=board_level,
            source="easy_tdx_mac_board_ranking",
            fetched_at="2026-08-10T09:31:00",
            sectors=[make_sector(len(quotes), leader_code="000001").model_copy(update={"board_code": "881251", "board_level": board_level, "board_source": "easy_tdx_mac_board_ranking"})],
            name_to_code={"测试板块": "881251"},
            code_to_name={"881251": "测试板块"},
            error="",
        ),
    )
    monkeypatch.setattr(
        service.data_source,
        "fetch_board_member_codes",
        lambda board_name_or_code, board_level=3: [quote.code for quote in quotes],
    )

    first_page = service.stock_board(sector="测试板块", page=1, page_size=20)
    second_page = service.stock_board(sector="测试板块", page=2, page_size=20)

    assert first_page.scope == "full_market"
    assert first_page.selected_sector == "测试板块"
    assert first_page.total == 25
    assert len(first_page.items) == 20
    assert first_page.items[0].code == "000020"  # display pin, not scan filter
    assert second_page.page == 2
    assert len(second_page.items) == 5
    assert {item.code for item in first_page.items}.isdisjoint({item.code for item in second_page.items})
    assert {item.code for item in first_page.items + second_page.items} == {item.code for item in quotes}


def test_stock_search_matches_code_and_name_and_marks_watchlisted(tmp_path):
    quotes = [
        make_quote("300476", 1.0, amount=100_000_000, name="胜宏科技", themes=["PCB"]),
        make_quote("300308", 2.0, amount=200_000_000, name="中际旭创", themes=["CPO"]),
        make_quote("000001", 0.5, amount=300_000_000, name="平安银行", themes=["银行"]),
    ]
    watchlist = [WatchlistItem(code="300476", name="胜宏科技", themes=["PCB"])]
    service = make_service(tmp_path, watchlist=watchlist)
    service._context_cache = make_context(quotes, watchlist, themes=[])

    exact = service.search_stocks("300476")
    name_prefix = service.search_stocks("中际")
    name_contains = service.search_stocks("科技")
    limited = service.search_stocks("300", limit=1)

    assert exact[0]["code"] == "300476"
    assert exact[0]["watchlisted"] is True
    assert exact[0]["source"] == "current_context"
    assert exact[0]["themes"] == ["PCB"]
    assert name_prefix[0]["code"] == "300308"
    assert name_contains[0]["code"] == "300476"
    assert len(limited) == 1


def test_watchlist_preview_does_not_expose_internal_theme_code(tmp_path, monkeypatch):
    quote = make_quote("300209", 3.2, amount=6_000_000_000, name="行云科技", themes=["X250602"])
    watchlist = [WatchlistItem(code="300209", name="行云科技", themes=["X250602"])]
    service = make_service(tmp_path, watchlist=watchlist)
    context = make_context([quote], watchlist=watchlist, themes=[])
    context.sectors = []
    monkeypatch.setattr(service, "_get_context", lambda: context)
    # 无官方板块映射时退回「未归类」，绝不暴露内部代码
    monkeypatch.setattr(service, "_stock_board_display_map", lambda: {})

    search = service.search_stocks("300209")
    watch_preview, _ = service._context_watch_previews(context)

    assert search[0]["themes"] == ["X250602"]
    assert search[0]["sector"] == "未归类"
    assert watch_preview[0]["themes"] == ["X250602"]
    assert watch_preview[0]["sector"] == "未归类"

    # 有官方板块映射时显示申万三级板块名
    monkeypatch.setattr(service, "_stock_board_display_map", lambda: {"300209": "跨境电商"})
    watch_preview_mapped, _ = service._context_watch_previews(context)
    assert watch_preview_mapped[0]["sector"] == "跨境电商"


def test_stock_search_empty_query_does_not_load_context(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_get_context", lambda: (_ for _ in ()).throw(AssertionError("should not load context")))

    assert service.search_stocks("   ") == []


def test_official_board_uses_cached_members_and_local_quote_grouping(tmp_path, monkeypatch):
    quotes = [
        make_quote("000001", 4.0, amount=300_000_000, minute_amount_ratio=2.0),
        make_quote("000002", 2.0, amount=200_000_000),
        make_quote("000003", -1.0, amount=100_000_000),
        make_quote("000004", -3.0, amount=400_000_000),
    ]
    service = make_service(tmp_path)
    context = make_context(quotes, themes=[])
    monkeypatch.setattr(service, "_get_context", lambda: context)
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-10T09:31:00",
        sectors=[
            SectorSnapshot(
                name="强板块",
                heat_score=1,
                avg_change_pct=-9.9,
                up_count=0,
                total_count=3,
                limit_up_count=0,
                opened_limit_count=0,
                core_attack=False,
                core_codes=[],
                leader_code=None,
                leader_name=None,
                reasons=["官方缓存旧排名"],
                board_code="881001",
                board_level=3,
                board_source="easy_tdx_mac_board_ranking",
            ),
            SectorSnapshot(
                name="弱板块",
                heat_score=99,
                avg_change_pct=9.9,
                up_count=1,
                total_count=1,
                limit_up_count=0,
                opened_limit_count=0,
                core_attack=True,
                core_codes=[],
                leader_code=None,
                leader_name=None,
                reasons=["官方缓存旧排名"],
                board_code="881002",
                board_level=3,
                board_source="easy_tdx_mac_board_ranking",
            ),
        ],
        name_to_code={"强板块": "881001", "弱板块": "881002"},
        code_to_name={"881001": "强板块", "881002": "弱板块"},
        members_by_code={"881001": ["000001", "000002", "000003"], "881002": ["000004"]},
    )
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)
    monkeypatch.setattr(
        service.data_source,
        "fetch_board_member_codes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cached members should be used")),
    )

    payload = service.terminal(board_level=3, page_size=20)
    board = service.stock_board(sector="强板块", board_level=3, page_size=20)

    assert payload.board_source == "easy_tdx_cached_members_local_quote_aggregation"
    assert payload.source_status["board_local_grouping"] is True
    assert payload.source_status["board_member_cached_count"] == 2
    assert payload.sectors[0].name == "强板块"
    assert payload.sectors[0].up_count == 2
    assert payload.sectors[0].avg_change_pct == 1.67
    assert "成分股3只" in payload.sectors[0].reasons
    assert board.total == 3
    assert {item.code for item in board.items} == {"000001", "000002", "000003"}


def test_terminal_waits_for_official_board_members_before_using_official_grouping(tmp_path, monkeypatch):
    quotes = [
        make_quote("000001", 4.0, amount=300_000_000, minute_amount_ratio=2.0, themes=["测试板块"]),
        make_quote("000002", 2.0, amount=200_000_000, themes=["测试板块"]),
    ]
    service = make_service(tmp_path)
    context = make_context(quotes, themes=[])
    context.sectors = [make_sector(len(quotes)).model_copy(update={"name": "测试板块", "core_codes": ["000001"]})]
    monkeypatch.setattr(service, "_get_context", lambda: context)
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-14T10:00:00",
        sectors=[
            make_sector(len(quotes)).model_copy(
                update={
                    "name": "官方板块",
                    "board_code": "881001",
                    "board_level": 3,
                    "board_source": "easy_tdx_mac_board_ranking",
                }
            )
        ],
        name_to_code={"官方板块": "881001"},
        code_to_name={"881001": "官方板块"},
        members_by_code={},
    )
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)

    payload = service.terminal(board_level=3, page_size=20)
    board = service.stock_board(board_level=3, page_size=20)

    assert payload.board_source == "signal_engine_theme_rank"
    assert payload.source_status["official_board_available"] is True
    assert payload.source_status["official_board_member_ready"] is False
    assert payload.source_status["board_member_cached_count"] == 0
    assert [item.name for item in payload.sectors] == ["测试板块"]
    assert board.board_source == "signal_engine_theme_rank"
    assert {item.sector for item in board.items} == {"测试板块"}


def test_official_board_excludes_new_listing_distortion_from_sector_strength(tmp_path, monkeypatch):
    quotes = [
        make_quote("301717", 662.24, name="N超纯", amount=7_040_170_496, minute_amount_ratio=3.5),
        make_quote("688012", -6.90, name="中微公司", amount=11_412_088_832, minute_amount_ratio=1.2),
        make_quote("688120", 1.82, name="华海清科", amount=4_372_927_488, minute_amount_ratio=1.8),
    ]
    service = make_service(tmp_path)
    context = make_context(quotes, themes=[])
    monkeypatch.setattr(service, "_get_context", lambda: context)
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-11T15:00:00",
        sectors=[make_sector(len(quotes)).model_copy(update={"name": "半导体设备", "board_code": "881321", "board_level": 3})],
        name_to_code={"半导体设备": "881321"},
        code_to_name={"881321": "半导体设备"},
        members_by_code={"881321": [quote.code for quote in quotes]},
    )
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)

    payload = service.terminal(board_level=3, page_size=20)
    board = service.stock_board(sector="半导体设备", board_level=3, page_size=20)
    sector = next(item for item in payload.sectors if item.name == "半导体设备")

    assert sector.new_listing_excluded_count == 1
    assert sector.raw_total_count == 3
    assert sector.total_count == 2
    assert sector.avg_change_pct == -2.54
    assert sector.leader_code == "688120"
    assert "301717" not in sector.core_codes
    assert any("新股扰动剔除1只：N超纯" in reason for reason in sector.reasons)
    assert {item.code for item in board.items} == {"301717", "688012", "688120"}


def test_stock_board_constructs_only_visible_rows_on_full_market_page(tmp_path, monkeypatch):
    quotes = [
        make_quote(
            f"{index:06d}",
            change_pct=(index % 20) / 10,
            amount=10_000_000 + index * 100_000,
        )
        for index in range(1, 201)
    ]
    service = make_service(tmp_path)
    context = make_context(quotes, themes=[])
    monkeypatch.setattr(service, "_get_context", lambda: context)
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-12T10:00:00",
        sectors=[make_sector(len(quotes)).model_copy(update={"board_code": "881001", "board_level": 3})],
        name_to_code={"测试板块": "881001"},
        code_to_name={"881001": "测试板块"},
        members_by_code={"881001": [quote.code for quote in quotes]},
    )
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)

    original = service._stock_board_item
    calls = {"count": 0}

    def counted_stock_board_item(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_stock_board_item", counted_stock_board_item)

    board = service.stock_board(board_level=3, page=1, page_size=20)

    assert board.total == len(quotes)
    assert len(board.items) == 20
    assert calls["count"] == 20


def test_terminal_refreshes_visible_quotes_without_full_market_scan(tmp_path, monkeypatch):
    quotes = [make_quote(f"{index:06d}", 1.0, amount=10_000_000 + index) for index in range(1, 31)]
    watchlist = [WatchlistItem(code="000030", name="测试000030", themes=["测试板块"])]
    service = make_service(tmp_path, watchlist=watchlist)
    context = make_context(quotes, watchlist=watchlist)
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False},
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update({"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False})
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-12T10:00:00",
        sectors=[make_sector(len(quotes)).model_copy(update={"board_code": "881001", "board_level": 3})],
        name_to_code={"测试板块": "881001"},
        code_to_name={"881001": "测试板块"},
        members_by_code={"881001": [quote.code for quote in quotes]},
    )
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)
    requested: list[str] = []

    def fake_subset(codes, base_quotes=None):
        requested.extend(codes)
        board_code = next((code for code in codes if code != "000030"), codes[0] if codes else "000030")
        refreshed = {board_code, "000030"}
        return {
            code: next(quote for quote in quotes if quote.code == code).model_copy(
                update={"price": 12.34, "change_pct": 3.4, "updated_at": "10:00:02"}
            )
            for code in codes
            if code in refreshed
        }

    monkeypatch.setattr(service.data_source, "fetch_quote_subset", fake_subset, raising=False)

    payload = service.terminal(board_level=3, page=1, page_size=20)

    assert "000030" in requested
    assert len(requested) < len(quotes)
    board_item = next(item for item in payload.stock_board.items if item.code != "000030")
    watch_item = next(item for item in payload.watchlist_preview if item["code"] == "000030")
    assert board_item.price == 12.34
    assert board_item.updated_at == "10:00:02"
    assert watch_item["price"] == 12.34
    assert payload.stock_board.updated_at == "10:00:02"
    assert payload.source_status["visible_quote_refresh_count"] == 2


def test_terminal_accepts_request_local_watchlist_without_mutating_store(tmp_path, monkeypatch):
    quotes = [make_quote(f"00000{index}", index / 10, amount=10_000_000 + index) for index in range(1, 5)]
    server_watch = [WatchlistItem(code="000001", name="测试000001", themes=["测试板块"])]
    client_watch = [WatchlistItem(code="000003", name="测试000003", themes=["测试板块"])]
    service = make_service(tmp_path, watchlist=server_watch)
    context = make_context(quotes, watchlist=server_watch)
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: False)

    payload = service.terminal(board_level=3, page=1, page_size=20, fast=True, client_watchlist=client_watch)

    assert payload.watchlist_codes == ["000003"]
    assert [item.code for item in payload.watchlist] == ["000003"]
    assert [item["code"] for item in payload.watchlist_preview] == ["000003"]
    assert next(item for item in payload.stock_board.items if item.code == "000003").watchlisted is True
    assert next(item for item in payload.stock_board.items if item.code == "000001").watchlisted is False
    assert [item.code for item in service.watchlist_store.list_items()] == ["000001"]
    assert [item.code for item in context.watchlist] == ["000001"]


def test_terminal_cache_isolated_by_request_local_watchlist(tmp_path, monkeypatch):
    quotes = [make_quote(f"00000{index}", index / 10, amount=10_000_000 + index) for index in range(1, 5)]
    service = make_service(tmp_path)
    context = make_context(quotes, watchlist=[])
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: False)

    first = service.terminal(
        board_level=3,
        page=1,
        page_size=20,
        fast=True,
        client_watchlist=[WatchlistItem(code="000002", name="测试000002", themes=["测试板块"])],
    )
    second = service.terminal(
        board_level=3,
        page=1,
        page_size=20,
        fast=True,
        client_watchlist=[WatchlistItem(code="000004", name="测试000004", themes=["测试板块"])],
    )

    assert first.watchlist_codes == ["000002"]
    assert second.watchlist_codes == ["000004"]
    assert next(item for item in first.stock_board.items if item.code == "000002").watchlisted is True
    assert next(item for item in first.stock_board.items if item.code == "000004").watchlisted is False
    assert next(item for item in second.stock_board.items if item.code == "000002").watchlisted is False
    assert next(item for item in second.stock_board.items if item.code == "000004").watchlisted is True


def test_fast_terminal_omits_heavy_homepage_reads(tmp_path, monkeypatch):
    quotes = [make_quote(f"{index:06d}", index / 10, amount=10_000_000 + index) for index in range(1, 31)]
    watchlist = [WatchlistItem(code="000030", name="测试000030", themes=["测试板块"])]
    service = make_service(tmp_path, watchlist=watchlist)
    context = make_context(quotes, watchlist=watchlist)
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=[],
        data_mode="live",
        source_status={
            "active_source": "easy_tdx",
            "trade_date": "20260812",
            "clock_label": "10:00:00",
            "frozen": False,
        },
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update(
        {"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False}
    )
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(
        service.data_source,
        "fetch_board_context",
        lambda board_level=3: (_ for _ in ()).throw(AssertionError("fast terminal must not fetch official boards")),
    )
    monkeypatch.setattr(
        service.trajectory_store,
        "stock_feature_mini_series_by_code",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fast terminal must not read mini-chart SQLite")),
    )
    monkeypatch.setattr(
        service.trajectory_store,
        "sector_feature_series_by_name",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fast terminal must not read sector-flow SQLite")),
    )
    monkeypatch.setattr(
        service.data_source,
        "fetch_quote_subset",
        lambda codes, base_quotes=None: {
            code: next(quote for quote in quotes if quote.code == code).model_copy(
                update={"price": 11.11, "change_pct": 1.11, "updated_at": "10:00:02"}
            )
            for code in codes[:2]
        },
        raising=False,
    )

    payload = service.terminal(board_level=3, page=1, page_size=20, fast=True)

    assert payload.source_status["terminal_fast_mode"] is True
    assert payload.sector_flow == []
    assert payload.source_status["board_source"] == "signal_engine_theme_rank_fast"
    assert payload.source_status["visible_quote_refresh_mode"] == "subset"
    assert payload.source_status["stock_mini_chart_loaded_count"] == 0
    assert all(item.mini_chart.source_quality == "deferred" for item in payload.stock_board.items)


def test_fast_terminal_returns_cached_context_while_refresh_runs_in_background(tmp_path, monkeypatch):
    quotes = [make_quote(f"{index:06d}", index / 10, amount=10_000_000 + index) for index in range(1, 24)]
    service = make_service(tmp_path)
    context = make_context(quotes)
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False},
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update({"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False})
    service._context_cache = context
    service._context_cache_at = 0
    service._context_cache_bucket = service._context_bucket()
    calls = {"background": 0}
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service, "_refresh_context", lambda: (_ for _ in ()).throw(AssertionError("fast terminal must not block on full refresh")))
    monkeypatch.setattr(service, "_ensure_background_context_refresh", lambda: calls.__setitem__("background", calls["background"] + 1))
    monkeypatch.setattr(service.data_source, "fetch_quote_subset", lambda codes, base_quotes=None: {}, raising=False)

    payload = service.terminal(board_level=3, page_size=20, fast=True)

    assert payload.source_status["terminal_fast_mode"] is True
    assert calls["background"] == 1
    assert payload.stock_board.total == len(quotes)


def test_fast_terminal_sorts_once_and_builds_only_visible_rows(tmp_path, monkeypatch):
    quotes = [make_quote(f"{index:06d}", index / 10, amount=10_000_000 + index) for index in range(1, 121)]
    service = make_service(tmp_path)
    context = make_context(quotes)
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False},
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update({"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False})
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service.data_source, "fetch_quote_subset", lambda codes, base_quotes=None: {}, raising=False)

    original_sort_key = service._board_sort_key_for_quote
    original_item = service._stock_board_item
    calls = {"sort": 0, "item": 0}

    def counted_sort_key(*args, **kwargs):
        calls["sort"] += 1
        return original_sort_key(*args, **kwargs)

    def counted_item(*args, **kwargs):
        calls["item"] += 1
        return original_item(*args, **kwargs)

    monkeypatch.setattr(service, "_board_sort_key_for_quote", counted_sort_key)
    monkeypatch.setattr(service, "_stock_board_item", counted_item)

    payload = service.terminal(board_level=3, page=1, page_size=20, fast=True)

    assert payload.source_status["terminal_fast_mode"] is True
    assert calls["sort"] <= len(quotes)
    assert calls["item"] <= 20
    assert len(payload.stock_board.items) == 20


def test_fast_terminal_reuses_sorted_entries_but_refreshes_visible_quotes(tmp_path, monkeypatch):
    quotes = [make_quote(f"{index:06d}", index / 10, amount=10_000_000 + index) for index in range(1, 121)]
    service = make_service(tmp_path)
    context = make_context(quotes)
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False},
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update({"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False})
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)

    quote_calls = {"count": 0}

    def fetch_quote_subset(codes, base_quotes=None):
        quote_calls["count"] += 1
        first_code = list(codes)[0]
        quote = next(item for item in quotes if item.code == first_code)
        return {
            first_code: quote.model_copy(
                update={
                    "price": 88 + quote_calls["count"],
                    "change_pct": 8 + quote_calls["count"],
                    "updated_at": f"10:00:0{quote_calls['count']}",
                }
            )
        }

    monkeypatch.setattr(service.data_source, "fetch_quote_subset", fetch_quote_subset, raising=False)
    original_sort_key = service._board_sort_key_for_quote
    calls = {"sort": 0}

    def counted_sort_key(*args, **kwargs):
        calls["sort"] += 1
        return original_sort_key(*args, **kwargs)

    monkeypatch.setattr(service, "_board_sort_key_for_quote", counted_sort_key)

    first = service.terminal(board_level=3, page=1, page_size=20, fast=True)
    second = service.terminal(board_level=3, page=1, page_size=20, fast=True)

    assert calls["sort"] <= len(quotes)
    assert quote_calls["count"] == 1
    assert first.stock_board.items[0].price == 89
    assert second.stock_board.items[0].price == 89


def test_fast_terminal_does_not_copy_full_context_for_visible_quote_updates(tmp_path, monkeypatch):
    quotes = [make_quote(f"{index:06d}", index / 10, amount=10_000_000 + index) for index in range(1, 41)]
    watch = WatchlistItem(code="000040", name="测试000040", sector="测试板块", tags=[])
    service = make_service(tmp_path, watchlist=[watch])
    context = make_context(quotes, watchlist=[watch])
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False},
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update({"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False})
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(
        service,
        "_context_with_quote_overrides",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fast path must not copy full quote context")),
    )
    monkeypatch.setattr(
        service.data_source,
        "fetch_quote_subset",
        lambda codes, base_quotes=None: {
            "000040": quotes[-1].model_copy(update={"price": 40.4, "change_pct": 4.04, "updated_at": "10:00:04"})
        },
        raising=False,
    )

    payload = service.terminal(board_level=3, page=1, page_size=20, fast=True)

    assert payload.market.updated_at == "10:00:04"
    assert payload.watchlist_preview[0]["price"] == 40.4
    assert payload.source_status["visible_quote_refresh_count"] == 1


def test_fast_terminal_visible_quote_refresh_uses_latency_budget(tmp_path, monkeypatch):
    import time

    quotes = [make_quote(f"{index:06d}", index / 10, amount=10_000_000 + index) for index in range(1, 41)]
    service = make_service(tmp_path)
    service.settings.visible_quote_refresh_budget_ms = 50
    service.settings.visible_quote_min_interval_seconds = 0.2
    # 盘中载荷共享缓存默认 1s；这里缩短到 0.2s，让第二次调用越过 TTL 触发重建，
    # 测试聚焦可见行情后台刷新语义而非载荷缓存。
    service.settings.terminal_payload_live_cache_seconds = 0.2
    context = make_context(quotes)
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False},
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update({"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False})
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)

    def slow_subset(codes, base_quotes=None):
        time.sleep(0.3)
        return {
            str(codes[0]).zfill(6): quotes[0].model_copy(update={"price": 77.7, "updated_at": "10:00:07"})
        }

    monkeypatch.setattr(service.data_source, "fetch_quote_subset", slow_subset, raising=False)

    started_at = time.perf_counter()
    first = service.terminal(board_level=3, page=1, page_size=20, fast=True)
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    time.sleep(0.35)
    second = service.terminal(board_level=3, page=1, page_size=20, fast=True)

    assert elapsed_ms < 180
    assert first.source_status["visible_quote_refresh_mode"] == "refreshing"
    assert first.stock_board.items[0].price != 77.7
    assert second.source_status["visible_quote_refresh_mode"] in {"throttled_cache", "refreshing_cache", "cache"}
    assert second.stock_board.items[0].price == 77.7


def test_terminal_payload_cache_is_shared_across_calls_in_live_window(tmp_path, monkeypatch):
    """盘中 live：同一视图的 terminal 载荷在 TTL 内只构建一次，所有调用方共享。"""
    import time

    quotes = [make_quote(f"{index:06d}", index / 10, amount=10_000_000 + index) for index in range(1, 41)]
    service = make_service(tmp_path)
    context = make_context(quotes)
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False},
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update({"active_source": "easy_tdx", "trade_date": "20260812", "clock_label": "10:00:00", "frozen": False})
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service.data_source, "fetch_quote_subset", lambda *args, **kwargs: {}, raising=False)

    builds = {"count": 0}
    original_builder = service._terminal_fast_payload_for_context

    def counting_builder(*args, **kwargs):
        builds["count"] += 1
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(service, "_terminal_fast_payload_for_context", counting_builder)

    first = service.terminal(board_level=3, page=1, page_size=20, fast=True)
    service.terminal(board_level=3, page=1, page_size=20, fast=True)
    assert builds["count"] == 1

    # 命中返回深拷贝：连接级字段挂载不污染共享缓存
    first.watchlist_codes.append("000001")
    third = service.terminal(board_level=3, page=1, page_size=20, fast=True)
    assert builds["count"] == 1
    assert third.watchlist_codes == []

    # 不同视图参数各自独立缓存
    service.terminal(board_level=3, page=2, page_size=20, fast=True)
    assert builds["count"] == 2

    # TTL 过期后同一视图重建一次
    service.settings.terminal_payload_live_cache_seconds = 0.2
    time.sleep(0.25)
    service.terminal(board_level=3, page=2, page_size=20, fast=True)
    assert builds["count"] == 3


def test_sector_mini_charts_are_limited_to_front_rows_and_selected_sector(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    sectors = [
        make_sector(10, leader_code=f"{index:06d}").model_copy(update={"name": f"板块{index:03d}"})
        for index in range(1, 121)
    ]
    context = make_context([make_quote("000001", 1.0)], themes=[])
    requested: list[str] = []

    def fake_sector_series(trade_date, sector_names, max_rows=180):
        requested.extend(sector_names)
        return {
            name: [
                {"captured_at": "09:30:00", "avg_change_pct": 0.0, "heat_score": 50, "flow_delta": 0},
                {"captured_at": "09:31:00", "avg_change_pct": 1.0, "heat_score": 55, "flow_delta": 1},
            ]
            for name in sector_names
        }

    monkeypatch.setattr(service.trajectory_store, "sector_feature_series_by_name", fake_sector_series)

    decorated = service._decorate_sector_mini_charts(
        context,
        sectors,
        preferred_names={"板块120"},
    )

    assert len(requested) <= 49
    assert "板块001" in requested
    assert "板块120" in requested
    assert decorated[0].mini_chart.point_count == 2
    assert decorated[-1].mini_chart.point_count == 2
    assert decorated[60].mini_chart.source_quality == "unavailable"


def test_bootstrapped_terminal_uses_trajectory_sector_flow(tmp_path, monkeypatch):
    quotes = [
        make_quote("000001", 4.0, amount=300_000_000, minute_amount_ratio=2.0),
        make_quote("000002", 2.0, amount=200_000_000),
    ]
    service = make_service(tmp_path)
    context = make_context(quotes, themes=[])
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=context.snapshot.indices,
        data_mode="local_trajectory",
        source_status={
            **context.snapshot.source_status,
            "active_source": "local_trajectory_bootstrap",
            "frozen": False,
        },
    )
    context.source_status.update({"active_source": "local_trajectory_bootstrap", "bootstrap": True, "frozen": False})
    # 三帧轨迹：价格与累计成交额逐帧上行，个股 tick 增量 = 0.6亿 + 0.4亿 = 1.0亿/分钟
    frames = [
        ("09:30:00", [make_quote("000001", 4.0, amount=300_000_000, minute_amount_ratio=2.0),
                      make_quote("000002", 2.0, amount=200_000_000)]),
        ("09:31:00", [make_quote("000001", 5.0, amount=360_000_000, minute_amount_ratio=2.0),
                      make_quote("000002", 3.0, amount=240_000_000)]),
        ("09:32:00", [make_quote("000001", 6.0, amount=420_000_000, minute_amount_ratio=2.0),
                      make_quote("000002", 4.0, amount=280_000_000)]),
    ]
    for captured_at, frame_quotes in frames:
        service.trajectory_store.record_context(
            trade_date="20260806",
            captured_at=captured_at,
            updated_at=captured_at,
            frozen=False,
            source_quality="test",
            market=context.market,
            sectors=[make_sector(len(quotes))],
            quotes=frame_quotes,
            signals=context.signals_all,
        )
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-10T09:31:00",
        sectors=[make_sector(len(quotes)).model_copy(update={"board_code": "881001", "board_level": 3})],
        name_to_code={"测试板块": "881001"},
        code_to_name={"881001": "测试板块"},
        members_by_code={"881001": [quote.code for quote in quotes]},
    )
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: False)
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)
    monkeypatch.setattr(
        service.data_source,
        "fetch_minute_series",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bootstrap terminal should use local trajectory")),
    )

    payload = service.terminal(board_level=3, page_size=20)

    # 首帧不阻塞：sector_flow 先为空，后台从本地轨迹重建后随增量带出
    assert payload.sector_flow == []
    # 本地轨迹读取已优化到毫秒级，后台线程可能在断言前就跑完并把自己
    # 从登记表里弹出——以结果为准：轮询等待重建完成（fetch_minute_series
    # 仍被 monkeypatch 为直接抛错，若走了分钟线源会立刻炸）。
    import time

    deadline = time.time() + 10
    payload = service.terminal(board_level=3, page_size=20)
    while not payload.sector_flow and time.time() < deadline:
        for thread in list(service._sector_flow_refresh_threads.values()):
            thread.join(timeout=0.5)
        time.sleep(0.05)
        payload = service.terminal(board_level=3, page_size=20)

    assert payload.sector_flow
    series = payload.sector_flow[0]
    # 统一净流入口径：每分钟全成员成交额增量×方向（亿），不再是热度分轨迹
    assert series.flow_basis == "每分钟净流入(全成员成交额增量×方向)"
    assert [point.value for point in series.points] == [1.0, 1.0]
    assert series.final_value == 2.0


def test_bootstrapped_terminal_refreshes_visible_quotes(tmp_path, monkeypatch):
    quotes = [
        make_quote("000001", 1.0, amount=300_000_000),
        make_quote("000002", 2.0, amount=200_000_000),
    ]
    service = make_service(tmp_path)
    context = make_context(quotes, themes=[])
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=context.snapshot.indices,
        data_mode="local_trajectory",
        source_status={
            **context.snapshot.source_status,
            "active_source": "local_trajectory_bootstrap",
            "frozen": False,
            "trade_date": "20260812",
            "clock_label": "10:00:00",
        },
    )
    context.market = context.market.model_copy(update={"frozen": False, "updated_at": "10:00:00"})
    context.source_status.update(
        {
            "active_source": "local_trajectory_bootstrap",
            "bootstrap": True,
            "frozen": False,
            "trade_date": "20260812",
            "clock_label": "10:00:00",
        }
    )
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-12T10:00:00",
        sectors=[make_sector(len(quotes)).model_copy(update={"board_code": "881001", "board_level": 3})],
        name_to_code={"测试板块": "881001"},
        code_to_name={"881001": "测试板块"},
        members_by_code={"881001": [quote.code for quote in quotes]},
    )
    requested: list[str] = []

    def fake_subset(codes, base_quotes=None):
        requested.extend(codes)
        return {
            "000001": quotes[0].model_copy(update={"price": 12.34, "change_pct": 3.4, "updated_at": "10:00:03"})
        }

    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)
    monkeypatch.setattr(service.data_source, "fetch_quote_subset", fake_subset, raising=False)

    payload = service.terminal(board_level=3, page_size=20)

    assert "000001" in requested
    assert payload.stock_board.items[0].price == 12.34
    assert payload.stock_board.updated_at == "10:00:03"
    assert payload.source_status["visible_quote_refresh_count"] == 1


def test_bootstrapped_terminal_skips_trajectory_sector_flow_during_trading_window(tmp_path, monkeypatch):
    quotes = [make_quote("000001", 1.0), make_quote("000002", 2.0)]
    service = make_service(tmp_path)
    context = make_context(quotes, themes=[])
    context.snapshot = MarketSnapshot(
        quotes=context.snapshot.quotes,
        indices=context.snapshot.indices,
        data_mode="local_trajectory",
        source_status={**context.snapshot.source_status, "active_source": "local_trajectory_bootstrap", "frozen": False},
    )
    context.source_status.update({"active_source": "local_trajectory_bootstrap", "bootstrap": True, "frozen": False})
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-12T10:00:00",
        sectors=[make_sector(len(quotes)).model_copy(update={"board_code": "881001", "board_level": 3})],
        name_to_code={"测试板块": "881001"},
        code_to_name={"881001": "测试板块"},
        members_by_code={"881001": [quote.code for quote in quotes]},
    )

    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr("app.services.is_trading_window", lambda: True)
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)
    monkeypatch.setattr(service.data_source, "fetch_quote_subset", lambda codes, base_quotes=None: {})
    monkeypatch.setattr(
        service.trajectory_store,
        "sector_feature_series_by_name",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sector trajectory should not block intraday terminal")),
    )

    payload = service.terminal(board_level=3, page_size=20)

    assert payload.sector_flow == []


def test_sector_rank_uses_official_easy_tdx_board_grouping(tmp_path, monkeypatch):
    quotes = [
        make_quote("000001", 4.0, amount=300_000_000),
        make_quote("000002", 2.0, amount=200_000_000),
    ]
    service = make_service(tmp_path)
    context = make_context(quotes, themes=[])
    context.sectors = [make_sector(len(quotes)).model_copy(update={"name": "X140401"})]
    board_context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-12T10:00:00",
        sectors=[make_sector(len(quotes)).model_copy(update={"name": "官方板块", "board_code": "881001", "board_level": 3})],
        name_to_code={"官方板块": "881001"},
        code_to_name={"881001": "官方板块"},
        members_by_code={"881001": [quote.code for quote in quotes]},
    )

    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service.data_source, "fetch_board_context", lambda board_level=3: board_context)

    sectors = service.sector_rank(board_level=3)

    assert sectors[0].name == "官方板块"
    assert sectors[0].board_code == "881001"
    assert sectors[0].board_source == "easy_tdx_cached_members_local_quote_aggregation"
    assert all(not sector.name.startswith("X") for sector in sectors)


def test_stock_types_are_dynamic_and_follow_priority(tmp_path):
    service = make_service(tmp_path, themes=[{"name": "测试板块", "core_codes": ["000002"]}])
    sector_snapshot = make_sector(10, leader_code="000001")

    leader = make_quote("000001", 4.0)
    core = make_quote("000002", 3.0)
    limit = make_quote("000003", 9.9, limit_up=True)
    attack = make_quote("000004", 2.0, minute_amount_ratio=2.0)
    pressure = make_quote("000005", -2.0)

    assert service._classify_stock(leader, sector_snapshot, {"000002"})[0] == "板块龙头"
    assert service._classify_stock(core, sector_snapshot, {"000002"})[0] == "核心容量"
    assert service._classify_stock(limit, sector_snapshot, set())[0] == "涨停/回封"
    assert service._classify_stock(attack, sector_snapshot, set())[0] == "大单进攻"
    assert service._classify_stock(pressure, sector_snapshot, set())[0] == "掉队/抛压"


def test_sector_structure_tags_follow_market_convention(tmp_path):
    """板块龙头=板块领涨股（涨幅第一且为正）；核心容量=板块中军（流通市值第一）或显式 core。"""
    service = make_service(tmp_path)
    sector = make_sector(10, leader_code="000001").model_copy(
        update={"capacity_leader_code": "000009", "capacity_leader_name": "测试000009"}
    )

    # 涨幅第一名为负：领跌不算龙头
    falling_top = make_quote("000001", -0.5)
    assert service._classify_stock(falling_top, sector, set())[0] != "板块龙头"

    # 板块中军（流通市值第一）：市值口径，不看涨跌与成交额
    capacity_stock = make_quote("000009", 0.5, amount=50_000_000)
    assert service._classify_stock(capacity_stock, sector, set())[0] == "核心容量"

    # 非中军、无显式 core：成交额再大也不挂核心容量
    member = make_quote("000010", 1.0, amount=800_000_000)
    assert "核心容量" not in service._classify_stock(member, sector, set())[1]

    # 手工主题/自选显式 core 不受板块口径影响
    assert service._classify_stock(member, sector, {"000010"})[0] == "核心容量"


def test_capacity_leader_prefers_float_mcap_and_falls_back_to_amount(tmp_path):
    """中军判定：有流通市值数据取市值第一；无数据回退成交额第一。"""
    service = make_service(tmp_path)
    high_turnover_small_cap = make_quote("000001", 2.0, amount=900_000_000)
    big_cap = make_quote("000002", 0.5, amount=100_000_000)

    quotes = [high_turnover_small_cap, big_cap]
    # 有市值数据：大市值但成交额小的票是中军（市场"容量核心"严格口径）
    leader = service._capacity_leader(quotes, {"000001": 5e9, "000002": 5e10})
    assert leader is not None and leader.code == "000002"
    # 市值数据缺失：回退成交额第一（盘口代理）
    leader = service._capacity_leader(quotes, {})
    assert leader is not None and leader.code == "000001"
    # 部分覆盖：只用有市值数据的成员比
    leader = service._capacity_leader(quotes, {"000001": 5e9})
    assert leader is not None and leader.code == "000001"


def test_easy_tdx_order_flow_accepts_five_level_volume_aliases(tmp_path):
    settings = AppSettings()
    source = EasyTdxMarketDataSource(settings, EasyTdxDailyDataSource(settings))
    raw = {
        "bid1": 10.10,
        "bid2": 10.09,
        "bid3": 10.08,
        "bid4": 10.07,
        "bid5": 10.06,
        "ask1": 10.11,
        "ask2": 10.12,
        "ask3": 10.13,
        "ask4": 10.14,
        "ask5": 10.15,
        "bid_vol1": 1000,
        "bid_vol2": 900,
        "bid_vol3": 800,
        "bid_vol4": 700,
        "bid_vol5": 600,
        "ask_vol1": 100,
        "ask_vol2": 100,
        "ask_vol3": 100,
        "ask_vol4": 100,
        "ask_vol5": 100,
        "b_vol": 6000,
        "s_vol": 2000,
    }

    observation = source._order_flow_from_raw(raw, price=10.10, open_price=10.00, minute_amount_ratio=1.8)

    assert observation.available is True
    assert len(observation.levels) == 10
    assert observation.bid_depth_amount > observation.ask_depth_amount
    assert observation.direction in {"买盘增强", "放量承接"}
    assert observation.confidence.startswith("中等")
    assert observation.data_quality == "l1_five_level"
    assert observation.level2_available is False
    assert observation.active_imbalance_pct > 0
    assert "队列" in observation.disclaimer


def test_easy_tdx_transaction_flow_is_l1_and_detects_large_directional_trades():
    source = EasyTdxMarketDataSource(AppSettings(), EasyTdxDailyDataSource(AppSettings()))
    rows = [
        {"time": "09:31", "price": 10.00, "vol": 100},
        {"time": "09:32", "price": 10.10, "vol": 1_000},
        {"time": "09:33", "price": 10.05, "vol": 50},
        {"time": "09:34", "price": 10.20, "vol": 3_000},
        {"time": "09:35", "price": 10.20, "vol": 500},
    ]

    observation = source._transaction_flow_from_rows(
        "300476",
        "20260807",
        rows,
        "easy_tdx_history_transaction_data",
    )

    assert observation.available is True
    assert observation.data_quality == "l1_transaction"
    assert observation.buy_amount > observation.sell_amount
    assert observation.large_buy_count >= 1
    assert observation.score > 0
    assert [point.time for point in observation.points] == ["09:31", "09:32", "09:33", "09:34", "09:35"]
    assert observation.points[0].rolling_score == 0
    assert observation.points[1].rolling_score > 0
    assert [trade.time for trade in observation.recent_trades[:3]] == ["09:35", "09:34", "09:33"]
    assert observation.recent_trades[1].side_label == "买"
    assert observation.recent_trades[1].price == 10.2
    assert observation.recent_trades[1].large is True
    assert "不是委托队列" in observation.note


def test_easy_tdx_transaction_flow_prefers_l1_direction_field_and_neutralizes_special_values():
    source = EasyTdxMarketDataSource(AppSettings(), EasyTdxDailyDataSource(AppSettings()))
    rows = [
        {"time": "09:31", "price": 10.00, "vol": 100, "buyorsell": 1},
        {"time": "09:32", "price": 10.10, "vol": 1_000, "buyorsell": 1},
        {"time": "09:33", "price": 10.05, "vol": 50, "buyorsell": 0},
        {"time": "15:29", "price": 10.05, "vol": 500, "buyorsell": 5},
    ]

    observation = source._transaction_flow_from_rows(
        "300476",
        "20260807",
        rows,
        "easy_tdx_history_transaction_data",
    )

    # 09:32 is an up-tick, but the explicit TDX L1 field marks it as sell.
    assert observation.sell_amount > observation.buy_amount
    assert observation.neutral_volume == 0
    assert observation.as_of == "09:33"
    assert observation.confidence.startswith("中等：TDX L1")
    assert [trade.side_label for trade in observation.recent_trades] == ["买", "卖", "卖"]
    assert any("盘前/盘后成交 1笔已排除" in item for item in observation.evidence)


def test_easy_tdx_recent_trade_time_uses_clock_part_not_date_prefix():
    source = EasyTdxMarketDataSource(AppSettings(), EasyTdxDailyDataSource(AppSettings()))

    row = source._transaction_row_from_tick(
        {"time": "2026-08-11 13:26:07", "price": 42.5, "vol": 100, "buyorsell": 0}
    )
    observation = source._transaction_flow_from_rows(
        "600206",
        "20260811",
        [row],
        "easy_tdx_history_transaction_data",
    )

    assert observation.recent_trades[0].time == "13:26:07"


def test_easy_tdx_capabilities_do_not_claim_queue_data():
    source = EasyTdxMarketDataSource(AppSettings(), EasyTdxDailyDataSource(AppSettings()))

    capabilities = source.capabilities()

    assert capabilities["five_level_order_book"] is True
    assert capabilities["quote_depth"] is True
    assert capabilities["transaction_tape"] is True
    assert capabilities["level2_available"] is False
    assert capabilities["auction_proxy"] is True
    assert capabilities["auction_series"] is True
    assert capabilities["auction_0925"] is True
    assert capabilities["auction_0925_direct"] is False
    assert capabilities["transaction_data"] is True


def test_dashboard_capability_status_keeps_quote_depth_and_tape_distinct(tmp_path):
    service = make_service(tmp_path)

    status = service._public_market_capability_status()

    assert status["order_book_capability"] == "quote_depth"
    assert status["quote_depth"] is True
    assert status["transaction_tape"] is True
    assert status["level2_available"] is False
    assert "委托队列" in status["level2_note"]
    assert status["auction_proxy"] is True


def test_easy_tdx_explicit_auction_fields_are_marked_actual():
    source = EasyTdxMarketDataSource(AppSettings(), EasyTdxDailyDataSource(AppSettings()))
    raw = {
        "auction_price": 10.25,
        "last_close": 10.0,
        "auction_volume": 2500,
        "auction_amount": 2_562_500,
        "unmatched_buy": 1200,
        "unmatched_sell": 300,
    }

    auction = source._auction_from_raw(raw, prev_close=10.0)

    assert auction.available is True
    assert auction.data_quality == "actual"
    assert auction.change_pct == 2.5
    assert auction.unmatched_buy_volume > auction.unmatched_sell_volume
    assert auction.order_imbalance_pct > 0


def test_auction_tracker_derives_upward_price_trajectory(tmp_path):
    settings = AppSettings()
    settings.auction_history_file = tmp_path / "auction.jsonl"
    tracker = AuctionSnapshotTracker(settings)
    base = AuctionSnapshot(
        available=True,
        source="tdx_l1_preopen_quote",
        data_quality="proxy",
        trade_date="20260810",
        as_of="09:22:00",
        price=10.00,
        prev_close=9.90,
        change_pct=1.01,
        volume=1000,
        amount=1_000_000,
        order_imbalance_pct=5,
        phase="call_auction",
        indicative=True,
    )

    first = tracker.observe("000001", base)
    second = tracker.observe(
        "000001",
        base.model_copy(
            update={
                "as_of": "09:24:00",
                "price": 10.05,
                "change_pct": 1.52,
                "volume": 1800,
                "order_imbalance_pct": 12,
            }
        ),
    )

    assert first.snapshot_count == 1
    assert second.snapshot_count == 2
    assert second.price_slope_pct > 0
    assert second.volume_delta == 800
    assert second.trajectory == "竞价上修"
    assert len(tracker.history("000001", "20260810")) == 2


def test_easy_tdx_close_quote_does_not_claim_live_order_flow(tmp_path):
    source = EasyTdxDailyDataSource(AppSettings())
    meta = StockMeta(code="300476", ts_code="300476.SZ", name="测试胜宏", market="创业板")
    quote = source._quote_from_daily(
        "300476",
        meta,
        {
            "open": 10,
            "high": 11,
            "low": 9.8,
            "close": 10.8,
            "pre_close": 10,
            "pct_chg": 8,
            "vol": 1000,
            "amount": 2000,
        },
        "20260806",
    )

    assert quote.order_flow.available is False
    assert quote.order_flow.direction == "无盘口"


def test_easy_tdx_preopen_quote_builds_labeled_auction_proxy(monkeypatch):
    source = EasyTdxMarketDataSource(AppSettings(), EasyTdxDailyDataSource(AppSettings()))
    monkeypatch.setattr("app.data_sources.is_preopen_window", lambda now=None: True)
    raw = {
        "price": 10.35,
        "last_close": 10.0,
        "cur_vol": 2500,
        "bid_vol1": 5000,
        "bid_vol2": 3000,
        "ask_vol1": 1000,
        "ask_vol2": 800,
    }

    auction = source._auction_from_raw(raw, prev_close=10.0)

    assert auction.available is True
    assert auction.source == "tdx_l1_preopen_quote"
    assert auction.data_quality == "proxy"
    assert auction.change_pct == 3.5
    assert auction.order_imbalance_pct > 0
    assert "09:30" in auction.note


def test_live_fetch_prefers_easy_tdx_current_auction_over_proxy(tmp_path, monkeypatch):
    settings = AppSettings()
    settings.auction_history_file = tmp_path / "auction.jsonl"
    close_source = EasyTdxDailyDataSource(settings)
    source = EasyTdxMarketDataSource(settings, close_source)
    today = RealDateTime.now().strftime("%Y%m%d")
    meta = StockMeta(code="300476", ts_code="300476.SZ", name="测试胜宏", market="创业板")
    seed_quote = make_quote("300476", 1.0, auction=AuctionSnapshot())
    live_quote = seed_quote.model_copy(
        update={
            "price": 10.25,
            "change_pct": 2.5,
            "auction": AuctionSnapshot(
                available=True,
                source="tdx_l1_preopen_quote",
                data_quality="proxy",
                trade_date=today,
                price=10.20,
                prev_close=10.0,
                change_pct=2.0,
            ),
        }
    )
    seed_snapshot = MarketSnapshot(
        quotes=[seed_quote],
        indices=[],
        data_mode="closed_static",
        source_status={"active_source": "easy_tdx_daily_close"},
    )

    class FakeTdxClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

    class FakeMacClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

    monkeypatch.setattr(close_source, "fetch", lambda universe: seed_snapshot)
    monkeypatch.setattr(source, "_client", lambda: FakeTdxClient())
    monkeypatch.setattr(source, "_should_fetch_current_easy_tdx_auction", lambda now, universe_size: True)
    monkeypatch.setattr(source, "_mac_client", lambda: FakeMacClient())
    monkeypatch.setattr(source, "_fetch_quotes", lambda api, universe: ([live_quote], {"chunks": 1}))
    monkeypatch.setattr(source, "_fetch_indices", lambda api, seed: [])
    monkeypatch.setattr(
        source,
        "_fetch_current_auction_snapshot",
        lambda client, code, prev_close, trade_date: AuctionSnapshot(
            available=True,
            source="easy_tdx_auction",
            data_quality="actual",
            trade_date=trade_date,
            as_of="09:24:57",
            price=10.30,
            prev_close=10.0,
            change_pct=3.0,
            volume=20_000,
            amount=206_000,
            status="实时集合竞价明细 direction_raw=buy",
            confidence="较高：easy_tdx 集合竞价明细",
        ),
    )

    snapshot = source.fetch({"300476": meta})

    auction = snapshot.quotes[0].auction
    assert auction.available is True
    assert auction.source == "easy_tdx_auction"
    assert auction.data_quality == "actual"
    assert auction.change_pct == 3.0
    assert snapshot.source_status["auction_source"] == "easy_tdx_auction"


def test_live_fetch_continues_when_daily_seed_unavailable(monkeypatch):
    settings = AppSettings()
    close_source = EasyTdxDailyDataSource(settings)
    source = EasyTdxMarketDataSource(settings, close_source)
    meta = StockMeta(code="300476", ts_code="300476.SZ", name="测试胜宏", market="创业板")
    live_quote = make_quote("300476", 2.5)

    class FakeTdxClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

    monkeypatch.setattr(
        close_source,
        "fetch_seed",
        lambda universe: (_ for _ in ()).throw(data_sources.DataSourceError("easy_tdx指数日K没有可用交易日。")),
    )
    monkeypatch.setattr(source, "_client", lambda: FakeTdxClient())
    monkeypatch.setattr(source, "_fetch_quotes", lambda api, universe: ([live_quote], {"chunks": 1, "raw_quote_count": 1}))
    monkeypatch.setattr(source, "_fetch_indices", lambda api, seed: [])
    monkeypatch.setattr(source, "_should_fetch_current_easy_tdx_auction", lambda now, universe_size: False)

    snapshot = source.fetch({"300476": meta})

    assert snapshot.data_mode == "live"
    assert [quote.code for quote in snapshot.quotes] == ["300476"]
    assert snapshot.source_status["active_source"] == "easy_tdx"
    assert snapshot.source_status["quote_count"] == 1
    assert snapshot.source_status["seed_source"] == "unavailable"
    assert "easy_tdx指数日K没有可用交易日" in snapshot.source_status["seed_error"]

def test_first_live_index_snapshot_cannot_promote_formal_buy_t(tmp_path):
    service = make_service(tmp_path)
    snapshot = MarketSnapshot(
        quotes=[],
        indices=[],
        data_mode="live",
        source_status={"active_source": "easy_tdx"},
    )
    market = MarketState(
        trend="分歧转强",
        emotion_score=70,
        breadth_pct=60,
        index_turning=True,
        amount_expanding=True,
        mainline="测试板块",
        indices=[],
        reasons=["指数从低位反弹"],
        updated_at="09:33:00",
        index_turning_mode="snapshot_rebound_proxy",
    )

    guarded = service._market_for_signals(snapshot, market)
    confirmed = service._market_for_signals(
        snapshot,
        market.model_copy(update={"index_turning_mode": "rolling_turn"}),
    )

    assert guarded.index_turning is False
    assert any("等待下一分钟" in reason for reason in guarded.reasons)
    assert confirmed.index_turning is True


def test_closed_context_is_cached_without_repeated_source_scan(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    quote = make_quote("000001", 2.0)
    snapshot = MarketSnapshot(
        quotes=[quote],
        indices=[
            IndexSnapshot(
                code="000001",
                name="上证指数",
                price=3300,
                prev_close=3290,
                open=3290,
                high=3310,
                low=3280,
                change_pct=0.3,
                rebound_from_low_pct=0.6,
                minute_amount_ratio=1.2,
                amount=100,
            )
        ],
        data_mode="closed_static",
        source_status={"active_source": "test", "trade_date": "20260806", "frozen": True, "clock_label": "15:00:00"},
    )
    calls = {"fetch": 0}

    def fetch(watchlist, themes):
        calls["fetch"] += 1
        return snapshot

    service.data_source.fetch = fetch
    monkeypatch.setattr("app.services.is_trading_window", lambda: False)

    service.dashboard()
    service.dashboard()
    assert calls["fetch"] == 1

    service._context_cache_at -= service.settings.terminal_context_frozen_cache_seconds + 1
    service.dashboard()
    assert calls["fetch"] == 2

