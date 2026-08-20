from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from app.models import PositionRecord, WatchlistItem
from app.principal import Principal
from app.user_state import LegacyImportResult, RevisionConflict, UserStateUnavailable
from app import user_state_json as user_state_json_module
from app.user_state_json import JsonPrincipalStateRepository


def _principal(name: str) -> Principal:
    return Principal("anonymous_client", f"{name}-0001")


def _write_from_process(path: str, start: int) -> None:
    """Top-level spawn target for the cross-process lock test."""

    repo = JsonPrincipalStateRepository(path)
    principal = _principal("process")
    for index in range(start, start + 5):
        repo.upsert_watchlist(
            principal,
            WatchlistItem(code=f"{index:06d}", name=f"股票{index}"),
        )


def test_watchlist_and_positions_are_isolated_by_principal(tmp_path) -> None:
    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    alice = _principal("alice")
    bob = _principal("bob")

    repo.upsert_watchlist(alice, WatchlistItem(code="300476", name="胜宏科技"))
    repo.upsert_position(
        bob,
        PositionRecord(code="300476", name="胜宏科技", cost=10, quantity=100, available_quantity=0),
    )

    assert [item.code for item in repo.list_watchlist(alice)] == ["300476"]
    assert repo.list_watchlist(bob) == []
    assert [item.code for item in repo.list_positions(bob)] == ["300476"]
    assert repo.list_positions(alice) == []


def test_successful_mutation_increments_revision_and_stale_writes_conflict(tmp_path) -> None:
    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    principal = _principal("alice")

    first = repo.upsert_watchlist(principal, WatchlistItem(code="300476", name="胜宏科技"))
    assert first.revision == 1

    second = repo.delete_watchlist(principal, "300476", expected_revision=first.revision)
    assert second.revision == 2
    with pytest.raises(RevisionConflict) as error:
        repo.upsert_watchlist(
            principal,
            WatchlistItem(code="000001", name="平安银行"),
            expected_revision=first.revision,
        )
    assert error.value.expected_revision == 1
    assert error.value.current_revision == 2


def test_legacy_import_is_idempotent_and_never_resurrects_existing_state(tmp_path) -> None:
    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    principal = _principal("alice")
    old = [WatchlistItem(code="300476", name="胜宏科技")]

    first = repo.import_legacy_watchlist_once(principal, old)
    again = repo.import_legacy_watchlist_once(
        principal, [WatchlistItem(code="000001", name="平安银行")]
    )

    assert isinstance(first, LegacyImportResult) and first.applied is True
    assert again.applied is False
    assert [item.code for item in repo.list_watchlist(principal)] == ["300476"]


def test_existing_canonical_state_wins_over_legacy_import_and_marker_prevents_resurrection(tmp_path) -> None:
    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    principal = _principal("alice")
    canonical = WatchlistItem(code="000001", name="平安银行")
    repo.upsert_watchlist(principal, canonical)

    result = repo.import_legacy_watchlist_once(
        principal, [WatchlistItem(code="300476", name="胜宏科技")]
    )

    assert result.applied is False
    assert result.reason == "existing_state"
    assert repo.list_watchlist(principal) == [canonical]
    assert repo.import_legacy_watchlist_once(principal, [WatchlistItem(code="300308", name="中际旭创")]).applied is False
    assert repo.list_watchlist(principal) == [canonical]


def test_legacy_import_caps_items_at_two_hundred(tmp_path) -> None:
    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    principal = _principal("alice")
    items = [
        WatchlistItem(code=f"{index:06d}", name=f"股票{index}")
        for index in range(1, 206)
    ]

    result = repo.import_legacy_watchlist_once(principal, items)

    assert result.applied is True
    assert len(result.items) == 200
    assert len(repo.list_watchlist(principal)) == 200
    assert result.revision == 1


def test_legacy_import_after_pre_migration_canonical_delete_is_one_time(tmp_path) -> None:
    """An empty state before the first migration is eligible for import.

    Once the import writes its marker, deleting imported rows cannot make a
    later browser payload resurrect them.
    """

    repo = JsonPrincipalStateRepository(tmp_path / "principal_state.json")
    principal = _principal("alice")
    canonical = WatchlistItem(code="000001", name="平安银行")
    repo.upsert_watchlist(principal, canonical)
    repo.delete_watchlist(principal, canonical.code)

    legacy = WatchlistItem(code="300476", name="胜宏科技")
    first = repo.import_legacy_watchlist_once(principal, [legacy])
    repo.delete_watchlist(principal, legacy.code, expected_revision=first.revision)
    second = repo.import_legacy_watchlist_once(principal, [legacy])

    assert first.applied is True
    assert second.applied is False
    assert repo.list_watchlist(principal) == []


def test_process_lock_serializes_repeated_mutations_from_separate_processes(tmp_path) -> None:
    context = get_context("spawn")
    principal = _principal("process")
    for round_number in range(3):
        path = tmp_path / f"principal_state_{round_number}.json"
        first = context.Process(target=_write_from_process, args=(str(path), 1))
        second = context.Process(target=_write_from_process, args=(str(path), 6))
        first.start()
        second.start()
        first.join(timeout=30)
        second.join(timeout=30)

        assert first.exitcode == 0
        assert second.exitcode == 0
        repo = JsonPrincipalStateRepository(path)
        state = repo.get_state(principal)
        assert state.revision == 10
        assert len(state.watchlist) == 10
        assert path.with_name(path.name + ".lock").stat().st_size == 1


def test_missing_state_file_initializes_empty_but_malformed_json_is_unavailable(tmp_path) -> None:
    path = tmp_path / "principal_state.json"
    repo = JsonPrincipalStateRepository(path)
    principal = _principal("alice")
    assert repo.get_state(principal).revision == 0

    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(UserStateUnavailable):
        repo.get_state(principal)

    path.write_bytes(b"\xff\xfe")
    with pytest.raises(UserStateUnavailable):
        repo.get_state(principal)


def test_state_file_permission_error_is_unavailable(tmp_path, monkeypatch) -> None:
    path = tmp_path / "principal_state.json"
    repo = JsonPrincipalStateRepository(path)
    principal = _principal("alice")
    repo.upsert_watchlist(principal, WatchlistItem(code="300476", name="胜宏科技"))
    original_read_text = Path.read_text

    def denied_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied_read)
    with pytest.raises(UserStateUnavailable):
        repo.get_state(principal)


def test_state_write_permission_error_is_unavailable(tmp_path, monkeypatch) -> None:
    path = tmp_path / "principal_state.json"
    repo = JsonPrincipalStateRepository(path)
    principal = _principal("alice")
    original_open = user_state_json_module.os.open

    def denied_temp_open(file, *args, **kwargs):
        if str(file).startswith(str(path) + ".") and str(file).endswith(".tmp"):
            raise PermissionError("denied")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(user_state_json_module.os, "open", denied_temp_open)
    with pytest.raises(UserStateUnavailable):
        repo.upsert_watchlist(principal, WatchlistItem(code="300476", name="胜宏科技"))


def test_state_lock_open_error_is_unavailable(tmp_path, monkeypatch) -> None:
    path = tmp_path / "principal_state.json"
    repo = JsonPrincipalStateRepository(path)
    principal = _principal("alice")
    original_open = Path.open

    def denied_lock_open(self, *args, **kwargs):
        if self == repo.lock_path:
            raise PermissionError("denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_lock_open)
    with pytest.raises(UserStateUnavailable):
        repo.get_state(principal)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable on Windows")
def test_atomic_temp_and_replaced_state_are_private(tmp_path, monkeypatch) -> None:
    path = tmp_path / "principal_state.json"
    repo = JsonPrincipalStateRepository(path)
    principal = _principal("alice")
    observed_modes: list[int] = []
    original_replace = Path.replace

    def spy_replace(self, target):
        observed_modes.append(stat.S_IMODE(self.stat().st_mode))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    repo.upsert_watchlist(principal, WatchlistItem(code="300476", name="胜宏科技"))

    assert observed_modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_json_writes_are_atomic_and_concurrent_mutations_keep_valid_state(tmp_path, monkeypatch) -> None:
    path = tmp_path / "principal_state.json"
    repo = JsonPrincipalStateRepository(path)
    principal = _principal("alice")
    replacements: list[str] = []
    original_replace = type(path).replace

    def spy_replace(self, target):
        replacements.append(str(self))
        return original_replace(self, target)

    monkeypatch.setattr(type(path), "replace", spy_replace)

    def add(index: int) -> None:
        repo.upsert_watchlist(
            principal,
            WatchlistItem(code=f"{index:06d}", name=f"股票{index}"),
        )

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(add, range(1, 21)))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[principal.storage_key]["revision"] == 20
    assert len(payload[principal.storage_key]["watchlist"]) == 20
    assert replacements
    assert all(name.endswith(".tmp") for name in replacements)
    assert not path.with_name(path.name + ".tmp").exists()
