from __future__ import annotations

import os

from app.config import AppSettings
from app.data_sources import BoardContext, EasyTdxBoardDataSource
from app.models import SectorSnapshot
from app.services import DashboardService
from app.storage import CloudBackedPositionStore, CloudBackedWatchlistStore


class MemoryStateStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def get_json(self, namespace: str, key: str, default=None):
        return self.values.get((namespace, key), default)

    def set_json(self, namespace: str, key: str, value) -> None:
        self.values[(namespace, key)] = value


def _sector(name: str = "光纤光缆") -> SectorSnapshot:
    return SectorSnapshot(
        name=name,
        heat_score=80,
        avg_change_pct=2.2,
        up_count=2,
        total_count=3,
        limit_up_count=0,
        opened_limit_count=0,
        core_attack=True,
        core_codes=["300476"],
        leader_code="300476",
        leader_name="胜宏科技",
        reasons=["test"],
        board_code="881001",
        board_level=3,
        board_source="easy_tdx_mac_board_ranking",
    )


def test_app_settings_reads_cloudbase_persistence_environment(monkeypatch) -> None:
    monkeypatch.setenv("WATCH_PERSISTENCE_BACKEND", "cloudbase_nosql")
    monkeypatch.setenv("WATCH_CLOUDBASE_ENV_ID", "server-d2g7x597t019f5cb0")
    monkeypatch.setenv("WATCH_CLOUDBASE_API_TOKEN", "token")
    monkeypatch.setenv("WATCH_CLOUDBASE_STATE_COLLECTION", "custom_state")
    monkeypatch.setenv("WATCH_CLOUDBASE_MYSQL_INSTANCE", "default")
    monkeypatch.setenv("WATCH_CLOUDBASE_MYSQL_SCHEMA", "server-d2g7x597t019f5cb0")

    settings = AppSettings()

    assert settings.persistence_backend == "cloudbase_nosql"
    assert settings.cloudbase_env_id == "server-d2g7x597t019f5cb0"
    assert settings.cloudbase_api_token == "token"
    assert settings.cloudbase_state_collection == "custom_state"
    assert settings.message_store_backend == "cloudbase_mysql"
    assert settings.cloudbase_mysql_instance == "default"
    assert settings.cloudbase_mysql_schema == "server-d2g7x597t019f5cb0"
    assert settings.public_source_status["persistence_backend"] == "cloudbase_nosql"
    assert settings.public_source_status["message_store_backend"] == "cloudbase_mysql"
    assert settings.public_source_status["message_store_configured"] is True
    assert settings.public_source_status["cloud_persistence_configured"] is True


def test_dashboard_service_uses_cloud_backed_stores_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("WATCH_PERSISTENCE_BACKEND", "cloudbase_nosql")
    monkeypatch.setenv("WATCH_CLOUDBASE_ENV_ID", "server-d2g7x597t019f5cb0")
    monkeypatch.setenv("WATCH_CLOUDBASE_API_TOKEN", "token")
    settings = AppSettings()

    service = DashboardService(settings, state_store=object())

    assert isinstance(service.watchlist_store, CloudBackedWatchlistStore)
    assert isinstance(service.position_store, CloudBackedPositionStore)
    assert service.trajectory_store.state_store is not None
    assert service.data_source.boards.state_store is service.state_store


def test_easy_tdx_board_context_restores_members_from_cloud_state(tmp_path, monkeypatch) -> None:
    settings = AppSettings()
    settings.data_dir = tmp_path
    cloud = MemoryStateStore()
    cloud.set_json(
        "board_context",
        "level_3",
        {
            "board_level": 3,
            "source": "easy_tdx_mac_board_ranking",
            "available": True,
            "fetched_at": "2026-08-14T10:00:00",
            "sectors": [_sector().model_dump(mode="json")],
            "name_to_code": {"光纤光缆": "881001"},
            "code_to_name": {"881001": "光纤光缆"},
            "members_by_code": {"881001": ["300476", "300308"]},
            "error": "",
        },
    )
    source = EasyTdxBoardDataSource(settings, state_store=cloud)
    monkeypatch.setattr(
        source,
        "_fetch_context",
        lambda level: (_ for _ in ()).throw(AssertionError("cloud board context should avoid network cold fetch")),
    )

    context = source.fetch_context(3)

    assert context.available is True
    assert context.name_to_code["光纤光缆"] == "881001"
    assert context.members_by_code["881001"] == ["300476", "300308"]


def test_easy_tdx_board_context_persists_members_to_cloud_state(tmp_path) -> None:
    settings = AppSettings()
    settings.data_dir = tmp_path
    cloud = MemoryStateStore()
    source = EasyTdxBoardDataSource(settings, state_store=cloud)
    context = BoardContext(
        board_level=3,
        source="easy_tdx_mac_board_ranking",
        available=True,
        fetched_at="2026-08-14T10:00:00",
        sectors=[_sector()],
        name_to_code={"光纤光缆": "881001"},
        code_to_name={"881001": "光纤光缆"},
        members_by_code={"881001": ["300476", "300308"]},
    )

    source._persist_context_to_disk(3, context, force=True)

    payload = cloud.values[("board_context", "level_3")]
    assert payload["members_by_code"]["881001"] == ["300476", "300308"]


def test_dashboard_service_keeps_local_stores_when_cloud_not_configured(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("WATCH_CLOUDBASE_") or key == "WATCH_PERSISTENCE_BACKEND":
            monkeypatch.delenv(key, raising=False)
    settings = AppSettings()

    service = DashboardService(settings)

    assert not isinstance(service.watchlist_store, CloudBackedWatchlistStore)
    assert service.trajectory_store.state_store is None
