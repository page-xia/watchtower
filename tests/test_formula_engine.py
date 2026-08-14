from __future__ import annotations

import pytest

from app.formula_engine import (
    CROSS,
    DEFAULT_TREND_NEAR_THRESHOLD_PCT,
    EMA,
    FILTER,
    HHV,
    LLV,
    MA,
    REF,
    SAR,
    SMA,
    FormulaSeriesResult,
    TrendLineSeriesResult,
    compute_formula_series,
    compute_formula_state,
    compute_trend_line_series,
    evaluate_gold_resonance,
    summarize_l1_transactions,
    trend_line_proximity,
)


def _bars(prices: list[float]) -> list[dict[str, float | str]]:
    return [
        {
            "time": f"09:{31 + index:02d}",
            "open": price * 0.998,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "price": price,
        }
        for index, price in enumerate(prices)
    ]


def _tight_bars(prices: list[float]) -> list[dict[str, float | str]]:
    return [
        {
            "time": f"09:{31 + index:02d}",
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price,
            "price": price,
        }
        for index, price in enumerate(prices)
    ]


def test_tdx_moving_window_functions_use_visible_prefix() -> None:
    values = [1.0, 2.0, 3.0, 2.0, 5.0]

    assert MA(values, 3) == pytest.approx([1.0, 1.5, 2.0, 7 / 3, 10 / 3])
    assert EMA([1.0, 2.0, 3.0], 3) == pytest.approx([1.0, 1.5, 2.25])
    assert SMA([1.0, 2.0, 3.0], 3, 1) == pytest.approx([1.0, 4 / 3, 17 / 9])
    assert HHV(values, 3) == [1.0, 2.0, 3.0, 3.0, 5.0]
    assert LLV(values, 3) == [1.0, 1.0, 1.0, 2.0, 2.0]
    assert REF(values, 2) == [None, None, 1.0, 2.0, 3.0]


def test_cross_filter_and_sar_are_point_in_time() -> None:
    assert CROSS([1, 2, 1, 4], [1.5, 1.5, 1.5, 1.5]) == [False, True, False, True]
    assert FILTER([True, True, False, True, True, False], 2) == [True, False, False, True, False, False]

    highs = [10, 10.2, 10.4, 10.3, 10.1, 9.9, 10.0]
    lows = [9.8, 9.9, 10.0, 9.9, 9.7, 9.5, 9.6]
    closes = [9.9, 10.1, 10.3, 10.0, 9.8, 9.6, 9.9]
    short = SAR(highs, lows, closes, 10, 2, 20)
    extended = SAR(highs + [99, 1], lows + [98, 0.8], closes + [98.5, 1.0], 10, 2, 20)

    assert len(short) == len(highs)
    assert all(value is not None for value in short)
    assert short == pytest.approx(extended[: len(short)])


def test_formula_series_exports_chinese_and_snake_case_state_keys() -> None:
    prices = [
        10.0,
        9.94,
        9.88,
        9.82,
        9.76,
        9.72,
        9.70,
        9.84,
        10.02,
        10.18,
        10.25,
    ]
    rows = _tight_bars(prices)
    trend = compute_trend_line_series(rows, trend_near_threshold_pct=1.0)
    result = compute_formula_series(rows, trend_near_threshold_pct=1.0, trend_states=trend.states)
    latest = result.latest

    assert isinstance(result, FormulaSeriesResult)
    assert isinstance(trend, TrendLineSeriesResult)
    assert len(result.states) == len(rows)
    assert latest["多方力度"] == latest["duo_strength"]
    assert latest["空方力度"] == latest["kong_strength"]
    assert latest["白线"] == latest["white_line"]
    assert latest["黄线"] == latest["yellow_line"]
    assert latest["赶快出手原始值"] == 0
    assert "CROSS(多方力度,6.78)" in latest["赶快出手说明"]
    assert latest["validation_status"] == "research_only"
    assert latest["near_trend_threshold_pct"] == 1.0
    assert latest["source_quality"] == "tdx_formula_minute+daily_trend"

    expected_white = EMA(EMA(prices, 10), 10)[-1]
    ma14, ma28, ma57, ma114 = (MA(prices, period)[-1] for period in (14, 28, 57, 114))
    expected_yellow = (ma14 + ma28 + ma57 + ma114) / 4
    assert latest["white_line"] == pytest.approx(expected_white)
    assert latest["yellow_line"] == pytest.approx(expected_yellow)
    assert result.states[2]["protection_price"] == pytest.approx((prices[0] + prices[1] + rows[2]["open"]) / 3)


def test_formula_state_and_sar_do_not_change_when_future_bars_are_appended() -> None:
    base = _bars([10.0, 9.94, 9.88, 9.82, 9.76, 9.72, 9.70, 9.84, 10.02])
    future = _bars([88.0, 1.0, 99.0])

    short = compute_formula_series(base).states
    extended = compute_formula_series(base + future).states

    assert short == extended[: len(short)]


def test_quick_entry_uses_cross_of_bullish_power_not_original_zero_field() -> None:
    prices = [10.0, 9.5, 9.1, 8.8, 8.6, 8.5, 8.7, 9.3, 10.0, 10.8, 11.2]
    result = compute_formula_series(_bars(prices))

    quick_states = [state for state in result.states if state["quick_entry"]]

    assert quick_states
    assert all(state["赶快出手原始值"] == 0 for state in quick_states)
    assert quick_states[0]["多方力度"] > 6.78


def test_today_protection_price_uses_two_prior_closes_and_current_open() -> None:
    rows = _bars([10.0, 10.2, 10.4, 10.6])
    result = compute_formula_series(rows)

    assert result.states[0]["protection_price"] is None
    assert result.states[1]["protection_price"] is None
    assert result.states[2]["今日保护价"] == pytest.approx((10.0 + 10.2 + rows[2]["open"]) / 3)
    assert result.states[3]["protection_price"] == pytest.approx((10.2 + 10.4 + rows[3]["open"]) / 3)


def test_gold_resonance_requires_buy_candidate_near_trend_and_vetoes_l1_pressure() -> None:
    formula_hit = {
        "赶快出手": True,
        "主力吸筹": 1.2,
        "near_trend_line": False,
    }
    near_formula_hit = {
        **formula_hit,
        "near_trend_line": True,
        "near_trend_line_name": "白线",
    }

    rejected, rejected_reasons = evaluate_gold_resonance(formula_hit, buy_candidate=False)
    far_gold, far_reasons = evaluate_gold_resonance(formula_hit, buy_candidate=True, l1_buy_support=True)
    gold, reasons = evaluate_gold_resonance(near_formula_hit, buy_candidate=True, l1_buy_support=True)
    vetoed, veto_reasons = evaluate_gold_resonance(near_formula_hit, buy_candidate=True, l1_sell_pressure=True)

    assert rejected is False
    assert rejected_reasons == ["非买T候选"]
    assert far_gold is False
    assert far_reasons == []
    assert gold is True
    assert "赶快出手+主力吸筹" in reasons
    assert "接近白线" in reasons
    assert "L1逐笔买盘支持" in reasons
    assert vetoed is False
    assert "L1明显抛压否决" in veto_reasons


def test_gold_resonance_can_use_near_white_or_yellow_trend_line() -> None:
    state = {
        "buy_candidate": True,
        "quick_entry": False,
        "main_accumulation": 0.0,
        "near_trend_line": True,
        "near_trend_line_name": "黄线",
    }

    gold, reasons = evaluate_gold_resonance(state, buy_candidate=True)

    assert gold is True
    assert "接近黄线" in reasons


def test_near_trend_line_alone_does_not_create_formula_buy_or_gold() -> None:
    rows = _bars([10.0] * 10)
    trend = compute_trend_line_series(rows, trend_near_threshold_pct=1.0)
    result = compute_formula_series(rows, trend_near_threshold_pct=1.0, trend_states=trend.states)

    assert all(state["near_trend_line"] for state in result.states)
    assert not any(state["buy_candidate"] for state in result.states)
    assert not any(state["gold_resonance"] for state in result.states)


def test_minute_formula_without_external_trend_does_not_invent_near_line() -> None:
    result = compute_formula_series(_bars([10.0] * 10), trend_near_threshold_pct=1.0)

    assert all(state["white_line"] == 0 for state in result.states)
    assert all(state["yellow_line"] == 0 for state in result.states)
    assert not any(state["near_trend_line"] for state in result.states)
    assert all(state["trend_source_quality"] == "trend_unavailable" for state in result.states)


def test_default_near_trend_line_threshold_is_three_percent() -> None:
    rows = _bars([10.0] * 120 + [10.3])
    trend = compute_trend_line_series(rows)
    strict_trend = compute_trend_line_series(rows, trend_near_threshold_pct=2.8)
    result = compute_formula_series(rows, trend_states=trend.states)
    strict = compute_formula_series(rows, trend_near_threshold_pct=2.8, trend_states=strict_trend.states)

    assert result.latest["near_trend_threshold_pct"] == DEFAULT_TREND_NEAR_THRESHOLD_PCT
    assert result.latest["trend_distance_pct"] == pytest.approx(2.815443, abs=0.0001)
    assert result.latest["near_trend_line"] is True
    assert strict.latest["near_trend_line"] is False


def test_trend_line_proximity_uses_three_percent_of_current_price_boundary() -> None:
    near_white = trend_line_proximity(100.0, 97.0, 103.2)
    near_yellow = trend_line_proximity(100.0, 96.8, 102.99)
    outside = trend_line_proximity(100.0, 96.99, 103.01)

    assert near_white["near_trend_threshold_pct"] == DEFAULT_TREND_NEAR_THRESHOLD_PCT
    assert near_white["white_distance_pct"] == pytest.approx(3.0)
    assert near_white["yellow_distance_pct"] == pytest.approx(3.2)
    assert near_white["trend_distance_pct"] == pytest.approx(3.0)
    assert near_white["near_trend_line"] is True
    assert near_white["near_trend_line_name"] == "白线"
    assert near_yellow["near_trend_line"] is True
    assert near_yellow["near_trend_line_name"] == "黄线"
    assert outside["trend_distance_pct"] == pytest.approx(3.01)
    assert outside["near_trend_line"] is False


def test_trend_line_proximity_rejects_far_600206_example() -> None:
    result = trend_line_proximity(52.63, 42.59, 41.06)

    assert result["white_distance_pct"] == pytest.approx(19.0766, abs=0.0001)
    assert result["yellow_distance_pct"] == pytest.approx(21.9837, abs=0.0001)
    assert result["near_trend_line"] is False
    assert result["trend_distance_pct"] > DEFAULT_TREND_NEAR_THRESHOLD_PCT


def test_sell_candidate_uses_drawicon_rsi_cross_primitive() -> None:
    result = compute_formula_series(_bars([10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 12.0]))
    sell_states = [state for state in result.states if state["sell_candidate"]]

    assert sell_states
    assert sell_states[0]["sell_trigger"] is True
    assert sell_states[0]["sell_signal_reasons"] == ["DRAWICON(CROSS(88.8,RSI),90,15)"]


def test_l1_transaction_summary_treats_special_buyorsell_as_neutral() -> None:
    rows = [
        {"time": "09:31:01", "price": 10.0, "vol": 100, "buyorsell": 0},
        {"time": "09:31:02", "price": 10.1, "vol": 60, "buyorsell": 1},
        {"time": "09:31:03", "price": 10.2, "vol": 50, "buyorsell": 7},
        {"time": "09:31:04", "price": 10.3, "vol": 40},
        {"time": "09:31:05", "price": 10.2, "vol": 30},
    ]

    summary = summarize_l1_transactions(rows)

    assert summary.count == 5
    assert summary.buy_amount == pytest.approx(100_000 + 41_200)
    assert summary.sell_amount == pytest.approx(60_600 + 30_600)
    assert summary.neutral_amount == pytest.approx(51_000)
    assert summary.available is True


def test_compute_formula_state_can_attach_l1_gold_fields_without_changing_series_api() -> None:
    prices = [10.0 - 0.01 * index for index in range(20)]
    prices.extend([prices[-1] + 0.01 * step for step in range(1, 3)])
    rows = _tight_bars(prices)
    trend_states = compute_trend_line_series(rows).states
    state = compute_formula_state(
        rows,
        l1_flow={"available": True, "rolling_score": 32, "rolling_imbalance_pct": 24, "rolling_count": 20},
        trend_states=trend_states,
    ).to_dict()

    assert state["l1_buy_support"] is True
    assert state["l1_sell_pressure"] is False
    assert state["quick_entry"] is True
    assert state["gold_resonance"] is True
    assert "L1逐笔买盘支持" in state["resonance_reasons"]

    pressured = compute_formula_state(
        rows,
        l1_flow={"available": True, "rolling_score": -35, "rolling_imbalance_pct": -32, "rolling_count": 20},
        trend_states=trend_states,
    ).to_dict()
    assert pressured["l1_sell_pressure"] is True
    assert pressured["quick_entry"] is True
    assert pressured["gold_resonance"] is False
    assert "L1明显抛压否决" in pressured["resonance_reasons"]
