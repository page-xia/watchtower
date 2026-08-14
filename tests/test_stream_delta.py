"""Snapshot/delta protocol tests for the terminal websocket tracker."""

from __future__ import annotations

from app.stream_delta import TerminalDeltaTracker


def _payload(price: float = 10.0, amount: float = 1e8, total: int = 5213, updated: str = "09:31:00") -> dict:
    return {
        "market": {"updated_at": updated, "emotion_score": 50},
        "sectors": [{"name": "半导体", "heat_score": 80}],
        "sector_flow": [{"name": "半导体", "points": [{"time": "09:31", "value": 1.0}]}],
        "events": [{"time": "09:31", "level": "market", "title": "开盘", "detail": ""}],
        "watchlist": [],
        "watchlist_preview": [],
        "positions_preview": [],
        "data_mode": "live",
        "source_status": {"updated_at": updated},
        "selected_sector": None,
        "sector_focus": None,
        "board_level": 3,
        "board_source": "test",
        "watchlist_codes": [],
        "stock_board": {
            "scope": "full_market",
            "selected_sector": None,
            "board_level": 3,
            "board_source": "test",
            "sort": "activity",
            "page": 1,
            "page_size": 40,
            "total": total,
            "updated_at": updated,
            "data_mode": "live",
            "frozen": False,
            "available_sorts": ["activity"],
            "items": [
                {"code": "300476", "name": "胜宏科技", "price": price, "amount": amount},
                {"code": "300308", "name": "中际旭创", "price": 20.0, "amount": 2e8},
            ],
        },
    }


def test_first_message_is_full_snapshot() -> None:
    tracker = TerminalDeltaTracker()
    message = tracker.next_message(_payload())
    assert message is not None
    assert message["type"] == "snapshot"
    assert message["seq"] == 1
    assert message["data"]["stock_board"]["items"][0]["code"] == "300476"


def test_unchanged_payload_produces_no_message() -> None:
    tracker = TerminalDeltaTracker()
    tracker.next_message(_payload())
    assert tracker.next_message(_payload()) is None


def test_changed_item_is_upserted_unchanged_is_not() -> None:
    tracker = TerminalDeltaTracker()
    tracker.next_message(_payload())
    message = tracker.next_message(_payload(price=10.5))
    assert message is not None
    assert message["type"] == "delta"
    board = message["sections"]["board"]
    assert [item["code"] for item in board["upsert"]] == ["300476"]
    assert "remove" not in board
    # 只有榜单行变化时，market 等未变分区不应出现在增量里
    assert "market" not in message["sections"]


def test_removed_and_reordered_items() -> None:
    tracker = TerminalDeltaTracker()
    tracker.next_message(_payload())
    payload = _payload()
    payload["stock_board"]["items"] = [payload["stock_board"]["items"][1]]
    message = tracker.next_message(payload)
    assert message is not None
    board = message["sections"]["board"]
    assert board["remove"] == ["300476"]
    assert board["order"] == ["300308"]
    assert "upsert" not in board


def test_meta_only_change() -> None:
    tracker = TerminalDeltaTracker()
    tracker.next_message(_payload())
    payload = _payload()
    payload["data_mode"] = "close_snapshot"
    message = tracker.next_message(payload)
    assert message is not None
    assert message["sections"]["meta"]["data_mode"] == "close_snapshot"
    assert "board" not in message["sections"]
