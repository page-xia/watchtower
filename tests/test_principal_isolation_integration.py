"""End-to-end contracts for principal overlays and shared market work."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from uuid import uuid4

from app.models import WatchlistItem
from app.principal import Principal
from app.services import DashboardService
from app.user_state import RevisionConflict
from app.user_state_json import JsonPrincipalStateRepository
from test_dashboard_service import make_principal_service


class CountingJsonPrincipalStateRepository(JsonPrincipalStateRepository):
    """The real local backend plus a narrow lookup-observability seam."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.looked_up_principals: list[str] = []

    def get_state(self, principal: Principal):  # type: ignore[no-untyped-def]
        self.looked_up_principals.append(principal.storage_key)
        return super().get_state(principal)


def _principal() -> Principal:
    return Principal("anonymous_client", uuid4().hex)


def _service_with_json_store(tmp_path: Path) -> tuple[object, CountingJsonPrincipalStateRepository]:
    service = make_principal_service(tmp_path)
    store = CountingJsonPrincipalStateRepository(tmp_path / "principal-state.json")
    service.user_state_store = store
    service._principal_state_cache.clear()
    return service, store


def _market_facts(payload) -> dict[str, tuple[float, float, float]]:  # type: ignore[no-untyped-def]
    return {
        item.code: (item.price, item.activity_score, item.signal_score)
        for item in payload.stock_board.items
    }


def test_two_random_principals_get_private_overlay_without_changing_market_facts(tmp_path) -> None:
    service, store = _service_with_json_store(tmp_path)
    alice = _principal()
    bob = _principal()
    watched = WatchlistItem(code="300476", name="胜宏科技")

    service.upsert_watchlist(alice, watched)
    alice_payload = service.terminal(principal=alice, page_size=20, fast=True)
    bob_payload = service.terminal(principal=bob, page_size=20, fast=True)

    assert _market_facts(alice_payload) == _market_facts(bob_payload)
    assert alice_payload.stock_board.items[0].code == "300476"
    assert alice_payload.stock_board.items[0].watchlisted is True
    bob_row = next(item for item in bob_payload.stock_board.items if item.code == "300476")
    assert bob_row.watchlisted is False
    assert alice_payload.watchlist_codes == ["300476"]
    assert bob_payload.watchlist_codes == []

    service.delete_watchlist(alice, "300476", expected_revision=alice_payload.personalization_revision)
    alice_after_delete = service.terminal(principal=alice, page_size=20, fast=True)
    bob_after_delete = service.terminal(principal=bob, page_size=20, fast=True)

    alice_row_after_delete = next(item for item in alice_after_delete.stock_board.items if item.code == "300476")
    assert alice_row_after_delete.watchlisted is False
    assert alice_after_delete.watchlist_codes == []
    assert bob_after_delete.model_dump() == bob_payload.model_dump()
    assert set(store.looked_up_principals) == {alice.storage_key, bob.storage_key}


def test_fifty_principals_share_one_public_context_per_cache_window(tmp_path, monkeypatch) -> None:
    service, store = _service_with_json_store(tmp_path)
    context = service._context_cache
    assert context is not None
    service._context_cache = None
    service._context_cache_at = 0.0
    service._context_cache_bucket = None
    # ``make_principal_service`` pins a fixture context for unit tests.  Put
    # the real cache gate back here so this integration test observes the
    # number of public-context refreshes instead of merely terminal calls.
    monkeypatch.setattr(
        service,
        "_get_context",
        DashboardService._get_context.__get__(service, DashboardService),
    )
    counters = {"public_context": 0, "upstream_snapshot": 0}

    def build_public_context():
        counters["public_context"] += 1
        counters["upstream_snapshot"] += 1
        service._context_cache = context
        service._context_cache_at = __import__("time").time()
        service._context_cache_bucket = service._context_bucket()
        return context

    monkeypatch.setattr(service, "_refresh_context", build_public_context)
    overlay_durations: list[float] = []
    original_overlay = service._apply_principal_overlay

    def timed_overlay(*args, **kwargs):  # type: ignore[no-untyped-def]
        started = perf_counter()
        result = original_overlay(*args, **kwargs)
        overlay_durations.append(perf_counter() - started)
        return result

    monkeypatch.setattr(service, "_apply_principal_overlay", timed_overlay)
    terminal_durations: list[float] = []
    principals = [_principal() for _ in range(50)]
    for principal in principals:
        started = perf_counter()
        payload = service.terminal(principal=principal, page_size=20, fast=True)
        terminal_durations.append(perf_counter() - started)
        assert payload.personalization_revision == 0

    assert counters == {"public_context": 1, "upstream_snapshot": 1}
    assert set(store.looked_up_principals) == {principal.storage_key for principal in principals}
    assert len(store.looked_up_principals) == 50
    assert _p95(overlay_durations) < 0.05
    # This is deliberately looser than the documented production target: it
    # protects the shared-cache contract without depending on CI scheduler load.
    assert _p95(terminal_durations) < 0.5


def test_stale_write_and_one_time_legacy_import_preserve_canonical_server_state(tmp_path) -> None:
    _service, store = _service_with_json_store(tmp_path)
    principal = _principal()
    competing_items = [
        WatchlistItem(code="300476", name="胜宏科技"),
        WatchlistItem(code="300308", name="中际旭创"),
    ]

    def write_from_revision_zero(item: WatchlistItem) -> str:
        try:
            store.upsert_watchlist(principal, item, expected_revision=0)
        except RevisionConflict:
            return "conflict"
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write_from_revision_zero, competing_items))

    assert sorted(outcomes) == ["conflict", "success"]
    assert store.get_state(principal).revision == 1

    # A different principal models the browser's first legacy import when
    # canonical state already exists on the server.
    principal = _principal()
    canonical = WatchlistItem(code="000001", name="平安银行")
    first = store.upsert_watchlist(principal, canonical, expected_revision=0)

    conflicting_legacy = [WatchlistItem(code="300476", name="胜宏科技")]
    first_import = store.import_legacy_watchlist_once(principal, conflicting_legacy)
    second_import = store.import_legacy_watchlist_once(
        principal,
        [WatchlistItem(code="300308", name="中际旭创")],
    )

    assert first.revision == 1
    assert first_import.applied is False
    assert first_import.reason == "existing_state"
    assert second_import.applied is False
    assert [item.code for item in store.list_watchlist(principal)] == ["000001"]


def _p95(samples: list[float]) -> float:
    assert samples
    if len(samples) == 1:
        return samples[0]
    return quantiles(samples, n=100, method="inclusive")[94]
