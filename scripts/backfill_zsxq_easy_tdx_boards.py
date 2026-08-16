from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from push_zsxq_messages import (
    DEFAULT_BOARD_CACHE_DIR,
    DEFAULT_SOURCE_DB,
    load_easy_tdx_board_names_by_stock,
    normalize_stock_code,
)


CN_TZ = ZoneInfo("Asia/Shanghai")


class BackfillResult(NamedTuple):
    rows: list[dict[str, Any]]
    stock_events: int
    coverable_events: int
    unmapped_events: int
    skipped_existing_name: int
    skipped_code_conflict: int
    unmapped_stocks: Counter[tuple[str, str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill ZSXQ event sector links with easy_tdx official level 1/2/3 boards."
    )
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB), help="Upstream stock_agent.sqlite path.")
    parser.add_argument(
        "--board-cache-dir",
        default=str(DEFAULT_BOARD_CACHE_DIR),
        help="Directory containing easy_tdx_board_level_{1,2,3}.json.",
    )
    parser.add_argument("--apply", action="store_true", help="Write missing sector links. Omit for dry-run audit.")
    parser.add_argument("--no-backup", action="store_true", help="Skip SQLite file backup before --apply.")
    parser.add_argument("--sample-limit", type=int, default=10, help="Number of sample inserted rows to print.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_db = Path(args.source_db)
    if not source_db.exists():
        print(f"source db not found: {source_db}", file=sys.stderr)
        return 2

    board_names_by_stock = load_easy_tdx_board_names_by_stock(args.board_cache_dir)
    known_at = datetime.now(CN_TZ).isoformat(timespec="seconds")
    backup_path = ""
    with sqlite3.connect(source_db) as conn:
        result = build_backfill_rows(conn, board_names_by_stock, known_at=known_at)
        inserted = 0
        if args.apply and result.rows:
            if not args.no_backup:
                backup_path = str(backup_sqlite_file(source_db))
            inserted = apply_backfill_rows(conn, result.rows)
            conn.commit()

    print(
        json.dumps(
            {
                "ok": True,
                "source_db": str(source_db),
                "board_cache_dir": str(args.board_cache_dir),
                "apply": bool(args.apply),
                "backup_path": backup_path,
                "mapped_stock_count": len(board_names_by_stock),
                "stock_events": result.stock_events,
                "coverable_events": result.coverable_events,
                "unmapped_events": result.unmapped_events,
                "missing_board_links": len(result.rows),
                "inserted_links": inserted,
                "skipped_existing_name": result.skipped_existing_name,
                "skipped_code_conflict": result.skipped_code_conflict,
                "top_unmapped_stocks": [
                    {"code": code, "name": name, "links": count}
                    for (code, name), count in result.unmapped_stocks.most_common(30)
                ],
                "sample_rows": result.rows[: max(0, int(args.sample_limit))],
            },
            ensure_ascii=False,
        )
    )
    return 0


def build_backfill_rows(
    conn: sqlite3.Connection,
    board_names_by_stock: dict[str, list[dict[str, Any]]],
    *,
    known_at: str,
) -> BackfillResult:
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        stock_links = conn.execute(
            """
            SELECT event_id, code, name, relevance, impact
            FROM event_entity_links
            WHERE lower(entity_type) = 'stock'
            ORDER BY event_id, code
            """
        ).fetchall()
        existing_links = conn.execute(
            """
            SELECT event_id, entity_type, code, name
            FROM event_entity_links
            WHERE lower(entity_type) IN ('sector', 'theme')
            """
        ).fetchall()
    finally:
        conn.row_factory = previous_factory

    existing_names: dict[str, set[str]] = defaultdict(set)
    existing_sector_codes: dict[str, set[str]] = defaultdict(set)
    for row in existing_links:
        event_id = str(row["event_id"] or "").strip()
        name = str(row["name"] or "").strip()
        code = str(row["code"] or "").strip()
        entity_type = str(row["entity_type"] or "").strip().lower()
        if event_id and name:
            existing_names[event_id].add(name)
        if event_id and code and entity_type == "sector":
            existing_sector_codes[event_id].add(code)

    planned_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    stock_events: set[str] = set()
    coverable_events: set[str] = set()
    unmapped_stocks: Counter[tuple[str, str]] = Counter()
    skipped_existing_name = 0
    skipped_code_conflict = 0

    for row in stock_links:
        event_id = str(row["event_id"] or "").strip()
        stock_code = normalize_stock_code(row["code"])
        stock_name = str(row["name"] or "").strip()
        if not event_id or not stock_code:
            continue
        stock_events.add(event_id)
        boards = board_names_by_stock.get(stock_code, [])
        if not boards:
            unmapped_stocks[(stock_code, stock_name)] += 1
            continue
        coverable_events.add(event_id)
        for board in boards:
            board_name = str(board.get("name") or "").strip()
            board_code = str(board.get("code") or "").strip() or board_name
            level = int(board.get("level") or 0)
            if not board_name or not board_code:
                continue
            if board_name in existing_names[event_id]:
                skipped_existing_name += 1
                continue
            if board_code in existing_sector_codes[event_id]:
                skipped_code_conflict += 1
                continue
            key = (event_id, board_code)
            relevance = max(0.55, min(0.86, float(row["relevance"] or 0) * 0.8))
            impact = max(0.5, min(0.82, float(row["impact"] or 0) * 0.8))
            current = planned_by_key.get(key)
            if current is not None:
                current["relevance"] = max(float(current["relevance"]), relevance)
                current["impact"] = max(float(current["impact"]), impact)
                continue
            planned_by_key[key] = {
                "event_id": event_id,
                "entity_type": "sector",
                "code": board_code,
                "name": board_name,
                "role": f"easy_tdx申万{level}级/stock:{stock_code}",
                "relevance": relevance,
                "impact": impact,
                "known_at": known_at,
            }
            existing_names[event_id].add(board_name)
            existing_sector_codes[event_id].add(board_code)

    rows = sorted(planned_by_key.values(), key=lambda item: (item["event_id"], item["code"], item["name"]))
    return BackfillResult(
        rows=rows,
        stock_events=len(stock_events),
        coverable_events=len(coverable_events),
        unmapped_events=len(stock_events - coverable_events),
        skipped_existing_name=skipped_existing_name,
        skipped_code_conflict=skipped_code_conflict,
        unmapped_stocks=unmapped_stocks,
    )


def apply_backfill_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    cursor = conn.executemany(
        """
        INSERT OR IGNORE INTO event_entity_links (
          event_id, entity_type, code, name, role, relevance, impact, known_at
        ) VALUES (
          :event_id, :entity_type, :code, :name, :role, :relevance, :impact, :known_at
        )
        """,
        rows,
    )
    return int(cursor.rowcount or 0)


def backup_sqlite_file(path: Path) -> Path:
    timestamp = datetime.now(CN_TZ).strftime("%Y%m%dT%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak-{timestamp}")
    shutil.copy2(path, backup_path)
    return backup_path


if __name__ == "__main__":
    raise SystemExit(main())
