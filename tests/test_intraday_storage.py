import json
import sqlite3

import pytest
from pydantic import ValidationError

from app.models import PositionRecord
from app.storage import CloudBackedPositionStore, CloudBackedWatchlistStore, PositionStore
from app.trajectory_store import IntradayWatchtowerStore


def test_position_store_upserts_persists_and_deletes(tmp_path) -> None:
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    first = PositionRecord(
        code="300308",
        name="中际旭创",
        cost=102.5,
        quantity=1200,
        available_quantity=800,
        t_allocation_pct=25,
    )
    updated = first.model_copy(update={"cost": 101.8, "available_quantity": 1200})

    store.upsert(first)
    store.upsert(updated)

    items = store.list_items()
    assert len(items) == 1
    assert items[0].cost == 101.8
    assert json.loads(path.read_text(encoding="utf-8"))[0]["t_allocation_pct"] == 25
    assert store.delete("300308") is True
    assert store.list_items() == []


class MemoryStateStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def get_json(self, namespace: str, key: str, default=None):
        return self.values.get((namespace, key), default)

    def set_json(self, namespace: str, key: str, value) -> None:
        self.values[(namespace, key)] = value


def test_cloud_backed_watchlist_store_prefers_cloud_state(tmp_path) -> None:
    cloud = MemoryStateStore()
    cloud.set_json(
        "settings",
        "watchlist",
        [{"code": "300476", "name": "胜宏科技", "themes": ["PCB"], "core": True, "position": False}],
    )
    local_path = tmp_path / "watchlist.json"
    local_path.write_text(json.dumps([{"code": "000001", "name": "平安银行"}]), encoding="utf-8")
    store = CloudBackedWatchlistStore(local_path, cloud)

    items = store.list_items()
    saved = store.upsert(items[0].model_copy(update={"notes": "云端保存"}))

    assert [item.code for item in items] == ["300476"]
    assert saved.notes == "云端保存"
    assert cloud.values[("settings", "watchlist")][0]["notes"] == "云端保存"
    assert json.loads(local_path.read_text(encoding="utf-8"))[0]["notes"] == "云端保存"


def test_cloud_backed_position_store_prefers_cloud_state(tmp_path) -> None:
    cloud = MemoryStateStore()
    cloud.set_json(
        "settings",
        "positions",
        [{"code": "300308", "name": "中际旭创", "quantity": 100, "available_quantity": 80}],
    )
    store = CloudBackedPositionStore(tmp_path / "positions.json", cloud)

    item = store.list_items()[0]
    updated = store.upsert(item.model_copy(update={"available_quantity": 60}))

    assert item.code == "300308"
    assert updated.available_quantity == 60
    assert cloud.values[("settings", "positions")][0]["available_quantity"] == 60


def test_position_rejects_available_quantity_above_total() -> None:
    with pytest.raises(ValidationError, match="可卖数量不能大于持仓数量"):
        PositionRecord(
            code="300308",
            quantity=100,
            available_quantity=200,
        )


def test_trajectory_store_context_upsert_is_idempotent(tmp_path) -> None:
    path = tmp_path / "intraday.sqlite"
    store = IntradayWatchtowerStore(path)
    market = {"trend": "分歧转强", "updated_at": "09:35:05"}
    sectors = [{"name": "PCB", "heat_score": 80, "avg_change_pct": 2.2, "flow_delta": 3.0}]
    quote = {
        "code": "300476",
        "price": 64.0,
        "change_pct": 5.0,
        "amount": 1_000_000_000,
        "minute_amount_ratio": 2.0,
        "order_flow": {
            "available": True,
            "direction": "买盘增强",
            "score": 30,
            "data_quality": "l1_five_level",
        },
    }
    signal = {
        "code": "300476",
        "phase": "先手预警",
        "signal": "观察",
        "score": 62,
        "updated_at": "09:35:05",
        "invalidation_price": 62.8,
        "source_quality": "easy_tdx_l1_five_level",
        "reasons": ["板块点火"],
        "risks": ["等待确认"],
    }

    for score in (62, 66):
        store.record_context(
            trade_date="20260806",
            captured_at="09:35:05",
            updated_at="09:35:05",
            frozen=False,
            source_quality="live_l1_five_level_proxy",
            market=market,
            sectors=sectors,
            quotes=[quote],
            signals=[{**signal, "score": score}],
            priority_codes=["300476"],
        )

    status = store.status()
    assert status["market_snapshots"] == 1
    assert status["sector_snapshots"] == 1
    assert status["stock_snapshots"] == 1
    assert status["signal_transitions"] == 1
    with sqlite3.connect(path) as connection:
        saved_score = connection.execute(
            "SELECT score FROM signal_transitions WHERE code = ?",
            ("300476",),
        ).fetchone()[0]
        flow_count = connection.execute("SELECT COUNT(*) FROM order_flow_trajectory").fetchone()[0]
    assert saved_score == 66
    assert flow_count == 1


def test_trajectory_store_initialization_tolerates_existing_writer_lock(tmp_path) -> None:
    path = tmp_path / "intraday.sqlite"
    IntradayWatchtowerStore(path)

    class FastTimeoutStore(IntradayWatchtowerStore):
        def _connect(self) -> sqlite3.Connection:
            connection = sqlite3.connect(str(self.path), timeout=0.01)
            connection.row_factory = sqlite3.Row
            return connection

    locker = sqlite3.connect(path, timeout=0.01)
    try:
        locker.execute("PRAGMA journal_mode=WAL")
        locker.execute("BEGIN IMMEDIATE")

        store = FastTimeoutStore(path)

        assert store.status()["schema_version"] == IntradayWatchtowerStore.SCHEMA_VERSION
    finally:
        locker.rollback()
        locker.close()


def test_trajectory_store_can_limit_stock_features_to_priority_codes(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    store.record_context(
        trade_date="20260812",
        captured_at="10:00:00",
        updated_at="10:00:00",
        frozen=False,
        source_quality="test",
        market={"trend": "test", "updated_at": "10:00:00"},
        sectors=[{"name": "PCB", "heat_score": 80, "avg_change_pct": 2.2, "flow_delta": 3.0}],
        quotes=[
            {"code": "300476", "price": 64.0, "change_pct": 5.0, "amount": 1_000_000, "minute_amount_ratio": 2.0},
            {"code": "300308", "price": 100.0, "change_pct": 2.0, "amount": 2_000_000, "minute_amount_ratio": 1.5},
        ],
        signals=[],
        priority_codes=["300476"],
        stock_feature_codes=["300476"],
    )

    status = store.status()
    latest = store.latest_context_payload()

    assert status["market_snapshots"] == 1
    assert status["sector_snapshots"] == 1
    assert status["stock_snapshots"] == 1
    assert latest is not None
    assert {item["code"] for item in latest["quotes"]} == {"300476", "300308"}
    assert set(store.stock_feature_series_by_code("20260812", ["300476", "300308"])) == {"300476"}


def test_latest_context_payload_prefers_small_snapshot_file(tmp_path, monkeypatch) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    full_quote = {
        "code": "300476",
        "name": "胜宏科技",
        "themes": ["PCB"],
        "price": 64.0,
        "prev_close": 60.0,
        "open": 61.0,
        "high": 65.0,
        "low": 60.5,
        "day_high": 65.0,
        "day_low": 60.5,
        "change_pct": 5.0,
        "amount": 1_000_000,
        "minute_amount_ratio": 2.0,
        "updated_at": "10:00:00",
    }
    store.record_context(
        trade_date="20260812",
        captured_at="10:00:00",
        updated_at="10:00:00",
        frozen=False,
        source_quality="test",
        market={"trend": "test", "updated_at": "10:00:00"},
        sectors=[{"name": "PCB", "heat_score": 80, "avg_change_pct": 2.2, "flow_delta": 3.0}],
        quotes=[full_quote],
        signals=[],
        stock_feature_codes=[],
    )
    monkeypatch.setattr(
        store,
        "_latest_context_payload_from_sqlite",
        lambda trade_date=None: (_ for _ in ()).throw(AssertionError("small latest file should avoid SQLite fallback")),
    )

    latest = store.latest_context_payload()

    assert latest is not None
    assert latest["trade_date"] == "20260812"
    assert latest["quotes"][0]["code"] == "300476"


def test_trajectory_store_can_restore_latest_context_from_cloud_state(tmp_path) -> None:
    cloud = MemoryStateStore()
    cloud.set_json(
        "latest_context",
        "latest",
        {
            "trade_date": "20260812",
            "captured_at": "14:59:59",
            "updated_at": "14:59:59",
            "frozen": False,
            "source_quality": "cloud_snapshot",
            "market": {"trend": "分歧转强", "updated_at": "14:59:59"},
            "sectors": [{"name": "PCB", "heat_score": 80, "avg_change_pct": 2.2, "flow_delta": 3.0}],
            "quotes": [{"code": "300476", "name": "胜宏科技", "price": 64.0, "change_pct": 5.0}],
        },
    )
    store = IntradayWatchtowerStore(tmp_path / "empty.sqlite", state_store=cloud)

    latest = store.latest_context_payload()

    assert latest is not None
    assert latest["source_quality"] == "cloud_snapshot"
    assert latest["quotes"][0]["code"] == "300476"


def test_trajectory_store_writes_latest_context_to_cloud_state(tmp_path) -> None:
    cloud = MemoryStateStore()
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite", state_store=cloud)

    store.record_context(
        trade_date="20260812",
        captured_at="10:00:00",
        updated_at="10:00:00",
        frozen=False,
        source_quality="test",
        market={"trend": "test", "updated_at": "10:00:00"},
        sectors=[{"name": "PCB", "heat_score": 80, "avg_change_pct": 2.2, "flow_delta": 3.0}],
        quotes=[{"code": "300476", "name": "胜宏科技", "price": 64.0, "change_pct": 5.0}],
        signals=[],
        stock_feature_codes=[],
    )

    payload = cloud.values[("latest_context", "latest")]
    assert payload["trade_date"] == "20260812"
    assert payload["quotes"][0]["code"] == "300476"


def test_trajectory_store_batch_series_uses_latest_rows_in_time_order(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    for minute, suffix in [("09:30:00", 0), ("09:31:00", 1), ("09:32:00", 2)]:
        store.record_context(
            trade_date="20260807",
            captured_at=minute,
            updated_at=minute,
            frozen=False,
            source_quality="test",
            market={"trend": "test", "updated_at": minute},
            sectors=[
                {"name": "PCB", "heat_score": 70 + suffix, "avg_change_pct": 1.0 + suffix, "flow_delta": suffix},
                {"name": "CPO", "heat_score": 60 + suffix, "avg_change_pct": 0.5 + suffix, "flow_delta": suffix},
            ],
            quotes=[
                {
                    "code": "300476",
                    "price": 10 + suffix,
                    "change_pct": suffix,
                    "amount": 1_000_000 + suffix,
                    "minute_amount_ratio": 1.0 + suffix,
                },
                {
                    "code": "300308",
                    "price": 20 + suffix,
                    "change_pct": suffix,
                    "amount": 2_000_000 + suffix,
                    "minute_amount_ratio": 2.0 + suffix,
                },
            ],
            signals=[],
        )

    stock_rows = store.stock_feature_series_by_code(
        "20260807",
        ["300476", "300308", "999999"],
        max_rows=2,
    )
    sector_rows = store.sector_feature_series_by_name(
        "20260807",
        ["PCB", "CPO", "missing"],
        max_rows=2,
    )

    assert set(stock_rows) == {"300476", "300308"}
    assert [row["captured_at"] for row in stock_rows["300476"]] == ["09:31:00", "09:32:00"]
    assert [row["price"] for row in stock_rows["300308"]] == [21.0, 22.0]
    assert set(sector_rows) == {"PCB", "CPO"}
    assert [row["captured_at"] for row in sector_rows["PCB"]] == ["09:31:00", "09:32:00"]
    assert [row["heat_score"] for row in sector_rows["CPO"]] == [61.0, 62.0]

    latest = store.latest_context_payload()

    assert latest is not None
    assert latest["trade_date"] == "20260807"
    assert latest["captured_at"] == "09:32:00"
    assert latest["market"]["updated_at"] == "09:32:00"
    assert {item["code"] for item in latest["quotes"]} == {"300476", "300308"}
    assert {item["name"] for item in latest["sectors"]} == {"PCB", "CPO"}


def test_trajectory_store_mini_series_samples_full_session_not_tail_only(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    rows = []
    for index in range(12):
        rows.append((f"09:15:{index:02d}", 0.0))
    for index in range(30):
        rows.append((f"09:{30 + index:02d}:00", min(10.0, index * 0.55)))
    for index in range(60):
        rows.append((f"13:{index:02d}:00", 10.0))
    for index in range(12):
        rows.append((f"15:05:{index:02d}", 10.0))

    for captured_at, change_pct in rows:
        store.record_context(
            trade_date="20260811",
            captured_at=captured_at,
            updated_at=captured_at,
            frozen=False,
            source_quality="test",
            market={"trend": "test", "updated_at": captured_at},
            sectors=[],
            quotes=[
                {
                    "code": "600667",
                    "price": round(10 * (1 + change_pct / 100), 3),
                    "change_pct": change_pct,
                    "amount": 1_000_000,
                    "minute_amount_ratio": 1.5,
                }
            ],
            signals=[],
        )

    mini_rows = store.stock_feature_mini_series_by_code("20260811", ["600667"], max_rows=8)["600667"]

    assert len(mini_rows) <= 8
    assert mini_rows[0]["captured_at"] == "09:30:00"
    assert mini_rows[-1]["captured_at"] == "13:59:00"
    assert any(row["captured_at"] < "10:00:00" and row["change_pct"] >= 5 for row in mini_rows)


def test_trajectory_store_persists_protocol_projection_idempotently(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "research.sqlite")
    report = {
        "protocol_version": "research_protocol_v1",
        "run_id": "run-1",
        "generated_at": "2026-08-07T16:00:00",
        "sample": {"date_count": 2},
        "validation": {"status": "sample_insufficient", "oos_event_count": 0},
        "data_manifests": [
            {
                "trade_date": "20260807",
                "code": "300476",
                "source_quality": "l1_transaction",
                "minute_count": 240,
                "transaction_count": 300,
                "minute_coverage": 1,
                "transaction_coverage": 1,
            }
        ],
        "candidates": [
            {
                "trade_date": "20260807",
                "time": "09:35",
                "index": 4,
                "code": "300476",
                "direction": "positive_t",
                "setup": "卖压吸收",
                "hypothesis_id": "H2",
            }
        ],
        "outcomes": [
            {
                "trade_date": "20260807",
                "candidate_time": "09:35",
                "candidate_index": 4,
                "code": "300476",
                "direction": "positive_t",
                "setup": "卖压吸收",
                "hypothesis_id": "H2",
                "horizon": 5,
                "fill_status": "filled",
                "target_first": True,
                "net_r": 1.2,
            }
        ],
        "daily_regimes": [
            {"trade_date": "20260807", "code": "300476", "regime": "improving/improving"}
        ],
    }
    store.record_protocol_report(report)
    store.record_protocol_report(report)
    status = store.status()
    assert status["research_runs"] == 1
    assert status["strategy_events"] == 1
    assert status["trade_outcomes"] == 1
    assert status["data_manifests"] == 1
    assert status["daily_regimes"] == 1
    assert status["latest_research_run"]["validation_status"] == "sample_insufficient"
    with sqlite3.connect(store.path) as connection:
        strategy_key = connection.execute("SELECT event_key FROM strategy_events").fetchone()[0]
        outcome_key = connection.execute("SELECT event_key FROM trade_outcomes").fetchone()[0]
    assert strategy_key == outcome_key
    assert "run-1" in strategy_key


def test_protocol_projection_rolls_back_the_whole_run_on_failure(tmp_path, monkeypatch) -> None:
    store = IntradayWatchtowerStore(tmp_path / "research.sqlite")
    report = {
        "protocol_version": "research_protocol_v1",
        "run_id": "run-atomic",
        "generated_at": "2026-08-07T16:00:00",
        "validation": {"status": "sample_insufficient"},
        "sample": {"date_count": 1},
        "data_manifests": [{"trade_date": "20260807", "code": "300476"}],
    }

    def fail_manifest(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("projection failure")

    monkeypatch.setattr(store, "record_data_manifest", fail_manifest)

    with pytest.raises(RuntimeError, match="projection failure"):
        store.record_protocol_report(report)

    status = store.status()
    assert status["research_runs"] == 0
    assert status["data_manifests"] == 0


def test_trajectory_cleanup_keeps_recent_trade_days_and_research_tables(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    for trade_date in ("20260808", "20260809", "20260810"):
        store.record_context(
            trade_date=trade_date,
            captured_at="10:00:00",
            updated_at="10:00:00",
            frozen=False,
            source_quality="test",
            market={"trend": trade_date, "updated_at": "10:00:00"},
            sectors=[{"name": "PCB", "heat_score": 80, "avg_change_pct": 2.2, "flow_delta": 3.0}],
            quotes=[
                {
                    "code": "300476",
                    "price": 64.0,
                    "change_pct": 5.0,
                    "amount": 1_000_000,
                    "minute_amount_ratio": 2.0,
                    "order_flow": {
                        "available": True,
                        "direction": "买盘增强",
                        "score": 30,
                        "data_quality": "l1_five_level",
                    },
                }
            ],
            signals=[
                {
                    "code": "300476",
                    "phase": "先手预警",
                    "signal": "买T候选",
                    "score": 70,
                    "updated_at": "10:00:00",
                    "invalidation_price": 62.8,
                    "source_quality": "formula",
                    "reasons": ["测试"],
                    "risks": [],
                }
            ],
            priority_codes=["300476"],
        )
    store.record_protocol_report(
        {
            "protocol_version": "research_protocol_v1",
            "run_id": "kept-research",
            "generated_at": "2026-08-08T16:00:00",
            "sample": {"date_count": 1},
            "validation": {"status": "research_only"},
            "data_manifests": [{"trade_date": "20260808", "code": "300476"}],
            "candidates": [
                {
                    "trade_date": "20260808",
                    "time": "09:35",
                    "index": 1,
                    "code": "300476",
                    "direction": "positive_t",
                    "setup": "测试",
                }
            ],
            "outcomes": [
                {
                    "trade_date": "20260808",
                    "candidate_time": "09:35",
                    "candidate_index": 1,
                    "code": "300476",
                    "direction": "positive_t",
                    "setup": "测试",
                    "horizon": 5,
                }
            ],
            "daily_regimes": [{"trade_date": "20260808", "code": "300476", "regime": "test"}],
        }
    )

    result = store.cleanup_high_frequency_history(retain_trade_days=2)

    assert result["deleted_rows"] == 6
    assert result["keep_trade_dates"] == ["20260810", "20260809"]
    with sqlite3.connect(store.path) as connection:
        for table in (
            "market_trajectory",
            "sector_trajectory",
            "stock_features",
            "order_flow_trajectory",
            "signal_transitions",
            "data_quality_events",
        ):
            dates = {
                row[0]
                for row in connection.execute(f"SELECT DISTINCT trade_date FROM {table} ORDER BY trade_date")
            }
            assert dates == {"20260809", "20260810"}
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM strategy_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM data_manifests").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM daily_regimes").fetchone()[0] == 1



def test_payload_json_is_zlib_compressed_and_readable(tmp_path) -> None:
    """新写入的 payload_json 为 zlib BLOB；读取口同时兼容旧明文 JSON。"""
    path = tmp_path / "intraday.sqlite"
    store = IntradayWatchtowerStore(path)
    quote = {
        "code": "300476",
        "name": "胜宏科技",
        "price": 64.0,
        "change_pct": 5.0,
        "amount": 1_000_000_000,
        "minute_amount_ratio": 2.0,
        "prev_close": 61.0,
        "open": 62.0,
    }
    store.record_context(
        trade_date="20260806",
        captured_at="09:35:05",
        updated_at="09:35:05",
        frozen=False,
        source_quality="test",
        market={"trend": "震荡", "updated_at": "09:35:05"},
        sectors=[{"name": "PCB", "heat_score": 80}],
        quotes=[quote],
        signals=[],
    )
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT payload_json FROM stock_features WHERE code = ?",
            ("300476",),
        ).fetchone()[0]
    assert isinstance(raw, bytes)  # 压缩 BLOB，不再是明文 TEXT
    assert len(raw) < len(json.dumps(quote, ensure_ascii=False))  # 确实更小

    decoded = IntradayWatchtowerStore.decode_payload(raw)
    assert decoded["name"] == "胜宏科技"
    assert decoded["prev_close"] == 61.0
    # 旧格式明文 JSON 依然可读
    legacy = IntradayWatchtowerStore.decode_payload(json.dumps(quote, ensure_ascii=False))
    assert legacy["price"] == 64.0

    # 读取链路端到端：latest_context 从压缩 payload 重建 quotes
    ctx = store.latest_context_payload("20260806")
    assert ctx is not None and ctx["quotes"][0]["code"] == "300476"
    assert ctx["quotes"][0]["name"] == "胜宏科技"


def _quote_row(code: str, price: float, change_pct: float = 1.0) -> dict:
    return {
        "code": code,
        "price": price,
        "change_pct": change_pct,
        "amount": 1_000_000,
        "minute_amount_ratio": 1.5,
        "vol": 1234,
    }


def _record_batch(store: IntradayWatchtowerStore, captured_at: str, quotes: list[dict]) -> None:
    store.record_context(
        trade_date="20260811",
        captured_at=captured_at,
        updated_at=captured_at,
        frozen=False,
        source_quality="test",
        market={"trend": "test", "updated_at": captured_at},
        sectors=[],
        quotes=quotes,
        signals=[],
    )


def test_feature_mem_mirror_serves_reads_without_sql_after_backfill(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    _record_batch(store, "09:31:00", [_quote_row("600667", 10.0)])
    _record_batch(store, "09:32:00", [_quote_row("600667", 10.1)])

    # 首次读取：SQL 权威回填
    first = store.stock_feature_series("20260811", "600667", max_rows=180)
    assert [row["captured_at"] for row in first] == ["09:31:00", "09:32:00"]
    assert first[0]["vol"] == 1234  # payload 字段保留

    # 采集继续写入：镜像追加保鲜
    _record_batch(store, "09:33:00", [_quote_row("600667", 10.2)])

    # 直接清空 SQLite，证明后续读取完全由内存镜像服务
    with sqlite3.connect(str(store.path)) as conn:
        conn.execute("DELETE FROM stock_features")
    served = store.stock_feature_series("20260811", "600667", max_rows=180)
    assert [row["captured_at"] for row in served] == ["09:31:00", "09:32:00", "09:33:00"]
    assert served[-1]["price"] == 10.2


def test_feature_mem_mirror_rewrites_same_captured_at(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    _record_batch(store, "09:31:00", [_quote_row("600667", 10.0)])
    assert store.stock_feature_series("20260811", "600667", max_rows=180)[-1]["price"] == 10.0

    # 同一 captured_at 重写（ON CONFLICT 语义的镜像）：覆盖而不是追加
    _record_batch(store, "09:31:00", [_quote_row("600667", 10.5)])
    served = store.stock_feature_series("20260811", "600667", max_rows=180)
    assert len(served) == 1
    assert served[-1]["price"] == 10.5


def test_feature_mem_falls_back_to_sql_when_limit_exceeds_coverage(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    for minute in range(31, 36):  # 5 行
        _record_batch(store, f"09:{minute:02d}:00", [_quote_row("600667", 10.0 + minute * 0.01)])

    shallow = store.stock_feature_series("20260811", "600667", max_rows=2)
    assert len(shallow) == 2
    # 覆盖深度只有 2，请求 5 行必须回退 SQL 拿到完整序列
    deep = store.stock_feature_series("20260811", "600667", max_rows=5)
    assert len(deep) == 5
    assert deep[0]["captured_at"] == "09:31:00"


def test_feature_mem_empty_backfill_then_mirror_append(tmp_path) -> None:
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    # 停牌票：首次读取空结果也回填，之后恢复交易时镜像能正确追加
    assert store.stock_feature_series("20260811", "600667", max_rows=180) == []
    assert "600667" not in store.stock_feature_series_by_code("20260811", ["600667"], max_rows=180)

    _record_batch(store, "13:00:00", [_quote_row("600667", 10.0)])
    served = store.stock_feature_series("20260811", "600667", max_rows=180)
    assert [row["captured_at"] for row in served] == ["13:00:00"]


def test_stock_features_columnar_read_skips_payload_decode(tmp_path, monkeypatch) -> None:
    """列化快路径：vol/minute_amount 是独立列时，读取完全不解码 payload。"""
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    _record_batch(store, "09:31:00", [_quote_row("600667", 10.0)])
    _record_batch(store, "09:32:00", [_quote_row("600667", 10.1)])

    def forbidden_decode(value):  # noqa: ANN001
        raise AssertionError("列化数据不应触发 payload 解码")

    monkeypatch.setattr(store, "_loads", forbidden_decode)
    rows = store.stock_feature_series("20260811", "600667", max_rows=180)
    assert [row["captured_at"] for row in rows] == ["09:31:00", "09:32:00"]
    assert rows[0]["vol"] == 1234
    assert rows[0]["minute_amount"] == 0  # _quote_row 未提供 minute_amount，列为 0


def test_stock_features_legacy_rows_fall_back_to_payload_decode(tmp_path) -> None:
    """老数据（vol 列为 NULL）仍走 payload 解码，字段与列覆盖语义不变。"""
    store = IntradayWatchtowerStore(tmp_path / "intraday.sqlite")
    # 直接手写一条老格式行：vol/minute_amount 为 NULL，payload 完整
    with sqlite3.connect(str(store.path)) as conn:
        conn.execute(
            """
            INSERT INTO stock_features
                (trade_date, captured_at, code, price, change_pct, amount, minute_amount, vol, minute_amount_ratio, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            ("20260811", "09:31:00", "600667", 10.0, 1.0, 1_000_000, 1.5, store._json({"vol": 77, "minute_amount": 555, "extra": "keep"})),
        )
    rows = store.stock_feature_series("20260811", "600667", max_rows=180)
    assert rows[0]["vol"] == 77
    assert rows[0]["minute_amount"] == 555
    assert rows[0]["extra"] == "keep"
    assert rows[0]["price"] == 10.0
