"""End-to-end API tests: delta WS protocol, opening markers endpoint, gzip."""

from __future__ import annotations

from starlette.testclient import TestClient

from app.main import app


def test_opening_markers_endpoint_shape() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/opening/markers?offset=0&limit=20")
    assert resp.status_code == 200
    payload = resp.json()
    assert {"trade_date", "total", "offset", "limit", "items"} <= set(payload)
    assert isinstance(payload["items"], list)


def test_websocket_delta_snapshot_contains_opening_markers() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream?view=terminal&format=delta&page_size=5") as ws:
            message = ws.receive_json()
    assert message["type"] == "snapshot"
    assert message["seq"] == 1
    data = message["data"]
    assert "stock_board" in data
    assert "opening_markers" in data  # 菱形流分区挂载在终端 payload 上


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
