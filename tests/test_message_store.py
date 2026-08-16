from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from app.message_store import MessageStore
from app.models import MessageEvent, MessageEventLink, MessageTopic, ZsxqMessageIngestRequest


def ingest_payload(summary: str = "PCB订单改善，胜宏科技受益。") -> ZsxqMessageIngestRequest:
    return ZsxqMessageIngestRequest(
        source="zsxq",
        run_id="unit-run",
        start="2026-08-06T00:00:00+08:00",
        end="2026-08-06T23:59:59+08:00",
        upstream_latest_at="2026-08-06T10:00:00+08:00",
        topics=[
            MessageTopic(
                topic_id="topic-1",
                title="胜宏科技订单跟踪",
                content="服务器PCB订单继续改善。",
                create_time="2026-08-06T09:45:00+08:00",
                owner_name="消息源",
                readers=100,
                has_files=True,
                media_kind="file",
                media_summary="【附件投研要点】核心逻辑：AI服务器PCB订单继续改善，产能利用率提升。",
                source="zsxq",
            )
        ],
        events=[
            MessageEvent(
                event_id="event-1",
                topic_id="topic-1",
                title="PCB订单改善",
                summary=summary,
                event_type="订单",
                direction=1,
                confidence=0.82,
                impact_strength=0.76,
                valid_from="2026-08-06T09:45:00+08:00",
                expires_at="2026-08-13T09:45:00+08:00",
                keywords=["PCB", "胜宏科技"],
            )
        ],
        links=[
            MessageEventLink(
                event_id="event-1",
                entity_type="stock",
                code="300476",
                name="胜宏科技",
                role="受益标的",
                relevance=0.92,
                impact=0.8,
            ),
            MessageEventLink(
                event_id="event-1",
                entity_type="sector",
                code="pcb_ccl_eglass",
                name="PCB/CCL/电子布",
                role="主线板块",
                relevance=0.88,
                impact=0.7,
            ),
        ],
    )


class FakeCloudBaseMysqlRest:
    def __init__(self) -> None:
        self.tables: dict[str, dict[Any, dict[str, Any]]] = {
            "message_topics": {},
            "message_events": {},
            "message_event_links": {},
            "message_sync_runs": {},
            "message_evidence_cache": {},
        }
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        match = re.search(r"/v1/rdb/rest/default/server-d2g7x597t019f5cb0/(?P<table>[^/?]+)", str(request.url))
        assert match, f"unexpected URL {request.url}"
        table = match.group("table")
        assert table in self.tables
        if request.method == "POST":
            assert "resolution=merge-duplicates" in request.headers["prefer"]
            rows = json.loads(request.content.decode("utf-8"))
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                assert row["_openid"] == "watchtower"
                self.tables[table][self._key(table, row)] = dict(row)
            return httpx.Response(201, json=[], headers={"Content-Range": f"*/{len(rows)}"})
        if request.method == "GET":
            rows, total_count = self._filtered_rows(table, request.url.params.multi_items())
            content_range = f"0-{max(len(rows) - 1, 0)}/{total_count}"
            return httpx.Response(200, json=rows, headers={"Content-Range": content_range})
        raise AssertionError(f"unexpected method {request.method}")

    def _filtered_rows(self, table: str, params: Iterable[tuple[str, str]]) -> tuple[list[dict[str, Any]], int]:
        rows = list(self.tables[table].values())
        limit: int | None = None
        order = ""
        for key, value in params:
            if key in {"select", "offset"}:
                continue
            if key == "limit":
                limit = int(value)
                continue
            if key == "order":
                order = value
                continue
            rows = [row for row in rows if self._matches(row.get(key), value)]
        if order:
            for item in reversed([part.strip() for part in order.split(",") if part.strip()]):
                field, _, direction = item.partition(".")
                rows.sort(key=lambda row: str(row.get(field) or ""), reverse=direction == "desc")
        total_count = len(rows)
        if limit is not None:
            rows = rows[:limit]
        return [dict(row) for row in rows], total_count

    def _matches(self, raw: Any, expression: str) -> bool:
        value = str(raw or "")
        if expression.startswith("eq."):
            return value == unquote(expression[3:])
        if expression.startswith("like."):
            pattern = unquote(expression[5:]).replace("%", "")
            return pattern in value
        if expression.startswith("in.(") and expression.endswith(")"):
            candidates = {unquote(part) for part in expression[4:-1].split(",")}
            return value in candidates
        raise AssertionError(f"unsupported filter {expression}")

    def _key(self, table: str, row: dict[str, Any]) -> Any:
        if table == "message_topics":
            return row["topic_id"]
        if table == "message_events":
            return row["event_id"]
        if table == "message_sync_runs":
            return row["run_id"]
        if table == "message_evidence_cache":
            return (row["scope"], row["cache_key"])
        return (row["event_id"], row["entity_type"], row["code"], row["name"])


class FlakyCloudBaseMysqlRest(FakeCloudBaseMysqlRest):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail_once and request.method == "GET" and "message_event_links" in str(request.url):
            self.fail_once = False
            return httpx.Response(
                400,
                json={
                    "code": "INVALID_REQUEST",
                    "message": "Execute sql error. dial tcp 30.47.14.16:28309: connect: connection refused",
                },
            )
        return super().handle(request)


class FlakyPostCloudBaseMysqlRest(FakeCloudBaseMysqlRest):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail_once and request.method == "POST" and "message_topics" in str(request.url):
            self.requests.append(request)
            assert request.headers["authorization"] == "Bearer test-token"
            self.fail_once = False
            return httpx.Response(
                500,
                json={
                    "code": "INTERNAL",
                    "message": "Execute sql error. dial tcp 30.47.14.16:28309: connect: connection refused",
                },
            )
        return super().handle(request)


def make_store(fake: FakeCloudBaseMysqlRest, cache_seconds: float = 15.0) -> MessageStore:
    return MessageStore(
        env_id="server-d2g7x597t019f5cb0",
        token="test-token",
        instance="default",
        schema="server-d2g7x597t019f5cb0",
        cache_seconds=cache_seconds,
        http_client=fake.client(),
        # 测试里同步执行物化刷新，保证行为确定、可断言。
        async_refresh=False,
    )


def test_message_store_uses_cloudbase_mysql_rest_and_preserves_media_logic() -> None:
    fake = FakeCloudBaseMysqlRest()
    store = make_store(fake)

    first = store.upsert_messages(ingest_payload())
    second = store.upsert_messages(ingest_payload(summary="更新后的PCB订单摘要。"))
    status = store.status(ingest_enabled=True)
    evidence = store.evidence_for("300476", ["PCB"])
    detail = store.message_detail("event-1", ingest_enabled=True)

    assert first.ok is True
    assert second.ok is True
    assert status.db_file.startswith("cloudbase_mysql://server-d2g7x597t019f5cb0/default/")
    assert "sqlite" not in status.db_file.lower()
    assert status.topic_count == 1
    assert status.event_count == 1
    assert status.link_count == 2
    assert status.latest_run is not None
    assert status.latest_run.topic_count == 1
    assert evidence.stock[0].event_summary == "更新后的PCB订单摘要。"
    assert "核心逻辑：AI服务器PCB订单继续改善" in evidence.stock[0].display_text
    assert evidence.sector[0].name == "PCB/CCL/电子布"
    assert detail is not None
    assert detail.topic.media_kind == "file"
    assert "核心逻辑：AI服务器PCB订单继续改善" in detail.topic.media_summary
    assert detail.links[0].code == "300476"
    assert any("/v1/rdb/rest/default/server-d2g7x597t019f5cb0/message_topics" in str(req.url) for req in fake.requests)


def test_message_store_retries_transient_mysql_get_failures() -> None:
    fake = FlakyCloudBaseMysqlRest()
    fake.tables["message_topics"]["topic-1"] = {
        "_openid": "watchtower",
        "topic_id": "topic-1",
        "title": "胜宏科技订单跟踪",
        "content": "服务器PCB订单继续改善。",
        "create_time": "2026-08-06T09:45:00+08:00",
        "owner_name": "消息源",
        "likes": 0,
        "readers": 0,
        "comments": 0,
        "has_files": 0,
        "has_images": 0,
        "media_kind": "text",
        "media_summary": "",
        "source": "zsxq",
        "updated_at": "2026-08-06T09:45:00+08:00",
    }
    fake.tables["message_events"]["event-1"] = {
        "_openid": "watchtower",
        "event_id": "event-1",
        "topic_id": "topic-1",
        "title": "PCB订单改善",
        "summary": "PCB订单改善，胜宏科技受益。",
        "event_type": "订单",
        "direction": "1",
        "confidence": 0.82,
        "impact_strength": 0.76,
        "valid_from": "2026-08-06T09:45:00+08:00",
        "expires_at": "2026-08-13T09:45:00+08:00",
        "keywords_json": '["PCB", "胜宏科技"]',
        "updated_at": "2026-08-06T09:45:00+08:00",
    }
    fake.tables["message_event_links"][("event-1", "stock", "300476", "胜宏科技")] = {
        "_openid": "watchtower",
        "event_id": "event-1",
        "entity_type": "stock",
        "code": "300476",
        "name": "胜宏科技",
        "role": "受益标的",
        "relevance": 0.92,
        "impact": 0.8,
        "updated_at": "2026-08-06T09:45:00+08:00",
    }
    fake.tables["message_event_links"][("event-1", "sector", "pcb_ccl_eglass", "PCB/CCL/电子布")] = {
        "_openid": "watchtower",
        "event_id": "event-1",
        "entity_type": "sector",
        "code": "pcb_ccl_eglass",
        "name": "PCB/CCL/电子布",
        "role": "主线板块",
        "relevance": 0.88,
        "impact": 0.7,
        "updated_at": "2026-08-06T09:45:00+08:00",
    }

    store = make_store(fake, cache_seconds=0)
    evidence = store.evidence_for("300476", ["PCB"])

    assert evidence.stock and evidence.sector
    assert fake.fail_once is False
    assert sum(1 for req in fake.requests if req.method == "GET" and "message_event_links" in str(req.url)) >= 2


def test_message_store_retries_idempotent_upsert_posts_after_cloudbase_mysql_refusal() -> None:
    fake = FlakyPostCloudBaseMysqlRest()
    store = make_store(fake, cache_seconds=0)

    response = store.upsert_messages(ingest_payload())

    assert response.ok is True
    assert fake.fail_once is False
    assert fake.tables["message_topics"]["topic-1"]["title"] == "胜宏科技订单跟踪"
    assert sum(1 for req in fake.requests if req.method == "POST" and "message_topics" in str(req.url)) == 2


def test_evidence_read_through_caches_dynamic_misses() -> None:
    """首次未命中走动态计算并回写物化表；第二次只读物化表，不碰 links 大表。"""
    fake = FakeCloudBaseMysqlRest()
    store = make_store(fake, cache_seconds=0)
    store.upsert_messages(ingest_payload())
    fake.requests.clear()

    first = store.evidence_for("300476", ["PCB"])
    fake.requests.clear()
    second = store.evidence_for("300476", ["PCB"])

    assert [item.event_id for item in first.stock] == ["event-1"]
    assert [item.event_id for item in second.stock] == ["event-1"]
    assert first.sector and second.sector
    touched = {re.search(r"message_[a-z_]+", str(req.url)).group(0) for req in fake.requests}
    assert "message_evidence_cache" in touched
    assert "message_event_links" not in touched
    assert "message_events" not in touched
    assert "message_topics" not in touched


def test_upsert_refresh_updates_materialized_evidence_and_alias_terms() -> None:
    """同步新数据后，受影响个股与互为子串的板块查询词都重建物化值。"""
    fake = FakeCloudBaseMysqlRest()
    store = make_store(fake, cache_seconds=0)
    store.upsert_messages(ingest_payload())
    assert store.evidence_for("300476", ["PCB"]).stock[0].event_summary == "PCB订单改善，胜宏科技受益。"

    store.upsert_messages(ingest_payload(summary="更新后的PCB订单摘要。"))

    refreshed = store.evidence_for("300476", ["PCB"])
    assert refreshed.stock[0].event_summary == "更新后的PCB订单摘要。"
    # 「PCB」是已缓存查询词，与变更链接名「PCB/CCL/电子布」互为子串 → 别名桥接重建。
    sector_row = fake.tables["message_evidence_cache"].get(("sector", "PCB"))
    assert sector_row is not None
    assert "更新后的PCB订单摘要。" in sector_row["payload"]


def test_evidence_many_uncached_terms_no_pool_deadlock() -> None:
    """外层证据任务（stock + N 个板块词）数量超过读池 worker 时，
    每个外层任务还会向内嵌套扇出叶子查询。叶子查询必须走独立池，
    否则外层占满 worker 并互等子任务会死锁（生产曾因此整接口挂起）。"""
    fake = FakeCloudBaseMysqlRest()
    store = make_store(fake, cache_seconds=0)
    store.upsert_messages(ingest_payload())
    terms = [f"未缓存词{i}" for i in range(10)]
    result: dict[str, Any] = {}

    def run() -> None:
        result["bundle"] = store.evidence_for("300476", terms)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive(), "evidence_for deadlocked on nested pool fan-out"
    assert result["bundle"].stock[0].event_id == "event-1"


def test_message_store_records_reported_counts_for_summary_run() -> None:
    fake = FakeCloudBaseMysqlRest()
    store = make_store(fake)
    payload = ZsxqMessageIngestRequest(
        source="zsxq",
        run_id="summary-run",
        reported_topic_count=667,
        reported_event_count=68,
        reported_link_count=586,
    )

    response = store.upsert_messages(payload)
    status = store.status(ingest_enabled=True)

    assert response.topic_count == 667
    assert response.event_count == 68
    assert response.link_count == 586
    assert status.topic_count == 0
    assert status.event_count == 0
    assert status.link_count == 0
    assert status.latest_run is not None
    assert status.latest_run.run_id == "summary-run"
    assert status.latest_run.topic_count == 667
    assert status.latest_run.event_count == 68
    assert status.latest_run.link_count == 586


def test_message_store_is_empty_and_non_sqlite_when_cloudbase_mysql_is_not_configured() -> None:
    store = MessageStore(env_id="", token="", instance="default", schema="server-d2g7x597t019f5cb0")

    status = store.status(ingest_enabled=True)

    assert status.db_file == "cloudbase_mysql://unconfigured"
    assert status.topic_count == 0
    assert status.latest_run is None
    assert store.evidence_for("300476", ["PCB"]).stock == []
    assert store.message_detail("event-1") is None
    with pytest.raises(RuntimeError):
        store.upsert_messages(ingest_payload())


def test_message_store_reuses_recent_status_and_evidence_results() -> None:
    fake = FakeCloudBaseMysqlRest()
    store = make_store(fake, cache_seconds=60)
    store.upsert_messages(ingest_payload())
    fake.requests.clear()

    first_status = store.status(ingest_enabled=True)
    second_status = store.status(ingest_enabled=True)
    first_evidence = store.evidence_for("300476", ["PCB"])
    second_evidence = store.evidence_for("300476", ["PCB"])

    assert first_status.topic_count == 1
    assert second_status.topic_count == 1
    assert first_evidence.stock
    assert second_evidence.stock
    assert len(fake.requests) < 25


def test_sector_evidence_searches_past_orphan_links_with_same_name() -> None:
    fake = FakeCloudBaseMysqlRest()
    for index in range(300):
        row = {
            "_openid": "watchtower",
            "event_id": f"orphan-{index}",
            "entity_type": "sector",
            "code": "X4006",
            "name": "电子化学品",
            "role": "easy_tdx申万三级/stock:688549",
            "relevance": 0.95,
            "impact": 0.9,
            "updated_at": "",
        }
        fake.tables["message_event_links"][fake._key("message_event_links", row)] = row
    store = make_store(fake)
    store.upsert_messages(
        ZsxqMessageIngestRequest(
            source="zsxq",
            run_id="valid-sector-run",
            topics=[
                MessageTopic(
                    topic_id="topic-688549",
                    title="中巨芯消息",
                    content="电子化学品国产替代推进。",
                    create_time="2026-08-08T21:42:32+08:00",
                )
            ],
            events=[
                MessageEvent(
                    event_id="event-688549",
                    topic_id="topic-688549",
                    title="电子化学品催化",
                    summary="中巨芯受益电子化学品板块催化。",
                    event_type="板块催化",
                    direction=1,
                    confidence=0.88,
                    impact_strength=0.74,
                    valid_from="2026-08-08T21:42:32+08:00",
                    keywords=["电子化学品", "中巨芯"],
                )
            ],
            links=[
                MessageEventLink(
                    event_id="event-688549",
                    entity_type="sector",
                    code="X4006",
                    name="电子化学品",
                    role="easy_tdx申万三级/stock:688549",
                    relevance=0.9,
                    impact=0.8,
                )
            ],
        )
    )

    evidence = store.evidence_for("688549", ["电子化学品"])

    assert [item.event_id for item in evidence.sector] == ["event-688549"]


def test_stock_evidence_searches_past_orphan_links_with_same_code() -> None:
    fake = FakeCloudBaseMysqlRest()
    for index in range(100):
        row = {
            "_openid": "watchtower",
            "event_id": f"orphan-stock-{index}",
            "entity_type": "stock",
            "code": "688549",
            "name": "中巨芯",
            "role": "直接提及",
            "relevance": 0.95,
            "impact": 0.9,
            "updated_at": "",
        }
        fake.tables["message_event_links"][fake._key("message_event_links", row)] = row
    store = make_store(fake)
    store.upsert_messages(
        ZsxqMessageIngestRequest(
            source="zsxq",
            run_id="valid-stock-run",
            topics=[
                MessageTopic(
                    topic_id="topic-688549",
                    title="中巨芯消息",
                    content="中巨芯中报改善。",
                    create_time="2026-08-08T21:42:32+08:00",
                )
            ],
            events=[
                MessageEvent(
                    event_id="event-688549",
                    topic_id="topic-688549",
                    title="中巨芯中报点评",
                    summary="中巨芯受益电子化学品板块催化。",
                    event_type="earnings",
                    direction=1,
                    confidence=0.88,
                    impact_strength=0.74,
                    valid_from="2026-08-08T21:42:32+08:00",
                    keywords=["中巨芯"],
                )
            ],
            links=[
                MessageEventLink(
                    event_id="event-688549",
                    entity_type="stock",
                    code="688549",
                    name="中巨芯",
                    role="直接提及",
                    relevance=0.9,
                    impact=0.8,
                )
            ],
        )
    )

    evidence = store.evidence_for("688549", ["电子化学品"])

    assert [item.event_id for item in evidence.stock] == ["event-688549"]


def test_sector_evidence_collapses_same_event_across_sector_levels() -> None:
    """同一事件挂多级板块链接（光纤光缆/通信设备/通信）时只保留相关性最高的一条。"""
    fake = FakeCloudBaseMysqlRest()
    store = make_store(fake)
    store.upsert_messages(
        ZsxqMessageIngestRequest(
            source="zsxq",
            run_id="dup-sector-run",
            topics=[
                MessageTopic(
                    topic_id="topic-600487",
                    title="通信复盘",
                    content="光纤光缆景气上行。",
                    create_time="2026-08-14T19:12:00+08:00",
                )
            ],
            events=[
                MessageEvent(
                    event_id="event-600487",
                    topic_id="topic-600487",
                    title="光纤光缆催化",
                    summary="亨通光电受益。",
                    event_type="板块催化",
                    direction=1,
                    confidence=0.9,
                    impact_strength=0.8,
                    valid_from="2026-08-14T19:12:00+08:00",
                    keywords=["光纤光缆"],
                )
            ],
            links=[
                MessageEventLink(
                    event_id="event-600487", entity_type="sector", code="光纤光缆",
                    name="光纤光缆", role="三级板块", relevance=0.95, impact=0.9,
                ),
                MessageEventLink(
                    event_id="event-600487", entity_type="sector", code="881338",
                    name="通信设备", role="二级板块", relevance=0.72, impact=0.7,
                ),
                MessageEventLink(
                    event_id="event-600487", entity_type="sector", code="881337",
                    name="通信", role="一级板块", relevance=0.72, impact=0.7,
                ),
            ],
        )
    )

    evidence = store.evidence_for("600487", ["光纤光缆", "通信设备", "通信"])

    assert [item.event_id for item in evidence.sector] == ["event-600487"]
    assert evidence.sector[0].name == "光纤光缆"
