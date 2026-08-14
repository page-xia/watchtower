from datetime import datetime as RealDateTime, timezone as RealTimezone
import sys
import types

import app.data_sources as ds


class FixedDatetime:
    @classmethod
    def now(cls, tz=None):  # noqa: D401, ARG003
        return RealDateTime(2026, 8, 6, 16, 5, 0)


class SundayDatetime:
    @classmethod
    def now(cls, tz=None):  # noqa: D401, ARG003
        return RealDateTime(2026, 8, 9, 10, 5, 0)


class HolidayTradingWindowDatetime:
    @classmethod
    def now(cls, tz=None):  # noqa: D401, ARG003
        return RealDateTime(2026, 8, 10, 10, 5, 0)


class LunchDatetime:
    @classmethod
    def now(cls, tz=None):  # noqa: D401, ARG003
        return RealDateTime(2026, 8, 10, 12, 15, 0)


class UtcContainerDatetime:
    @classmethod
    def now(cls, tz=None):  # noqa: D401
        utc_now = RealDateTime(2026, 8, 14, 2, 23, 0, tzinfo=RealTimezone.utc)
        if tz is not None:
            return utc_now.astimezone(tz)
        return utc_now.replace(tzinfo=None)


def test_lunch_break_stays_inside_intraday_data_window(monkeypatch) -> None:
    monkeypatch.setattr(ds, "datetime", LunchDatetime)

    assert ds.market_session() == "lunch_break"
    assert ds.is_trading_window() is True


def test_trading_window_defaults_to_china_timezone_for_utc_container(monkeypatch) -> None:
    monkeypatch.setattr(ds, "datetime", UtcContainerDatetime)

    assert ds.market_session() == "morning"
    assert ds.is_trading_window() is True


def test_easy_tdx_index_quote_corrects_shanghai_scale(monkeypatch) -> None:
    settings = ds.AppSettings()
    source = ds.EasyTdxMarketDataSource(settings, ds.EasyTdxDailyDataSource(settings))
    monkeypatch.setattr(ds, "easy_tdx_market_for_code", lambda code, index=False: code)

    class FakeApi:
        def get_security_quotes(self, request):  # noqa: ARG002
            return [
                {
                    "code": "000001",
                    "price": 394.959,
                    "pre_close": 394.004,
                    "open": 394.382,
                    "high": 396.636,
                    "low": 393.863,
                    "amount": 912_711_155_712,
                    "decimal_point": 3,
                },
                {
                    "code": "399001",
                    "price": 14189.09,
                    "pre_close": 14311.01,
                    "open": 14348.95,
                    "high": 14373.77,
                    "low": 14102.66,
                    "amount": 1_057_321_517_056,
                    "decimal_point": 2,
                },
            ]

    seeds = [
        ds.IndexSnapshot(
            code="000001",
            name="上证指数",
            price=3940.04,
            prev_close=3900.35,
            open=3896.49,
            high=3940.93,
            low=3885.62,
            change_pct=1.02,
            rebound_from_low_pct=1.4,
            amount=1,
        ),
        ds.IndexSnapshot(
            code="399001",
            name="深证成指",
            price=14311.01,
            prev_close=14110.12,
            open=14152.78,
            high=14396.07,
            low=14049.34,
            change_pct=1.42,
            rebound_from_low_pct=1.86,
            amount=1,
        ),
    ]

    indices = {item.code: item for item in source._fetch_indices(FakeApi(), seeds)}

    assert indices["000001"].price == 3949.59
    assert indices["000001"].prev_close == 3940.04
    assert indices["000001"].open == 3943.82
    assert indices["000001"].change_pct == 0.24
    assert indices["399001"].price == 14189.09


def test_easy_tdx_index_quote_survives_missing_daily_seed(monkeypatch) -> None:
    settings = ds.AppSettings()
    source = ds.EasyTdxMarketDataSource(settings, ds.EasyTdxDailyDataSource(settings))
    monkeypatch.setattr(ds, "easy_tdx_market_for_code", lambda code, index=False: code)

    class FakeApi:
        def get_security_quotes(self, request):  # noqa: ARG002
            return [
                {
                    "code": "000001",
                    "price": 394.959,
                    "pre_close": 394.004,
                    "open": 394.382,
                    "high": 396.636,
                    "low": 393.863,
                    "amount": 912_711_155_712,
                    "decimal_point": 3,
                },
                {
                    "code": "399001",
                    "price": 14189.09,
                    "pre_close": 14311.01,
                    "open": 14348.95,
                    "high": 14373.77,
                    "low": 14102.66,
                    "amount": 1_057_321_517_056,
                    "decimal_point": 2,
                },
            ]

    indices = {item.code: item for item in source._fetch_indices(FakeApi(), [])}

    assert indices["000001"].name == "上证指数"
    assert indices["000001"].price == 3949.59
    assert indices["000001"].prev_close == 3940.04
    assert indices["000001"].change_pct == 0.24
    assert indices["399001"].name == "深证成指"
    assert indices["399001"].price == 14189.09


def test_easy_tdx_uses_history_transaction_api_on_sunday(monkeypatch) -> None:
    settings = ds.AppSettings()
    source = ds.EasyTdxMarketDataSource(settings, ds.EasyTdxDailyDataSource(settings))
    calls = {"live": 0, "history": 0, "date": None}

    class FakePage:
        def __init__(self, ticks):
            self.ticks = ticks
            self.count = len(ticks)

    class FakeTrades:
        def today(self, code, start=0, count=1800, include_raw=False):  # noqa: ARG002
            calls["live"] += 1
            raise AssertionError("Sunday must not use the live transaction endpoint")

        def history(self, code, trade_date, start=0, count=2000, include_raw=False):  # noqa: ARG002
            calls["history"] += 1
            calls["date"] = int(trade_date)
            return FakePage(
                [
                    types.SimpleNamespace(time_label="09:30", price=10.0, volume=100, side="buy"),
                    types.SimpleNamespace(time_label="15:29", price=10.0, volume=50, side="neutral"),
                ]
            )

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.trades = FakeTrades()
            self.auctions = types.SimpleNamespace(series=lambda code, include_raw=False: None)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

    easy_tdx_module = types.ModuleType("easy_tdx")
    easy_tdx_module.TdxClient = FakeClient
    monkeypatch.setitem(sys.modules, "easy_tdx", easy_tdx_module)
    monkeypatch.setattr(ds, "datetime", SundayDatetime)
    monkeypatch.setattr(ds, "is_trading_window", lambda: False)

    observation = source.fetch_transaction_flow(
        "300476",
        trade_date="20260807",
        full_session=True,
    )

    assert calls == {"live": 0, "history": 1, "date": 20260807}
    assert observation.source == "easy_tdx_history_transaction_data"
    assert observation.full_session is True
    assert observation.as_of == "09:30"
    assert observation.points[0].time == "09:30"


def test_easy_tdx_uses_history_transaction_api_when_today_is_not_known_trade_date(monkeypatch) -> None:
    settings = ds.AppSettings()
    close_source = ds.EasyTdxDailyDataSource(settings)
    monkeypatch.setattr(close_source, "_recent_trade_dates", lambda: ["20260807"])
    source = ds.EasyTdxMarketDataSource(settings, close_source)
    calls = {"live": 0, "history": 0, "date": None}

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

        def get_transaction_data(self, market, code, start=0, count=1800):  # noqa: ARG002
            calls["live"] += 1
            raise AssertionError("Non-trading day must not use live transaction tape")

        def get_history_transaction_data(self, market, code, trade_date, start=0, count=1800):  # noqa: ARG002
            calls["history"] += 1
            calls["date"] = int(trade_date)
            return [
                {"time": "09:30", "price": 10.0, "vol": 100, "buyorsell": 0},
                {"time": "09:31", "price": 10.1, "vol": 60, "buyorsell": 1},
            ]

    easy_tdx_module = types.ModuleType("easy_tdx")
    easy_tdx_module.TdxClient = FakeClient
    monkeypatch.setitem(sys.modules, "easy_tdx", easy_tdx_module)
    monkeypatch.setattr(ds, "easy_tdx_market_for_code", lambda code, index=False: 0)
    monkeypatch.setattr(ds, "datetime", HolidayTradingWindowDatetime)
    monkeypatch.setattr(ds, "is_trading_window", lambda: True)

    observation = source.fetch_transaction_flow("300476", trade_date="20260810", full_session=True)

    assert calls == {"live": 0, "history": 1, "date": 20260810}
    assert observation.source == "easy_tdx_history_transaction_data"
    assert observation.available is True


def test_replay_is_frozen_after_close(monkeypatch) -> None:
    monkeypatch.setattr(ds, "datetime", FixedDatetime)
    monkeypatch.setattr(ds, "is_trading_window", lambda: False)

    themes = [{"name": "AI硬件", "members": ["300308"], "core_codes": ["300308"]}]
    source = ds.ReplayMarketDataSource()

    monkeypatch.setattr(ds.time, "time", lambda: 111111)
    snapshot1 = source.fetch([], themes)
    monkeypatch.setattr(ds.time, "time", lambda: 999999)
    snapshot2 = source.fetch([], themes)

    assert snapshot1.data_mode == "closed_static"
    assert snapshot1.source_status["frozen"] is True
    assert snapshot1.source_status["clock_label"] == "15:00:00"
    assert snapshot1.quotes[0].price == snapshot2.quotes[0].price
    assert snapshot1.quotes[0].updated_at == snapshot2.quotes[0].updated_at == "15:00:00"


def test_auto_mode_uses_easy_tdx_close_snapshot_after_close(monkeypatch) -> None:
    settings = ds.AppSettings()
    settings.data_mode = "auto"
    settings.scan_scope = "full_market"
    router = ds.MarketDataRouter(settings)

    calls = {"close": 0, "replay": 0}

    monkeypatch.setattr(router.universe, "load", lambda watchlist, themes: ({}, {"signal_scope": "full_market", "universe_size": 0}))

    def fake_close(universe):  # noqa: ARG001
        calls["close"] += 1
        return ds.MarketSnapshot(
            quotes=[],
            indices=[],
            data_mode="closed_static",
            source_status={"active_source": "easy_tdx_daily_close", "frozen": True, "note": "ok"},
        )

    def fake_replay(watchlist, themes, universe):  # noqa: ARG001
        calls["replay"] += 1
        raise AssertionError("replay should not be used when close snapshot is available")

    monkeypatch.setattr(router.close_source, "fetch", fake_close)
    monkeypatch.setattr(router.replay, "fetch", fake_replay)
    monkeypatch.setattr(ds, "is_trading_window", lambda: False)

    snapshot = router.fetch([], [])

    assert snapshot.data_mode == "closed_static"
    assert snapshot.source_status["active_source"] == "easy_tdx_daily_close"
    assert calls["close"] == 1
    assert calls["replay"] == 0


def test_auto_mode_uses_live_snapshot_during_lunch_break(monkeypatch) -> None:
    settings = ds.AppSettings()
    settings.data_mode = "auto"
    settings.scan_scope = "full_market"
    router = ds.MarketDataRouter(settings)

    calls = {"live": 0, "close": 0}

    monkeypatch.setattr(ds, "datetime", LunchDatetime)
    monkeypatch.setattr(
        router.universe,
        "load",
        lambda watchlist, themes: ({}, {"signal_scope": "full_market", "universe_size": 0}),
    )

    def fake_live(universe):  # noqa: ARG001
        calls["live"] += 1
        return ds.MarketSnapshot(
            quotes=[],
            indices=[],
            data_mode="live",
            source_status={
                "active_source": "easy_tdx",
                "clock_label": "12:15:00",
                "frozen": False,
                "market_session": "lunch_break",
            },
        )

    def fake_close(universe):  # noqa: ARG001
        calls["close"] += 1
        raise AssertionError("lunch break must not use the daily close snapshot")

    monkeypatch.setattr(router.live, "fetch", fake_live)
    monkeypatch.setattr(router.close_source, "fetch", fake_close)

    snapshot = router.fetch([], [])

    assert snapshot.data_mode == "live"
    assert snapshot.source_status["clock_label"] == "12:15:00"
    assert snapshot.source_status["frozen"] is False
    assert snapshot.source_status["market_session"] == "lunch_break"
    assert calls == {"live": 1, "close": 0}


def test_auto_mode_does_not_fallback_to_close_when_lunch_live_fails(monkeypatch) -> None:
    settings = ds.AppSettings()
    settings.data_mode = "auto"
    settings.scan_scope = "full_market"
    router = ds.MarketDataRouter(settings)

    calls = {"live": 0, "close": 0}

    monkeypatch.setattr(ds, "datetime", LunchDatetime)
    monkeypatch.setattr(
        router.universe,
        "load",
        lambda watchlist, themes: ({}, {"signal_scope": "full_market", "universe_size": 0}),
    )

    def fake_live(universe):  # noqa: ARG001
        calls["live"] += 1
        raise ds.DataSourceError("午盘服务器暂不可用")

    def fake_close(universe):  # noqa: ARG001
        calls["close"] += 1
        raise AssertionError("intraday live failure must not become a close snapshot")

    monkeypatch.setattr(router.live, "fetch", fake_live)
    monkeypatch.setattr(router.close_source, "fetch", fake_close)

    snapshot = router.fetch([], [])

    assert snapshot.data_mode == "unavailable"
    assert snapshot.source_status["active_source"] == "easy_tdx_unavailable"
    assert snapshot.source_status["clock_label"] == "12:15:00"
    assert snapshot.source_status["frozen"] is False
    assert snapshot.source_status["market_session"] == "lunch_break"
    assert snapshot.source_status["data_quality"] == "intraday_live_unavailable"
    assert calls == {"live": 1, "close": 0}


def test_close_source_backfills_to_previous_trade_day(monkeypatch) -> None:
    settings = ds.AppSettings()
    source = ds.EasyTdxDailyDataSource(settings)
    universe = {
        "300308": ds.StockMeta(code="300308", ts_code="300308.SZ", name="中际旭创", industry="CPO"),
    }

    monkeypatch.setattr(source, "_recent_trade_dates", lambda: ["20260807", "20260806"])
    monkeypatch.setattr(source, "_load_cached_daily", lambda trade_date: [])
    monkeypatch.setattr(source, "_load_cached_indices", lambda trade_date: [])
    monkeypatch.setattr(source, "_fetch_index_rows", lambda trade_date: [{"ts_code": "000001.SH", "trade_date": trade_date, "open": 1, "high": 1, "low": 1, "close": 1, "pre_close": 1, "pct_chg": 0, "vol": 1, "amount": 1}] if trade_date == "20260806" else [])

    def fake_fetch_daily(universe, trade_date: str):  # noqa: ARG001
        if trade_date == "20260807":
            return []
        return [
            {
                "ts_code": "300308.SZ",
                "trade_date": trade_date,
                "open": 94.0,
                "high": 99.5,
                "low": 93.7,
                "close": 98.2,
                "pre_close": 100.0,
                "pct_chg": -1.8,
                "vol": 1000,
                "amount": 2000,
            }
        ]

    monkeypatch.setattr(source, "_fetch_daily_rows", fake_fetch_daily)

    snapshot = source.fetch(universe)

    assert snapshot.source_status["active_source"] == "easy_tdx_daily_close"
    assert snapshot.source_status["trade_date"] == "20260806"
    assert snapshot.source_status["empty_trade_dates_skipped"] == ["20260807"]
    assert snapshot.quotes[0].code == "300308"


def test_close_source_builds_explicit_historical_snapshot(monkeypatch) -> None:
    settings = ds.AppSettings()
    source = ds.EasyTdxDailyDataSource(settings)
    universe = {
        "300308": ds.StockMeta(code="300308", ts_code="300308.SZ", name="中际旭创", industry="CPO"),
    }
    daily_row = {
        "ts_code": "300308.SZ",
        "symbol": "300308",
        "trade_date": "20260806",
        "open": 901.02,
        "high": 995.0,
        "low": 882.55,
        "close": 955.0,
        "pre_close": 947.74,
        "pct_chg": 0.766,
        "vol": 422950.59,
        "amount": 39767147.8988,
    }
    index_row = {
        "ts_code": "000001.SH",
        "trade_date": "20260806",
        "open": 3300,
        "high": 3320,
        "low": 3280,
        "close": 3310,
        "pre_close": 3300,
        "pct_chg": 0.3,
        "vol": 1,
        "amount": 1,
    }
    monkeypatch.setattr(source, "_load_cached_daily", lambda trade_date: [daily_row] if trade_date == "20260806" else [])
    monkeypatch.setattr(source, "_load_cached_indices", lambda trade_date: [index_row] if trade_date == "20260806" else [])

    snapshot = source.fetch_for_date(universe, "20260806")

    assert snapshot.source_status["trade_date"] == "20260806"
    assert snapshot.source_status["historical_request"] is True
    assert snapshot.quotes[0].prev_close == 947.74
    assert snapshot.quotes[0].price == 955.0


def test_minute_series_reuses_short_cache(monkeypatch) -> None:
    settings = ds.AppSettings()
    settings.minute_series_live_cache_seconds = 30
    router = ds.MarketDataRouter(settings)
    calls = {"count": 0}

    monkeypatch.setattr(ds, "is_trading_window", lambda: True)

    def fake_fetch(code: str, trade_date: str, live: bool = False):  # noqa: ARG001
        calls["count"] += 1
        return [{"price": 10.0, "vol": 100}]

    monkeypatch.setattr(router.minute_replay, "fetch", fake_fetch)

    first = router.fetch_minute_series("300476", "20260807", live=True)
    second = router.fetch_minute_series("300476", "20260807", live=True)

    assert first == second == [{"price": 10.0, "vol": 100}]
    assert first is not second
    assert calls["count"] == 1


def test_index_minute_series_uses_explicit_shanghai_market(monkeypatch) -> None:
    settings = ds.AppSettings()
    source = ds.EasyTdxMinuteReplaySource(settings)
    monkeypatch.setattr(ds, "is_trading_window", lambda: False)

    calls = {"code": None, "history": 0}

    class FakePage:
        def __init__(self, points):
            self.points = points
            self.count = len(points)

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.minutes = self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

        def connect(self):
            return None

        def close(self):
            return None

        def today(self, code, include_raw=False):  # noqa: ARG002
            return FakePage([])

        def history(self, code, trade_date, include_raw=False):  # noqa: ARG002
            calls["code"] = code
            calls["history"] += 1
            return FakePage(
                [
                    types.SimpleNamespace(time_label="09:31", price=3298.0, volume=900),
                    types.SimpleNamespace(time_label="09:32", price=3300.0, volume=1000),
                    types.SimpleNamespace(time_label="09:33", price=3302.0, volume=1100),
                ]
            )

    easy_tdx_module = types.ModuleType("easy_tdx")
    easy_tdx_module.TdxClient = FakeClient
    monkeypatch.setitem(sys.modules, "easy_tdx", easy_tdx_module)

    rows = source.fetch_index("000001", "20260807", live=False)

    assert rows == [
        {"price": 3298.0, "vol": 900.0, "amount": 0.0, "time": "09:31"},
        {"price": 3300.0, "vol": 1000.0, "amount": 0.0, "time": "09:32"},
        {"price": 3302.0, "vol": 1100.0, "amount": 0.0, "time": "09:33"},
    ]
    assert calls["code"] == "sh000001"
    assert calls["history"] == 1


def test_easy_tdx_live_minute_falls_back_when_rows_are_abnormal(monkeypatch) -> None:
    settings = ds.AppSettings()
    source = ds.EasyTdxMinuteReplaySource(settings)
    monkeypatch.setattr(ds, "is_trading_window", lambda: True)

    calls = {"live": 0, "history": 0}

    class FakePage:
        def __init__(self, points):
            self.points = points
            self.count = len(points)

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.minutes = self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

        def connect(self):
            return None

        def close(self):
            return None

        def today(self, code, include_raw=False):  # noqa: ARG002
            calls["live"] += 1
            return FakePage(
                [
                    types.SimpleNamespace(time_label="09:31", price=0.0, volume=48),
                    types.SimpleNamespace(time_label="09:32", price=0.48, volume=48),
                    types.SimpleNamespace(time_label="09:33", price=1.02, volume=16),
                    types.SimpleNamespace(time_label="09:34", price=3558.68, volume=-90),
                    types.SimpleNamespace(time_label="09:35", price=10386.5, volume=167),
                ]
            )

        def history(self, code, trade_date, include_raw=False):  # noqa: ARG002
            calls["history"] += 1
            return FakePage(
                [
                    types.SimpleNamespace(time_label="09:31", price=121.96, volume=21778),
                    types.SimpleNamespace(time_label="09:32", price=121.69, volume=25647),
                    types.SimpleNamespace(time_label="09:33", price=121.65, volume=11263),
                ]
            )

    easy_tdx_module = types.ModuleType("easy_tdx")
    easy_tdx_module.TdxClient = FakeClient
    monkeypatch.setitem(sys.modules, "easy_tdx", easy_tdx_module)

    rows = source.fetch("002463", "20260807", live=True)

    assert rows == [
        {"price": 121.96, "vol": 21778.0, "amount": 0.0, "time": "09:31"},
        {"price": 121.69, "vol": 25647.0, "amount": 0.0, "time": "09:32"},
        {"price": 121.65, "vol": 11263.0, "amount": 0.0, "time": "09:33"},
    ]
    assert calls == {"live": 1, "history": 1}


