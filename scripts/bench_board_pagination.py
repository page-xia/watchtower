"""实测首页活跃股榜单翻页的载荷构建耗时（本地复现用，不进生产链路）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\bench_board_pagination.py [--pages 4] [--sort activity]

逐页调用 service.terminal()，打印 source_status 里的分阶段耗时与整包大小，
用于定位翻页时哪些阶段是页无关的重复开销。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import service  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=4)
    parser.add_argument("--page-size", type=int, default=40)
    parser.add_argument("--sort", default="activity")
    parser.add_argument("--repeat", type=int, default=2, help="每页重复次数，观察缓存命中后的耗时")
    args = parser.parse_args()

    print(f"trade context warming ...")
    t0 = time.perf_counter()
    payload = service.terminal(sort=args.sort, page=1, page_size=args.page_size)
    warm_ms = (time.perf_counter() - t0) * 1000
    status = payload.source_status
    print(f"warmup page=1: {warm_ms:.0f}ms total={status.get('board_total')} stages={json.dumps(status.get('terminal_stage_elapsed_ms'), ensure_ascii=False)}")

    for page in range(1, args.pages + 1):
        for attempt in range(1, args.repeat + 1):
            t0 = time.perf_counter()
            payload = service.terminal(sort=args.sort, page=page, page_size=args.page_size)
            elapsed = (time.perf_counter() - t0) * 1000
            status = payload.source_status
            body = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)
            stages = status.get("terminal_stage_elapsed_ms") or {}
            stage_text = " ".join(f"{k}={v}" for k, v in stages.items())
            print(
                f"page={page} try={attempt}: {elapsed:7.1f}ms "
                f"payload_kb={len(body) // 1024} "
                f"mini_missing={status.get('stock_mini_chart_missing_count')} "
                f"total_ms={status.get('terminal_payload_elapsed_ms')} | {stage_text}"
            )


if __name__ == "__main__":
    main()
