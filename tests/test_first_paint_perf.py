"""首屏性能回归：分时缩略图与板块资金流的 stale-while-revalidate / 后台预热。

80GB 轨迹库上同步采样曾是首屏最大开销（分时 1.7s+、板块资金流 1.4s），
请求路径必须零 SQLite 等待，由后台 worker 填充缓存后随 WS 增量带出。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from app.models import MiniIntradaySeries, SectorFlowSeries
from app.services import DashboardService


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
        quotes=[],
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
