"""Regression test: 板块资金动能成员口径必须让资金净流入 top-5 保底入围。

旧顺序是热度 top-N 先占席、资金榜补到 cap（N+2）即止——资金第 4/5 名
会被热度榜挤掉（2026-08-17 生产实例：资金第 4 的半导体设备缺席面板）。
修复后资金 top-5 优先进，热度按序补到 cap，被截断的是热度榜尾部。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from app.services import DashboardService


def _service() -> DashboardService:
    service = DashboardService.__new__(DashboardService)
    service._sector_flow_lock = threading.Lock()
    service._sector_flow_names_by_key = {}
    service.engine = SimpleNamespace(sector_flow_top_n=10)
    return service


def _sector(name: str, flow_delta: float) -> SimpleNamespace:
    return SimpleNamespace(name=name, flow_delta=flow_delta)


def test_money_top5_guaranteed_over_heat_tail() -> None:
    service = _service()
    # 热度 top-10（输入顺序即热度排名），资金净额都很小
    heat = [_sector(f"热度{i}", flow_delta=1.0) for i in range(1, 11)]
    # 资金 top-5：净流入远高于热度榜，但热度排在 10 名之外
    money = [_sector(f"资金{i}", flow_delta=50.0 - i) for i in range(1, 6)]
    sectors = heat + money

    selected = service._sector_flow_membership("k1", sectors)
    names = [sector.name for sector in selected]

    for i in range(1, 6):
        assert f"资金{i}" in names, f"资金第 {i} 名必须保底入围"
    assert len(names) <= 12  # cap = N+2
    # 资金 5 席保底后热度补 7 席，被截断的是热度榜尾部（8/9/10）
    assert "热度1" in names and "热度7" in names
    assert "热度10" not in names


def test_heat_top10_fully_kept_when_money_overlaps() -> None:
    service = _service()
    # 资金 top-5 全部落在热度 top-10 内：并集就是热度 top-10，无截断
    heat = [_sector(f"热度{i}", flow_delta=100.0 if i <= 5 else 1.0) for i in range(1, 11)]
    selected = service._sector_flow_membership("k2", heat)
    names = [sector.name for sector in selected]
    assert names[:10] == [f"热度{i}" for i in range(1, 11)]


def test_negative_money_flow_never_picked() -> None:
    service = _service()
    heat = [_sector(f"热度{i}", flow_delta=-5.0) for i in range(1, 11)]
    money = [_sector("净流出板块", flow_delta=-0.5)]
    selected = service._sector_flow_membership("k3", heat + money)
    names = [sector.name for sector in selected]
    assert "净流出板块" not in names


def test_hysteresis_retains_previous_within_extended_range() -> None:
    service = _service()
    first = [_sector(f"热度{i}", flow_delta=1.0) for i in range(1, 11)]
    service._sector_flow_membership("k4", first)
    # 下一轮：老朋友「热度10」掉到热度第 11（仍在 top-(N+3)=13 内），
    # 资金榜与热度榜完全重叠腾出空位 → 滞回保留老朋友
    second = [_sector(f"新热度{i}", flow_delta=1.0) for i in range(1, 11)]
    second.append(_sector("热度10", flow_delta=1.0))
    selected = service._sector_flow_membership("k4", second)
    names = [sector.name for sector in selected]
    assert "热度10" in names
