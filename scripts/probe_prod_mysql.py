"""带重试的生产 MySQL 证据采集：崩溃循环中抓住存活窗口执行诊断查询。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from prod_db import connect  # noqa: E402

QUERIES = [
    (
        "log_output",
        "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('log_output','log_queries_not_using_indexes','innodb_buffer_pool_instances','performance_schema')",
    ),
    (
        "table_sizes",
        "SELECT table_name, table_rows, ROUND(data_length/1048576,1) AS data_mb, ROUND(index_length/1048576,1) AS index_mb "
        "FROM information_schema.tables WHERE table_schema=DATABASE() ORDER BY data_length DESC",
    ),
    ("processlist", "SELECT id, user, host, db, command, time, state, LEFT(info,160) AS info FROM information_schema.processlist ORDER BY time DESC LIMIT 30"),
    (
        "slow_log_recent",
        "SELECT start_time, query_time, lock_time, rows_sent, rows_examined, LEFT(sql_text,200) AS sql "
        "FROM mysql.slow_log WHERE start_time > NOW() - INTERVAL 2 HOUR ORDER BY start_time DESC LIMIT 20",
    ),
]


def run_with_retry(label: str, sql: str, attempts: int = 12) -> None:
    for attempt in range(attempts):
        try:
            conn = connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cols = [d[0] for d in cur.description] if cur.description else []
                    rows = cur.fetchall()
                conn.commit()
            finally:
                conn.close()
            print(f"\n=== {label} (attempt {attempt + 1}) ===")
            if cols:
                print(" | ".join(cols))
            for row in rows:
                print(" | ".join(str(v)[:200] for v in row))
            if not rows:
                print("(no rows)")
            return
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            print(f"[{label}] attempt {attempt + 1} failed: {msg[:100]}", flush=True)
            time.sleep(5)
    print(f"\n=== {label} FAILED after {attempts} attempts ===")


for label, sql in QUERIES:
    run_with_retry(label, sql)
