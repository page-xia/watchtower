"""Tests for the process-local event buffer used by live channels."""

from __future__ import annotations

import asyncio

from app.data_update_buffer import DataUpdateBuffer


def test_commit_is_versioned_and_keeps_latest_section_values() -> None:
    buffer = DataUpdateBuffer(queue_size=1)

    first = buffer.commit({"market": {"price": 10}}, reason="quote")
    second = buffer.commit({"market": {"price": 11}, "sectors": ["A"]}, reason="refresh")

    assert first.version == 1
    assert second.version == 2
    assert second.changed_sections == frozenset({"market", "sectors"})
    assert buffer.snapshot().version == 2
    assert buffer.snapshot().sections["market"] == {"price": 11}
    assert buffer.snapshot().reason == "refresh"

    second.sections["market"]["price"] = 99
    assert buffer.snapshot().sections["market"] == {"price": 11}


def test_subscriber_coalesces_burst_and_stops_after_close() -> None:
    async def scenario() -> None:
        buffer = DataUpdateBuffer(queue_size=1)
        subscription = buffer.subscribe()

        buffer.commit({"mini_chart": 1}, reason="first")
        buffer.commit({"market": 2}, reason="second")
        update = await asyncio.wait_for(subscription.get(), timeout=1)
        assert update.version == 2
        assert update.sections["market"] == 2
        assert update.changed_sections == frozenset({"mini_chart", "market"})

        subscription.close()
        buffer.commit({"market": 3}, reason="third")
        await asyncio.sleep(0)
        assert subscription.closed is True

    asyncio.run(scenario())


def test_subscriber_keeps_monotonic_version_when_thread_callbacks_arrive_out_of_order() -> None:
    async def scenario() -> None:
        buffer = DataUpdateBuffer(queue_size=1)
        first = buffer.commit({"mini_chart": 1}, reason="first")
        second = buffer.commit({"market": 2}, reason="second")
        subscription = buffer.subscribe()

        subscription._offer(second)
        subscription._offer(first)
        update = await subscription.get()

        assert update.version == second.version
        assert update.sections["market"] == 2
        assert update.changed_sections == frozenset({"mini_chart", "market"})
        subscription.close()

    asyncio.run(scenario())
