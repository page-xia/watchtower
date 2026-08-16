"""直连生产 CynosDB MySQL，本地全量构建星球消息物化证据并写回 message_evidence_cache。

与 app/message_store.py 的动态路径语义完全一致：
- stock：entity_type=stock 且 code 精确匹配，updated_at/relevance 倒序取 120 候选；
- sector：name 精确 → code(slug) 精确 → （都无命中才）name/code 模糊包含，
  候选上限 80/40，按链接 key 去重；
- 同一事件跨多级板块链接折叠为一条（relevance 最高，持平取 impact 最大）；
- 孤儿链接（event/topic 缺失）跳过；
- 最终按 (create_time, impact_strength, confidence) 倒序取前 8。

覆盖范围：links 表出现过的全部 stock code、全部 sector/theme 的 name 与 code。
未出现过的板块显示名由应用侧 read-through / prebuild 端点补齐。

用法: .\\.venv\\Scripts\\python.exe scripts\\materialize_message_evidence.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.message_store import MessageStore  # noqa: E402
from scripts.prod_db import connect  # noqa: E402

STORE = MessageStore(env_id="", token="")  # 只借用纯函数，不走网络

STOCK_CANDIDATE_LIMIT = 120
SECTOR_CANDIDATE_LIMIT = 80
SECTOR_FUZZY_LIMIT = 40
EVIDENCE_LIMIT = 8


def fetch_all(cur, table: str, columns: list[str]) -> list[dict]:
    cur.execute(f"SELECT {', '.join(columns)} FROM {table}")
    rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def fetch_all_resilient(table: str, columns: list[str], retries: int = 4) -> list[dict]:
    """serverless 实例在重负载下会断公网连接，换连接重试。"""
    last_error: Exception | None = None
    for attempt in range(retries):
        conn = None
        try:
            conn = connect()
            with conn.cursor() as cur:
                return fetch_all(cur, table, columns)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"  fetch {table} attempt {attempt + 1} failed: {exc!r}", flush=True)
            time.sleep(2 * (attempt + 1))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    raise last_error or RuntimeError(f"fetch {table} failed")


def evidence_for_links(links: list[dict], match_scope: str, event_by_id: dict, topic_by_id: dict) -> list:
    """复刻 MessageStore._evidence_for_links 的纯计算部分（事件/话题用本地映射）。"""
    deduped: list[dict] = []
    seen: set[tuple] = set()
    for row in links:
        key = STORE._link_key(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    best_index_by_event: dict[str, int] = {}
    collapsed: list[dict] = []
    for row in deduped:
        event_id = str(row.get("event_id") or "")
        best_index = best_index_by_event.get(event_id)
        if best_index is None:
            best_index_by_event[event_id] = len(collapsed)
            collapsed.append(row)
            continue
        best = collapsed[best_index]
        if (STORE._float(row.get("relevance")), STORE._float(row.get("impact"))) > (
            STORE._float(best.get("relevance")),
            STORE._float(best.get("impact")),
        ):
            collapsed[best_index] = row

    evidence = []
    for link in collapsed:
        event = event_by_id.get(str(link.get("event_id") or ""))
        if not event:
            continue
        topic = topic_by_id.get(str(event.get("topic_id") or ""))
        if not topic:
            continue
        evidence.append(STORE._evidence_from_rows(topic, event, link, match_scope))

    evidence.sort(
        key=lambda item: (
            str(item.create_time or ""),
            float(item.impact_strength or 0),
            float(item.confidence or 0),
        ),
        reverse=True,
    )
    return evidence[:EVIDENCE_LIMIT]


def by_updated_relevance(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda r: (str(r.get("updated_at") or ""), STORE._float(r.get("relevance"))),
        reverse=True,
    )


def sector_links_for_term(term: str, sector_links: list[dict]) -> list[dict]:
    """复刻 _sector_evidence 单词语义：name 精确 + code(slug) 精确，都无命中才模糊。"""
    slug = STORE._slug_term(term)
    exact: list[dict] = []
    seen: set[tuple] = set()

    def add(rows: list[dict], cap: int) -> list[dict]:
        picked = []
        for row in by_updated_relevance(rows)[:cap]:
            key = STORE._link_key(row)
            if key in seen:
                continue
            seen.add(key)
            picked.append(row)
        return picked

    exact += add([r for r in sector_links if str(r.get("name") or "") == term], SECTOR_CANDIDATE_LIMIT)
    if slug:
        exact += add([r for r in sector_links if str(r.get("code") or "") == slug], SECTOR_CANDIDATE_LIMIT)
    if exact:
        return exact
    if not STORE._should_use_fuzzy_term(term):
        return exact
    fuzzy = add([r for r in sector_links if term in str(r.get("name") or "")], SECTOR_FUZZY_LIMIT)
    if slug:
        fuzzy += add([r for r in sector_links if slug in str(r.get("code") or "")], SECTOR_FUZZY_LIMIT)
    return exact + fuzzy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="调试用：只处理前 N 个实体")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    started = time.monotonic()
    print("fetching tables ...", flush=True)
    links = fetch_all_resilient("message_event_links", ["event_id", "entity_type", "code", "name", "role", "relevance", "impact", "updated_at"])
    events = fetch_all_resilient("message_events", ["event_id", "topic_id", "title", "summary", "event_type", "direction", "confidence", "impact_strength", "valid_from", "expires_at", "keywords_json"])
    topics = fetch_all_resilient("message_topics", ["topic_id", "title", "content", "create_time", "owner_name", "likes", "readers", "comments", "has_files", "has_images", "media_kind", "media_summary", "source"])
    print(f"links={len(links)} events={len(events)} topics={len(topics)} in {time.monotonic()-started:.1f}s", flush=True)

    event_by_id = {str(e["event_id"]): e for e in events}
    topic_by_id = {str(t["topic_id"]): t for t in topics}
    stock_links_by_code: dict[str, list[dict]] = {}
    sector_links: list[dict] = []
    for row in links:
        entity_type = str(row.get("entity_type") or "").strip().lower()
        if entity_type == "stock":
            code = str(row.get("code") or "").strip().zfill(6)
            if code:
                stock_links_by_code.setdefault(code, []).append(row)
        elif entity_type in {"sector", "theme"}:
            sector_links.append(row)

    sector_terms = sorted(
        {
            str(r.get("name") or "").strip()
            for r in sector_links
            if str(r.get("name") or "").strip()
        }
        | {
            str(r.get("code") or "").strip()
            for r in sector_links
            if str(r.get("code") or "").strip()
        }
    )
    stock_codes = sorted(stock_links_by_code)
    print(f"stock codes={len(stock_codes)} sector terms={len(sector_terms)}", flush=True)

    if args.limit:
        stock_codes = stock_codes[: args.limit]
        sector_terms = sector_terms[: args.limit]

    now = datetime.now().isoformat(timespec="seconds")
    rows: list[tuple] = []

    t0 = time.monotonic()
    for index, code in enumerate(stock_codes):
        candidates = by_updated_relevance(stock_links_by_code[code])[:STOCK_CANDIDATE_LIMIT]
        evidence = evidence_for_links(candidates, "stock", event_by_id, topic_by_id)
        payload = json.dumps([item.model_dump(mode="json") for item in evidence], ensure_ascii=False)
        rows.append(("watchtower", "stock", code, payload, now, now))
        if (index + 1) % 500 == 0:
            print(f"  stock {index + 1}/{len(stock_codes)} ({time.monotonic()-t0:.0f}s)", flush=True)
    print(f"stock done in {time.monotonic()-t0:.1f}s", flush=True)

    t0 = time.monotonic()
    for index, term in enumerate(sector_terms):
        candidates = sector_links_for_term(term, sector_links)
        evidence = evidence_for_links(candidates, "sector", event_by_id, topic_by_id)
        payload = json.dumps([item.model_dump(mode="json") for item in evidence], ensure_ascii=False)
        rows.append(("watchtower", "sector", term, payload, now, now))
        if (index + 1) % 100 == 0:
            print(f"  sector {index + 1}/{len(sector_terms)} ({time.monotonic()-t0:.0f}s)", flush=True)
    print(f"sector done in {time.monotonic()-t0:.1f}s", flush=True)

    if args.dry_run:
        print(f"dry-run: would upsert {len(rows)} rows")
        return

    t0 = time.monotonic()
    sql = (
        "INSERT INTO message_evidence_cache (_openid, scope, cache_key, payload, built_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE payload=VALUES(payload), built_at=VALUES(built_at), updated_at=VALUES(updated_at)"
    )

    def connect_resilient(retries: int = 8) -> object:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                return connect()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                print(f"  connect attempt {attempt + 1} failed: {exc!r}", flush=True)
                time.sleep(min(30, 5 * (attempt + 1)))
        raise last_error or RuntimeError("connect failed")

    conn = connect_resilient()
    try:
        for offset in range(0, len(rows), 500):
            batch = rows[offset : offset + 500]
            for attempt in range(4):
                try:
                    with conn.cursor() as cur:
                        cur.executemany(sql, batch)
                    conn.commit()
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"  upsert @{offset} attempt {attempt + 1} failed: {exc!r}", flush=True)
                    time.sleep(2 * (attempt + 1))
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = connect_resilient()
            else:
                raise RuntimeError(f"upsert batch at offset {offset} failed permanently")
            print(f"  upserted {min(offset + 500, len(rows))}/{len(rows)}", flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    print(f"upserted {len(rows)} rows in {time.monotonic()-t0:.1f}s", flush=True)
    print(f"total {time.monotonic()-started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
