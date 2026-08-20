"""做T分时公式（做T公式.md / zuot_tdx_levels_v1）的单元测试。"""

import time

from app.formula_engine import (
    BIG_MINUTE_AMOUNT_YUAN,
    SIGNAL_VERSION,
    ZuoTDayContext,
    compute_zuot_series,
    compute_zuot_snapshot,
    compute_zuot_state,
    cross,
    ema,
    latest_zuot_event,
    longcross,
)


def _rows(closes, vol=1000.0):
    """构造分钟行：vol 单位手，amount 由 vol*close*100 推导（元）。"""
    return [
        {"time": f"09:{31 + i:02d}", "close": c, "vol": vol}
        for i, c in enumerate(closes)
    ]


# 标准日上下文：l1=100, h1=116, p1=16 → 支撑=101, 阻力=114, 中轴=107.5
DAY = ZuoTDayContext(
    prev_close=100.0,
    day_high=116.0,
    day_low=100.0,
    change_pct=2.0,
    volume_ratio=1.2,
    turnover_rate=3.5,
    total_amount=100_000_000.0,  # 1亿 → 10000万
    outer_volume=6000.0,
    inner_volume=4000.0,
)


def test_ema_matches_tdx_recursion():
    values = [10.0, 11.0, 12.0, 13.0]
    result = ema(values, 3)
    alpha = 2.0 / 4.0
    assert result[0] == 10.0
    for i in range(1, len(values)):
        expected = alpha * values[i] + (1 - alpha) * result[i - 1]
        assert abs(result[i] - expected) < 1e-9


def test_cross_scalar_and_series():
    # 上穿常量
    assert cross([1.0, 2.0, 3.0], 2.0) == [False, False, True]
    # 下穿不算（CROSS 只认上穿）
    assert cross([3.0, 2.0, 1.0], 2.0) == [False, False, False]
    # 序列对序列
    assert cross([1.0, 3.0], [2.0, 2.0]) == [False, True]
    # 首根永远 False
    assert cross([5.0], 1.0) == [False]


def test_longcross_requires_sustained_below():
    # left 在前 2 根一直低于 right，本根上穿 → True
    assert longcross([1.0, 1.0, 2.0], [1.5, 1.5, 1.5], 2) == [False, False, True]
    # 只有 1 根低于 → False（不满足 N=2 持续低于）
    assert longcross([2.0, 1.0, 2.0], [1.5, 1.5, 1.5], 2) == [False, False, False]
    # 未上穿 → False
    assert longcross([1.0, 1.0, 1.2], [1.5, 1.5, 1.5], 2) == [False, False, False]


def test_day_context_levels():
    assert DAY.h1 == 116.0
    assert DAY.l1 == 100.0
    assert abs(DAY.resistance - 114.0) < 1e-9  # 100 + 16*7/8
    assert abs(DAY.support - 101.0) < 1e-9  # 100 + 16*0.5/8
    assert abs(DAY.mid - 107.5) < 1e-9
    assert DAY.levels_available


def test_day_context_unavailable_levels_are_zero():
    empty = ZuoTDayContext()
    assert not empty.levels_available
    assert empty.resistance == 0.0
    assert empty.support == 0.0
    assert empty.mid == 0.0


def test_empty_rows_return_empty():
    result = compute_zuot_series([], DAY)
    assert result.states == []
    assert result.latest == {}


def test_vwap_is_cumulative():
    rows = [
        {"time": "09:31", "close": 10.0, "vol": 100.0},
        {"time": "09:32", "close": 20.0, "vol": 300.0},
    ]
    result = compute_zuot_series(rows, DAY)
    # 第一根 vwap = close（等量兜底）；第二根 = (10*100 + 20*300) / (100+300) = 17.5
    assert abs(result.states[0]["vwap"] - 10.0) < 1e-6
    assert abs(result.states[1]["vwap"] - 17.5) < 1e-6


def test_big_minute_amount_classification():
    big = BIG_MINUTE_AMOUNT_YUAN + 1.0  # 160万+1元
    rows = [
        {"time": "09:31", "close": 100.0, "vol": 10.0, "amount": 1000.0},
        {"time": "09:32", "close": 101.0, "vol": 10.0, "amount": big},  # 上涨大单 → A2
        {"time": "09:33", "close": 100.5, "vol": 10.0, "amount": big},  # 下跌大单 → A3
        {"time": "09:34", "close": 100.6, "vol": 10.0, "amount": 1000.0},  # 小单不计
    ]
    result = compute_zuot_series(rows, DAY)
    last = result.states[-1]
    wan = big / 10_000.0
    # states 层 big_buy/big_sell 保留两位小数（万元）
    assert abs(last["big_buy_amount"] - wan) < 0.01
    assert abs(last["big_sell_amount"] - wan) < 0.01
    assert abs(last["fund_flow"]) < 0.02  # 买卖抵消
    # 首根（index=0）即使金额达标也不累计（无上一根可比方向）
    first = result.states[0]
    assert first["big_buy_amount"] == 0
    assert first["big_sell_amount"] == 0


def test_buy_signal_on_support_reclaim_break():
    # 支撑=101：前 3 根现价在支撑上方，第 4 根跌破支撑 → LONGCROSS(支撑,现价,2) 触发买
    closes = [102.0, 102.5, 102.2, 100.5]
    result = compute_zuot_series(_rows(closes), DAY)
    signals = [s["buy_signal"] for s in result.states]
    assert signals == [False, False, False, True]


def test_sell_signal_on_resistance_breakout():
    # 阻力=114：前 3 根现价在阻力下方，第 4 根突破阻力 → LONGCROSS(现价,阻力,2) 触发卖
    closes = [112.0, 113.0, 113.5, 114.5]
    result = compute_zuot_series(_rows(closes), DAY)
    signals = [s["sell_signal"] for s in result.states]
    assert signals == [False, False, False, True]


def test_no_signals_without_levels():
    result = compute_zuot_series(_rows([100.0, 99.0, 98.0]))
    assert all(not s["buy_signal"] and not s["sell_signal"] for s in result.states)


def test_score_and_advice_full_marks():
    # 单边上行：ma30>qs、现价>vwap、现价>支撑；再加一根上涨大单让 A2>A3 → 满分 100
    closes = [100.0 + i * 0.5 for i in range(40)]
    rows = _rows(closes)
    rows[-1]["amount"] = BIG_MINUTE_AMOUNT_YUAN * 2  # 尾盘上涨大单
    latest = compute_zuot_state(rows, DAY)
    assert latest["score"] == 100
    assert latest["advice"].startswith("★强推")
    assert latest["trend_text"] == "多头强势"
    assert latest["position_text"] in {"强势区", "突破阻力"}
    assert latest["vwap_relation"] == "↑均价"
    assert latest["source_quality"] == SIGNAL_VERSION


def test_score_zero_in_downtrend_below_support():
    # 单边下行且现价跌破支撑 → 0 分规避
    closes = [115.0 - i * 0.5 for i in range(40)]
    latest = compute_zuot_state(_rows(closes), DAY)
    assert latest["score"] == 0
    assert latest["advice"].startswith("X规避")
    assert latest["position_text"] == "跌破支撑"


def test_outer_inner_amount_split():
    # 总额 1亿=10000万，外盘 6000 / 内盘 4000 → 买 6000万 卖 4000万 净 +2000万
    latest = compute_zuot_state(_rows([102.0, 102.5, 102.8]), DAY)
    assert abs(latest["buy_amount_wan"] - 6000.0) < 1e-6
    assert abs(latest["sell_amount_wan"] - 4000.0) < 1e-6
    assert abs(latest["net_amount_wan"] - 2000.0) < 1e-6
    assert abs(latest["buy_pct"] - 60.0) < 1e-6
    assert abs(latest["sell_pct"] - 40.0) < 1e-6


def test_latest_texts_and_lines():
    latest = compute_zuot_state(_rows([102.0, 102.5, 102.8]), DAY)
    assert latest["volume_text"] == "平量"  # volume_ratio=1.2
    assert latest["line_a"].startswith("个股 ")
    assert "|" in latest["line_a"]
    assert latest["line_b"].startswith("资金")
    assert latest["change_pct"] == 2.0
    assert latest["turnover_rate"] == 3.5
    assert latest["volume_ratio"] == 1.2


def test_latest_zuot_event_window():
    closes = [102.0, 102.5, 102.2, 100.5]  # 第 4 根触发买信号
    states = compute_zuot_series(_rows(closes), DAY).states
    event = latest_zuot_event(states, recent_bars=3)
    assert event is not None
    assert event["signal"] == "buy"
    assert event["index"] == 3
    assert event["invalidation_price"] == states[3]["support"]
    # 窗口只盖最近 1 根时事件仍可见；空序列返回 None
    assert latest_zuot_event([], recent_bars=3) is None
    # 信号在窗口之外（recent_bars=0 视为 1，只看最后一根）
    earlier = compute_zuot_series(_rows([102.0, 102.5, 102.2, 100.5, 101.5, 101.8]), DAY).states
    event2 = latest_zuot_event(earlier, recent_bars=2)
    assert event2 is None


def test_point_in_time_and_performance():
    # 240 根分钟线：全量递推必须秒级内完成（回归：旧公式此处 O(n²) 级 CPU）
    closes = [100.0 + (i % 17) * 0.3 for i in range(240)]
    rows = _rows(closes)
    start = time.perf_counter()
    result = compute_zuot_series(rows, DAY)
    elapsed = time.perf_counter() - start
    assert len(result.states) == 240
    assert elapsed < 0.5
    # point-in-time：前 i 根的最新状态与全量第 i 个状态的买卖信号一致
    for cut in (30, 120, 239):
        prefix = compute_zuot_series(rows[: cut + 1], DAY).states[-1]
        full = result.states[cut]
        assert prefix["buy_signal"] == full["buy_signal"]
        assert prefix["sell_signal"] == full["sell_signal"]
        assert abs(prefix["vwap"] - full["vwap"]) < 1e-6


def test_zuot_snapshot_uptrend_full_score():
    # 榜单紧凑快照：单边上行 + 外盘>内盘×1.5 + 现价在支撑上方 → 满分强推
    pcts = [i * 0.05 for i in range(60)]
    snap = compute_zuot_snapshot(
        pcts,
        vwap_pct=1.0,
        price=10.5,
        resistance=10.8,
        support=10.1,
        outer_volume=15000.0,
        inner_volume=9000.0,
        flow_available=True,
    )
    assert snap["available"] is True
    assert snap["trend_text"] == "多头强势"
    assert snap["trend_bull"] is True
    assert snap["fund_pct"] == 25.0
    assert snap["fund_attitude"] == "积极做多"
    assert snap["position_text"] == "强势区"
    assert snap["vwap_relation"] == "↑均价"
    assert snap["score"] == 100
    assert snap["advice"].startswith("★强推")
    assert snap["advice_label"] == "强推"


def test_zuot_snapshot_downtrend_and_flow_unavailable():
    # 单边下行 + 内盘主导 + 现价在支撑上方（20 分兜底）→ 减仓
    pcts = [-i * 0.04 for i in range(60)]
    snap = compute_zuot_snapshot(
        pcts,
        vwap_pct=-1.0,
        price=9.5,
        resistance=9.9,
        support=9.2,
        outer_volume=5000.0,
        inner_volume=12000.0,
        flow_available=True,
    )
    assert snap["trend_text"] == "空头弱势"
    assert snap["fund_attitude"] == "积极做空"
    assert snap["score"] == 20
    assert snap["advice_label"] == "减仓"
    # 盘口不可用：资金维度不计分、不显示
    snap2 = compute_zuot_snapshot(
        pcts, price=9.5, resistance=9.9, support=9.2, flow_available=False
    )
    assert snap2["fund_available"] is False
    assert snap2["fund_text"] == "--"
    assert snap2["score"] == 20


def test_zuot_snapshot_requires_two_points():
    assert compute_zuot_snapshot([], price=10.0)["available"] is False
    assert compute_zuot_snapshot([1.0], price=10.0)["available"] is False
