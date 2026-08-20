from __future__ import annotations

from typing import Any

import pytest

from app.models import PositionRecord, WatchlistItem
from app.principal import Principal
from app.user_state import RevisionConflict, UserStateUnavailable
from app.user_state_mysql import (
    MYSQL_SCHEMA_MIGRATION_STATEMENTS,
    MYSQL_SCHEMA_STATEMENTS,
    MySqlPrincipalStateRepository,
)


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.execute_calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeCursor":
        self.connection.last_cursor = self
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, args: Any = None) -> None:
        values = tuple(args) if args is not None else None
        self.execute_calls.append((sql, values))
        self._rows = self.connection.handle(sql, values)

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class FakeConnection:
    """Tiny SQL-aware fake used to assert repository contracts without RDS."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, str], dict[str, Any]] = {}
        self.watchlist: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.positions: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.migrations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.last_cursor: FakeCursor | None = None
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def begin(self) -> None:
        return None

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        return None

    def ping(self, reconnect: bool = True) -> None:
        return None

    def handle(self, sql: str, args: tuple[Any, ...] | None) -> list[dict[str, Any]]:
        compact = " ".join(sql.split()).upper()
        vals = args or ()
        if compact.startswith("SELECT REVISION FROM PRINCIPAL_STATES"):
            key = (str(vals[0]), str(vals[1]))
            row = self.state.get(key)
            return [{"revision": row["revision"]}] if row else []
        if compact.startswith("SELECT CODE, NAME, THEMES_JSON"):
            key = (str(vals[0]), str(vals[1]))
            return [v for k, v in self.watchlist.items() if k[:2] == key]
        if compact.startswith("SELECT CODE, PAYLOAD_JSON"):
            key = (str(vals[0]), str(vals[1]))
            return [v for k, v in self.positions.items() if k[:2] == key]
        if compact.startswith("SELECT MIGRATION_KEY, RESULT"):
            row = self.migrations.get((str(vals[0]), str(vals[1]), str(vals[2])))
            return [row] if row else []
        if compact.startswith("INSERT INTO PRINCIPAL_STATES"):
            key = (str(vals[0]), str(vals[1]))
            self.state.setdefault(key, {"revision": 0})
            return []
        if compact.startswith("INSERT INTO PRINCIPAL_WATCHLIST_ITEMS"):
            key = (str(vals[0]), str(vals[1]), str(vals[2]))
            self.watchlist[key] = {
                "principal_type": key[0],
                "principal_id": key[1],
                "code": key[2],
                "name": vals[3],
                "themes_json": vals[4],
                "core": vals[5],
                "position": vals[6],
                "notes": vals[7],
            }
            return []
        if compact.startswith("INSERT INTO PRINCIPAL_POSITIONS"):
            key = (str(vals[0]), str(vals[1]), str(vals[2]))
            self.positions[key] = {
                "principal_type": key[0],
                "principal_id": key[1],
                "code": key[2],
                "payload_json": vals[3],
            }
            return []
        if compact.startswith("INSERT INTO PRINCIPAL_MIGRATIONS"):
            key = (str(vals[0]), str(vals[1]), str(vals[2]))
            self.migrations[key] = {"migration_key": key[2], "result": vals[3]}
            return []
        if compact.startswith("DELETE FROM PRINCIPAL_WATCHLIST_ITEMS"):
            self.watchlist.pop((str(vals[0]), str(vals[1]), str(vals[2])), None)
            return []
        if compact.startswith("DELETE FROM PRINCIPAL_POSITIONS"):
            self.positions.pop((str(vals[0]), str(vals[1]), str(vals[2])), None)
            return []
        if compact.startswith("UPDATE PRINCIPAL_STATES SET REVISION"):
            key = (str(vals[-2]), str(vals[-1]))
            self.state.setdefault(key, {"revision": 0})["revision"] += 1
            return []
        return []


def _principal(name: str = "alice") -> Principal:
    return Principal("anonymous_client", f"{name}-0001")


def test_mysql_repository_scopes_every_query_by_principal() -> None:
    connection = FakeConnection()
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: connection)

    repo.list_watchlist(_principal())

    assert connection.last_cursor is not None
    sql, args = connection.last_cursor.execute_calls[0]
    normalized = " ".join(sql.split())
    assert "principal_type = %s" in normalized
    assert "principal_id = %s" in normalized
    assert args is not None and args[:2] == ("anonymous_client", "alice-0001")


def test_mysql_mutation_updates_revision_in_one_transaction() -> None:
    connection = FakeConnection()
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: connection)

    result = repo.upsert_watchlist(_principal(), WatchlistItem(code="300476", name="胜宏科技"))

    assert result.revision == 1
    assert connection.commit_count == 1
    assert connection.rollback_count == 0
    assert any("principal_type" in sql and "principal_id" in sql for sql, _ in connection.last_cursor.execute_calls)


def test_mysql_revisions_are_monotonic_across_mutations() -> None:
    connection = FakeConnection()
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: connection)
    principal = _principal()
    first = repo.upsert_watchlist(principal, WatchlistItem(code="300476", name="胜宏科技"))
    second = repo.delete_watchlist(principal, "300476", expected_revision=first.revision)
    assert (first.revision, second.revision) == (1, 2)


def test_mysql_state_isolated_and_json_columns_map_to_models() -> None:
    connection = FakeConnection()
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: connection)
    alice, bob = _principal("alice"), _principal("bob")

    repo.upsert_watchlist(alice, WatchlistItem(code="300476", name="胜宏科技", themes=["算力"], core=True))
    repo.upsert_position(bob, PositionRecord(code="300476", quantity=100, available_quantity=0, cost=10))

    assert [item.code for item in repo.list_watchlist(alice)] == ["300476"]
    assert repo.list_watchlist(bob) == []
    assert repo.list_positions(alice) == []
    position = repo.list_positions(bob)[0]
    assert position.code == "300476" and position.quantity == 100


def test_mysql_watchlist_roundtrip_preserves_position_flag() -> None:
    connection = FakeConnection()
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: connection)
    principal = _principal()

    repo.upsert_watchlist(
        principal,
        WatchlistItem(code="300476", name="胜宏科技", position=False),
    )

    assert repo.list_watchlist(principal)[0].position is False


def test_mysql_stale_revision_rolls_back() -> None:
    connection = FakeConnection()
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: connection)
    principal = _principal()
    first = repo.upsert_watchlist(principal, WatchlistItem(code="300476", name="胜宏科技"))

    with pytest.raises(RevisionConflict):
        repo.delete_watchlist(principal, "300476", expected_revision=0)

    assert connection.commit_count == 1
    assert connection.rollback_count == 1


def test_mysql_legacy_import_is_one_time_and_repeated_call_does_not_advance_revision() -> None:
    connection = FakeConnection()
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: connection)
    principal = _principal()

    first = repo.import_legacy_watchlist_once(
        principal,
        [WatchlistItem(code="300476", name="胜宏科技")],
    )
    repeated = repo.import_legacy_watchlist_once(
        principal,
        [WatchlistItem(code="000001", name="平安银行")],
    )

    assert first.applied is True and first.revision == 1
    assert repeated.applied is False and repeated.reason == "already_imported"
    assert repeated.revision == 1
    assert [item.code for item in repeated.items] == ["300476"]


@pytest.mark.parametrize("operation", ["upsert_watchlist", "delete_watchlist", "upsert_position", "delete_position"])
def test_every_mysql_mutation_statement_is_principal_scoped(operation: str) -> None:
    connection = FakeConnection()
    repo = MySqlPrincipalStateRepository(connection_factory=lambda: connection)
    principal = _principal()
    if operation == "upsert_watchlist":
        repo.upsert_watchlist(principal, WatchlistItem(code="300476", name="胜宏科技"))
    elif operation == "delete_watchlist":
        repo.delete_watchlist(principal, "300476")
    elif operation == "upsert_position":
        repo.upsert_position(principal, PositionRecord(code="300476"))
    else:
        repo.delete_position(principal, "300476")

    assert connection.last_cursor is not None
    for sql, _args in connection.last_cursor.execute_calls:
        normalized = " ".join(sql.split()).lower()
        if normalized.startswith(("select ", "insert ", "update ", "delete ")):
            assert "principal_type" in normalized
            assert "principal_id" in normalized


def test_mysql_connection_failure_is_unavailable() -> None:
    def fail() -> Any:
        raise OSError("connection refused")

    repo = MySqlPrincipalStateRepository(connection_factory=fail)
    with pytest.raises(UserStateUnavailable):
        repo.list_watchlist(_principal())


def test_schema_contains_all_principal_tables_and_scoped_keys() -> None:
    joined = "\n".join(MYSQL_SCHEMA_STATEMENTS)
    for table in ("principal_states", "principal_watchlist_items", "principal_positions", "principal_migrations"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
    assert "PRIMARY KEY (principal_type, principal_id, code)" in joined
    assert any("ADD COLUMN IF NOT EXISTS position" in statement for statement in MYSQL_SCHEMA_MIGRATION_STATEMENTS)


def test_from_settings_uses_mysql_configuration() -> None:
    connection = FakeConnection()

    class Settings:
        user_store_backend = "mysql"
        user_mysql_config = {"host": "rds", "port": 3306, "user": "watcher", "pwd": "secret", "db": "watchtower_user"}
        user_mysql_connect_timeout = 3
        user_mysql_pool_size = 4

    repo = MySqlPrincipalStateRepository.from_settings(Settings(), connection_factory=lambda: connection)
    assert repo.db_name == "watchtower_user"


def test_app_settings_reads_user_store_env_without_leaking_password(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import AppSettings

    monkeypatch.setenv("WATCH_USER_STORE_BACKEND", "mysql")
    monkeypatch.setenv("WATCH_USER_MYSQL_HOST", "rds")
    monkeypatch.setenv("WATCH_USER_MYSQL_USER", "watcher")
    monkeypatch.setenv("WATCH_USER_MYSQL_PWD", "secret")
    monkeypatch.setenv("WATCH_USER_MYSQL_DB", "watchtower_user")
    settings = AppSettings()
    assert settings.user_store_backend == "mysql"
    assert settings.user_mysql_config["pwd"] == "secret"
    status = settings.public_source_status
    assert status["user_store_db"] == "watchtower_user"
    assert "secret" not in repr(status)
