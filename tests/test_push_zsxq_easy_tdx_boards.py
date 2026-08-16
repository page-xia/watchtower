from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "push_zsxq_easy_tdx_boards.py"
SPEC = importlib.util.spec_from_file_location("push_zsxq_easy_tdx_boards", MODULE_PATH)
assert SPEC and SPEC.loader
push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(push)


def create_source_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE raw_topics (
              topic_id TEXT PRIMARY KEY,
              title TEXT,
              content TEXT,
              create_time TEXT,
              owner_name TEXT,
              likes INTEGER,
              readers INTEGER,
              comments INTEGER,
              has_files INTEGER,
              has_images INTEGER
            );
            CREATE TABLE events (
              event_id TEXT PRIMARY KEY,
              topic_id TEXT,
              title TEXT,
              summary TEXT,
              event_type TEXT,
              direction TEXT,
              confidence REAL,
              impact_strength REAL,
              valid_from TEXT,
              expires_at TEXT,
              keywords_json TEXT
            );
            CREATE TABLE event_entity_links (
              event_id TEXT,
              entity_type TEXT,
              code TEXT,
              name TEXT,
              role TEXT,
              relevance REAL,
              impact REAL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO raw_topics VALUES (
              'topic-1', '标题', '正文', '2026-08-06T09:30:00+08:00', '作者', 1, 2, 3, 0, 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO events VALUES (
              'event-1', 'topic-1', '事件', '摘要', 'research', '1', 0.8, 0.7,
              '2026-08-06T09:30:00+08:00', '2026-08-07T09:30:00+08:00', '["PCB"]'
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO event_entity_links VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("event-1", "stock", "300476", "胜宏科技", "受益标的", 0.9, 0.8),
                ("event-1", "sector", "X1000", "电子", "easy_tdx申万一级/stock:300476", 0.72, 0.64),
                ("event-1", "sector", "manual", "人工主题", "手动主题", 0.7, 0.6),
                ("event-1", "theme", "theme-1", "PCB主题", "manual", 0.7, 0.6),
            ],
        )
        conn.commit()


def test_iter_repair_batches_selects_easy_tdx_sector_links_only(tmp_path: Path) -> None:
    db_path = tmp_path / "stock_agent.sqlite"
    create_source_db(db_path)

    batches = list(
        push.iter_repair_batches(
            db_path,
            start="2026-08-01T00:00:00+08:00",
            end="2026-08-31T23:59:59+08:00",
            batch_size=10,
            include_topics_events=True,
        )
    )

    assert len(batches) == 1
    batch = batches[0]
    assert [topic["topic_id"] for topic in batch["topics"]] == ["topic-1"]
    assert [event["event_id"] for event in batch["events"]] == ["event-1"]
    assert [(link["entity_type"], link["code"], link["name"]) for link in batch["links"]] == [
        ("sector", "X1000", "电子")
    ]


def test_iter_repair_batches_can_emit_links_only(tmp_path: Path) -> None:
    db_path = tmp_path / "stock_agent.sqlite"
    create_source_db(db_path)

    batches = list(
        push.iter_repair_batches(
            db_path,
            start="2026-08-01T00:00:00+08:00",
            end="2026-08-31T23:59:59+08:00",
            batch_size=1,
            include_topics_events=False,
        )
    )

    assert batches == [
        {
            "topics": [],
            "events": [],
            "links": [
                {
                    "event_id": "event-1",
                    "entity_type": "sector",
                    "code": "X1000",
                    "name": "电子",
                    "role": "easy_tdx申万一级/stock:300476",
                    "relevance": 0.72,
                    "impact": 0.64,
                }
            ],
        }
    ]
