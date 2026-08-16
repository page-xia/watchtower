"""模拟 CloudBase MySQL REST 网关延迟，对比星球消息读取改造前后的墙钟时间。

用法: .\\.venv\\Scripts\\python.exe scripts\\bench_message_store.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.message_store import MessageStore

LATENCY_SECONDS = 0.15  # 模拟单次 REST 往返（TLS 握手 + 查询）延迟
LINK_ROWS = 5000  # 缩小版 links 表（生产 37 万行，慢查询同比放大）


def build_tables() -> dict[str, dict]:
    topics: dict = {}
    events: dict = {}
    links: dict = {}
    runs: dict = {}
    cache: dict = {}
    for i in range(200):
        tid = f"topic-{i}"
        eid = f"event-{i}"
        topics[tid] = {
            "_openid": "watchtower", "topic_id": tid, "title": f"主题{i}",
            "content": "内容", "create_time": "2026-08-14T10:00:00+08:00",
            "owner_name": "", "likes": 0, "readers": 0, "comments": 0,
            "has_files": 0, "has_images": 0, "media_kind": "text",
            "media_summary": "", "source": "zsxq", "updated_at": "",
        }
        events[eid] = {
            "_openid": "watchtower", "event_id": eid, "topic_id": tid,
            "title": f"事件{i}", "summary": "摘要", "event_type": "板块催化",
            "direction": "1", "confidence": 0.8, "impact_strength": 0.7,
            "valid_from": "2026-08-14T10:00:00+08:00", "expires_at": "",
            "keywords_json": "[]", "updated_at": "",
        }
    for i in range(LINK_ROWS):
        if i < 30:
            row = {
                "_openid": "watchtower", "event_id": f"event-{i % 200}",
                "entity_type": "sector", "code": "X4006", "name": "电子化学品",
                "role": "主线", "relevance": 0.9, "impact": 0.8, "updated_at": "",
            }
        elif i < 60:
            row = {
                "_openid": "watchtower", "event_id": f"event-{i % 200}",
                "entity_type": "stock", "code": "603115", "name": "海星股份",
                "role": "直接提及", "relevance": 0.9, "impact": 0.8, "updated_at": "",
            }
        else:
            row = {
                "_openid": "watchtower", "event_id": f"event-{i % 200}",
                "entity_type": "sector", "code": f"X{1000 + i}", "name": f"板块{i}",
                "role": "", "relevance": 0.1, "impact": 0.1, "updated_at": "",
            }
        links[(row["event_id"], row["entity_type"], row["code"], row["name"])] = row
    runs["run-1"] = {
        "_openid": "watchtower", "run_id": "run-1", "source": "zsxq",
        "started_at": "", "finished_at": "2026-08-15T20:59:46+08:00",
        "range_start": "", "range_end": "", "topic_count": 8, "event_count": 7,
        "link_count": 10, "upstream_latest_at": "", "status": "success",
        "error": "", "updated_at": "",
    }
    return {
        "message_topics": topics,
        "message_events": events,
        "message_event_links": links,
        "message_sync_runs": runs,
        "message_evidence_cache": cache,
    }


def row_key(table: str, row: dict):
    if table == "message_topics":
        return row["topic_id"]
    if table == "message_events":
        return row["event_id"]
    if table == "message_sync_runs":
        return row["run_id"]
    if table == "message_evidence_cache":
        return (row["scope"], row["cache_key"])
    return (row["event_id"], row["entity_type"], row["code"], row["name"])


def matches(raw, expression: str) -> bool:
    value = str(raw or "")
    if expression.startswith("eq."):
        return value == unquote(expression[3:])
    if expression.startswith("like."):
        return unquote(expression[5:]).replace("%", "") in value
    if expression.startswith("in.(") and expression.endswith(")"):
        return value in {unquote(p) for p in expression[4:-1].split(",")}
    raise AssertionError(expression)


def make_handler(tables: dict, counter: dict):
    def handle(request: httpx.Request) -> httpx.Response:
        time.sleep(LATENCY_SECONDS)
        counter["n"] = counter.get("n", 0) + 1
        match = re.search(r"/(?P<table>message_[a-z_]+)", str(request.url))
        table = match.group("table")
        if request.method == "POST":
            rows = json.loads(request.content.decode("utf-8"))
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                tables[table][row_key(table, row)] = dict(row)
            return httpx.Response(201, json=[], headers={"Content-Range": f"*/{len(rows)}"})
        rows = list(tables[table].values())
        limit = None
        order = ""
        for key, value in request.url.params.multi_items():
            if key in {"select", "offset"}:
                continue
            if key == "limit":
                limit = int(value)
                continue
            if key == "order":
                order = value
                continue
            rows = [r for r in rows if matches(r.get(key), value)]
        if order:
            for item in reversed([p.strip() for p in order.split(",") if p]):
                field, _, direction = item.partition(".")
                rows.sort(key=lambda r: str(r.get(field) or ""), reverse=direction == "desc")
        total = len(rows)
        if limit is not None:
            rows = rows[:limit]
        return httpx.Response(200, json=rows, headers={"Content-Range": f"0-0/{total}"})

    return handle


def bench(label: str, store: MessageStore, counter: dict) -> None:
    counter["n"] = 0
    start = time.monotonic()
    status = store.status(ingest_enabled=True)
    bundle = store.evidence_for("603115", ["电子化学品", "电子", "X4006", "电子化学品Ⅲ"])
    elapsed = time.monotonic() - start
    print(
        f"{label}: {elapsed:.2f}s | REST 查询 {counter['n']} 次 | "
        f"status(topic={status.topic_count}) stock={len(bundle.stock)} sector={len(bundle.sector)}"
    )


def main() -> None:
    tables = build_tables()
    counter: dict = {}
    client = httpx.Client(transport=httpx.MockTransport(make_handler(tables, counter)))
    # 走「共享长连接 client」路径（不注入 http_client，直接塞到 _shared_client）
    store = MessageStore(
        env_id="env", token="t", instance="default", schema="s",
        base_url="https://mock.local", cache_seconds=0,
        async_refresh=False,
    )
    store._shared_client = client
    bench("冷路径(物化未命中→动态计算+回写)", store, counter)
    bench("热路径(物化命中)", store, counter)
    bench("热路径(再次)", store, counter)
    store.close()


if __name__ == "__main__":
    main()
