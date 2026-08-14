from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx


DEFAULT_SOURCE_DB = Path(r"G:\ai\lh\zsxq\data\stock_agent.sqlite")
DEFAULT_TARGET_URL = "http://127.0.0.1:8788"
CN_TZ = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push classified ZSXQ messages into intraday watchtower.")
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB), help="Upstream stock_agent.sqlite path.")
    parser.add_argument("--target-url", default=os.getenv("WATCH_TARGET_URL", DEFAULT_TARGET_URL), help="Watchtower base URL.")
    parser.add_argument("--token", default=os.getenv("WATCH_INGEST_TOKEN", ""), help="Bearer token for ingest API.")
    parser.add_argument("--start", default="", help="Inclusive topic create_time lower bound, ISO string.")
    parser.add_argument("--end", default="", help="Inclusive topic create_time upper bound, ISO string.")
    parser.add_argument("--lookback-days", type=int, default=7, help="Used when --start is omitted.")
    parser.add_argument("--batch-size", type=int, default=500, help="Joined rows per ingest request.")
    parser.add_argument("--dry-run", action="store_true", help="Read and summarize without posting.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_db = Path(args.source_db)
    if not source_db.exists():
        print(f"source db not found: {source_db}", file=sys.stderr)
        return 2
    if not args.token and not args.dry_run:
        print("missing token: pass --token or set WATCH_INGEST_TOKEN", file=sys.stderr)
        return 2

    start, end = resolve_window(args.start, args.end, int(args.lookback_days))
    target = args.target_url.rstrip("/")
    run_started = datetime.now(CN_TZ).isoformat(timespec="seconds")
    push_run_id = f"zsxq-push-{datetime.now(CN_TZ).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    totals = {"topics": 0, "events": 0, "links": 0, "batches": 0, "requests": 0}
    topic_ids: set[str] = set()
    event_ids: set[str] = set()
    link_ids: set[tuple[str, str, str, str]] = set()
    upstream_latest_at = latest_upstream_time(source_db, start, end)

    try:
        with httpx.Client(timeout=60) as client:
            for batch in iter_batches(source_db, start, end, max(1, int(args.batch_size))):
                if not batch["topics"] and not batch["events"] and not batch["links"]:
                    continue
                totals["batches"] += 1
                update_unique_ids(batch, topic_ids, event_ids, link_ids)
                totals["topics"] = len(topic_ids)
                totals["events"] = len(event_ids)
                totals["links"] = len(link_ids)
                if args.dry_run:
                    continue
                totals["requests"] += 1
                payload = {
                    "source": "zsxq",
                    "run_id": f"{push_run_id}-{totals['batches']:04d}",
                    "started_at": run_started,
                    "finished_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                    "start": start,
                    "end": end,
                    "upstream_latest_at": upstream_latest_at,
                    "topics": batch["topics"],
                    "events": batch["events"],
                    "links": batch["links"],
                }
                response = client.post(
                    f"{target}/api/ingest/zsxq/messages",
                    headers={"Authorization": f"Bearer {args.token}"},
                    json=payload,
                )
                response.raise_for_status()
            if not args.dry_run:
                totals["requests"] += 1
                response = client.post(
                    f"{target}/api/ingest/zsxq/messages",
                    headers={"Authorization": f"Bearer {args.token}"},
                    json={
                        "source": "zsxq",
                        "run_id": push_run_id,
                        "started_at": run_started,
                        "finished_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                        "start": start,
                        "end": end,
                        "upstream_latest_at": upstream_latest_at,
                        "status": "success",
                        "reported_topic_count": totals["topics"],
                        "reported_event_count": totals["events"],
                        "reported_link_count": totals["links"],
                        "topics": [],
                        "events": [],
                        "links": [],
                    },
                )
                response.raise_for_status()
    except Exception as exc:
        error = safe_error(exc, str(args.token or ""))
        if not args.dry_run:
            record_failed_run(
                target=target,
                token=str(args.token or ""),
                start=start,
                end=end,
                upstream_latest_at=upstream_latest_at,
                started_at=run_started,
                error=error,
            )
        print(
            json.dumps(
                {
                    "ok": False,
                    "source_db": str(source_db),
                    "target_url": target,
                    "start": start,
                    "end": end,
                    "upstream_latest_at": upstream_latest_at,
                    "dry_run": bool(args.dry_run),
                    "error": error,
                    **totals,
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "source_db": str(source_db),
                "target_url": target,
                "start": start,
                "end": end,
                "upstream_latest_at": upstream_latest_at,
                "dry_run": bool(args.dry_run),
                **totals,
            },
            ensure_ascii=False,
        )
    )
    return 0


def resolve_window(start: str, end: str, lookback_days: int) -> tuple[str, str]:
    finished = parse_datetime(end, end_of_day=True) if end else datetime.now(CN_TZ)
    started = parse_datetime(start) if start else finished - timedelta(days=max(1, lookback_days))
    return started.isoformat(), finished.isoformat()


def parse_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    normalized = value.strip()
    date_only = len(normalized) == 8 and normalized.isdigit()
    date_only = date_only or len(normalized) == 10 and normalized[4] == "-" and normalized[7] == "-"
    if date_only:
        fmt = "%Y%m%d" if len(normalized) == 8 else "%Y-%m-%d"
        date_value = datetime.strptime(normalized, fmt).date()
        boundary = clock_time.max if end_of_day else clock_time.min
        return datetime.combine(date_value, boundary, tzinfo=CN_TZ)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = datetime.strptime(normalized, "%Y-%m-%d")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def safe_error(exc: Exception, token: str = "") -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return f"ingest HTTP {response.status_code}"
    message = str(exc).strip() or type(exc).__name__
    if token:
        message = message.replace(token, "[redacted]")
    return f"{type(exc).__name__}: {message[:300]}"


def update_unique_ids(
    batch: dict[str, list[dict[str, Any]]],
    topic_ids: set[str],
    event_ids: set[str],
    link_ids: set[tuple[str, str, str, str]],
) -> None:
    topic_ids.update(str(topic.get("topic_id") or "") for topic in batch["topics"] if topic.get("topic_id"))
    event_ids.update(str(event.get("event_id") or "") for event in batch["events"] if event.get("event_id"))
    link_ids.update(
        (
            str(link.get("event_id") or "").strip(),
            str(link.get("entity_type") or "").strip().lower(),
            str(link.get("code") or "").strip(),
            str(link.get("name") or "").strip(),
        )
        for link in batch["links"]
        if link.get("event_id") and link.get("entity_type")
    )


def record_failed_run(
    *,
    target: str,
    token: str,
    start: str,
    end: str,
    upstream_latest_at: str,
    started_at: str,
    error: str,
) -> None:
    """Leave a failure watermark when the receiver is reachable.

    This is best-effort: a network outage cannot be recorded remotely, but an
    HTTP rejection or server-side error can still be visible in
    ``/api/messages/status`` on the next inspection.
    """
    if not token:
        return
    payload = {
        "source": "zsxq",
        "run_id": f"zsxq-push-error-{datetime.now(CN_TZ).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "started_at": started_at,
        "finished_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "start": start,
        "end": end,
        "upstream_latest_at": upstream_latest_at,
        "status": "error",
        "error": error,
        "topics": [],
        "events": [],
        "links": [],
    }
    try:
        with httpx.Client(timeout=10) as client:
            client.post(
                f"{target}/api/ingest/zsxq/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except Exception:
        return


def latest_upstream_time(path: Path, start: str, end: str) -> str:
    with connect_readonly(path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(create_time), '') FROM raw_topics WHERE create_time >= ? AND create_time <= ?",
            (start, end),
        ).fetchone()
    return str(row[0] or "") if row else ""


def iter_batches(path: Path, start: str, end: str, batch_size: int):
    offset = 0
    while True:
        with connect_readonly(path) as conn:
            rows = conn.execute(
                """
                SELECT
                  l.code, l.name, l.role, l.relevance, l.impact, l.entity_type,
                  e.event_id, e.title AS event_title, e.summary AS event_summary,
                  e.event_type, e.direction, e.confidence, e.impact_strength,
                  e.valid_from, e.expires_at, e.keywords_json,
                  r.topic_id, r.title AS topic_title, r.content AS topic_content,
                  r.create_time, r.owner_name, r.likes, r.readers, r.comments,
                  r.has_files, r.has_images, COALESCE(m.summary, '') AS media_summary
                FROM raw_topics r
                LEFT JOIN topic_media_summaries m ON m.topic_id = r.topic_id
                LEFT JOIN events e ON e.topic_id = r.topic_id
                LEFT JOIN event_entity_links l ON l.event_id = e.event_id
                WHERE r.create_time >= ? AND r.create_time <= ?
                ORDER BY r.create_time ASC, e.event_id ASC
                LIMIT ? OFFSET ?
                """,
                (start, end, batch_size, offset),
            ).fetchall()
        if not rows:
            break
        yield rows_to_payload(rows)
        offset += batch_size


def rows_to_payload(rows: list[sqlite3.Row]) -> dict[str, list[dict[str, Any]]]:
    topics: dict[str, dict[str, Any]] = {}
    events: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    for row in rows:
        topic_id = str(row["topic_id"] or "")
        event_id = str(row["event_id"] or "")
        entity_type = str(row["entity_type"] or "").strip().lower()
        code = str(row["code"] or "").strip()
        name = str(row["name"] or "").strip()
        if topic_id:
            topics[topic_id] = {
                "topic_id": topic_id,
                "title": str(row["topic_title"] or ""),
                "content": str(row["topic_content"] or ""),
                "create_time": str(row["create_time"] or ""),
                "owner_name": str(row["owner_name"] or ""),
                "likes": int(row["likes"] or 0),
                "readers": int(row["readers"] or 0),
                "comments": int(row["comments"] or 0),
                "has_files": bool(row["has_files"]),
                "has_images": bool(row["has_images"]),
                "media_summary": str(row["media_summary"] or ""),
                "source": "zsxq",
            }
        if event_id:
            events[event_id] = {
                "event_id": event_id,
                "topic_id": topic_id,
                "title": str(row["event_title"] or ""),
                "summary": str(row["event_summary"] or ""),
                "event_type": str(row["event_type"] or ""),
                "direction": row["direction"],
                "confidence": float(row["confidence"] or 0),
                "impact_strength": float(row["impact_strength"] or 0),
                "valid_from": str(row["valid_from"] or ""),
                "expires_at": str(row["expires_at"] or ""),
                "keywords": json_list(row["keywords_json"]),
            }
        if event_id and entity_type:
            links[(event_id, entity_type, code, name)] = {
                "event_id": event_id,
                "entity_type": entity_type,
                "code": code,
                "name": name,
                "role": str(row["role"] or ""),
                "relevance": float(row["relevance"] or 0),
                "impact": float(row["impact"] or 0),
            }

    return {
        "topics": list(topics.values()),
        "events": list(events.values()),
        "links": list(links.values()),
    }


def json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    raise SystemExit(main())
