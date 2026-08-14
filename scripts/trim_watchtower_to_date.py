"""把日内盯盘 SQLite 瘦身为「仅保留指定交易日」的致密新库。

用途：用户只需要当日数据。旧库可能膨胀到几十 GB（历史高频行从未回收），
导致单票查询退化为大文件随机页读。本脚本：

- 从源库读取表结构与索引 DDL（保持口径一致）；
- 把所有含 trade_date 的表按 ``--date`` 过滤复制（默认今天）；
- 无 trade_date 的小表（schema_meta / research_runs）整表复制；
- 新库开启 auto_vacuum=INCREMENTAL 与 WAL，索引在批量复制后重建；
- 源库只读，不做任何修改；切换（改名）由调用方在服务停止后执行。

Usage:
    python scripts/trim_watchtower_to_date.py --date 20260813
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "runtime" / "intraday_watchtower.sqlite"
DST = ROOT / "data" / "runtime" / "intraday_watchtower_trimmed.sqlite"

TABLES = (
    "schema_meta",
    "market_trajectory",
    "signal_transitions",
    "data_quality_events",
    "strategy_events",
    "daily_regimes",
    "research_runs",
    "trade_outcomes",
    "data_manifests",
    "stock_features",
    "sector_trajectory",
    "order_flow_trajectory",
)


def _table_ddl(src: sqlite3.Connection) -> dict[str, str]:
    rows = src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(name): str(sql) for name, sql in rows}


def _index_ddl(src: sqlite3.Connection) -> list[str]:
    rows = src.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    return [str(sql) for (sql,) in rows]


def _has_trade_date(conn: sqlite3.Connection, table: str) -> bool:
    return any(str(row[1]) == "trade_date" for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="保留的交易日 YYYYMMDD，默认今天")
    args = parser.parse_args()
    keep_date = str(args.date).strip()
    if not keep_date.isdigit() or len(keep_date) != 8:
        print(f"无效日期: {keep_date}")
        return 1
    if not SRC.exists():
        print(f"源库不存在: {SRC}")
        return 1
    if DST.exists():
        DST.unlink()

    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(str(DST), timeout=60)
    dst.execute("PRAGMA auto_vacuum=2")  # INCREMENTAL，建表前设置
    dst.execute("PRAGMA journal_mode=OFF")  # 批量复制期不写日志，建完再开 WAL
    dst.execute("PRAGMA synchronous=OFF")

    ddl_by_name = _table_ddl(src)
    with dst:
        for table in TABLES:
            ddl = ddl_by_name.get(table)
            if ddl:
                dst.execute(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))

    started = time.perf_counter()
    src.execute("ATTACH DATABASE ? AS dstdb", (str(DST),))
    try:
        for table in TABLES:
            if table not in ddl_by_name:
                continue
            t0 = time.perf_counter()
            if _has_trade_date(src, table):
                sql = f"INSERT INTO dstdb.{table} SELECT * FROM {table} WHERE trade_date = ?"
                cursor = src.execute(sql, (keep_date,))
            else:
                cursor = src.execute(f"INSERT INTO dstdb.{table} SELECT * FROM {table}")
            copied = max(0, int(cursor.rowcount or 0))
            print(f"[copy] {table}: {copied} rows in {time.perf_counter() - t0:.1f}s", flush=True)
    finally:
        src.commit()  # ATTACH 的写入挂在源连接事务里，DETACH 前必须先提交
        src.execute("DETACH DATABASE dstdb")
    dst.commit()

    for sql in _index_ddl(src):
        sql = sql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
        t0 = time.perf_counter()
        with dst:
            dst.execute(sql)
        print(f"[index] {sql.split('INDEX IF NOT EXISTS ')[1].split(' ')[0]} in {time.perf_counter() - t0:.1f}s", flush=True)

    dst.execute("PRAGMA journal_mode=WAL")
    integrity = dst.execute("PRAGMA integrity_check(100)").fetchone()[0]
    size_mb = DST.stat().st_size / 1e6
    print(f"[done] keep_date={keep_date} size={size_mb:.0f}MB integrity={integrity} elapsed={time.perf_counter() - started:.1f}s", flush=True)
    if integrity != "ok":
        print("[fail] 新库完整性检查未通过，请勿切换")
        return 2
    print("[next] 停止服务后：把源库改名备份，再把本库改名为 intraday_watchtower.sqlite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
