"""暗盘个股摘要按需补数：履约生产容器登记的补数请求。

链路（2026-08-18，见 app/eod_store.py 与 AGENTS.md）：
  生产容器读快照未覆盖的个股 → 登记请求（dark_pool/eod_requests）
  → 本脚本用本地 MySQL（watchtower_eod）计算个股摘要
  → 推送单票文档（dark_pool_stock/<code>）→ 生产读侧回退读取。

不拉 Tushare，只读本地已落库的 EOD 数据；因此请求的股票只有在本地库
已有其 moneyflow/daily_basic 等数据时才能履约（一般跑过 ingest 即覆盖）。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\fulfill_dark_pool_requests.py            # 履约一遍后退出
  .\\.venv\\Scripts\\python.exe scripts\\fulfill_dark_pool_requests.py --loop     # 常驻轮询（默认 20s）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.eod_store import REQUEST_KEY, REQUEST_NAMESPACE, STOCK_NAMESPACE  # noqa: E402

# 请求在队列里挂超过该时长且本地始终无数据 → 丢弃，避免无效代码永久占位
REQUEST_EXPIRE_SECONDS = 24 * 3600


def _cloud_store():
    from app.cloud_persistence import CloudBaseNoSqlStateStore

    if not settings.cloudbase_env_id or not settings.cloudbase_api_token:
        return None
    return CloudBaseNoSqlStateStore(
        env_id=settings.cloudbase_env_id,
        token=settings.cloudbase_api_token,
        collection=settings.cloudbase_state_collection,
        instance=settings.cloudbase_database_instance,
        database=settings.cloudbase_database_name,
        base_url=settings.cloudbase_api_base_url or None,
        timeout=settings.cloudbase_api_timeout_seconds,
    )


def fulfill_once() -> int:
    cloud = _cloud_store()
    if cloud is None:
        print("缺少 WATCH_CLOUDBASE_ENV_ID / WATCH_CLOUDBASE_API_TOKEN，无法履约")
        return 1
    doc = cloud.get_json(REQUEST_NAMESPACE, REQUEST_KEY, {}) or {}
    raw_codes = doc.get("codes") if isinstance(doc, dict) else None
    requests = dict(raw_codes) if isinstance(raw_codes, dict) else {}
    now = int(time.time())
    # 过期请求直接丢弃
    requests = {
        str(code).zfill(6): int(ts or 0)
        for code, ts in requests.items()
        if str(code).zfill(6).isdigit() and now - int(ts or 0) < REQUEST_EXPIRE_SECONDS
    }
    if not requests:
        if raw_codes:
            cloud.delete_json(REQUEST_NAMESPACE, REQUEST_KEY)
        print("无待履约请求")
        return 0

    from app.dark_pool import DarkPoolMonitor
    from app.eod_store import build_eod_store

    monitor = DarkPoolMonitor(
        settings,
        context_provider=lambda: None,  # 名称/板块由生产读侧回填，单票文档只存数值
        eod_store=build_eod_store(settings),
    )
    done: list[str] = []
    for code in sorted(requests):
        summary = monitor._stock_eod(code)
        if summary.get("eod_available"):
            cloud.set_json(STOCK_NAMESPACE, code, summary)
            done.append(code)
            print(f"已推送 {code}（trade_date={summary.get('trade_date') or '--'}）")
        else:
            print(f"{code} 本地无数据，保留请求（{summary.get('note') or '无摘要'}）")

    remaining = {code: ts for code, ts in requests.items() if code not in done}
    if remaining:
        cloud.set_json(REQUEST_NAMESPACE, REQUEST_KEY, {"codes": remaining})
    else:
        cloud.delete_json(REQUEST_NAMESPACE, REQUEST_KEY)
    print(f"履约完成：成功 {len(done)} / 待处理 {len(requests)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="暗盘个股摘要按需补数履约")
    parser.add_argument("--loop", action="store_true", help="常驻轮询履约，Ctrl+C 退出")
    parser.add_argument("--interval", type=float, default=20.0, help="--loop 轮询间隔秒数（默认 20）")
    args = parser.parse_args()

    if not args.loop:
        return fulfill_once()
    print(f"进入常驻履约循环（每 {args.interval:g}s 轮询，Ctrl+C 退出）")
    try:
        while True:
            try:
                fulfill_once()
            except Exception as exc:  # noqa: BLE001
                print(f"履约异常（继续轮询）：{exc}")
            time.sleep(max(2.0, args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
