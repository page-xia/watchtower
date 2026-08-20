"""app/dark_pool.py 个股摘要：生产快照未覆盖时的按需补数链路。"""

from __future__ import annotations

import types

from app.dark_pool import DarkPoolMonitor


class _FakeSnapshotStore:
    backend = "cloudbase_snapshot"
    available = True

    def __init__(self, summary: dict | None = None, request_ok: bool = True) -> None:
        self._summary = summary
        self._request_ok = request_ok
        self.requested: list[str] = []

    def stock_summary(self, code: str) -> dict | None:
        return self._summary

    def request_stock(self, code: str) -> bool:
        self.requested.append(code)
        return self._request_ok


def _monitor(store: _FakeSnapshotStore) -> DarkPoolMonitor:
    settings = types.SimpleNamespace(dark_pool_enabled=True)
    return DarkPoolMonitor(settings, context_provider=lambda: None, eod_store=store)


class TestStockEodSnapshotMiss:
    def test_hit_returns_summary(self) -> None:
        store = _FakeSnapshotStore(summary={"flow_10d": [], "trade_date": "20260818"})
        out = _monitor(store)._stock_eod("300476")
        assert out["eod_available"] is True
        assert out["trade_date"] == "20260818"
        assert store.requested == []

    def test_miss_registers_request_and_returns_pending(self) -> None:
        store = _FakeSnapshotStore(summary=None, request_ok=True)
        out = _monitor(store)._stock_eod("600519")
        assert out["eod_available"] is False
        assert out["pending"] is True
        assert "补数请求" in out["note"]
        assert store.requested == ["600519"]

    def test_miss_without_request_keeps_static_note(self) -> None:
        store = _FakeSnapshotStore(summary=None, request_ok=False)
        out = _monitor(store)._stock_eod("600519")
        assert out["eod_available"] is False
        assert "pending" not in out
        assert "快照预计算范围" in out["note"]
