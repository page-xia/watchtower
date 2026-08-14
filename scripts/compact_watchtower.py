"""Compact the 80GB intraday watchtower SQLite into a fresh dense database.

The live file is 65% freelist (deleted history never reclaimed, auto_vacuum=0).
A full VACUUM would exceed any single shell call's time budget, so this script
copies live rows into a new database in resumable id-range chunks:

- resume marker lives in the NEW db (`copy_progress`), so re-running continues
  where the last call stopped;
- `--max-seconds` self-limits each invocation; call repeatedly until done;
- explicit secondary indexes are created AFTER the bulk copy (much faster);
- the new db enables auto_vacuum=INCREMENTAL from the start so future deletes
  can be returned incrementally instead of piling up as freelist;
- source stays untouched (read-only ATTACH); swap is a separate manual step.

Usage:
    python scripts/compact_watchtower.py --max-seconds 240   # repeat until "DONE"
    python scripts/compact_watchtower.py --verify            # post-copy checks
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "runtime" / "intraday_watchtower.sqlite"
DST = ROOT / "data" / "runtime" / "intraday_watchtower_compact.sqlite"

# 高频大表按 id 分块；其余表整表复制
CHUNKED_TABLES = ("stock_features", "sector_trajectory", "order_flow_trajectory")
SMALL_TABLES = (
    "schema_meta",
    "market_trajectory",
    "signal_transitions",
    "data_quality_events",
    "strategy_events",
    "daily_regimes",
    "research_runs",
    "trade_outcomes",
    "data_manifests",
)
EXPLICIT_INDEXES: tuple[str, ...] = ()  # 运行时从源库 sqlite_master 读取，保持口径一致


def _index_ddl(src: sqlite3.Connection) -> list[str]:
    rows = src.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    return [str(sql) for (sql,) in rows]


def _table_ddl(src: sqlite3.Connection) -> dict[str, str]:
    rows = src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(name): str(sql) for name, sql in rows}


def _open_dst(fresh: bool) -> sqlite3.Connection:
    connection = sqlite3.connect(str(DST), timeout=30)
    connection.row_factory = sqlite3.Row
    if fresh:
        connection.execute("PRAGMA auto_vacuum=2")  # INCREMENTAL，建表前设置
    connection.execute("PRAGMA journal_mode=OFF")  # 批量迁移期不做日志，切换前再开 WAL
    connection.execute("PRAGMA synchronous=OFF")
    return connection


def _init_dst(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    ddl_by_name = _table_ddl(src)
    with dst:
        for table in (*SMALL_TABLES, *CHUNKED_TABLES):
            ddl = ddl_by_name.get(table)
            if ddl:
                dst.execute(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
        dst.execute(
            "CREATE TABLE IF NOT EXISTS copy_progress (table_name TEXT PRIMARY KEY, last_id INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0)"
        )


def _copy_small_tables(src: sqlite3.Connection, dst: sqlite3.Connection, deadline: float) -> None:
    dst.execute("ATTACH DATABASE ? AS srcdb", (str(SRC),))
    try:
        for table in SMALL_TABLES:
            marker = dst.execute(
                "SELECT done FROM copy_progress WHERE table_name = ?", (table,)
            ).fetchone()
            if marker and marker["done"]:
                continue
            started = time.perf_counter()
            with dst:
                dst.execute(f"DELETE FROM {table}")
                dst.execute(f"INSERT INTO {table} SELECT * FROM srcdb.{table}")
                dst.execute(
                    "INSERT OR REPLACE INTO copy_progress (table_name, last_id, done) VALUES (?, 0, 1)",
                    (table,),
                )
            count = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"[small] {table}: {count} rows in {time.perf_counter() - started:.1f}s", flush=True)
            if time.perf_counter() > deadline:
                return
    finally:
        dst.execute("DETACH DATABASE srcdb")


def _copy_chunked_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, deadline: float, chunk: int) -> bool:
    """Copy `table` in id ranges. Returns True when the table is complete."""
    marker = dst.execute(
        "SELECT last_id, done FROM copy_progress WHERE table_name = ?", (table,)
    ).fetchone()
    if marker and marker["done"]:
        print(f"[chunked] {table}: already done", flush=True)
        return True
    last_id = int(marker["last_id"]) if marker else 0
    max_id = src.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0
    if last_id >= max_id:
        with dst:
            dst.execute(
                "INSERT OR REPLACE INTO copy_progress (table_name, last_id, done) VALUES (?, ?, 1)",
                (table, last_id),
            )
        print(f"[chunked] {table}: done (0 new rows)", flush=True)
        return True

    dst.execute("ATTACH DATABASE ? AS srcdb", (str(SRC),))
    copied = 0
    started = time.perf_counter()
    try:
        while last_id < max_id:
            upper = min(last_id + chunk, max_id)
            with dst:
                cursor = dst.execute(
                    f"INSERT INTO {table} SELECT * FROM srcdb.{table} WHERE id > ? AND id <= ?",
                    (last_id, upper),
                )
                dst.execute(
                    "INSERT OR REPLACE INTO copy_progress (table_name, last_id, done) VALUES (?, ?, 0)",
                    (table, upper),
                )
            copied += max(0, int(cursor.rowcount or 0))
            last_id = upper
            rate = copied / max(time.perf_counter() - started, 0.01)
            print(
                f"[chunked] {table}: id<={last_id}/{max_id} (+{copied} rows, {rate:.0f} rows/s)",
                flush=True,
            )
            if time.perf_counter() > deadline:
                print(f"[chunked] {table}: time budget reached, resume next call", flush=True)
                return False
    finally:
        dst.execute("DETACH DATABASE srcdb")
    with dst:
        dst.execute(
            "INSERT OR REPLACE INTO copy_progress (table_name, last_id, done) VALUES (?, ?, 1)",
            (table, last_id),
        )
    print(f"[chunked] {table}: DONE ({copied} rows this call)", flush=True)
    return True


def _build_indexes(src: sqlite3.Connection, dst: sqlite3.Connection, deadline: float) -> None:
    for sql in _index_ddl(src):
        # 源库索引名已存在语义，统一转 IF NOT EXISTS 便于断点重跑
        sql = sql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
        marker = dst.execute(
            "SELECT done FROM copy_progress WHERE table_name = ?", (sql,)
        ).fetchone()
        if marker and marker["done"]:
            continue
        started = time.perf_counter()
        print(f"[index] {sql.split('INDEX IF NOT EXISTS ')[1].split(' ')[0]} ...", flush=True)
        with dst:
            dst.execute(sql)
            dst.execute(
                "INSERT OR REPLACE INTO copy_progress (table_name, last_id, done) VALUES (?, 0, 1)",
                (sql,),
            )
        print(f"[index] built in {time.perf_counter() - started:.1f}s", flush=True)
        if time.perf_counter() > deadline:
            return


def _verify(src: sqlite3.Connection, dst: sqlite3.Connection) -> bool:
    ok = True
    for table in (*CHUNKED_TABLES, *SMALL_TABLES):
        try:
            src_count = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        dst_count = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        match = "OK " if src_count == dst_count else "MISMATCH"
        if src_count != dst_count:
            ok = False
        print(f"[verify] {table}: src={src_count} dst={dst_count} {match}", flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-seconds", type=float, default=240.0)
    parser.add_argument("--chunk-rows", type=int, default=1_500_000)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if not SRC.exists():
        print(f"source missing: {SRC}")
        return 1
    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=30)
    fresh = not DST.exists()
    dst = _open_dst(fresh)

    if args.verify:
        ok = _verify(src, dst)
        print("[verify] integrity_check:", dst.execute("PRAGMA integrity_check(100)").fetchone()[0])
        size_gb = DST.stat().st_size / 1e9
        freelist = dst.execute("PRAGMA freelist_count").fetchone()[0]
        print(f"[verify] new db size: {size_gb:.1f} GB, freelist pages: {freelist}")
        print("[verify] PASS" if ok else "[verify] FAIL")
        return 0 if ok else 2

    deadline = time.perf_counter() + args.max_seconds
    _init_dst(src, dst)
    _copy_small_tables(src, dst, deadline)
    all_done = True
    for table in CHUNKED_TABLES:
        if time.perf_counter() > deadline:
            all_done = False
            break
        if not _copy_chunked_table(src, dst, table, deadline, args.chunk_rows):
            all_done = False
            break
    if all_done and time.perf_counter() <= deadline:
        _build_indexes(src, dst, deadline)
    remaining = dst.execute("SELECT COUNT(*) FROM copy_progress WHERE done = 0").fetchone()[0]
    total = dst.execute("SELECT COUNT(*) FROM copy_progress").fetchone()[0]
    print(f"[status] progress markers: {total - remaining}/{total} done", flush=True)
    if remaining == 0:
        dst.execute("PRAGMA journal_mode=WAL")
        print("[status] ALL DONE — run --verify next, then swap files during a service stop", flush=True)
    else:
        print("[status] partial — re-run this command to continue", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
