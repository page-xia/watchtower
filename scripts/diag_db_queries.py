# -*- coding: utf-8 -*-
"""线上库查询模式与索引只读诊断（第二轮）。"""
from __future__ import annotations

import pymysql

from scripts.diag_db_memory import CONN

TABLES = ["message_event_links", "message_topics", "message_events", "message_sync_runs", "message_evidence_cache"]


def main() -> None:
    conn = pymysql.connect(**CONN)
    try:
        with conn.cursor() as cur:
            print("=== 各表索引 ===")
            for t in TABLES:
                print(f"\n-- {t}")
                cur.execute(f"SHOW INDEX FROM `{t}`")
                for r in cur.fetchall():
                    # Table, Non_unique, Key_name, Seq, Column, ...
                    print(f"  key={r[2]} seq={r[3]} col={r[4]} unique={'Y' if r[1] == 0 else 'N'}")

            print("\n=== 语句摘要 Top 15（按总耗时）===")
            try:
                cur.execute(
                    "SELECT SCHEMA_NAME, DIGEST_TEXT, COUNT_STAR, "
                    "ROUND(SUM_TIMER_WAIT/1e12,2) AS total_s, "
                    "ROUND(AVG_TIMER_WAIT/1e9,1) AS avg_ms, "
                    "SUM_ROWS_EXAMINED, SUM_ROWS_SENT, SUM_CREATED_TMP_DISK_TABLES, SUM_SORT_ROWS "
                    "FROM performance_schema.events_statements_summary_by_digest "
                    "ORDER BY SUM_TIMER_WAIT DESC LIMIT 15"
                )
                for r in cur.fetchall():
                    print(f"\n  calls={r[2]} total={r[3]}s avg={r[4]}ms examined={r[5]} sent={r[6]} tmpdisk={r[7]} sortrows={r[8]}")
                    print(f"  {(r[1] or '')[:400]}")
            except Exception as exc:
                print(f"不可用: {exc}")

            print("\n=== 全表扫描语句 Top 10 ===")
            try:
                cur.execute(
                    "SELECT DIGEST_TEXT, COUNT_STAR, SUM_NO_INDEX_USED, SUM_NO_GOOD_INDEX_USED, "
                    "SUM_ROWS_EXAMINED, ROUND(AVG_TIMER_WAIT/1e9,1) AS avg_ms "
                    "FROM performance_schema.events_statements_summary_by_digest "
                    "WHERE SUM_NO_INDEX_USED > 0 OR SUM_NO_GOOD_INDEX_USED > 0 "
                    "ORDER BY SUM_ROWS_EXAMINED DESC LIMIT 10"
                )
                for r in cur.fetchall():
                    print(f"\n  calls={r[1]} no_index={r[2]} no_good_index={r[3]} examined={r[4]} avg={r[5]}ms")
                    print(f"  {(r[0] or '')[:400]}")
            except Exception as exc:
                print(f"不可用: {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
