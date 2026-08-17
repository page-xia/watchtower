from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.data_sources import china_now, is_trading_window, market_session


REALTIME_SESSIONS = {"preopen", "morning", "afternoon"}
REDUCED_SESSIONS = {"lunch_break"}
FINALIZING_SESSIONS = {"closing_buffer"}


def _next_open_at(now: datetime) -> str:
    candidate = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now >= candidate:
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    return candidate.isoformat()


def market_refresh_policy(
    now: datetime | None = None,
    *,
    live_interval_seconds: float = 1.0,
    static_interval_seconds: float = 30.0,
) -> dict[str, Any]:
    """Traffic policy for UI polling/streaming without changing data-source semantics.

    `is_trading_window()` intentionally remains broad for data-source routing
    (preopen through the close buffer). This policy is narrower: visible
    intraday sessions stay realtime, lunch/final close buffer are reduced, and
    static sessions should not keep timers or websocket reconnect loops alive.
    """

    current = now if now is not None else china_now()
    session = market_session(current)
    live_interval = max(0.2, float(live_interval_seconds))
    static_interval = max(5.0, float(static_interval_seconds))

    if session in REALTIME_SESSIONS:
        traffic_mode = "realtime"
        should_poll = True
        should_stream = True
        poll_interval_ms: int | None = int(live_interval * 1000)
        stream_interval_seconds: float | None = live_interval
        final_refresh = False
    elif session in REDUCED_SESSIONS:
        traffic_mode = "reduced"
        should_poll = True
        should_stream = True
        poll_interval_ms = int(static_interval * 1000)
        stream_interval_seconds = static_interval
        final_refresh = False
    elif session in FINALIZING_SESSIONS:
        traffic_mode = "finalizing"
        should_poll = True
        should_stream = True
        poll_interval_ms = int(static_interval * 1000)
        stream_interval_seconds = static_interval
        final_refresh = True
    else:
        traffic_mode = "static"
        should_poll = False
        should_stream = False
        poll_interval_ms = None
        stream_interval_seconds = None
        final_refresh = False

    return {
        "market_session": session,
        "is_trading_window": is_trading_window(current),
        "traffic_mode": traffic_mode,
        "should_poll": should_poll,
        "should_stream": should_stream,
        "poll_interval_ms": poll_interval_ms,
        "stream_interval_seconds": stream_interval_seconds,
        "final_refresh": final_refresh,
        "next_open_at": _next_open_at(current),
    }
