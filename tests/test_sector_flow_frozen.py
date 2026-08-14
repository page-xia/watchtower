"""Regression test: frozen/closed context must rebuild sector flow from the
local trajectory instead of partial after-hours TDX minute fetches."""

from __future__ import annotations

from types import SimpleNamespace

from app.models import SectorFlowSeries
from app.services import DashboardService


def _frozen_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        source_status={"trade_date": "20260812", "frozen": True},
        data_mode="closed_static",
    )


def _sectors() -> list[SimpleNamespace]:
    return [SimpleNamespace(name="半导体"), SimpleNamespace(name="PCB")]


def test_frozen_sector_flow_prefers_local_trajectory(monkeypatch) -> None:
    service = DashboardService.__new__(DashboardService)
    service._sector_flow_lock = __import__("threading").Lock()
    service._sector_flow_cache_by_key = {}
    service._sector_flow_names_by_key = {}
    service.engine = SimpleNamespace(sector_flow_top_n=10)
    service.settings = SimpleNamespace(
        terminal_context_frozen_cache_seconds=300,
        sector_flow_refresh_seconds=10,
    )

    expected = [
        SectorFlowSeries(name="半导体", heat_score=80, final_value=80.0, change_pct=1.2, points=[]),
        SectorFlowSeries(name="PCB", heat_score=70, final_value=70.0, change_pct=0.8, points=[]),
    ]
    calls = {"trajectory": 0, "build": 0}

    def fake_trajectory(trade_date, sectors, **_kwargs):
        calls["trajectory"] += 1
        assert trade_date == "20260812"
        return expected

    def fake_build(*args, **kwargs):
        calls["build"] += 1
        return []

    monkeypatch.setattr(service, "_sector_flow_from_trajectory", fake_trajectory)
    monkeypatch.setattr(service, "_build_and_cache_sector_flow", fake_build)

    result = service._sector_flow_for_context(_frozen_snapshot(), _sectors())

    assert [series.name for series in result] == ["半导体", "PCB"]
    assert calls["trajectory"] == 1
    assert calls["build"] == 0  # 不再走逐票 TDX 历史分钟线构建


def test_frozen_sector_flow_falls_back_when_trajectory_empty(monkeypatch) -> None:
    service = DashboardService.__new__(DashboardService)
    service._sector_flow_lock = __import__("threading").Lock()
    service._sector_flow_cache_by_key = {}
    service._sector_flow_names_by_key = {}
    service.engine = SimpleNamespace(sector_flow_top_n=10)
    service.settings = SimpleNamespace(
        terminal_context_frozen_cache_seconds=300,
        sector_flow_refresh_seconds=10,
    )
    monkeypatch.setattr(service, "_sector_flow_from_trajectory", lambda *a, **_kwargs: [])
    monkeypatch.setattr(
        service,
        "_build_and_cache_sector_flow",
        lambda *args, **kwargs: [
            SectorFlowSeries(name="PCB", heat_score=70, final_value=70.0, change_pct=0.8, points=[])
        ],
    )

    result = service._sector_flow_for_context(_frozen_snapshot(), _sectors())
    assert [series.name for series in result] == ["PCB"]
