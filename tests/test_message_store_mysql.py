from __future__ import annotations

from typing import Any

import pytest

from app.message_store import (
    MessageStore,
    MessageStoreError,
    _build_count_sql,
    _build_select_sql,
    _build_upsert_sql,
)


def make_mysql_config() -> dict[str, Any]:
    return {
        "host": "rm-test.mysql.rds.aliyuncs.com",
        "port": 3306,
        "user": "watcher",
        "pwd": "secret-pwd",
        "db": "watchtower_msg",
    }


def test_select_translation_eq_in_like_order_limit() -> None:
    sql, args = _build_select_sql(
        "message_event_links",
        [
            ("entity_type", "in.(sector,theme)"),
            ("name", "like.%机器人%"),
            ("code", "eq.300476"),
            ("select", "*"),
            ("order", "updated_at.desc,relevance.desc"),
            ("limit", "100"),
        ],
    )
    assert sql == (
        "SELECT * FROM message_event_links "
        "WHERE entity_type IN (%s,%s) AND name LIKE %s AND code = %s "
        "ORDER BY updated_at DESC, relevance DESC LIMIT 100"
    )
    assert args == ["sector", "theme", "%机器人%", "300476"]


def test_select_translation_field_list_and_asc() -> None:
    sql, args = _build_select_sql(
        "message_evidence_cache",
        [
            ("select", "cache_key"),
            ("scope", "eq.sector"),
            ("order", "cache_key.asc"),
            ("limit", "1"),
        ],
    )
    assert sql == "SELECT cache_key FROM message_evidence_cache WHERE scope = %s ORDER BY cache_key ASC LIMIT 1"
    assert args == ["sector"]


def test_select_translation_in_with_chinese_terms() -> None:
    sql, args = _build_select_sql(
        "message_evidence_cache",
        [("cache_key", "in.(算力,机器人,AI应用)")],
    )
    assert sql == "SELECT * FROM message_evidence_cache WHERE cache_key IN (%s,%s,%s)"
    assert args == ["算力", "机器人", "AI应用"]


def test_select_translation_no_params() -> None:
    sql, args = _build_select_sql("message_topics", [])
    assert sql == "SELECT * FROM message_topics"
    assert args == []


def test_select_translation_rejects_bad_identifier_and_operator() -> None:
    with pytest.raises(MessageStoreError):
        _build_select_sql("message_topics; DROP TABLE x", [])
    with pytest.raises(MessageStoreError):
        _build_select_sql("message_topics", [("na;me", "eq.x")])
    with pytest.raises(MessageStoreError):
        _build_select_sql("message_topics", [("name", "gte.5")])


def test_count_translation() -> None:
    assert _build_count_sql("message_event_links") == "SELECT COUNT(*) AS cnt FROM message_event_links"


def test_upsert_translation_updates_non_primary_keys() -> None:
    sql, args = _build_upsert_sql(
        "message_topics",
        [
            {"topic_id": "t1", "title": "标题1", "_openid": "watchtower"},
            {"topic_id": "t2", "title": "标题2", "_openid": "watchtower"},
        ],
    )
    assert sql == (
        "INSERT INTO message_topics (topic_id, title, _openid) VALUES (%s,%s,%s), (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE title = VALUES(title), _openid = VALUES(_openid)"
    )
    assert args == ["t1", "标题1", "watchtower", "t2", "标题2", "watchtower"]


def test_upsert_translation_multi_column_primary_key() -> None:
    sql, _ = _build_upsert_sql(
        "message_event_links",
        [{"event_id": "e1", "entity_type": "stock", "code": "300476", "name": "胜宏科技", "relevance": 0.9}],
    )
    assert "ON DUPLICATE KEY UPDATE relevance = VALUES(relevance)" in sql
    # 主键列不出现在更新列表里。
    assert "event_id = VALUES(event_id)" not in sql
    assert "entity_type = VALUES(entity_type)" not in sql


def test_upsert_translation_rejects_empty_rows() -> None:
    with pytest.raises(MessageStoreError):
        _build_upsert_sql("message_topics", [])


def test_mysql_backend_available_and_db_file() -> None:
    store = MessageStore(mysql_config=make_mysql_config())
    assert store.available is True
    assert store.db_file == "mysql://rm-test.mysql.rds.aliyuncs.com/watchtower_msg"
    assert "secret-pwd" not in store.db_file
    store.close()

    incomplete = MessageStore(mysql_config={"host": "rm-test", "user": "", "db": ""})
    assert incomplete.available is False
    assert incomplete.db_file == "mysql://unconfigured"
    incomplete.close()


def test_from_settings_mysql_backend_calls_ensure_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(MessageStore, "ensure_schema", lambda self: called.append(True))

    class FakeSettings:
        message_store_backend = "mysql"
        message_store_cache_seconds = 60.0
        msg_mysql_config = make_mysql_config()

    store = MessageStore.from_settings(FakeSettings())
    assert called == [True]
    assert store.available is True
    assert store.db_file.startswith("mysql://")
    store.close()


def test_from_settings_cloudbase_backend_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_ensure_schema(self: MessageStore) -> None:
        raise AssertionError("cloudbase backend must not call ensure_schema")

    monkeypatch.setattr(MessageStore, "ensure_schema", fail_ensure_schema)

    class FakeSettings:
        message_store_backend = "cloudbase_mysql"
        cloudbase_env_id = "env-1"
        cloudbase_api_token = "token"
        cloudbase_mysql_instance = "default"
        cloudbase_mysql_schema = "env-1"
        cloudbase_api_base_url = ""
        cloudbase_api_timeout_seconds = 5.0
        message_store_cache_seconds = 60.0
        cloudbase_mysql_openid = "watchtower"

    store = MessageStore.from_settings(FakeSettings())
    assert store.available is True
    assert store.db_file == "cloudbase_mysql://env-1/default/env-1"
    store.close()


def test_ensure_schema_requires_mysql_mode() -> None:
    store = MessageStore(env_id="", token="", instance="default", schema="")
    with pytest.raises(MessageStoreError):
        store.ensure_schema()
    store.close()


def test_upsert_many_uses_mysql_transport_without_httpx() -> None:
    """直连模式下 _upsert_many 走 SQL 翻译 + 连接池，不触碰 httpx。"""
    executed: list[tuple[str, tuple[Any, ...] | None]] = []

    class FakeCursor:
        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, sql: str, args: Any = None) -> None:
            executed.append((sql, tuple(args) if args else None))

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def ping(self, reconnect: bool = True) -> None:
            return None

        def close(self) -> None:
            return None

    store = MessageStore(mysql_config=make_mysql_config())
    pool = store._get_mysql_pool()
    pool._connect = lambda: FakeConn()  # type: ignore[method-assign]
    rows = [
        {"_openid": "watchtower", "scope": "stock", "cache_key": "300476", "payload": "[]", "built_at": "t", "updated_at": "t"}
        for _ in range(205)
    ]
    store._upsert_many("message_evidence_cache", rows)
    # 205 行按 100 分块 = 3 条 INSERT，均带 ON DUPLICATE KEY UPDATE。
    assert len(executed) == 3
    assert all("ON DUPLICATE KEY UPDATE" in sql for sql, _ in executed)
    assert executed[0][1] is not None and len(executed[0][1]) == 100 * 6
    assert executed[2][1] is not None and len(executed[2][1]) == 5 * 6
    store.close()
