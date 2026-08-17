from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from app.config import DATA_DIR, POSITION_FILE, THEMES_FILE, WATCHLIST_FILE, load_yaml
from app.models import AnalysisRecord, PositionRecord, WatchlistItem


DEFAULT_WATCHLIST: list[WatchlistItem] = []


class JsonStateStore(Protocol):
    def get_json(self, namespace: str, key: str, default: Any = None) -> Any:
        ...

    def set_json(self, namespace: str, key: str, value: Any) -> None:
        ...


class WatchlistStore:
    def __init__(self, path: Path = WATCHLIST_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list_items(self) -> list[WatchlistItem]:
        if not self.path.exists():
            self.save_all(DEFAULT_WATCHLIST)
            return list(DEFAULT_WATCHLIST)
        with self.path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return [WatchlistItem.model_validate(item) for item in raw]

    def save_all(self, items: list[WatchlistItem]) -> None:
        payload = [item.model_dump() for item in items]
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def upsert(self, item: WatchlistItem) -> WatchlistItem:
        items = self.list_items()
        replaced = False
        next_items: list[WatchlistItem] = []
        for existing in items:
            if existing.code == item.code:
                next_items.append(item)
                replaced = True
            else:
                next_items.append(existing)
        if not replaced:
            next_items.append(item)
        self.save_all(next_items)
        return item

    def delete(self, code: str) -> bool:
        items = self.list_items()
        next_items = [item for item in items if item.code != code]
        self.save_all(next_items)
        return len(next_items) != len(items)


class CloudBackedWatchlistStore(WatchlistStore):
    def __init__(self, path: Path = WATCHLIST_FILE, state_store: JsonStateStore | None = None) -> None:
        super().__init__(path)
        self.state_store = state_store

    def list_items(self) -> list[WatchlistItem]:
        cloud_items = self._load_cloud_items()
        if cloud_items is not None:
            super().save_all(cloud_items)
            return cloud_items
        return super().list_items()

    def save_all(self, items: list[WatchlistItem]) -> None:
        super().save_all(items)
        if self.state_store is None:
            return
        try:
            self.state_store.set_json(
                "settings",
                "watchlist",
                [item.model_dump(mode="json") for item in items],
            )
        except Exception:
            return

    def _load_cloud_items(self) -> list[WatchlistItem] | None:
        if self.state_store is None:
            return None
        try:
            raw = self.state_store.get_json("settings", "watchlist")
        except Exception:
            return None
        if not isinstance(raw, list):
            return None
        try:
            return [WatchlistItem.model_validate(item) for item in raw if isinstance(item, dict)]
        except Exception:
            return None


class PositionStore:
    """Small local position repository kept separate from watchlist settings."""

    def __init__(self, path: Path = POSITION_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list_items(self) -> list[PositionRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [PositionRecord.model_validate(item) for item in raw if isinstance(item, dict)]

    def get(self, code: str) -> PositionRecord | None:
        normalized = str(code).zfill(6)
        return next((item for item in self.list_items() if item.code == normalized), None)

    def upsert(self, item: PositionRecord) -> PositionRecord:
        normalized = item.model_copy(
            update={
                "code": str(item.code).zfill(6),
                "updated_at": item.updated_at or datetime.now().isoformat(timespec="seconds"),
            }
        )
        items = self.list_items()
        replaced = False
        next_items: list[PositionRecord] = []
        for existing in items:
            if existing.code == normalized.code:
                next_items.append(normalized)
                replaced = True
            else:
                next_items.append(existing)
        if not replaced:
            next_items.append(normalized)
        self._save(next_items)
        return normalized

    def delete(self, code: str) -> bool:
        normalized = str(code).zfill(6)
        items = self.list_items()
        next_items = [item for item in items if item.code != normalized]
        self._save(next_items)
        return len(next_items) != len(items)

    def _save(self, items: list[PositionRecord]) -> None:
        self.path.write_text(
            json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class CloudBackedPositionStore(PositionStore):
    def __init__(self, path: Path = POSITION_FILE, state_store: JsonStateStore | None = None) -> None:
        super().__init__(path)
        self.state_store = state_store

    def list_items(self) -> list[PositionRecord]:
        cloud_items = self._load_cloud_items()
        if cloud_items is not None:
            super()._save(cloud_items)
            return cloud_items
        return super().list_items()

    def _save(self, items: list[PositionRecord]) -> None:
        super()._save(items)
        if self.state_store is None:
            return
        try:
            self.state_store.set_json(
                "settings",
                "positions",
                [item.model_dump(mode="json") for item in items],
            )
        except Exception:
            return

    def _load_cloud_items(self) -> list[PositionRecord] | None:
        if self.state_store is None:
            return None
        try:
            raw = self.state_store.get_json("settings", "positions")
        except Exception:
            return None
        if not isinstance(raw, list):
            return None
        try:
            return [PositionRecord.model_validate(item) for item in raw if isinstance(item, dict)]
        except Exception:
            return None


class ThemeStore:
    def __init__(self, path: Path = THEMES_FILE) -> None:
        self.path = path

    def list_themes(self) -> list[dict]:
        data = load_yaml(self.path, {"themes": []})
        themes = data.get("themes", [])
        if not isinstance(themes, list):
            return []
        return themes

    def themes_for_code(self, code: str) -> list[str]:
        names: list[str] = []
        for theme in self.list_themes():
            members = set(theme.get("members", [])) | set(theme.get("core_codes", []))
            if code in members:
                names.append(theme.get("name", ""))
        return [name for name in names if name]


class AnalysisStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (DATA_DIR / "runtime" / "analysis")
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, record: AnalysisRecord) -> AnalysisRecord:
        path = self._path_for(record.code, record.trade_date, record.generated_at)
        path.write_text(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        return record

    def load(self, code: str, trade_date: str | None = None) -> AnalysisRecord | None:
        if trade_date:
            path = self._path_for_latest(code, trade_date)
            if path and path.exists():
                return self._read(path)
        latest = self._latest_path(code)
        return self._read(latest) if latest else None

    def _latest_path(self, code: str) -> Path | None:
        paths = sorted(self.root.glob(f"{code}_*.json"))
        return paths[-1] if paths else None

    def _path_for_latest(self, code: str, trade_date: str) -> Path:
        paths = sorted(self.root.glob(f"{code}_{trade_date}_*.json"))
        return paths[-1] if paths else self._path_for(code, trade_date, datetime.now().isoformat())

    def _path_for(self, code: str, trade_date: str, generated_at: str) -> Path:
        stamp = (
            generated_at.replace(":", "")
            .replace("-", "")
            .replace("T", "_")
            .replace(".", "")
        )
        return self.root / f"{code}_{trade_date}_{stamp}.json"

    def _read(self, path: Path | None) -> AnalysisRecord | None:
        if not path or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return AnalysisRecord.model_validate(payload)
        except Exception:
            return None
