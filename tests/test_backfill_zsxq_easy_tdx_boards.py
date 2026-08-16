from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "backfill_zsxq_easy_tdx_boards.py"
SPEC = importlib.util.spec_from_file_location("backfill_zsxq_easy_tdx_boards", MODULE_PATH)
assert SPEC and SPEC.loader
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event_entity_links (
          event_id TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          role TEXT NOT NULL,
          relevance REAL NOT NULL,
          impact REAL NOT NULL,
          known_at TEXT,
          PRIMARY KEY(event_id, entity_type, code)
        )
        """
    )
    return conn


def test_build_backfill_rows_adds_easy_tdx_board_names_for_stock_links() -> None:
    conn = make_conn()
    conn.execute(
        """
        INSERT INTO event_entity_links
          (event_id, entity_type, code, name, role, relevance, impact)
        VALUES
          ('event-1', 'stock', '300476', '胜宏科技', '受益标的', 0.9, 0.8)
        """
    )

    result = backfill.build_backfill_rows(
        conn,
        {
            "300476": [
                {"level": 1, "code": "881318", "name": "电子"},
                {"level": 2, "code": "881492", "name": "元件"},
                {"level": 3, "code": "881493", "name": "PCB"},
            ]
        },
        known_at="2026-08-15T10:00:00+08:00",
    )

    assert [(row["code"], row["name"]) for row in result.rows] == [
        ("881318", "电子"),
        ("881492", "元件"),
        ("881493", "PCB"),
    ]
    assert result.coverable_events == 1
    assert result.unmapped_events == 0


def test_build_backfill_rows_skips_existing_sector_name_to_avoid_duplicate_evidence() -> None:
    conn = make_conn()
    conn.executemany(
        """
        INSERT INTO event_entity_links
          (event_id, entity_type, code, name, role, relevance, impact)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("event-1", "stock", "688549", "中巨芯-U", "受益标的", 0.9, 0.8),
            ("event-1", "sector", "电子化学品", "电子化学品", "旧tdx三级", 0.9, 0.8),
        ],
    )

    result = backfill.build_backfill_rows(
        conn,
        {
            "688549": [
                {"level": 1, "code": "881318", "name": "电子"},
                {"level": 2, "code": "881479", "name": "电子化学品"},
                {"level": 3, "code": "881479", "name": "电子化学品"},
            ]
        },
        known_at="2026-08-15T10:00:00+08:00",
    )

    assert [(row["code"], row["name"]) for row in result.rows] == [("881318", "电子")]
    assert result.skipped_existing_name == 2
