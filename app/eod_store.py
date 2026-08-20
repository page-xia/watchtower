"""EOD 数据访问层（2026-08-18：tushare_eod.sqlite → MySQL 统一）。

数据源边界不变（见 AGENTS.md）：本层只做 Tushare 收盘数据的存取，盘中实时
行情与 L1 磁带仍走 easy_tdx。

两个后端：

- ``mysql``（本地默认）：pymysql 直连 MySQL，库默认 ``watchtower_eod``
  （``ts2db_config.yaml`` 的 ``db_config`` 或 ``WATCH_EOD_MYSQL_*`` 覆盖），
  表结构/回填由 ``scripts/ingest_eod_tushare.py`` 负责，表名统一 ``eod_*``。
- ``cloudbase_snapshot``（生产云托管）：容器访问不到本地 MySQL，CloudBase
  rdb REST 网关是行接口、跑不了暗盘面板的聚合 SQL，因此 EOD 面板改由收盘
  管线预计算成快照推送 CloudBase NoSQL
  （``ingest_eod_tushare.py --push-prod``），本层读快照文档。

临时/热数据一律留内存：本层自带 latest_date（60s）与快照（300s）TTL 缓存，
上层 dark_pool 还有整包 300s TTL（EOD_CACHE_TTL_SECONDS）。
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from typing import Any, Protocol

logger = logging.getLogger(__name__)

TABLE_PREFIX = "eod_"
LATEST_DATE_CACHE_SECONDS = 60
SNAPSHOT_CACHE_SECONDS = 300
SNAPSHOT_NAMESPACE = "dark_pool"
SNAPSHOT_KEY = "eod_snapshot"
# 按需补数（2026-08-18）：快照未覆盖的个股，容器登记请求 → 本地管线履约
# （scripts/fulfill_dark_pool_requests.py）→ 单票摘要文档，读侧回退读取。
STOCK_NAMESPACE = "dark_pool_stock"
REQUEST_NAMESPACE = "dark_pool"
REQUEST_KEY = "eod_requests"
STOCK_HIT_CACHE_SECONDS = 300    # 单票摘要为日频数据，命中缓存长一点
STOCK_MISS_CACHE_SECONDS = 15    # 未命中短缓存：履约后尽快可见，又不打爆 NoSQL
REQUEST_DEDUP_SECONDS = 600      # 同一代码 10 分钟内不重复登记请求


class EodStore(Protocol):
    """暗盘资金等模块依赖的最小接口。"""

    backend: str

    @property
    def available(self) -> bool: ...

    def table(self, logical: str) -> str:
        """逻辑数据集名（moneyflow / daily_basic …）→ 物理表名。"""
        ...

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        """执行只读 SQL，返回 dict 行；trade_date 等日期值归一成 'YYYYMMDD' 字符串。"""
        ...

    def latest_date(self, logical: str) -> str:
        """该数据集最新交易日（'YYYYMMDD'），无数据返回 ''。"""
        ...


def _normalize_value(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y%m%d%H%M%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y%m%d")
    if isinstance(value, _dt.time):
        return value.strftime("%H:%M:%S")
    return value


class PyMySqlEodStore:
    """本地/直连 MySQL 后端：每次查询开短连接，避免长连接空闲被服务端踢掉。"""

    backend = "mysql"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        db: str = "watchtower_eod",
        connect_timeout: float = 3.0,
        read_timeout: float = 15.0,
    ) -> None:
        self._conn_kwargs = {
            "host": host,
            "port": int(port),
            "user": user,
            "password": password,
            "database": db,
            "connect_timeout": connect_timeout,
            "read_timeout": read_timeout,
            "charset": "utf8mb4",
        }
        self.db = db
        self._latest_cache: dict[str, tuple[float, str]] = {}
        self._latest_lock = threading.Lock()

    @property
    def available(self) -> bool:
        try:
            import pymysql  # noqa: F401
        except ImportError:
            return False
        return bool(self._conn_kwargs["database"])

    def table(self, logical: str) -> str:
        return f"{TABLE_PREFIX}{logical}"

    def _connect(self) -> Any:
        import pymysql

        return pymysql.connect(**self._conn_kwargs)

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        import pymysql.cursors

        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                # 空参数传 None：pymysql 对非 None args 会做 % 转义格式化，
                # 会把 SQL 里的 DATE_FORMAT('%Y%m%d') 字面量当占位符报错。
                cur.execute(sql, args if args else None)
                rows = cur.fetchall()
        finally:
            conn.close()
        return [{k: _normalize_value(v) for k, v in row.items()} for row in rows]

    def latest_date(self, logical: str) -> str:
        now = time.monotonic()
        with self._latest_lock:
            cached = self._latest_cache.get(logical)
            if cached and now - cached[0] < LATEST_DATE_CACHE_SECONDS:
                return cached[1]
        try:
            rows = self.query(f"SELECT DATE_FORMAT(MAX(trade_date), '%Y%m%d') AS d FROM {self.table(logical)}")
            value = str(rows[0]["d"] or "") if rows else ""
        except Exception as exc:  # noqa: BLE001 - 表不存在等场景按无数据处理
            logger.warning("eod latest_date failed for %s: %s", logical, exc)
            value = ""
        with self._latest_lock:
            self._latest_cache[logical] = (now, value)
        return value

    def invalidate_latest(self, logical: str | None = None) -> None:
        with self._latest_lock:
            if logical is None:
                self._latest_cache.clear()
            else:
                self._latest_cache.pop(logical, None)


class SnapshotEodStore:
    """生产快照后端：读收盘管线预计算并推送到 CloudBase NoSQL 的暗盘快照。

    快照结构即 dark_pool 面板/个股摘要的输出形状（含名称/板块），读侧零计算。
    快照未覆盖的个股：stock_summary 回退读单票摘要文档（STOCK_NAMESPACE/<code>，
    由本地履约管线按需补数），request_stock 负责登记补数请求。
    """

    backend = "cloudbase_snapshot"

    def __init__(self, state_store: Any, cache_seconds: float = SNAPSHOT_CACHE_SECONDS) -> None:
        self._store = state_store
        self._cache_seconds = max(1.0, float(cache_seconds))
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self._stock_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._requested: dict[str, float] = {}

    @property
    def available(self) -> bool:
        return bool(self._store is not None and getattr(self._store, "available", False))

    def table(self, logical: str) -> str:  # 快照模式没有物理表，保留接口形状
        return f"{TABLE_PREFIX}{logical}"

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        raise NotImplementedError("cloudbase_snapshot 后端不支持 SQL，请用 eod_payload/stock_summary")

    def latest_date(self, logical: str) -> str:
        return str(self.eod_payload().get("trade_date") or "")

    def eod_payload(self) -> dict[str, Any]:
        """首页暗盘 EOD 快照（market/absorb/offmarket），无快照返回 {'available': False}。"""
        now = time.monotonic()
        with self._lock:
            if self._cache and now - self._cache[0] < self._cache_seconds:
                return dict(self._cache[1])
        doc: dict[str, Any] = {}
        if self.available:
            try:
                doc = self._store.get_json(SNAPSHOT_NAMESPACE, SNAPSHOT_KEY, {}) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("eod snapshot fetch failed: %s", exc)
                doc = {}
        payload = dict(doc) if doc.get("available") else {
            "available": False,
            "note": "尚未推送收盘快照：scripts/ingest_eod_tushare.py --push-prod",
        }
        with self._lock:
            self._cache = (now, payload)
        return dict(payload)

    def stock_summary(self, code: str) -> dict[str, Any] | None:
        code = str(code or "").zfill(6)
        stocks = self.eod_payload().get("stocks") or {}
        summary = stocks.get(code)
        if isinstance(summary, dict):
            return dict(summary)
        return self._stock_doc(code)

    def _stock_doc(self, code: str) -> dict[str, Any] | None:
        """单票摘要文档回退：按需补数履约结果（STOCK_NAMESPACE/<code>）。"""
        now = time.monotonic()
        with self._lock:
            cached = self._stock_cache.get(code)
        if cached:
            ttl = STOCK_HIT_CACHE_SECONDS if cached[1] else STOCK_MISS_CACHE_SECONDS
            if now - cached[0] < ttl:
                return dict(cached[1]) if cached[1] else None
        doc: dict[str, Any] | None = None
        if self.available:
            try:
                raw = self._store.get_json(STOCK_NAMESPACE, code, None)
                if isinstance(raw, dict) and raw.get("eod_available"):
                    doc = raw
            except Exception as exc:  # noqa: BLE001
                logger.warning("eod stock doc fetch failed for %s: %s", code, exc)
        with self._lock:
            self._stock_cache[code] = (now, doc)
        return dict(doc) if doc else None

    def request_stock(self, code: str) -> bool:
        """登记个股补数请求（REQUEST_NAMESPACE/REQUEST_KEY），返回是否已确保在队列中。

        本地管线（scripts/fulfill_dark_pool_requests.py）轮询该文档，用本地 MySQL
        计算个股摘要后写 STOCK_NAMESPACE/<code> 并出队。进程内 10 分钟去重。
        """
        code = str(code or "").zfill(6)
        if not (len(code) == 6 and code.isdigit()) or not self.available:
            return False
        now = time.monotonic()
        with self._lock:
            last = self._requested.get(code, 0.0)
            if now - last < REQUEST_DEDUP_SECONDS:
                return True
        try:
            doc = self._store.get_json(REQUEST_NAMESPACE, REQUEST_KEY, {}) or {}
            codes = doc.get("codes") if isinstance(doc, dict) else None
            codes = dict(codes) if isinstance(codes, dict) else {}
            codes[code] = int(time.time())
            self._store.set_json(REQUEST_NAMESPACE, REQUEST_KEY, {"codes": codes})
        except Exception as exc:  # noqa: BLE001
            logger.warning("eod stock request register failed for %s: %s", code, exc)
            return False
        with self._lock:
            self._requested[code] = now
        return True


def build_eod_store(settings: Any) -> EodStore:
    """按配置构建 EOD 访问层：默认本地 MySQL，生产云托管用快照。"""
    backend = str(getattr(settings, "eod_store_backend", "mysql") or "mysql").strip().lower()
    if backend == "cloudbase_snapshot":
        state_store = _build_snapshot_state_store(settings)
        return SnapshotEodStore(state_store)
    cfg = dict(getattr(settings, "eod_db_config", {}) or {})
    return PyMySqlEodStore(
        host=str(cfg.get("host") or "127.0.0.1"),
        port=int(cfg.get("port") or 3306),
        user=str(cfg.get("user") or "root"),
        password=str(cfg.get("pwd") or cfg.get("password") or ""),
        db=str(cfg.get("db") or "watchtower_eod"),
    )


def _build_snapshot_state_store(settings: Any) -> Any:
    if not getattr(settings, "cloudbase_env_id", "") or not getattr(settings, "cloudbase_api_token", ""):
        return None
    from app.cloud_persistence import CloudBaseNoSqlStateStore

    return CloudBaseNoSqlStateStore(
        env_id=settings.cloudbase_env_id,
        token=settings.cloudbase_api_token,
        collection=settings.cloudbase_state_collection,
        instance=settings.cloudbase_database_instance,
        database=settings.cloudbase_database_name,
        base_url=settings.cloudbase_api_base_url or None,
        timeout=settings.cloudbase_api_timeout_seconds,
    )
