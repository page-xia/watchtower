"""板块资金动能数据质量回归测试（2026-08-17 生产事故）。

生产症状：
1. 锚定把 final_value 换成 L1 真值后不重排序 —— 消费电子组件(+50.27亿) 排在
   半导体材料(+24.71亿) 后面，榜单顺序与数值矛盾。
2. 形状与 L1 真值符号相反的已知错误数据被「守卫保留原曲线」放行 ——
   其他养殖展示 +0.76亿，而全成员外/内盘计数器真值是 -0.27亿（3/3 覆盖）。
3. 盘中快照代理曲线不定标，回退路径（成交额增量×方向）漂移后直接持久化。
4. 板块强弱卡的 flow_delta 用「成交额×涨跌幅」代理，与资金动能图的 L1 真值
   同屏两个数（集成电路设计 133.3亿 vs 221.7亿）。
5. _sample_mini_rows 把显式 max_points=120 静默钳回 48，轨迹回灌曲线欠采样。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from app.models import OrderFlowObservation, Quote, SectorFlowPoint, SectorFlowSeries
from app.services import DashboardService


def _quote(
    code: str,
    price: float,
    *,
    buy: float = 0.0,
    sell: float = 0.0,
    order_flow_available: bool = True,
    amount: float = 0.0,
    change_pct: float = 1.0,
) -> Quote:
    return Quote(
        code=code,
        name=f"股{code}",
        price=price,
        prev_close=price,
        open=price,
        high=price,
        low=price,
        day_high=price,
        day_low=price,
        change_pct=change_pct,
        amount=amount,
        updated_at="14:00:00",
        order_flow=OrderFlowObservation(
            available=order_flow_available,
            active_buy_volume=buy,
            active_sell_volume=sell,
        ),
    )


def _series(name: str, values: list[float], heat: int = 100) -> SectorFlowSeries:
    return SectorFlowSeries(
        name=name,
        heat_score=heat,
        final_value=round(sum(values), 2),
        change_pct=1.0,
        points=[SectorFlowPoint(time=f"09:{31 + i:02d}", value=value) for i, value in enumerate(values)],
        flow_basis="每分钟净流入(全成员L1主动量差，缺省用成交额增量×方向)",
    )


def _anchor_service() -> DashboardService:
    service = DashboardService.__new__(DashboardService)
    service._sector_flow_lock = threading.Lock()
    return service


def test_anchor_list_resorts_by_truth_total() -> None:
    """锚定把 total 换成真值后必须按真值重排：甲 shape 25→truth 5，乙 shape 10→truth 30。"""
    service = _anchor_service()
    quotes = [
        _quote("000001", 10, buy=600_000, sell=100_000),  # 净 50万手×10×100 = 5亿
        _quote("000002", 20, buy=1_600_000, sell=100_000),  # 净 150万手×20×100 = 30亿
    ]
    sectors = [SimpleNamespace(name="甲"), SimpleNamespace(name="乙")]
    loader = lambda sector: {"甲": ["000001"], "乙": ["000002"]}[sector.name]  # noqa: E731
    flow_list = [
        _series("甲", [10.0, 10.0, 5.0]),  # shape 25，输入顺序在前
        _series("乙", [6.0, 4.0]),  # shape 10
    ]

    anchored = service._anchor_flow_list_to_active_net(flow_list, sectors, quotes, loader)

    assert [item.name for item in anchored] == ["乙", "甲"], "锚定后必须按真值 final_value 重排"
    assert anchored[0].final_value == 30.0
    assert anchored[1].final_value == 5.0


def test_anchor_list_drops_sign_contradiction() -> None:
    """形状与 L1 真值符号相反（量级均 ≥0.1亿）是已知错误数据，必须下掉而不是展示。"""
    service = _anchor_service()
    quotes = [
        _quote("000003", 10, buy=73_000, sell=100_000),  # 净 -2.7万手×10×100 = -0.27亿
        _quote("000004", 10, buy=190_000, sell=100_000),  # 净 9万手×10×100 = +0.9亿
    ]
    sectors = [SimpleNamespace(name="其他养殖"), SimpleNamespace(name="丁")]
    loader = lambda sector: {"其他养殖": ["000003"], "丁": ["000004"]}[sector.name]  # noqa: E731
    flow_list = [
        _series("其他养殖", [0.5, 0.26]),  # shape +0.76，真值 -0.27，符号矛盾
        _series("丁", [0.3, 0.2]),  # shape +0.5，真值 +0.9，正常锚定
    ]

    anchored = service._anchor_flow_list_to_active_net(flow_list, sectors, quotes, loader)

    names = [item.name for item in anchored]
    assert "其他养殖" not in names, "符号矛盾的已知错误曲线不能留在面板上"
    assert "丁" in names
    assert anchored[0].final_value == 0.9


def _proxy_service() -> DashboardService:
    service = DashboardService.__new__(DashboardService)
    service._sector_flow_lock = threading.Lock()
    service._sector_flow_proxy_by_key = {}
    service._sector_flow_names_by_key = {}
    service._quote_tick_cache = {}
    service._quote_tick_cache_date = ""
    service.engine = SimpleNamespace(sector_flow_top_n=10)
    return service


def _snapshot(trade_date: str, clock: str, quotes: list[Quote]) -> SimpleNamespace:
    return SimpleNamespace(
        data_mode="live",
        quotes=quotes,
        source_status={"trade_date": trade_date, "clock_label": clock, "active_source": "easy_tdx"},
    )


def test_proxy_tick_anchors_to_snapshot_truth() -> None:
    """盘中代理曲线必须向当前快照 L1 真值定标：回退路径漂移被收敛到真值。

    5 成员板块：M1-M4 有 order_flow（净额真值合计 0.08亿，外推 5/4 → truth 0.1亿），
    M5 order_flow 不可用走「成交额增量×方向」回退，单 tick 贡献 +0.5亿漂移。
    未锚定时代理 total ≈ 0.56亿；锚定后必须收敛到 0.1亿。
    """
    service = _proxy_service()
    sector = SimpleNamespace(
        name="半导体",
        flow_delta=1.0,
        heat_score=100,
        avg_change_pct=2.0,
        leader_code="000001",
        leader_name="股000001",
        core_codes=["000001"],
        reasons=[],
    )
    members = ["000001", "000002", "000003", "000004", "000005"]
    loader = lambda _sector: members  # noqa: E731

    tick1 = [
        *[_quote(code, 10, buy=1_000, sell=500, amount=5e8) for code in members[:4]],
        _quote("000005", 10, order_flow_available=False, amount=5e8),
    ]
    tick2 = [
        *[_quote(code, 10, buy=3_000, sell=1_000, amount=6e8) for code in members[:4]],
        _quote("000005", 10.1, order_flow_available=False, amount=5.5e8, change_pct=1.0),
    ]
    service._sector_flow_proxy_tick("k", _snapshot("20260817", "13:59:00", tick1), [sector], loader)
    result = service._sector_flow_proxy_tick("k", _snapshot("20260817", "14:00:00", tick2), [sector], loader)

    assert len(result) == 1
    series = result[0]
    truth = round(4 * (2_000 * 10 * 100) / 1e8 * 5 / 4, 2)  # 0.1 亿
    assert abs(series.final_value - truth) < 1e-6, (
        f"代理曲线必须定标到 L1 真值 {truth} 亿，而不是漂移值 {series.final_value} 亿"
    )
    assert "L1主动量定标" in series.flow_basis


def test_aggregate_sector_snapshot_prefers_l1_truth() -> None:
    """板块强弱卡 flow_delta 与资金动能图同源：order_flow 覆盖足够时用 L1 真值。"""
    service = DashboardService.__new__(DashboardService)
    service.engine = SimpleNamespace(
        sector_metric_quotes=lambda quotes: (list(quotes), []),
        sector_exclusion_reason=lambda excluded: "",
    )
    quotes = [
        _quote("000001", 10, buy=600_000, sell=100_000, amount=1e9, change_pct=3.0),
        _quote("000002", 20, buy=300_000, sell=100_000, amount=5e8, change_pct=2.0),
    ]
    # L1 真值：50万手×10×100 + 20万手×20×100 = 5亿 + 4亿 = 9亿
    sector = service._aggregate_sector_snapshot(
        "集成电路设计",
        quotes,
        {},
        board_code="881322",
        board_level=3,
        board_source="easy_tdx_cached_members_local_quote_aggregation",
    )
    assert abs(sector.flow_delta - 9.0) < 1e-6, f"flow_delta 必须是 L1 真值 9 亿，得到 {sector.flow_delta}"
    assert any("主动净额" in reason for reason in sector.reasons)


def test_aggregate_sector_snapshot_falls_back_without_order_flow() -> None:
    """order_flow 不可用成员超 20% 时回退「成交额×涨跌幅」代理并保留旧文案。"""
    service = DashboardService.__new__(DashboardService)
    service.engine = SimpleNamespace(
        sector_metric_quotes=lambda quotes: (list(quotes), []),
        sector_exclusion_reason=lambda excluded: "",
    )
    quotes = [
        _quote("000001", 10, order_flow_available=False, amount=1e9, change_pct=2.0),
        _quote("000002", 20, order_flow_available=False, amount=5e8, change_pct=-1.0),
    ]
    sector = service._aggregate_sector_snapshot(
        "种子",
        quotes,
        {},
        board_code="",
        board_level=3,
        board_source="easy_tdx_cached_members_local_quote_aggregation",
    )
    # 代理口径：(10亿×2% + 5亿×-1%) = 0.15亿
    assert abs(sector.flow_delta - 0.15) < 1e-6
    assert any("动能代理" in reason for reason in sector.reasons)


def test_sample_mini_rows_honors_explicit_120_cap() -> None:
    """轨迹回灌显式要 120 点，不能被内部 48 上限静默钳位（欠采样出假波动）。"""
    rows = []
    labels = [f"09:{m:02d}" for m in range(30, 60)] + [f"10:{m:02d}" for m in range(0, 60)]
    labels += [f"11:{m:02d}" for m in range(0, 31)] + [f"13:{m:02d}" for m in range(0, 60)]
    labels += [f"14:{m:02d}" for m in range(0, 60)] + ["15:00"]
    labels = [label for label in labels if "09:30" <= label <= "11:30" or "13:00" <= label <= "15:00"][:240]
    for index, label in enumerate(labels):
        rows.append({"captured_at": label, "change_pct": 0.1 if index % 2 == 0 else -0.1})

    sampled = DashboardService._sample_mini_rows(rows, max_points=120)

    assert len(sampled) > 48, f"显式 max_points=120 不能被钳到 48，得到 {len(sampled)}"
    assert len(sampled) <= 120


def test_opening_backfill_checks_every_sector_not_only_earliest_series() -> None:
    """一个板块完整不能掩盖另一个板块午后才开始的残缺曲线。"""
    service = DashboardService.__new__(DashboardService)
    service.engine = SimpleNamespace(
        _session_times=lambda _count: [
            *[f"09:{minute:02d}" for minute in range(31, 60)],
            *[f"10:{minute:02d}" for minute in range(60)],
            *[f"11:{minute:02d}" for minute in range(31)],
            *[f"13:{minute:02d}" for minute in range(60)],
            *[f"14:{minute:02d}" for minute in range(60)],
            "15:00",
        ]
    )
    snapshot = SimpleNamespace(source_status={"clock_label": "15:00:00"})
    complete = _series("完整板块", [0.1] * 60)
    complete.points[0] = SectorFlowPoint(time="09:31", value=0.1)
    complete.points[-1] = SectorFlowPoint(time="15:00", value=0.1)
    late = _series("午后残缺", [0.1] * 5)
    late.points[0] = SectorFlowPoint(time="13:29", value=0.1)
    late.points[-1] = SectorFlowPoint(time="15:00", value=0.1)

    assert service._sector_flow_needs_opening_backfill([complete, late], snapshot) is True


def test_live_tail_check_checks_every_sector_not_only_latest_series() -> None:
    """一个板块追到当前分钟不能掩盖另一个板块尾盘断流。"""
    service = DashboardService.__new__(DashboardService)
    service.engine = SimpleNamespace(
        _session_times=lambda _count: [
            *[f"09:{minute:02d}" for minute in range(31, 60)],
            *[f"10:{minute:02d}" for minute in range(60)],
            *[f"11:{minute:02d}" for minute in range(31)],
            *[f"13:{minute:02d}" for minute in range(60)],
            *[f"14:{minute:02d}" for minute in range(60)],
            "15:00",
        ]
    )
    snapshot = SimpleNamespace(source_status={"clock_label": "15:00:00"})
    current = _series("当前完整", [0.1, 0.2])
    current.points = [
        SectorFlowPoint(time="09:31", value=0.1),
        SectorFlowPoint(time="15:00", value=0.2),
    ]
    stale = _series("尾盘断流", [0.1, 0.2])
    stale.points = [
        SectorFlowPoint(time="09:31", value=0.1),
        SectorFlowPoint(time="14:20", value=0.2),
    ]

    assert service._sector_flow_needs_live_tail([current, stale], snapshot) is True


def test_trajectory_sector_member_reads_are_batched() -> None:
    """大板块成员必须分批读取，避免一次物化数百万条 tick 导致生产 OOM。"""
    service = DashboardService.__new__(DashboardService)
    member_codes = [f"{index:06d}" for index in range(1, 65)]
    batch_sizes: list[int] = []

    def load_ticks(_trade_date: str, codes: list[str]) -> dict[str, list[dict]]:
        batch_sizes.append(len(codes))
        return {
            code: [
                {"time": "09:30", "price": 10.0, "amount": 0.0},
                {"time": "09:31", "price": 10.1, "amount": 1_000_000.0},
                {"time": "09:32", "price": 10.2, "amount": 2_000_000.0},
            ]
            for code in codes
        }

    service.trajectory_store = SimpleNamespace(stock_feature_ticks_by_code=load_ticks)
    sector = SimpleNamespace(
        name="大板块",
        heat_score=80,
        avg_change_pct=1.0,
        leader_code=member_codes[0],
        leader_name="龙头",
        core_codes=member_codes[:3],
        reasons=[],
    )

    result = service._sector_flow_from_stock_trajectory(
        "20260820",
        [sector],
        lambda _sector: member_codes,
        [],
    )

    assert result
    assert len(batch_sizes) > 1
    assert max(batch_sizes) <= 16


def test_background_rebuild_serializes_without_dropping_other_board_levels(monkeypatch) -> None:
    """全局单飞应排队不同板块层级，而不是静默丢掉后到的重建。"""
    service = DashboardService.__new__(DashboardService)
    service._sector_flow_lock = threading.Lock()
    service._sector_flow_rebuild_lock = threading.Lock()
    service._sector_flow_refresh_threads = {}
    service._sector_flow_cache_by_key = {}
    service._sector_flow_names_by_key = {}
    service.engine = SimpleNamespace(sector_flow_top_n=10)
    service.settings = SimpleNamespace(sector_flow_minute_refresh_seconds=60)
    started = threading.Event()
    release = threading.Event()
    built: list[str] = []
    complete_labels = [f"09:{minute:02d}" for minute in range(31, 60)]
    complete_labels += [f"10:{minute:02d}" for minute in range(50)]
    complete_labels += ["15:00"]

    def fake_build(cache_key, *_args, **_kwargs):
        built.append(cache_key)
        if cache_key == "level-1":
            started.set()
            assert release.wait(timeout=5)
        item = _series("板块", [0.1] * len(complete_labels))
        item.points = [SectorFlowPoint(time=label, value=0.1) for label in complete_labels]
        return [item]

    monkeypatch.setattr(service, "_build_and_cache_sector_flow", fake_build)
    monkeypatch.setattr(service, "_clear_payload_caches", lambda: None)
    snapshot = SimpleNamespace(
        source_status={"trade_date": "20260820", "clock_label": "15:00:00", "frozen": True},
        data_mode="live",
    )
    sectors = [SimpleNamespace(name="板块", flow_delta=1.0)]

    service._schedule_sector_flow_trajectory_refresh("level-1", snapshot, sectors, None)
    assert started.wait(timeout=5)
    service._schedule_sector_flow_trajectory_refresh("level-2", snapshot, sectors, None)
    threads = list(service._sector_flow_refresh_threads.values())
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert built == ["level-1", "level-2"]
    assert set(service._sector_flow_cache_by_key) == {"level-1", "level-2"}


def test_closed_sector_flow_rejects_sparse_full_span_series() -> None:
    """有开盘和收盘端点也不够：全日仅 67 点仍是生产上可见的断档曲线。"""
    service = DashboardService.__new__(DashboardService)
    labels = [f"09:{minute:02d}" for minute in range(31, 60)]
    labels += [f"10:{minute:02d}" for minute in range(38)]
    labels[-1] = "15:00"
    series = SectorFlowSeries(
        name="社会服务",
        heat_score=70,
        final_value=9.25,
        change_pct=1.0,
        points=[SectorFlowPoint(time=label, value=0.1) for label in labels],
    )
    snapshot = SimpleNamespace(source_status={"clock_label": "15:00:00"})

    assert len(series.points) == 67
    assert service._sector_flow_series_covers_snapshot(series, snapshot) is False


def test_trajectory_gap_and_sampling_preserve_total_flow() -> None:
    """采集中断分摊与 120 点压缩都必须守恒，不能让前端积分少算资金。"""
    service = DashboardService.__new__(DashboardService)
    member = "000001"
    ticks = [{"time": "09:30", "price": 10.0, "amount": 0.0}]
    amount = 0.0
    for minute in range(31, 60, 3):
        amount += 300_000_000.0
        ticks.append({"time": f"09:{minute:02d}", "price": 10.0 + minute / 100, "amount": amount})
    for hour in (10, 13, 14):
        for minute in range(0, 60, 3):
            if hour == 14 and minute > 57:
                break
            amount += 300_000_000.0
            ticks.append({"time": f"{hour:02d}:{minute:02d}", "price": 11.0 + hour / 100 + minute / 1000, "amount": amount})
    ticks.append({"time": "15:00", "price": 12.0, "amount": amount + 300_000_000.0})

    service.trajectory_store = SimpleNamespace(
        stock_feature_ticks_by_code=lambda _date, _codes: {member: ticks}
    )
    sector = SimpleNamespace(
        name="守恒板块",
        heat_score=80,
        avg_change_pct=1.0,
        leader_code=member,
        leader_name="守恒票",
        core_codes=[member],
        reasons=[],
    )

    result = service._sector_flow_from_stock_trajectory(
        "20260820", [sector], lambda _sector: [member], []
    )

    assert len(result) == 1
    series = result[0]
    assert len(series.points) <= 120
    assert abs(sum(point.value for point in series.points) - series.final_value) <= 0.05


def test_long_gap_spreads_across_all_trading_minutes() -> None:
    """长空窗不能把一小时累计额伪装成最后 5 分钟的资金尖峰。"""
    labels = DashboardService._flow_gap_labels("09:30", "10:30")

    assert labels[0] == "09:31"
    assert labels[-1] == "10:30"
    assert len(labels) == 60


def test_incomplete_background_rebuild_is_throttled_and_not_published(monkeypatch) -> None:
    """持续残缺的上游结果不能清缓存自激重建，也不能立刻重复拉分钟线。"""
    service = DashboardService.__new__(DashboardService)
    service._sector_flow_lock = threading.Lock()
    service._sector_flow_rebuild_lock = threading.Lock()
    service._sector_flow_refresh_threads = {}
    service._sector_flow_cache_by_key = {}
    service._sector_flow_names_by_key = {}
    service._sector_flow_last_build_at = {}
    service.settings = SimpleNamespace(sector_flow_minute_refresh_seconds=60)
    service.engine = SimpleNamespace(sector_flow_top_n=10)
    builds: list[str] = []
    clears: list[bool] = []
    sparse = _series("板块", [0.1, 0.2])
    sparse.points = [
        SectorFlowPoint(time="14:59", value=0.1),
        SectorFlowPoint(time="15:00", value=0.2),
    ]

    def fake_build(cache_key, *_args, **_kwargs):
        builds.append(cache_key)
        return [sparse]

    monkeypatch.setattr(service, "_build_and_cache_sector_flow", fake_build)
    monkeypatch.setattr(service, "_clear_payload_caches", lambda: clears.append(True))
    snapshot = SimpleNamespace(
        source_status={"trade_date": "20260820", "clock_label": "15:00:00", "frozen": True},
        data_mode="live",
    )
    sectors = [SimpleNamespace(name="板块", flow_delta=1.0)]

    service._schedule_sector_flow_trajectory_refresh("level-1", snapshot, sectors, None)
    for thread in list(service._sector_flow_refresh_threads.values()):
        thread.join(timeout=5)
    service._schedule_sector_flow_trajectory_refresh("level-1", snapshot, sectors, None)

    assert builds == ["level-1"]
    assert "level-1" not in service._sector_flow_cache_by_key
    assert clears == []
