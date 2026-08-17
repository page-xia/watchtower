from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx


DEFAULT_SOURCE_DB = Path(r"G:\ai\lh\zsxq\data\stock_agent.sqlite")
DEFAULT_TARGET_URL = "http://127.0.0.1:8788"
DEFAULT_BOARD_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "runtime"
CN_TZ = ZoneInfo("Asia/Shanghai")
INGEST_MAX_ATTEMPTS = 3
INGEST_RETRY_STATUSES = {429, 500, 502, 503, 504}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push classified ZSXQ messages into intraday watchtower.")
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB), help="Upstream stock_agent.sqlite path.")
    parser.add_argument("--target-url", action="append", default=[], help="Watchtower base URL; repeat to fan out.")
    parser.add_argument("--token", default=os.getenv("WATCH_INGEST_TOKEN", ""), help="Bearer token for ingest API.")
    parser.add_argument("--start", default="", help="Inclusive topic create_time lower bound, ISO string.")
    parser.add_argument("--end", default="", help="Inclusive topic create_time upper bound, ISO string.")
    parser.add_argument("--lookback-days", type=int, default=7, help="Used when --start is omitted.")
    parser.add_argument("--batch-size", type=int, default=500, help="Joined rows per ingest request.")
    parser.add_argument("--media-mode", choices=["fast", "full"], default="full", help="Fast mode skips file/voice topics.")
    parser.add_argument(
        "--no-tdx-board-enrich",
        dest="tdx_board_enrich",
        action="store_false",
        default=True,
        help="Do not append easy_tdx official level 1/2/3 board links from runtime cache.",
    )
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
    push_run_id = f"zsxq-push-{datetime.now(CN_TZ).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    media_mode = normalize_media_mode(getattr(args, "media_mode", "full"))
    board_names_by_stock = (
        load_easy_tdx_board_names_by_stock()
        if bool(getattr(args, "tdx_board_enrich", True))
        else {}
    )
    totals = {"topics": 0, "events": 0, "links": 0, "batches": 0, "requests": 0, "targets": len(target_urls)}
    topic_ids: set[str] = set()
    event_ids: set[str] = set()
    link_ids: set[tuple[str, str, str, str]] = set()
    upstream_latest_at = latest_upstream_time(source_db, start, end)
    errors: list[dict[str, Any]] = []

    try:
        with httpx.Client(timeout=60) as client:
            for batch in iter_batches(source_db, start, end, max(1, int(args.batch_size)), media_mode, board_names_by_stock):
                if not batch["topics"] and not batch["events"] and not batch["links"]:
                    continue
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
                    error = post_payload_with_autosplit(
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
        error = safe_error(exc, str(args.token or ""))
        if not args.dry_run:
            for target_url in target_urls or [""]:
                if target_url:
                    record_failed_run(
                        target=target_url,
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
                    "target_url": target_urls[0] if target_urls else "",
                    "target_urls": target_urls,
                    "target_count": len(target_urls),
                    "start": start,
                    "end": end,
                    "upstream_latest_at": upstream_latest_at,
                    "media_mode": media_mode,
                    "dry_run": bool(args.dry_run),
                    "error": error,
                    **totals,
                },
                ensure_ascii=False,
            )
        )
        return 1

    if errors:
        print(
            json.dumps(
                {
                    "ok": False,
                    "source_db": str(source_db),
                    "target_url": target_urls[0] if target_urls else "",
                    "target_urls": target_urls,
                    "target_count": len(target_urls),
                    "start": start,
                    "end": end,
                    "upstream_latest_at": upstream_latest_at,
                    "media_mode": media_mode,
                    "dry_run": bool(args.dry_run),
                    "errors": errors,
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
                "target_url": target_urls[0] if target_urls else "",
                "target_urls": target_urls,
                "target_count": len(target_urls),
                "start": start,
                "end": end,
                "upstream_latest_at": upstream_latest_at,
                "media_mode": media_mode,
                "dry_run": bool(args.dry_run),
                **totals,
            },
            ensure_ascii=False,
        )
    )
    return 0


def normalize_media_mode(value: str) -> str:
    mode = str(value or "full").strip().lower()
    return mode if mode in {"fast", "full"} else "full"


def resolve_target_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    else:
        candidates = list(value or [])
    if not candidates:
        env_urls = str(os.getenv("WATCH_TARGET_URLS", "") or "").strip()
        if env_urls:
            candidates.extend([part.strip() for part in re.split(r"[;,]", env_urls) if part.strip()])
        else:
            primary = str(os.getenv("WATCH_TARGET_URL", DEFAULT_TARGET_URL) or DEFAULT_TARGET_URL).strip()
            if primary:
                candidates.append(primary)
            local = str(os.getenv("WATCH_LOCAL_TARGET_URL", "") or "").strip()
            if local:
                candidates.append(local)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        target = str(item or "").strip().rstrip("/")
        if not target or target in seen:
            continue
        seen.add(target)
        cleaned.append(target)
    return cleaned


def resolve_window(start: str, end: str, lookback_days: int) -> tuple[str, str]:
    finished = parse_datetime(end, end_of_day=True) if end else datetime.now(CN_TZ)
    started = parse_datetime(start) if start else finished - timedelta(days=max(1, lookback_days))
    return started.isoformat(), finished.isoformat()


def build_batch_payload(
    *,
    source: str,
    run_id: str,
    started_at: str,
    finished_at: str,
    start: str,
    end: str,
    upstream_latest_at: str,
    topics: list[dict[str, Any]],
    events: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source": source,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "start": start,
        "end": end,
        "upstream_latest_at": upstream_latest_at,
        "topics": topics,
        "events": events,
        "links": links,
    }


def build_summary_payload(
    *,
    source: str,
    run_id: str,
    started_at: str,
    finished_at: str,
    start: str,
    end: str,
    upstream_latest_at: str,
    reported_topic_count: int,
    reported_event_count: int,
    reported_link_count: int,
    status: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "start": start,
        "end": end,
        "upstream_latest_at": upstream_latest_at,
        "status": status,
        "reported_topic_count": reported_topic_count,
        "reported_event_count": reported_event_count,
        "reported_link_count": reported_link_count,
        "topics": [],
        "events": [],
        "links": [],
    }


def post_payload_to_target(
    client: httpx.Client,
    target_url: str,
    payload: dict[str, Any],
    token: str,
    *,
    run_started: str,
    start: str,
    end: str,
    upstream_latest_at: str,
) -> str:
    last_error = ""
    for attempt in range(1, INGEST_MAX_ATTEMPTS + 1):
        try:
            response = client.post(
                f"{target_url}/api/ingest/zsxq/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            response.raise_for_status()
            return ""
        except Exception as exc:
            error = safe_error(exc, token)
            last_error = error
            if attempt < INGEST_MAX_ATTEMPTS and is_retryable_ingest_error(exc):
                time.sleep(min(2.0, 0.5 * (2 ** (attempt - 1))))
                continue
            record_failed_run(
                target=target_url,
                token=token,
                start=start,
                end=end,
                upstream_latest_at=upstream_latest_at,
                started_at=run_started,
                error=error,
            )
            return error
    return last_error


def post_payload_with_autosplit(
    client: httpx.Client,
    target_url: str,
    payload: dict[str, Any],
    token: str,
    *,
    run_started: str,
    start: str,
    end: str,
    upstream_latest_at: str,
    depth: int = 0,
) -> str:
    """Post a batch payload; on persistent failure split it in half and retry.

    The CloudBase MySQL REST gateway behind the ingest endpoint rejects or
    times out on oversized upserts (notably since easy_tdx board enrichment
    inflated link rows per batch). Upserts are idempotent, so re-posting a
    subset of rows is safe.
    """
    error = post_payload_to_target(
        client,
        target_url,
        payload,
        token,
        run_started=run_started,
        start=start,
        end=end,
        upstream_latest_at=upstream_latest_at,
    )
    if not error:
        return ""
    topics = list(payload.get("topics") or [])
    events = list(payload.get("events") or [])
    links = list(payload.get("links") or [])
    largest = max(len(topics), len(events), len(links))
    if largest <= 25 or depth >= 5:
        return error
    halves = [
        (topics[: len(topics) // 2], events[: len(events) // 2], links[: len(links) // 2], "a"),
        (topics[len(topics) // 2 :], events[len(events) // 2 :], links[len(links) // 2 :], "b"),
    ]
    sub_errors: list[str] = []
    for sub_topics, sub_events, sub_links, suffix in halves:
        if not sub_topics and not sub_events and not sub_links:
            continue
        sub_payload = dict(
            payload,
            topics=sub_topics,
            events=sub_events,
            links=sub_links,
            run_id=f"{payload.get('run_id') or 'batch'}{suffix}",
        )
        sub_error = post_payload_with_autosplit(
            client,
            target_url,
            sub_payload,
            token,
            run_started=run_started,
            start=start,
            end=end,
            upstream_latest_at=upstream_latest_at,
            depth=depth + 1,
        )
        if sub_error:
            sub_errors.append(sub_error)
    if sub_errors:
        return "; ".join(dict.fromkeys(sub_errors))
    return ""


def is_retryable_ingest_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return int(exc.response.status_code) in INGEST_RETRY_STATUSES
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


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


def iter_batches(
    path: Path,
    start: str,
    end: str,
    batch_size: int,
    media_mode: str = "full",
    board_names_by_stock: dict[str, list[dict[str, Any]]] | None = None,
):
    normalized_mode = normalize_media_mode(media_mode)
    with connect_readonly(path) as conn:
        cursor = conn.execute(
            """
            SELECT
              l.code, l.name, l.role, l.relevance, l.impact, l.entity_type,
              e.event_id, e.title AS event_title, e.summary AS event_summary,
              e.event_type, e.direction, e.confidence, e.impact_strength,
              e.valid_from, e.expires_at, e.keywords_json,
              r.topic_id, r.title AS topic_title, r.content AS topic_content,
              r.create_time, r.owner_name, r.likes, r.readers, r.comments,
              r.has_files, r.has_images, r.source_json, COALESCE(m.summary, '') AS media_summary
            FROM raw_topics r
            LEFT JOIN topic_media_summaries m ON m.topic_id = r.topic_id
            LEFT JOIN events e ON e.topic_id = r.topic_id
            LEFT JOIN event_entity_links l ON l.event_id = e.event_id
            WHERE r.create_time >= ? AND r.create_time <= ?
            ORDER BY
              r.create_time ASC,
              e.event_id ASC,
              l.entity_type ASC,
              l.code ASC,
              l.name ASC
            """,
            (start, end),
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            yield rows_to_payload(rows, media_mode=normalized_mode, board_names_by_stock=board_names_by_stock)


def rows_to_payload(
    rows: list[sqlite3.Row],
    media_mode: str = "full",
    board_names_by_stock: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    topics: dict[str, dict[str, Any]] = {}
    events: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    normalized_mode = normalize_media_mode(media_mode)

    for row in rows:
        topic_id = str(row["topic_id"] or "")
        event_id = str(row["event_id"] or "")
        entity_type = str(row["entity_type"] or "").strip().lower()
        code = str(row["code"] or "").strip()
        name = str(row["name"] or "").strip()
        media_kind = topic_media_kind_from_row(row)
        if normalized_mode == "fast" and media_kind not in {"text", "image"}:
            continue
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
                "media_kind": media_kind,
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

    enrich_links_with_easy_tdx_boards(links, board_names_by_stock or {})
    return {
        "topics": list(topics.values()),
        "events": list(events.values()),
        "links": list(links.values()),
    }


def load_easy_tdx_board_names_by_stock(
    cache_dir: Path | str = DEFAULT_BOARD_CACHE_DIR,
) -> dict[str, list[dict[str, Any]]]:
    cache_root = Path(cache_dir)
    output: dict[str, list[dict[str, Any]]] = {}
    for level in (1, 2, 3):
        payload = read_json(cache_root / f"easy_tdx_board_level_{level}.json")
        if not isinstance(payload, dict):
            continue
        sectors = payload.get("sectors")
        members_by_code = payload.get("members_by_code")
        if not isinstance(sectors, list) or not isinstance(members_by_code, dict):
            continue
        name_by_code = {
            str(item.get("board_code") or "").strip(): str(item.get("name") or "").strip()
            for item in sectors
            if isinstance(item, dict)
            and str(item.get("board_code") or "").strip()
            and str(item.get("name") or "").strip()
        }
        code_to_name = {
            str(key).strip(): str(value).strip()
            for key, value in dict(payload.get("code_to_name") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        for board_code, members in members_by_code.items():
            normalized_board_code = str(board_code or "").strip()
            board_name = name_by_code.get(normalized_board_code) or code_to_name.get(normalized_board_code) or ""
            if not normalized_board_code or not board_name or not isinstance(members, list):
                continue
            for raw_code in members:
                stock_code = normalize_stock_code(raw_code)
                if not stock_code:
                    continue
                items = output.setdefault(stock_code, [])
                if any(item["level"] == level and item["name"] == board_name for item in items):
                    continue
                items.append({"level": level, "code": normalized_board_code, "name": board_name})
    for items in output.values():
        items.sort(key=lambda item: int(item.get("level") or 0))
    return output


def enrich_links_with_easy_tdx_boards(
    links: dict[tuple[str, str, str, str], dict[str, Any]],
    board_names_by_stock: dict[str, list[dict[str, Any]]],
) -> None:
    if not links or not board_names_by_stock:
        return
    existing_sector_names = {
        (str(link.get("event_id") or ""), str(link.get("name") or "").strip())
        for link in links.values()
        if str(link.get("entity_type") or "").strip().lower() == "sector"
        and str(link.get("name") or "").strip()
    }
    stock_links = [
        link
        for link in links.values()
        if str(link.get("entity_type") or "").strip().lower() == "stock"
    ]
    for stock_link in stock_links:
        event_id = str(stock_link.get("event_id") or "").strip()
        stock_code = normalize_stock_code(stock_link.get("code"))
        if not event_id or not stock_code:
            continue
        for board in board_names_by_stock.get(stock_code, []):
            board_name = str(board.get("name") or "").strip()
            board_code = str(board.get("code") or "").strip() or board_name
            level = int(board.get("level") or 0)
            if not board_name or (event_id, board_name) in existing_sector_names:
                continue
            relevance = max(0.55, min(0.86, float(stock_link.get("relevance") or 0) * 0.8))
            impact = max(0.5, min(0.82, float(stock_link.get("impact") or 0) * 0.8))
            links[(event_id, "sector", board_code, board_name)] = {
                "event_id": event_id,
                "entity_type": "sector",
                "code": board_code,
                "name": board_name,
                "role": f"easy_tdx申万{level}级/stock:{stock_code}",
                "relevance": relevance,
                "impact": impact,
            }
            existing_sector_names.add((event_id, board_name))


def normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() else ""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def topic_media_kind_from_row(row: sqlite3.Row | dict[str, Any]) -> str:
    has_files = bool(row["has_files"])
    has_images = bool(row["has_images"])
    source_json = normalize_source_json(row.get("source_json") if isinstance(row, dict) else row["source_json"])
    payload = source_json.get("topic") if isinstance(source_json.get("topic"), dict) else source_json
    files = payload.get("files") if isinstance(payload, dict) else []
    if not has_files and not has_images:
        return "text"
    if has_images and not has_files:
        return "image"
    if has_files and not has_images:
        if isinstance(files, list) and files and all(_attachment_is_audio(item) for item in files if isinstance(item, dict)):
            return "voice"
        return "file"
    return "mixed"


def normalize_source_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _attachment_is_audio(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    parts = [
        str(item.get(key) or "").strip().lower()
        for key in ("kind", "type", "mime", "mime_type", "content_type", "name", "title", "file_name", "suffix", "ext", "url", "path", "description")
    ]
    text = " ".join(part for part in parts if part)
    return bool(text) and any(hint in text for hint in ("audio", "voice", "mp3", "m4a", "aac", "wav", "amr", "ogg", "flac", "opus", "caf", "mid", "midi"))


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
