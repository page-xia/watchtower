"""stream_hub 单测：共享构建、Resync 掉队重同步、频道回收、频道数上限。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.stream_hub import RESYNC, ChannelLimitExceeded, ChannelSpec, StreamHub


def make_spec(build_counter: dict, *, interval: float = 0.05, tick_messages: list[str] | None = None):
    """生成一个测试频道：每 tick 计数一次，按序号发消息。"""
    messages = tick_messages or ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8"]

    async def build():
        index = build_counter["count"]
        build_counter["count"] += 1
        text = messages[index % len(messages)]
        return {"tick": index, "text": text}, text

    def snapshot_text(payload) -> str:
        return json.dumps({"type": "snapshot", "data": payload})

    return ChannelSpec(build=build, snapshot_text=snapshot_text, interval=lambda: interval)


async def collect(queue: asyncio.Queue, count: int, timeout: float = 2.0) -> list:
    items = []
    for _ in range(count):
        items.append(await asyncio.wait_for(queue.get(), timeout))
    return items


def test_subscribers_share_one_build_per_tick():
    async def scenario():
        hub = StreamHub(queue_size=8, channel_idle_seconds=30)
        counter = {"count": 0}
        factory = lambda: make_spec(counter)
        sub_a = hub.subscribe(("view", 1), factory)
        sub_b = hub.subscribe(("view", 1), factory)
        try:
            # 两个订阅者各收 3 条；构建次数应远小于 2 连接 × tick 数
            got_a, got_b = await asyncio.gather(collect(sub_a.queue, 3), collect(sub_b.queue, 3))
            assert got_a == got_b
            assert counter["count"] <= 4  # 单发布者：约每 tick 一次，而非每连接一次
        finally:
            await hub.aclose()

    asyncio.run(scenario())


def test_new_subscriber_gets_snapshot_from_latest_payload():
    async def scenario():
        hub = StreamHub(queue_size=8, channel_idle_seconds=30)
        counter = {"count": 0}
        factory = lambda: make_spec(counter)
        sub_a = hub.subscribe(("view", 1), factory)
        await collect(sub_a.queue, 2)  # 频道已产出 latest_payload
        sub_b = hub.subscribe(("view", 1), factory)
        try:
            snapshot = sub_b.snapshot_text()
            assert snapshot is not None
            data = json.loads(snapshot)
            assert data["type"] == "snapshot"
            assert "tick" in data["data"]
        finally:
            await hub.aclose()

    asyncio.run(scenario())


def test_slow_subscriber_gets_resync_and_recovers():
    async def scenario():
        hub = StreamHub(queue_size=2, channel_idle_seconds=30)
        counter = {"count": 0}
        factory = lambda: make_spec(counter, interval=0.01)
        fast_sub = hub.subscribe(("view", 1), factory)
        slow_sub = hub.subscribe(("view", 1), factory)
        try:
            # 快订阅者持续消费；慢订阅者不消费，队列（2 条）很快被塞满
            fast_msgs: list = []
            stop_fast = asyncio.Event()

            async def consume_fast():
                while not stop_fast.is_set():
                    try:
                        fast_msgs.append(await asyncio.wait_for(fast_sub.queue.get(), 0.5))
                    except asyncio.TimeoutError:
                        continue

            consumer = asyncio.create_task(consume_fast())
            await asyncio.sleep(0.5)
            stop_fast.set()
            await consumer

            # 慢订阅者队列应被投入 RESYNC 哨兵
            drained = []
            while not slow_sub.queue.empty():
                drained.append(slow_sub.queue.get_nowait())
            assert RESYNC in drained

            # 快订阅者不受影响：持续收到正常文本消息，无 RESYNC
            # （发布者节拍下限 0.2s，0.5s 窗口约 2-3 tick）
            assert len(fast_msgs) >= 2
            assert all(isinstance(item, str) for item in fast_msgs)

            # 慢订阅者按 RESYNC 语义重拿快照后能继续
            snapshot = slow_sub.snapshot_text()
            assert snapshot is not None and "snapshot" in snapshot
        finally:
            await hub.aclose()

    asyncio.run(scenario())


def test_channel_reaped_after_idle_and_recreated():
    async def scenario():
        hub = StreamHub(queue_size=8, channel_idle_seconds=1)
        counter = {"count": 0}
        factory = lambda: make_spec(counter, interval=0.05)
        sub = hub.subscribe(("view", 1), factory)
        await collect(sub.queue, 1)
        hub.unsubscribe(sub)
        # 等宽限期 + 发布者 1s 检查节拍
        await asyncio.sleep(2.6)
        assert ("view", 1) not in hub.channels
        # 重新订阅 → 频道重建，构建从头再来
        sub2 = hub.subscribe(("view", 1), factory)
        msgs = await collect(sub2.queue, 1)
        assert msgs
        await hub.aclose()

    asyncio.run(scenario())


def test_channel_limit_raises_for_caller_fallback():
    async def scenario():
        hub = StreamHub(queue_size=8, channel_idle_seconds=30, max_channels=8)
        counter = {"count": 0}
        factory = lambda: make_spec(counter)
        subs = [hub.subscribe(("view", index), factory) for index in range(8)]
        with pytest.raises(ChannelLimitExceeded):
            hub.subscribe(("view", 999), factory)
        # 已有频道不受影响
        assert len(hub.channels) == 8
        for sub in subs:
            hub.unsubscribe(sub)
        await hub.aclose()

    asyncio.run(scenario())


def test_unsubscribed_channel_stops_building():
    async def scenario():
        hub = StreamHub(queue_size=8, channel_idle_seconds=30)
        counter = {"count": 0}
        factory = lambda: make_spec(counter, interval=0.02)
        sub = hub.subscribe(("view", 1), factory)
        await collect(sub.queue, 2)
        hub.unsubscribe(sub)
        await asyncio.sleep(0.3)  # 发布者进入空转等待，不再构建
        frozen = counter["count"]
        await asyncio.sleep(0.3)
        assert counter["count"] == frozen
        await hub.aclose()

    asyncio.run(scenario())
