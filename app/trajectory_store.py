from __future__ import annotations

"""Local, append-only intraday evidence storage.

The live dashboard is allowed to lose a browser connection.  This repository
keeps the small feature payload needed for later replay independently of the
UI and does not persist credentials or raw upstream packets.
"""

import json
import sqlite3
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol


class JsonStateStore(Protocol):
    def get_json(self, namespace: str, key: str, default: Any = None) -> Any:
        ...

    def set_json(self, namespace: str, key: str, value: Any) -> None:
        ...


class IntradayWatchtowerStore:
    SCHEMA_VERSION = 4
    HIGH_FREQUENCY_TABLES = (
        "market_trajectory",
        "sector_trajectory",
        "stock_features",
        "order_flow_trajectory",
        "signal_transitions",
        "data_quality_events",
    )

    def __init__(
        self,
        path: Path,
        enabled: bool = True,
        initialize: bool = True,
        state_store: JsonStateStore | None = None,
    ) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.state_store = state_store
        self._lock = threading.RLock()
        # stock_features 内存镜像：采集写入时同步维护（写穿透），全市场刷新/详情页
        # 的公式行读取不再走 SQLite+zlib+JSON 回环。条目只能由 SQL 权威回填创建
        # （保证从当日开盘起完整），采集追加负责保鲜；SQLite 仍是持久层，用于
        # 进程重启恢复、回放与研究。
        self._feature_mem_lock = threading.Lock()
        self._feature_mem: dict[str, dict[str, dict[str, Any]]] = {}
        self._feature_mem_max_rows = 288
        self._feature_mem_max_dates = 2
        if self.enabled and initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _read_connect(self, timeout: float = 0.2) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=timeout)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_trajectory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    frozen INTEGER NOT NULL DEFAULT 0,
                    source_quality TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(trade_date, captured_at)
                );
                CREATE TABLE IF NOT EXISTS sector_trajectory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    heat_score REAL NOT NULL DEFAULT 0,
                    avg_change_pct REAL NOT NULL DEFAULT 0,
                    flow_delta REAL NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    UNIQUE(trade_date, captured_at, sector_name)
                );
                CREATE TABLE IF NOT EXISTS stock_features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    code TEXT NOT NULL,
                    price REAL NOT NULL DEFAULT 0,
                    change_pct REAL NOT NULL DEFAULT 0,
                    amount REAL NOT NULL DEFAULT 0,
                    minute_amount REAL,
                    vol REAL,
                    minute_amount_ratio REAL NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL,
                    UNIQUE(trade_date, captured_at, code)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_features_code_date
                    ON stock_features(code, trade_date, captured_at);
                CREATE INDEX IF NOT EXISTS idx_sector_trajectory_name_date
                    ON sector_trajectory(sector_name, trade_date, captured_at);
                CREATE TABLE IF NOT EXISTS order_flow_trajectory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    code TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT '',
                    score REAL NOT NULL DEFAULT 0,
                    available INTEGER NOT NULL DEFAULT 0,
                    source_quality TEXT NOT NULL DEFAULT 'unavailable',
                    payload_json TEXT NOT NULL,
                    UNIQUE(trade_date, captured_at, code)
                );
                CREATE TABLE IF NOT EXISTS signal_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    code TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    invalidation_price REAL NOT NULL DEFAULT 0,
                    source_quality TEXT NOT NULL DEFAULT '',
                    reason_json TEXT NOT NULL,
                    risk_json TEXT NOT NULL,
                    UNIQUE(trade_date, time, code, phase)
                );
                CREATE TABLE IF NOT EXISTS data_quality_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(trade_date, captured_at, source)
                );
                CREATE TABLE IF NOT EXISTS strategy_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    code TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'none',
                    action TEXT NOT NULL DEFAULT 'observe',
                    phase TEXT NOT NULL DEFAULT '',
                    setup TEXT NOT NULL DEFAULT '',
                    regime TEXT NOT NULL DEFAULT '',
                    executable INTEGER NOT NULL DEFAULT 0,
                    validation_status TEXT NOT NULL DEFAULT 'research_only',
                    hypothesis_id TEXT NOT NULL DEFAULT '',
                    source_quality TEXT NOT NULL DEFAULT '',
                    invalidation_price REAL NOT NULL DEFAULT 0,
                    event_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_events_code_time
                    ON strategy_events(code, trade_date, time);
                CREATE TABLE IF NOT EXISTS daily_regimes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL DEFAULT '',
                    regime TEXT NOT NULL DEFAULT '',
                    source_quality TEXT NOT NULL DEFAULT '',
                    strategy_version TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    UNIQUE(trade_date, code)
                );
                CREATE TABLE IF NOT EXISTS research_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    protocol_version TEXT NOT NULL DEFAULT '',
                    validation_status TEXT NOT NULL DEFAULT 'research_only',
                    sample_days INTEGER NOT NULL DEFAULT 0,
                    sample_events INTEGER NOT NULL DEFAULT 0,
                    oos_events INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL,
                    horizon INTEGER NOT NULL DEFAULT 0,
                    direction TEXT NOT NULL DEFAULT 'none',
                    code TEXT NOT NULL DEFAULT '',
                    trade_date TEXT NOT NULL DEFAULT '',
                    target_first INTEGER,
                    net_return_pct REAL NOT NULL DEFAULT 0,
                    net_r REAL NOT NULL DEFAULT 0,
                    mae_pct REAL NOT NULL DEFAULT 0,
                    mfe_pct REAL NOT NULL DEFAULT 0,
                    fill_status TEXT NOT NULL DEFAULT 'no_fill',
                    payload_json TEXT NOT NULL,
                    UNIQUE(event_key, horizon)
                );
                CREATE TABLE IF NOT EXISTS data_manifests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL DEFAULT '',
                    source_quality TEXT NOT NULL DEFAULT 'unavailable',
                    minute_count INTEGER NOT NULL DEFAULT 0,
                    transaction_count INTEGER NOT NULL DEFAULT 0,
                    minute_coverage REAL NOT NULL DEFAULT 0,
                    transaction_coverage REAL NOT NULL DEFAULT 0,
                    transaction_minute_count INTEGER NOT NULL DEFAULT 0,
                    transaction_page_count INTEGER NOT NULL DEFAULT 0,
                    transaction_sequence_count INTEGER NOT NULL DEFAULT 0,
                    transaction_raw_time_count INTEGER NOT NULL DEFAULT 0,
                    transaction_gap_count INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    UNIQUE(trade_date, code)
                );
                """
            )
            manifest_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(data_manifests)").fetchall()
            }
            for column, declaration in (
                ("transaction_minute_count", "INTEGER NOT NULL DEFAULT 0"),
                ("transaction_page_count", "INTEGER NOT NULL DEFAULT 0"),
                ("transaction_sequence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("transaction_raw_time_count", "INTEGER NOT NULL DEFAULT 0"),
                ("transaction_gap_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in manifest_columns:
                    connection.execute(
                        f"ALTER TABLE data_manifests ADD COLUMN {column} {declaration}"
                    )
            # stock_features 列化迁移：vol/minute_amount 提为独立列后，
            # 热路径读取直接拿原生数值，不再 zlib+JSON 解码 payload。
            # 老数据的这两列为 NULL，读取端回退 payload 解码一次后入内存镜像。
            feature_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(stock_features)").fetchall()
            }
            for column in ("vol", "minute_amount"):
                if column not in feature_columns:
                    connection.execute(f"ALTER TABLE stock_features ADD COLUMN {column} REAL")
            schema_version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                ("schema_version",),
            ).fetchone()
            if schema_version is None or str(schema_version["value"]) != str(self.SCHEMA_VERSION):
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("schema_version", str(self.SCHEMA_VERSION)),
                )

    @staticmethod
    def _json(value: Any) -> sqlite3.Binary:
        """Serialize payload as zlib-compressed JSON BLOB.

        17.4M rows/day x ~915B JSON was ~16GB/day of redundant payload;
        level-1 zlib cuts it ~60% at negligible CPU.  Plain-JSON rows written
        before this change remain readable via ``decode_payload``.
        """
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        return sqlite3.Binary(zlib.compress(text.encode("utf-8"), 1))

    @staticmethod
    def _dump(model: Any) -> dict[str, Any]:
        if hasattr(model, "model_dump"):
            return model.model_dump(mode="json")
        return dict(model)

    def _latest_context_file(self) -> Path:
        return self.path.with_name(f"{self.path.stem}_latest_context.json")

    def _write_latest_context_file(
        self,
        *,
        trade_date: str,
        captured_at: str,
        updated_at: str,
        frozen: bool,
        source_quality: str,
        market: dict[str, Any],
        sectors: list[dict[str, Any]],
        quotes: list[dict[str, Any]],
    ) -> None:
        payload = {
            "trade_date": str(trade_date),
            "captured_at": str(captured_at),
            "updated_at": str(updated_at),
            "frozen": bool(frozen),
            "source_quality": str(source_quality),
            "market": market,
            "sectors": sectors,
            "quotes": [
                {key: value for key, value in quote.items() if key != "order_flow"}
                for quote in quotes
                if str(quote.get("code") or "").strip()
            ],
        }
        path = self._latest_context_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), default=str)
            tmp_path.replace(path)
        except Exception:
            pass
        self._write_latest_context_cloud(payload)

    def _write_latest_context_cloud(self, payload: dict[str, Any]) -> None:
        if self.state_store is None:
            return
        try:
            self.state_store.set_json("latest_context", "latest", payload)
            trade_date = str(payload.get("trade_date") or "").strip()
            if trade_date:
                self.state_store.set_json("latest_context", trade_date, payload)
        except Exception:
            return

    def _read_latest_context_file(self, trade_date: str | None = None) -> dict[str, Any] | None:
        path = self._latest_context_file()
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        normalized_date = str(trade_date or "").strip()
        if normalized_date and str(payload.get("trade_date") or "") != normalized_date:
            return None
        if not payload.get("market") or not payload.get("sectors") or not payload.get("quotes"):
            return None
        return payload

    def _read_latest_context_cloud(self, trade_date: str | None = None) -> dict[str, Any] | None:
        if self.state_store is None:
            return None
        normalized_date = str(trade_date or "").strip()
        keys = [normalized_date] if normalized_date else ["latest"]
        for key in keys:
            try:
                payload = self.state_store.get_json("latest_context", key)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if normalized_date and str(payload.get("trade_date") or "") != normalized_date:
                continue
            if payload.get("market") and payload.get("sectors") and payload.get("quotes"):
                return payload
        return None

    # -- stock_features 内存镜像（写穿透） -------------------------------------

    def _feature_mem_day(self, trade_date: str) -> dict[str, dict[str, Any]]:
        day = self._feature_mem.get(trade_date)
        if day is None:
            # 只保留最近几个交易日，防止跨天运行内存膨胀
            while len(self._feature_mem) >= self._feature_mem_max_dates:
                oldest = next(iter(self._feature_mem))
                self._feature_mem.pop(oldest, None)
            day = self._feature_mem[trade_date] = {}
        return day

    def _mirror_stock_feature_row(
        self,
        trade_date: str,
        captured_at: str,
        code: str,
        row: dict[str, Any],
    ) -> None:
        """采集写入时同步内存镜像（与列化读取端的瘦行结构完全一致）。

        只追加到已由 SQL 回填创建的条目：进程盘中启动时尚未回填的票不建半成品
        条目（只有开盘后部分行），首次读取走 SQL 拿到当日完整序列后再保鲜。
        """
        with self._feature_mem_lock:
            entry = self._feature_mem.get(trade_date, {}).get(code)
            if entry is None:
                return
            rows: deque[dict[str, Any]] = entry["rows"]
            if rows and str(rows[-1].get("captured_at") or "") == str(captured_at or ""):
                # ON CONFLICT 同刻重写的镜像：覆盖最后一行而不是追加
                rows[-1] = row
            else:
                rows.append(row)

    def _feature_mem_get(self, trade_date: str, code: str, limit: int) -> list[dict[str, Any]] | None:
        """内存镜像命中且覆盖深度足够时返回与 SQL 路径完全一致的结果。"""
        with self._feature_mem_lock:
            entry = self._feature_mem.get(trade_date, {}).get(code)
            if entry is None or int(entry["covered_limit"]) < limit:
                return None
            return list(entry["rows"])[-limit:]

    def _feature_mem_backfill(
        self,
        trade_date: str,
        code: str,
        series: list[dict[str, Any]],
        limit: int,
    ) -> None:
        """SQL 读取后回填内存镜像；空序列也回填（停牌票当日无行是合法结果）。"""
        with self._feature_mem_lock:
            day = self._feature_mem_day(trade_date)
            day[code] = {
                "rows": deque(series, maxlen=self._feature_mem_max_rows),
                "covered_limit": min(max(1, int(limit)), self._feature_mem_max_rows),
            }

    def record_context(
        self,
        *,
        trade_date: str,
        captured_at: str,
        updated_at: str,
        frozen: bool,
        source_quality: str,
        market: Any,
        sectors: Iterable[Any],
        quotes: Iterable[Any],
        signals: Iterable[Any],
        priority_codes: Iterable[str] = (),
        stock_feature_codes: Iterable[str] | None = None,
    ) -> None:
        if not self.enabled:
            return
        market_payload = self._dump(market)
        sector_rows = [self._dump(item) for item in sectors]
        quote_rows = [self._dump(item) for item in quotes]
        signal_rows = {str(item.get("code")): item for item in (self._dump(signal) for signal in signals)}
        high_frequency_codes = {str(code).zfill(6) for code in priority_codes if str(code).strip()}
        high_frequency_codes.update(
            code
            for code, signal in signal_rows.items()
            if str(signal.get("phase") or "观察") != "观察"
        )
        stock_codes = (
            {str(code).zfill(6) for code in stock_feature_codes if str(code).strip()}
            if stock_feature_codes is not None
            else None
        )
        self._write_latest_context_file(
            trade_date=trade_date,
            captured_at=captured_at,
            updated_at=updated_at,
            frozen=frozen,
            source_quality=source_quality,
            market=market_payload,
            sectors=sector_rows,
            quotes=quote_rows,
        )
        mirrored_features: list[tuple[str, str, str, dict[str, Any]]] = []
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_trajectory
                    (trade_date, captured_at, updated_at, frozen, source_quality, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, captured_at) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    frozen=excluded.frozen,
                    source_quality=excluded.source_quality,
                    payload_json=excluded.payload_json
                """,
                (trade_date, captured_at, updated_at, int(frozen), source_quality, self._json(market_payload)),
            )
            for sector in sector_rows:
                connection.execute(
                    """
                    INSERT INTO sector_trajectory
                        (trade_date, captured_at, sector_name, heat_score, avg_change_pct, flow_delta, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_date, captured_at, sector_name) DO UPDATE SET
                        heat_score=excluded.heat_score,
                        avg_change_pct=excluded.avg_change_pct,
                        flow_delta=excluded.flow_delta,
                        payload_json=excluded.payload_json
                    """,
                    (
                        trade_date,
                        captured_at,
                        str(sector.get("name") or ""),
                        float(sector.get("heat_score") or 0),
                        float(sector.get("avg_change_pct") or 0),
                        float(sector.get("flow_delta") or 0),
                        self._json(sector),
                    ),
                )
            for quote in quote_rows:
                code = str(quote.get("code") or "")
                if not code:
                    continue
                if stock_codes is None or code in stock_codes:
                    payload = {key: value for key, value in quote.items() if key != "order_flow"}
                    volume_value = float(quote.get("vol") or quote.get("volume") or 0)
                    minute_amount_value = float(quote.get("minute_amount") or 0)
                    connection.execute(
                        """
                        INSERT INTO stock_features
                            (trade_date, captured_at, code, price, change_pct, amount, minute_amount, vol, minute_amount_ratio, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(trade_date, captured_at, code) DO UPDATE SET
                            price=excluded.price,
                            change_pct=excluded.change_pct,
                            amount=excluded.amount,
                            minute_amount=excluded.minute_amount,
                            vol=excluded.vol,
                            minute_amount_ratio=excluded.minute_amount_ratio,
                            payload_json=excluded.payload_json
                        """,
                        (
                            trade_date,
                            captured_at,
                            code,
                            float(quote.get("price") or 0),
                            float(quote.get("change_pct") or 0),
                            float(quote.get("amount") or 0),
                            minute_amount_value,
                            volume_value,
                            float(quote.get("minute_amount_ratio") or 1),
                            self._json(payload),
                        ),
                    )
                    # 写穿透内存镜像：读路径（刷新/详情）与采集共享同一份数据；
                    # 先暂存，事务提交成功后才应用，避免回滚导致内存与库分叉
                    mirrored_features.append(
                        (
                            trade_date,
                            captured_at,
                            code,
                            {
                                "captured_at": str(captured_at or ""),
                                "price": float(quote.get("price") or 0),
                                "change_pct": float(quote.get("change_pct") or 0),
                                "amount": float(quote.get("amount") or 0),
                                "minute_amount": minute_amount_value,
                                "minute_amount_ratio": float(quote.get("minute_amount_ratio") or 1),
                                "vol": volume_value,
                                "volume": volume_value,
                            },
                        )
                    )
                if code in high_frequency_codes:
                    flow = quote.get("order_flow") if isinstance(quote.get("order_flow"), dict) else {}
                    connection.execute(
                        """
                        INSERT INTO order_flow_trajectory
                            (trade_date, captured_at, code, direction, score, available, source_quality, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(trade_date, captured_at, code) DO UPDATE SET
                            direction=excluded.direction,
                            score=excluded.score,
                            available=excluded.available,
                            source_quality=excluded.source_quality,
                            payload_json=excluded.payload_json
                        """,
                        (
                            trade_date,
                            captured_at,
                            code,
                            str(flow.get("direction") or ""),
                            float(flow.get("score") or 0),
                            int(bool(flow.get("available"))),
                            str(flow.get("data_quality") or "unavailable"),
                            self._json(flow),
                        ),
                    )
                signal = signal_rows.get(code)
                if signal and str(signal.get("phase") or "观察") != "观察":
                    self._upsert_signal_transition(
                        connection,
                        trade_date=trade_date,
                        time=str(signal.get("updated_at") or captured_at),
                        code=code,
                        phase=str(signal.get("phase") or "观察"),
                        signal=str(signal.get("signal") or "观察"),
                        score=float(signal.get("score") or 0),
                        invalidation_price=float(signal.get("invalidation_price") or 0),
                        source_quality=str(signal.get("source_quality") or source_quality),
                        reasons=list(signal.get("reasons") or []),
                        risks=list(signal.get("risks") or []),
                    )
            connection.execute(
                "INSERT INTO data_quality_events(trade_date, captured_at, source, quality, note) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(trade_date, captured_at, source) DO UPDATE SET quality=excluded.quality, note=excluded.note",
                (trade_date, captured_at, "dashboard", source_quality, "仅保存公开特征，不保存密钥或原始 source_json"),
            )
        # 事务提交成功后应用内存镜像（读路径与采集共享同一份数据，零 SQL 零解压）
        for m_date, m_captured, m_code, m_row in mirrored_features:
            self._mirror_stock_feature_row(m_date, m_captured, m_code, m_row)

    def record_signal_transition(
        self,
        *,
        trade_date: str,
        time: str,
        code: str,
        phase: str,
        signal: str,
        score: float,
        invalidation_price: float,
        source_quality: str,
        reasons: list[str],
        risks: list[str],
    ) -> None:
        if not self.enabled:
            return
        with self._lock, self._connect() as connection:
            self._upsert_signal_transition(
                connection,
                trade_date=trade_date,
                time=time,
                code=code,
                phase=phase,
                signal=signal,
                score=score,
                invalidation_price=invalidation_price,
                source_quality=source_quality,
                reasons=reasons,
                risks=risks,
            )

    def record_strategy_event(
        self,
        *,
        trade_date: str,
        time: str,
        code: str,
        marker: Any,
        event_key: str | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        """Upsert one actionable research marker for later replay auditing."""

        if not self.enabled:
            return
        payload = self._dump(marker)
        direction = str(payload.get("direction") or "none")
        action = str(payload.get("action") or "observe")
        phase = str(payload.get("phase") or "")
        stable_key = event_key or "|".join(
            [str(trade_date), str(time), str(code).zfill(6), direction, action, phase]
        )
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO strategy_events
                    (trade_date, time, code, direction, action, phase, setup, regime,
                     executable, validation_status, hypothesis_id, source_quality,
                     invalidation_price, event_key, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    setup=excluded.setup,
                    regime=excluded.regime,
                    executable=excluded.executable,
                    validation_status=excluded.validation_status,
                    hypothesis_id=excluded.hypothesis_id,
                    source_quality=excluded.source_quality,
                    invalidation_price=excluded.invalidation_price,
                    payload_json=excluded.payload_json
                """,
                (
                    str(trade_date),
                    str(time),
                    str(code).zfill(6),
                    direction,
                    action,
                    phase,
                    str(payload.get("setup") or ""),
                    str(payload.get("regime") or ""),
                    int(bool(payload.get("executable"))),
                    str(payload.get("validation_status") or "research_only"),
                    str(payload.get("hypothesis_id") or ""),
                    str(payload.get("source_quality") or ""),
                    float(payload.get("invalidation_price") or 0),
                    stable_key,
                    self._json(payload),
                ),
            )

        if _connection is not None:
            write(_connection)
        else:
            with self._lock, self._connect() as connection:
                write(connection)

    def record_daily_regime(
        self,
        *,
        trade_date: str,
        code: str = "",
        regime: str,
        source_quality: str = "",
        strategy_version: str = "",
        payload: Any = None,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        if not self.enabled:
            return
        dumped = self._dump(payload) if payload is not None else {"regime": regime}
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO daily_regimes
                    (trade_date, code, regime, source_quality, strategy_version, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code) DO UPDATE SET
                    regime=excluded.regime,
                    source_quality=excluded.source_quality,
                    strategy_version=excluded.strategy_version,
                    payload_json=excluded.payload_json
                """,
                (
                    str(trade_date),
                    str(code).zfill(6) if code else "",
                    str(regime),
                    str(source_quality),
                    str(strategy_version),
                    self._json(dumped),
                ),
            )

        if _connection is not None:
            write(_connection)
        else:
            with self._lock, self._connect() as connection:
                write(connection)

    def record_research_run(
        self,
        report: Mapping[str, Any],
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        if not self.enabled:
            return
        protocol = report.get("research_protocol") if isinstance(report, Mapping) else None
        payload_report = protocol if isinstance(protocol, Mapping) else report
        validation = payload_report.get("validation") if isinstance(payload_report, Mapping) else {}
        sample = payload_report.get("sample") if isinstance(payload_report, Mapping) else {}
        validation = validation if isinstance(validation, Mapping) else {}
        sample = sample if isinstance(sample, Mapping) else {}
        run_id = str(report.get("run_id") or report.get("generated_at") or "")
        if isinstance(payload_report, Mapping):
            run_id = str(payload_report.get("run_id") or payload_report.get("generated_at") or run_id)
        if not run_id:
            run_id = datetime.now().isoformat(timespec="seconds")
        candidates = payload_report.get("candidates", []) if isinstance(payload_report, Mapping) else []
        labels = payload_report.get("labels", []) if isinstance(payload_report, Mapping) else []
        outcomes = payload_report.get("outcomes", []) if isinstance(payload_report, Mapping) else []
        event_count = len(outcomes or labels or candidates) if isinstance(outcomes or labels or candidates, list) else 0
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO research_runs
                    (run_id, started_at, finished_at, protocol_version, validation_status,
                     sample_days, sample_events, oos_events, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    finished_at=excluded.finished_at,
                    protocol_version=excluded.protocol_version,
                    validation_status=excluded.validation_status,
                    sample_days=excluded.sample_days,
                    sample_events=excluded.sample_events,
                    oos_events=excluded.oos_events,
                    payload_json=excluded.payload_json
                """,
                (
                    run_id,
                    str(report.get("started_at") or report.get("generated_at") or ""),
                    str(report.get("finished_at") or report.get("generated_at") or ""),
                    str(report.get("protocol_version") or ""),
                    str(validation.get("status") or validation.get("validation_status") or "research_only"),
                    int((sample or {}).get("date_count") or 0),
                    int(event_count),
                    int(validation.get("oos_event_count") or 0),
                    self._json(payload_report),
                ),
            )

        if _connection is not None:
            write(_connection)
        else:
            with self._lock, self._connect() as connection:
                write(connection)

    def record_protocol_report(self, report: Mapping[str, Any]) -> None:
        """Persist one complete research protocol result idempotently.

        The JSON report remains the human-readable artifact.  This method keeps
        the queryable SQLite projection in sync so the API and later tooling do
        not need to read a report file or an upstream data source.
        """

        if not self.enabled:
            return
        protocol = report.get("research_protocol") if isinstance(report, Mapping) else None
        payload = protocol if isinstance(protocol, Mapping) else report
        if not isinstance(payload, Mapping):
            return
        payload = dict(payload)
        # A complete report can contain tens of thousands of labels and
        # counterfactual outcomes.  Keep one connection/transaction for the
        # complete projection.  If any row fails, the research run itself is
        # rolled back as well instead of leaving a half-written projection.
        run_id = str(payload.get("run_id") or payload.get("generated_at") or "")
        if not run_id:
            run_id = datetime.now().isoformat(timespec="seconds")
        payload["run_id"] = run_id
        with self._lock, self._connect() as connection:
            self.record_research_run(payload, _connection=connection)
            for manifest in payload.get("data_manifests", []) or []:
                if isinstance(manifest, Mapping):
                    self.record_data_manifest(manifest, _connection=connection)
            candidate_keys: dict[tuple[str, ...], list[str]] = {}
            candidate_occurrences: dict[tuple[str, ...], int] = {}
            for index, candidate in enumerate(payload.get("candidates", []) or []):
                if not isinstance(candidate, Mapping):
                    continue
                trade_date = str(candidate.get("trade_date") or "")
                code = str(candidate.get("code") or "").zfill(6)
                time_label = str(candidate.get("time") or "")
                direction = str(candidate.get("direction") or "none")
                setup = str(candidate.get("setup") or "")
                identity = (
                    trade_date,
                    code,
                    str(candidate.get("index") if candidate.get("index") is not None else time_label),
                    direction,
                    setup,
                    str(candidate.get("hypothesis_id") or ""),
                )
                occurrence = candidate_occurrences.get(identity, 0)
                candidate_occurrences[identity] = occurrence + 1
                event_key = "|".join(
                    ["research", run_id, "candidate", *identity, str(occurrence)]
                )
                candidate_keys.setdefault(identity, []).append(event_key)
                marker = {
                    **dict(candidate),
                    "action": "buy_t" if direction == "positive_t" else "sell_base" if direction == "reverse_t" else "observe",
                    "phase": "research_candidate",
                    "executable": False,
                    "validation_status": str(candidate.get("validation_status") or "research_only"),
                }
                self._record_strategy_event_on_connection(
                    connection,
                    trade_date=trade_date,
                    time=time_label,
                    code=code,
                    marker=marker,
                    event_key=event_key,
                )
            outcome_occurrences: dict[tuple[tuple[str, ...], int], int] = {}
            for index, outcome in enumerate(payload.get("outcomes", []) or []):
                if not isinstance(outcome, Mapping):
                    continue
                outcome_identity = (
                    str(outcome.get("trade_date") or ""),
                    str(outcome.get("code") or "").zfill(6),
                    str(
                        outcome.get("candidate_index")
                        if outcome.get("candidate_index") is not None
                        else outcome.get("candidate_time") or ""
                    ),
                    str(outcome.get("direction") or "none"),
                    str(outcome.get("setup") or ""),
                    str(outcome.get("hypothesis_id") or ""),
                )
                horizon = int(outcome.get("horizon") or 0)
                occurrence_key = (outcome_identity, horizon)
                occurrence = outcome_occurrences.get(occurrence_key, 0)
                outcome_occurrences[occurrence_key] = occurrence + 1
                known_candidate_keys = candidate_keys.get(outcome_identity, [])
                event_key = (
                    known_candidate_keys[occurrence]
                    if occurrence < len(known_candidate_keys)
                    else "|".join(
                        ["research", run_id, "outcome", *outcome_identity, str(occurrence)]
                    )
                )
                self.record_trade_outcome(event_key=event_key, outcome=outcome, _connection=connection)
            for regime in payload.get("daily_regimes", []) or []:
                if not isinstance(regime, Mapping):
                    continue
                self._record_daily_regime_on_connection(
                    connection,
                    trade_date=str(regime.get("trade_date") or ""),
                    code=str(regime.get("code") or ""),
                    regime=str(regime.get("regime") or ""),
                    source_quality=str(regime.get("source_quality") or ""),
                    strategy_version=str(regime.get("strategy_version") or payload.get("protocol_version") or ""),
                    payload=regime,
                )

    def _record_strategy_event_on_connection(
        self,
        connection: sqlite3.Connection,
        *,
        trade_date: str,
        time: str,
        code: str,
        marker: Any,
        event_key: str | None = None,
    ) -> None:
        payload = self._dump(marker)
        direction = str(payload.get("direction") or "none")
        action = str(payload.get("action") or "observe")
        phase = str(payload.get("phase") or "")
        stable_key = event_key or "|".join(
            [str(trade_date), str(time), str(code).zfill(6), direction, action, phase]
        )
        connection.execute(
            """
            INSERT INTO strategy_events
                (trade_date, time, code, direction, action, phase, setup, regime,
                 executable, validation_status, hypothesis_id, source_quality,
                 invalidation_price, event_key, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET
                setup=excluded.setup,
                regime=excluded.regime,
                executable=excluded.executable,
                validation_status=excluded.validation_status,
                hypothesis_id=excluded.hypothesis_id,
                source_quality=excluded.source_quality,
                invalidation_price=excluded.invalidation_price,
                payload_json=excluded.payload_json
            """,
            (
                str(trade_date), str(time), str(code).zfill(6), direction, action,
                phase, str(payload.get("setup") or ""), str(payload.get("regime") or ""),
                int(bool(payload.get("executable"))),
                str(payload.get("validation_status") or "research_only"),
                str(payload.get("hypothesis_id") or ""), str(payload.get("source_quality") or ""),
                float(payload.get("invalidation_price") or 0), stable_key, self._json(payload),
            ),
        )

    def _record_daily_regime_on_connection(
        self,
        connection: sqlite3.Connection,
        *,
        trade_date: str,
        code: str = "",
        regime: str,
        source_quality: str = "",
        strategy_version: str = "",
        payload: Any = None,
    ) -> None:
        dumped = self._dump(payload) if payload is not None else {"regime": regime}
        connection.execute(
            """
            INSERT INTO daily_regimes
                (trade_date, code, regime, source_quality, strategy_version, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, code) DO UPDATE SET
                regime=excluded.regime,
                source_quality=excluded.source_quality,
                strategy_version=excluded.strategy_version,
                payload_json=excluded.payload_json
            """,
            (str(trade_date), str(code).zfill(6) if code else "", str(regime),
             str(source_quality), str(strategy_version), self._json(dumped)),
        )

    def record_trade_outcome(
        self,
        *,
        event_key: str,
        outcome: Any,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        if not self.enabled:
            return
        payload = self._dump(outcome)
        target_first = payload.get("target_first")
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO trade_outcomes
                    (event_key, horizon, direction, code, trade_date, target_first,
                     net_return_pct, net_r, mae_pct, mfe_pct, fill_status, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key, horizon) DO UPDATE SET
                    target_first=excluded.target_first,
                    net_return_pct=excluded.net_return_pct,
                    net_r=excluded.net_r,
                    mae_pct=excluded.mae_pct,
                    mfe_pct=excluded.mfe_pct,
                    fill_status=excluded.fill_status,
                    payload_json=excluded.payload_json
                """,
                (
                    str(event_key),
                    int(payload.get("horizon") or 0),
                    str(payload.get("direction") or "none"),
                    str(payload.get("code") or ""),
                    str(payload.get("trade_date") or ""),
                    None if target_first is None else int(bool(target_first)),
                    float(payload.get("net_return_pct") or 0),
                    float(payload.get("net_r") or 0),
                    float(payload.get("mae_pct") or 0),
                    float(payload.get("mfe_pct") or 0),
                    str(payload.get("fill_status") or "no_fill"),
                    self._json(payload),
                ),
            )

        if _connection is not None:
            write(_connection)
        else:
            with self._lock, self._connect() as connection:
                write(connection)

    def record_data_manifest(
        self,
        manifest: Any,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        if not self.enabled:
            return
        payload = self._dump(manifest)
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO data_manifests
                    (trade_date, code, source_quality, minute_count, transaction_count,
                     minute_coverage, transaction_coverage, transaction_minute_count,
                     transaction_page_count, transaction_sequence_count,
                     transaction_raw_time_count, transaction_gap_count, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code) DO UPDATE SET
                    source_quality=excluded.source_quality,
                    minute_count=excluded.minute_count,
                    transaction_count=excluded.transaction_count,
                    minute_coverage=excluded.minute_coverage,
                    transaction_coverage=excluded.transaction_coverage,
                    transaction_minute_count=excluded.transaction_minute_count,
                    transaction_page_count=excluded.transaction_page_count,
                    transaction_sequence_count=excluded.transaction_sequence_count,
                    transaction_raw_time_count=excluded.transaction_raw_time_count,
                    transaction_gap_count=excluded.transaction_gap_count,
                    payload_json=excluded.payload_json
                """,
                (
                    str(payload.get("trade_date") or ""),
                    str(payload.get("code") or ""),
                    str(payload.get("source_quality") or "unavailable"),
                    int(payload.get("minute_count") or 0),
                    int(payload.get("transaction_count") or 0),
                    float(payload.get("minute_coverage") or 0),
                    float(payload.get("transaction_coverage") or 0),
                    int(payload.get("transaction_minute_count") or 0),
                    int(payload.get("transaction_page_count") or 0),
                    int(payload.get("transaction_sequence_count") or 0),
                    int(payload.get("transaction_raw_time_count") or 0),
                    len(payload.get("transaction_time_gaps") or []),
                    self._json(payload),
                ),
            )

        if _connection is not None:
            write(_connection)
        else:
            with self._lock, self._connect() as connection:
                write(connection)

    def cleanup_high_frequency_history(
        self,
        *,
        retain_trade_days: int = 2,
        truncate_wal: bool = True,
    ) -> dict[str, Any]:
        """Drop old high-frequency dashboard rows while keeping research projections.

        This intentionally does not run VACUUM.  Deleting rows releases pages for
        future SQLite reuse, while VACUUM rewrites the whole database and is too
        expensive for normal API startup on a large intraday store.
        """

        if not self.enabled or not self.path.exists():
            return {
                "enabled": False,
                "db_file": str(self.path),
                "skipped": "disabled_or_missing",
                "deleted_rows": 0,
            }
        retain = max(1, int(retain_trade_days or 1))
        union_sql = "\nUNION\n".join(
            f"SELECT trade_date FROM {table} WHERE trade_date != ''"
            for table in self.HIGH_FREQUENCY_TABLES
        )
        deleted_by_table: dict[str, int] = {}
        with self._lock:
            connection = self._connect()
            try:
                dates = [
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT trade_date FROM ({union_sql}) ORDER BY trade_date DESC"
                    ).fetchall()
                ]
                keep_dates = dates[:retain]
                if not keep_dates:
                    return {
                        "enabled": True,
                        "db_file": str(self.path),
                        "retain_trade_days": retain,
                        "keep_trade_dates": [],
                        "deleted_by_table": {},
                        "deleted_rows": 0,
                    }
                placeholders = ",".join("?" for _ in keep_dates)
                with connection:
                    for table in self.HIGH_FREQUENCY_TABLES:
                        cursor = connection.execute(
                            f"DELETE FROM {table} WHERE trade_date NOT IN ({placeholders})",
                            tuple(keep_dates),
                        )
                        deleted_by_table[table] = max(0, int(cursor.rowcount or 0))
                checkpoint: tuple[int, ...] | None = None
                if truncate_wal:
                    try:
                        checkpoint = tuple(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() or ())
                    except sqlite3.OperationalError:
                        checkpoint = None
                return {
                    "enabled": True,
                    "db_file": str(self.path),
                    "retain_trade_days": retain,
                    "keep_trade_dates": keep_dates,
                    "deleted_by_table": deleted_by_table,
                    "deleted_rows": sum(deleted_by_table.values()),
                    "wal_checkpoint": checkpoint,
                }
            finally:
                connection.close()

    def _upsert_signal_transition(
        self,
        connection: sqlite3.Connection,
        *,
        trade_date: str,
        time: str,
        code: str,
        phase: str,
        signal: str,
        score: float,
        invalidation_price: float,
        source_quality: str,
        reasons: list[str],
        risks: list[str],
    ) -> None:
        connection.execute(
            """
            INSERT INTO signal_transitions
                (trade_date, time, code, phase, signal, score, invalidation_price, source_quality, reason_json, risk_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, time, code, phase) DO UPDATE SET
                signal=excluded.signal,
                score=excluded.score,
                invalidation_price=excluded.invalidation_price,
                source_quality=excluded.source_quality,
                reason_json=excluded.reason_json,
                risk_json=excluded.risk_json
            """,
            (
                trade_date,
                time,
                code,
                phase,
                signal,
                score,
                invalidation_price,
                source_quality,
                self._json(reasons),
                self._json(risks),
            ),
        )

    @staticmethod
    def decode_payload(value: Any) -> dict[str, Any]:
        """Read both compressed (zlib BLOB) and legacy plain-JSON payloads."""
        if not value:
            return {}
        try:
            if isinstance(value, (bytes, bytearray, memoryview)):
                raw = bytes(value)
                try:
                    text = zlib.decompress(raw).decode("utf-8")
                except zlib.error:
                    text = raw.decode("utf-8")
            else:
                text = str(value)
            payload = json.loads(text)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    # 内部沿用旧名
    _loads = decode_payload

    def _stock_feature_row_from_record(self, row: Any) -> dict[str, Any]:
        """把 stock_features 记录还原成读取端瘦行。

        新数据走列化快路径：vol/minute_amount 是独立列，sqlite3 直接返回原生
        数值，完全不用碰 payload_json（零 zlib/JSON decode）。老数据这两列为
        NULL，回退 payload 解码一次（结果随后进内存镜像，每进程只付一次）。
        """
        captured_at = str(row["captured_at"] or "")
        base = {
            "captured_at": captured_at,
            "price": float(row["price"] or 0),
            "change_pct": float(row["change_pct"] or 0),
            "amount": float(row["amount"] or 0),
            "minute_amount_ratio": float(row["minute_amount_ratio"] or 1),
        }
        vol = row["vol"] if "vol" in row.keys() else None
        if vol is not None:
            volume_value = float(vol)
            base["minute_amount"] = float(row["minute_amount"] or 0)
            base["vol"] = volume_value
            base["volume"] = volume_value
            return base
        payload = self._loads(row["payload_json"])
        payload.update(base)
        return payload

    def stock_feature_series(
        self,
        trade_date: str,
        code: str,
        *,
        max_rows: int = 720,
    ) -> list[dict[str, Any]]:
        """Return persisted quote features for a stock without upstream reads."""

        normalized_date = str(trade_date or "").strip()
        normalized_code = str(code or "").zfill(6)
        if not self.enabled or not self.path.exists() or not normalized_date or not normalized_code:
            return []
        limit = max(1, min(int(max_rows or 720), 2880))
        # 内存镜像优先：与采集写穿透共享同一份数据，命中时零 SQL 零解压
        mem = self._feature_mem_get(normalized_date, normalized_code, limit)
        if mem is not None:
            return mem
        # 纯读取走独立连接且不持有 _lock：WAL 模式允许读写、读读并发，
        # 否则详情页的单票查询会被后台 120 票批量读/全量写入卡住数秒。
        with self._read_connect() as connection:
            rows = connection.execute(
                """
                SELECT captured_at, price, change_pct, amount, minute_amount, vol, minute_amount_ratio, payload_json
                FROM stock_features
                WHERE trade_date = ? AND code = ?
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                (normalized_date, normalized_code, limit),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in reversed(rows):
            output.append(self._stock_feature_row_from_record(row))
        self._feature_mem_backfill(normalized_date, normalized_code, output, limit)
        return output

    def stock_feature_series_by_code(
        self,
        trade_date: str,
        codes: Iterable[str],
        *,
        max_rows: int = 1200,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return recent stock feature rows for multiple codes without reopening SQLite."""

        normalized_date = str(trade_date or "").strip()
        normalized_codes = list(dict.fromkeys(str(code or "").zfill(6) for code in codes if str(code or "").strip()))
        if not self.enabled or not self.path.exists() or not normalized_date or not normalized_codes:
            return {}
        limit = max(1, min(int(max_rows or 1200), 2880))
        output: dict[str, list[dict[str, Any]]] = {}
        # 内存镜像命中部分直接返回（零 SQL 零解压）；未命中的才走批量 SQL 并回填
        sql_codes: list[str] = []
        for code in normalized_codes:
            mem = self._feature_mem_get(normalized_date, code, limit)
            if mem is None:
                sql_codes.append(code)
            elif mem:
                output[code] = mem
        if not sql_codes:
            return output
        # 同上：纯读取不持有 _lock，避免后台批量读/写挡住详情页查询。
        with self._read_connect() as connection:
            for index, code in enumerate(sql_codes):
                # 大批量读取（后台全量刷新 120 票 × 180 行，含 zlib 解压）是
                # CPU 密集循环；周期性出让 GIL，避免前台请求被车队效应饿死。
                if index and index % 16 == 0:
                    time.sleep(0)
                rows = connection.execute(
                    """
                    SELECT captured_at, code, price, change_pct, amount, minute_amount, vol, minute_amount_ratio, payload_json
                    FROM stock_features
                    WHERE trade_date = ? AND code = ?
                    ORDER BY captured_at DESC
                    LIMIT ?
                    """,
                    (normalized_date, code, limit),
                ).fetchall()
                series: list[dict[str, Any]] = []
                for row in reversed(rows):
                    series.append(self._stock_feature_row_from_record(row))
                # 空序列也回填：停牌票当日无行是合法结果，避免每轮重复空查
                self._feature_mem_backfill(normalized_date, code, series, limit)
                if series:
                    output[code] = series
        return output

    def stock_feature_mini_series(
        self,
        trade_date: str,
        code: str,
        *,
        max_rows: int = 240,
    ) -> list[dict[str, Any]]:
        normalized_date = str(trade_date or "").strip()
        normalized_code = str(code or "").zfill(6)
        if not self.enabled or not self.path.exists() or not normalized_date or not normalized_code:
            return []
        output = self.stock_feature_mini_series_by_code(
            normalized_date,
            [normalized_code],
            max_rows=max_rows,
        )
        return list(output.get(normalized_code) or [])

    _MINI_SERIES_SQL = """
        SELECT captured_at, price, change_pct, amount, minute_amount_ratio
        FROM (
            SELECT captured_at, price, change_pct, amount, minute_amount_ratio,
                   ROW_NUMBER() OVER (ORDER BY captured_at) AS rn,
                   COUNT(*) OVER () AS total
            FROM stock_features
            WHERE code = ? AND trade_date = ?
        )
        WHERE rn = 1 OR rn = total OR rn % MAX(1, total / 720) = 0
        ORDER BY captured_at
    """

    def _mini_series_rows(
        self,
        connection: sqlite3.Connection,
        trade_date: str,
        code: str,
    ) -> list[dict[str, Any]]:
        # 全天均匀采样：旧写法 ORDER BY captured_at DESC LIMIT 720
        # 只取到当天「最近 720 次抓取」（约 12:25 之后），导致缩略图丢
        # 掉整个上午。这里按行号等距抽样，始终覆盖 09:30→15:00 全天。
        rows = connection.execute(self._MINI_SERIES_SQL, (code, trade_date)).fetchall()
        return [
            {
                "captured_at": str(row["captured_at"] or ""),
                "price": float(row["price"] or 0),
                "change_pct": float(row["change_pct"] or 0),
                "amount": float(row["amount"] or 0),
                "minute_amount_ratio": float(row["minute_amount_ratio"] or 1),
            }
            for row in rows
        ]

    def _mini_series_worker(self, trade_date: str, codes: list[str]) -> dict[str, list[dict[str, Any]]]:
        # 每线程独立只读连接（WAL 支持并发读），绕开全局锁串行瓶颈；
        # 连接必须在使用它的线程内创建（sqlite3 check_same_thread）。
        output: dict[str, list[dict[str, Any]]] = {}
        connection = self._read_connect(timeout=10)
        try:
            for code in codes:
                rows = self._mini_series_rows(connection, trade_date, code)
                if rows:
                    output[code] = rows
        finally:
            connection.close()
        return output

    def stock_feature_mini_series_by_code(
        self,
        trade_date: str,
        codes: Iterable[str],
        *,
        max_rows: int = 240,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return representative stock feature rows across the full session.

        This keeps the early open, intraday extremes and late-session tail
        while returning far fewer rows than the raw per-second feature table.
        """

        normalized_date = str(trade_date or "").strip()
        normalized_codes = list(dict.fromkeys(str(code or "").zfill(6) for code in codes if str(code or "").strip()))
        if not self.enabled or not self.path.exists() or not normalized_date or not normalized_codes:
            return {}
        limit = max(8, min(int(max_rows or 240), 240))
        output: dict[str, list[dict[str, Any]]] = {}
        try:
            if len(normalized_codes) >= 6:
                # 大批量（首屏榜单 40+ 票）：每票约 3.6k 行扫描是 I/O 密集，
                # 多线程并发读把串行 40×40ms 压到约 1/6。
                workers = min(6, len(normalized_codes))
                chunk_size = (len(normalized_codes) + workers - 1) // workers
                chunks = [normalized_codes[i : i + chunk_size] for i in range(0, len(normalized_codes), chunk_size)]
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mini-series") as executor:
                    for partial in executor.map(lambda chunk: self._mini_series_worker(normalized_date, chunk), chunks):
                        output.update(partial)
            else:
                with self._lock, self._read_connect() as connection:
                    for code in normalized_codes:
                        rows = self._mini_series_rows(connection, normalized_date, code)
                        if rows:
                            output[code] = rows
        except sqlite3.OperationalError:
            return {}
        return {
            code: self._representative_stock_feature_rows(rows, limit)
            for code, rows in output.items()
        }

    def stock_feature_minute_bars_by_code(
        self,
        trade_date: str,
        codes: Iterable[str],
        *,
        max_minutes: int = 300,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return per-code uniform 1-minute bars (minute's last price/amount).

        Unlike the representative mini sampling, this keeps a uniform minute
        grid so相邻两根的成交额增量就是该分钟的真实成交增量，不会在采样密度
        突变处产生伪影尖刺。
        """

        normalized_date = str(trade_date or "").strip()
        normalized_codes = list(dict.fromkeys(str(code or "").zfill(6) for code in codes if str(code or "").strip()))
        if not self.enabled or not self.path.exists() or not normalized_date or not normalized_codes:
            return {}
        limit = max(30, min(int(max_minutes or 300), 480))
        output: dict[str, list[dict[str, Any]]] = {}
        try:
            with self._lock, self._read_connect() as connection:
                for code in normalized_codes:
                    rows = connection.execute(
                        """
                        SELECT minute, price, amount FROM (
                            SELECT substr(captured_at, 1, 5) AS minute, price, amount,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY substr(captured_at, 1, 5)
                                       ORDER BY captured_at DESC
                                   ) AS rn
                            FROM stock_features
                            WHERE code = ? AND trade_date = ?
                        )
                        WHERE rn = 1
                        ORDER BY minute DESC
                        LIMIT ?
                        """,
                        (code, normalized_date, limit),
                    ).fetchall()
                    if not rows:
                        continue
                    output[code] = [
                        {
                            "time": str(row["minute"] or ""),
                            "price": float(row["price"] or 0),
                            "amount": float(row["amount"] or 0),
                        }
                        for row in reversed(rows)
                    ]
        except sqlite3.OperationalError:
            return {}
        return output

    def stock_feature_ticks_by_code(
        self,
        trade_date: str,
        codes: Iterable[str],
        *,
        max_rows: int = 2880,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return per-code raw observation ticks (captured_at/price/amount), ascending.

        板块资金动能回灌专用：相邻观测的成交额增量 × 价格方向就是近 tick 级
        净流入步长——比分钟棒的二元方向细一个量级，锯齿噪声随窗口缩小而消失。
        不读 payload_json，控制 80GB 库上的批量 IO/解压开销。
        """

        normalized_date = str(trade_date or "").strip()
        normalized_codes = list(dict.fromkeys(str(code or "").zfill(6) for code in codes if str(code or "").strip()))
        if not self.enabled or not self.path.exists() or not normalized_date or not normalized_codes:
            return {}
        limit = max(30, min(int(max_rows or 2880), 2880))
        output: dict[str, list[dict[str, Any]]] = {}
        try:
            with self._read_connect() as connection:
                for index, code in enumerate(normalized_codes):
                    # 大批量读取周期出让 GIL，避免前台请求被车队效应饿死
                    if index and index % 16 == 0:
                        time.sleep(0)
                    rows = connection.execute(
                        """
                        SELECT captured_at, price, amount
                        FROM stock_features
                        WHERE code = ? AND trade_date = ?
                        ORDER BY captured_at DESC
                        LIMIT ?
                        """,
                        (code, normalized_date, limit),
                    ).fetchall()
                    if not rows:
                        continue
                    output[code] = [
                        {
                            "time": str(row["captured_at"] or ""),
                            "price": float(row["price"] or 0),
                            "amount": float(row["amount"] or 0),
                        }
                        for row in reversed(rows)
                    ]
        except sqlite3.OperationalError:
            return {}
        return output

    @staticmethod
    def _feature_time_label(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if "T" in text:
            text = text.rsplit("T", 1)[-1]
        if " " in text:
            text = text.rsplit(" ", 1)[-1]
        if len(text) >= 5 and text[2] == ":":
            return text[:5]
        if len(text) >= 4 and text[:4].isdigit():
            return f"{text[:2]}:{text[2:4]}"
        return text[:5]

    @staticmethod
    def _is_regular_feature_time(value: Any) -> bool:
        label = IntradayWatchtowerStore._feature_time_label(value)
        return ("09:30" <= label <= "11:30") or ("13:00" <= label <= "15:00")

    @staticmethod
    def _representative_stock_feature_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        regular_rows = [
            row
            for row in rows
            if IntradayWatchtowerStore._is_regular_feature_time(row.get("captured_at"))
        ]
        source_rows = regular_rows if len(regular_rows) >= 2 else rows
        if len(source_rows) <= limit:
            return source_rows
        last_index = len(source_rows) - 1

        def change_at(index: int) -> float:
            try:
                return float(source_rows[index].get("change_pct") or 0)
            except (TypeError, ValueError):
                return 0.0

        keep: set[int] = {0, last_index}
        keep.add(min(range(len(source_rows)), key=change_at))
        keep.add(max(range(len(source_rows)), key=change_at))
        for position in range(limit):
            keep.add(round(position * last_index / max(limit - 1, 1)))
            if len(keep) >= limit:
                break
        if len(keep) > limit:
            protected = {
                0,
                last_index,
                min(range(len(source_rows)), key=change_at),
                max(range(len(source_rows)), key=change_at),
            }
            remaining = [index for index in sorted(keep) if index not in protected]
            target = max(0, limit - len(protected))
            if target and remaining:
                step = (len(remaining) - 1) / max(target - 1, 1)
                protected.update(remaining[round(pos * step)] for pos in range(target))
            keep = protected
        return [source_rows[index] for index in sorted(keep)]

    def sector_feature_series(
        self,
        trade_date: str,
        sector_name: str,
        *,
        max_rows: int = 720,
    ) -> list[dict[str, Any]]:
        """Return persisted sector trajectory points without upstream reads."""

        normalized_date = str(trade_date or "").strip()
        normalized_name = str(sector_name or "").strip()
        if not self.enabled or not self.path.exists() or not normalized_date or not normalized_name:
            return []
        limit = max(1, min(int(max_rows or 720), 2880))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT captured_at, heat_score, avg_change_pct, flow_delta, payload_json
                FROM sector_trajectory
                WHERE trade_date = ? AND sector_name = ?
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                (normalized_date, normalized_name, limit),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload = self._loads(row["payload_json"])
            payload.update(
                {
                    "captured_at": str(row["captured_at"] or ""),
                    "heat_score": float(row["heat_score"] or 0),
                    "avg_change_pct": float(row["avg_change_pct"] or 0),
                    "flow_delta": float(row["flow_delta"] or 0),
                }
            )
            output.append(payload)
        return output

    def sector_feature_series_by_name(
        self,
        trade_date: str,
        sector_names: Iterable[str],
        *,
        max_rows: int = 180,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return recent sector trajectory rows for multiple sectors without reopening SQLite."""

        normalized_date = str(trade_date or "").strip()
        normalized_names = list(dict.fromkeys(str(name or "").strip() for name in sector_names if str(name or "").strip()))
        if not self.enabled or not self.path.exists() or not normalized_date or not normalized_names:
            return {}
        limit = max(1, min(int(max_rows or 180), 720))
        output: dict[str, list[dict[str, Any]]] = {}
        with self._lock, self._connect() as connection:
            for name in normalized_names:
                rows = connection.execute(
                    """
                    SELECT captured_at, sector_name, heat_score, avg_change_pct, flow_delta, payload_json
                    FROM sector_trajectory
                    WHERE trade_date = ? AND sector_name = ?
                    ORDER BY captured_at DESC
                    LIMIT ?
                    """,
                    (normalized_date, name, limit),
                ).fetchall()
                if not rows:
                    continue
                series: list[dict[str, Any]] = []
                for row in reversed(rows):
                    payload = self._loads(row["payload_json"])
                    payload.update(
                        {
                            "captured_at": str(row["captured_at"] or ""),
                            "heat_score": float(row["heat_score"] or 0),
                            "avg_change_pct": float(row["avg_change_pct"] or 0),
                            "flow_delta": float(row["flow_delta"] or 0),
                        }
                    )
                    series.append(payload)
                output[name] = series
        return output

    def latest_context_payload(self, trade_date: str | None = None) -> dict[str, Any] | None:
        """Return the latest persisted dashboard frame without contacting upstream data sources."""

        if not self.enabled:
            return None
        latest_file_payload = self._read_latest_context_file(trade_date) if self.path.exists() else None
        if latest_file_payload is not None:
            return latest_file_payload
        latest_cloud_payload = self._read_latest_context_cloud(trade_date)
        if latest_cloud_payload is not None:
            return latest_cloud_payload
        if not self.path.exists():
            return None
        return self._latest_context_payload_from_sqlite(trade_date)

    def _latest_context_payload_from_sqlite(self, trade_date: str | None = None) -> dict[str, Any] | None:
        normalized_date = str(trade_date or "").strip()
        market_sql = """
            SELECT trade_date, captured_at, updated_at, frozen, source_quality, payload_json
            FROM market_trajectory
        """
        params: tuple[Any, ...] = ()
        if normalized_date:
            market_sql += " WHERE trade_date = ?"
            params = (normalized_date,)
        market_sql += " ORDER BY id DESC LIMIT 1"
        with self._lock, self._connect() as connection:
            market_row = connection.execute(market_sql, params).fetchone()
            if market_row is None:
                return None
            row_trade_date = str(market_row["trade_date"] or "")
            captured_at = str(market_row["captured_at"] or "")
            sectors = connection.execute(
                """
                SELECT payload_json
                FROM sector_trajectory
                WHERE trade_date = ? AND captured_at = ?
                ORDER BY heat_score DESC, sector_name
                """,
                (row_trade_date, captured_at),
            ).fetchall()
            quotes = connection.execute(
                """
                SELECT payload_json
                FROM stock_features
                WHERE trade_date = ? AND captured_at = ?
                ORDER BY code
                """,
                (row_trade_date, captured_at),
            ).fetchall()
        return {
            "trade_date": row_trade_date,
            "captured_at": captured_at,
            "updated_at": str(market_row["updated_at"] or ""),
            "frozen": bool(market_row["frozen"]),
            "source_quality": str(market_row["source_quality"] or ""),
            "market": self._loads(market_row["payload_json"]),
            "sectors": [self._loads(row["payload_json"]) for row in sectors],
            "quotes": [self._loads(row["payload_json"]) for row in quotes],
        }

    def status(self) -> dict[str, Any]:
        if not self.enabled or not self.path.exists():
            return {"enabled": False, "db_file": str(self.path)}
        with self._lock, self._connect() as connection:
            counts = {
                "market_snapshots": int(connection.execute("SELECT COUNT(*) FROM market_trajectory").fetchone()[0]),
                "sector_snapshots": int(connection.execute("SELECT COUNT(*) FROM sector_trajectory").fetchone()[0]),
                "stock_snapshots": int(connection.execute("SELECT COUNT(*) FROM stock_features").fetchone()[0]),
                "signal_transitions": int(connection.execute("SELECT COUNT(*) FROM signal_transitions").fetchone()[0]),
                "strategy_events": int(connection.execute("SELECT COUNT(*) FROM strategy_events").fetchone()[0]),
                "daily_regimes": int(connection.execute("SELECT COUNT(*) FROM daily_regimes").fetchone()[0]),
                "research_runs": int(connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0]),
                "trade_outcomes": int(connection.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0]),
                "data_manifests": int(connection.execute("SELECT COUNT(*) FROM data_manifests").fetchone()[0]),
            }
            latest = connection.execute(
                "SELECT trade_date, captured_at, source_quality FROM market_trajectory ORDER BY id DESC LIMIT 1"
            ).fetchone()
            latest_research = connection.execute(
                """
                SELECT run_id, finished_at, protocol_version, validation_status,
                       sample_days, sample_events, oos_events
                FROM research_runs ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return {
            "enabled": True,
            "db_file": str(self.path),
            "schema_version": self.SCHEMA_VERSION,
            **counts,
            "latest_trade_date": str(latest[0]) if latest else "",
            "latest_captured_at": str(latest[1]) if latest else "",
            "latest_source_quality": str(latest[2]) if latest else "",
            "latest_research_run": {
                "run_id": str(latest_research[0]),
                "finished_at": str(latest_research[1]),
                "protocol_version": str(latest_research[2]),
                "validation_status": str(latest_research[3]),
                "sample_days": int(latest_research[4] or 0),
                "sample_events": int(latest_research[5] or 0),
                "oos_events": int(latest_research[6] or 0),
            } if latest_research else None,
        }

    def close(self) -> None:
        # Connections are short-lived by design; this method exists for the
        # collector lifecycle and future repository implementations.
        return None


class IntradayCollector:
    """Minimal background-loop contract used by the FastAPI application."""

    def __init__(self, service: Any, interval_seconds: int = 5) -> None:
        self.service = service
        self.interval_seconds = max(1, int(interval_seconds))
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.service.collect_once()
            except Exception:
                # A source outage must not terminate the API process.  The
                # next cycle can recover and the normal dashboard exposes the
                # source quality to the operator.
                continue
