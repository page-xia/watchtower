"""End-to-end API tests: delta WS protocol, gzip."""

from __future__ import annotations

from starlette.testclient import TestClient

from app.main import app


def test_websocket_delta_snapshot_contains_stock_board() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream?view=terminal&format=delta&page_size=5") as ws:
            message = ws.receive_json()
    assert message["type"] == "snapshot"
    assert message["seq"] == 1
    data = message["data"]
    assert "stock_board" in data


def test_websocket_legacy_format_still_full_payload() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream?view=terminal&page_size=5") as ws:
            message = ws.receive_json()
    # 旧版（static 页面）协议：直接推完整 terminal payload，无 type 包装
    assert "stock_board" in message
    assert "type" not in message


def test_gzip_middleware_compresses_large_payloads() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/stocks/board?page_size=40", headers={"accept-encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
