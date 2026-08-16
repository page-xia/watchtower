"""用生产服务环境变量里的 CloudBase token 直连真实 MySQL REST 网关，
本地复现星球消息读取路径（不打印任何密钥）。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.message_store import MessageStore


def load_env_from_detail() -> dict[str, str]:
    raw = Path(".detail.json").read_text(encoding="utf-8", errors="ignore")
    start = raw.find("{")
    data = json.loads(raw[start:])
    env_params = data["data"]["ServerConfig"]["EnvParams"]
    return {str(k): str(v) for k, v in json.loads(env_params).items()}


def main() -> None:
    env = load_env_from_detail()
    token = env.get("WATCH_CLOUDBASE_API_TOKEN", "")
    env_id = env.get("WATCH_CLOUDBASE_ENV_ID", "server-d2g7x597t019f5cb0")
    if not token:
        print("token 未解析到")
        return
    store = MessageStore(
        env_id=env_id,
        token=token,
        instance="default",
        schema=env.get("WATCH_CLOUDBASE_MYSQL_SCHEMA", env_id),
        timeout=3.0,
        cache_seconds=0,
    )
    print("available:", store.available)

    start = time.monotonic()
    try:
        status = store.status(ingest_enabled=True)
        print(f"status OK in {time.monotonic()-start:.2f}s topic={status.topic_count} event={status.event_count} link={status.link_count}")
    except Exception as exc:
        print(f"status FAILED in {time.monotonic()-start:.2f}s: {type(exc).__name__}: {exc}")

    start = time.monotonic()
    try:
        bundle = store.evidence_for("688549", ["电子化学品", "电子", "X4006"])
        print(
            f"evidence OK in {time.monotonic()-start:.2f}s "
            f"stock={len(bundle.stock)} sector={len(bundle.sector)}"
        )
    except Exception as exc:
        print(f"evidence FAILED in {time.monotonic()-start:.2f}s: {type(exc).__name__}: {exc}")
    store.close()


if __name__ == "__main__":
    main()
