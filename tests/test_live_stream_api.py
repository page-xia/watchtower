"""Persistent multiplexed WebSocket protocol tests."""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from app.main import _live_channel_key, _live_terminal_params, app, service


class _FakeModel:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return self._payload


def _terminal_payload(page: int) -> dict[str, Any]:
    return {
        "market": {"updated_at": f"09:{page:02d}:00"},
        "sectors": [],
        "sector_flow": [],
        "watchlist": [],
        "watchlist_preview": [],
        "positions_preview": [],
        "data_mode": "live",
        "source_status": {"updated_at": f"09:{page:02d}:00"},
        "selected_sector": None,
        "board_level": 3,
        "watchlist_codes": [],
        "personalization_status": "ready",
        "personalization_revision": 1,
        "stock_board": {
            "scope": "full_market",
            "selected_sector": None,
            "board_level": 3,
            "sort": "activity",
            "page": page,
            "page_size": 40,
            "total": 2,
            "updated_at": f"09:{page:02d}:00",
            "data_mode": "live",
            "frozen": False,
            "items": [{"code": "300476", "name": "胜宏科技", "price": 10.0}],
        },
    }


def _receive_channel(ws: Any, channel: str) -> dict[str, Any]:
    for _ in range(10):
        message = ws.receive_json()
        if message.get("type") == "channel" and message.get("channel") == channel:
            return message["message"]
    raise AssertionError(f"did not receive {channel} channel message")


def test_live_socket_switches_view_without_reconnect(monkeypatch) -> None:
    pages: list[int] = []
    terminal_calls: list[dict[str, Any]] = []

    def fake_terminal(**kwargs: Any) -> _FakeModel:
        page = int(kwargs["page"])
        pages.append(page)
        terminal_calls.append(kwargs)
        return _FakeModel(_terminal_payload(page))

    monkeypatch.setattr(service, "terminal", fake_terminal)
    monkeypatch.setattr(
        "app.main._stream_refresh_policy",
        lambda: {
            "market_session": "post_close",
            "traffic_mode": "static",
            "should_stream": False,
            "stream_interval_seconds": 30,
        },
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            ws.send_json(
                {
                    "type": "subscribe",
                    "channel": "terminal",
                    "params": {
                        "client_id": "alice-0001",
                        "page": 1,
                        "pageSize": 40,
                        "watchlistCodes": ["600519"],
                    },
                }
            )
            first = _receive_channel(ws, "terminal")
            assert first["type"] == "snapshot"
            assert first["data"]["stock_board"]["page"] == 1

            # Same WebSocket object: no close/reconnect when the view changes.
            ws.send_json(
                {
                    "type": "subscribe",
                    "channel": "terminal",
                    "params": {
                        "client_id": "alice-0001",
                        "page": 2,
                        "pageSize": 40,
                        "watchlistCodes": ["600519"],
                    },
                }
            )
            second = _receive_channel(ws, "terminal")
            assert second["type"] == "snapshot"
            assert second["data"]["stock_board"]["page"] == 2

    assert pages == [1, 2]
    assert all("client_watchlist" not in call for call in terminal_calls)
    assert [call["principal"].id for call in terminal_calls] == ["alice-0001", "alice-0001"]


def test_live_socket_refresh_rebuilds_existing_channel(monkeypatch) -> None:
    calls: list[int] = []
    refresh_counter = 0

    def fake_terminal(**kwargs: Any) -> _FakeModel:
        nonlocal refresh_counter
        refresh_counter += 1
        calls.append(refresh_counter)
        payload = _terminal_payload(int(kwargs["page"]))
        payload["market"]["updated_at"] = f"09:00:{refresh_counter:02d}"
        payload["source_status"]["updated_at"] = payload["market"]["updated_at"]
        return _FakeModel(payload)

    monkeypatch.setattr(service, "terminal", fake_terminal)
    monkeypatch.setattr(
        "app.main._stream_refresh_policy",
        lambda: {
            "market_session": "post_close",
            "traffic_mode": "static",
            "should_stream": False,
            "stream_interval_seconds": 30,
        },
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            ws.send_json(
                {
                    "type": "subscribe",
                    "channel": "terminal",
                    "params": {"page": 1, "pageSize": 40, "watchlistCodes": ["600519"]},
                }
            )
            first = _receive_channel(ws, "terminal")
            assert first["type"] == "snapshot"
            ws.send_json({"type": "refresh", "channel": "terminal"})
            refreshed = _receive_channel(ws, "terminal")
            assert refreshed["type"] in {"snapshot", "delta"}

    assert calls == [1, 2]


def test_live_socket_multiplexes_non_terminal_channels(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "index_minutes",
        lambda trade_date=None: {"trade_date": trade_date or "20260820", "indices": []},
    )
    monkeypatch.setattr(
        service,
        "dark_pool_payload",
        lambda sector=None, board_level=3: {"sector": sector, "board_level": board_level},
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            ws.send_json({"type": "subscribe", "channel": "index_minutes", "params": {}})
            ws.send_json(
                {
                    "type": "subscribe",
                    "channel": "dark_pool",
                    "params": {"sector": "半导体", "boardLevel": 3},
                }
            )

            index = _receive_channel(ws, "index_minutes")
            dark = _receive_channel(ws, "dark_pool")

            assert index["type"] == "snapshot"
            assert index["data"]["indices"] == []
            assert dark["type"] == "snapshot"
            assert dark["data"] == {"sector": "半导体", "board_level": 3}


def test_live_terminal_params_scopes_to_valid_client_and_ignores_watchlist_codes() -> None:
    params = _live_terminal_params(
        {"client_id": "alice-0001", "watchlistCodes": ["600519"]}
    )
    assert params.client_id == "alice-0001"
    assert params.principal is not None
    assert params.principal.storage_key == "anonymous_client:alice-0001"
    assert not hasattr(params, "watchlist_codes")


def test_live_channel_key_includes_principal_scope() -> None:
    alice = _live_channel_key("terminal", {"client_id": "alice-0001", "page": 1})
    bob = _live_channel_key("terminal", {"client_id": "bob-0001", "page": 1})
    assert alice != bob
    assert "anonymous_client:alice-0001" in alice
    assert "anonymous_client:bob-0001" in bob


def test_public_live_channel_key_is_shared_across_principals() -> None:
    alice = _live_channel_key("index_minutes", {"client_id": "alice-0001"})
    bob = _live_channel_key("index_minutes", {"client_id": "bob-0001"})
    assert alice == bob


def test_live_socket_rejects_invalid_client_id_without_subscribing(monkeypatch) -> None:
    called = False

    def fake_terminal(**kwargs: Any) -> _FakeModel:
        nonlocal called
        called = True
        return _FakeModel(_terminal_payload(1))

    monkeypatch.setattr(service, "terminal", fake_terminal)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            ws.send_json(
                {
                    "type": "subscribe",
                    "channel": "terminal",
                    "params": {"client_id": "bad"},
                }
            )
            for _ in range(3):
                message = ws.receive_json()
                if message.get("type") == "error":
                    assert message["channel"] == "terminal"
                    assert "client_id" in message["message"]
                    break
            else:
                raise AssertionError("expected invalid client_id error")
    assert called is False


def test_legacy_stream_rejects_invalid_client_id_without_internal_error() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream?view=terminal&client_id=bad") as ws:
            message = ws.receive_json()
            assert message["type"] == "error"
            assert "client_id" in message["message"]


def test_two_live_clients_with_same_market_params_keep_personal_payloads_isolated(monkeypatch) -> None:
    principals: list[str] = []

    def fake_terminal(**kwargs: Any) -> _FakeModel:
        principal = kwargs["principal"]
        principals.append(principal.id)
        payload = _terminal_payload(1)
        payload["watchlist_codes"] = [principal.id]
        payload["personalization_revision"] = len(principals)
        return _FakeModel(payload)

    monkeypatch.setattr(service, "terminal", fake_terminal)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as alice, client.websocket_connect("/ws/live") as bob:
            alice.send_json(
                {
                    "type": "subscribe",
                    "channel": "terminal",
                    "params": {"client_id": "alice-0001", "page": 1, "pageSize": 40},
                }
            )
            bob.send_json(
                {
                    "type": "subscribe",
                    "channel": "terminal",
                    "params": {"client_id": "bob-0001", "page": 1, "pageSize": 40},
                }
            )
            alice_payload = _receive_channel(alice, "terminal")["data"]
            bob_payload = _receive_channel(bob, "terminal")["data"]

    assert principals == ["alice-0001", "bob-0001"]
    assert alice_payload["watchlist_codes"] == ["alice-0001"]
    assert bob_payload["watchlist_codes"] == ["bob-0001"]
