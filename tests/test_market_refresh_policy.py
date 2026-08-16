from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.market_schedule import market_refresh_policy


CN = ZoneInfo("Asia/Shanghai")


def cn_time(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 14, hour, minute, tzinfo=CN)


def test_market_refresh_policy_keeps_intraday_realtime() -> None:
    policy = market_refresh_policy(cn_time(10, 0), live_interval_seconds=1, static_interval_seconds=30)

    assert policy["market_session"] == "morning"
    assert policy["traffic_mode"] == "realtime"
    assert policy["should_poll"] is True
    assert policy["should_stream"] is True
    assert policy["poll_interval_ms"] == 1000
    assert policy["stream_interval_seconds"] == 1


def test_market_refresh_policy_reduces_lunch_without_stopping() -> None:
    policy = market_refresh_policy(cn_time(12, 10), live_interval_seconds=1, static_interval_seconds=30)

    assert policy["market_session"] == "lunch_break"
    assert policy["traffic_mode"] == "reduced"
    assert policy["should_poll"] is True
    assert policy["should_stream"] is True
    assert policy["poll_interval_ms"] == 30000
    assert policy["stream_interval_seconds"] == 30


def test_market_refresh_policy_allows_closing_buffer_final_refresh() -> None:
    policy = market_refresh_policy(cn_time(15, 2), live_interval_seconds=1, static_interval_seconds=30)

    assert policy["market_session"] == "closing_buffer"
    assert policy["traffic_mode"] == "finalizing"
    assert policy["should_poll"] is True
    assert policy["should_stream"] is True
    assert policy["final_refresh"] is True
    assert policy["poll_interval_ms"] == 30000


def test_market_refresh_policy_stops_after_close() -> None:
    policy = market_refresh_policy(cn_time(15, 10), live_interval_seconds=1, static_interval_seconds=30)

    assert policy["market_session"] == "post_close"
    assert policy["traffic_mode"] == "static"
    assert policy["should_poll"] is False
    assert policy["should_stream"] is False
    assert policy["poll_interval_ms"] is None
    assert policy["stream_interval_seconds"] is None
