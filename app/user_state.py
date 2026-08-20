"""Contracts and value objects for principal-scoped personal state.

The market data layer is shared, while watchlists and positions are private to
one :class:`~app.principal.Principal`.  Backends implement the protocol below;
the local JSON backend is intentionally small so it can be used in development
and tests without a database service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from app.models import PositionRecord, WatchlistItem
from app.principal import Principal


PersonalizationStatus = str
StateItem = WatchlistItem | PositionRecord


@dataclass(frozen=True)
class PrincipalState:
    """Canonical personal state for one principal."""

    revision: int
    watchlist: list[WatchlistItem]
    positions: list[PositionRecord]
    personalization_status: PersonalizationStatus = "ready"


@dataclass(frozen=True)
class PrincipalMutation:
    """Result returned by a successful personal-state mutation."""

    revision: int
    item: StateItem | None = None


@dataclass(frozen=True)
class LegacyImportResult:
    """Result of the one-time browser watchlist migration."""

    applied: bool
    reason: str
    revision: int
    items: list[WatchlistItem]


class UserStateError(RuntimeError):
    """Base class for user-state persistence errors."""


class RevisionConflict(UserStateError):
    """Raised when a mutation was based on a stale state revision."""

    def __init__(self, expected_revision: int, current_revision: int) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        # ``actual_revision`` is a descriptive alias useful to API adapters;
        # retain ``current_revision`` as the canonical name for callers.
        self.actual_revision = current_revision
        super().__init__(
            f"revision conflict: expected {expected_revision}, current {current_revision}"
        )


class UserStateUnavailable(UserStateError):
    """Raised when a configured personal-state backend cannot be reached."""


class PrincipalStateRepository(Protocol):
    """Storage contract shared by JSON and database implementations."""

    def get_state(self, principal: Principal) -> PrincipalState:
        ...

    def list_watchlist(self, principal: Principal) -> list[WatchlistItem]:
        ...

    def upsert_watchlist(
        self,
        principal: Principal,
        item: WatchlistItem,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        ...

    def delete_watchlist(
        self,
        principal: Principal,
        code: str,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        ...

    def list_positions(self, principal: Principal) -> list[PositionRecord]:
        ...

    def upsert_position(
        self,
        principal: Principal,
        item: PositionRecord,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        ...

    def delete_position(
        self,
        principal: Principal,
        code: str,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        ...

    def import_legacy_watchlist_once(
        self,
        principal: Principal,
        items: Sequence[WatchlistItem],
    ) -> LegacyImportResult:
        ...


__all__ = [
    "LegacyImportResult",
    "PersonalizationStatus",
    "PrincipalMutation",
    "PrincipalState",
    "PrincipalStateRepository",
    "RevisionConflict",
    "StateItem",
    "UserStateError",
    "UserStateUnavailable",
]
