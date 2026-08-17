"""Delta protocol for the terminal websocket stream.

The terminal payload is rebuilt on every server tick (context is cached, the
build is cheap), but most of it is unchanged tick to tick.  This tracker
compares successive payloads and produces:

- a one-shot ``snapshot`` message when a client connects,
- ``delta`` messages carrying only changed sections afterwards,
- ``None`` when nothing changed (nothing is sent).

Board items are compared one by one so the frontend can keep object identity
for unchanged rows (React.memo) and avoid re-rendering the whole table.
"""

from __future__ import annotations

import json
from typing import Any

# Sections replaced wholesale when their signature changes.
_REPLACE_SECTIONS = (
    "market",
    "sectors",
    "sector_flow",
    "watchlist",
    "watchlist_preview",
    "positions_preview",
)

# Rarely-changing top-level scalar fields, grouped into one meta section.
_META_FIELDS = (
    "data_mode",
    "source_status",
    "selected_sector",
    "sector_focus",
    "board_level",
    "board_source",
    "watchlist_codes",
)

_BOARD_META_FIELDS = (
    "scope",
    "selected_sector",
    "board_level",
    "board_source",
    "sort",
    "page",
    "page_size",
    "total",
    "updated_at",
    "data_mode",
    "frozen",
    "available_sorts",
    "near_trend",
    "near_trend_ready",
    "near_trend_pending",
    "pin_buy",
)


def _sig(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class TerminalDeltaTracker:
    """Per-connection snapshot/delta state machine."""

    def __init__(self) -> None:
        self.seq = 0
        self._sent_snapshot = False
        self._section_sigs: dict[str, str] = {}
        self._meta_sig = ""
        self._board_meta_sig = ""
        self._board_item_sigs: dict[str, str] = {}
        self._board_order: list[str] = []

    def next_message(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.seq += 1
        if not self._sent_snapshot:
            self._sent_snapshot = True
            self._record(payload)
            return {"type": "snapshot", "seq": self.seq, "data": payload}

        sections: dict[str, Any] = {}
        for key in _REPLACE_SECTIONS:
            value = payload.get(key)
            sig = _sig(value)
            if sig != self._section_sigs.get(key):
                self._section_sigs[key] = sig
                sections[key] = value

        meta = {k: payload.get(k) for k in _META_FIELDS}
        meta_sig = _sig(meta)
        if meta_sig != self._meta_sig:
            self._meta_sig = meta_sig
            sections["meta"] = meta

        board_delta = self._board_delta(payload.get("stock_board") or {})
        if board_delta:
            sections["board"] = board_delta

        if not sections:
            return None
        return {"type": "delta", "seq": self.seq, "sections": sections}

    def _record(self, payload: dict[str, Any]) -> None:
        for key in _REPLACE_SECTIONS:
            self._section_sigs[key] = _sig(payload.get(key))
        self._meta_sig = _sig({k: payload.get(k) for k in _META_FIELDS})
        board = payload.get("stock_board") or {}
        items = board.get("items") or []
        self._board_meta_sig = _sig({k: board.get(k) for k in _BOARD_META_FIELDS})
        self._board_item_sigs = {
            str(item.get("code") or ""): _sig(item) for item in items if isinstance(item, dict)
        }
        self._board_order = [str(item.get("code") or "") for item in items if isinstance(item, dict)]

    def _board_delta(self, board: dict[str, Any]) -> dict[str, Any]:
        items = [item for item in (board.get("items") or []) if isinstance(item, dict)]
        delta: dict[str, Any] = {}

        meta = {k: board.get(k) for k in _BOARD_META_FIELDS}
        meta_sig = _sig(meta)
        if meta_sig != self._board_meta_sig:
            self._board_meta_sig = meta_sig
            delta["meta"] = meta

        new_sigs: dict[str, str] = {}
        order: list[str] = []
        upsert: list[dict[str, Any]] = []
        for item in items:
            code = str(item.get("code") or "")
            if not code:
                continue
            sig = _sig(item)
            new_sigs[code] = sig
            order.append(code)
            if self._board_item_sigs.get(code) != sig:
                upsert.append(item)

        removed = [code for code in self._board_item_sigs if code not in new_sigs]
        order_changed = order != self._board_order

        self._board_item_sigs = new_sigs
        self._board_order = order

        if upsert:
            delta["upsert"] = upsert
        if removed:
            delta["remove"] = removed
        if order_changed:
            delta["order"] = order
        return delta
