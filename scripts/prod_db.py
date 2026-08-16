"""直连生产 CynosDB MySQL 做运维诊断（读取 ts2db_config.yaml 的 db_prod）。

用法: .\\.venv\\Scripts\\python.exe scripts\\prod_db.py <sql>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import pymysql

CONFIG = Path(__file__).resolve().parent.parent / "ts2db_config.yaml"


def connect() -> pymysql.connections.Connection:
    text = CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^db_prod:\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise SystemExit("db_prod not found in ts2db_config.yaml")
    url = match.group(1)
    # 密码里可能含 @，从右往左找主机边界
    creds, _, hostpart = url.removeprefix("mysql://").rpartition("@")
    user, _, pwd = creds.partition(":")
    parsed = urlparse(f"mysql://x@{hostpart}")
    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=user,
        password=pwd,
        database=(parsed.path or "").lstrip("/") or None,
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=30,
    )


def main() -> None:
    sql = " ".join(sys.argv[1:]).strip() or "SELECT 1"
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description:
                cols = [d[0] for d in cur.description]
                print("\t".join(cols))
                for row in cur.fetchall():
                    print("\t".join(str(v)[:200] for v in row))
            else:
                print(f"rows affected: {cur.rowcount}")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
