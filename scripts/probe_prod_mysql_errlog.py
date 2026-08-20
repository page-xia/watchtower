"""抓 MySQL 错误日志（performance_schema.error_log）找崩溃原因。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from prod_db import connect  # noqa: E402

QUERIES = [
    (
        "error_log_recent",
        "SELECT LOGGED, PRIO, ERROR_CODE, LEFT(SUBSYSTEM,20) AS subsys, LEFT(DATA,300) AS data "
        "FROM performance_schema.error_log ORDER BY LOGGED DESC LIMIT 60",
    ),
    (
        "version_comment",
        "SHOW VARIABLES WHERE Variable_name IN ('version_comment','datadir')",
    ),
]


def run_with_retry(label: str, sql: str, attempts: int = 15) -> None:
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
                print(" | ".join(str(v)[:320] for v in row))
            if not rows:
                print("(no rows)")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] attempt {attempt + 1} failed: {str(exc)[:100]}", flush=True)
            time.sleep(5)
    print(f"\n=== {label} FAILED after {attempts} attempts ===")


for label, sql in QUERIES:
    run_with_retry(label, sql)
