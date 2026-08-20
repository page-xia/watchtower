"""app/eod_store.py 单元测试：日期归一、latest_date 缓存、快照后端、工厂。"""

from __future__ import annotations

import datetime as dt
import sys
import types

import pytest

from app.eod_store import (
    REQUEST_KEY,
    REQUEST_NAMESPACE,
    SNAPSHOT_KEY,
    SNAPSHOT_NAMESPACE,
    STOCK_NAMESPACE,
    PyMySqlEodStore,
    SnapshotEodStore,
    _normalize_value,
    build_eod_store,
)


class TestNormalizeValue:
    def test_date_to_yyyymmdd(self) -> None:
        assert _normalize_value(dt.date(2026, 8, 18)) == "20260818"

    def test_datetime_to_compact(self) -> None:
        assert _normalize_value(dt.datetime(2026, 8, 18, 9, 30, 5)) == "20260818093005"

    def test_time_to_hhmmss(self) -> None:
        assert _normalize_value(dt.time(9, 30, 5)) == "09:30:05"

    def test_plain_values_passthrough(self) -> None:
        assert _normalize_value("600000.SH") == "600000.SH"
        assert _normalize_value(12.5) == 12.5
        assert _normalize_value(None) is None


class _FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, args: tuple = ()) -> int:
        return len(self._rows)

    def fetchall(self) -> list[dict]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.closed = False

    def cursor(self, *_args: object, **_kwargs: object) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def close(self) -> None:
        self.closed = True


def _patch_pymysql(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    fake_module = types.ModuleType("pymysql")
    fake_cursors = types.ModuleType("pymysql.cursors")
    fake_cursors.DictCursor = object
    fake_module.cursors = fake_cursors
    fake_module.connect = lambda **_kwargs: _FakeConn(rows)
    monkeypatch.setitem(sys.modules, "pymysql", fake_module)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", fake_cursors)


class TestPyMySqlEodStore:
    def test_table_prefix(self) -> None:
        store = PyMySqlEodStore(db="watchtower_eod")
        assert store.table("moneyflow") == "eod_moneyflow"

    def test_query_normalizes_dates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pymysql(monkeypatch, [{"trade_date": dt.date(2026, 8, 18), "net": 1.5}])
        store = PyMySqlEodStore(db="watchtower_eod")
        rows = store.query("SELECT * FROM eod_moneyflow")
        assert rows == [{"trade_date": "20260818", "net": 1.5}]

    def test_latest_date_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        fake_module = types.ModuleType("pymysql")
        fake_cursors = types.ModuleType("pymysql.cursors")
        fake_cursors.DictCursor = object
        fake_module.cursors = fake_cursors

        def connect(**_kwargs: object) -> _FakeConn:
            calls.append("connect")
            return _FakeConn([{"d": "20260818"}])

        fake_module.connect = connect
        monkeypatch.setitem(sys.modules, "pymysql", fake_module)
        monkeypatch.setitem(sys.modules, "pymysql.cursors", fake_cursors)

        store = PyMySqlEodStore(db="watchtower_eod")
        assert store.latest_date("moneyflow") == "20260818"
        assert store.latest_date("moneyflow") == "20260818"
        assert len(calls) == 1
        store.invalidate_latest("moneyflow")
        assert store.latest_date("moneyflow") == "20260818"
        assert len(calls) == 2


class _FakeStateStore:
    available = True

    def __init__(self, docs: dict[tuple[str, str], object] | None = None) -> None:
        self._docs = dict(docs or {})
        self.get_calls = 0
        self.set_calls: list[tuple[str, str, object]] = []

    def get_json(self, namespace: str, key: str, default: object = None) -> object:
        self.get_calls += 1
        return self._docs.get((namespace, key), default)

    def set_json(self, namespace: str, key: str, value: object) -> None:
        self.set_calls.append((namespace, key, value))
        self._docs[(namespace, key)] = value

    def delete_json(self, namespace: str, key: str) -> None:
        self._docs.pop((namespace, key), None)


class TestSnapshotEodStore:
    def test_payload_and_stock_summary(self) -> None:
        doc = {
            "available": True,
            "trade_date": "20260818",
            "market": {"trade_date": "20260818"},
            "stocks": {"300476": {"flow_10d": [], "trade_date": "20260818"}},
        }
        state = _FakeStateStore({(SNAPSHOT_NAMESPACE, SNAPSHOT_KEY): doc})
        store = SnapshotEodStore(state)
        payload = store.eod_payload()
        assert payload["trade_date"] == "20260818"
        assert store.latest_date("moneyflow") == "20260818"
        assert store.stock_summary("300476") == {"flow_10d": [], "trade_date": "20260818"}
        assert store.stock_summary("000001") is None
        # 300s 内存缓存：重复读不再打 NoSQL（000001 未命中短缓存也只查一次）
        store.eod_payload()
        store.stock_summary("000001")
        assert state.get_calls == 2

    def test_stock_doc_fallback(self) -> None:
        """主快照未覆盖的个股：回退读单票摘要文档（按需补数履约结果）。"""
        doc = {"available": True, "trade_date": "20260818", "stocks": {}}
        state = _FakeStateStore(
            {
                (SNAPSHOT_NAMESPACE, SNAPSHOT_KEY): doc,
                (STOCK_NAMESPACE, "600519"): {"eod_available": True, "flow_10d": [], "trade_date": "20260818"},
            }
        )
        store = SnapshotEodStore(state)
        assert store.stock_summary("600519") == {"eod_available": True, "flow_10d": [], "trade_date": "20260818"}
        assert store.stock_summary("000001") is None

    def test_stock_doc_requires_eod_available(self) -> None:
        state = _FakeStateStore(
            {
                (SNAPSHOT_NAMESPACE, SNAPSHOT_KEY): {"available": True, "stocks": {}},
                (STOCK_NAMESPACE, "600519"): {"eod_available": False, "note": "无数据"},
            }
        )
        store = SnapshotEodStore(state)
        assert store.stock_summary("600519") is None

    def test_request_stock_registers_and_dedups(self) -> None:
        state = _FakeStateStore({(SNAPSHOT_NAMESPACE, SNAPSHOT_KEY): {"available": True, "stocks": {}}})
        store = SnapshotEodStore(state)
        assert store.request_stock("600519") is True
        assert store.request_stock("600519") is True  # 去重窗口内不重复写
        writes = [c for c in state.set_calls if c[:2] == (REQUEST_NAMESPACE, REQUEST_KEY)]
        assert len(writes) == 1
        doc = writes[0][2]
        assert "600519" in doc["codes"]
        # 已有请求上合并新代码
        assert store.request_stock("000001") is True
        doc2 = state.set_calls[-1][2]
        assert set(doc2["codes"]) == {"600519", "000001"}

    def test_request_stock_rejects_invalid_code(self) -> None:
        state = _FakeStateStore({})
        store = SnapshotEodStore(state)
        assert store.request_stock("abc") is False
        assert state.set_calls == []

    def test_missing_doc_returns_note(self) -> None:
        store = SnapshotEodStore(_FakeStateStore({}))
        payload = store.eod_payload()
        assert payload["available"] is False
        assert "push-prod" in payload["note"]

    def test_query_not_supported(self) -> None:
        store = SnapshotEodStore(_FakeStateStore({}))
        with pytest.raises(NotImplementedError):
            store.query("SELECT 1")


class _FakeSettings:
    def __init__(self, backend: str) -> None:
        self.eod_store_backend = backend
        self.eod_db_config = {"host": "127.0.0.1", "port": 3306, "user": "root", "pwd": "x", "db": "watchtower_eod"}
        self.cloudbase_env_id = ""
        self.cloudbase_api_token = ""


class TestBuildEodStore:
    def test_mysql_backend(self) -> None:
        store = build_eod_store(_FakeSettings("mysql"))
        assert isinstance(store, PyMySqlEodStore)
        assert store.db == "watchtower_eod"

    def test_snapshot_backend(self) -> None:
        store = build_eod_store(_FakeSettings("cloudbase_snapshot"))
        assert isinstance(store, SnapshotEodStore)
        assert store.available is False  # 无 cloudbase 凭据 → state_store 为 None
