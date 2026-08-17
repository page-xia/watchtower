# -*- coding: utf-8 -*-
"""第三轮：InnoDB 内存、重启迹象、慢日志表。"""
from __future__ import annotations

import re

import pymysql

from scripts.diag_db_memory import CONN


def main() -> None:
    conn = pymysql.connect(**CONN)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS LIKE 'Uptime'")
            print(f"Uptime now = {cur.fetchone()}")

            print("\n=== SHOW ENGINE INNODB STATUS（内存相关行）===")
            try:
                cur.execute("SHOW ENGINE INNODB STATUS")
                text = cur.fetchone()[2]
                keep = re.compile(
                    r"(Total .*memory allocated|Dictionary memory|Buffer pool size|Free buffers|Database pages|"
                    r"Old database pages|Modified db pages|readahead|History list length|Log sequence|"
                    r"BUFFER POOL AND MEMORY|row operations|queries inside|RW-|semaphore|OS WAIT|spin)",
                    re.IGNORECASE,
                )
                for line in text.splitlines():
                    if keep.search(line):
                        print(" ", line.strip())
            except Exception as exc:
                print(f"不可用: {exc}")

            print("\n=== mysql.slow_log（如可读）===")
            try:
                cur.execute("SELECT COUNT(*), MIN(start_time), MAX(start_time) FROM mysql.slow_log")
                print(f"  slow_log rows={cur.fetchone()}")
                cur.execute(
                    "SELECT start_time, query_time, rows_examined, rows_sent, LEFT(sql_text,300) "
                    "FROM mysql.slow_log ORDER BY start_time DESC LIMIT 10"
                )
                for r in cur.fetchall():
                    print(f"\n  {r[0]} query_time={r[1]} examined={r[2]} sent={r[3]}")
                    print(f"  {r[4]}")
            except Exception as exc:
                print(f"不可用: {exc}")

            print("\n=== 每个连接的内存上限估算 ===")
            cur.execute(
                "SELECT @@sort_buffer_size, @@join_buffer_size, @@read_buffer_size, @@read_rnd_buffer_size, "
                "@@thread_stack, @@binlog_cache_size, @@tmp_table_size, @@max_heap_table_size, @@net_buffer_length, @@max_allowed_packet"
            )
            vals = cur.fetchone()
            names = ["sort", "join", "read", "read_rnd", "thread_stack", "binlog_cache", "tmp_table", "max_heap", "net_buffer", "max_allowed_packet"]
            per_conn = 0
            for n, v in zip(names, vals):
                mb = v / 1024 / 1024
                print(f"  {n} = {mb:.2f} MB")
                if n != "max_allowed_packet":
                    per_conn += v
            print(f"  单连接理论上限 ≈ {per_conn / 1024 / 1024:.1f} MB（不含 max_allowed_packet 场景）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
