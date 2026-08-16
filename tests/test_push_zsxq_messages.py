from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "push_zsxq_messages.py"
SPEC = importlib.util.spec_from_file_location("push_zsxq_messages", MODULE_PATH)
assert SPEC and SPEC.loader
push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(push)


def test_resolve_window_includes_the_entire_explicit_end_date() -> None:
    start, end = push.resolve_window("20260806", "2026-08-07", 7)

    assert start == "2026-08-06T00:00:00+08:00"
    assert end == "2026-08-07T23:59:59.999999+08:00"


def test_resolve_window_uses_china_timezone_for_naive_iso_datetime() -> None:
    start, end = push.resolve_window("2026-08-06T09:00:00", "2026-08-06T10:00:00", 1)

    assert start.endswith("+08:00")
    assert end.endswith("+08:00")


def test_safe_error_redacts_ingest_token() -> None:
    message = push.safe_error(RuntimeError("Bearer secret-token failed"), "secret-token")

    assert "secret-token" not in message
    assert "[redacted]" in message


def test_post_payload_to_target_retries_retryable_http_500(monkeypatch) -> None:
    statuses = [500, 200]
    posts = []
    failed_runs = []

    class FakeClient:
        def post(self, url, *, headers, json):
            posts.append({"url": url, "headers": headers, "json": json})
            status = statuses.pop(0)
            return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(push.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(push, "record_failed_run", lambda **kwargs: failed_runs.append(kwargs))

    error = push.post_payload_to_target(
        FakeClient(),
        "https://watch.example",
        {"topics": [], "events": [], "links": []},
        "unit-token",
        run_started="2026-08-15T10:00:00+08:00",
        start="2026-08-01T00:00:00+08:00",
        end="2026-08-15T23:59:59+08:00",
        upstream_latest_at="2026-08-15T09:30:00+08:00",
    )

    assert error == ""
    assert len(posts) == 2
    assert failed_runs == []


def test_post_payload_to_target_records_failed_run_after_retry_exhaustion(monkeypatch) -> None:
    posts = []
    failed_runs = []

    class FakeClient:
        def post(self, url, *, headers, json):
            posts.append({"url": url, "headers": headers, "json": json})
            return httpx.Response(500, request=httpx.Request("POST", url))

    monkeypatch.setattr(push.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(push, "record_failed_run", lambda **kwargs: failed_runs.append(kwargs))

    error = push.post_payload_to_target(
        FakeClient(),
        "https://watch.example",
        {"topics": [], "events": [], "links": []},
        "unit-token",
        run_started="2026-08-15T10:00:00+08:00",
        start="2026-08-01T00:00:00+08:00",
        end="2026-08-15T23:59:59+08:00",
        upstream_latest_at="2026-08-15T09:30:00+08:00",
    )

    assert error == "ingest HTTP 500"
    assert len(posts) == push.INGEST_MAX_ATTEMPTS
    assert len(failed_runs) == 1


def make_row(
    topic_id: str,
    *,
    has_files: bool = False,
    has_images: bool = False,
    source_json: dict | None = None,
) -> dict:
    event_id = f"event-{topic_id}"
    return {
        "code": "300476",
        "name": "胜宏科技",
        "role": "受益标的",
        "relevance": 0.9,
        "impact": 0.8,
        "entity_type": "stock",
        "event_id": event_id,
        "event_title": f"{topic_id} event",
        "event_summary": f"{topic_id} summary",
        "event_type": "research",
        "direction": 1,
        "confidence": 0.8,
        "impact_strength": 0.7,
        "valid_from": "2026-08-06T09:30:00+08:00",
        "expires_at": "2026-08-07T09:30:00+08:00",
        "keywords_json": "[]",
        "topic_id": topic_id,
        "topic_title": f"{topic_id} title",
        "topic_content": f"{topic_id} content",
        "create_time": "2026-08-06T09:30:00+08:00",
        "owner_name": "消息源",
        "likes": 1,
        "readers": 2,
        "comments": 3,
        "has_files": int(has_files),
        "has_images": int(has_images),
        "media_summary": "",
        "source_json": json.dumps(source_json or {}, ensure_ascii=False),
    }


def test_rows_to_payload_labels_media_kind_and_filters_fast_mode() -> None:
    rows = [
        make_row("text"),
        make_row("image", has_images=True, source_json={"images": [{"path": "img.png"}]}),
        make_row("file", has_files=True, source_json={"files": [{"name": "研报.pdf", "kind": "file"}]}),
        make_row("voice", has_files=True, source_json={"files": [{"name": "录音.m4a", "kind": "audio"}]}),
    ]

    fast = push.rows_to_payload(rows, media_mode="fast")
    full = push.rows_to_payload(rows, media_mode="full")

    assert [topic["topic_id"] for topic in fast["topics"]] == ["text", "image"]
    assert {topic["topic_id"]: topic["media_kind"] for topic in fast["topics"]} == {
        "text": "text",
        "image": "image",
    }
    assert [event["topic_id"] for event in fast["events"]] == ["text", "image"]
    assert {topic["topic_id"]: topic["media_kind"] for topic in full["topics"]} == {
        "text": "text",
        "image": "image",
        "file": "file",
        "voice": "voice",
    }


def test_rows_to_payload_enriches_stock_links_with_easy_tdx_board_levels() -> None:
    payload = push.rows_to_payload(
        [make_row("tdx-board")],
        board_names_by_stock={
            "300476": [
                {"level": 1, "code": "X1000", "name": "电子"},
                {"level": 2, "code": "X2000", "name": "元件"},
                {"level": 3, "code": "X3000", "name": "PCB"},
            ]
        },
    )

    sector_links = [link for link in payload["links"] if link["entity_type"] == "sector"]

    assert [(link["code"], link["name"]) for link in sector_links] == [
        ("X1000", "电子"),
        ("X2000", "元件"),
        ("X3000", "PCB"),
    ]
    assert all(link["role"].startswith("easy_tdx申万") for link in sector_links)


def test_main_posts_unique_full_run_summary(monkeypatch, tmp_path, capsys) -> None:
    source_db = tmp_path / "stock_agent.sqlite"
    source_db.touch()
    posts = []
    batches = [
        {
            "topics": [{"topic_id": "topic-1"}],
            "events": [{"event_id": "event-1"}],
            "links": [
                {"event_id": "event-1", "entity_type": "stock", "code": "300476", "name": "胜宏科技"}
            ],
        },
        {
            "topics": [{"topic_id": "topic-1"}, {"topic_id": "topic-2"}],
            "events": [{"event_id": "event-1"}, {"event_id": "event-2"}],
            "links": [
                {"event_id": "event-1", "entity_type": "stock", "code": "300476", "name": "胜宏科技"},
                {"event_id": "event-2", "entity_type": "sector", "code": "pcb", "name": "PCB"},
            ],
        },
    ]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def post(self, url, *, headers, json):
            posts.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(
        push,
        "parse_args",
        lambda: SimpleNamespace(
            source_db=str(source_db),
            target_url="http://watchtower.test",
            token="unit-token",
            start="20260806",
            end="20260809",
            lookback_days=7,
            batch_size=2,
            dry_run=False,
        ),
    )
    monkeypatch.setattr(push, "latest_upstream_time", lambda *args: "2026-08-08T23:13:21+08:00")
    monkeypatch.setattr(push, "iter_batches", lambda *args: iter(batches))
    monkeypatch.setattr(push.httpx, "Client", FakeClient)

    assert push.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["topics"] == 2
    assert output["events"] == 2
    assert output["links"] == 2
    assert output["batches"] == 2
    assert output["requests"] == 3
    assert len(posts) == 3

    summary = posts[-1]["json"]
    assert posts[0]["json"]["run_id"] == f"{summary['run_id']}-0001"
    assert posts[1]["json"]["run_id"] == f"{summary['run_id']}-0002"
    assert summary["reported_topic_count"] == 2
    assert summary["reported_event_count"] == 2
    assert summary["reported_link_count"] == 2
    assert summary["topics"] == []
    assert summary["events"] == []
    assert summary["links"] == []


def test_main_fans_out_batches_and_summary_to_all_target_urls(monkeypatch, tmp_path, capsys) -> None:
    source_db = tmp_path / "stock_agent.sqlite"
    source_db.touch()
    posts = []
    batches = [
        {
            "topics": [{"topic_id": "topic-1", "media_kind": "text"}],
            "events": [{"event_id": "event-1"}],
            "links": [],
        }
    ]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def post(self, url, *, headers, json):
            posts.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(
        push,
        "parse_args",
        lambda: SimpleNamespace(
            source_db=str(source_db),
            target_url=["https://watch.example", "http://127.0.0.1:8788"],
            token="unit-token",
            start="20260806",
            end="20260806",
            lookback_days=1,
            batch_size=2,
            dry_run=False,
            media_mode="fast",
        ),
    )
    monkeypatch.setattr(push, "latest_upstream_time", lambda *args: "2026-08-06T09:30:00+08:00")
    monkeypatch.setattr(push, "iter_batches", lambda *args, **kwargs: iter(batches))
    monkeypatch.setattr(push.httpx, "Client", FakeClient)

    assert push.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["target_urls"] == ["https://watch.example", "http://127.0.0.1:8788"]
    assert output["target_count"] == 2
    assert output["requests"] == 4
    assert [post["url"] for post in posts] == [
        "https://watch.example/api/ingest/zsxq/messages",
        "http://127.0.0.1:8788/api/ingest/zsxq/messages",
        "https://watch.example/api/ingest/zsxq/messages",
        "http://127.0.0.1:8788/api/ingest/zsxq/messages",
    ]
