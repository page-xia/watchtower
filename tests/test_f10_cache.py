"""F10 持久缓存层（F10CacheStore + TushareF10DataSource 三层缓存）测试。"""

from __future__ import annotations

import time
from pathlib import Path

from app.data_sources import EasyTdxDetailDataSource
from app.f10_store import F10CacheStore
from app.f10_tushare import TushareF10DataSource
from app.models import DetailDataPayload, DetailDataTable, F10Category, F10Payload


class _SettingsStub:
    fundamentals_cache_seconds = 3600
    f10_cache_seconds = 64800
    easy_tdx_f10_timeout_seconds = 4.0
    secret_config: dict = {}


def _payload(code: str = "300476", name: str = "测试股份") -> F10Payload:
    return F10Payload(
        available=True,
        code=code,
        ts_code=f"{code}.SZ",
        name=name,
        fetched_at="2026-08-17T08:40:00",
        category_count=1,
        expected_category_count=13,
        categories=[F10Category(key="company_profile", title="公司概况", available=True)],
    )


# ---------------------------------------------------------------------------
# F10CacheStore
# ---------------------------------------------------------------------------


def test_store_roundtrip_local(tmp_path: Path) -> None:
    store = F10CacheStore(tmp_path / "f10_cache")
    payload = _payload().model_dump(mode="json")

    assert store.load("300476") is None

    store.save("300476", payload, fetched_ts=time.time())
    doc = store.load("300476")
    assert doc is not None
    assert doc["payload"]["name"] == "测试股份"
    assert doc["fetched_ts"] > 0
    assert store.age_seconds("300476") is not None
    assert store.age_seconds("300476") < 60

    index = store.list_index()
    assert "300476" in index


def test_store_index_sorted_and_capped(tmp_path: Path) -> None:
    store = F10CacheStore(tmp_path / "f10_cache")
    store.save("000001", _payload("000001").model_dump(mode="json"), fetched_ts=100.0)
    store.save("300476", _payload().model_dump(mode="json"), fetched_ts=200.0)
    index = store.list_index()
    assert index == {"000001": 100.0, "300476": 200.0}


def test_store_corrupt_file_returns_none(tmp_path: Path) -> None:
    cache_dir = tmp_path / "f10_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "300476.json").write_text("{not json", encoding="utf-8")
    store = F10CacheStore(cache_dir)
    assert store.load("300476") is None


# ---------------------------------------------------------------------------
# TushareF10DataSource 三层缓存
# ---------------------------------------------------------------------------


def _source_with_fresh_store(tmp_path: Path) -> TushareF10DataSource:
    store = F10CacheStore(tmp_path / "f10_cache")
    store.save("300476", _payload().model_dump(mode="json"), fetched_ts=time.time())
    return TushareF10DataSource(_SettingsStub(), store=store)


def test_fetch_hits_fresh_persistent_cache_without_network(tmp_path: Path) -> None:
    source = _source_with_fresh_store(tmp_path)

    def _no_network() -> None:
        raise AssertionError("不应触网：持久缓存新鲜")

    source._client = _no_network  # type: ignore[assignment]
    payload = source.fetch("300476")
    assert payload.available
    assert payload.name == "测试股份"
    # 命中后回填内存缓存
    assert "300476" in source._cache


def test_fetch_expired_persistent_cache_falls_through_to_live(tmp_path: Path) -> None:
    store = F10CacheStore(tmp_path / "f10_cache")
    store.save("300476", _payload().model_dump(mode="json"), fetched_ts=time.time() - 7 * 86400)
    source = TushareF10DataSource(_SettingsStub(), store=store)

    def _no_network() -> None:
        raise RuntimeError("无 token")

    source._client = _no_network  # type: ignore[assignment]
    payload = source.fetch("300476")
    # 实时拉取失败 → 兜底返回过期缓存，并在 note 中说明
    assert payload.name == "测试股份"
    assert "缓存数据" in payload.note


def test_refresh_stale_skips_fresh_entries(tmp_path: Path) -> None:
    source = _source_with_fresh_store(tmp_path)

    def _boom(code: str, force: bool = False):  # noqa: ARG001
        raise AssertionError("新鲜缓存不应被刷新")

    source.fetch = _boom  # type: ignore[assignment]
    stats = source.refresh_stale(["300476"], max_age_seconds=43200, limit=10)
    assert stats["skipped_fresh"] == 1
    assert stats["refreshed"] == []
    assert stats["failed"] == []


def test_refresh_stale_filters_invalid_codes(tmp_path: Path) -> None:
    source = _source_with_fresh_store(tmp_path)
    stats = source.refresh_stale(["abc", "", "300476"], max_age_seconds=43200, limit=10)
    assert stats["candidates"] == 1


def test_merge_with_cached_keeps_old_category_on_rate_limit(tmp_path: Path) -> None:
    """新拉取的分类被限流（有 error 且不可用）时，持久层保留旧缓存里的完整分类。"""
    store = F10CacheStore(tmp_path / "f10_cache")
    full = _payload()  # company_profile 可用
    full.categories.append(
        F10Category(key="research_reports", title="研报与盈利预测", available=True)
    )
    full.category_count = 2
    store.save("300476", full.model_dump(mode="json"), fetched_ts=time.time() - 86400)

    source = TushareF10DataSource(_SettingsStub(), store=store)
    partial = _payload()
    partial.categories.append(
        F10Category(key="research_reports", title="研报与盈利预测", available=False, error="频率超限")
    )
    merged = source._merge_with_cached("300476", partial)
    research = next(c for c in merged.categories if c.key == "research_reports")
    assert research.available  # 旧缓存分类补位
    assert merged.category_count == 2

    # 全新无缓存时不做合并（重新构造一份，避免被上一步原地修改）
    partial2 = _payload("000001")
    partial2.categories.append(
        F10Category(key="research_reports", title="研报与盈利预测", available=False, error="频率超限")
    )
    fresh = source._merge_with_cached("000001", partial2)
    assert fresh.category_count == 1


# ---------------------------------------------------------------------------
# EasyTdxDetailDataSource 持久缓存（资金流/技术指标/缠论）
# ---------------------------------------------------------------------------


def _detail_payload(kind: str, code: str = "300476") -> DetailDataPayload:
    return DetailDataPayload(
        available=True,
        source=kind,
        code=code,
        fetched_at="2026-08-17T08:40:00",
        summary={"main_net": 123.0},
        tables=[DetailDataTable(title="t", columns=["date"], rows=[{"date": "2026-08-14"}], row_count=1)],
    )


def test_detail_extras_persistent_cache_avoids_reload(tmp_path: Path) -> None:
    store = F10CacheStore(tmp_path / "detail_extras", namespace="detail_extras")
    ds = EasyTdxDetailDataSource(_SettingsStub(), store=store)

    calls = {"n": 0}

    def loader(code: str) -> DetailDataPayload:
        calls["n"] += 1
        return _detail_payload("capital_flow", code)

    first = ds._cached("capital_flow", "300476", loader)
    assert first.available and calls["n"] == 1
    assert store.load("capital_flow:300476") is not None

    # 清空内存缓存，模拟重启：第二次应命中持久层，不再调用 loader
    ds._cache.clear()
    second = ds._cached("capital_flow", "300476", loader)
    assert second.available and calls["n"] == 1
    assert second.summary["main_net"] == 123.0


def test_detail_extras_refresh_stale(tmp_path: Path) -> None:
    store = F10CacheStore(tmp_path / "detail_extras", namespace="detail_extras")
    ds = EasyTdxDetailDataSource(_SettingsStub(), store=store)
    store.save("capital_flow:300476", _detail_payload("capital_flow").model_dump(mode="json"), time.time())

    calls: list[str] = []

    def fake_loader(code: str) -> DetailDataPayload:
        calls.append(code)
        return _detail_payload("x", code)

    ds._fetch_capital_flow = fake_loader  # type: ignore[assignment]
    ds._fetch_technical_indicators = fake_loader  # type: ignore[assignment]
    ds._fetch_chanlun = fake_loader  # type: ignore[assignment]

    stats = ds.refresh_stale(["300476"], max_age_seconds=43200, limit=10)
    # capital_flow 新鲜被跳过，另外两类被刷新
    assert stats["skipped_fresh"] == 1
    assert stats["refreshed"] == 2
    assert calls == ["300476", "300476"]
