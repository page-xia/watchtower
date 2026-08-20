"""Process-local versioned update buffer for event-driven live channels.

The service owns the actual market/projection caches.  This buffer is the
atomic commit point and wake-up bus: producers publish one logical update,
and WebSocket channels receive only the newest commit for their event loop.
It intentionally has no external persistence or network dependency; a future
multi-process deployment can replace this boundary with Redis without
changing channel consumers.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class DataCommit:
    """One atomically published update and the latest known section values."""

    version: int
    committed_at: float
    changed_sections: frozenset[str]
    sections: Mapping[str, Any]
    reason: str = ""


class BufferSubscription:
    """Async subscriber that coalesces bursts to one newest commit."""

    def __init__(self, owner: "DataUpdateBuffer", queue_size: int) -> None:
        self._owner = owner
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[DataCommit] = asyncio.Queue(maxsize=max(1, int(queue_size)))
        self._last_version = 0
        self.closed = False

    async def get(self) -> DataCommit:
        return await self._queue.get()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._owner._remove(self)

    def _offer(self, commit: DataCommit) -> None:
        if self.closed:
            return
        # A slow consumer does not need every intermediate quote. Keep the
        # newest commit so the next rebuild sees the latest coherent revision.
        # Preserve the union of changed sections, otherwise a later unrelated
        # commit could hide an earlier relevant wake-up while coalescing.
        changed_sections = set(commit.changed_sections)
        newest = commit
        while not self._queue.empty():
            try:
                pending = self._queue.get_nowait()
                changed_sections.update(pending.changed_sections)
                if pending.version > newest.version:
                    newest = pending
            except asyncio.QueueEmpty:  # pragma: no cover - defensive race guard
                break
        # Commits can be scheduled from different producer threads, so loop
        # callbacks are not guaranteed to arrive in version order.  Keep the
        # notification monotonic while retaining every coalesced interest.
        if newest.version < self._last_version:
            newest = self._owner.snapshot()
        if changed_sections != set(newest.changed_sections):
            newest = DataCommit(
                version=newest.version,
                committed_at=newest.committed_at,
                changed_sections=frozenset(changed_sections),
                sections=newest.sections,
                reason=newest.reason,
            )
        self._last_version = max(self._last_version, newest.version)
        try:
            self._queue.put_nowait(newest)
        except asyncio.QueueFull:  # pragma: no cover - coalescing should prevent this
            pass


class DataUpdateBuffer:
    """Thread-safe latest-value store plus async update notifications."""

    def __init__(self, *, queue_size: int = 1) -> None:
        self._lock = threading.RLock()
        self._queue_size = max(1, int(queue_size))
        self._version = 0
        self._sections: dict[str, Any] = {}
        self._latest = DataCommit(
            version=0,
            committed_at=time.time(),
            changed_sections=frozenset(),
            sections={},
            reason="initial",
        )
        self._subscriptions: set[BufferSubscription] = set()

    def commit(self, changes: Mapping[str, Any], *, reason: str = "") -> DataCommit:
        """Atomically merge ``changes`` and notify all active subscribers."""

        if not isinstance(changes, Mapping) or not changes:
            raise ValueError("changes must be a non-empty mapping")
        with self._lock:
            changed = frozenset(str(key) for key in changes)
            self._sections.update({str(key): copy.deepcopy(value) for key, value in changes.items()})
            self._version += 1
            commit = DataCommit(
                version=self._version,
                committed_at=time.time(),
                changed_sections=changed,
                sections=MappingProxyType(copy.deepcopy(self._sections)),
                reason=str(reason or ""),
            )
            self._latest = commit
            subscribers = tuple(self._subscriptions)
        for subscription in subscribers:
            try:
                subscription._loop.call_soon_threadsafe(subscription._offer, self._detached(commit))
            except RuntimeError:
                # The owning request loop may have closed between taking the
                # subscriber snapshot and scheduling the notification.
                subscription.close()
        return self._detached(commit)

    def snapshot(self) -> DataCommit:
        """Return a detached copy of the latest committed state."""

        with self._lock:
            return self._detached(self._latest)

    @staticmethod
    def _detached(commit: DataCommit) -> DataCommit:
        return DataCommit(
            version=commit.version,
            committed_at=commit.committed_at,
            changed_sections=commit.changed_sections,
            sections=MappingProxyType(copy.deepcopy(dict(commit.sections))),
            reason=commit.reason,
        )

    def subscribe(self) -> BufferSubscription:
        subscription = BufferSubscription(self, self._queue_size)
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def _remove(self, subscription: BufferSubscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)


__all__ = ["BufferSubscription", "DataCommit", "DataUpdateBuffer"]
