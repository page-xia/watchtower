"""Locked, atomic JSON implementation of the principal state repository."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Sequence

from app.models import PositionRecord, WatchlistItem
from app.principal import Principal
from app.user_state import (
    LegacyImportResult,
    PrincipalMutation,
    PrincipalState,
    RevisionConflict,
    UserStateUnavailable,
)


LEGACY_WATCHLIST_MIGRATION = "browser_watchlist_v1"
MAX_LEGACY_ITEMS = 200


class JsonPrincipalStateRepository:
    """Persist each principal's state in one JSON bucket.

    A sibling lock file and a process-local re-entrant lock protect every read
    and write.  Mutations write a flushed ``.tmp`` sibling and then replace the
    destination, so a process crash cannot leave a partially-written document.
    """

    _thread_locks: dict[str, RLock] = {}
    _thread_locks_guard = RLock()

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise UserStateUnavailable(f"cannot initialize state directory: {self.path.parent}") from error
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        # Keep a stable, exactly-one-byte lock file.  Opening it in append mode
        # for each operation would grow it and replacing it would invalidate an
        # OS lock held by another process.
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.ftruncate(descriptor, 1)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise UserStateUnavailable(f"cannot initialize state lock: {self.lock_path}") from error
        self._thread_lock = self._lock_for_path(self.path)

    @classmethod
    def _lock_for_path(cls, path: Path) -> RLock:
        # Windows paths are case-insensitive; normalize aliases so two
        # repository instances cannot accidentally use different thread locks.
        key = os.path.normcase(str(path.resolve()))
        with cls._thread_locks_guard:
            lock = cls._thread_locks.get(key)
            if lock is None:
                lock = RLock()
                cls._thread_locks[key] = lock
            return lock

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        """Acquire both an in-process and an OS-level lock for this file."""

        with self._thread_lock:
            try:
                handle = self.lock_path.open("r+b")
            except OSError as error:
                raise UserStateUnavailable(f"cannot open state lock: {self.lock_path}") from error
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt

                    deadline = time.monotonic() + 30.0
                    while True:
                        handle.seek(0)
                        try:
                            # Non-blocking acquisition lets us retry without
                            # relying on LK_LOCK's implementation-defined
                            # retry interval on Windows.
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            acquired = True
                            break
                        except OSError as error:
                            if time.monotonic() >= deadline:
                                raise UserStateUnavailable(
                                    f"timed out acquiring state lock: {self.lock_path}"
                                ) from error
                            time.sleep(0.01)
                else:
                    import fcntl

                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                        acquired = True
                    except OSError as error:
                        raise UserStateUnavailable(
                            f"cannot acquire state lock: {self.lock_path}"
                        ) from error
                yield
            except UserStateUnavailable:
                raise
            except OSError as error:
                raise UserStateUnavailable(f"state lock I/O failed: {self.lock_path}") from error
            finally:
                if acquired:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError as error:
                        raise UserStateUnavailable(
                            f"cannot release state lock: {self.lock_path}"
                        ) from error
                try:
                    handle.close()
                except OSError as error:
                    raise UserStateUnavailable(
                        f"cannot close state lock: {self.lock_path}"
                    ) from error

    def get_state(self, principal: Principal) -> PrincipalState:
        with self._process_lock():
            document = self._load_document()
            return self._state_from_bucket(self._bucket(document, principal))

    def list_watchlist(self, principal: Principal) -> list[WatchlistItem]:
        return self.get_state(principal).watchlist

    def list_positions(self, principal: Principal) -> list[PositionRecord]:
        return self.get_state(principal).positions

    def upsert_watchlist(
        self,
        principal: Principal,
        item: WatchlistItem,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        normalized = self._normalize_watchlist(item)
        with self._process_lock():
            document = self._load_document()
            bucket = self._bucket(document, principal)
            revision = self._check_revision(bucket, expected_revision)
            rows = self._watchlist_from_bucket(bucket)
            replaced = False
            next_rows: list[WatchlistItem] = []
            for existing in rows:
                if existing.code == normalized.code:
                    next_rows.append(normalized)
                    replaced = True
                else:
                    next_rows.append(existing)
            if not replaced:
                next_rows.append(normalized)
            bucket["watchlist"] = [row.model_dump(mode="json") for row in next_rows]
            bucket["revision"] = revision + 1
            document[principal.storage_key] = bucket
            self._save_document(document)
            return PrincipalMutation(revision=revision + 1, item=normalized)

    def delete_watchlist(
        self,
        principal: Principal,
        code: str,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        normalized_code = self._normalize_code(code)
        with self._process_lock():
            document = self._load_document()
            bucket = self._bucket(document, principal)
            revision = self._check_revision(bucket, expected_revision)
            rows = [row for row in self._watchlist_from_bucket(bucket) if row.code != normalized_code]
            bucket["watchlist"] = [row.model_dump(mode="json") for row in rows]
            bucket["revision"] = revision + 1
            document[principal.storage_key] = bucket
            self._save_document(document)
            return PrincipalMutation(revision=revision + 1, item=None)

    def upsert_position(
        self,
        principal: Principal,
        item: PositionRecord,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        normalized = self._normalize_position(item)
        with self._process_lock():
            document = self._load_document()
            bucket = self._bucket(document, principal)
            revision = self._check_revision(bucket, expected_revision)
            rows = self._positions_from_bucket(bucket)
            replaced = False
            next_rows: list[PositionRecord] = []
            for existing in rows:
                if existing.code == normalized.code:
                    next_rows.append(normalized)
                    replaced = True
                else:
                    next_rows.append(existing)
            if not replaced:
                next_rows.append(normalized)
            bucket["positions"] = [row.model_dump(mode="json") for row in next_rows]
            bucket["revision"] = revision + 1
            document[principal.storage_key] = bucket
            self._save_document(document)
            return PrincipalMutation(revision=revision + 1, item=normalized)

    def delete_position(
        self,
        principal: Principal,
        code: str,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        normalized_code = self._normalize_code(code)
        with self._process_lock():
            document = self._load_document()
            bucket = self._bucket(document, principal)
            revision = self._check_revision(bucket, expected_revision)
            rows = [row for row in self._positions_from_bucket(bucket) if row.code != normalized_code]
            bucket["positions"] = [row.model_dump(mode="json") for row in rows]
            bucket["revision"] = revision + 1
            document[principal.storage_key] = bucket
            self._save_document(document)
            return PrincipalMutation(revision=revision + 1, item=None)

    def import_legacy_watchlist_once(
        self,
        principal: Principal,
        items: Sequence[WatchlistItem],
    ) -> LegacyImportResult:
        with self._process_lock():
            document = self._load_document()
            bucket = self._bucket(document, principal)
            revision = self._coerce_revision(bucket.get("revision", 0))
            migrations = bucket.get("migrations")
            if not isinstance(migrations, dict):
                migrations = {}

            current = self._watchlist_from_bucket(bucket)
            if migrations.get(LEGACY_WATCHLIST_MIGRATION):
                return LegacyImportResult(False, "already_imported", revision, current)

            # Mark the migration even when canonical state already exists.  A
            # later browser request must never be able to resurrect old data.
            migrations[LEGACY_WATCHLIST_MIGRATION] = True
            bucket["migrations"] = migrations
            if current:
                bucket["revision"] = revision + 1
                document[principal.storage_key] = bucket
                self._save_document(document)
                return LegacyImportResult(False, "existing_state", revision + 1, current)

            imported: list[WatchlistItem] = []
            seen_codes: set[str] = set()
            for item in items:
                normalized = self._normalize_watchlist(item)
                if normalized.code in seen_codes:
                    continue
                seen_codes.add(normalized.code)
                imported.append(normalized)
                if len(imported) >= MAX_LEGACY_ITEMS:
                    break
            bucket["watchlist"] = [item.model_dump(mode="json") for item in imported]
            bucket["revision"] = revision + 1
            document[principal.storage_key] = bucket
            self._save_document(document)
            return LegacyImportResult(True, "applied", revision + 1, imported)

    def _check_revision(self, bucket: dict[str, Any], expected_revision: int | None) -> int:
        revision = self._coerce_revision(bucket.get("revision", 0))
        if expected_revision is not None and expected_revision != revision:
            raise RevisionConflict(expected_revision, revision)
        return revision

    def _load_document(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as error:
            raise UserStateUnavailable(f"state file is not valid UTF-8: {self.path}") from error
        except OSError as error:
            raise UserStateUnavailable(f"cannot read state file: {self.path}") from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise UserStateUnavailable(f"state file is malformed: {self.path}") from error
        if not isinstance(payload, dict):
            raise UserStateUnavailable(f"state file must contain an object: {self.path}")
        return payload

    def _save_document(self, document: dict[str, Any]) -> None:
        # Include the writer PID so a transiently-unlocked Windows process
        # cannot overwrite another writer's source file.  The stable lock file
        # still serializes the destination replacement.
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    descriptor = None
                    json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as error:
                raise UserStateUnavailable(f"cannot write temporary state file: {temporary}") from error
            deadline = time.monotonic() + 30.0
            while True:
                try:
                    temporary.replace(self.path)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise UserStateUnavailable(
                            f"cannot atomically replace state file: {self.path}"
                        )
                    # Windows may briefly retain a read handle after the
                    # other process releases the lock; retry while preserving
                    # the atomic replace contract.
                    time.sleep(0.01)
                except OSError as error:
                    raise UserStateUnavailable(f"cannot replace state file: {self.path}") from error
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @staticmethod
    def _bucket(document: dict[str, Any], principal: Principal) -> dict[str, Any]:
        raw = document.get(principal.storage_key)
        if not isinstance(raw, dict):
            raw = {
                "revision": 0,
                "watchlist": [],
                "positions": [],
                "migrations": {},
            }
            document[principal.storage_key] = raw
        return raw

    @staticmethod
    def _state_from_bucket(bucket: dict[str, Any]) -> PrincipalState:
        return PrincipalState(
            revision=JsonPrincipalStateRepository._coerce_revision(bucket.get("revision", 0)),
            watchlist=JsonPrincipalStateRepository._watchlist_from_bucket(bucket),
            positions=JsonPrincipalStateRepository._positions_from_bucket(bucket),
        )

    @staticmethod
    def _watchlist_from_bucket(bucket: dict[str, Any]) -> list[WatchlistItem]:
        raw = bucket.get("watchlist", [])
        if not isinstance(raw, list):
            return []
        rows: list[WatchlistItem] = []
        for value in raw:
            if not isinstance(value, dict):
                continue
            try:
                rows.append(WatchlistItem.model_validate(value))
            except Exception:
                continue
        return rows

    @staticmethod
    def _positions_from_bucket(bucket: dict[str, Any]) -> list[PositionRecord]:
        raw = bucket.get("positions", [])
        if not isinstance(raw, list):
            return []
        rows: list[PositionRecord] = []
        for value in raw:
            if not isinstance(value, dict):
                continue
            try:
                rows.append(PositionRecord.model_validate(value))
            except Exception:
                continue
        return rows

    @staticmethod
    def _coerce_revision(value: Any) -> int:
        try:
            revision = int(value)
        except (TypeError, ValueError):
            return 0
        return max(revision, 0)

    @staticmethod
    def _normalize_code(code: object) -> str:
        value = str(code).strip()
        if value.isdigit():
            value = value.zfill(6)
        if len(value) != 6 or not value.isdigit():
            raise ValueError("stock code must contain six digits")
        return value

    @classmethod
    def _normalize_watchlist(cls, item: WatchlistItem) -> WatchlistItem:
        return item.model_copy(update={"code": cls._normalize_code(item.code)})

    @classmethod
    def _normalize_position(cls, item: PositionRecord) -> PositionRecord:
        return item.model_copy(update={"code": cls._normalize_code(item.code)})


__all__ = ["JsonPrincipalStateRepository", "LEGACY_WATCHLIST_MIGRATION", "MAX_LEGACY_ITEMS"]
