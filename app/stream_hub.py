"""Shared websocket broadcast hub: one publisher per channel, queues per connection.

现状痛点：`/ws/stream` 每个连接各跑一个轮询循环。service 层共享缓存已经把
数据构建收敛为全局一次，但深拷贝/序列化/delta 计算仍是 per-connection。

本模块把同一参数组合（频道）收敛为「单发布者 + 每连接队列」：

- 发布者先构建一次初始快照，之后只在 DataUpdateBuffer 提交相关分区或客户端
  显式刷新时调用 spec.build()；构建、序列化、delta 计算全局一次。
- 新订阅者先拿频道最新 payload 生成的全量快照，再进入共享消息流；delta 是
  严格顺序流，从该快照起应用后续广播即可，协议与逐连接 tracker 完全等价。
- 慢客户端队列满时投 Resync 哨兵并清空其队列，消费端遇到后重发最新快照，
  只影响自己，不拖垮频道。
- 频道在最后一个订阅者离开并过了宽限期后回收；频道总数超限抛
  ChannelLimitExceeded，由调用方回退到逐连接轮询。

线程模型：频道与订阅队列运行在同一个 asyncio 事件循环内；后台数据线程只通过
DataUpdateBuffer 的线程安全提交边界唤醒频道，不直接操作 asyncio 队列。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.data_update_buffer import BufferSubscription, DataCommit, DataUpdateBuffer


class _Resync:
    """订阅者掉队哨兵：消费端收到后应重发最新全量快照。"""


RESYNC = _Resync()


class ChannelLimitExceeded(Exception):
    """频道总数达到上限，调用方应回退到逐连接轮询。"""


@dataclass
class ChannelSpec:
    """一个频道的构建/编码逻辑，由接入方（main.py）按 WS 参数生成。

    build() 返回 (payload, broadcast_text)：
      - payload 用于给新订阅者/掉队订阅者生成快照，可为任意可序列化对象；
      - broadcast_text 为本 tick 要广播的消息文本，None 表示无变化不发送。
    snapshot_text(payload) 把 payload 编码成该频道协议下的全量快照文本。
    interval() 在事件驱动模式下仅作为安全唤醒超时，不会触发无变化重建；
    未注入 DataUpdateBuffer 的兼容模式仍按该间隔轮询。
    """

    build: Callable[[], Awaitable[tuple[Any, str | None]]]
    snapshot_text: Callable[[Any], str]
    interval: Callable[[], float]
    interests: frozenset[str] = frozenset({"*"})


@dataclass(eq=False)
class Subscription:
    channel: "StreamChannel"
    queue: asyncio.Queue = field(repr=False)

    def snapshot_text(self) -> str | None:
        """当前频道最新状态的快照文本；频道尚未产出过数据时为 None。"""
        if self.channel.latest_payload is None:
            return None
        return self.channel.spec.snapshot_text(self.channel.latest_payload)


class StreamChannel:
    def __init__(self, hub: "StreamHub", key: tuple, spec: ChannelSpec) -> None:
        self.hub = hub
        self.key = key
        self.spec = spec
        self.subscribers: set[Subscription] = set()
        self.latest_payload: Any = None
        self.task: asyncio.Task | None = None
        self.empty_since: float | None = None
        self._buffer_subscription: BufferSubscription | None = None
        self._refresh_event = asyncio.Event()

    def ensure_running(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"stream-channel-{abs(hash(self.key))}")

    def request_refresh(self) -> None:
        """Force one rebuild without replacing the channel or its subscribers."""

        self._refresh_event.set()

    def offer(self, text: str) -> None:
        """把一条广播文本投给所有订阅者；队列满的投 Resync 哨兵。"""
        for sub in list(self.subscribers):
            try:
                sub.queue.put_nowait(text)
            except asyncio.QueueFull:
                while not sub.queue.empty():
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - 防御
                        break
                try:
                    sub.queue.put_nowait(RESYNC)
                except asyncio.QueueFull:  # pragma: no cover - 防御
                    pass

    async def _run(self) -> None:
        idle = max(1, int(self.hub.channel_idle_seconds))
        try:
            if self.hub.update_buffer is not None:
                self._buffer_subscription = self.hub.update_buffer.subscribe()
                await self._build_once()
                await self._run_event_driven()
                return
            while True:
                if not self.subscribers:
                    if self.empty_since is None:
                        self.empty_since = time.monotonic()
                    elif time.monotonic() - self.empty_since >= idle:
                        return
                    await asyncio.sleep(1)
                    continue
                self.empty_since = None
                await self._build_once()
                await asyncio.sleep(max(0.2, float(self.spec.interval())))
        finally:
            if self._buffer_subscription is not None:
                self._buffer_subscription.close()
            self._buffer_subscription = None
            self.hub._drop_channel(self.key, self)

    async def _build_once(self) -> None:
        try:
            payload, text = await self.spec.build()
        except Exception:
            # 构建失败不杀死频道；下一个提交或显式刷新会重试。
            payload, text = None, None
        if payload is not None:
            self.latest_payload = payload
        if text:
            self.offer(text)

    async def _run_event_driven(self) -> None:
        assert self._buffer_subscription is not None
        subscription = self._buffer_subscription
        while True:
            if not self.subscribers:
                if self.empty_since is None:
                    self.empty_since = time.monotonic()
                elif time.monotonic() - self.empty_since >= max(1, int(self.hub.channel_idle_seconds)):
                    return
            else:
                self.empty_since = None

            try:
                reason = await asyncio.wait_for(
                    self._next_relevant_wakeup(subscription),
                    timeout=max(0.2, float(self.spec.interval())),
                )
            except asyncio.TimeoutError:
                # Safety timeout: do not rebuild without a data commit.
                continue
            if reason:
                await self._build_once()

    async def _next_relevant_wakeup(self, subscription: BufferSubscription) -> bool:
        while True:
            update_task = asyncio.create_task(subscription.get())
            refresh_task = asyncio.create_task(self._refresh_event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {update_task, refresh_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if refresh_task in done:
                    self._refresh_event.clear()
                    return True
                commit = update_task.result()
                if self._commit_matches(commit):
                    return True
            finally:
                for task in (update_task, refresh_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(update_task, refresh_task, return_exceptions=True)

    def _commit_matches(self, commit: DataCommit) -> bool:
        interests = self.spec.interests
        return "*" in interests or bool(interests.intersection(commit.changed_sections))


class StreamHub:
    """WS 频道注册表：按 key 复用频道，单发布者向多订阅者扇出。"""

    def __init__(
        self,
        *,
        queue_size: int = 8,
        channel_idle_seconds: int = 30,
        max_channels: int = 256,
        update_buffer: DataUpdateBuffer | None = None,
    ) -> None:
        self.queue_size = max(2, int(queue_size))
        self.channel_idle_seconds = max(1, int(channel_idle_seconds))
        self.max_channels = max(8, int(max_channels))
        self.update_buffer = update_buffer
        self.channels: dict[tuple, StreamChannel] = {}

    def subscribe(self, key: tuple, spec_factory: Callable[[], ChannelSpec]) -> Subscription:
        channel = self.channels.get(key)
        if channel is None:
            if len(self.channels) >= self.max_channels:
                raise ChannelLimitExceeded(f"stream channels capped at {self.max_channels}")
            channel = StreamChannel(self, key, spec_factory())
            self.channels[key] = channel
        sub = Subscription(channel=channel, queue=asyncio.Queue(maxsize=self.queue_size))
        channel.subscribers.add(sub)
        channel.ensure_running()
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.channel.subscribers.discard(sub)

    def refresh(self, sub: Subscription) -> None:
        """Request a real rebuild on an existing channel."""

        sub.channel.request_refresh()

    def _drop_channel(self, key: tuple, channel: StreamChannel) -> None:
        if self.channels.get(key) is channel:
            self.channels.pop(key, None)

    async def aclose(self) -> None:
        tasks = [channel.task for channel in self.channels.values() if channel.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.channels.clear()
