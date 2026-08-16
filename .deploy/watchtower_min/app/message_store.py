from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from collections.abc import Iterable
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
MESSAGE_EVIDENCE_STOCK_CANDIDATE_LIMIT = 120
MESSAGE_EVIDENCE_SECTOR_CANDIDATE_LIMIT = 80
MESSAGE_EVIDENCE_SECTOR_FUZZY_LIMIT = 40

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
    ) -> None:
        self.env_id = str(env_id or "").strip()
        self.token = str(token or "").strip()
        self.instance = self._normalize_instance(instance)
        self.schema = str(schema or self.env_id or "").strip()
        self.openid = str(openid or MESSAGE_OPENID).strip() or MESSAGE_OPENID
        self.timeout = max(0.5, float(timeout or 5.0))
        self.cache_seconds = max(0.0, float(cache_seconds or 0.0))
        self._client = http_client
        root = str(base_url or f"https://{self.env_id}.api.tcloudbasegateway.com").rstrip("/")
        self._base_url = f"{root}/v1/rdb/rest/{self.instance}/{self.schema}" if self.env_id and self.schema else ""
        self._legacy_path = _legacy_path
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self._cache_build_locks: dict[tuple[Any, ...], threading.Lock] = {}

    @classmethod
    def from_settings(cls, settings: Any) -> "MessageStore":
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
            cache_seconds=float(getattr(settings, "message_store_cache_seconds", 15.0) or 0.0),
            openid=str(getattr(settings, "cloudbase_mysql_openid", MESSAGE_OPENID) or MESSAGE_OPENID),
        )

    @staticmethod
    def schema_statements() -> list[str]:
        return list(MYSQL_SCHEMA_STATEMENTS)

    @property
    def available(self) -> bool:
        return bool(self.env_id and self.token and self.instance and self.schema and self._base_url)

    @property
    def db_file(self) -> str:
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

        return ZsxqMessageIngestResponse(
            ok=True,
            source=source,
            run_id=run_id,
            topic_count=int(topic_count or 0),
            event_count=int(event_count or 0),
            link_count=int(link_count or 0),
        )

    def status(self, ingest_enabled: bool = False) -> MessageStoreStatus:
        return self._cached(
            ("status", bool(ingest_enabled)),
            lambda: self._status_uncached(ingest_enabled=ingest_enabled),
        )

    def _status_uncached(self, ingest_enabled: bool = False) -> MessageStoreStatus:
        if not self.available:
            return MessageStoreStatus(db_file=self.db_file, ingest_enabled=ingest_enabled)

        topic_count = self._count("message_topics", "topic_id")
        event_count = self._count("message_events", "event_id")
        link_count = self._count("message_event_links", "event_id")
        latest_topic_time = self._latest_value("message_topics", "create_time")
        latest_event_time = self._latest_value("message_events", "valid_from")
        latest_run_rows = self._query_rows(
            "message_sync_runs",
            params=[("select", "*"), ("order", "finished_at.desc"), ("limit", "1")],
        )
        latest_run = self._sync_run_from_row(latest_run_rows[0]) if latest_run_rows else None

        return MessageStoreStatus(
            db_file=self.db_file,
            ingest_enabled=ingest_enabled,
            topic_count=topic_count,
            event_count=event_count,
            link_count=link_count,
            latest_topic_time=latest_topic_time,
            latest_event_time=latest_event_time,
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
        stock = self._stock_evidence(str(code).zfill(6), stock_limit)
        sector = self._sector_evidence(sector_terms or [], sector_limit)
        return MessageEvidenceBundle(stock=stock, sector=sector)

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
        topic_row = self._row_by_id("message_topics", "topic_id", topic_id)
        if topic_row is None:
            return None
        link_rows = self._query_rows(
            "message_event_links",
            params=[("event_id", f"eq.{event_id}"), ("order", "relevance.desc")],
        )
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
            sync=self.status(ingest_enabled=ingest_enabled),
        )

    def _cached(self, key: tuple[Any, ...], loader: Any) -> Any:
        if self.cache_seconds <= 0:
            return loader()

        now = monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] <= self.cache_seconds:
                return self._clone(cached[1])
            build_lock = self._cache_build_locks.setdefault(key, threading.Lock())

        with build_lock:
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached is not None and monotonic() - cached[0] <= self.cache_seconds:
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

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        candidate_limit = max(MESSAGE_EVIDENCE_SECTOR_CANDIDATE_LIMIT, int(limit) * 10)
        for term in normalized_terms:
            filters = [("name", f"eq.{term}")]
            slug = self._slug_term(term)
            if slug:
                filters.append(("code", f"eq.{slug}"))
            if self._should_use_fuzzy_term(term):
                filters.extend([("name", f"like.%{term}%")])
                if slug:
                    filters.append(("code", f"like.%{slug}%"))
            for field, expression in filters:
                row_limit = (
                    max(MESSAGE_EVIDENCE_SECTOR_FUZZY_LIMIT, int(limit) * 5)
                    if expression.startswith("like.")
                    else candidate_limit
                )
                for row in self._query_rows(
                    "message_event_links",
                    params=[
                        ("entity_type", "in.(sector,theme)"),
                        (field, expression),
                        ("order", "updated_at.desc,relevance.desc"),
                        ("limit", str(row_limit)),
                    ],
                ):
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
        response = self._request(
            "POST",
            self._table_url(table),
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=rows,
        )
        if response.status_code >= 400:
            raise MessageStoreError(self._error_message(response))

    def _query_rows(
        self,
        table: str,
        *,
        params: Iterable[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        response = self._request("GET", self._table_url(table), params=list(params or []), headers=headers)
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
        output: dict[str, dict[str, Any]] = {}
        for chunk in self._chunks(cleaned, 40):
            expression = "in.(" + ",".join(chunk) + ")"
            for row in self._query_rows(table, params=[(field, expression)]):
                key = str(row.get(field) or "")
                if key:
                    output[key] = row
        return output

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
                if self._client is not None:
                    response = self._client.request(method, url, headers=headers, **kwargs)
                else:
                    with httpx.Client(timeout=self.timeout) as client:
                        response = client.request(method, url, headers=headers, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
                sleep(0.15 * (attempt + 1))
                continue

            if self._is_retryable_response(response) and attempt + 1 < attempts:
                last_response = response
                response.close()
                sleep(0.15 * (attempt + 1))
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
        return "connect: connection refused" in lowered or "readtimeout" in lowered or "timeout" in lowered

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
