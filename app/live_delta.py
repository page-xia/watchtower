"""Delta protocol for generic live-stream channels.

Terminal already has a field-aware delta tracker.  The other live channels
(index minutes, dark-pool panels, and stock-detail panes) are much smaller and
do not need field-level patching on the server.  They use this tracker so the
socket only transmits after the serialized payload changes.
"""

from __future__ import annotations

import json
from typing import Any


def _signature(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class PayloadDeltaTracker:
    """Snapshot/full-payload delta state machine for a shared live channel."""

    def __init__(self) -> None:
        self.seq = 0
        self._last_signature: str | None = None

    def next_message(self, payload: Any) -> dict[str, Any] | None:
        signature = _signature(payload)
        if signature == self._last_signature:
            return None

        self.seq += 1
        self._last_signature = signature
        if self.seq == 1:
            return {"type": "snapshot", "seq": self.seq, "data": payload}
        return {"type": "delta", "seq": self.seq, "data": payload}

    def snapshot_message(self, payload: Any) -> dict[str, Any]:
        """Build a snapshot from the channel's latest payload."""
        return {"type": "snapshot", "seq": self.seq, "data": payload}
