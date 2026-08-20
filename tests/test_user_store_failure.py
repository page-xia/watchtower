from __future__ import annotations

from types import SimpleNamespace

from app.config import AppSettings
from app.models import WatchlistItem
from app.principal import Principal
from app.services import DashboardService
from app.user_state import UserStateUnavailable


class _GlobalWatchlistThatMustNotBeRead:
    def __init__(self) -> None:
        self.reads = 0

    def list_items(self):
        self.reads += 1
        raise AssertionError("global watchlist must never be used as a personal fallback")


class _FailingPrincipalStore:
    def get_state(self, principal):
        raise UserStateUnavailable("RDS unavailable")


def test_user_store_failure_returns_unavailable_empty_personalization_without_global_read(tmp_path) -> None:
    """A failed principal store must never expose an old process-global watchlist."""

    global_store = _GlobalWatchlistThatMustNotBeRead()
    settings = AppSettings()
    settings.data_dir = tmp_path
    service = DashboardService(
        settings,
        watchlist_store=global_store,
        user_state_store=_FailingPrincipalStore(),
    )

    state = service.principal_state(Principal("anonymous_client", "alice-0001"))

    assert state.personalization_status == "unavailable"
    assert state.watchlist == []
    assert global_store.reads == 0


def test_missing_principal_read_is_empty_even_with_a_legacy_global_store(tmp_path) -> None:
    """Anonymous reads are public data plus an empty overlay, never shared personal data."""

    class _GlobalWatchlist:
        def list_items(self):
            return [WatchlistItem(code="300476", name="不应读取")]

    settings = AppSettings()
    settings.data_dir = tmp_path
    service = DashboardService(settings, watchlist_store=_GlobalWatchlist())

    state = service._resolved_personal_state(None)

    assert state.personalization_status == "missing_identity"
    assert state.watchlist == []


def test_unknown_user_store_backend_fails_closed_instead_of_using_json(tmp_path) -> None:
    """A deployment typo must not silently create a local shared fallback."""

    settings = AppSettings()
    settings.data_dir = tmp_path
    settings.user_store_backend = "typo"
    settings.user_mysql_host = "configured.example"
    settings.user_mysql_user = "watchtower"
    settings.user_mysql_db = "watchtower_user"
    service = DashboardService(settings)

    state = service.principal_state(Principal("anonymous_client", "alice-0001"))

    assert state.personalization_status == "unavailable"
    assert settings.public_source_status["user_store_configured"] is False


def test_terminal_and_public_refresh_do_not_touch_global_stores_when_personal_store_fails(
    tmp_path,
    monkeypatch,
) -> None:
    """Global stores cannot influence shared facts, even in an injected legacy test setup."""

    global_store = _GlobalWatchlistThatMustNotBeRead()
    settings = AppSettings()
    service = DashboardService(
        settings,
        watchlist_store=global_store,
        position_store=global_store,
        user_state_store=_FailingPrincipalStore(),
    )
    principal = Principal("anonymous_client", "alice-0001")
    context = SimpleNamespace(
        source_status={},
        market=SimpleNamespace(frozen=True, updated_at=""),
        snapshot=SimpleNamespace(data_mode="closed_static"),
    )
    expected_payload = SimpleNamespace(personalization_status="unavailable", watchlist_codes=[])
    monkeypatch.setattr(service, "_get_context", lambda: context)
    monkeypatch.setattr(service, "_terminal_cache_key", lambda *args, **kwargs: "terminal-test")
    monkeypatch.setattr(service, "_payload_cache_ttl", lambda _: 0.0)
    monkeypatch.setattr(service, "_terminal_payload_for_context", lambda *args, **kwargs: expected_payload)
    monkeypatch.setattr(service, "_terminal_payload_complete", lambda _: False)

    payload = service.terminal(principal=principal)

    assert payload.personalization_status == "unavailable"
    assert payload.watchlist_codes == []

    fetched: list[tuple[object, object]] = []
    fallback = object()
    monkeypatch.setattr(service.data_source, "fetch", lambda watchlist, themes: fetched.append((watchlist, themes)) or object())
    monkeypatch.setattr(service, "_snapshot_unavailable", lambda _: True)
    monkeypatch.setattr(service, "_fallback_context_for_unavailable_snapshot", lambda *args: fallback)
    monkeypatch.setattr(service, "_publish_context_update", lambda *args, **kwargs: None)

    assert service._refresh_context() is fallback
    assert fetched[0][0] == []
    assert global_store.reads == 0
