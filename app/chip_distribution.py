from __future__ import annotations

"""筹码分布（筹码峰）计算引擎。

通达信式近似模型：
- 每个交易日的成交量按"三角分布"摊到 [最低, 最高] 价格区间（峰值在
  重心价 (H+L+2C)/4）；
- 每过一天，存量筹码按当日换手率整体衰减：mass *= (1 - turnover)，
  新成交的 turnover 部分进入当日价格区间；
- 最终分布归一化为 100%，得到 价格→筹码占比 的直方图。

换手率口径：
- 优先用真实流通股本（tushare daily_basic 快照 / easy_tdx F10）：
  turnover = vol(股) / float_shares(股)；
- 流通股本缺失时用估算模式：假设窗口内最大成交量对应 20% 换手
  （synth_float = max_vol * 5），quality 标注 estimated_turnover。

分时模式（当日量价分布）：分钟行按收盘价分桶累计成交量，叠加 VWAP。
"""

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


@dataclass(frozen=True)
class ChipBin:
    price: float
    weight: float  # 0..1 占总筹码比例


def _triangular_weights(
    centers: Sequence[float],
    low: float,
    high: float,
    peak: float,
) -> list[tuple[int, float]]:
    """返回落在 [low, high] 内 bin 的 (下标, 三角权重)。"""
    if high <= low:
        return []
    peak = min(max(peak, low), high)
    left_span = max(peak - low, 1e-9)
    right_span = max(high - peak, 1e-9)
    weights: list[tuple[int, float]] = []
    for index, center in enumerate(centers):
        if center < low or center > high:
            continue
        if center <= peak:
            w = (center - low) / left_span
        else:
            w = (high - center) / right_span
        # 端点给半高，避免单峰日两端权重为 0 显得断裂
        w = 0.05 + 0.95 * max(w, 0.0)
        weights.append((index, w))
    return weights


def compute_chip_distribution(
    rows: Sequence[Mapping[str, Any]],
    *,
    float_shares: float | None = None,
    bin_count: int = 90,
) -> dict[str, Any]:
    """历史筹码分布。rows 为升序日K（date/open/high/low/close/vol）。"""
    bars = [
        {
            "date": str(row.get("date") or ""),
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "close": _num(row.get("close")),
            "vol": max(_num(row.get("vol") or row.get("volume")), 0.0),
        }
        for row in rows
        if _num(row.get("close")) > 0
    ]
    if len(bars) < 20:
        return {"available": False, "note": "日K数据不足（<20根），无法构建筹码分布"}

    price_low = min(b["low"] for b in bars if b["low"] > 0)
    price_high = max(b["high"] for b in bars)
    if price_high <= price_low:
        return {"available": False, "note": "日K价格区间异常"}

    span_pad = (price_high - price_low) * 0.02
    price_low -= span_pad
    price_high += span_pad
    bin_count = max(30, min(int(bin_count), 160))
    width = (price_high - price_low) / bin_count
    centers = [price_low + (i + 0.5) * width for i in range(bin_count)]
    mass = [0.0] * bin_count

    estimated_turnover = not (float_shares and float_shares > 0)
    synth_float = None
    if estimated_turnover:
        max_vol = max(b["vol"] for b in bars)
        synth_float = max_vol * 5.0 if max_vol > 0 else None  # 假设峰值日换手 20%

    for bar in bars:
        if estimated_turnover:
            turnover = min(bar["vol"] / synth_float, 0.30) if synth_float else 0.0
        else:
            turnover = min(max(bar["vol"] / float_shares, 0.0), 1.0)
        if turnover <= 0:
            continue
        decay = 1.0 - turnover
        mass = [m * decay for m in mass]
        typical = (bar["high"] + bar["low"] + 2 * bar["close"]) / 4
        weights = _triangular_weights(centers, bar["low"], bar["high"], typical)
        total_w = sum(w for _, w in weights)
        if total_w <= 0:
            continue
        for index, w in weights:
            mass[index] += turnover * (w / total_w)

    total = sum(mass)
    if total <= 0:
        return {"available": False, "note": "筹码质量累积失败"}
    mass = [m / total for m in mass]

    close = bars[-1]["close"]
    winner_pct = sum(m for m, center in zip(mass, centers) if center <= close)
    avg_cost = sum(m * center for m, center in zip(mass, centers))

    def percentile(p: float) -> float:
        cumulative = 0.0
        for center, m in zip(centers, mass):
            cumulative += m
            if cumulative >= p:
                return center
        return centers[-1]

    p5, p95 = percentile(0.05), percentile(0.95)
    p15, p85 = percentile(0.15), percentile(0.85)
    concentration90 = (p95 - p5) / (p95 + p5) if (p95 + p5) > 0 else 0.0
    concentration70 = (p85 - p15) / (p85 + p15) if (p85 + p15) > 0 else 0.0

    # 峰检测：3 点平滑后找局部极大，取前 3 大
    smoothed = [
        (mass[max(0, i - 1)] + mass[i] + mass[min(bin_count - 1, i + 1)]) / 3
        for i in range(bin_count)
    ]
    peak_candidates: list[tuple[float, float]] = []
    for i in range(1, bin_count - 1):
        if smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1] and mass[i] > 0:
            peak_candidates.append((mass[i], centers[i]))
    peak_candidates.sort(reverse=True)
    peaks = [
        {"price": round(price, 3), "share": round(share, 4)}
        for share, price in peak_candidates[:3]
    ]

    return {
        "available": True,
        "price_low": round(price_low, 3),
        "price_high": round(price_high, 3),
        "bin_width": round(width, 4),
        "current_price": round(close, 3),
        "as_of": bars[-1]["date"],
        "bars_used": len(bars),
        "bins": [
            {"price": round(center, 3), "weight": round(m, 5)}
            for center, m in zip(centers, mass)
            if m > 1e-5
        ],
        "winner_pct": round(winner_pct, 4),
        "avg_cost": round(avg_cost, 3),
        "cost90": [round(p5, 3), round(p95, 3)],
        "cost70": [round(p15, 3), round(p85, 3)],
        "concentration90": round(concentration90, 4),
        "concentration70": round(concentration70, 4),
        "peaks": peaks,
        "quality": "estimated_turnover" if estimated_turnover else "ok",
    }


def compute_intraday_distribution(
    rows: Sequence[Mapping[str, Any]],
    *,
    prev_close: float = 0.0,
    bin_count: int = 60,
) -> dict[str, Any]:
    """分时模式：当日分钟量价分布（volume-at-price）。"""
    minutes = [
        {
            "time": str(row.get("time") or "")[:5],
            "price": _num(row.get("close") or row.get("price")),
            "vol": max(_num(row.get("vol") or row.get("volume")), 0.0),
            "amount": max(_num(row.get("amount")), 0.0),
        }
        for row in rows
        if _num(row.get("close") or row.get("price")) > 0
    ]
    if not minutes:
        return {"available": False, "note": "当日分钟数据为空"}

    prices = [m["price"] for m in minutes]
    price_low, price_high = min(prices), max(prices)
    if price_high <= price_low:
        price_high = price_low * 1.001 + 0.001
    pad = (price_high - price_low) * 0.05
    price_low -= pad
    price_high += pad
    bin_count = max(20, min(int(bin_count), 120))
    width = (price_high - price_low) / bin_count
    vols = [0.0] * bin_count
    total_vol = 0.0
    total_amount = 0.0
    for m in minutes:
        total_vol += m["vol"]
        total_amount += m["amount"] if m["amount"] > 0 else m["vol"] * m["price"] * 100
        index = int((m["price"] - price_low) / width)
        index = max(0, min(bin_count - 1, index))
        vols[index] += m["vol"]

    vwap = total_amount / (total_vol * 100) if total_vol > 0 else minutes[-1]["price"]
    centers = [price_low + (i + 0.5) * width for i in range(bin_count)]
    max_vol_bin = max(range(bin_count), key=lambda i: vols[i]) if any(v > 0 for v in vols) else None

    return {
        "available": True,
        "price_low": round(price_low, 3),
        "price_high": round(price_high, 3),
        "bin_width": round(width, 4),
        "current_price": round(prices[-1], 3),
        "prev_close": round(prev_close, 3) if prev_close > 0 else None,
        "vwap": round(vwap, 3),
        "total_vol": round(total_vol, 1),
        "bins": [
            {"price": round(center, 3), "vol": round(vols[i], 1)}
            for i, center in enumerate(centers)
            if vols[i] > 0
        ],
        "peak_price": round(centers[max_vol_bin], 3) if max_vol_bin is not None else None,
        "as_of": minutes[-1]["time"],
    }


__all__ = ["compute_chip_distribution", "compute_intraday_distribution"]
