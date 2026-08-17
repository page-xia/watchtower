from __future__ import annotations

"""做T分时公式（做T公式.md）的 point-in-time 实现。

公式语义对齐通达信分时图：

- 均线系统：MA30=EMA(C,30)，强弱=EMA(C,900)，全部分钟级 O(n) 递推。
- 阻力/支撑/中轴：H1=MAX(昨收,最高)，L1=MIN(昨收,最低)，P1=H1-L1，
  阻力=L1+P1*7/8，支撑=L1+P1*0.5/8，中=(支撑+阻力)/2，当日为常量。
- 均价：SUM(V*C,0)/SUM(V,0)（累计 VWAP）。
- 买卖信号：买=LONGCROSS(支撑,现价,2)（回踩支撑），卖=LONGCROSS(现价,阻力,2)（冲高兑现）。
- 机构资金：分钟成交额(万)=V*C/100，成交额/8>20（即分钟成交额>160万）为大单分钟，
  按涨跌方向累计 A2（买）/A3（卖），资金流向=A2-A3。
- 综合评分：MA30>强弱 30 分 + 现价>均价 20 分 + A2>A3 30 分 + 现价>支撑 20 分。

第 i 个状态只用前 i 根分钟线，盘中实时与盘后回放口径一致。
"""

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Sequence


EPSILON = 1e-12

# 做T公式.md：成交额:=V*C/100（万元），大单分钟条件 成交额/8>20（万元）
# 即单分钟成交额 > 160 万元（=1_600_000 元）。
BIG_MINUTE_AMOUNT_YUAN = 1_600_000.0

SIGNAL_VERSION = "zuot_tdx_levels_v1"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _value(row: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return _number(row.get(key), default)
    return default


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if abs(denominator) > EPSILON else default


def ema(values: Sequence[float], period: int) -> list[float]:
    window = max(1, int(period))
    alpha = 2.0 / (window + 1.0)
    result: list[float] = []
    previous: float | None = None
    for value in values:
        item = _number(value)
        previous = item if previous is None else alpha * item + (1.0 - alpha) * previous
        result.append(previous)
    return result


def cross(left: Sequence[float] | float, right: Sequence[float] | float) -> list[bool]:
    """TDX CROSS(A,B)：A 上穿 B（上一根 A<=B，本根 A>B）。常量可传标量。"""
    length = max(
        len(left) if not isinstance(left, (int, float)) else 0,
        len(right) if not isinstance(right, (int, float)) else 0,
    )
    left_values = _series(left, length)
    right_values = _series(right, length)
    result: list[bool] = []
    for index in range(length):
        if index == 0:
            result.append(False)
            continue
        result.append(
            left_values[index - 1] <= right_values[index - 1]
            and left_values[index] > right_values[index]
        )
    return result


def longcross(left: Sequence[float] | float, right: Sequence[float] | float, period: int) -> list[bool]:
    """TDX LONGCROSS(A,B,N)：A 在此前 N 个周期内一直低于 B，本根上穿 B。"""
    length = max(
        len(left) if not isinstance(left, (int, float)) else 0,
        len(right) if not isinstance(right, (int, float)) else 0,
    )
    left_values = _series(left, length)
    right_values = _series(right, length)
    window = max(1, int(period))
    result: list[bool] = []
    for index in range(length):
        if index == 0:
            result.append(False)
            continue
        crossed = (
            left_values[index - 1] <= right_values[index - 1]
            and left_values[index] > right_values[index]
        )
        if not crossed:
            result.append(False)
            continue
        start = max(0, index - window)
        result.append(
            all(left_values[j] < right_values[j] for j in range(start, index))
        )
    return result


def _series(value: Sequence[float] | float, length: int) -> list[float]:
    if isinstance(value, (int, float)):
        return [float(value)] * length
    result = [_number(item) for item in value]
    if len(result) < length:
        result.extend([0.0] * (length - len(result)))
    return result[:length]


@dataclass(frozen=True)
class ZuoTDayContext:
    """做T公式的日级常量输入（通达信 DYNAINFO 等价物）。

    prev_close=DYNAINFO(3) 昨收，day_high=DYNAINFO(5) 最高，day_low=DYNAINFO(6) 最低，
    change_pct=DYNAINFO(14)*100 涨幅，volume_ratio=DYNAINFO(17) 量比，
    turnover_rate=DYNAINFO(37) 换手率，total_amount=DYNAINFO(10) 总额(元)，
    outer_volume=DYNAINFO(26) 外盘(手)，inner_volume=DYNAINFO(25) 内盘(手)。
    """

    prev_close: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    change_pct: float = 0.0
    volume_ratio: float = 1.0
    turnover_rate: float | None = None
    total_amount: float = 0.0
    outer_volume: float = 0.0
    inner_volume: float = 0.0

    @property
    def levels_available(self) -> bool:
        return self.prev_close > 0 and self.day_high > 0 and self.day_low > 0

    @property
    def h1(self) -> float:
        return max(self.prev_close, self.day_high)

    @property
    def l1(self) -> float:
        return min(self.prev_close, self.day_low)

    @property
    def resistance(self) -> float:
        """阻力=L1+P1*7/8"""
        if not self.levels_available:
            return 0.0
        p1 = self.h1 - self.l1
        return self.l1 + p1 * 7.0 / 8.0

    @property
    def support(self) -> float:
        """支撑=L1+P1*0.5/8"""
        if not self.levels_available:
            return 0.0
        p1 = self.h1 - self.l1
        return self.l1 + p1 * 0.5 / 8.0

    @property
    def mid(self) -> float:
        """中轴=(支撑+阻力)/2"""
        if not self.levels_available:
            return 0.0
        return (self.support + self.resistance) / 2.0


@dataclass(frozen=True)
class ZuoTSeriesResult:
    states: list[dict[str, Any]]
    latest: dict[str, Any]
    day: ZuoTDayContext = field(default=ZuoTDayContext)


def _trend_text(ma30_now: float, qs_now: float, ma30_prev3: float) -> str:
    direction = ma30_now - qs_now
    if direction > 0 and ma30_now > ma30_prev3:
        return "多头强势"
    if direction > 0:
        return "多头震荡"
    if direction < 0 and ma30_now < ma30_prev3:
        return "空头弱势"
    return "空头震荡"


def _volume_text(volume_ratio: float) -> str:
    if volume_ratio > 1.5:
        return "放量"
    if volume_ratio > 0.8:
        return "平量"
    return "缩量"


def _fund_attitude(big_buy: float, big_sell: float) -> str:
    if big_buy > big_sell * 1.5:
        return "积极做多"
    if big_buy > big_sell:
        return "偏多"
    if big_sell > big_buy * 1.5:
        return "积极做空"
    return "偏空"


def _position_text(price: float, resistance: float, mid: float, support: float) -> str:
    if resistance <= 0 or support <= 0:
        return "位置未知"
    if price > resistance:
        return "突破阻力"
    if price > mid:
        return "强势区"
    if price > support:
        return "弱势区"
    return "跌破支撑"


def _advice(score: float) -> tuple[str, str]:
    if score >= 70:
        return f"★强推({score:.0f}) 积极做多", "趋势量能资金偏多,支撑低吸阻力高抛"
    if score >= 50:
        return f"☆关注({score:.0f}) 逢低吸纳", "趋势向上但存分歧,回踩支撑轻仓试多"
    if score >= 30:
        return f"△观望({score:.0f}) 等待信号", "多空不明交投清淡,等待明朗再操作"
    if score >= 15:
        return f"○减仓({score:.0f}) 控制风险", "空头信号增多,反弹减仓等底部企稳"
    return f"X规避({score:.0f}) 离场观望", "空头趋势资金流出,不建议参与等反转"


def compute_zuot_series(
    rows: Sequence[Mapping[str, Any]],
    day: ZuoTDayContext | Mapping[str, Any] | None = None,
) -> ZuoTSeriesResult:
    """对分钟序列 point-in-time 计算做T公式全量状态。"""
    if day is None:
        day_ctx = ZuoTDayContext()
    elif isinstance(day, ZuoTDayContext):
        day_ctx = day
    else:
        day_ctx = ZuoTDayContext(
            prev_close=_number(day.get("prev_close")),
            day_high=_number(day.get("day_high") or day.get("high")),
            day_low=_number(day.get("day_low") or day.get("low")),
            change_pct=_number(day.get("change_pct")),
            volume_ratio=_number(day.get("volume_ratio"), 1.0) or 1.0,
            turnover_rate=(
                None
                if day.get("turnover_rate") in (None, "")
                else _number(day.get("turnover_rate"))
            ),
            total_amount=_number(day.get("total_amount") or day.get("amount")),
            outer_volume=_number(day.get("outer_volume")),
            inner_volume=_number(day.get("inner_volume")),
        )

    normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
    if not normalized:
        return ZuoTSeriesResult(states=[], latest={}, day=day_ctx)

    closes = [_value(row, "close", "price", "last") for row in normalized]
    times = [
        str(row.get("time") or row.get("datetime") or "")[:5]
        for row in normalized
    ]
    volumes = [max(_value(row, "vol", "volume"), 0.0) for row in normalized]
    amounts: list[float] = []
    for index, row in enumerate(normalized):
        amount = max(_value(row, "amount"), 0.0)
        if amount <= 0 and volumes[index] > 0 and closes[index] > 0:
            amount = volumes[index] * closes[index] * 100.0
        amounts.append(amount)

    ma30 = ema(closes, 30)
    qs = ema(closes, 900)
    resistance = day_ctx.resistance
    support = day_ctx.support
    mid = day_ctx.mid

    # 均价：SUM(V*C,0)/SUM(V,0)，V 单位为手，V*C*100 为元。
    vwaps: list[float] = []
    cumulative_amount = 0.0
    cumulative_volume = 0.0
    for index, close in enumerate(closes):
        cumulative_amount += amounts[index]
        cumulative_volume += volumes[index] * 100.0
        vwaps.append(
            _safe_div(cumulative_amount, cumulative_volume, default=close)
        )

    # 机构资金：分钟成交额>160万 记为大单分钟，按方向累计 A2/A3（万元）。
    big_buy: list[float] = []
    big_sell: list[float] = []
    running_buy = 0.0
    running_sell = 0.0
    for index, close in enumerate(closes):
        amount_wan = amounts[index] / 10_000.0
        if amounts[index] > BIG_MINUTE_AMOUNT_YUAN and index > 0:
            if close > closes[index - 1]:
                running_buy += amount_wan
            elif close < closes[index - 1]:
                running_sell += amount_wan
        big_buy.append(running_buy)
        big_sell.append(running_sell)

    buy_cross = cross(support, closes) if support > 0 else [False] * len(closes)
    sell_cross = cross(closes, resistance) if resistance > 0 else [False] * len(closes)
    buy_signal = longcross(support, closes, 2) if support > 0 else [False] * len(closes)
    sell_signal = longcross(closes, resistance, 2) if resistance > 0 else [False] * len(closes)

    states: list[dict[str, Any]] = []
    for index, close in enumerate(closes):
        states.append(
            {
                "time": times[index],
                "close": round(close, 4),
                "ma30": round(ma30[index], 6),
                "qs": round(qs[index], 6),
                "vwap": round(vwaps[index], 4),
                "resistance": round(resistance, 4),
                "support": round(support, 4),
                "mid": round(mid, 4),
                "big_buy_amount": round(big_buy[index], 2),
                "big_sell_amount": round(big_sell[index], 2),
                "fund_flow": round(big_buy[index] - big_sell[index], 2),
                "buy_signal": bool(buy_signal[index]),
                "sell_signal": bool(sell_signal[index]),
                "buy_cross": bool(buy_cross[index]),
                "sell_cross": bool(sell_cross[index]),
                "point_count": index + 1,
            }
        )

    latest = _latest_state(states[-1], ma30, closes, day_ctx, vwaps) if states else {}
    return ZuoTSeriesResult(states=states, latest=latest, day=day_ctx)


def _latest_state(
    last: dict[str, Any],
    ma30: list[float],
    closes: list[float],
    day: ZuoTDayContext,
    vwaps: list[float],
) -> dict[str, Any]:
    index = len(closes) - 1
    ma30_now = ma30[index]
    qs_now = float(last["qs"])
    ma30_prev3 = ma30[index - 3] if index >= 3 else ma30[0]
    price = float(last["close"])
    vwap = float(last["vwap"])
    big_buy = float(last["big_buy_amount"])
    big_sell = float(last["big_sell_amount"])
    big_total = big_buy + big_sell
    fund_flow = big_buy - big_sell

    trend_text = _trend_text(ma30_now, qs_now, ma30_prev3)
    volume_ratio = day.volume_ratio if day.volume_ratio > 0 else 1.0
    volume_text = _volume_text(volume_ratio)
    fund_pct = _safe_div(abs(fund_flow), big_total) * 100.0
    fund_text = (
        f"+{fund_pct:.0f}%" if fund_flow > 0 else f"-{fund_pct:.0f}%" if fund_flow < 0 else "0%"
    )
    fund_attitude = _fund_attitude(big_buy, big_sell)
    position_text = _position_text(price, day.resistance, day.mid, day.support)
    vwap_relation = "↑均价" if price > vwap else "↓均价" if price < vwap else "≈均价"

    score = 0
    score += 30 if ma30_now > qs_now else 0
    score += 20 if price > vwap else 0
    score += 30 if big_buy > big_sell else 0
    score += 20 if day.support > 0 and price > day.support else 0
    advice, advice_detail = _advice(score)

    # 买卖净（通达信按成交额拆分，万元）：总额*外盘/(内盘+外盘)
    total_wan = day.total_amount / 10_000.0
    outer = day.outer_volume
    inner = day.inner_volume
    plate = outer + inner
    buy_wan = total_wan * outer / plate if plate > 0 else 0.0
    sell_wan = total_wan * inner / plate if plate > 0 else 0.0
    net_wan = buy_wan - sell_wan
    split_total = buy_wan + sell_wan
    buy_pct = _safe_div(buy_wan, split_total) * 100.0
    sell_pct = _safe_div(sell_wan, split_total) * 100.0

    turnover_text = (
        f"{day.turnover_rate:.1f}%" if day.turnover_rate is not None else "--"
    )
    line_a = f"个股 {trend_text} | {volume_text} {turnover_text}"
    line_b = f"资金{fund_text} {fund_attitude} | {position_text} {vwap_relation}"

    return {
        **last,
        "trend_text": trend_text,
        "volume_text": volume_text,
        "volume_ratio": round(volume_ratio, 2),
        "turnover_rate": day.turnover_rate,
        "change_pct": round(day.change_pct, 2),
        "fund_text": fund_text,
        "fund_attitude": fund_attitude,
        "position_text": position_text,
        "vwap_relation": vwap_relation,
        "score": score,
        "advice": advice,
        "advice_detail": advice_detail,
        "buy_amount_wan": round(buy_wan, 1),
        "sell_amount_wan": round(sell_wan, 1),
        "net_amount_wan": round(net_wan, 1),
        "buy_pct": round(buy_pct, 1),
        "sell_pct": round(sell_pct, 1),
        "line_a": line_a,
        "line_b": line_b,
        "source_quality": SIGNAL_VERSION,
    }


def compute_zuot_state(
    rows: Sequence[Mapping[str, Any]],
    day: ZuoTDayContext | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """只取最新一根的做T公式状态（含评分/建议/买卖净）。"""
    return dict(compute_zuot_series(rows, day=day).latest)


def latest_zuot_event(
    states: Sequence[Mapping[str, Any]],
    *,
    recent_bars: int = 3,
) -> dict[str, Any] | None:
    """在最近 N 根分钟里找最后一个做T买/卖信号（看板提升用）。"""
    if not states:
        return None
    window = max(1, int(recent_bars))
    start = max(0, len(states) - window)
    event: dict[str, Any] | None = None
    for index in range(start, len(states)):
        state = states[index]
        if state.get("buy_signal"):
            event = {
                "index": index,
                "signal": "buy",
                "time": str(state.get("time") or "")[:5],
                "price": _number(state.get("close")),
                "reasons": ["LONGCROSS(支撑,现价,2) 回踩支撑↖买"],
                "invalidation_price": _number(state.get("support")),
            }
        elif state.get("sell_signal"):
            event = {
                "index": index,
                "signal": "sell",
                "time": str(state.get("time") or "")[:5],
                "price": _number(state.get("close")),
                "reasons": ["LONGCROSS(现价,阻力,2) 冲高兑现↗卖"],
                "invalidation_price": _number(state.get("resistance")),
            }
    return event


EMA = ema
CROSS = cross
LONGCROSS = longcross


__all__ = [
    "BIG_MINUTE_AMOUNT_YUAN",
    "SIGNAL_VERSION",
    "ZuoTDayContext",
    "ZuoTSeriesResult",
    "compute_zuot_series",
    "compute_zuot_state",
    "latest_zuot_event",
    "ema",
    "cross",
    "longcross",
    "EMA",
    "CROSS",
    "LONGCROSS",
]
