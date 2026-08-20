"""监测生产 CynosDB 实例可用性：每 10 秒探测一次，持续 ~5 分钟。

记录: 时间 | 结果 | Uptime(若成功) | Threads_connected | 建连耗时
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from prod_db import connect  # noqa: E402

DURATION_S = 300
INTERVAL_S = 10

start = time.monotonic()
rows = []
while time.monotonic() - start < DURATION_S:
    ts = datetime.now().strftime("%H:%M:%S")
    t0 = time.monotonic()
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute(
                "SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime','Threads_connected','Connections','Aborted_clients')"
            )
            status = dict(cur.fetchall())
        conn.close()
        elapsed = time.monotonic() - t0
        line = (
            f"{ts} OK    uptime={status.get('Uptime', '?'):>6}s "
            f"threads={status.get('Threads_connected', '?'):>3} "
            f"conn_total={status.get('Connections', '?'):>5} "
            f"aborted={status.get('Aborted_clients', '?'):>3} "
            f"connect={elapsed:.2f}s"
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        msg = str(exc)
        if len(msg) > 90:
            msg = msg[:90]
        line = f"{ts} FAIL  connect={elapsed:.2f}s {msg}"
    print(line, flush=True)
    rows.append(line)
    time.sleep(INTERVAL_S)

Path("logs/prod_mysql_watch.log").write_text("\n".join(rows) + "\n", encoding="utf-8")
print("saved -> logs/prod_mysql_watch.log")
