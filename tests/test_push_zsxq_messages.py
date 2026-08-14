from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


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
