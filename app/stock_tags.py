from __future__ import annotations

"""个股 题材/概念/板块 标签：easy_tdx 所属板块 + SQLite 持久化。

口径约束（AGENTS.md）：板块 taxonomy 只用 easy_tdx 官方板块族谱
（get_belong_board 返回的 TDX 板块），不混入东财/同花顺等第三方概念库；
tushare 不参与板块分类。

board_type 实测语义（2026-08 实测 600519/300476，与请求枚举 BoardType 不同）：
  12 → 行业（881xxx 通达信行业板块）  0/1/7/8/9 → 行业类兜底
  4  → 概念（5G概念/CPO概念…）
  3  → 地域（贵州板块/广东板块…）
  5  → 风格（绩优股/基金重仓…）

持久化：data/runtime/stock_tags.sqlite，默认 TTL 7 天；命中新鲜缓存不触网，
过期/缺失时实时拉取并回写；拉取失败回退旧缓存（stale 标记）。
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.config import DATA_DIR


DB_FILE = DATA_DIR / "runtime" / "stock_tags.sqlite"
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
MAX_CONCEPTS = 16

_TYPE_INDUSTRY = {0, 1, 7, 8, 9, 12}
_TYPE_CONCEPT = {4}
_TYPE_REGION = {3}
_TYPE_STYLE = {5}


class StockTagStore:
    def __init__(self, db_file: Path | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.db_file = db_file or DB_FILE
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(3600, int(ttl_seconds))
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_file), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_tags (
                    code TEXT PRIMARY KEY,
                    industry TEXT DEFAULT '',
                    concepts TEXT DEFAULT '[]',
                    styles TEXT DEFAULT '[]',
                    regions TEXT DEFAULT '[]',
                    source TEXT DEFAULT '',
                    fetched_at REAL DEFAULT 0
                )
                """
            )

    # ------------------------------------------------------------------ read

    def get(self, code: str) -> dict[str, Any] | None:
        normalized = str(code or "").strip().zfill(6)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stock_tags WHERE code = ?", (normalized,)
            ).fetchone()
        if row is None:
            return None
        return self._payload_from_row(row)

    @staticmethod
    def _payload_from_row(row: sqlite3.Row) -> dict[str, Any]:
        def load_list(raw: Any) -> list[str]:
            try:
                value = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                return []
            return [str(item) for item in value if str(item).strip()]

        return {
            "code": row["code"],
            "industry": str(row["industry"] or ""),
            "concepts": load_list(row["concepts"]),
            "styles": load_list(row["styles"]),
            "regions": load_list(row["regions"]),
            "source": str(row["source"] or ""),
            "fetched_at": float(row["fetched_at"] or 0),
        }

    # ----------------------------------------------------------------- write

    def upsert(self, payload: dict[str, Any]) -> None:
        code = str(payload.get("code") or "").strip().zfill(6)
        if len(code) != 6:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO stock_tags (code, industry, concepts, styles, regions, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    industry=excluded.industry,
                    concepts=excluded.concepts,
                    styles=excluded.styles,
                    regions=excluded.regions,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                (
                    code,
                    str(payload.get("industry") or ""),
                    json.dumps(list(payload.get("concepts") or []), ensure_ascii=False),
                    json.dumps(list(payload.get("styles") or []), ensure_ascii=False),
                    json.dumps(list(payload.get("regions") or []), ensure_ascii=False),
                    str(payload.get("source") or ""),
                    float(payload.get("fetched_at") or time.time()),
                ),
            )

    # ------------------------------------------------------------ fetch path

    def get_or_fetch(self, code: str, fetcher: Any | None = None, max_age_seconds: int | None = None) -> dict[str, Any]:
        """新鲜缓存直接返回；否则实时拉取回写；失败回退旧缓存。"""
        normalized = str(code or "").strip().zfill(6)
        max_age = max_age_seconds or self.ttl_seconds
        cached = self.get(normalized)
        now = time.time()
        if cached and now - cached["fetched_at"] <= max_age:
            cached["stale"] = False
            return cached
        if fetcher is not None:
            try:
                fresh = fetcher(normalized)
            except Exception:
                fresh = None
            if fresh:
                fresh.setdefault("fetched_at", now)
                self.upsert(fresh)
                payload = self.get(normalized)
                if payload:
                    payload["stale"] = False
                    return payload
        if cached:
            cached["stale"] = True
            return cached
        return {
            "code": normalized,
            "industry": "",
            "concepts": [],
            "styles": [],
            "regions": [],
            "source": "",
            "fetched_at": 0,
            "stale": True,
            "available": False,
        }


def classify_belong_boards(rows: list[dict[str, Any]], code: str) -> dict[str, Any]:
    """get_belong_board 原始行 → 行业/概念/风格/地域 分组。"""
    industry: list[str] = []
    concepts: list[str] = []
    styles: list[str] = []
    regions: list[str] = []
    for row in rows:
        name = str(row.get("board_name") or "").strip()
        if not name:
            continue
        try:
            board_type = int(row.get("board_type"))
        except (TypeError, ValueError):
            continue
        if board_type in _TYPE_INDUSTRY:
            industry.append(name)
        elif board_type in _TYPE_CONCEPT:
            concepts.append(name)
        elif board_type in _TYPE_REGION:
            regions.append(name)
        elif board_type in _TYPE_STYLE:
            styles.append(name)
    return {
        "code": str(code).zfill(6),
        # TDX 行业返回 大行业+子行业（如 酿酒/白酒），取最末级更贴近展示
        "industry": industry[-1] if industry else "",
        "industries": industry,
        "concepts": list(dict.fromkeys(concepts))[:MAX_CONCEPTS],
        "styles": list(dict.fromkeys(styles))[:8],
        "regions": list(dict.fromkeys(regions))[:4],
        "source": "easy_tdx_belong_board",
        "fetched_at": time.time(),
    }


__all__ = ["StockTagStore", "classify_belong_boards", "DB_FILE"]
