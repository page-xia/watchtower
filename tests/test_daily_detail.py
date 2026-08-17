from __future__ import annotations

"""日线公式引擎 / 筹码分布 / 题材标签 的单元测试。"""

import math

from app.chip_distribution import compute_chip_distribution, compute_intraday_distribution
from app.daily_formula_engine import (
    DailyFormulaInput,
    compute_main_chart,
    compute_sub_resonance,
    compute_sub_trend,
)
from app.stock_tags import classify_belong_boards


def _trend_rows(n: int, start: float = 10.0, step: float = 0.05, vol: float = 5_000_000) -> list[dict]:
    rows = []
    price = start
    for i in range(n):
        date = f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}"
        close = price + step
        rows.append(
            {
                "date": date,
                "open": price,
                "high": close * 1.01,
                "low": price * 0.99,
                "close": close,
                "vol": vol,
                "amount": close * vol,
            }
        )
        price = close
    return rows


def _flat_rows(n: int, base: float = 10.0) -> list[dict]:
    rows = []
    for i in range(n):
        date = f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}"
        wave = 0.02 if i % 2 == 0 else -0.02
        close = base + wave
        rows.append(
            {
                "date": date,
                "open": base,
                "high": max(base, close) * 1.005,
                "low": min(base, close) * 0.995,
                "close": close,
                "vol": 3_000_000,
                "amount": close * 3_000_000,
            }
        )
    return rows


# --------------------------------------------------------------------- 主图


def test_main_chart_uptrend_hold_and_score():
    rows = _trend_rows(120, step=0.06)
    data = DailyFormulaInput.from_rows(rows, float_shares=2e8, winner_pct=0.8)
    main = compute_main_chart(data)
    assert main["available"]
    assert len(main["swl"]) == 120
    assert main["candle_state"][-1] in {"hold", "normal"}
    # 单边上涨末根应为持股态
    assert main["candle_state"][-1] == "hold"
    assert 0 <= main["score_h"] <= 80
    # 强支撑 = 近20日最高 * 0.809（服务端保留 3 位小数）
    hi20 = max(r["high"] for r in rows[-20:])
    assert math.isclose(main["strong_support"], hi20 * 0.809, abs_tol=1e-3)
    assert main["tomorrow"]["support"] < main["tomorrow"]["resistance"]
    assert main["tips"]["stock"] is not None
    assert "text" in main["tips"]["stock"]


def test_main_chart_limit_up_and_lianban():
    rows = _flat_rows(60)
    # 第 50/51 根二连板（>9.5% 且收在最高）
    for idx in (50, 51):
        prev_close = rows[idx - 1]["close"]
        rows[idx]["close"] = round(prev_close * 1.100, 4)
        rows[idx]["open"] = prev_close
        rows[idx]["high"] = rows[idx]["close"]
        rows[idx]["low"] = prev_close * 0.999
        if idx == 51:
            rows[idx]["close"] = round(rows[50]["close"] * 1.100, 4)
            rows[idx]["high"] = rows[idx]["close"]
            rows[idx]["low"] = rows[50]["close"] * 0.999
    main = compute_main_chart(DailyFormulaInput.from_rows(rows, float_shares=2e8))
    assert 50 in main["markers"]["limit_up"]
    lianban = {item["index"]: item["count"] for item in main["markers"]["lianban"]}
    assert lianban.get(51) == 2


def test_main_chart_broken_board():
    rows = _flat_rows(60)
    prev_close = rows[40]["close"]
    rows[41]["high"] = round(prev_close * 1.098, 4)
    rows[41]["close"] = round(prev_close * 1.02, 4)
    rows[41]["low"] = prev_close * 0.99
    main = compute_main_chart(DailyFormulaInput.from_rows(rows))
    assert 41 in main["markers"]["broken"]


def test_main_chart_without_float_shares_degrades():
    rows = _trend_rows(80)
    main = compute_main_chart(DailyFormulaInput.from_rows(rows))
    assert main["available"]
    assert main["quality"]["float_shares"] is False
    assert all(v is None for v in main["cost_line"])


# ------------------------------------------------------------------- 副图1


def test_sub_resonance_series_lengths():
    rows = _trend_rows(150)
    sub = compute_sub_resonance(DailyFormulaInput.from_rows(rows))
    assert sub["available"]
    for key in ("z7", "z8", "www", "tdxlfxj", "kongqi"):
        assert len(sub[key]) == 150
    assert all(-100.0001 <= v <= 100.0001 for v in sub["z7"] if v is not None)
    assert all(v >= 0 for v in sub["www"] if v is not None)
    for strip in ("strip_weak", "strip_mid", "strip_top"):
        assert set(sub[strip]) <= {0, 1}
        # 弱/中互斥
    assert all(not (a and b) for a, b in zip(sub["strip_weak"], sub["strip_mid"]))


# ------------------------------------------------------------------- 副图2


def test_sub_trend_series_and_marks():
    rows = _trend_rows(200, step=0.03)
    sub = compute_sub_trend(DailyFormulaInput.from_rows(rows))
    assert sub["available"]
    for key in ("trend_line", "main_accum", "rich_accum", "chongding"):
        assert len(sub[key]) == 200
    assert all(0 <= v <= 100.001 for v in sub["main_accum"] if v is not None)
    for key in ("niu", "shao", "yellow_pin", "pink_pin", "jigou_chu", "zhuli_chu", "red_hat", "red_triangle"):
        assert isinstance(sub["markers"][key], list)


# --------------------------------------------------------------------- 筹码


def test_chip_distribution_mass_and_winner():
    rows = _trend_rows(180, step=0.04)
    chip = compute_chip_distribution(rows, float_shares=2e8)
    assert chip["available"]
    total = sum(b["weight"] for b in chip["bins"])
    assert math.isclose(total, 1.0, rel_tol=1e-3)
    assert 0.9 <= chip["winner_pct"] <= 1.0  # 单边上涨末端几乎全部获利
    assert chip["cost90"][0] < chip["cost90"][1]
    assert chip["cost70"][0] >= chip["cost90"][0]
    assert chip["avg_cost"] < chip["current_price"]
    assert chip["quality"] == "ok"


def test_chip_distribution_estimated_mode():
    rows = _flat_rows(100)
    chip = compute_chip_distribution(rows)
    assert chip["available"]
    assert chip["quality"] == "estimated_turnover"


def test_intraday_distribution():
    rows = [
        {"time": f"09:{30 + i:02d}", "close": 10 + i * 0.01, "vol": 1000 + i * 10}
        for i in range(30)
    ]
    chip = compute_intraday_distribution(rows, prev_close=9.9)
    assert chip["available"]
    assert chip["vwap"] > 9.9
    total = sum(b["vol"] for b in chip["bins"])
    assert math.isclose(total, sum(r["vol"] for r in rows), rel_tol=1e-6)
    assert chip["peak_price"] is not None


# --------------------------------------------------------------------- 标签


def test_classify_belong_boards():
    rows = [
        {"board_type": 12, "board_name": "酿酒"},
        {"board_type": 12, "board_name": "白酒"},
        {"board_type": 4, "board_name": "白酒概念"},
        {"board_type": 4, "board_name": "乡村振兴"},
        {"board_type": 3, "board_name": "贵州板块"},
        {"board_type": 5, "board_name": "绩优股"},
        {"board_type": 5, "board_name": "基金重仓"},
    ]
    result = classify_belong_boards(rows, "600519")
    assert result["industry"] == "白酒"
    assert result["concepts"] == ["白酒概念", "乡村振兴"]
    assert result["regions"] == ["贵州板块"]
    assert result["styles"] == ["绩优股", "基金重仓"]
