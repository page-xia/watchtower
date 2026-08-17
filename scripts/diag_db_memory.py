# -*- coding: utf-8 -*-
"""线上 CynosDB MySQL 内存占用只读诊断。

用法: .venv\\Scripts\\python.exe scripts\\diag_db_memory.py
只执行 SHOW / information_schema / sys 只读查询，不写任何数据。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pymysql
import yaml


def _load_conn() -> dict:
    """从 ts2db_config.yaml 的 db_prod 读取连接（密码允许含 @，按最后一个 @ 切分）。"""
    config = yaml.safe_load(Path(__file__).resolve().parent.parent.joinpath("ts2db_config.yaml").read_text(encoding="utf-8"))
    raw = str(config["db_prod"])
    parsed = urlparse(raw)
    # urlparse 无法处理密码中的 @，手工切分
    auth_host = raw.split("://", 1)[1]
    auth, hostpart = auth_host.rsplit("@", 1)
    user, password = auth.split(":", 1)
    host, _, rest = hostpart.partition(":")
    port, _, database = rest.partition("/")
    return dict(
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=database,
        connect_timeout=10,
        read_timeout=30,
        charset="utf8mb4",
    )


CONN = _load_conn()

VARIABLES = [
    "version",
    "innodb_buffer_pool_size",
    "innodb_buffer_pool_instances",
    "max_connections",
    "tmp_table_size",
    "max_heap_table_size",
    "sort_buffer_size",
    "join_buffer_size",
    "read_buffer_size",
    "read_rnd_buffer_size",
    "binlog_cache_size",
    "thread_cache_size",
    "table_open_cache",
    "performance_schema",
    "innodb_log_buffer_size",
    "innodb_log_file_size",
]

STATUS = [
    "Uptime",
    "Connections",
    "Max_used_connections",
    "Threads_connected",
    "Threads_running",
    "Aborted_connects",
    "Aborted_clients",
    "Innodb_buffer_pool_pages_total",
    "Innodb_buffer_pool_pages_free",
    "Innodb_buffer_pool_pages_dirty",
    "Innodb_buffer_pool_pages_data",
    "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_reads",
    "Innodb_buffer_pool_wait_free",
    "Innodb_history_list_length",
    "Innodb_rows_read",
    "Created_tmp_tables",
    "Created_tmp_disk_tables",
    "Sort_merge_passes",
    "Sort_rows",
    "Opened_tables",
    "Open_tables",
    "Slow_queries",
    "Questions",
    "Select_scan",
    "Select_full_join",
]


def fetch_map(cur, sql: str, keys: list[str]) -> dict[str, str]:
    cur.execute(sql)
    rows = cur.fetchall()
    lowered = {k.lower(): k for k in keys}
    out: dict[str, str] = {}
    for name, value in rows:
        if str(name).lower() in lowered:
            out[str(name)] = str(value)
    return out


def main() -> None:
    conn = pymysql.connect(**CONN)
    try:
        with conn.cursor() as cur:
            print("=== 连接信息 ===")
            cur.execute("SELECT VERSION(), @@hostname, @@port")
            row = cur.fetchone()
            print(f"version={row[0]} host={row[1]} port={row[2]}")

            print("\n=== 关键参数 ===")
            variables = fetch_map(cur, "SHOW GLOBAL VARIABLES", VARIABLES)
            for k in VARIABLES:
                for name, value in variables.items():
                    if name.lower() == k:
                        v = value
                        if k.endswith("size") or k.endswith("cache"):
                            try:
                                v = f"{int(value) / 1024 / 1024:.1f} MB ({value})"
                            except ValueError:
                                pass
                        print(f"{k} = {v}")

            print("\n=== 全局状态 ===")
            status = fetch_map(cur, "SHOW GLOBAL STATUS", STATUS)
            for k in STATUS:
                for name, value in status.items():
                    if name.lower() == k.lower():
                        print(f"{name} = {value}")

            # buffer pool 命中率
            try:
                rr = float(status.get("Innodb_buffer_pool_read_requests", 0))
                rd = float(status.get("Innodb_buffer_pool_reads", 0))
                if rr:
                    print(f"\nBuffer pool 命中率 = {(1 - rd / rr) * 100:.4f}%")
                pages_total = float(status.get("Innodb_buffer_pool_pages_total", 0))
                pages_free = float(status.get("Innodb_buffer_pool_pages_free", 0))
                if pages_total:
                    print(f"Buffer pool 已用页占比 = {(1 - pages_free / pages_total) * 100:.2f}%")
            except Exception:
                pass

            print("\n=== performance_schema 全局内存 ===")
            try:
                cur.execute(
                    "SELECT event_name, current_alloc, high_alloc FROM sys.memory_global_by_current_bytes "
                    "ORDER BY current_count_used DESC LIMIT 0"
                )
            except Exception:
                pass
            try:
                cur.execute("SELECT * FROM sys.memory_global_total")
                print(f"sys.memory_global_total: {cur.fetchone()}")
            except Exception as exc:
                print(f"sys.memory_global_total 不可用: {exc}")

            print("\n=== 内存事件 Top 15 (sys.memory_global_by_current_bytes) ===")
            try:
                cur.execute(
                    "SELECT event_name, current_alloc, high_alloc "
                    "FROM sys.memory_global_by_current_bytes LIMIT 15"
                )
                for r in cur.fetchall():
                    print(f"  {r[0]}: current={r[1]} high={r[2]}")
            except Exception as exc:
                print(f"不可用: {exc}")

            print("\n=== 线程内存 Top 10 (sys.memory_by_thread_by_current_bytes) ===")
            try:
                cur.execute(
                    "SELECT thread_id, user, current_allocated, total_allocated "
                    "FROM sys.memory_by_thread_by_current_bytes LIMIT 10"
                )
                for r in cur.fetchall():
                    print(f"  tid={r[0]} user={r[1]} current={r[2]} total={r[3]}")
            except Exception as exc:
                print(f"不可用: {exc}")

            print("\n=== processlist 状态分布 ===")
            cur.execute(
                "SELECT COALESCE(command,'NULL'), COALESCE(state,'NULL'), COUNT(*), MAX(time) "
                "FROM information_schema.processlist GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20"
            )
            for r in cur.fetchall():
                print(f"  command={r[0]} state={r[1]} count={r[2]} max_time={r[3]}s")

            print("\n=== 用户/来源分布 ===")
            cur.execute(
                "SELECT user, SUBSTRING_INDEX(host,':',1), COUNT(*) "
                "FROM information_schema.processlist GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15"
            )
            for r in cur.fetchall():
                print(f"  user={r[0]} host={r[1]} count={r[2]}")

            print("\n=== 长事务 ===")
            try:
                cur.execute(
                    "SELECT trx_id, trx_state, trx_started, TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_s, "
                    "trx_rows_locked, trx_rows_modified FROM information_schema.innodb_trx "
                    "ORDER BY trx_started ASC LIMIT 10"
                )
                rows = cur.fetchall()
                if not rows:
                    print("  (无活跃事务)")
                for r in rows:
                    print(f"  trx={r[0]} state={r[1]} started={r[2]} age={r[3]}s locked={r[4]} modified={r[5]}")
            except Exception as exc:
                print(f"不可用: {exc}")

            print("\n=== 库内表大小 Top 20 ===")
            cur.execute(
                "SELECT table_name, engine, table_rows, "
                "ROUND(data_length/1024/1024,1) AS data_mb, ROUND(index_length/1024/1024,1) AS index_mb "
                "FROM information_schema.tables WHERE table_schema = DATABASE() "
                "ORDER BY data_length + index_length DESC LIMIT 20"
            )
            for r in cur.fetchall():
                print(f"  {r[0]} engine={r[1]} rows={r[2]} data={r[3]}MB index={r[4]}MB")

            print("\n=== 慢查询相关 ===")
            for k in ("Slow_queries", "Select_scan", "Select_full_join"):
                if k in status:
                    print(f"  {k} = {status[k]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
