from __future__ import annotations

import copy
import json
import logging
import queue
import re
import threading
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from time import monotonic, sleep
from typing import Any

import httpx

from app.models import (
    MessageDetailPayload,
    MessageEvidence,
    MessageEvidenceBundle,
    MessageEvent,
    MessageEventLink,
    MessageStoreStatus,
    MessageSyncRunStatus,
    MessageTopic,
    ZsxqMessageIngestRequest,
    ZsxqMessageIngestResponse,
)


MESSAGE_OPENID = "watchtower"
MESSAGE_STORE_GET_RETRIES = 3
MESSAGE_STORE_MUTATION_RETRIES = 3
# CloudBase MySQL REST 网关对单请求行数/耗时敏感：大批量 upsert 容易超时或 500。
# upsert 按键幂等，按 100 行分块串行写入可稳定通过。
MESSAGE_STORE_UPSERT_CHUNK_SIZE = 100
# 写请求单独放宽超时（默认读超时只有几秒，批量 upsert 不够用）。
MESSAGE_STORE_MUTATION_MIN_TIMEOUT = 30.0
MESSAGE_EVIDENCE_STOCK_CANDIDATE_LIMIT = 120
MESSAGE_EVIDENCE_SECTOR_CANDIDATE_LIMIT = 80
MESSAGE_EVIDENCE_SECTOR_FUZZY_LIMIT = 40
MESSAGE_EVIDENCE_CACHE_TABLE = "message_evidence_cache"
# status() 的 count=exact 是大表全表计数，详情页不消费它，放宽 TTL 到 5 分钟。
MESSAGE_STATUS_CACHE_MIN_SECONDS = 300.0
# like '%关键词%' 只能全表扫描，串行化以免并发把 MySQL CPU 打满后集体超时。
MESSAGE_STORE_HEAVY_QUERY_CONCURRENCY = 2
# 直连 MySQL（mysql 后端）连接池上限：读线程池 6+12 并发，16 条连接够用且
# 不会把 RDS 小规格实例的连接数打满。
MESSAGE_STORE_MYSQL_POOL_SIZE = 16

# 各表主键：直连模式 upsert 的 ON DUPLICATE KEY UPDATE 只更新非主键列。
MYSQL_TABLE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "message_topics": ("topic_id",),
    "message_events": ("event_id",),
    "message_event_links": ("event_id", "entity_type", "code", "name"),
    "message_sync_runs": ("run_id",),
    "message_evidence_cache": ("scope", "cache_key"),
}

_MYSQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """表名/列名全部来自代码内部常量，仍统一校验标识符合法性，防止拼接注入。"""
    value = str(name or "").strip()
    if not _MYSQL_IDENTIFIER_RE.match(value):
        raise MessageStoreError(f"invalid mysql identifier: {name!r}")
    return value


def _translate_filter(column: str, expression: str) -> tuple[str, list[Any]]:
    """PostgREST 过滤表达式 → (WHERE 片段, 参数)。值一律走 %s 占位符。"""
    field = _validate_identifier(column)
    if expression.startswith("eq."):
        return f"{field} = %s", [expression[3:]]
    if expression.startswith("like."):
        return f"{field} LIKE %s", [expression[5:]]
    if expression.startswith("in.(") and expression.endswith(")"):
        # 实际调用点的 in 元素都是纯文本（不含引号/逗号），直接按逗号切。
        items = expression[4:-1].split(",")
        if not items or any(not item for item in items):
            return "1 = 0", []
        return f"{field} IN ({','.join(['%s'] * len(items))})", items
    raise MessageStoreError(f"unsupported filter expression: {column}={expression}")


def _build_select_sql(table: str, params: Iterable[tuple[str, str]]) -> tuple[str, list[Any]]:
    """把 REST 风格的查询参数翻译成参数化 SELECT，供直连模式与单元测试使用。"""
    select_clause = "*"
    order_clause = ""
    limit_clause = ""
    where_parts: list[str] = []
    args: list[Any] = []
    for key, raw_value in params:
        value = str(raw_value or "").strip()
        if key == "select":
            if value and value != "*":
                select_clause = ", ".join(_validate_identifier(part) for part in value.split(","))
        elif key == "order":
            order_parts = []
            for item in value.split(","):
                field, _, direction = item.strip().partition(".")
                order_parts.append(
                    f"{_validate_identifier(field)} {'DESC' if direction.strip().lower() == 'desc' else 'ASC'}"
                )
            if order_parts:
                order_clause = " ORDER BY " + ", ".join(order_parts)
        elif key == "limit":
            limit_clause = f" LIMIT {max(1, int(value))}"
        else:
            fragment, fragment_args = _translate_filter(key, value)
            where_parts.append(fragment)
            args.extend(fragment_args)
    sql = f"SELECT {select_clause} FROM {_validate_identifier(table)}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    return sql + order_clause + limit_clause, args


def _build_count_sql(table: str) -> str:
    return f"SELECT COUNT(*) AS cnt FROM {_validate_identifier(table)}"


def _build_upsert_sql(table: str, rows: list[dict[str, Any]]) -> tuple[str, list[Any]]:
    """INSERT ... ON DUPLICATE KEY UPDATE：语义对齐 REST 的 merge-duplicates。"""
    if not rows:
        raise MessageStoreError("upsert requires at least one row")
    table_name = _validate_identifier(table)
    columns = [_validate_identifier(key) for key in rows[0]]
    if not columns:
        raise MessageStoreError("upsert row has no columns")
    primary_keys = set(MYSQL_TABLE_PRIMARY_KEYS.get(table_name, ()))
    update_columns = [column for column in columns if column not in primary_keys]
    placeholders = "(" + ",".join(["%s"] * len(columns)) + ")"
    sql = (
        f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES "
        + ", ".join([placeholders] * len(rows))
    )
    if update_columns:
        sql += " ON DUPLICATE KEY UPDATE " + ", ".join(f"{c} = VALUES({c})" for c in update_columns)
    else:
        # 整行都是主键（当前没有这种表）：冲突时退化为幂等空更新。
        sql += f" ON DUPLICATE KEY UPDATE {columns[0]} = {columns[0]}"
    args = [row.get(column) for row in rows for column in columns]
    return sql, args


class _MySqlConnectionPool:
    """极简 pymysql 连接池：懒创建、用完归还、取出时 ping 保活。

    pymysql 连接不是线程安全的，MessageStore 读路径有 6+12 并发线程，
    必须每次独占一条连接；RDS 空闲会踢长连接，取出时 ping(reconnect=True)。
    """

    def __init__(self, conn_kwargs: dict[str, Any], max_size: int = MESSAGE_STORE_MYSQL_POOL_SIZE) -> None:
        self._conn_kwargs = conn_kwargs
        self._max_size = max(1, int(max_size))
        self._idle: queue.Queue[Any] = queue.Queue(maxsize=self._max_size)
        self._lock = threading.Lock()
        self._created = 0
        self._closed = False

    def acquire(self) -> Any:
        if self._closed:
            raise MessageStoreError("mysql connection pool is closed")
        conn: Any | None = None
        try:
            conn = self._idle.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._max_size:
                    self._created += 1
                    create = True
                else:
                    create = False
            if create:
                try:
                    conn = self._connect()
                except Exception:
                    with self._lock:
                        self._created -= 1
                    raise
            else:
                conn = self._idle.get()
        try:
            conn.ping(reconnect=True)
        except Exception:
            # 连接已死且重连失败：丢弃并释放配额，让下次取用时重建。
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1
            raise
        return conn

    def release(self, conn: Any) -> None:
        if self._closed:
            self._discard(conn)
            return
        try:
            self._idle.put_nowait(conn)
        except queue.Full:
            self._discard(conn)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                return
            self._discard(conn)

    def _connect(self) -> Any:
        import pymysql

        return pymysql.connect(**self._conn_kwargs)

    @staticmethod
    def _discard(conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass

logger = logging.getLogger(__name__)

MYSQL_SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS message_topics (
        _openid VARCHAR(128) NOT NULL DEFAULT 'watchtower',
        topic_id VARCHAR(128) NOT NULL,
        title TEXT NOT NULL,
        content MEDIUMTEXT NOT NULL,
        create_time VARCHAR(64) NOT NULL DEFAULT '',
        owner_name VARCHAR(255) NOT NULL DEFAULT '',
        likes INT NOT NULL DEFAULT 0,
        readers INT NOT NULL DEFAULT 0,
        comments INT NOT NULL DEFAULT 0,
        has_files TINYINT(1) NOT NULL DEFAULT 0,
        has_images TINYINT(1) NOT NULL DEFAULT 0,
        media_kind VARCHAR(32) NOT NULL DEFAULT 'text',
        media_summary MEDIUMTEXT NOT NULL,
        source VARCHAR(64) NOT NULL DEFAULT 'zsxq',
        updated_at VARCHAR(64) NOT NULL DEFAULT '',
        PRIMARY KEY (topic_id),
        KEY idx_message_topics_openid (_openid),
        KEY idx_message_topics_create_time (create_time),
        KEY idx_message_topics_media_kind (media_kind)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS message_events (
        _openid VARCHAR(128) NOT NULL DEFAULT 'watchtower',
        event_id VARCHAR(128) NOT NULL,
        topic_id VARCHAR(128) NOT NULL,
        title TEXT NOT NULL,
        summary MEDIUMTEXT NOT NULL,
        event_type VARCHAR(64) NOT NULL DEFAULT '',
        direction VARCHAR(32) NOT NULL DEFAULT '',
        confidence DOUBLE NOT NULL DEFAULT 0,
        impact_strength DOUBLE NOT NULL DEFAULT 0,
        valid_from VARCHAR(64) NOT NULL DEFAULT '',
        expires_at VARCHAR(64) NOT NULL DEFAULT '',
        keywords_json MEDIUMTEXT NOT NULL,
        updated_at VARCHAR(64) NOT NULL DEFAULT '',
        PRIMARY KEY (event_id),
        KEY idx_message_events_openid (_openid),
        KEY idx_message_events_topic_id (topic_id),
        KEY idx_message_events_valid_from (valid_from)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS message_event_links (
        _openid VARCHAR(128) NOT NULL DEFAULT 'watchtower',
        event_id VARCHAR(128) NOT NULL,
        entity_type VARCHAR(32) NOT NULL,
        code VARCHAR(128) NOT NULL DEFAULT '',
        name VARCHAR(255) NOT NULL DEFAULT '',
        role VARCHAR(128) NOT NULL DEFAULT '',
        relevance DOUBLE NOT NULL DEFAULT 0,
        impact DOUBLE NOT NULL DEFAULT 0,
        updated_at VARCHAR(64) NOT NULL DEFAULT '',
        PRIMARY KEY (event_id, entity_type, code, name),
        KEY idx_message_links_openid (_openid),
        KEY idx_message_links_entity (entity_type, code, name),
        KEY idx_message_links_entity_name_time (entity_type, name, updated_at, relevance),
        KEY idx_message_links_entity_code_time (entity_type, code, updated_at, relevance),
        KEY idx_message_links_event_id (event_id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS message_sync_runs (
        _openid VARCHAR(128) NOT NULL DEFAULT 'watchtower',
        run_id VARCHAR(160) NOT NULL,
        source VARCHAR(64) NOT NULL DEFAULT 'zsxq',
        started_at VARCHAR(64) NOT NULL DEFAULT '',
        finished_at VARCHAR(64) NOT NULL DEFAULT '',
        range_start VARCHAR(64) NOT NULL DEFAULT '',
        range_end VARCHAR(64) NOT NULL DEFAULT '',
        topic_count INT NOT NULL DEFAULT 0,
        event_count INT NOT NULL DEFAULT 0,
        link_count INT NOT NULL DEFAULT 0,
        upstream_latest_at VARCHAR(64) NOT NULL DEFAULT '',
        status VARCHAR(32) NOT NULL DEFAULT '',
        error TEXT NOT NULL,
        updated_at VARCHAR(64) NOT NULL DEFAULT '',
        PRIMARY KEY (run_id),
        KEY idx_message_sync_runs_openid (_openid),
        KEY idx_message_sync_runs_finished_at (finished_at)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS message_evidence_cache (
        _openid VARCHAR(128) NOT NULL DEFAULT 'watchtower',
        scope VARCHAR(16) NOT NULL,
        cache_key VARCHAR(255) NOT NULL,
        payload MEDIUMTEXT NOT NULL,
        built_at VARCHAR(64) NOT NULL DEFAULT '',
        updated_at VARCHAR(64) NOT NULL DEFAULT '',
        PRIMARY KEY (scope, cache_key),
        KEY idx_message_evidence_cache_openid (_openid)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
]


class MessageStoreError(RuntimeError):
    pass


class MessageStore:
    def __init__(
        self,
        _legacy_path: Any | None = None,
        *,
        env_id: str = "",
        token: str = "",
        instance: str = "default",
        schema: str = "",
        base_url: str | None = None,
        timeout: float = 5.0,
        cache_seconds: float = 15.0,
        openid: str = MESSAGE_OPENID,
        http_client: httpx.Client | None = None,
        async_refresh: bool = True,
        mysql_config: dict[str, Any] | None = None,
    ) -> None:
        self.env_id = str(env_id or "").strip()
        self.token = str(token or "").strip()
        self.instance = self._normalize_instance(instance)
        self.schema = str(schema or self.env_id or "").strip()
        self.openid = str(openid or MESSAGE_OPENID).strip() or MESSAGE_OPENID
        self.timeout = max(0.5, float(timeout or 5.0))
        self.cache_seconds = max(0.0, float(cache_seconds or 0.0))
        # 直连模式（pymysql → 阿里云 RDS）：传了 mysql_config 即启用，
        # 与 CloudBase REST 传输完全互斥，直连不使用 httpx client。
        self._mysql_config = dict(mysql_config) if mysql_config is not None else None
        self._mysql_pool: _MySqlConnectionPool | None = None
        self._mysql_pool_lock = threading.Lock()
        self._client = http_client
        # 未注入 client 时共享一个长连接 client：星球消息证据一次读取要点十几次
        # REST 查询，每次新建 client 会重复 TCP+TLS 握手，是详情接口慢的主因。
        self._shared_client: httpx.Client | None = None
        self._shared_client_lock = threading.Lock()
        # 读取并发池：外层证据任务（stock/sector 各词）走它，每个外层任务
        # 内部还会并发扇出叶子查询（in 批查、精确/模糊条件）。
        self._read_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="message-store-read")
        # 叶子查询专用池：只跑纯 REST 查询、绝不再嵌套提交任务。
        # 与外层池隔离是为了防死锁——若嵌套任务与外层任务同池，外层任务
        # 占满 worker 并阻塞等子任务时，子任务永远拿不到 worker。
        self._query_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="message-store-query")
        # like '%..%' 全表扫描单独限流，避免并发拖垮 MySQL 后集体超时。
        self._heavy_query_semaphore = threading.Semaphore(MESSAGE_STORE_HEAVY_QUERY_CONCURRENCY)
        # 物化证据重建走单 worker 后台队列：同步批次接连到来时串行消化，
        # 不与读路径抢连接。async_refresh=False 时（测试）退化为同步执行。
        self._async_refresh = bool(async_refresh)
        self._refresh_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="message-store-refresh")
        root = str(base_url or f"https://{self.env_id}.api.tcloudbasegateway.com").rstrip("/")
        self._base_url = f"{root}/v1/rdb/rest/{self.instance}/{self.schema}" if self.env_id and self.schema else ""
        self._legacy_path = _legacy_path
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self._cache_build_locks: dict[tuple[Any, ...], threading.Lock] = {}

    @classmethod
    def from_settings(cls, settings: Any) -> "MessageStore":
        backend = str(getattr(settings, "message_store_backend", "cloudbase_mysql") or "cloudbase_mysql")
        if backend.strip().lower() == "mysql":
            # pymysql 直连（阿里云 RDS）：构造后立即建表，失败抛错不静默降级。
            store = cls(
                cache_seconds=float(getattr(settings, "message_store_cache_seconds", 60.0) or 0.0),
                openid=str(getattr(settings, "cloudbase_mysql_openid", MESSAGE_OPENID) or MESSAGE_OPENID),
                mysql_config=dict(getattr(settings, "msg_mysql_config", None) or {}),
            )
            store.ensure_schema()
            return store
        return cls(
            env_id=str(getattr(settings, "cloudbase_env_id", "") or ""),
            token=str(getattr(settings, "cloudbase_api_token", "") or ""),
            instance=str(getattr(settings, "cloudbase_mysql_instance", "default") or "default"),
            schema=str(
                getattr(settings, "cloudbase_mysql_schema", "")
                or getattr(settings, "cloudbase_env_id", "")
                or ""
            ),
            base_url=str(getattr(settings, "cloudbase_api_base_url", "") or "") or None,
            timeout=float(getattr(settings, "cloudbase_api_timeout_seconds", 5.0) or 5.0),
            cache_seconds=float(getattr(settings, "message_store_cache_seconds", 60.0) or 0.0),
            openid=str(getattr(settings, "cloudbase_mysql_openid", MESSAGE_OPENID) or MESSAGE_OPENID),
        )

    @staticmethod
    def schema_statements() -> list[str]:
        return list(MYSQL_SCHEMA_STATEMENTS)

    @property
    def available(self) -> bool:
        if self._mysql_config is not None:
            cfg = self._mysql_config
            return bool(
                str(cfg.get("host") or "").strip()
                and str(cfg.get("user") or "").strip()
                and str(cfg.get("db") or "").strip()
            )
        return bool(self.env_id and self.token and self.instance and self.schema and self._base_url)

    @property
    def db_file(self) -> str:
        if self._mysql_config is not None:
            if not self.available:
                return "mysql://unconfigured"
            # 只暴露 host/db，密码不出现在状态信息里。
            return f"mysql://{str(self._mysql_config.get('host') or '').strip()}/{str(self._mysql_config.get('db') or '').strip()}"
        if not self.available:
            return "cloudbase_mysql://unconfigured"
        return f"cloudbase_mysql://{self.env_id}/{self.instance}/{self.schema}"

    def upsert_messages(self, payload: ZsxqMessageIngestRequest) -> ZsxqMessageIngestResponse:
        if not self.available:
            raise MessageStoreError("CloudBase MySQL message store is not configured")

        now = datetime.now().isoformat(timespec="seconds")
        source = payload.source or "zsxq"
        run_id = payload.run_id or f"{source}-{now}-{uuid.uuid4().hex[:8]}"
        started_at = payload.started_at or now
        finished_at = payload.finished_at or now
        topic_count = len(payload.topics) if payload.reported_topic_count is None else payload.reported_topic_count
        event_count = len(payload.events) if payload.reported_event_count is None else payload.reported_event_count
        link_count = len(payload.links) if payload.reported_link_count is None else payload.reported_link_count

        self._upsert_many("message_topics", [self._topic_row(topic, source, now) for topic in payload.topics])
        self._upsert_many("message_events", [self._event_row(event, now) for event in payload.events])
        self._upsert_many("message_event_links", [self._link_row(link, now) for link in payload.links])
        self._upsert_many(
            "message_sync_runs",
            [
                {
                    "_openid": self.openid,
                    "run_id": run_id,
                    "source": source,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "range_start": payload.start or "",
                    "range_end": payload.end or "",
                    "topic_count": int(topic_count or 0),
                    "event_count": int(event_count or 0),
                    "link_count": int(link_count or 0),
                    "upstream_latest_at": payload.upstream_latest_at or "",
                    "status": payload.status or "success",
                    "error": payload.error or "",
                    "updated_at": now,
                }
            ],
        )
        self._clear_cache()
        # 写入后异步预热状态缓存，避免下一次 /api/messages/status 冷启动空壳。
        self._refresh_status_async(("status", True), True)
        self._schedule_refresh(
            stock_codes=sorted(
                {
                    str(link.code or "").strip().zfill(6)
                    for link in payload.links
                    if str(link.entity_type or "").strip().lower() == "stock" and str(link.code or "").strip()
                }
            ),
            changed_sector_names=sorted(
                {
                    value
                    for link in payload.links
                    if str(link.entity_type or "").strip().lower() in {"sector", "theme"}
                    for value in (str(link.name or "").strip(), str(link.code or "").strip())
                    if value
                }
            ),
        )

        return ZsxqMessageIngestResponse(
            ok=True,
            source=source,
            run_id=run_id,
            topic_count=int(topic_count or 0),
            event_count=int(event_count or 0),
            link_count=int(link_count or 0),
        )

    def status(self, ingest_enabled: bool = False, *, wait: bool = False) -> MessageStoreStatus:
        """Stale-while-revalidate status.

        count=exact on 38 万行的 links 表经 REST 网关要 ~19s，而域名网关
        ~10s 就断连，同步计算会把 /api/messages/status 打成 000/全 0。
        默认后台线程刷新缓存：请求永远立即返回（冷启动先给空壳），
        网关超时不再影响接口可用性。传 wait=True 强制同步计算（测试/脚本用）。
        """
        key = ("status", bool(ingest_enabled))
        ttl = max(self.cache_seconds, MESSAGE_STATUS_CACHE_MIN_SECONDS)
        if wait:
            return self._cached(
                key,
                lambda: self._status_uncached(ingest_enabled=ingest_enabled),
                ttl=ttl,
            )
        now = monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None and now - cached[0] <= ttl:
            return self._clone(cached[1])
        self._refresh_status_async(key, ingest_enabled)
        if cached is not None:
            return self._clone(cached[1])
        return MessageStoreStatus(db_file=self.db_file, ingest_enabled=ingest_enabled)

    def _refresh_status_async(self, key: tuple[Any, ...], ingest_enabled: bool) -> None:
        with self._cache_lock:
            build_lock = self._cache_build_locks.setdefault(key, threading.Lock())
        if not build_lock.acquire(blocking=False):
            return

        def work() -> None:
            try:
                value = self._status_uncached(ingest_enabled=ingest_enabled)
                snapshot = self._clone(value)
                with self._cache_lock:
                    self._cache[key] = (monotonic(), snapshot)
            except Exception as exc:
                logger.warning("message status background refresh failed: %r", exc)
            finally:
                build_lock.release()

        threading.Thread(target=work, daemon=True).start()

    def _status_uncached(self, ingest_enabled: bool = False) -> MessageStoreStatus:
        if not self.available:
            return MessageStoreStatus(db_file=self.db_file, ingest_enabled=ingest_enabled)

        results = self._run_parallel(
            [
                ("topic_count", lambda: self._count("message_topics", "topic_id")),
                ("event_count", lambda: self._count("message_events", "event_id")),
                ("link_count", lambda: self._count("message_event_links", "event_id")),
                ("latest_topic_time", lambda: self._latest_value("message_topics", "create_time")),
                ("latest_event_time", lambda: self._latest_value("message_events", "valid_from")),
                (
                    "latest_run_rows",
                    lambda: self._query_rows(
                        "message_sync_runs",
                        params=[("select", "*"), ("order", "finished_at.desc"), ("limit", "1")],
                    ),
                ),
            ],
            leaf=True,
        )
        latest_run_rows = results.get("latest_run_rows") or []
        latest_run = self._sync_run_from_row(latest_run_rows[0]) if latest_run_rows else None

        return MessageStoreStatus(
            db_file=self.db_file,
            ingest_enabled=ingest_enabled,
            topic_count=results.get("topic_count") or 0,
            event_count=results.get("event_count") or 0,
            link_count=results.get("link_count") or 0,
            latest_topic_time=results.get("latest_topic_time") or "",
            latest_event_time=results.get("latest_event_time") or "",
            latest_run=latest_run,
        )

    def evidence_for(
        self,
        code: str,
        sector_terms: list[str] | None = None,
        stock_limit: int = 8,
        sector_limit: int = 8,
    ) -> MessageEvidenceBundle:
        normalized_code = str(code).zfill(6)
        normalized_terms = tuple(self._normalize_terms(sector_terms or []))
        return self._cached(
            ("evidence", normalized_code, normalized_terms, int(stock_limit), int(sector_limit)),
            lambda: self._evidence_for_uncached(
                normalized_code,
                list(normalized_terms),
                stock_limit=stock_limit,
                sector_limit=sector_limit,
            ),
        )

    def _evidence_for_uncached(
        self,
        code: str,
        sector_terms: list[str] | None = None,
        stock_limit: int = 8,
        sector_limit: int = 8,
    ) -> MessageEvidenceBundle:
        if not self.available:
            return MessageEvidenceBundle()

        # 物化缓存优先：stock 按代码、sector 按词各一次索引查询（可并行），
        # 命中时整个详情消息面板只需 1~2 次 REST 往返，亚秒返回。
        normalized_code = str(code).zfill(6)
        terms = self._normalize_terms(sector_terms or [])
        cached = self._best_effort(
            "evidence_cache_read",
            lambda: self._read_evidence_cache(normalized_code, terms),
            {},
        )

        stock_items: list[MessageEvidence] | None = None
        sector_by_term: dict[str, list[MessageEvidence]] = {}
        missing_terms: list[str] = []
        stock_row = cached.get(("stock", normalized_code))
        if stock_row is not None:
            stock_items = stock_row
        for term in terms:
            row = cached.get(("sector", term))
            if row is None:
                missing_terms.append(term)
            else:
                sector_by_term[term] = row

        if stock_items is None or missing_terms:
            # 未命中的键走动态计算（read-through），结果含空列表一并回写，
            # 保证任何股票/板块最多只慢一次。
            computed = self._compute_evidence_misses(
                normalized_code if stock_items is None else None,
                missing_terms,
                stock_limit=stock_limit,
                sector_limit=sector_limit,
            )
            if stock_items is None:
                stock_items = computed.get(("stock", normalized_code), [])
            for term in missing_terms:
                sector_by_term[term] = computed.get(("sector", term), [])

        return MessageEvidenceBundle(
            stock=(stock_items or [])[: max(1, stock_limit)],
            sector=self._merge_sector_terms(sector_by_term, terms, sector_limit),
        )

    def _compute_evidence_misses(
        self,
        stock_code: str | None,
        sector_terms: list[str],
        *,
        stock_limit: int = 8,
        sector_limit: int = 8,
    ) -> dict[tuple[str, str], list[MessageEvidence]]:
        def guarded(label: str, fn: Any) -> Any:
            def run() -> tuple[bool, list[MessageEvidence]]:
                try:
                    return True, fn()
                except Exception as exc:
                    # 计算失败只降级本次响应为空，绝不回写，避免一次抖动
                    # 把物化表里的好数据刷成空。
                    logger.warning("message evidence compute failed: %s error=%r", label, exc)
                    return False, []

            return run

        tasks: list[tuple[tuple[str, str], Any]] = []
        if stock_code:
            tasks.append(
                (
                    ("stock", stock_code),
                    guarded("stock_evidence", lambda: self._stock_evidence(stock_code, stock_limit)),
                )
            )
        for term in sector_terms:
            tasks.append(
                (
                    ("sector", term),
                    guarded(f"sector_evidence:{term}", lambda term=term: self._sector_evidence([term], sector_limit)),
                )
            )
        outcomes = self._run_parallel(tasks) if tasks else {}
        results = {key: items for key, (_, items) in outcomes.items()}
        now = datetime.now().isoformat(timespec="seconds")
        rows = [
            self._evidence_cache_row(scope, key, items, now)
            for (scope, key), (ok, items) in outcomes.items()
            if ok
        ]
        if rows:
            self._best_effort("evidence_cache_write", lambda: self._upsert_many(MESSAGE_EVIDENCE_CACHE_TABLE, rows), None)
        return results

    @staticmethod
    def _merge_sector_terms(
        sector_by_term: dict[str, list[MessageEvidence]],
        terms: list[str],
        limit: int,
    ) -> list[MessageEvidence]:
        """跨词合并：同一事件可能在多个板块词的缓存里各出现一次，按事件收敛，
        保留相关性最高（持平取影响最大）的一条，与动态路径的折叠语义一致。"""
        best_index_by_event: dict[str, int] = {}
        merged: list[MessageEvidence] = []
        for term in terms:
            for item in sector_by_term.get(term) or []:
                event_id = str(item.event_id or "")
                best_index = best_index_by_event.get(event_id)
                if best_index is None:
                    best_index_by_event[event_id] = len(merged)
                    merged.append(item)
                    continue
                best = merged[best_index]
                if (float(item.relevance or 0), float(item.impact or 0)) > (
                    float(best.relevance or 0),
                    float(best.impact or 0),
                ):
                    merged[best_index] = item
        merged.sort(
            key=lambda item: (
                str(item.create_time or ""),
                float(item.impact_strength or 0),
                float(item.confidence or 0),
            ),
            reverse=True,
        )
        return merged[: max(1, limit)]

    def _read_evidence_cache(
        self,
        code: str,
        terms: list[str],
    ) -> dict[tuple[str, str], list[MessageEvidence]]:
        tasks: list[tuple[str, Any]] = [
            (
                "stock",
                lambda: self._query_rows(
                    MESSAGE_EVIDENCE_CACHE_TABLE,
                    params=[("scope", "eq.stock"), ("cache_key", f"eq.{code}"), ("limit", "1")],
                ),
            )
        ]
        if terms:
            tasks.append(
                (
                    "sector",
                    lambda: self._query_rows(
                        MESSAGE_EVIDENCE_CACHE_TABLE,
                        params=[
                            ("scope", "eq.sector"),
                            ("cache_key", "in.(" + ",".join(terms) + ")"),
                        ],
                    ),
                )
            )
        results = self._run_parallel(tasks, leaf=True)
        output: dict[tuple[str, str], list[MessageEvidence]] = {}
        for rows in results.values():
            for row in rows or []:
                scope = str(row.get("scope") or "")
                key = str(row.get("cache_key") or "")
                if not scope or not key:
                    continue
                items = self._decode_evidence_payload(row.get("payload"))
                if items is None:
                    continue
                output[(scope, key)] = items
        return output

    @staticmethod
    def _decode_evidence_payload(raw: Any) -> list[MessageEvidence] | None:
        try:
            payload = json.loads(str(raw or "[]"))
            if not isinstance(payload, list):
                return None
            return [MessageEvidence.model_validate(item) for item in payload if isinstance(item, dict)]
        except Exception:
            return None

    def _evidence_cache_row(
        self,
        scope: str,
        key: str,
        items: list[MessageEvidence],
        now: str,
    ) -> dict[str, Any]:
        return {
            "_openid": self.openid,
            "scope": scope,
            "cache_key": key,
            "payload": json.dumps(
                [item.model_dump(mode="json") for item in items],
                ensure_ascii=False,
            ),
            "built_at": now,
            "updated_at": now,
        }

    def _schedule_refresh(
        self,
        stock_codes: list[str] | None = None,
        sector_terms: list[str] | None = None,
        changed_sector_names: list[str] | None = None,
    ) -> None:
        """同步写入后重建受影响实体的物化证据。默认后台串行执行，不拖慢 ingest 响应。"""
        if not self.available:
            return
        if not stock_codes and not sector_terms and not changed_sector_names:
            return

        def job() -> None:
            try:
                self.refresh_entities(
                    stock_codes=stock_codes,
                    sector_terms=sector_terms,
                    changed_sector_names=changed_sector_names,
                )
            except Exception as exc:
                logger.warning("message evidence refresh failed: error=%r", exc, exc_info=True)

        if self._async_refresh:
            self._refresh_pool.submit(job)
        else:
            job()

    def refresh_entities(
        self,
        stock_codes: list[str] | None = None,
        sector_terms: list[str] | None = None,
        changed_sector_names: list[str] | None = None,
    ) -> dict[str, int]:
        """重建指定股票/板块词的物化证据并回写缓存表。

        changed_sector_names 用于别名桥接：查询词（如申万显示名「电子化学品Ⅲ」）
        与链接名（「电子化学品」）常不一致，这里把已缓存查询词中与本次变更链接名
        互为子串的一并重建，保证后续读取持续命中。
        """
        if not self.available:
            return {"stock": 0, "sector": 0}

        codes = [str(code or "").strip().zfill(6) for code in stock_codes or [] if str(code or "").strip()]
        terms: list[str] = []
        seen_terms: set[str] = set()
        for value in [*(sector_terms or []), *(changed_sector_names or [])]:
            term = str(value or "").strip()
            if term and term not in seen_terms:
                seen_terms.add(term)
                terms.append(term)
        changed = [str(value or "").strip() for value in changed_sector_names or [] if str(value or "").strip()]
        if changed:
            for key in self._best_effort("sector_cache_keys", self._sector_cache_keys, []):
                if key in seen_terms:
                    continue
                if any(key in name or name in key for name in changed):
                    seen_terms.add(key)
                    terms.append(key)

        now = datetime.now().isoformat(timespec="seconds")

        def guarded(label: str, fn: Any) -> Any:
            def run() -> tuple[bool, list[MessageEvidence]]:
                try:
                    return True, fn()
                except Exception as exc:
                    logger.warning("message evidence refresh compute failed: %s error=%r", label, exc)
                    return False, []

            return run

        tasks: list[tuple[tuple[str, str], Any]] = [
            (
                ("stock", code),
                guarded(f"refresh_stock:{code}", lambda code=code: self._stock_evidence(code, 8)),
            )
            for code in dict.fromkeys(codes)
        ]
        tasks += [
            (
                ("sector", term),
                guarded(f"refresh_sector:{term}", lambda term=term: self._sector_evidence([term], 8)),
            )
            for term in terms
        ]
        outcomes = self._run_parallel(tasks) if tasks else {}
        # 只回写计算成功的键：失败保留旧物化值，避免把证据刷丢。
        rows = [
            self._evidence_cache_row(scope, key, items, now)
            for (scope, key), (ok, items) in outcomes.items()
            if ok
        ]
        if rows:
            self._upsert_many(MESSAGE_EVIDENCE_CACHE_TABLE, rows)
        # 物化值变了，进程内 60s 缓存里可能还有旧快照。
        self._clear_cache()
        return {
            "stock": sum(1 for scope, _ in outcomes if scope == "stock"),
            "sector": sum(1 for scope, _ in outcomes if scope == "sector"),
        }

    def _sector_cache_keys(self) -> list[str]:
        rows = self._query_rows(
            MESSAGE_EVIDENCE_CACHE_TABLE,
            params=[("select", "cache_key"), ("scope", "eq.sector"), ("limit", "1000")],
        )
        return [str(row.get("cache_key") or "") for row in rows if str(row.get("cache_key") or "").strip()]

    def evidence_cache_has_rows(self, scope: str = "sector") -> bool:
        """物化表是否已有数据（用于首次同步后的全量预建自举判断）。"""
        if not self.available:
            return False
        rows = self._best_effort(
            "evidence_cache_probe",
            lambda: self._query_rows(
                MESSAGE_EVIDENCE_CACHE_TABLE,
                params=[("select", "cache_key"), ("scope", f"eq.{scope}"), ("limit", "1")],
            ),
            [],
        )
        return bool(rows)

    def message_detail(
        self,
        event_id: str,
        ingest_enabled: bool = False,
    ) -> MessageDetailPayload | None:
        normalized = str(event_id or "").strip()
        if not normalized:
            return None
        return self._cached(
            ("detail", normalized, bool(ingest_enabled)),
            lambda: self._message_detail_uncached(normalized, ingest_enabled=ingest_enabled),
        )

    def _message_detail_uncached(
        self,
        event_id: str,
        ingest_enabled: bool = False,
    ) -> MessageDetailPayload | None:
        if not self.available:
            return None
        event_rows = self._query_rows(
            "message_events",
            params=[("event_id", f"eq.{event_id}"), ("limit", "1")],
        )
        if not event_rows:
            return None
        event_row = event_rows[0]
        topic_id = str(event_row.get("topic_id") or "")
        results = self._run_parallel(
            [
                ("topic_row", lambda: self._row_by_id("message_topics", "topic_id", topic_id)),
                (
                    "link_rows",
                    lambda: self._query_rows(
                        "message_event_links",
                        params=[("event_id", f"eq.{event_id}"), ("order", "relevance.desc")],
                    ),
                ),
                ("sync", lambda: self.status(ingest_enabled=ingest_enabled)),
            ]
        )
        topic_row = results.get("topic_row")
        if topic_row is None:
            return None
        link_rows = results.get("link_rows") or []
        link_rows.sort(
            key=lambda row: (
                -self._float(row.get("relevance")),
                -self._float(row.get("impact")),
                str(row.get("entity_type") or ""),
                str(row.get("code") or ""),
                str(row.get("name") or ""),
            )
        )
        return MessageDetailPayload(
            topic=self._topic_from_row(topic_row),
            event=self._event_from_row(event_row),
            links=[self._link_from_row(row) for row in link_rows],
            sync=results.get("sync") or self.status(ingest_enabled=ingest_enabled),
        )

    def _cached(self, key: tuple[Any, ...], loader: Any, ttl: float | None = None) -> Any:
        effective_ttl = self.cache_seconds if ttl is None else max(0.0, float(ttl))
        if effective_ttl <= 0:
            return loader()

        now = monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] <= effective_ttl:
                return self._clone(cached[1])
            build_lock = self._cache_build_locks.setdefault(key, threading.Lock())

        with build_lock:
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached is not None and monotonic() - cached[0] <= effective_ttl:
                    return self._clone(cached[1])
            value = loader()
            snapshot = self._clone(value)
            with self._cache_lock:
                self._cache[key] = (monotonic(), snapshot)
                while len(self._cache) > 128:
                    oldest_key = min(self._cache, key=lambda item: self._cache[item][0])
                    self._cache.pop(oldest_key, None)
                    self._cache_build_locks.pop(oldest_key, None)
            return self._clone(snapshot)

    def _clear_cache(self) -> None:
        with self._cache_lock:
            self._cache = {}
            self._cache_build_locks = {}

    @staticmethod
    def _clone(value: Any) -> Any:
        if hasattr(value, "model_copy"):
            return value.model_copy(deep=True)
        return copy.deepcopy(value)

    def _stock_evidence(self, code: str, limit: int) -> list[MessageEvidence]:
        if not code:
            return []
        candidate_limit = max(MESSAGE_EVIDENCE_STOCK_CANDIDATE_LIMIT, int(limit) * 15)
        rows = self._query_rows(
            "message_event_links",
            params=[
                ("entity_type", "eq.stock"),
                ("code", f"eq.{code}"),
                ("order", "updated_at.desc,relevance.desc"),
                ("limit", str(candidate_limit)),
            ],
        )
        return self._evidence_for_links(rows, "stock", limit)

    def _sector_evidence(self, terms: list[str], limit: int) -> list[MessageEvidence]:
        normalized_terms = self._normalize_terms(terms)
        if not normalized_terms:
            return []

        candidate_limit = max(MESSAGE_EVIDENCE_SECTOR_CANDIDATE_LIMIT, int(limit) * 10)
        fuzzy_limit = max(MESSAGE_EVIDENCE_SECTOR_FUZZY_LIMIT, int(limit) * 5)

        # 每个 term 拆成精确条件（name 全等 / code slug 全等）和模糊条件（like %..%）。
        # like '%关键词%' 在 37 万行的 links 表上走全表扫描，是接口慢的重头；
        # 先并发跑精确条件，只有精确条件一条都没命中的 term 才补模糊扫描。
        exact_filters: list[list[tuple[str, str, int]]] = []
        fuzzy_filters: list[list[tuple[str, str, int]]] = []
        for term in normalized_terms:
            slug = self._slug_term(term)
            exact: list[tuple[str, str, int]] = [("name", f"eq.{term}", candidate_limit)]
            if slug:
                exact.append(("code", f"eq.{slug}", candidate_limit))
            fuzzy: list[tuple[str, str, int]] = []
            if self._should_use_fuzzy_term(term):
                fuzzy.append(("name", f"like.%{term}%", fuzzy_limit))
                if slug:
                    fuzzy.append(("code", f"like.%{slug}%", fuzzy_limit))
            exact_filters.append(exact)
            fuzzy_filters.append(fuzzy)

        def run_query(field: str, expression: str, row_limit: int) -> list[dict[str, Any]]:
            def query() -> list[dict[str, Any]]:
                return self._query_rows(
                    "message_event_links",
                    params=[
                        ("entity_type", "in.(sector,theme)"),
                        (field, expression),
                        ("order", "updated_at.desc,relevance.desc"),
                        ("limit", str(row_limit)),
                    ],
                )

            if expression.startswith("like."):
                # 全表扫描给足超时（独立 10s），并经信号量限流串行执行。
                return self._best_effort(
                    f"sector_fuzzy:{field}",
                    lambda: self._heavy_query(
                        lambda: self._query_rows(
                            "message_event_links",
                            params=[
                                ("entity_type", "in.(sector,theme)"),
                                (field, expression),
                                ("order", "updated_at.desc,relevance.desc"),
                                ("limit", str(row_limit)),
                            ],
                            request_timeout=10.0,
                        )
                    ),
                    [],
                )
            return self._best_effort(f"sector_exact:{field}", query, [])

        exact_tasks = [
            ((term_index, filter_index), lambda f=field, e=expression, r=row_limit: run_query(f, e, r))
            for term_index, filters in enumerate(exact_filters)
            for filter_index, (field, expression, row_limit) in enumerate(filters)
        ]
        exact_results = self._run_parallel(exact_tasks, leaf=True)

        fuzzy_tasks = []
        for term_index, filters in enumerate(fuzzy_filters):
            if not filters:
                continue
            has_exact_hit = any(
                exact_results.get((term_index, filter_index))
                for filter_index in range(len(exact_filters[term_index]))
            )
            if has_exact_hit:
                continue
            for filter_index, (field, expression, row_limit) in enumerate(filters):
                fuzzy_tasks.append(
                    ((term_index, filter_index), lambda f=field, e=expression, r=row_limit: run_query(f, e, r))
                )
        fuzzy_results = self._run_parallel(fuzzy_tasks, leaf=True)

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for term_index in range(len(normalized_terms)):
            for result_set, filters in (
                (exact_results, exact_filters[term_index]),
                (fuzzy_results, fuzzy_filters[term_index]),
            ):
                for filter_index in range(len(filters)):
                    for row in result_set.get((term_index, filter_index)) or []:
                        entity_type = str(row.get("entity_type") or "").strip().lower()
                        if entity_type not in {"sector", "theme"}:
                            continue
                        key = self._link_key(row)
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append(row)
        return self._evidence_for_links(rows, "sector", limit)

    def _evidence_for_links(
        self,
        links: list[dict[str, Any]],
        match_scope: str,
        limit: int,
    ) -> list[MessageEvidence]:
        if not links:
            return []
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for row in links:
            key = self._link_key(row)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        # 同一事件常同时挂多级板块链接（光纤光缆/通信设备/通信），逐链接展示
        # 会在前端刷屏。按事件收敛为一条，保留相关性最高（持平取影响最大）的链接。
        best_index_by_event: dict[str, int] = {}
        collapsed: list[dict[str, Any]] = []
        for row in deduped:
            event_id = str(row.get("event_id") or "")
            best_index = best_index_by_event.get(event_id)
            if best_index is None:
                best_index_by_event[event_id] = len(collapsed)
                collapsed.append(row)
                continue
            best = collapsed[best_index]
            if (
                self._float(row.get("relevance")),
                self._float(row.get("impact")),
            ) > (
                self._float(best.get("relevance")),
                self._float(best.get("impact")),
            ):
                collapsed[best_index] = row
        deduped = collapsed

        event_by_id = self._rows_by_ids(
            "message_events",
            "event_id",
            [str(row.get("event_id") or "") for row in deduped],
        )
        topic_by_id = self._rows_by_ids(
            "message_topics",
            "topic_id",
            [str(event.get("topic_id") or "") for event in event_by_id.values()],
        )

        evidence: list[MessageEvidence] = []
        for link in deduped:
            event = event_by_id.get(str(link.get("event_id") or ""))
            if not event:
                continue
            topic = topic_by_id.get(str(event.get("topic_id") or ""))
            if not topic:
                continue
            evidence.append(self._evidence_from_rows(topic, event, link, match_scope))

        evidence.sort(
            key=lambda item: (
                str(item.create_time or ""),
                float(item.impact_strength or 0),
                float(item.confidence or 0),
            ),
            reverse=True,
        )
        return evidence[: max(1, limit)]

    def _upsert_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        chunk_size = max(1, MESSAGE_STORE_UPSERT_CHUNK_SIZE)
        if self._mysql_config is not None:
            for offset in range(0, len(rows), chunk_size):
                sql, args = _build_upsert_sql(table, rows[offset : offset + chunk_size])
                self._mysql_execute(sql, args)
            return
        mutation_timeout = max(float(self.timeout), MESSAGE_STORE_MUTATION_MIN_TIMEOUT)
        for offset in range(0, len(rows), chunk_size):
            chunk = rows[offset : offset + chunk_size]
            response = self._request(
                "POST",
                self._table_url(table),
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                json=chunk,
                timeout=mutation_timeout,
            )
            if response.status_code >= 400:
                raise MessageStoreError(self._error_message(response))

    def _query_rows(
        self,
        table: str,
        *,
        params: Iterable[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        if self._mysql_config is not None:
            # 直连没有网关超时问题，request_timeout 忽略。
            sql, args = _build_select_sql(table, list(params or []))
            return self._mysql_query(sql, args)
        extra: dict[str, Any] = {}
        if request_timeout is not None:
            extra["timeout"] = max(0.5, float(request_timeout))
        response = self._request("GET", self._table_url(table), params=list(params or []), headers=headers, **extra)
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise MessageStoreError(self._error_message(response))
        payload = response.json()
        if isinstance(payload, list):
            return [dict(row) for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            return [payload]
        return []

    def _count(self, table: str, key_field: str) -> int:
        if self._mysql_config is not None:
            rows = self._mysql_query(_build_count_sql(table), [])
            return int(rows[0].get("cnt") or 0) if rows else 0
        response = self._request(
            "GET",
            self._table_url(table),
            params=[("select", key_field), ("limit", "1")],
            headers={"Prefer": "count=exact"},
        )
        if response.status_code == 404:
            return 0
        if response.status_code >= 400:
            raise MessageStoreError(self._error_message(response))
        content_range = str(response.headers.get("Content-Range") or response.headers.get("content-range") or "")
        return self._parse_count(content_range, response)

    def _latest_value(self, table: str, field: str) -> str:
        rows = self._query_rows(
            table,
            params=[("select", field), ("order", f"{field}.desc"), ("limit", "1")],
        )
        return str(rows[0].get(field) or "") if rows else ""

    def _row_by_id(self, table: str, field: str, value: str) -> dict[str, Any] | None:
        if not value:
            return None
        rows = self._query_rows(table, params=[(field, f"eq.{value}"), ("limit", "1")])
        return rows[0] if rows else None

    def _rows_by_ids(self, table: str, field: str, values: Iterable[str]) -> dict[str, dict[str, Any]]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = str(value or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            cleaned.append(item)
        chunks = list(self._chunks(cleaned, 40))
        results = self._run_parallel(
            [
                (
                    index,
                    lambda chunk=chunk: self._best_effort(
                        f"rows_by_ids:{table}",
                        lambda: self._query_rows(
                            table,
                            params=[(field, "in.(" + ",".join(chunk) + ")")],
                        ),
                        [],
                    ),
                )
                for index, chunk in enumerate(chunks)
            ],
            leaf=True,
        )
        output: dict[str, dict[str, Any]] = {}
        for index in range(len(chunks)):
            for row in results.get(index) or []:
                key = str(row.get(field) or "")
                if key:
                    output[key] = row
        return output

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        with self._shared_client_lock:
            if self._shared_client is None:
                self._shared_client = httpx.Client(
                    timeout=self.timeout,
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                )
            return self._shared_client

    def _mysql_conn_kwargs(self) -> dict[str, Any]:
        cfg = self._mysql_config or {}
        return {
            "host": str(cfg.get("host") or "").strip(),
            "port": int(cfg.get("port") or 3306),
            "user": str(cfg.get("user") or "").strip(),
            "password": str(cfg.get("pwd") or cfg.get("password") or ""),
            "database": str(cfg.get("db") or "").strip(),
            "charset": "utf8mb4",
            "autocommit": True,
            "connect_timeout": 5,
        }

    def _get_mysql_pool(self) -> _MySqlConnectionPool:
        if self._mysql_config is None:
            raise MessageStoreError("mysql message store is not configured")
        with self._mysql_pool_lock:
            if self._mysql_pool is None:
                self._mysql_pool = _MySqlConnectionPool(self._mysql_conn_kwargs())
            return self._mysql_pool

    def _mysql_query(self, sql: str, args: list[Any]) -> list[dict[str, Any]]:
        import pymysql.cursors

        pool = self._get_mysql_pool()
        conn = pool.acquire()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                # 空参数传 None：pymysql 对非 None args 会做 % 格式化，
                # 会把 SQL 里的字面 % 当占位符报错。
                cur.execute(sql, args if args else None)
                return [dict(row) for row in cur.fetchall()]
        except MessageStoreError:
            raise
        except Exception as exc:
            raise MessageStoreError(f"mysql message store query failed: {exc}") from exc
        finally:
            pool.release(conn)

    def _mysql_execute(self, sql: str, args: list[Any]) -> None:
        pool = self._get_mysql_pool()
        conn = pool.acquire()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args if args else None)
        except MessageStoreError:
            raise
        except Exception as exc:
            raise MessageStoreError(f"mysql message store write failed: {exc}") from exc
        finally:
            pool.release(conn)

    def ensure_schema(self) -> None:
        """直连模式建表：逐条执行 CREATE TABLE IF NOT EXISTS（RDS 是普通 MySQL 8.0，
        没有 CloudBase 的 _openid 特殊处理，直接建即可）。失败抛错，不静默吞。"""
        if self._mysql_config is None:
            raise MessageStoreError("ensure_schema is only available in mysql backend mode")
        for statement in self.schema_statements():
            self._mysql_execute(statement, [])

    def close(self) -> None:
        with self._shared_client_lock:
            if self._shared_client is not None:
                self._shared_client.close()
                self._shared_client = None
        with self._mysql_pool_lock:
            if self._mysql_pool is not None:
                self._mysql_pool.close()
                self._mysql_pool = None
        self._read_pool.shutdown(wait=False, cancel_futures=True)
        self._query_pool.shutdown(wait=False, cancel_futures=True)
        self._refresh_pool.shutdown(wait=False, cancel_futures=True)

    def _run_parallel(self, tasks: Iterable[tuple[Any, Any]], leaf: bool = False) -> dict[Any, Any]:
        """并发执行只读小任务，按任务标签返回结果，保持调用方组装顺序。

        leaf=True 表示任务是不再嵌套提交子任务的纯查询，走独立叶子池，
        避免与外层任务同池互等造成死锁。
        """
        task_list = list(tasks)
        if not task_list:
            return {}
        if len(task_list) == 1:
            key, fn = task_list[0]
            return {key: fn()}
        pool = self._query_pool if leaf else self._read_pool
        futures = {pool.submit(fn): key for key, fn in task_list}
        return {key: futures_future.result() for futures_future, key in futures.items()}

    def _best_effort(self, label: str, fn: Any, fallback: Any) -> Any:
        """证据类查询的容错包装：单条查询失败降级为空结果，不拖垮整个面板。"""
        try:
            return fn()
        except Exception as exc:
            logger.warning("message store best-effort query failed: %s error=%r", label, exc)
            return fallback

    def _heavy_query(self, fn: Any) -> Any:
        """全表扫描类查询（like '%..%'）走信号量限流。"""
        with self._heavy_query_semaphore:
            return fn()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(kwargs.pop("headers", {}) or {})
        normalized_method = method.upper()
        attempts = (
            MESSAGE_STORE_GET_RETRIES
            if normalized_method == "GET"
            else MESSAGE_STORE_MUTATION_RETRIES
            if normalized_method == "POST"
            else 1
        )
        last_response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._get_client().request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                # 覆盖 ConnectError/PoolTimeout/RemoteProtocolError（长
                # 连接被网关半开关闭）等传输层错误，读请求重试是安全的。
                # 退避放宽到 0.4/0.8s：TDSQL serverless 实例抖动恢复需要秒级。
                last_error = exc
                if isinstance(exc, httpx.ReadTimeout) and attempt >= 1:
                    # 读超时说明查询本身慢或服务端过载，多次重试只会把
                    # 单次 5s 超时放大成 16s+ 并继续压垮 DB；最多给一次机会。
                    raise
                if attempt + 1 >= attempts:
                    raise
                sleep(0.4 * (attempt + 1))
                continue

            if self._is_retryable_response(response) and attempt + 1 < attempts:
                last_response = response
                response.close()
                sleep(0.4 * (attempt + 1))
                continue
            return response

        if last_response is not None:
            return last_response
        if last_error is not None:
            raise last_error
        raise RuntimeError("request retry loop exited unexpectedly")

    def _table_url(self, table: str) -> str:
        return f"{self._base_url}/{table}"

    @staticmethod
    def _is_retryable_response(response: httpx.Response) -> bool:
        if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
            return True
        if response.status_code != 400:
            return False
        message = ""
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "")
        if not message:
            try:
                message = response.text
            except Exception:
                message = ""
        lowered = message.lower()
        return (
            "connect: connection refused" in lowered
            or "invalid connection" in lowered
            or "readtimeout" in lowered
            or "timeout" in lowered
        )

    @staticmethod
    def _should_use_fuzzy_term(term: str) -> bool:
        cleaned = str(term or "").strip()
        if not cleaned:
            return False
        if cleaned.isascii():
            return len(cleaned) >= 3
        return len(cleaned) >= 4

    def _topic_row(self, topic: MessageTopic, source: str, now: str) -> dict[str, Any]:
        return {
            "_openid": self.openid,
            "topic_id": str(topic.topic_id or ""),
            "title": str(topic.title or ""),
            "content": str(topic.content or ""),
            "create_time": str(topic.create_time or ""),
            "owner_name": str(topic.owner_name or ""),
            "likes": int(topic.likes or 0),
            "readers": int(topic.readers or 0),
            "comments": int(topic.comments or 0),
            "has_files": 1 if topic.has_files else 0,
            "has_images": 1 if topic.has_images else 0,
            "media_kind": str(topic.media_kind or "text"),
            "media_summary": str(topic.media_summary or ""),
            "source": str(topic.source or source or "zsxq"),
            "updated_at": now,
        }

    def _event_row(self, event: MessageEvent, now: str) -> dict[str, Any]:
        return {
            "_openid": self.openid,
            "event_id": str(event.event_id or ""),
            "topic_id": str(event.topic_id or ""),
            "title": str(event.title or ""),
            "summary": str(event.summary or ""),
            "event_type": str(event.event_type or ""),
            "direction": "" if event.direction is None else str(event.direction),
            "confidence": float(event.confidence or 0),
            "impact_strength": float(event.impact_strength or 0),
            "valid_from": str(event.valid_from or ""),
            "expires_at": str(event.expires_at or ""),
            "keywords_json": json.dumps(event.keywords, ensure_ascii=False),
            "updated_at": now,
        }

    def _link_row(self, link: MessageEventLink, now: str) -> dict[str, Any]:
        return {
            "_openid": self.openid,
            "event_id": str(link.event_id or ""),
            "entity_type": str(link.entity_type or "").strip().lower(),
            "code": str(link.code or "").strip(),
            "name": str(link.name or "").strip(),
            "role": str(link.role or ""),
            "relevance": float(link.relevance or 0),
            "impact": float(link.impact or 0),
            "updated_at": now,
        }

    def _topic_from_row(self, row: dict[str, Any]) -> MessageTopic:
        return MessageTopic(
            topic_id=str(row.get("topic_id") or ""),
            title=str(row.get("title") or ""),
            content=str(row.get("content") or ""),
            create_time=str(row.get("create_time") or ""),
            owner_name=str(row.get("owner_name") or ""),
            likes=self._int(row.get("likes")),
            readers=self._int(row.get("readers")),
            comments=self._int(row.get("comments")),
            has_files=self._bool(row.get("has_files")),
            has_images=self._bool(row.get("has_images")),
            media_kind=str(row.get("media_kind") or "text"),
            media_summary=str(row.get("media_summary") or ""),
            source=str(row.get("source") or "zsxq"),
        )

    def _event_from_row(self, row: dict[str, Any]) -> MessageEvent:
        return MessageEvent(
            event_id=str(row.get("event_id") or ""),
            topic_id=str(row.get("topic_id") or ""),
            title=str(row.get("title") or ""),
            summary=str(row.get("summary") or ""),
            event_type=str(row.get("event_type") or ""),
            direction=str(row.get("direction") or ""),
            confidence=self._float(row.get("confidence")),
            impact_strength=self._float(row.get("impact_strength")),
            valid_from=str(row.get("valid_from") or ""),
            expires_at=str(row.get("expires_at") or ""),
            keywords=self._json_list(row.get("keywords_json")),
        )

    def _link_from_row(self, row: dict[str, Any]) -> MessageEventLink:
        return MessageEventLink(
            event_id=str(row.get("event_id") or ""),
            entity_type=str(row.get("entity_type") or ""),
            code=str(row.get("code") or ""),
            name=str(row.get("name") or ""),
            role=str(row.get("role") or ""),
            relevance=self._float(row.get("relevance")),
            impact=self._float(row.get("impact")),
        )

    def _evidence_from_rows(
        self,
        topic: dict[str, Any],
        event: dict[str, Any],
        link: dict[str, Any],
        match_scope: str,
    ) -> MessageEvidence:
        return MessageEvidence(
            source=str(topic.get("source") or "zsxq"),
            topic_id=str(topic.get("topic_id") or ""),
            topic_title=str(topic.get("title") or ""),
            topic_content=self._trim(topic.get("content"), 600),
            display_text=self._trim(self._display_text(topic, event), 600),
            create_time=str(topic.get("create_time") or ""),
            owner_name=str(topic.get("owner_name") or ""),
            likes=self._int(topic.get("likes")),
            readers=self._int(topic.get("readers")),
            comments=self._int(topic.get("comments")),
            has_files=self._bool(topic.get("has_files")),
            has_images=self._bool(topic.get("has_images")),
            media_summary=self._trim(topic.get("media_summary"), 600),
            event_id=str(event.get("event_id") or ""),
            event_title=str(event.get("title") or ""),
            event_summary=self._trim(event.get("summary"), 600),
            event_type=str(event.get("event_type") or ""),
            direction=str(event.get("direction") or ""),
            confidence=self._float(event.get("confidence")),
            impact_strength=self._float(event.get("impact_strength")),
            valid_from=str(event.get("valid_from") or ""),
            expires_at=str(event.get("expires_at") or ""),
            keywords=self._json_list(event.get("keywords_json")),
            entity_type=str(link.get("entity_type") or ""),
            code=str(link.get("code") or ""),
            name=str(link.get("name") or ""),
            role=str(link.get("role") or ""),
            relevance=self._float(link.get("relevance")),
            impact=self._float(link.get("impact")),
            match_scope=match_scope,
        )

    def _sync_run_from_row(self, row: dict[str, Any]) -> MessageSyncRunStatus:
        return MessageSyncRunStatus(
            run_id=str(row.get("run_id") or ""),
            source=str(row.get("source") or ""),
            started_at=str(row.get("started_at") or ""),
            finished_at=str(row.get("finished_at") or ""),
            start=str(row.get("range_start") or ""),
            end=str(row.get("range_end") or ""),
            topic_count=self._int(row.get("topic_count")),
            event_count=self._int(row.get("event_count")),
            link_count=self._int(row.get("link_count")),
            upstream_latest_at=str(row.get("upstream_latest_at") or ""),
            status=str(row.get("status") or ""),
            error=str(row.get("error") or ""),
        )

    def _parse_count(self, content_range: str, response: httpx.Response) -> int:
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                return int(tail)
        payload = response.json()
        return len(payload) if isinstance(payload, list) else 0

    def _error_message(self, response: httpx.Response) -> str:
        try:
            payload: Any = response.json()
        except Exception:
            payload = response.text
        return f"CloudBase MySQL message store request failed: {response.status_code} {payload}"

    def _link_key(self, row: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(row.get("event_id") or ""),
            str(row.get("entity_type") or "").strip().lower(),
            str(row.get("code") or "").strip(),
            str(row.get("name") or "").strip(),
        )

    def _display_text(self, topic: dict[str, Any], event: dict[str, Any]) -> str:
        for value in (
            topic.get("media_summary"),
            event.get("summary"),
            topic.get("content"),
            topic.get("title"),
        ):
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _normalize_terms(self, terms: list[str]) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for term in terms:
            value = str(term or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
        return values

    def _slug_term(self, term: str) -> str:
        value = re.sub(r"[^0-9A-Za-z_]+", "_", term).strip("_").lower()
        return re.sub(r"_+", "_", value)

    def _json_list(self, value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        try:
            parsed = json.loads(str(value))
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()]

    @staticmethod
    def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
        for index in range(0, len(values), max(1, size)):
            yield values[index : index + max(1, size)]

    @staticmethod
    def _normalize_instance(value: str) -> str:
        normalized = str(value or "default").strip() or "default"
        return "default" if normalized == "(default)" else normalized

    @staticmethod
    def _trim(value: Any, limit: int) -> str:
        return str(value or "")[: max(1, int(limit or 1))]

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value or "").strip().lower() in {"1", "true", "yes"}
