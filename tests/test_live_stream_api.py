"""Persistent multiplexed WebSocket protocol tests."""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from app.main import app, service


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

    def fake_terminal(**kwargs: Any) -> _FakeModel:
        page = int(kwargs["page"])
        pages.append(page)
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
                    "params": {"page": 1, "pageSize": 40, "watchlist_codes": []},
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
                    "params": {"page": 2, "pageSize": 40, "watchlist_codes": []},
                }
            )
            second = _receive_channel(ws, "terminal")
            assert second["type"] == "snapshot"
            assert second["data"]["stock_board"]["page"] == 2

    assert pages == [1, 2]


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
