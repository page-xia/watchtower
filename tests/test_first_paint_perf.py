"""首屏性能回归：分时缩略图与板块资金流的 stale-while-revalidate / 后台预热。

80GB 轨迹库上同步采样曾是首屏最大开销（分时 1.7s+、板块资金流 1.4s），
请求路径必须零 SQLite 等待，由后台 worker 填充缓存后随 WS 增量带出。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from app.models import MiniIntradaySeries, SectorFlowSeries
from app.services import BoardEntry, DashboardService


def _service() -> DashboardService:
    service = DashboardService.__new__(DashboardService)
    service._sector_flow_lock = threading.Lock()
    service._sector_flow_cache_by_key = {}
    service._sector_flow_names_by_key = {}
    service._sector_flow_refresh_threads = {}
    service._terminal_cache_by_key = {}
    service._stock_mini_chart_cache = {}
    service._sector_mini_chart_cache = {}
    service._mini_chart_warm_lock = threading.Lock()
    service._mini_chart_warm_pending = set()
    service._mini_chart_warm_thread = None
    service._last_stock_mini_chart_elapsed_ms = 0.0
    service._last_stock_mini_chart_missing_count = 0
    service._last_stock_mini_chart_loaded_count = 0
    service.settings = SimpleNamespace(
        terminal_context_frozen_cache_seconds=300,
        sector_flow_refresh_seconds=10,
    )
    service.engine = SimpleNamespace(sector_flow_top_n=10)
    return service


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        market=SimpleNamespace(frozen=True),
        snapshot=SimpleNamespace(source_status={"trade_date": "20260812"}),
        source_status={"trade_date": "20260812"},
    )


def _rows() -> list[dict]:
    return [
        {"captured_at": "09:31:00", "price": 10.0, "change_pct": 0.5, "amount": 1e6, "minute_amount_ratio": 1.2},
        {"captured_at": "09:32:00", "price": 10.1, "change_pct": 1.5, "amount": 2e6, "minute_amount_ratio": 1.4},
    ]


def test_mini_charts_defer_and_warm_in_background() -> None:
    service = _service()
    calls: list[list[str]] = []

    def loader(date: str, codes: list[str], max_rows: int = 0) -> dict:
        calls.append(list(codes))
        return {code: _rows() for code in codes}

    service.trajectory_store = SimpleNamespace(stock_feature_mini_series_by_code=loader)
    result = service._stock_mini_charts_by_code(_context(), ["300476"])
    assert result["300476"].source_quality == "deferred"  # 请求路径立即返回
    assert calls == []  # 同步路径不读 SQLite

    thread = service._mini_chart_warm_thread
    assert thread is not None
    thread.join(timeout=10)
    assert calls == [["300476"]]

    cached = service._stock_mini_charts_by_code(_context(), ["300476"])
    assert cached["300476"].source_quality not in {"deferred", "unavailable"}


def test_mini_charts_serve_stale_entry_while_revalidating() -> None:
    service = _service()
    stale = MiniIntradaySeries(source_quality="local_trajectory")
    service._stock_mini_chart_cache[("20260812", "300476")] = (time.time() - 10_000, stale)
    calls: list[list[str]] = []

    def loader(date: str, codes: list[str], max_rows: int = 0) -> dict:
        calls.append(list(codes))
        return {code: _rows() for code in codes}

    service.trajectory_store = SimpleNamespace(stock_feature_mini_series_by_code=loader)
    result = service._stock_mini_charts_by_code(_context(), ["300476"])
    assert result["300476"] is stale  # 过期缓存先用着，避免占位符闪烁
    thread = service._mini_chart_warm_thread
    assert thread is not None
    thread.join(timeout=10)
    assert calls == [["300476"]]  # 后台已完成刷新


def test_frozen_sector_flow_deferred_and_warmed(monkeypatch) -> None:
    service = _service()
    expected = [SectorFlowSeries(name="PCB", heat_score=70, final_value=70.0, change_pct=0.8, points=[])]
    monkeypatch.setattr(service, "_sector_flow_from_trajectory", lambda *a, **k: expected)
    snapshot = SimpleNamespace(
        source_status={"trade_date": "20260812", "frozen": True},
        data_mode="closed_static",
        # 空快照不调度延迟重建（2026-08-17 起的有意行为）：没有 quotes 无法做
        # L1 真值锚定，未锚定结果会占住冻结缓存。测试给一只票让调度发生。
        quotes=[SimpleNamespace(code="300476")],
    )
    sectors = [SimpleNamespace(name="PCB")]

    result = service._sector_flow_for_context(snapshot, sectors, allow_deferred=True)
    assert result == []  # 首屏立即返回，不等 80GB 轨迹库冷读

    threads = list(service._sector_flow_refresh_threads.values())
    assert threads
    for thread in threads:
        thread.join(timeout=10)

    cached = service._sector_flow_for_context(snapshot, sectors, allow_deferred=True)
    assert [series.name for series in cached] == ["PCB"]


def test_frozen_sector_flow_stays_synchronous_without_deferred(monkeypatch) -> None:
    service = _service()
    expected = [SectorFlowSeries(name="PCB", heat_score=70, final_value=70.0, change_pct=0.8, points=[])]
    monkeypatch.setattr(service, "_sector_flow_from_trajectory", lambda *a, **k: expected)
    snapshot = SimpleNamespace(
        source_status={"trade_date": "20260812", "frozen": True},
        data_mode="closed_static",
        quotes=[],
    )
    result = service._sector_flow_for_context(snapshot, [SimpleNamespace(name="PCB")])
    assert [series.name for series in result] == ["PCB"]  # 默认行为不变


def test_mini_chart_falls_back_to_easy_tdx_minutes_when_trajectory_starts_late() -> None:
    """盘中新入榜/新点开的票轨迹库只有后半段：回退 easy_tdx 分钟线补全天。"""
    service = _service()
    service._context_cache = SimpleNamespace(
        snapshot=SimpleNamespace(quotes=[]),
        market=SimpleNamespace(frozen=True),
    )

    def loader(date: str, codes: list[str], max_rows: int = 0) -> dict:
        # 轨迹库只覆盖打开榜单之后的半段（10:30 起）
        return {
            code: [
                {"captured_at": "10:30:00", "price": 10.0, "change_pct": 0.5, "amount": 1e6, "minute_amount_ratio": 1.0},
                {"captured_at": "10:31:00", "price": 10.1, "change_pct": 1.0, "amount": 1e6, "minute_amount_ratio": 1.0},
            ]
            for code in codes
        }

    def fetch_minute_series(code: str, trade_date: str, live: bool = False) -> list[dict]:
        return [
            {"time": "09:30", "price": 9.9, "amount": 5e5},
            {"time": "09:31", "price": 10.0, "amount": 5e5},
            {"time": "10:30", "price": 10.1, "amount": 5e5},
        ]

    service.trajectory_store = SimpleNamespace(stock_feature_mini_series_by_code=loader)
    service.data_source = SimpleNamespace(fetch_minute_series=fetch_minute_series)

    result = service._stock_mini_charts_by_code(_context(), ["300476"])
    assert result["300476"].source_quality == "deferred"  # 请求路径不等回退

    thread = service._mini_chart_warm_thread
    assert thread is not None
    thread.join(timeout=10)

    cached = service._stock_mini_charts_by_code(_context(), ["300476"])
    chart = cached["300476"]
    assert chart.source_quality == "easy_tdx_minute_fallback"
    assert chart.times[0] == "09:30"  # 早盘段已补齐


def test_pin_buy_entries_filters_to_near_buy_point() -> None:
    """置顶买点过滤：只保留最近信号为买T、且现价 ≤ 买点+1% 的票，保持原排序。"""
    service = DashboardService.__new__(DashboardService)
    service._last_action_lock = threading.Lock()
    service._last_action_by_code = {
        "300476": {"last_action": "买T", "last_action_price": 10.0, "last_action_time": "10:00"},
        "300001": {"last_action": "买T", "last_action_price": 20.0, "last_action_time": "09:45"},
        "300002": {"last_action": "减T/卖T", "last_action_price": 30.0, "last_action_time": "11:00"},
    }

    def entry(code: str, price: float, rank: int) -> BoardEntry:
        return BoardEntry(
            sort_key=(rank,),
            quote=SimpleNamespace(code=code, price=price),
            sector=None,
        )

    entries = [
        entry("300001", 20.1, 0),  # 买点上方 +0.5%（≤+1%）→ 保留
        entry("300476", 10.05, 1),  # 买点上方 +0.5% → 保留
        entry("300002", 30.0, 2),  # 最近是卖点 → 过滤掉
        entry("300003", 15.0, 3),  # 当天无买卖点 → 过滤掉
    ]
    filtered = service._pin_buy_entries(entries)
    assert [item.quote.code for item in filtered] == ["300001", "300476"]

    # 低于买点的票同样保留（现价 ≤ 买点+1%，单边上限）
    below = service._pin_buy_entries([entry("300476", 9.5, 0)])
    assert [item.quote.code for item in below] == ["300476"]

    # 现价超过买点 +1% → 过滤掉
    above = service._pin_buy_entries([entry("300476", 10.2, 0)])
    assert above == []
