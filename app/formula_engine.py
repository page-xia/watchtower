from __future__ import annotations

"""Point-in-time implementation of the TDX formulas used by the T view."""

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


EPSILON = 1e-12
DEFAULT_TREND_NEAR_THRESHOLD_PCT = 3.0


@dataclass(frozen=True)
class FormulaSeriesResult:
    states: list[dict[str, Any]]
    latest: dict[str, Any]


@dataclass(frozen=True)
class TrendLineSeriesResult:
    states: list[dict[str, Any]]
    latest: dict[str, Any]


@dataclass(frozen=True)
class L1TransactionSummary:
    available: bool = False
    count: int = 0
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    neutral_amount: float = 0.0
    rolling_score: int = 0
    rolling_imbalance_pct: float = 0.0
    l1_buy_support: bool = False
    l1_sell_pressure: bool = False


@dataclass(frozen=True)
class FormulaStateResult:
    state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.state)


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


def _series(value: Sequence[float] | float, length: int) -> list[float]:
    if isinstance(value, (int, float)):
        return [float(value)] * length
    result = [_number(item) for item in value]
    if len(result) < length:
        result.extend([0.0] * (length - len(result)))
    return result[:length]


def ref(values: Sequence[float], periods: int = 1, default: Any = None) -> list[Any]:
    offset = max(0, int(periods))
    result: list[float] = []
    for index, _ in enumerate(values):
        source = index - offset
        result.append(_number(values[source], default) if source >= 0 else default)
    return result


def ma(values: Sequence[float], period: int) -> list[float]:
    window = max(1, int(period))
    result: list[float] = []
    running = 0.0
    queue: list[float] = []
    for value in values:
        item = _number(value)
        queue.append(item)
        running += item
        if len(queue) > window:
            running -= queue.pop(0)
        result.append(running / len(queue) if queue else 0.0)
    return result


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


def sma(values: Sequence[float], period: int, weight: int = 1) -> list[float]:
    window = max(1, int(period))
    m = max(1, min(int(weight), window))
    result: list[float] = []
    previous: float | None = None
    for value in values:
        item = _number(value)
        previous = item if previous is None else (m * item + (window - m) * previous) / window
        result.append(previous)
    return result


def hhv(values: Sequence[float], period: int) -> list[float]:
    window = max(1, int(period))
    result: list[float] = []
    for index, _ in enumerate(values):
        start = max(0, index - window + 1)
        result.append(max(_number(item) for item in values[start : index + 1]))
    return result


def llv(values: Sequence[float], period: int) -> list[float]:
    window = max(1, int(period))
    result: list[float] = []
    for index, _ in enumerate(values):
        start = max(0, index - window + 1)
        result.append(min(_number(item) for item in values[start : index + 1]))
    return result


def cross(left: Sequence[float] | float, right: Sequence[float] | float) -> list[bool]:
    length = max(
        len(left) if not isinstance(left, (int, float)) else 0,
        len(right) if not isinstance(right, (int, float)) else 0,
    )
    left_values = _series(left, length)
    right_values = _series(right, length)
    result: list[bool] = []
    for index, (left_value, right_value) in enumerate(zip(left_values, right_values)):
        if index == 0:
            result.append(False)
            continue
        result.append(left_values[index - 1] <= right_values[index - 1] and left_value > right_value)
    return result


def filter_signal(values: Sequence[bool], period: int) -> list[bool]:
    cooldown_period = max(0, int(period))
    cooldown = 0
    result: list[bool] = []
    for value in values:
        active = bool(value)
        if cooldown > 0:
            result.append(False)
            cooldown -= 1
            continue
        if active:
            result.append(True)
            cooldown = cooldown_period
        else:
            result.append(False)
    return result


def sar(
    highs: Sequence[float],
    lows: Sequence[float],
    closes_or_period: Sequence[float] | int | None = None,
    period: int = 10,
    step: float = 2,
    maximum: float = 20,
) -> list[float]:
    """Approximate TDX SAR(10,2,20) with standard parabolic SAR semantics."""

    if isinstance(closes_or_period, int):
        period = closes_or_period
    del period
    count = min(len(highs), len(lows))
    if count == 0:
        return []
    high_values = [_number(item) for item in highs[:count]]
    low_values = [_number(item) for item in lows[:count]]
    if count == 1:
        return [low_values[0]]

    step_value = max(0.001, _number(step, 2) / 100.0)
    max_value = max(step_value, _number(maximum, 20) / 100.0)
    uptrend = high_values[1] >= high_values[0]
    extreme = high_values[0] if uptrend else low_values[0]
    current = low_values[0] if uptrend else high_values[0]
    af = step_value
    result = [current]

    for index in range(1, count):
        current = current + af * (extreme - current)
        if uptrend:
            current = min(current, low_values[index - 1])
            if index > 1:
                current = min(current, low_values[index - 2])
            if low_values[index] < current:
                uptrend = False
                current = extreme
                extreme = low_values[index]
                af = step_value
            else:
                if high_values[index] > extreme:
                    extreme = high_values[index]
                    af = min(max_value, af + step_value)
        else:
            current = max(current, high_values[index - 1])
            if index > 1:
                current = max(current, high_values[index - 2])
            if high_values[index] > current:
                uptrend = True
                current = extreme
                extreme = high_values[index]
                af = step_value
            else:
                if low_values[index] < extreme:
                    extreme = low_values[index]
                    af = min(max_value, af + step_value)
        result.append(current)
    return result


def trend_line_proximity(
    close: float,
    white_line: float,
    yellow_line: float,
    *,
    threshold_pct: float = DEFAULT_TREND_NEAR_THRESHOLD_PCT,
) -> dict[str, Any]:
    """Return whether price is within the configured percent of either trend line.

    The T rule treats "near white/yellow line" as absolute price gap divided by
    current price. A-share prices are positive, but abs(close) keeps the formula
    explicit and guards bad input.
    """

    close_value = _number(close)
    white_value = _number(white_line)
    yellow_value = _number(yellow_line)
    denominator = abs(close_value)
    threshold = max(0.01, _number(threshold_pct, DEFAULT_TREND_NEAR_THRESHOLD_PCT))
    if denominator <= EPSILON:
        return {
            "white_distance_pct": 999.0,
            "yellow_distance_pct": 999.0,
            "trend_distance_pct": 999.0,
            "near_trend_line": False,
            "near_trend_line_name": "",
            "near_trend_threshold_pct": threshold,
        }
    white_distance = abs(close_value - white_value) / denominator * 100.0
    yellow_distance = abs(close_value - yellow_value) / denominator * 100.0
    if white_distance <= yellow_distance:
        trend_distance = white_distance
        line_name = "白线"
    else:
        trend_distance = yellow_distance
        line_name = "黄线"
    return {
        "white_distance_pct": white_distance,
        "yellow_distance_pct": yellow_distance,
        "trend_distance_pct": trend_distance,
        "near_trend_line": trend_distance <= threshold,
        "near_trend_line_name": line_name,
        "near_trend_threshold_pct": threshold,
    }


def _missing_trend_proximity(threshold_pct: float) -> dict[str, Any]:
    threshold = max(0.01, _number(threshold_pct, DEFAULT_TREND_NEAR_THRESHOLD_PCT))
    return {
        "white_distance_pct": 999.0,
        "yellow_distance_pct": 999.0,
        "trend_distance_pct": 999.0,
        "near_trend_line": False,
        "near_trend_line_name": "",
        "near_trend_threshold_pct": threshold,
    }


def _trend_state_from_lines(
    close: float,
    white_line: float,
    yellow_line: float,
    *,
    threshold_pct: float = DEFAULT_TREND_NEAR_THRESHOLD_PCT,
    source_quality: str = "tdx_formula_daily_trend",
    time_label: str = "",
    point_count: int = 0,
) -> dict[str, Any]:
    close_value = _number(close)
    white_value = _number(white_line)
    yellow_value = _number(yellow_line)
    proximity = (
        trend_line_proximity(close_value, white_value, yellow_value, threshold_pct=threshold_pct)
        if close_value > 0 and white_value > 0 and yellow_value > 0
        else _missing_trend_proximity(threshold_pct)
    )
    white_distance = _number(proximity["white_distance_pct"])
    yellow_distance = _number(proximity["yellow_distance_pct"])
    trend_distance = _number(proximity["trend_distance_pct"])
    near_trend_line = bool(proximity["near_trend_line"])
    near_trend_line_name = str(proximity["near_trend_line_name"])
    threshold = _number(proximity["near_trend_threshold_pct"], DEFAULT_TREND_NEAR_THRESHOLD_PCT)
    return {
        "time": str(time_label or "")[:5],
        "close": round(close_value, 4),
        "白线": round(white_value, 6),
        "黄线": round(yellow_value, 6),
        "白线距离_pct": round(white_distance, 6),
        "黄线距离_pct": round(yellow_distance, 6),
        "趋势线最近距离_pct": round(trend_distance, 6),
        "趋势线接近阈值_pct": threshold,
        "是否接近趋势线": near_trend_line,
        "white_line": round(white_value, 6),
        "yellow_line": round(yellow_value, 6),
        "white_distance_pct": round(white_distance, 6),
        "yellow_distance_pct": round(yellow_distance, 6),
        "trend_distance_pct": round(trend_distance, 6),
        "near_trend_line": near_trend_line,
        "near_trend_line_name": near_trend_line_name,
        "near_trend_threshold_pct": threshold,
        "trend_source_quality": source_quality,
        "point_count": point_count,
    }


def compute_trend_line_series(
    rows: Sequence[Mapping[str, Any]],
    *,
    trend_near_threshold_pct: float = DEFAULT_TREND_NEAR_THRESHOLD_PCT,
    source_quality: str = "tdx_formula_daily_trend",
) -> TrendLineSeriesResult:
    """Compute 趋势公式.md white/yellow lines from daily K-line rows.

    白线: EMA(EMA(C,10),10)
    黄线: (MA(C,14)+MA(C,28)+MA(C,57)+MA(C,114))/4
    """

    normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
    if not normalized:
        return TrendLineSeriesResult(states=[], latest={})
    closes = [_value(row, "close", "price", "last") for row in normalized]
    white_line = ema(ema(closes, 10), 10)
    yellow_parts = [ma(closes, period) for period in (14, 28, 57, 114)]
    yellow_line = [
        sum(part[index] for part in yellow_parts) / len(yellow_parts)
        for index in range(len(closes))
    ]
    threshold = max(0.01, _number(trend_near_threshold_pct, DEFAULT_TREND_NEAR_THRESHOLD_PCT))
    states = [
        _trend_state_from_lines(
            closes[index],
            white_line[index],
            yellow_line[index],
            threshold_pct=threshold,
            source_quality=source_quality,
            time_label=str(row.get("time") or row.get("datetime") or row.get("date") or "")[:5],
            point_count=index + 1,
        )
        for index, row in enumerate(normalized)
    ]
    return TrendLineSeriesResult(states=states, latest=states[-1] if states else {})


def _formula_row_time(row: Mapping[str, Any]) -> str:
    return str(row.get("time") or row.get("datetime") or "")[:5]


def _trend_override_at(
    trend_states: Sequence[Mapping[str, Any]] | None,
    index: int,
    close: float,
    *,
    threshold_pct: float,
) -> dict[str, Any]:
    if not trend_states or index >= len(trend_states):
        return {
            **_missing_trend_proximity(threshold_pct),
            "白线": 0.0,
            "黄线": 0.0,
            "white_line": 0.0,
            "yellow_line": 0.0,
            "trend_source_quality": "trend_unavailable",
        }
    raw = dict(trend_states[index] or {})
    white = _value(raw, "白线", "white_line")
    yellow = _value(raw, "黄线", "yellow_line")
    if white <= 0 or yellow <= 0:
        return {
            **_missing_trend_proximity(threshold_pct),
            "白线": 0.0,
            "黄线": 0.0,
            "white_line": 0.0,
            "yellow_line": 0.0,
            "trend_source_quality": str(raw.get("trend_source_quality") or raw.get("source_quality") or "trend_unavailable"),
        }
    return _trend_state_from_lines(
        close,
        white,
        yellow,
        threshold_pct=threshold_pct,
        source_quality=str(raw.get("trend_source_quality") or raw.get("source_quality") or "tdx_formula_daily_trend"),
        time_label=str(raw.get("time") or ""),
        point_count=int(_number(raw.get("point_count"), index + 1)),
    )


def compute_formula_series(
    rows: Sequence[Mapping[str, Any]],
    *,
    trend_near_threshold_pct: float = DEFAULT_TREND_NEAR_THRESHOLD_PCT,
    trend_states: Sequence[Mapping[str, Any]] | None = None,
) -> FormulaSeriesResult:
    normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
    if not normalized:
        return FormulaSeriesResult(states=[], latest={})

    closes = [_value(row, "close", "price", "last") for row in normalized]
    first_close = next((value for value in closes if value > 0), 0.0)
    opens = [
        _value(row, "open", default=first_close or closes[index])
        for index, row in enumerate(normalized)
    ]
    highs = [
        max(_value(row, "high", default=closes[index]), closes[index])
        for index, row in enumerate(normalized)
    ]
    lows = [
        min(_value(row, "low", default=closes[index]), closes[index])
        for index, row in enumerate(normalized)
    ]

    hhv27 = hhv(highs, 27)
    llv27 = llv(lows, 27)
    rsv27 = [
        _safe_div(closes[index] - llv27[index], hhv27[index] - llv27[index]) * 100.0
        for index in range(len(closes))
    ]
    sma_rsv27 = sma(rsv27, 5, 1)
    duo_strength = [
        3.0 * sma_rsv27[index] - 2.0 * sma(sma_rsv27, 3, 1)[index]
        for index in range(len(closes))
    ]

    hhv55 = hhv(highs, 55)
    llv55 = llv(lows, 55)
    kong_strength = [
        _safe_div(hhv55[index] - closes[index], hhv55[index] - llv55[index]) * 100.0
        for index in range(len(closes))
    ]

    prev_close = ref(closes, 1, closes[0] if closes else 0)
    up_delta = [max(closes[index] - prev_close[index], 0.0) for index in range(len(closes))]
    abs_delta = [abs(closes[index] - prev_close[index]) for index in range(len(closes))]
    rsi_up = sma(up_delta, 3, 1)
    rsi_abs = sma(abs_delta, 3, 1)
    rsi = [_safe_div(rsi_up[index], rsi_abs[index]) * 100.0 for index in range(len(closes))]
    sell_trigger = cross(88.8, rsi)

    prev_low = ref(lows, 1, lows[0] if lows else 0)
    low_abs = [abs(lows[index] - prev_low[index]) for index in range(len(lows))]
    low_up = [max(lows[index] - prev_low[index], 0.0) for index in range(len(lows))]
    var3333 = [
        _safe_div(sma(low_abs, 3, 1)[index], sma(low_up, 3, 1)[index]) * 100.0
        for index in range(len(lows))
    ]
    var4444 = ema([value * 10.0 for value in var3333], 3)
    var5555 = llv(lows, 30)
    var6666 = hhv(var4444, 30)
    ma58 = ma(closes, 58)
    absorption_base = [
        (var4444[index] + var6666[index] * 2.0) / 2.0 if lows[index] <= var5555[index] else 0.0
        for index in range(len(lows))
    ]
    main_absorption_raw = ema(absorption_base, 3)
    main_absorption = [
        main_absorption_raw[index] / 618.0 * (1.0 if ma58[index] > 0 else 0.0)
        for index in range(len(closes))
    ]

    fast_trigger = cross(duo_strength, 6.78)
    ref1 = ref(closes, 1)
    ref2 = ref(closes, 2)
    protection_price = [
        (ref2[index] + ref1[index] + opens[index]) / 3.0
        if ref1[index] is not None and ref2[index] is not None
        else None
        for index in range(len(closes))
    ]
    sar_line = sar(highs, lows, closes, 10, 2, 20)

    states: list[dict[str, Any]] = []
    threshold = max(0.01, _number(trend_near_threshold_pct, DEFAULT_TREND_NEAR_THRESHOLD_PCT))
    for index, row in enumerate(normalized):
        close = closes[index]
        trend_state = _trend_override_at(
            trend_states,
            index,
            close,
            threshold_pct=threshold,
        )
        white_line = _value(trend_state, "白线", "white_line")
        yellow_line = _value(trend_state, "黄线", "yellow_line")
        white_distance = _value(trend_state, "白线距离_pct", "white_distance_pct", default=999.0)
        yellow_distance = _value(trend_state, "黄线距离_pct", "yellow_distance_pct", default=999.0)
        trend_distance = _value(trend_state, "趋势线最近距离_pct", "trend_distance_pct", default=999.0)
        near_trend_line = bool(trend_state.get("是否接近趋势线") or trend_state.get("near_trend_line"))
        near_trend_line_name = str(trend_state.get("near_trend_line_name") or "")
        trend_source_quality = str(trend_state.get("trend_source_quality") or "trend_unavailable")
        formula_buy_reasons: list[str] = []
        if fast_trigger[index]:
            formula_buy_reasons.append("赶快出手=CROSS(多方力度,6.78)")
        if main_absorption[index] > 0:
            formula_buy_reasons.append("主力吸筹>0")
        formula_sell_reasons = ["DRAWICON(CROSS(88.8,RSI),90,15)"] if sell_trigger[index] else []
        buy_candidate = bool(formula_buy_reasons)
        state = {
            "time": _formula_row_time(row),
            "close": round(close, 4),
            "多方力度": round(duo_strength[index], 6),
            "空方力度": round(kong_strength[index], 6),
            "主力吸筹": round(main_absorption[index], 6),
            "赶快出手": bool(fast_trigger[index]),
            "赶快出手原始值": 0,
            "赶快出手说明": "原公式字段为0；图形触发使用CROSS(多方力度,6.78)",
            "今日保护价": round(protection_price[index], 6) if protection_price[index] is not None else None,
            "白线": round(white_line, 6),
            "黄线": round(yellow_line, 6),
            "白线距离_pct": round(white_distance, 6),
            "黄线距离_pct": round(yellow_distance, 6),
            "趋势线最近距离_pct": round(trend_distance, 6),
            "是否接近趋势线": near_trend_line,
            "是否金色共振": False,
            "duo_strength": round(duo_strength[index], 6),
            "kong_strength": round(kong_strength[index], 6),
            "main_absorption": round(main_absorption[index], 6),
            "main_accumulation": round(main_absorption[index], 6),
            "fast_trigger": bool(fast_trigger[index]),
            "quick_entry": bool(fast_trigger[index]),
            "protection_price": round(protection_price[index], 6) if protection_price[index] is not None else None,
            "white_line": round(white_line, 6),
            "yellow_line": round(yellow_line, 6),
            "white_distance_pct": round(white_distance, 6),
            "yellow_distance_pct": round(yellow_distance, 6),
            "trend_distance_pct": round(trend_distance, 6),
            "near_trend_line": near_trend_line,
            "near_trend_line_name": near_trend_line_name,
            "near_trend_threshold_pct": threshold,
            "buy_candidate": buy_candidate,
            "buy_signal_reasons": formula_buy_reasons,
            "formula_buy_reasons": formula_buy_reasons,
            "sell_candidate": bool(sell_trigger[index]),
            "sell_trigger": bool(sell_trigger[index]),
            "sell_signal_reasons": formula_sell_reasons,
            "formula_sell_reasons": formula_sell_reasons,
            "sar": round(sar_line[index], 6) if index < len(sar_line) else 0,
            "rsi": round(rsi[index], 6),
            "source_quality": (
                "tdx_formula_minute+daily_trend"
                if trend_source_quality != "trend_unavailable"
                else "tdx_formula_minute+trend_unavailable"
            ),
            "trend_source_quality": trend_source_quality,
            "validation_status": "research_only",
            "point_count": index + 1,
            "trigger_note": "赶快出手源码为0；实盘触发按CROSS(多方力度,6.78)观察",
        }
        gold, reasons = evaluate_gold_resonance(
            state,
            buy_candidate=buy_candidate,
            l1_sell_pressure=False,
            l1_buy_support=False,
        )
        state["是否金色共振"] = gold
        state["gold_resonance"] = gold
        state["resonance_reasons"] = reasons
        states.append(state)

    return FormulaSeriesResult(states=states, latest=states[-1] if states else {})


def evaluate_gold_resonance(
    formula_state: Mapping[str, Any],
    *,
    buy_candidate: bool,
    l1_sell_pressure: bool = False,
    l1_buy_support: bool = False,
) -> tuple[bool, list[str]]:
    if not buy_candidate:
        return False, ["非买T候选"]
    if l1_sell_pressure:
        return False, ["L1明显抛压否决"]
    formula_buy_candidate = bool(formula_state.get("buy_candidate"))
    fast_trigger = bool(formula_state.get("赶快出手") or formula_state.get("fast_trigger"))
    absorption = _number(
        formula_state.get("主力吸筹")
        or formula_state.get("main_absorption")
        or formula_state.get("main_accumulation")
    )
    near_trend_line = bool(formula_state.get("是否接近趋势线") or formula_state.get("near_trend_line"))
    if not (formula_buy_candidate or fast_trigger or absorption > 0):
        return False, ["非公式买T候选"]
    if not near_trend_line:
        return False, []

    reasons: list[str] = []
    if fast_trigger and absorption > 0:
        reasons.append("赶快出手+主力吸筹")
    elif fast_trigger:
        reasons.append("赶快出手=CROSS(多方力度,6.78)")
    elif absorption > 0:
        reasons.append("主力吸筹>0")
    elif formula_buy_candidate:
        reasons.append("公式买T候选")
    line_name = str(formula_state.get("near_trend_line_name") or "白/黄趋势线")
    reasons.append(f"接近{line_name}")
    if l1_buy_support:
        reasons.append("L1逐笔买盘支持")
    return True, reasons


def _transaction_side(row: Mapping[str, Any], previous_price: float | None) -> str:
    raw_side = row.get("buyorsell")
    if raw_side in {0, "0"}:
        return "buy"
    if raw_side in {1, "1"}:
        return "sell"
    if raw_side not in (None, ""):
        return "neutral"
    price = _number(row.get("price"))
    if previous_price is None or price <= 0:
        return "neutral"
    if price > previous_price:
        return "buy"
    if price < previous_price:
        return "sell"
    return "neutral"


def summarize_l1_transactions(rows: Sequence[Mapping[str, Any]]) -> L1TransactionSummary:
    buy_amount = 0.0
    sell_amount = 0.0
    neutral_amount = 0.0
    count = 0
    previous_price: float | None = None
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        price = _number(raw.get("price"))
        volume = _number(raw.get("vol") or raw.get("volume"))
        amount = _number(raw.get("amount"), price * volume * 100.0)
        if price <= 0 or volume <= 0 or amount <= 0:
            continue
        side = _transaction_side(raw, previous_price)
        if side == "buy":
            buy_amount += amount
        elif side == "sell":
            sell_amount += amount
        else:
            neutral_amount += amount
        previous_price = price
        count += 1
    directional = buy_amount + sell_amount
    imbalance = (buy_amount - sell_amount) / directional * 100.0 if directional > 0 else 0.0
    score = int(max(-100, min(100, round(imbalance))))
    return L1TransactionSummary(
        available=count > 0,
        count=count,
        buy_amount=buy_amount,
        sell_amount=sell_amount,
        neutral_amount=neutral_amount,
        rolling_score=score,
        rolling_imbalance_pct=imbalance,
        l1_buy_support=score >= 18 or imbalance >= 18,
        l1_sell_pressure=score <= -25 or imbalance <= -20,
    )


def evaluate_l1_flow(value: Mapping[str, Any] | L1TransactionSummary | None) -> dict[str, Any]:
    if value is None:
        return {"available": False, "l1_buy_support": False, "l1_sell_pressure": False}
    if isinstance(value, L1TransactionSummary):
        score = value.rolling_score
        imbalance = value.rolling_imbalance_pct
        return {
            "available": value.available,
            "rolling_score": score,
            "rolling_imbalance_pct": imbalance,
            "l1_buy_support": value.l1_buy_support,
            "l1_sell_pressure": value.l1_sell_pressure,
        }
    score = _number(value.get("rolling_score", value.get("score", 0)))
    imbalance = _number(value.get("rolling_imbalance_pct", value.get("imbalance_pct", 0)))
    available = bool(value.get("available", True))
    return {
        "available": available,
        "rolling_score": score,
        "rolling_imbalance_pct": imbalance,
        "l1_buy_support": bool(available and (score >= 18 or imbalance >= 18)),
        "l1_sell_pressure": bool(available and (score <= -25 or imbalance <= -20)),
    }


def compute_formula_state(
    rows: Sequence[Mapping[str, Any]],
    *,
    buy_t_candidate: bool = False,
    l1_flow: Mapping[str, Any] | L1TransactionSummary | None = None,
    trend_near_threshold_pct: float = DEFAULT_TREND_NEAR_THRESHOLD_PCT,
    trend_states: Sequence[Mapping[str, Any]] | None = None,
) -> FormulaStateResult:
    result = compute_formula_series(
        rows,
        trend_near_threshold_pct=trend_near_threshold_pct,
        trend_states=trend_states,
    )
    state = dict(result.latest)
    if not state:
        return FormulaStateResult(state={})
    flow = evaluate_l1_flow(l1_flow)
    formula_buy_candidate = bool(
        state.get("buy_candidate")
        or state.get("赶快出手")
        or state.get("fast_trigger")
        or _number(state.get("主力吸筹") or state.get("main_absorption") or state.get("main_accumulation")) > 0
    )
    gold, reasons = evaluate_gold_resonance(
        state,
        buy_candidate=bool(buy_t_candidate or formula_buy_candidate),
        l1_sell_pressure=bool(flow.get("l1_sell_pressure")),
        l1_buy_support=bool(flow.get("l1_buy_support")),
    )
    state.update(
        {
            "l1_buy_support": bool(flow.get("l1_buy_support")),
            "l1_sell_pressure": bool(flow.get("l1_sell_pressure")),
            "gold_resonance": gold,
            "是否金色共振": gold,
            "resonance_reasons": reasons,
        }
    )
    return FormulaStateResult(state=state)


SMA = sma
EMA = ema
MA = ma
HHV = hhv
LLV = llv
REF = ref
CROSS = cross
FILTER = filter_signal
SAR = sar


__all__ = [
    "FormulaSeriesResult",
    "TrendLineSeriesResult",
    "FormulaStateResult",
    "L1TransactionSummary",
    "compute_formula_series",
    "compute_trend_line_series",
    "compute_formula_state",
    "evaluate_gold_resonance",
    "evaluate_l1_flow",
    "summarize_l1_transactions",
    "trend_line_proximity",
    "sma",
    "ema",
    "ma",
    "hhv",
    "llv",
    "ref",
    "cross",
    "filter_signal",
    "sar",
    "SMA",
    "EMA",
    "MA",
    "HHV",
    "LLV",
    "REF",
    "CROSS",
    "FILTER",
    "SAR",
]
