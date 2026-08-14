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


def test_message_store_upsert_is_idempotent_and_updates(tmp_path) -> None:
    store = MessageStore(tmp_path / "messages.sqlite")

    first = store.upsert_messages(ingest_payload())
    second = store.upsert_messages(ingest_payload(summary="更新后的PCB订单摘要。"))
    status = store.status(ingest_enabled=True)
    evidence = store.evidence_for("300476", ["PCB"])

    assert first.ok is True
    assert second.ok is True
    assert status.topic_count == 1
    assert status.event_count == 1
    assert status.link_count == 2
    assert status.latest_run is not None
    assert status.latest_run.topic_count == 1
    assert evidence.stock[0].event_summary == "更新后的PCB订单摘要。"
    assert evidence.sector[0].name == "PCB/CCL/电子布"


def test_message_store_records_reported_counts_for_summary_run(tmp_path) -> None:
    store = MessageStore(tmp_path / "messages.sqlite")
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


def test_message_store_message_detail_returns_full_payload(tmp_path) -> None:
    store = MessageStore(tmp_path / "messages.sqlite")
    store.upsert_messages(ingest_payload())

    payload = store.message_detail("event-1", ingest_enabled=True)

    assert payload is not None
    assert payload.topic.topic_id == "topic-1"
    assert payload.event.event_id == "event-1"
    assert payload.event.summary == "PCB订单改善，胜宏科技受益。"
    assert payload.links[0].code == "300476"
    assert payload.sync is not None
