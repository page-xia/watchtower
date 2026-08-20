"""Principal-scoped personal-state repository backed by RDS MySQL.

The repository deliberately keeps the SQL small and explicit.  Every query
that touches personal rows carries both pieces of the principal key, and every
mutation locks/creates the state row before changing data and advancing its
revision.  This prevents a missing or stale client identity from ever falling
back to the legacy global JSON files.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

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


MYSQL_SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS principal_states (
        principal_type VARCHAR(32) NOT NULL,
        principal_id VARCHAR(128) NOT NULL,
        revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
        created_at DATETIME(3) NOT NULL,
        updated_at DATETIME(3) NOT NULL,
        PRIMARY KEY (principal_type, principal_id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS principal_watchlist_items (
        principal_type VARCHAR(32) NOT NULL,
        principal_id VARCHAR(128) NOT NULL,
        code CHAR(6) NOT NULL,
        name VARCHAR(64) NOT NULL DEFAULT '',
        themes_json JSON NOT NULL,
        core TINYINT(1) NOT NULL DEFAULT 0,
        position TINYINT(1) NOT NULL DEFAULT 1,
        notes VARCHAR(1000) NOT NULL DEFAULT '',
        created_at DATETIME(3) NOT NULL,
        updated_at DATETIME(3) NOT NULL,
        PRIMARY KEY (principal_type, principal_id, code),
        KEY idx_watchlist_principal (principal_type, principal_id, updated_at)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS principal_positions (
        principal_type VARCHAR(32) NOT NULL,
        principal_id VARCHAR(128) NOT NULL,
        code CHAR(6) NOT NULL,
        payload_json JSON NOT NULL,
        created_at DATETIME(3) NOT NULL,
        updated_at DATETIME(3) NOT NULL,
        PRIMARY KEY (principal_type, principal_id, code),
        KEY idx_positions_principal (principal_type, principal_id, updated_at)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS principal_migrations (
        principal_type VARCHAR(32) NOT NULL,
        principal_id VARCHAR(128) NOT NULL,
        migration_key VARCHAR(64) NOT NULL,
        result VARCHAR(32) NOT NULL,
        created_at DATETIME(3) NOT NULL,
        PRIMARY KEY (principal_type, principal_id, migration_key)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
]

# ``CREATE TABLE IF NOT EXISTS`` does not add columns to a table created by an
# earlier image.  Keep this tiny forward migration in bootstrap so upgrading
# an installation made before ``WatchlistItem.position`` was persisted is
# safe and idempotent on MySQL 8/RDS.
MYSQL_SCHEMA_MIGRATION_STATEMENTS: list[str] = [
    "ALTER TABLE principal_watchlist_items ADD COLUMN IF NOT EXISTS position TINYINT(1) NOT NULL DEFAULT 1 AFTER core",
]


def _utc_now() -> datetime:
    """Return a naive UTC timestamp accepted by MySQL DATETIME columns."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


class MySqlPrincipalStateRepository:
    """Implementation of :class:`PrincipalStateRepository` for RDS MySQL.

    ``connection_factory`` is injectable for tests and for callers that own a
    connection pool.  Without one, a short-lived pymysql connection is opened
    for each operation; the personal state workload is intentionally bounded
    and this keeps transaction ownership unambiguous.
    """

    def __init__(
        self,
        mysql_config: Mapping[str, Any] | None = None,
        *,
        connection_factory: Callable[[], Any] | None = None,
        connect_timeout: float = 5.0,
        pool_size: int = 4,
    ) -> None:
        self.mysql_config = dict(mysql_config or {})
        self.connection_factory = connection_factory
        self.connect_timeout = max(0.1, float(connect_timeout))
        self.pool_size = max(1, int(pool_size))
        self.db_name = str(self.mysql_config.get("db") or self.mysql_config.get("database") or "").strip()

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> "MySqlPrincipalStateRepository":
        backend = str(getattr(settings, "user_store_backend", "mysql") or "").strip().lower()
        if backend != "mysql":
            raise ValueError("MySqlPrincipalStateRepository requires WATCH_USER_STORE_BACKEND=mysql")
        config = getattr(settings, "user_mysql_config", None)
        if not isinstance(config, Mapping):
            config = {
                "host": getattr(settings, "user_mysql_host", ""),
                "port": getattr(settings, "user_mysql_port", 3306),
                "user": getattr(settings, "user_mysql_user", ""),
                "pwd": getattr(settings, "user_mysql_pwd", ""),
                "db": getattr(settings, "user_mysql_db", "watchtower_user"),
            }
        return cls(
            config,
            connection_factory=connection_factory,
            connect_timeout=float(getattr(settings, "user_mysql_connect_timeout", 5.0)),
            pool_size=int(getattr(settings, "user_mysql_pool_size", 4)),
        )

    @property
    def configured(self) -> bool:
        return bool(
            str(self.mysql_config.get("host") or "").strip()
            and str(self.mysql_config.get("user") or "").strip()
            and str(self.mysql_config.get("db") or self.mysql_config.get("database") or "").strip()
        )

    @property
    def connection_target(self) -> str:
        host = str(self.mysql_config.get("host") or "").strip() or "unconfigured"
        db = self.db_name or "unconfigured"
        port = self.mysql_config.get("port", 3306)
        return f"mysql://{host}:{port}/{db}"

    def _connect(self) -> Any:
        if self.connection_factory is not None:
            try:
                return self.connection_factory()
            except UserStateUnavailable:
                raise
            except Exception as error:
                raise UserStateUnavailable("cannot connect to user MySQL store") from error
        if not self.configured:
            raise UserStateUnavailable("user MySQL store is not configured")
        try:
            import pymysql

            kwargs = {
                "host": self.mysql_config.get("host"),
                "port": int(self.mysql_config.get("port", 3306)),
                "user": self.mysql_config.get("user"),
                "password": self.mysql_config.get("pwd", self.mysql_config.get("password", "")),
                "database": self.db_name,
                "connect_timeout": self.connect_timeout,
                "charset": "utf8mb4",
                "autocommit": False,
            }
            # DictCursor makes row mapping deterministic; injected fakes need
            # not implement cursorclass and are called through the factory path.
            kwargs["cursorclass"] = pymysql.cursors.DictCursor
            return pymysql.connect(**kwargs)
        except Exception as error:  # pragma: no cover - exercised with injected failures
            raise UserStateUnavailable(f"cannot connect to user MySQL store: {self.connection_target}") from error

    @staticmethod
    def _close(connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            pass

    @staticmethod
    def _rollback(connection: Any) -> None:
        try:
            connection.rollback()
        except Exception:
            pass

    def ensure_schema(self) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                for statement in MYSQL_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
                for statement in MYSQL_SCHEMA_MIGRATION_STATEMENTS:
                    cursor.execute(statement)
            connection.commit()
        except UserStateUnavailable:
            self._rollback(connection)
            raise
        except Exception as error:
            self._rollback(connection)
            raise UserStateUnavailable("cannot initialize user MySQL schema") from error
        finally:
            self._close(connection)

    def get_state(self, principal: Principal) -> PrincipalState:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                revision = self._read_revision(cursor, principal, for_update=False)
                watchlist = self._read_watchlist(cursor, principal)
                positions = self._read_positions(cursor, principal)
            return PrincipalState(revision=revision, watchlist=watchlist, positions=positions)
        except UserStateUnavailable:
            raise
        except Exception as error:
            raise UserStateUnavailable("cannot read user MySQL state") from error
        finally:
            self._close(connection)

    def list_watchlist(self, principal: Principal) -> list[WatchlistItem]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                return self._read_watchlist(cursor, principal)
        except UserStateUnavailable:
            raise
        except Exception as error:
            raise UserStateUnavailable("cannot read user MySQL watchlist") from error
        finally:
            self._close(connection)

    def list_positions(self, principal: Principal) -> list[PositionRecord]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                return self._read_positions(cursor, principal)
        except UserStateUnavailable:
            raise
        except Exception as error:
            raise UserStateUnavailable("cannot read user MySQL positions") from error
        finally:
            self._close(connection)

    def upsert_watchlist(
        self,
        principal: Principal,
        item: WatchlistItem,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        normalized = self._normalize_watchlist(item)

        def operation(cursor: Any, now: datetime, revision: int) -> WatchlistItem:
            cursor.execute(
                """
                INSERT INTO principal_watchlist_items
                    (principal_type, principal_id, code, name, themes_json, core, position, notes, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE name=VALUES(name), themes_json=VALUES(themes_json),
                    core=VALUES(core), position=VALUES(position), notes=VALUES(notes), updated_at=VALUES(updated_at)
                """,
                (
                    principal.type,
                    principal.id,
                    normalized.code,
                    normalized.name,
                    json.dumps(normalized.themes, ensure_ascii=False),
                    int(bool(normalized.core)),
                    int(bool(normalized.position)),
                    normalized.notes,
                    now,
                    now,
                ),
            )
            return normalized

        return self._mutate(principal, expected_revision, operation)

    def delete_watchlist(
        self,
        principal: Principal,
        code: str,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        normalized_code = self._normalize_code(code)

        def operation(cursor: Any, _now: datetime, _revision: int) -> None:
            cursor.execute(
                "DELETE FROM principal_watchlist_items WHERE principal_type = %s AND principal_id = %s AND code = %s",
                (principal.type, principal.id, normalized_code),
            )
            return None

        return self._mutate(principal, expected_revision, operation)

    def upsert_position(
        self,
        principal: Principal,
        item: PositionRecord,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        normalized = self._normalize_position(item)

        def operation(cursor: Any, now: datetime, revision: int) -> PositionRecord:
            cursor.execute(
                """
                INSERT INTO principal_positions
                    (principal_type, principal_id, code, payload_json, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE payload_json=VALUES(payload_json), updated_at=VALUES(updated_at)
                """,
                (
                    principal.type,
                    principal.id,
                    normalized.code,
                    json.dumps(normalized.model_dump(mode="json"), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return normalized

        return self._mutate(principal, expected_revision, operation)

    def delete_position(
        self,
        principal: Principal,
        code: str,
        *,
        expected_revision: int | None = None,
    ) -> PrincipalMutation:
        normalized_code = self._normalize_code(code)

        def operation(cursor: Any, _now: datetime, _revision: int) -> None:
            cursor.execute(
                "DELETE FROM principal_positions WHERE principal_type = %s AND principal_id = %s AND code = %s",
                (principal.type, principal.id, normalized_code),
            )
            return None

        return self._mutate(principal, expected_revision, operation)

    def import_legacy_watchlist_once(
        self,
        principal: Principal,
        items: Sequence[WatchlistItem],
    ) -> LegacyImportResult:
        def operation(cursor: Any, now: datetime, revision: int) -> tuple[bool, list[WatchlistItem], str]:
            cursor.execute(
                "SELECT migration_key, result FROM principal_migrations "
                "WHERE principal_type = %s AND principal_id = %s AND migration_key = %s",
                (principal.type, principal.id, LEGACY_WATCHLIST_MIGRATION),
            )
            if cursor.fetchone():
                current = self._read_watchlist(cursor, principal)
                return False, current, "already_imported"
            current = self._read_watchlist(cursor, principal)
            if current:
                reason = "existing_state"
                imported: list[WatchlistItem] = []
            else:
                reason = "applied"
                imported = []
                seen: set[str] = set()
                for item in items:
                    normalized = self._normalize_watchlist(item)
                    if normalized.code in seen:
                        continue
                    seen.add(normalized.code)
                    imported.append(normalized)
                    if len(imported) >= MAX_LEGACY_ITEMS:
                        break
                for normalized in imported:
                    cursor.execute(
                        """
                        INSERT INTO principal_watchlist_items
                            (principal_type, principal_id, code, name, themes_json, core, position, notes, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE code=VALUES(code)
                        """,
                        (
                            principal.type,
                            principal.id,
                            normalized.code,
                            normalized.name,
                            json.dumps(normalized.themes, ensure_ascii=False),
                            int(bool(normalized.core)),
                            int(bool(normalized.position)),
                            normalized.notes,
                            now,
                            now,
                        ),
                    )
            cursor.execute(
                """
                INSERT INTO principal_migrations
                    (principal_type, principal_id, migration_key, result, created_at)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE result=VALUES(result)
                """,
                (principal.type, principal.id, LEGACY_WATCHLIST_MIGRATION, reason, now),
            )
            return bool(imported), imported or current, reason

        (applied, imported, reason), next_revision = self._mutate_with_result(principal, None, operation)
        return LegacyImportResult(applied=applied, reason=reason, revision=next_revision, items=imported)

    def _mutate(self, principal: Principal, expected_revision: int | None, operation: Callable[[Any, datetime, int], Any]) -> PrincipalMutation:
        connection = self._connect()
        try:
            self._begin(connection)
            with connection.cursor() as cursor:
                self._ensure_principal_state(cursor, principal)
                revision = self._read_revision(cursor, principal, for_update=True)
                if expected_revision is not None and int(expected_revision) != revision:
                    raise RevisionConflict(int(expected_revision), revision)
                item = operation(cursor, _utc_now(), revision)
                next_revision = self._increment_revision(cursor, principal, revision)
            connection.commit()
            return PrincipalMutation(revision=next_revision, item=item)
        except RevisionConflict:
            self._rollback(connection)
            raise
        except UserStateUnavailable:
            self._rollback(connection)
            raise
        except Exception as error:
            self._rollback(connection)
            raise UserStateUnavailable("cannot write user MySQL state") from error
        finally:
            self._close(connection)

    def _mutate_with_result(self, principal: Principal, expected_revision: int | None, operation: Callable[[Any, datetime, int], tuple[Any, Any, str]]) -> tuple[tuple[Any, Any, str], int]:
        connection = self._connect()
        try:
            self._begin(connection)
            with connection.cursor() as cursor:
                self._ensure_principal_state(cursor, principal)
                revision = self._read_revision(cursor, principal, for_update=True)
                if expected_revision is not None and int(expected_revision) != revision:
                    raise RevisionConflict(int(expected_revision), revision)
                result = operation(cursor, _utc_now(), revision)
                # A repeated migration is a read-only idempotent request: the
                # marker already exists, so do not manufacture a new revision.
                if isinstance(result, tuple) and len(result) >= 3 and result[2] == "already_imported":
                    next_revision = revision
                else:
                    next_revision = self._increment_revision(cursor, principal, revision)
            connection.commit()
            return result, next_revision
        except RevisionConflict:
            self._rollback(connection)
            raise
        except UserStateUnavailable:
            self._rollback(connection)
            raise
        except Exception as error:
            self._rollback(connection)
            raise UserStateUnavailable("cannot write user MySQL state") from error
        finally:
            self._close(connection)

    @staticmethod
    def _begin(connection: Any) -> None:
        begin = getattr(connection, "begin", None)
        if callable(begin):
            begin()

    @staticmethod
    def _ensure_principal_state(cursor: Any, principal: Principal) -> None:
        now = _utc_now()
        cursor.execute(
            """
            INSERT INTO principal_states (principal_type, principal_id, revision, created_at, updated_at)
            VALUES (%s,%s,0,%s,%s)
            ON DUPLICATE KEY UPDATE principal_id=VALUES(principal_id)
            """,
            (principal.type, principal.id, now, now),
        )

    @staticmethod
    def _read_revision(cursor: Any, principal: Principal, *, for_update: bool) -> int:
        sql = "SELECT revision FROM principal_states WHERE principal_type = %s AND principal_id = %s"
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (principal.type, principal.id))
        row = cursor.fetchone()
        if not row:
            return 0
        return max(int(MySqlPrincipalStateRepository._row_get(row, "revision", 0)), 0)

    @staticmethod
    def _increment_revision(cursor: Any, principal: Principal, revision: int) -> int:
        now = _utc_now()
        cursor.execute(
            "UPDATE principal_states SET revision = revision + 1, updated_at = %s "
            "WHERE principal_type = %s AND principal_id = %s",
            (now, principal.type, principal.id),
        )
        # The caller locked the row in this transaction; revision is therefore
        # deterministic without another round trip.
        return revision + 1

    @classmethod
    def _read_watchlist(cls, cursor: Any, principal: Principal) -> list[WatchlistItem]:
        cursor.execute(
            "SELECT code, name, themes_json, core, position, notes, created_at, updated_at "
            "FROM principal_watchlist_items WHERE principal_type = %s AND principal_id = %s ORDER BY code",
            (principal.type, principal.id),
        )
        rows = cursor.fetchall() or []
        result: list[WatchlistItem] = []
        for row in rows:
            themes = cls._json_value(cls._row_get(row, "themes_json", []), [])
            if not isinstance(themes, list):
                themes = []
            try:
                result.append(
                    WatchlistItem(
                        code=cls._normalize_code(cls._row_get(row, "code", "")),
                        name=str(cls._row_get(row, "name", "") or ""),
                        themes=[str(value) for value in themes],
                        core=bool(cls._row_get(row, "core", False)),
                        position=bool(cls._row_get(row, "position", True)),
                        notes=str(cls._row_get(row, "notes", "") or ""),
                    )
                )
            except Exception:
                continue
        return result

    @classmethod
    def _read_positions(cls, cursor: Any, principal: Principal) -> list[PositionRecord]:
        cursor.execute(
            "SELECT code, payload_json, created_at, updated_at FROM principal_positions "
            "WHERE principal_type = %s AND principal_id = %s ORDER BY code",
            (principal.type, principal.id),
        )
        rows = cursor.fetchall() or []
        result: list[PositionRecord] = []
        for row in rows:
            payload = cls._json_value(cls._row_get(row, "payload_json", {}), {})
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("code", cls._row_get(row, "code", ""))
            try:
                result.append(PositionRecord.model_validate(payload))
            except Exception:
                continue
        return result

    @staticmethod
    def _row_get(row: Any, key: str, default: Any = None) -> Any:
        if isinstance(row, Mapping):
            return row.get(key, default)
        # DictCursor is used in production; tuple fallback keeps simple fakes
        # useful and maps only the columns selected by this module.
        tuple_indices = {
            "revision": 0,
            "code": 0,
            "name": 1,
            "themes_json": 2,
            "core": 3,
            "position": 4,
            "notes": 5,
            "payload_json": 1,
        }
        index = tuple_indices.get(key)
        if index is None:
            return default
        try:
            return row[index]
        except (IndexError, KeyError, TypeError):
            return default

    @staticmethod
    def _json_value(value: Any, default: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        if value is None:
            return default
        try:
            return json.loads(str(value))
        except (TypeError, ValueError):
            return default

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


MySQLPrincipalStateRepository = MySqlPrincipalStateRepository

__all__ = [
    "LEGACY_WATCHLIST_MIGRATION",
    "MAX_LEGACY_ITEMS",
    "MYSQL_SCHEMA_STATEMENTS",
    "MYSQL_SCHEMA_MIGRATION_STATEMENTS",
    "MySqlPrincipalStateRepository",
    "MySQLPrincipalStateRepository",
]
