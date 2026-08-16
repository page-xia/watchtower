from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from push_zsxq_messages import (  # noqa: E402
    DEFAULT_SOURCE_DB,
    build_batch_payload,
    build_summary_payload,
    latest_upstream_time,
    post_payload_to_target,
    resolve_target_urls,
    resolve_window,
)


CN_TZ = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push only easy_tdx official board links from the ZSXQ source DB."
    )
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB), help="Upstream stock_agent.sqlite path.")
    parser.add_argument("--target-url", action="append", default=[], help="Watchtower base URL; repeat to fan out.")
    parser.add_argument("--token", default=os.getenv("WATCH_INGEST_TOKEN", ""), help="Bearer token for ingest API.")
    parser.add_argument("--start", default="", help="Inclusive topic create_time lower bound, ISO string.")
    parser.add_argument("--end", default="", help="Inclusive topic create_time upper bound, ISO string.")
    parser.add_argument("--lookback-days", type=int, default=7, help="Used when --start is omitted.")
    parser.add_argument("--batch-size", type=int, default=250, help="easy_tdx sector links per ingest request.")
    parser.add_argument("--delay-seconds", type=float, default=0.2, help="Delay between ingest requests.")
    parser.add_argument("--max-batches", type=int, default=0, help="Stop after N batches; 0 means no limit.")
    parser.add_argument("--links-only", action="store_true", help="Push links without topic/event rows.")
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

    target_urls = resolve_target_urls(args.target_url)
    if not target_urls and not args.dry_run:
        print("missing target url: pass --target-url or set WATCH_TARGET_URL", file=sys.stderr)
        return 2

    start, end = resolve_window(args.start, args.end, int(args.lookback_days))
    run_started = datetime.now(CN_TZ).isoformat(timespec="seconds")
    push_run_id = f"zsxq-tdx-board-repair-{datetime.now(CN_TZ).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    upstream_latest_at = latest_upstream_time(source_db, start, end)
    totals = {"topics": 0, "events": 0, "links": 0, "batches": 0, "requests": 0, "targets": len(target_urls)}
    topic_ids: set[str] = set()
    event_ids: set[str] = set()
    link_ids: set[tuple[str, str, str, str]] = set()
    errors: list[dict[str, Any]] = []

    try:
        with httpx.Client(timeout=60) as client:
            for batch in iter_repair_batches(
                source_db,
                start=start,
                end=end,
                batch_size=max(1, int(args.batch_size)),
                include_topics_events=not bool(args.links_only),
            ):
                totals["batches"] += 1
                update_unique_ids(batch, topic_ids, event_ids, link_ids)
                totals["topics"] = len(topic_ids)
                totals["events"] = len(event_ids)
                totals["links"] = len(link_ids)
                if args.dry_run:
                    continue
                payload = build_batch_payload(
                    source="zsxq",
                    run_id=f"{push_run_id}-{totals['batches']:04d}",
                    started_at=run_started,
                    finished_at=datetime.now(CN_TZ).isoformat(timespec="seconds"),
                    start=start,
                    end=end,
                    upstream_latest_at=upstream_latest_at,
                    topics=batch["topics"],
                    events=batch["events"],
                    links=batch["links"],
                )
                for target_url in target_urls:
                    totals["requests"] += 1
                    error = post_payload_to_target(
                        client,
                        target_url,
                        payload,
                        args.token,
                        run_started=run_started,
                        start=start,
                        end=end,
                        upstream_latest_at=upstream_latest_at,
                    )
                    if error:
                        errors.append(
                            {
                                "target_url": target_url,
                                "stage": "batch",
                                "batch": totals["batches"],
                                "error": error,
                            }
                        )
                if args.max_batches and totals["batches"] >= int(args.max_batches):
                    break
                if float(args.delay_seconds) > 0:
                    time.sleep(float(args.delay_seconds))

            if not args.dry_run:
                summary_payload = build_summary_payload(
                    source="zsxq",
                    run_id=push_run_id,
                    started_at=run_started,
                    finished_at=datetime.now(CN_TZ).isoformat(timespec="seconds"),
                    start=start,
                    end=end,
                    upstream_latest_at=upstream_latest_at,
                    reported_topic_count=totals["topics"],
                    reported_event_count=totals["events"],
                    reported_link_count=totals["links"],
                    status="partial" if errors else "success",
                )
                for target_url in target_urls:
                    totals["requests"] += 1
                    error = post_payload_to_target(
                        client,
                        target_url,
                        summary_payload,
                        args.token,
                        run_started=run_started,
                        start=start,
                        end=end,
                        upstream_latest_at=upstream_latest_at,
                    )
                    if error:
                        errors.append({"target_url": target_url, "stage": "summary", "error": error})
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "source_db": str(source_db),
                    "target_urls": target_urls,
                    "start": start,
                    "end": end,
                    "dry_run": bool(args.dry_run),
                    "links_only": bool(args.links_only),
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    **totals,
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "ok": not bool(errors),
                "source_db": str(source_db),
                "target_urls": target_urls,
                "start": start,
                "end": end,
                "dry_run": bool(args.dry_run),
                "links_only": bool(args.links_only),
                "errors": errors,
                **totals,
            },
            ensure_ascii=False,
        )
    )
    return 1 if errors else 0


def iter_repair_batches(
    path: Path,
    *,
    start: str,
    end: str,
    batch_size: int,
    include_topics_events: bool = True,
) -> Iterable[dict[str, list[dict[str, Any]]]]:
    with connect_readonly(path) as conn:
        cursor = conn.execute(
            """
            SELECT
              r.topic_id, r.title AS topic_title, r.content AS topic_content,
              r.create_time, r.owner_name, r.likes, r.readers, r.comments,
              r.has_files, r.has_images,
              e.event_id, e.title AS event_title, e.summary AS event_summary,
              e.event_type, e.direction, e.confidence, e.impact_strength,
              e.valid_from, e.expires_at, e.keywords_json,
              l.entity_type, l.code, l.name, l.role, l.relevance, l.impact
            FROM event_entity_links l
            JOIN events e ON e.event_id = l.event_id
            JOIN raw_topics r ON r.topic_id = e.topic_id
            WHERE r.create_time >= ?
              AND r.create_time <= ?
              AND lower(l.entity_type) = 'sector'
              AND l.role LIKE 'easy_tdx申万%'
            ORDER BY r.create_time ASC, e.event_id ASC, l.code ASC, l.name ASC
            """,
            (start, end),
        )
        while True:
            rows = cursor.fetchmany(max(1, int(batch_size)))
            if not rows:
                break
            yield rows_to_payload(rows, include_topics_events=include_topics_events)


def rows_to_payload(rows: list[sqlite3.Row], *, include_topics_events: bool) -> dict[str, list[dict[str, Any]]]:
    topics: dict[str, dict[str, Any]] = {}
    events: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        topic_id = str(row["topic_id"] or "")
        event_id = str(row["event_id"] or "")
        if include_topics_events and topic_id:
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
                "media_kind": topic_media_kind(row),
                "media_summary": "",
                "source": "zsxq",
            }
        if include_topics_events and event_id:
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
        entity_type = str(row["entity_type"] or "").strip().lower()
        code = str(row["code"] or "").strip()
        name = str(row["name"] or "").strip()
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
    return {"topics": list(topics.values()), "events": list(events.values()), "links": list(links.values())}


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


def topic_media_kind(row: sqlite3.Row) -> str:
    has_files = bool(row["has_files"])
    has_images = bool(row["has_images"])
    if has_files and has_images:
        return "mixed"
    if has_files:
        return "file"
    if has_images:
        return "image"
    return "text"


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
