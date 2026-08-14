from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

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


class MessageStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def upsert_messages(self, payload: ZsxqMessageIngestRequest) -> ZsxqMessageIngestResponse:
        now = datetime.now().isoformat(timespec="seconds")
        source = payload.source or "zsxq"
        run_id = payload.run_id or f"{source}-{now}-{uuid.uuid4().hex[:8]}"
        started_at = payload.started_at or now
        finished_at = payload.finished_at or now
        topic_count = len(payload.topics) if payload.reported_topic_count is None else payload.reported_topic_count
        event_count = len(payload.events) if payload.reported_event_count is None else payload.reported_event_count
        link_count = len(payload.links) if payload.reported_link_count is None else payload.reported_link_count

        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for topic in payload.topics:
                conn.execute(
                    """
                    INSERT INTO message_topics (
                        topic_id, title, content, create_time, owner_name, likes, readers,
                        comments, has_files, has_images, media_summary, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(topic_id) DO UPDATE SET
                        title = excluded.title,
                        content = excluded.content,
                        create_time = excluded.create_time,
                        owner_name = excluded.owner_name,
                        likes = excluded.likes,
                        readers = excluded.readers,
                        comments = excluded.comments,
                        has_files = excluded.has_files,
                        has_images = excluded.has_images,
                        media_summary = excluded.media_summary,
                        source = excluded.source,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        topic.topic_id,
                        topic.title,
                        topic.content,
                        topic.create_time,
                        topic.owner_name,
                        int(topic.likes or 0),
                        int(topic.readers or 0),
                        int(topic.comments or 0),
                        1 if topic.has_files else 0,
                        1 if topic.has_images else 0,
                        topic.media_summary,
                        topic.source or source,
                    ),
                )

            for event in payload.events:
                conn.execute(
                    """
                    INSERT INTO message_events (
                        event_id, topic_id, title, summary, event_type, direction,
                        confidence, impact_strength, valid_from, expires_at,
                        keywords_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(event_id) DO UPDATE SET
                        topic_id = excluded.topic_id,
                        title = excluded.title,
                        summary = excluded.summary,
                        event_type = excluded.event_type,
                        direction = excluded.direction,
                        confidence = excluded.confidence,
                        impact_strength = excluded.impact_strength,
                        valid_from = excluded.valid_from,
                        expires_at = excluded.expires_at,
                        keywords_json = excluded.keywords_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        event.event_id,
                        event.topic_id,
                        event.title,
                        event.summary,
                        event.event_type,
                        "" if event.direction is None else str(event.direction),
                        float(event.confidence or 0),
                        float(event.impact_strength or 0),
                        event.valid_from,
                        event.expires_at,
                        json.dumps(event.keywords, ensure_ascii=False),
                    ),
                )

            for link in payload.links:
                conn.execute(
                    """
                    INSERT INTO message_event_links (
                        event_id, entity_type, code, name, role, relevance, impact, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(event_id, entity_type, code, name) DO UPDATE SET
                        role = excluded.role,
                        relevance = excluded.relevance,
                        impact = excluded.impact,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    self._link_values(link),
                )

            conn.execute(
                """
                INSERT INTO message_sync_runs (
                    run_id, source, started_at, finished_at, range_start, range_end,
                    topic_count, event_count, link_count, upstream_latest_at,
                    status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    source = excluded.source,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    range_start = excluded.range_start,
                    range_end = excluded.range_end,
                    topic_count = excluded.topic_count,
                    event_count = excluded.event_count,
                    link_count = excluded.link_count,
                    upstream_latest_at = excluded.upstream_latest_at,
                    status = excluded.status,
                    error = excluded.error
                """,
                (
                    run_id,
                    source,
                    started_at,
                    finished_at,
                    payload.start or "",
                    payload.end or "",
                    topic_count,
                    event_count,
                    link_count,
                    payload.upstream_latest_at or "",
                    payload.status or "success",
                    payload.error or "",
                ),
            )

        return ZsxqMessageIngestResponse(
            ok=True,
            source=source,
            run_id=run_id,
            topic_count=topic_count,
            event_count=event_count,
            link_count=link_count,
        )

    def status(self, ingest_enabled: bool = False) -> MessageStoreStatus:
        with self._connect() as conn:
            topic_count = self._scalar(conn, "SELECT COUNT(*) FROM message_topics")
            event_count = self._scalar(conn, "SELECT COUNT(*) FROM message_events")
            link_count = self._scalar(conn, "SELECT COUNT(*) FROM message_event_links")
            latest_topic_time = str(self._scalar(conn, "SELECT COALESCE(MAX(create_time), '') FROM message_topics") or "")
            latest_event_time = str(self._scalar(conn, "SELECT COALESCE(MAX(valid_from), '') FROM message_events") or "")
            row = conn.execute(
                """
                SELECT run_id, source, started_at, finished_at, range_start, range_end,
                       topic_count, event_count, link_count, upstream_latest_at, status, error
                FROM message_sync_runs
                ORDER BY finished_at DESC, rowid DESC
                LIMIT 1
                """
            ).fetchone()

        latest_run = self._sync_run_from_row(row) if row else None
        return MessageStoreStatus(
            db_file=str(self.path),
            ingest_enabled=ingest_enabled,
            topic_count=int(topic_count or 0),
            event_count=int(event_count or 0),
            link_count=int(link_count or 0),
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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    t.topic_id,
                    t.title AS topic_title,
                    t.content,
                    t.create_time,
                    t.owner_name,
                    t.likes,
                    t.readers,
                    t.comments,
                    t.has_files,
                    t.has_images,
                    t.media_summary,
                    t.source,
                    e.event_id,
                    e.title AS event_title,
                    e.summary,
                    e.event_type,
                    e.direction,
                    e.confidence,
                    e.impact_strength,
                    e.valid_from,
                    e.expires_at,
                    e.keywords_json
                FROM message_events e
                JOIN message_topics t ON t.topic_id = e.topic_id
                WHERE e.event_id = ?
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                return None
            link_rows = conn.execute(
                """
                SELECT event_id, entity_type, code, name, role, relevance, impact
                FROM message_event_links
                WHERE event_id = ?
                ORDER BY relevance DESC, impact DESC, entity_type, code, name
                """,
                (normalized,),
            ).fetchall()

        topic = MessageTopic(
            topic_id=str(row["topic_id"] or ""),
            title=str(row["topic_title"] or ""),
            content=str(row["content"] or ""),
            create_time=str(row["create_time"] or ""),
            owner_name=str(row["owner_name"] or ""),
            likes=int(row["likes"] or 0),
            readers=int(row["readers"] or 0),
            comments=int(row["comments"] or 0),
            has_files=bool(row["has_files"]),
            has_images=bool(row["has_images"]),
            media_summary=str(row["media_summary"] or ""),
            source=str(row["source"] or "zsxq"),
        )
        event = MessageEvent(
            event_id=str(row["event_id"] or ""),
            topic_id=topic.topic_id,
            title=str(row["event_title"] or ""),
            summary=str(row["summary"] or ""),
            event_type=str(row["event_type"] or ""),
            direction=str(row["direction"] or ""),
            confidence=float(row["confidence"] or 0),
            impact_strength=float(row["impact_strength"] or 0),
            valid_from=str(row["valid_from"] or ""),
            expires_at=str(row["expires_at"] or ""),
            keywords=self._json_list(row["keywords_json"]),
        )
        links = [
            MessageEventLink(
                event_id=str(link["event_id"] or ""),
                entity_type=str(link["entity_type"] or ""),
                code=str(link["code"] or ""),
                name=str(link["name"] or ""),
                role=str(link["role"] or ""),
                relevance=float(link["relevance"] or 0),
                impact=float(link["impact"] or 0),
            )
            for link in link_rows
        ]
        return MessageDetailPayload(
            topic=topic,
            event=event,
            links=links,
            sync=self.status(ingest_enabled=ingest_enabled),
        )

    def _stock_evidence(self, code: str, limit: int) -> list[MessageEvidence]:
        if not code:
            return []
        sql = f"""
            {self._evidence_select_sql()}
            WHERE l.entity_type = 'stock' AND l.code = ?
            ORDER BY t.create_time DESC, e.impact_strength DESC, e.confidence DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (code, max(1, limit))).fetchall()
        return [self._evidence_from_row(row, "stock") for row in rows]

    def _sector_evidence(self, terms: list[str], limit: int) -> list[MessageEvidence]:
        normalized_terms = self._normalize_terms(terms)
        if not normalized_terms:
            return []

        clauses: list[str] = []
        params: list[Any] = []
        for term in normalized_terms:
            local_clauses = ["l.name = ?", "l.name LIKE ?"]
            params.extend([term, f"%{term}%"])
            slug = self._slug_term(term)
            if slug:
                local_clauses.extend(["l.code = ?", "l.code LIKE ?"])
                params.extend([slug, f"%{slug}%"])
            clauses.append(f"({' OR '.join(local_clauses)})")

        sql = f"""
            {self._evidence_select_sql()}
            WHERE l.entity_type IN ('sector', 'theme') AND ({' OR '.join(clauses)})
            ORDER BY t.create_time DESC, e.impact_strength DESC, e.confidence DESC
            LIMIT ?
        """
        params.append(max(1, limit * 3))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        seen: set[tuple[str, str, str, str]] = set()
        evidence: list[MessageEvidence] = []
        for row in rows:
            key = (str(row["event_id"]), str(row["entity_type"]), str(row["code"]), str(row["name"]))
            if key in seen:
                continue
            seen.add(key)
            evidence.append(self._evidence_from_row(row, "sector"))
            if len(evidence) >= limit:
                break
        return evidence

    def _evidence_select_sql(self) -> str:
        return """
            SELECT
                t.source,
                t.topic_id,
                t.title AS topic_title,
                substr(t.content, 1, 600) AS topic_content,
                t.create_time,
                t.owner_name,
                t.likes,
                t.readers,
                t.comments,
                t.has_files,
                t.has_images,
                substr(t.media_summary, 1, 600) AS media_summary,
                e.event_id,
                e.title AS event_title,
                substr(e.summary, 1, 600) AS event_summary,
                e.event_type,
                e.direction,
                e.confidence,
                e.impact_strength,
                e.valid_from,
                e.expires_at,
                e.keywords_json,
                l.entity_type,
                l.code,
                l.name,
                l.role,
                l.relevance,
                l.impact
            FROM message_event_links l
            JOIN message_events e ON e.event_id = l.event_id
            JOIN message_topics t ON t.topic_id = e.topic_id
        """

    def _evidence_from_row(self, row: sqlite3.Row, match_scope: str) -> MessageEvidence:
        return MessageEvidence(
            source=str(row["source"] or "zsxq"),
            topic_id=str(row["topic_id"] or ""),
            topic_title=str(row["topic_title"] or ""),
            topic_content=str(row["topic_content"] or ""),
            create_time=str(row["create_time"] or ""),
            owner_name=str(row["owner_name"] or ""),
            likes=int(row["likes"] or 0),
            readers=int(row["readers"] or 0),
            comments=int(row["comments"] or 0),
            has_files=bool(row["has_files"]),
            has_images=bool(row["has_images"]),
            media_summary=str(row["media_summary"] or ""),
            event_id=str(row["event_id"] or ""),
            event_title=str(row["event_title"] or ""),
            event_summary=str(row["event_summary"] or ""),
            event_type=str(row["event_type"] or ""),
            direction=str(row["direction"] or ""),
            confidence=float(row["confidence"] or 0),
            impact_strength=float(row["impact_strength"] or 0),
            valid_from=str(row["valid_from"] or ""),
            expires_at=str(row["expires_at"] or ""),
            keywords=self._json_list(row["keywords_json"]),
            entity_type=str(row["entity_type"] or ""),
            code=str(row["code"] or ""),
            name=str(row["name"] or ""),
            role=str(row["role"] or ""),
            relevance=float(row["relevance"] or 0),
            impact=float(row["impact"] or 0),
            match_scope=match_scope,
        )

    def _sync_run_from_row(self, row: sqlite3.Row) -> MessageSyncRunStatus:
        return MessageSyncRunStatus(
            run_id=str(row["run_id"] or ""),
            source=str(row["source"] or ""),
            started_at=str(row["started_at"] or ""),
            finished_at=str(row["finished_at"] or ""),
            start=str(row["range_start"] or ""),
            end=str(row["range_end"] or ""),
            topic_count=int(row["topic_count"] or 0),
            event_count=int(row["event_count"] or 0),
            link_count=int(row["link_count"] or 0),
            upstream_latest_at=str(row["upstream_latest_at"] or ""),
            status=str(row["status"] or ""),
            error=str(row["error"] or ""),
        )

    def _link_values(self, link: MessageEventLink) -> tuple[Any, ...]:
        return (
            link.event_id,
            link.entity_type.strip().lower(),
            link.code.strip(),
            link.name.strip(),
            link.role,
            float(link.relevance or 0),
            float(link.impact or 0),
        )

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
        try:
            parsed = json.loads(str(value))
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()]

    def _scalar(self, conn: sqlite3.Connection, sql: str) -> Any:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_topics (
                    topic_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    create_time TEXT NOT NULL DEFAULT '',
                    owner_name TEXT NOT NULL DEFAULT '',
                    likes INTEGER NOT NULL DEFAULT 0,
                    readers INTEGER NOT NULL DEFAULT 0,
                    comments INTEGER NOT NULL DEFAULT 0,
                    has_files INTEGER NOT NULL DEFAULT 0,
                    has_images INTEGER NOT NULL DEFAULT 0,
                    media_summary TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'zsxq',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS message_events (
                    event_id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    impact_strength REAL NOT NULL DEFAULT 0,
                    valid_from TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT '',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS message_event_links (
                    event_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    code TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    relevance REAL NOT NULL DEFAULT 0,
                    impact REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (event_id, entity_type, code, name)
                );

                CREATE TABLE IF NOT EXISTS message_sync_runs (
                    run_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT 'zsxq',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    range_start TEXT NOT NULL DEFAULT '',
                    range_end TEXT NOT NULL DEFAULT '',
                    topic_count INTEGER NOT NULL DEFAULT 0,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    link_count INTEGER NOT NULL DEFAULT 0,
                    upstream_latest_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_message_topics_create_time
                    ON message_topics(create_time);
                CREATE INDEX IF NOT EXISTS idx_message_events_topic_id
                    ON message_events(topic_id);
                CREATE INDEX IF NOT EXISTS idx_message_events_valid_from
                    ON message_events(valid_from);
                CREATE INDEX IF NOT EXISTS idx_message_links_entity
                    ON message_event_links(entity_type, code, name);
                CREATE INDEX IF NOT EXISTS idx_message_sync_runs_finished_at
                    ON message_sync_runs(finished_at);
                """
            )
