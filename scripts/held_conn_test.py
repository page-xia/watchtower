"""判别实验：长持一条连接，看 MySQL 是「崩溃」还是「自动暂停」。

- 崩溃：长持连接会在重启时被打断（查询报错）。
- 自动暂停：只要持有连接，uptime 应持续增长超过 34s。
每 5 秒在长持连接上查一次 uptime，同时每 10 秒开一条短连接交叉验证。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from prod_db import connect  # noqa: E402


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


held = None
held_since = None
for attempt in range(10):
    try:
        held = connect()
        held_since = time.monotonic()
        print(f"{ts()} held connection established", flush=True)
        break
    except Exception as exc:  # noqa: BLE001
        print(f"{ts()} held connect failed: {str(exc)[:80]}", flush=True)
        time.sleep(3)

if held is None:
    raise SystemExit("could not establish held connection")

start = time.monotonic()
tick = 0
while time.monotonic() - start < 120:
    tick += 1
    # 长持连接查询
    try:
        with held.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS LIKE 'Uptime'")
            row = cur.fetchone()
        print(
            f"{ts()} HELD ok uptime={row[1]:>5}s held_for={time.monotonic() - held_since:5.1f}s",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"{ts()} HELD BROKEN after {time.monotonic() - held_since:.1f}s: {str(exc)[:90]}",
            flush=True,
        )
        try:
            held = connect()
            held_since = time.monotonic()
            print(f"{ts()} held re-established", flush=True)
        except Exception as exc2:  # noqa: BLE001
            print(f"{ts()} held re-establish failed: {str(exc2)[:80]}", flush=True)
    # 交叉验证短连接
    if tick % 2 == 0:
        try:
            probe = connect()
            with probe.cursor() as cur:
                cur.execute("SHOW GLOBAL STATUS LIKE 'Uptime'")
                prow = cur.fetchone()
            probe.close()
            print(f"{ts()} probe ok uptime={prow[1]:>5}s", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{ts()} probe FAIL: {str(exc)[:80]}", flush=True)
    time.sleep(5)

try:
    held.close()
except Exception:  # noqa: BLE001
    pass
