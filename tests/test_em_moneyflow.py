from __future__ import annotations

from app.em_moneyflow import EMMoneyflowCache


def test_background_fetch_notifies_event_buffer_on_success(monkeypatch) -> None:
    notifications: list[str] = []
    cache = EMMoneyflowCache(enabled=True, on_update=lambda: notifications.append("updated"))
    monkeypatch.setattr(
        "app.em_moneyflow.fetch_full_market",
        lambda: {"600519": {"code": "600519", "main_net": 1.0}},
    )

    cache._fetch_once()

    assert notifications == ["updated"]
    assert cache.snapshot()["available"] is True


def test_background_fetch_notifies_when_error_state_changes(monkeypatch) -> None:
    notifications: list[str] = []
    cache = EMMoneyflowCache(enabled=True, on_update=lambda: notifications.append("updated"))

    def fail() -> dict:
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr("app.em_moneyflow.fetch_full_market", fail)

    cache._fetch_once()

    assert notifications == ["updated"]
    assert "upstream unavailable" in cache.snapshot()["note"]
