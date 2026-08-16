from __future__ import annotations

"""Research-first protocol for intraday positive-T and reverse-T studies.

The online signal engine in :mod:`app.signal_engine` predates this protocol
and is kept for API compatibility.  This module is intentionally dependency
light and point-in-time: every feature at index ``i`` is calculated from the
prefix ending at ``i``.  It is therefore suitable both for offline event
studies and for producing an auditable replay layer without turning a
hand-written intuition into a production threshold.

The module does not claim that a candidate is profitable.  It records a
hypothesis, an executable label, counterfactual variants and validation
status.  A small cache or a short history should consequently result in
``sample_insufficient``/``research_only`` rather than a confident trade.
"""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from collections import defaultdict
import hashlib
import math
import random
import statistics
from typing import Any, Iterable, Mapping, Sequence


EPSILON = 1e-12

# These names are deliberately kept as a feature family.  They are useful for
# ablation and explanation, but none of them is a live trading gate.
FORMULA_FEATURE_KEYS: tuple[str, ...] = (
    "formula_rsi",
    "formula_stoch",
    "formula_trend_score",
    "formula_support_score",
    "formula_exhaustion_score",
    "formula_macd_proxy",
    "formula_macd_signal_proxy",
    "formula_bollinger_position",
    "formula_bollinger_width_pct",
    "formula_sar_distance_pct",
    "formula_position_pct",
    "formula_running_high_distance_pct",
)

DAILY_REGIME_FEATURE_KEYS: tuple[str, ...] = (
    "daily_regime_available",
    "daily_regime_status",
    "daily_regime",
    "daily_observations",
    "daily_ma20_distance_pct",
    "daily_macd",
    "daily_macd_signal",
    "daily_adx",
    "daily_bollinger_width_pct",
    "daily_bollinger_position",
    "daily_price_basis",
)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _positive(value: Any, default: float = 0.0) -> float:
    return max(0.0, _float(value, default))


def _pct(value: float, reference: float) -> float:
    return (value - reference) / reference * 100.0 if abs(reference) > EPSILON else 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _median(values: Iterable[float], default: float = 0.0) -> float:
    clean = [item for item in (_float(value) for value in values) if math.isfinite(item)]
    return statistics.median(clean) if clean else default


def _time_key(value: Any) -> tuple[int, int, int]:
    """Return a sortable key for HH:MM[:SS] values without changing labels."""

    text = str(value or "")
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        second = int(parts[2]) if len(parts) > 2 else 0
        return hour, minute, second
    except (TypeError, ValueError, IndexError):
        return 99, 99, 99


def _clock_text(value: Any) -> str:
    """Extract an HH:MM[:SS] token while retaining synthetic bar labels."""

    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        text = text.rsplit("T", 1)[-1]
    if " " in text:
        text = text.rsplit(" ", 1)[-1]
    return text


def _session_minute_number(value: Any) -> int | None:
    """Map a session minute to a monotonic number, excluding lunch break."""

    hour, minute, _ = _time_key(value)
    if (hour, minute) < (9, 30) or (hour, minute) > (15, 0):
        return None
    if (hour, minute) <= (11, 30):
        return (hour * 60 + minute) - (9 * 60 + 30)
    if (hour, minute) >= (13, 0):
        return 120 + (hour * 60 + minute) - (13 * 60)
    return None


def _transaction_bar_bucket(value: Any) -> str:
    """Map TDX L1 transaction interval labels to close-labelled minute bars.

    TDX historical minute data commonly labels the first bar ``09:31`` and
    the first afternoon bar ``13:01``.  Historical transaction rows are
    start-labelled: in real caches, all prints named ``09:31`` sum to the
    second minute bar, not the first.  Move every valid session label to its
    close-labelled bar while capping the morning/afternoon closing prints.
    """

    text = _clock_text(value)[:5]
    hour, minute, _ = _time_key(text)
    total = hour * 60 + minute
    if 9 * 60 + 30 <= total <= 11 * 60 + 30:
        close_total = min(total + 1, 11 * 60 + 30)
        return f"{close_total // 60:02d}:{close_total % 60:02d}"
    if 13 * 60 <= total <= 15 * 60:
        close_total = min(total + 1, 15 * 60)
        return f"{close_total // 60:02d}:{close_total % 60:02d}"
    return text


def _transaction_minute_label(row: Mapping[str, Any]) -> str:
    raw = row.get("raw_time") or row.get("time") or row.get("datetime") or ""
    return _transaction_bar_bucket(_clock_text(raw)[:5])


def _expected_session_minutes(count: int = 240) -> list[str]:
    result: list[str] = []
    for total in range(9 * 60 + 31, 11 * 60 + 31):
        result.append(f"{total // 60:02d}:{total % 60:02d}")
    for total in range(13 * 60 + 1, 15 * 60 + 1):
        result.append(f"{total // 60:02d}:{total % 60:02d}")
    return result[: max(0, int(count))]


def _next_executable_index(rows: Sequence[Mapping[str, Any]], start: int) -> int | None:
    """Return the next valid session bar after a research event."""

    for index in range(max(0, int(start) + 1), len(rows)):
        row = rows[index]
        if _bar_price(row) <= 0:
            continue
        time_label = _bar_time(row)
        # Synthetic unit-test/research rows may omit labels.  They are still
        # usable for a price-path label; real feeds retain the session check.
        # Unit-test/research fixtures may use ``bar_17`` as a positional label.
        # A real out-of-session timestamp is still rejected.
        if (
            time_label
            and not time_label.lower().startswith("bar_")
            and _session_minute_number(time_label) is None
        ):
            continue
        return index
    return None


def _bar_time(row: Mapping[str, Any], fallback: str = "") -> str:
    return _clock_text(row.get("time") or row.get("datetime") or fallback)[:8]


def _bar_price(row: Mapping[str, Any], fallback: float = 0.0) -> float:
    return _positive(row.get("price") or row.get("close") or row.get("last"), fallback)


def _bar_amount(row: Mapping[str, Any], price: float) -> float:
    explicit = _positive(row.get("amount") or row.get("turnover") or row.get("money"))
    if explicit > 0:
        return explicit
    # TDX minute ``vol`` is normally hands.  Keep the conversion explicit
    # and mark it in the feature payload instead of pretending it is an exact
    # exchange turnover field.
    volume = _positive(row.get("vol") or row.get("volume") or row.get("volumn"))
    return volume * price * 100.0


def _bar_high(row: Mapping[str, Any], price: float) -> float:
    return max(price, _positive(row.get("high"), price))


def _bar_low(row: Mapping[str, Any], price: float) -> float:
    low = _positive(row.get("low"), price)
    return min(price, low) if low > 0 else price


def compute_daily_regime(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute point-in-time daily context when enough prior daily bars exist.

    This is a research feature family, not a trading gate.  The caller must
    pass rows strictly before the target session.  With fewer than 20 prior
    sessions the function reports an explicit unavailable state instead of
    filling MA20/MACD/ADX/Bollinger values with intraday data.
    """

    ordered = sorted(
        (dict(row) for row in rows if isinstance(row, Mapping)),
        key=lambda row: str(row.get("trade_date") or row.get("date") or ""),
    )
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for row in ordered:
        raw_close = _positive(row.get("close") or row.get("price"))
        close = _positive(row.get("adj_close"), raw_close)
        if close <= 0:
            continue
        adjustment = close / raw_close if raw_close > EPSILON else 1.0
        closes.append(close)
        highs.append(
            _positive(
                row.get("adj_high"),
                _positive(row.get("high"), raw_close or close) * adjustment,
            )
        )
        lows.append(
            _positive(
                row.get("adj_low"),
                _positive(row.get("low"), raw_close or close) * adjustment,
            )
        )
    adjusted = next(
        (str(row.get("adjustment_method") or "adj_close") for row in ordered if row.get("adj_close")),
        "",
    )
    base: dict[str, Any] = {
        "available": False,
        "status": "insufficient_history",
        "observations": len(closes),
        "required_observations": 20,
        "price_basis": adjusted or "close",
        "ma20": None,
        "ma20_distance_pct": None,
        "macd": None,
        "macd_signal": None,
        "adx": None,
        "bollinger_width_pct": None,
        "bollinger_position": None,
        "regime": "unknown",
    }
    if len(closes) < 20:
        return base

    def ema(values: Sequence[float], period: int) -> float:
        alpha = 2.0 / (period + 1.0)
        result = float(values[0])
        for value in values[1:]:
            result = alpha * float(value) + (1.0 - alpha) * result
        return result

    ma20_window = closes[-20:]
    ma20 = statistics.mean(ma20_window)
    std = statistics.pstdev(ma20_window) if len(ma20_window) > 1 else 0.0
    upper = ma20 + 2.0 * std
    lower = max(0.0, ma20 - 2.0 * std)
    macd = ema(closes, 12) - ema(closes, 26)
    # A signal line needs a history of MACD values; calculate the prefix
    # sequence rather than using any value after the target date.
    macd_series: list[float] = []
    for index in range(len(closes)):
        prefix = closes[: index + 1]
        macd_series.append(ema(prefix, 12) - ema(prefix, 26))
    macd_signal = ema(macd_series, 9)

    true_ranges: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for index in range(1, len(closes)):
        high = highs[index]
        low = lows[index]
        previous_close = closes[index - 1]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        up = high - highs[index - 1]
        down = lows[index - 1] - low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    period = min(14, len(true_ranges))
    tr = sum(true_ranges[-period:]) / max(period, 1)
    plus = sum(plus_dm[-period:]) / max(period, 1)
    minus = sum(minus_dm[-period:]) / max(period, 1)
    plus_di = plus / max(tr, EPSILON) * 100.0
    minus_di = minus / max(tr, EPSILON) * 100.0
    adx = abs(plus_di - minus_di) / max(plus_di + minus_di, EPSILON) * 100.0
    position = (closes[-1] - lower) / (upper - lower) if upper > lower else 0.5
    trend = (
        "up" if closes[-1] > ma20 and macd >= macd_signal and plus_di >= minus_di
        else "down" if closes[-1] < ma20 and macd < macd_signal and minus_di > plus_di
        else "range"
    )
    base.update(
        {
            "available": True,
            "status": "available",
            "ma20": round(ma20, 8),
            "ma20_distance_pct": round(_pct(closes[-1], ma20), 8),
            "macd": round(macd, 8),
            "macd_signal": round(macd_signal, 8),
            "adx": round(adx, 8),
            "bollinger_width_pct": round(_pct(upper, lower), 8) if lower > 0 else None,
            "bollinger_position": round(_clamp(position, 0.0, 1.0), 8),
            "regime": trend,
        }
    )
    return base


def _formula_features(
    prices: Sequence[float],
    index: int,
    vwap: float,
    running_high: float,
) -> dict[str, Any]:
    """Return continuous formula-inspired position features for one prefix."""

    prefix = [value for value in prices[: index + 1] if value > 0]
    if not prefix:
        return {
            "formula_rsi": 50.0,
            "formula_stoch": 50.0,
            "formula_trend_score": 50.0,
            "formula_support_score": 0.0,
            "formula_exhaustion_score": 0.0,
            "formula_macd_proxy": 0.0,
            "formula_macd_signal_proxy": 0.0,
            "formula_bollinger_position": 0.5,
            "formula_bollinger_width_pct": 0.0,
            "formula_sar_distance_pct": 0.0,
            "formula_position_pct": 50.0,
            "formula_running_high_distance_pct": 0.0,
        }
    short = prefix[-9:]
    changes = [short[pos] - short[pos - 1] for pos in range(1, len(short))]
    gains = sum(value for value in changes if value > 0)
    losses = sum(-value for value in changes if value < 0)
    rsi = 100.0 if gains > 0 and losses <= EPSILON else 50.0 if gains <= EPSILON else 100.0 - 100.0 / (1.0 + gains / max(losses, EPSILON))
    low = min(short)
    high = max(short)
    stoch = (short[-1] - low) / (high - low) * 100.0 if high > low else 50.0
    recent_range = prefix[-20:]
    range_low = min(recent_range)
    range_high = max(recent_range)
    position = (prefix[-1] - range_low) / (range_high - range_low) if range_high > range_low else 0.5
    slope = _pct(prefix[-1], prefix[max(0, len(prefix) - 5)]) if len(prefix) > 1 else 0.0
    trend_score = _clamp(stoch * 0.55 + _clamp((slope * 30.0) + 50.0, 0.0, 100.0) * 0.45, 0.0, 100.0)
    support_score = _clamp(
        50.0
        + (1.0 - abs(_pct(prefix[-1], vwap)) / 2.0) * 25.0
        + (1.0 - position) * 25.0,
        0.0,
        100.0,
    )
    exhaustion_score = _clamp(stoch * 0.55 + rsi * 0.45, 0.0, 100.0) if position >= 0.7 else _clamp(rsi * 0.35 + position * 65.0, 0.0, 100.0)

    def ema(values: Sequence[float], period: int) -> float:
        alpha = 2.0 / (period + 1.0)
        result = float(values[0])
        for value in values[1:]:
            result = alpha * float(value) + (1.0 - alpha) * result
        return result

    macd_series: list[float] = []
    for end in range(max(0, len(prefix) - 35), len(prefix)):
        values = prefix[: end + 1]
        macd_series.append(ema(values, 12) - ema(values, 26))
    macd = macd_series[-1] if macd_series else 0.0
    signal = ema(macd_series, 9) if macd_series else 0.0
    band_mean = statistics.mean(recent_range)
    band_std = statistics.pstdev(recent_range) if len(recent_range) > 1 else 0.0
    band_upper = band_mean + 2.0 * band_std
    band_lower = band_mean - 2.0 * band_std
    band_position = (prefix[-1] - band_lower) / (band_upper - band_lower) if band_upper > band_lower else 0.5
    sar_proxy = min(prefix[-5:]) if slope >= 0 else max(prefix[-5:])
    return {
        "formula_rsi": round(rsi, 8),
        "formula_stoch": round(stoch, 8),
        "formula_trend_score": round(trend_score, 8),
        "formula_support_score": round(support_score, 8),
        "formula_exhaustion_score": round(exhaustion_score, 8),
        "formula_macd_proxy": round(macd, 8),
        "formula_macd_signal_proxy": round(signal, 8),
        "formula_bollinger_position": round(_clamp(band_position, 0.0, 1.0), 8),
        "formula_bollinger_width_pct": round(_pct(band_upper, band_lower), 8) if band_lower > 0 else 0.0,
        "formula_sar_distance_pct": round(_pct(prefix[-1], sar_proxy), 8) if sar_proxy > 0 else 0.0,
        "formula_position_pct": round(position * 100.0, 8),
        "formula_running_high_distance_pct": round(_pct(prefix[-1], running_high), 8) if running_high > 0 else 0.0,
    }


@dataclass(frozen=True)
class HypothesisSpec:
    hypothesis_id: str
    title: str
    mechanism: str
    observable_features: tuple[str, ...]
    counterfactual: tuple[str, ...]
    a_priori_label: str
    outcome_definition: str
    status: str = "registered"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


HYPOTHESES: tuple[HypothesisSpec, ...] = (
    HypothesisSpec(
        "H1",
        "核心先动后跟随",
        "板块中的信息/资金冲击先被核心或容量票定价，再向同板块成员传导。",
        ("core_lead_lag", "sector_breadth_change", "stock_relative_strength", "price_response"),
        ("shuffle_core_stock_timing", "matched_same_sector_time"),
        "先手/确认点之后的目标先触达概率与净R",
        "同板块同时间窗口比较有核心先后与无先后的候选，使用下一可成交时段标签。",
    ),
    HypothesisSpec(
        "H2",
        "卖压吸收比单纯买单更重要",
        "卖方成交占优而价格不再恶化，说明供给被承接；随后方向改善才是可交易变化。",
        ("sell_pressure", "low_stability", "price_response_to_sell", "flow_improvement"),
        ("shuffle_same_minute_transactions", "minute_price_only"),
        "吸收候选的 MAE 与目标先触达结果",
        "将卖压吸收与直接买方追价分开标记，比较悲观成交下的 MAE/净R。",
    ),
    HypothesisSpec(
        "H3",
        "成交方向必须结合价格响应",
        "主动成交额方向本身既可能是推动，也可能是高位分配；价格响应效率决定含义。",
        ("direction_imbalance", "price_response", "price_efficiency", "extension"),
        ("minute_price_only", "direction_only"),
        "同方向成交额不同价格响应的条件结果",
        "在相近位置和市场状态下分层，比较方向与价格响应联合特征的增量。",
    ),
    HypothesisSpec(
        "H4",
        "市场和板块是条件变量",
        "个股微观结构的可交易性取决于指数和板块是否同步改善，而不是简单加分。",
        ("market_slope", "market_acceleration", "sector_slope", "sector_breadth"),
        ("context_ablation", "matched_regime_control"),
        "按市场/板块状态分层的方向条件概率",
        "同一微观结构在不同环境状态分别统计，不把环境信息混成单一总分。",
    ),
    HypothesisSpec(
        "H5",
        "首次回踩优于追突破",
        "突破后的首次回踩提供更清晰的失效位，可能改善可实现盈亏比。",
        ("breakout", "first_retest", "support_distance", "price_efficiency"),
        ("entry_timing_control", "matched_breakout_control"),
        "突破即买、首次回踩、延迟追高三组的净R分布",
        "在同一突破事件内只比较事先定义的执行时刻，避免用事后最低点选择买点。",
    ),
    HypothesisSpec(
        "H6",
        "开盘先验是否有增量信息",
        "竞价和开盘早期成交轨迹可能改善早盘位置判断，但不应被假定为有效。",
        ("auction_prior", "opening_flow_path", "opening_price_response", "early_mae"),
        ("opening_prior_ablation", "late_session_control"),
        "加入与不加入先验时的早盘 MAE、提前量和目标概率差",
        "只比较增量预测效果；缺失竞价数据保持缺失，不用开盘价冒充真实竞价。",
    ),
    HypothesisSpec(
        "H7",
        "公式特征改善位置判断",
        "多窗口趋势、低位承接和高位钝化更可能帮助位置判断，而非独立产生买点。",
        ("trend_context", "support_location", "exhaustion", "formula_auxiliary"),
        ("formula_ablation", "minute_price_only"),
        "加入公式启发特征后追高/过早抄底错误率变化",
        "公式特征只作为分层或解释变量，不能单独把事件升级为可执行交易。",
    ),
)

HYPOTHESIS_REGISTRY: dict[str, HypothesisSpec] = {item.hypothesis_id: item for item in HYPOTHESES}


@dataclass(frozen=True)
class ProtocolConfig:
    """Study controls, not live buy/sell thresholds.

    The few numeric values here define measurement windows and data-quality
    requirements.  Feature values are normalized into rolling/训练集分位数
    before comparisons, so the protocol does not search for a magic number.
    """

    study_name: str = "intraday_t_research_first"
    protocol_version: str = "research_protocol_v1"
    warmup_bars: int = 5
    execution_delay_bars: int = 1
    outcome_horizons: tuple[int, ...] = (5, 15, 30)
    target_r_multiple: float = 1.5
    stop_volatility_multiple: float = 1.5
    friction_pct: float = 0.20
    pessimistic_slippage_pct: float = 0.10
    minimum_days: int = 20
    out_of_sample_days: int = 60
    minimum_events: int = 30
    bootstrap_iterations: int = 400
    quantile_bins: int = 3
    cooldown_bars: int = 3
    # Computational bound for the cross-stock control.  It is a study
    # resource limit, never an entry/exit condition.  A truncated control set
    # is reported explicitly so it cannot be mistaken for a null result.
    max_matched_control_events: int = 12000
    seed: int = 20260809


@dataclass
class ResearchSample:
    code: str
    name: str = ""
    trade_date: str = ""
    bars: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    market_bars: list[dict[str, Any]] = field(default_factory=list)
    sector_bars: list[list[dict[str, Any]]] = field(default_factory=list)
    sector_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    one_word: bool = False
    # Position fields are optional research context.  A missing position must
    # not silently turn a theoretical reverse-T label into an executable plan.
    position_quantity: float = 0.0
    available_quantity: float = 0.0
    position_known: bool = False
    entry_date: str = ""
    t_plus_one_restricted: bool = False
    auction_prior: dict[str, Any] = field(default_factory=dict)
    source_quality: str = "minute_proxy"

    def key(self) -> tuple[str, str]:
        return str(self.code).zfill(6), str(self.trade_date)


@dataclass(frozen=True)
class EventCandidate:
    code: str
    name: str
    trade_date: str
    index: int
    time: str
    direction: str
    setup: str
    hypothesis_id: str
    features: dict[str, Any] = field(default_factory=dict)
    evidence_sequence: tuple[str, ...] = ()
    source_quality: str = "minute_proxy"
    validation_status: str = "research_only"
    executable: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_sequence"] = list(self.evidence_sequence)
        return payload


@dataclass(frozen=True)
class TradeLabel:
    code: str
    name: str
    trade_date: str
    candidate_time: str
    candidate_index: int
    direction: str
    setup: str
    execution_time: str = ""
    execution_index: int = -1
    execution_price: float = 0.0
    sell_price: float = 0.0
    buyback_price: float = 0.0
    target_price: float = 0.0
    invalidation_price: float = 0.0
    risk_pct: float = 0.0
    gross_return_pct: float = 0.0
    net_return_pct: float = 0.0
    net_r: float = 0.0
    target_first: bool | None = None
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    holding_bars: int = 0
    fill_status: str = "no_fill"
    no_fill_reason: str = ""
    execution_reason: str = ""
    t_plus_one_blocked: bool = False
    path_quality: str = "close_only"
    horizon: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TradeOutcome:
    """A label outcome with enough fields for clustered statistics."""

    code: str
    trade_date: str
    candidate_time: str
    direction: str
    setup: str
    candidate_index: int = -1
    candidate_ordinal: int = 0
    hypothesis_id: str = ""
    target_first: bool | None = None
    net_return_pct: float = 0.0
    net_r: float = 0.0
    risk_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    holding_bars: int = 0
    fill_status: str = "no_fill"
    no_fill_reason: str = ""
    source_quality: str = ""
    horizon: int = 0
    counterfactual: str = "observed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataManifest:
    trade_date: str
    code: str = ""
    minute_count: int = 0
    expected_minute_count: int = 240
    minute_coverage: float = 0.0
    transaction_count: int = 0
    transaction_coverage: float = 0.0
    transaction_minute_count: int = 0
    transaction_expected_minute_count: int = 240
    transaction_first_time: str = ""
    transaction_last_time: str = ""
    transaction_first_raw_time: str = ""
    transaction_last_raw_time: str = ""
    transaction_page_count: int = 0
    transaction_page_numbers: tuple[int, ...] = ()
    transaction_sequence_count: int = 0
    transaction_sequence_first: int | None = None
    transaction_sequence_last: int | None = None
    transaction_raw_time_count: int = 0
    transaction_metadata_coverage: float = 0.0
    transaction_sequence_ordered: bool = False
    transaction_time_gaps: tuple[str, ...] = ()
    transaction_missing_minutes: tuple[str, ...] = ()
    time_gaps: tuple[str, ...] = ()
    direction_counts: dict[str, int] = field(default_factory=dict)
    source_quality: str = "unavailable"
    auction_available: bool = False
    one_word: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["time_gaps"] = list(self.time_gaps)
        payload["transaction_page_numbers"] = list(self.transaction_page_numbers)
        payload["transaction_time_gaps"] = list(self.transaction_time_gaps)
        payload["transaction_missing_minutes"] = list(self.transaction_missing_minutes)
        payload["notes"] = list(self.notes)
        return payload


def registered_hypotheses() -> list[dict[str, Any]]:
    return [item.to_dict() for item in HYPOTHESES]


def get_hypothesis(hypothesis_id: str) -> HypothesisSpec | None:
    return HYPOTHESIS_REGISTRY.get(str(hypothesis_id or "").upper())


def normalize_transaction_direction(
    row: Mapping[str, Any],
    previous_price: float | None = None,
) -> tuple[int, str]:
    """Return ``(+1/-1/0, quality)`` while retaining special values as neutral.

    TDX commonly uses ``0`` for buy and ``1`` for sell.  Unknown numeric
    values are deliberately neutral.  A price-tick fallback is allowed only
    when the direction field is absent, never when it contains an unknown
    special value.
    """

    raw = row.get("buyorsell", row.get("direction", None))
    if raw is not None and str(raw).strip() != "":
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return 0, "special_neutral"
        if value == 0:
            return 1, "buyorsell"
        if value == 1:
            return -1, "buyorsell"
        return 0, "special_neutral"

    if previous_price is not None:
        price = _bar_price(row)
        if price > previous_price:
            return 1, "price_tick_fallback"
        if price < previous_price:
            return -1, "price_tick_fallback"
    return 0, "neutral"


def _transaction_rows_by_time(
    transactions: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group prints without deduplicating or changing their original order."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    previous_price: float | None = None
    for sequence, raw in enumerate(transactions):
        row = dict(raw)
        raw_time = str(row.get("raw_time") or row.get("time") or row.get("datetime") or "")
        minute_label = _transaction_minute_label(row)
        direction, quality = normalize_transaction_direction(row, previous_price)
        price = _bar_price(row, previous_price or 0.0)
        row["_sequence"] = sequence
        row["_raw_time"] = raw_time
        row["_direction"] = direction
        row["_direction_quality"] = quality
        row["_amount"] = _bar_amount(row, price)
        row["_price"] = price
        # Feature buckets are minute-labelled (09:31), while transaction
        # prints may carry seconds (09:31:27).  Keep the original time in the
        # row but aggregate by the completed minute without dropping prints.
        if minute_label:
            grouped.setdefault(minute_label, []).append(row)
        if price > 0:
            previous_price = price
    return grouped


def _context_features(
    rows: Sequence[Mapping[str, Any]],
    count: int,
    prefix: str,
) -> list[dict[str, float]]:
    prices: list[float] = []
    amounts: list[float] = []
    result: list[dict[str, float]] = []
    for index in range(count):
        row = rows[index] if index < len(rows) else (rows[-1] if rows else {})
        price = _bar_price(row, prices[-1] if prices else 0.0)
        amount = _bar_amount(row, price)
        prices.append(price)
        amounts.append(amount)
        history_amounts = amounts[:-1]
        baseline = _median(history_amounts[-20:], amount) or amount
        previous = prices[-2] if len(prices) > 1 else price
        prior = prices[-4] if len(prices) > 3 else previous
        slope = _pct(price, prior) if prior else 0.0
        previous_slope = (
            _pct(previous, prices[-5] if len(prices) > 4 else previous)
            if len(prices) > 1
            else 0.0
        )
        result.append(
            {
                f"{prefix}_price": price,
                f"{prefix}_amount": amount,
                f"{prefix}_amount_ratio": amount / baseline if baseline > 0 else 1.0,
                f"{prefix}_slope": slope,
                f"{prefix}_acceleration": slope - previous_slope,
            }
        )
    return result


def _transaction_flow_features(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize one minute of L1 prints without calling them queue data.

    The first/second half fields preserve a small amount of within-minute
    ordering information.  They are deliberately continuous research
    variables: a shuffled tape can be compared with the observed tape, while
    no single field is promoted to a live buy rule.
    """

    tx_rows = list(rows)
    if not tx_rows:
        return {
            "buy_amount": 0.0,
            "sell_amount": 0.0,
            "neutral_amount": 0.0,
            "transaction_amount": 0.0,
            "transaction_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "neutral_count": 0,
            "special_neutral_count": 0,
            "direction_imbalance": 0.0,
            "first_half_imbalance": 0.0,
            "second_half_imbalance": 0.0,
            "flow_order_shift": 0.0,
            "transaction_price_response_pct": 0.0,
            "buy_price_response_pct": 0.0,
            "sell_price_response_pct": 0.0,
            "size_proxy_imbalance": 0.0,
            "transaction_first_sequence": None,
            "transaction_last_sequence": None,
        }

    def amount(row: Mapping[str, Any]) -> float:
        return _positive(row.get("_amount"), _bar_amount(row, _bar_price(row)))

    def direction(row: Mapping[str, Any]) -> int:
        return int(_float(row.get("_direction")))

    def imbalance(part: Sequence[Mapping[str, Any]]) -> float:
        buy = sum(amount(row) for row in part if direction(row) > 0)
        sell = sum(amount(row) for row in part if direction(row) < 0)
        return (buy - sell) / (buy + sell) * 100.0 if buy + sell > EPSILON else 0.0

    buy_rows = [row for row in tx_rows if direction(row) > 0]
    sell_rows = [row for row in tx_rows if direction(row) < 0]
    neutral_rows = [row for row in tx_rows if direction(row) == 0]
    buy_amount = sum(amount(row) for row in buy_rows)
    sell_amount = sum(amount(row) for row in sell_rows)
    neutral_amount = sum(amount(row) for row in neutral_rows)
    total = buy_amount + sell_amount + neutral_amount
    midpoint = max(1, len(tx_rows) // 2)
    first_half = tx_rows[:midpoint]
    second_half = tx_rows[midpoint:]
    prices = [_positive(row.get("_price"), _bar_price(row)) for row in tx_rows]
    first_price = next((value for value in prices if value > 0), 0.0)
    last_price = next((value for value in reversed(prices) if value > 0), first_price)

    def directional_response(part: Sequence[Mapping[str, Any]], sign: int) -> float:
        selected = [
            _positive(row.get("_price"), _bar_price(row))
            for row in part
            if direction(row) == sign and _positive(row.get("_price"), _bar_price(row)) > 0
        ]
        return _pct(selected[-1], selected[0]) if len(selected) >= 2 else 0.0

    # A within-minute median size is a relative size proxy.  It is not a
    # claim about exchange-defined "large orders".
    median_size = _median((amount(row) for row in tx_rows), 0.0)
    size_rows = [row for row in tx_rows if amount(row) >= median_size] if median_size > 0 else []
    size_buy = sum(amount(row) for row in size_rows if direction(row) > 0)
    size_sell = sum(amount(row) for row in size_rows if direction(row) < 0)
    return {
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "neutral_amount": neutral_amount,
        "transaction_amount": total,
        "transaction_count": len(tx_rows),
        "buy_count": len(buy_rows),
        "sell_count": len(sell_rows),
        "neutral_count": len(neutral_rows),
        "special_neutral_count": sum(
            1 for row in neutral_rows if str(row.get("_direction_quality") or "") == "special_neutral"
        ),
        "direction_imbalance": (buy_amount - sell_amount) / (buy_amount + sell_amount) * 100.0
        if buy_amount + sell_amount > EPSILON
        else 0.0,
        "first_half_imbalance": imbalance(first_half),
        "second_half_imbalance": imbalance(second_half),
        "flow_order_shift": imbalance(second_half) - imbalance(first_half),
        "transaction_price_response_pct": _pct(last_price, first_price) if first_price > 0 else 0.0,
        "buy_price_response_pct": directional_response(tx_rows, 1),
        "sell_price_response_pct": directional_response(tx_rows, -1),
        "size_proxy_imbalance": (size_buy - size_sell) / (size_buy + size_sell) * 100.0
        if size_buy + size_sell > EPSILON
        else 0.0,
        "transaction_first_sequence": tx_rows[0].get("source_sequence", tx_rows[0].get("_sequence")),
        "transaction_last_sequence": tx_rows[-1].get("source_sequence", tx_rows[-1].get("_sequence")),
    }


def extract_point_features(
    bars: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]] | None = None,
    *,
    market_bars: Sequence[Mapping[str, Any]] | None = None,
    sector_bars: Sequence[Sequence[Mapping[str, Any]]] | None = None,
    prev_close: float = 0.0,
    auction_prior: Mapping[str, Any] | None = None,
    daily_regime: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract continuous, point-in-time features.

    The function intentionally does not accept a future window.  Appending a
    future bar cannot change any returned prefix item, which is an easy audit
    invariant for look-ahead prevention.
    """

    normalized = [dict(row) for row in bars if isinstance(row, Mapping)]
    if not normalized:
        return []
    count = len(normalized)
    prices: list[float] = []
    amounts: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    times: list[str] = []
    tx_groups = _transaction_rows_by_time(list(transactions or []))
    market = _context_features(list(market_bars or []), count, "market")
    sector_contexts = [
        _context_features(list(rows), count, f"sector_{idx}")
        for idx, rows in enumerate(sector_bars or [])
        if rows
    ]
    daily = dict(daily_regime or {})
    daily_status = str(daily.get("status") or "insufficient_history")
    daily_available = bool(daily.get("available")) and daily_status == "available"
    previous_flow = 0.0
    running_vwap_amount = 0.0
    running_vwap_volume = 0.0
    result: list[dict[str, Any]] = []
    for index, row in enumerate(normalized):
        fallback = prices[-1] if prices else _positive(prev_close)
        price = _bar_price(row, fallback)
        amount = _bar_amount(row, price)
        volume = _positive(row.get("vol") or row.get("volume"))
        time_label = _bar_time(row, f"bar_{index}")
        high = _bar_high(row, price)
        low = _bar_low(row, price)
        prices.append(price)
        amounts.append(amount)
        highs.append(high)
        lows.append(low)
        times.append(time_label)
        running_vwap_amount += amount
        running_vwap_volume += volume * 100.0 if volume > 0 else amount / max(price, EPSILON)
        vwap = running_vwap_amount / max(running_vwap_volume, EPSILON)
        history_amounts = amounts[:-1]
        baseline_amount = _median(history_amounts[-20:], amount) or amount
        recent = prices[max(0, index - 4): index + 1]
        prior_recent = prices[max(0, index - 9): max(0, index - 4)]
        previous = prices[index - 1] if index else price
        previous_previous = prices[index - 2] if index > 1 else previous
        slope_1 = _pct(price, previous) if previous else 0.0
        slope_3 = _pct(price, prices[index - 3]) if index >= 3 and prices[index - 3] else slope_1
        prior_slope_3 = (
            _pct(previous, prices[index - 4])
            if index >= 4 and prices[index - 4]
            else 0.0
        )
        acceleration = slope_3 - prior_slope_3
        local_low = min(recent) if recent else price
        prior_low = min(prior_recent) if prior_recent else local_low
        local_high = max(recent) if recent else price
        prior_high = max(prior_recent) if prior_recent else local_high
        rebound = _pct(price, min(lows[max(0, index - 20): index + 1]) or price)
        pullback = _pct(max(highs[max(0, index - 20): index + 1]) or price, price)
        range_width = max(recent) - min(recent) if recent else 0.0
        displacement = abs(price - previous_previous)
        efficiency = displacement / max(sum(abs(prices[pos] - prices[pos - 1]) for pos in range(max(1, index - 3), index + 1)), EPSILON)

        tx_rows = tx_groups.get(time_label[:5], [])
        if not tx_rows and time_label:
            tx_rows = tx_groups.get(time_label, [])
        tx_flow = _transaction_flow_features(tx_rows)
        buy_amount = _float(tx_flow.get("buy_amount"))
        sell_amount = _float(tx_flow.get("sell_amount"))
        neutral_amount = _float(tx_flow.get("neutral_amount"))
        total_tx_amount = _float(tx_flow.get("transaction_amount"))
        flow_imbalance = _float(tx_flow.get("direction_imbalance"))
        prior_flow = previous_flow
        flow_change = flow_imbalance - previous_flow
        previous_flow = flow_imbalance
        sell_pressure = flow_imbalance < 0 and sell_amount > buy_amount
        low_stable = bool(index > 0 and local_low >= prior_low * 0.998)
        price_response = slope_1 if sell_pressure else slope_3
        response_efficiency = price_response / max(abs(flow_imbalance), 1.0)
        amount_ratio = amount / max(baseline_amount, EPSILON)
        sector_rows = [item[index] for item in sector_contexts if item]
        sector_slope = _median(item.get(f"sector_{idx}_slope", 0.0) for idx, item in enumerate(sector_rows)) if sector_rows else 0.0
        sector_amount_ratio = (
            _median(
                (item.get(f"sector_{idx}_amount_ratio", 1.0) for idx, item in enumerate(sector_rows)),
                1.0,
            )
            if sector_rows
            else 1.0
        )
        market_item = market[index] if market else {}
        market_slope = _float(market_item.get("market_slope"))
        market_acceleration = _float(market_item.get("market_acceleration"))
        sector_improving = sector_slope > 0 or sector_amount_ratio > 1.0
        market_improving = market_slope > 0 or market_acceleration > 0
        breakout = bool(index >= 2 and price >= prior_high and slope_1 > 0 and amount_ratio >= 1.0)
        after_breakout = any(
            prices[pos] >= max(prices[:pos] or [prices[pos]])
            for pos in range(max(1, index - 5), index)
        )
        first_retest = bool(
            after_breakout
            and index >= 2
            and price > previous
            and low >= min(prices[max(0, index - 4): index]) * 0.998
            and price <= local_high * 1.002
        )
        absorption = bool(
            sell_pressure
            and low_stable
            and (
                slope_1 >= 0
                or flow_change > 0
                or _float(tx_flow.get("flow_order_shift")) > 0
                or _float(tx_flow.get("transaction_price_response_pct")) >= 0
            )
        )
        response_improving = bool(
            (flow_change > 0 or _float(tx_flow.get("flow_order_shift")) > 0)
            and slope_1 >= prior_slope_3
        )
        extension = _pct(price, min(prices[max(0, index - 20): index + 1]) or price)
        exhaustion = bool(
            index >= 2
            and slope_1 <= 0
            and (flow_imbalance < previous_flow or amount_ratio > 1.0)
            and price >= local_high * 0.995
        )
        # ``previous_flow`` was updated above; use a local prior value from the
        # feature stream for the visible comparison.
        prior_feature_flow = prior_flow
        exhaustion = bool(
            index >= 2
            and (slope_1 <= 0 or efficiency < 0.35)
            and price >= local_high * 0.995
            and (flow_imbalance <= prior_feature_flow or amount_ratio >= 1.0)
        )
        opening_prior = dict(auction_prior or {})
        auction_available = bool(opening_prior.get("available"))
        feature = {
            "index": index,
            "time": time_label,
            "price": price,
            "high": high,
            "low": low,
            "amount": amount,
            "volume": volume,
            "amount_ratio": amount_ratio,
            "rolling_amount_median": baseline_amount,
            "vwap": vwap,
            "vwap_distance_pct": _pct(price, vwap),
            "change_pct": _pct(price, prev_close) if prev_close else 0.0,
            "slope_1": slope_1,
            "slope_3": slope_3,
            "price_acceleration": acceleration,
            "local_low": local_low,
            "local_high": local_high,
            "prior_low": prior_low,
            "prior_high": prior_high,
            "rebound_pct": rebound,
            "pullback_pct": pullback,
            "range_width": range_width,
            "price_efficiency": efficiency,
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "neutral_amount": neutral_amount,
            "transaction_amount": total_tx_amount,
            "transaction_count": len(tx_rows),
            "buy_count": int(tx_flow.get("buy_count") or 0),
            "sell_count": int(tx_flow.get("sell_count") or 0),
            "neutral_count": int(tx_flow.get("neutral_count") or 0),
            "special_neutral_count": int(tx_flow.get("special_neutral_count") or 0),
            "direction_imbalance": flow_imbalance,
            "direction_change": flow_change,
            "first_half_imbalance": _float(tx_flow.get("first_half_imbalance")),
            "second_half_imbalance": _float(tx_flow.get("second_half_imbalance")),
            "flow_order_shift": _float(tx_flow.get("flow_order_shift")),
            "transaction_price_response_pct": _float(tx_flow.get("transaction_price_response_pct")),
            "buy_price_response_pct": _float(tx_flow.get("buy_price_response_pct")),
            "sell_price_response_pct": _float(tx_flow.get("sell_price_response_pct")),
            "size_proxy_imbalance": _float(tx_flow.get("size_proxy_imbalance")),
            "transaction_first_sequence": tx_flow.get("transaction_first_sequence"),
            "transaction_last_sequence": tx_flow.get("transaction_last_sequence"),
            "sell_pressure": sell_pressure,
            "low_stable": low_stable,
            "price_response": price_response,
            "response_efficiency": response_efficiency,
            "absorption": absorption,
            "response_improving": response_improving,
            "breakout": breakout,
            "first_retest": first_retest,
            "extension_pct": extension,
            "exhaustion": exhaustion,
            "market_slope": market_slope,
            "market_acceleration": market_acceleration,
            "market_improving": market_improving,
            "sector_slope": sector_slope,
            "sector_amount_ratio": sector_amount_ratio,
            "sector_improving": sector_improving,
            "auction_available": auction_available,
            "auction_change_pct": _float(opening_prior.get("change_pct")),
            "auction_imbalance_pct": _float(opening_prior.get("order_imbalance_pct")),
            "source_quality": "l1_transaction" if tx_rows else "minute_price_amount_proxy",
            "daily_regime_available": daily_available,
            "daily_regime_status": daily_status,
            "daily_regime": str(daily.get("regime") or "unknown"),
            "daily_observations": int(daily.get("observations") or 0),
            "daily_ma20_distance_pct": daily.get("ma20_distance_pct"),
            "daily_macd": daily.get("macd"),
            "daily_macd_signal": daily.get("macd_signal"),
            "daily_adx": daily.get("adx"),
            "daily_bollinger_width_pct": daily.get("bollinger_width_pct"),
            "daily_bollinger_position": daily.get("bollinger_position"),
            "daily_price_basis": str(daily.get("price_basis") or "unavailable"),
        }
        feature.update(_formula_features(prices, index, vwap, max(highs) if highs else price))
        # Keep the prefix invariant explicit: no context fields are calculated
        # from bars after ``index``.
        result.append(feature)
    return result


def _evidence_for_positive(feature: Mapping[str, Any]) -> tuple[str, ...]:
    evidence: list[str] = []
    if _float(feature.get("slope_3")) > 0:
        evidence.append("个股价格推进改善")
    if feature.get("absorption"):
        evidence.append("卖压后价格响应未继续恶化")
    if feature.get("response_improving"):
        evidence.append("成交方向改善且价格有响应")
    if _float(feature.get("flow_order_shift")) > 0:
        evidence.append("分钟后半段成交方向改善")
    if _float(feature.get("transaction_price_response_pct")) > 0:
        evidence.append("逐笔成交后的价格响应为正")
    if feature.get("sector_improving"):
        evidence.append("板块动能同步改善")
    if feature.get("market_improving"):
        evidence.append("市场斜率/加速度改善")
    if feature.get("breakout"):
        evidence.append("突破前序结构")
    if feature.get("first_retest"):
        evidence.append("突破后首次回踩承接")
    return tuple(evidence)


def _evidence_for_reverse(feature: Mapping[str, Any]) -> tuple[str, ...]:
    evidence: list[str] = []
    if feature.get("exhaustion"):
        evidence.append("高位推进效率下降")
    if _float(feature.get("slope_1")) < 0:
        evidence.append("短周期价格斜率转弱")
    if feature.get("sell_pressure"):
        evidence.append("成交方向偏卖且价格响应变差")
    if _float(feature.get("flow_order_shift")) < 0:
        evidence.append("分钟后半段成交方向转弱")
    if not feature.get("sector_improving"):
        evidence.append("板块未同步改善")
    if not feature.get("market_improving"):
        evidence.append("市场未同步改善")
    return tuple(evidence)


def generate_candidates(
    sample: ResearchSample,
    *,
    features: Sequence[Mapping[str, Any]] | None = None,
    config: ProtocolConfig | None = None,
) -> list[EventCandidate]:
    """Generate sparse research events from state transitions.

    This is a candidate generator, not a trade rule.  It looks for changes in
    structure and evidence order and leaves target/stop/execution to the label
    stage.  A cooldown is only used to avoid emitting the same transition on
    every minute.
    """

    protocol = config or ProtocolConfig()
    rows = list(features or extract_point_features(
        sample.bars,
        sample.transactions,
        market_bars=sample.market_bars,
        sector_bars=sample.sector_bars,
        prev_close=_float(sample.metadata.get("prev_close")),
        auction_prior=sample.auction_prior,
        daily_regime=sample.metadata.get("daily_regime"),
    ))
    candidates: list[EventCandidate] = []
    last_by_direction: dict[str, int] = {}
    previous: Mapping[str, Any] = {}
    for index, feature in enumerate(rows):
        if index < protocol.warmup_bars:
            previous = feature
            continue
        evidence = _evidence_for_positive(feature)
        prior_evidence = _evidence_for_positive(previous)
        positive_transition = bool(
            (feature.get("absorption") and feature.get("response_improving"))
            or (feature.get("first_retest"))
            or (
                feature.get("breakout")
                and (feature.get("sector_improving") or feature.get("market_improving"))
            )
            or (
                _float(feature.get("slope_1")) > 0
                and _float(previous.get("slope_1")) <= 0
                and (_float(feature.get("amount_ratio")) > 1 or feature.get("response_improving"))
            )
        )
        reverse_evidence = _evidence_for_reverse(feature)
        reverse_transition = bool(
            feature.get("exhaustion")
            or (
                _float(feature.get("slope_1")) < 0
                and _float(previous.get("slope_1")) >= 0
                and feature.get("sell_pressure")
            )
        )
        # Evidence order is informative only when at least one new item
        # appears.  This prevents a persistent trend from creating a new event
        # every minute while retaining early and confirmation transitions.
        positive_new = tuple(item for item in evidence if item not in prior_evidence)
        reverse_new = tuple(item for item in reverse_evidence if item not in _evidence_for_reverse(previous))
        for direction, active, new_evidence, setup, hypothesis in (
            ("positive_t", positive_transition, positive_new, "先手结构", "H1"),
            ("reverse_t", reverse_transition, reverse_new, "高位衰竭/破位", "H3"),
        ):
            if not active or not new_evidence:
                continue
            prior_index = last_by_direction.get(direction, -10_000)
            if index - prior_index < protocol.cooldown_bars:
                continue
            if direction == "positive_t":
                if feature.get("first_retest"):
                    setup = "首次回踩"
                    hypothesis = "H5"
                elif feature.get("absorption"):
                    setup = "卖压吸收后响应"
                    hypothesis = "H2"
                elif feature.get("breakout"):
                    setup = "核心/板块传导突破"
                    hypothesis = "H1"
                elif feature.get("response_improving"):
                    hypothesis = "H3"
                sequence = new_evidence
            else:
                sequence = new_evidence
                if feature.get("sell_pressure") and feature.get("exhaustion"):
                    hypothesis = "H3"
            candidates.append(
                EventCandidate(
                    code=str(sample.code).zfill(6),
                    name=sample.name,
                    trade_date=sample.trade_date,
                    index=index,
                    time=str(feature.get("time") or ""),
                    direction=direction,
                    setup=setup,
                    hypothesis_id=hypothesis,
                    features=dict(feature),
                    evidence_sequence=tuple(sequence),
                    source_quality=str(feature.get("source_quality") or sample.source_quality),
                    validation_status="research_only",
                    executable=False,
                )
            )
            last_by_direction[direction] = index
        previous = feature
    return candidates


def _is_four_factor_baseline(candidate: EventCandidate) -> bool:
    """Identify the pre-registered four-factor comparison group.

    This is intentionally a named *control* for the study, not a production
    gate.  The observed candidate generator remains free to record earlier or
    incomplete transitions so the study can measure what confirmation costs.
    """

    feature = candidate.features
    return bool(
        candidate.direction == "positive_t"
        and feature.get("market_improving")
        and feature.get("sector_improving")
        and feature.get("breakout")
        and _float(feature.get("amount_ratio")) >= 1.0
        and (
            feature.get("response_improving")
            or _float(feature.get("direction_imbalance")) > 0
            or feature.get("absorption")
        )
    )


def _structural_levels(
    features: Sequence[Mapping[str, Any]],
    index: int,
    config: ProtocolConfig,
) -> tuple[float, float, float]:
    feature = features[index]
    price = _positive(feature.get("price"))
    start = max(0, index - 8)
    visible = [_positive(item.get("price")) for item in features[start: index + 1]]
    visible = [item for item in visible if item > 0]
    if not visible or price <= 0:
        return 0.0, 0.0, 0.0
    support = max(
        _positive(feature.get("vwap")),
        _positive(feature.get("local_low")),
        min(visible),
    )
    resistance = max(visible)
    # Risk is derived from observed local movement, not a user supplied
    # percentage.  The protocol config only says how far the observed noise is
    # projected for a label.
    moves = [abs(visible[pos] - visible[pos - 1]) for pos in range(1, len(visible))]
    noise = _median(moves, price * 0.002)
    risk_distance = max(price * 0.001, noise * config.stop_volatility_multiple)
    return support, resistance, risk_distance


def _execution_price(row: Mapping[str, Any], direction: str, slippage_pct: float) -> float:
    price = _bar_price(row)
    if price <= 0:
        return 0.0
    if direction == "positive_t":
        return price * (1.0 + slippage_pct / 100.0)
    return price * (1.0 - slippage_pct / 100.0)


def label_candidate(
    sample: ResearchSample,
    candidate: EventCandidate,
    *,
    features: Sequence[Mapping[str, Any]] | None = None,
    config: ProtocolConfig | None = None,
    horizon: int | None = None,
) -> TradeLabel:
    """Create a conservative, executable-time label for one candidate."""

    protocol = config or ProtocolConfig()
    rows = list(features or extract_point_features(
        sample.bars,
        sample.transactions,
        market_bars=sample.market_bars,
        sector_bars=sample.sector_bars,
        prev_close=_float(sample.metadata.get("prev_close")),
        auction_prior=sample.auction_prior,
        daily_regime=sample.metadata.get("daily_regime"),
    ))
    selected_horizon = int(horizon or (protocol.outcome_horizons[-1] if protocol.outcome_horizons else 30))
    execution_anchor = candidate.index + max(0, protocol.execution_delay_bars - 1)
    execution_index = _next_executable_index(rows, execution_anchor)
    if execution_index is None or execution_index >= len(sample.bars):
        return TradeLabel(
            code=candidate.code,
            name=candidate.name,
            trade_date=candidate.trade_date,
            candidate_time=candidate.time,
            candidate_index=candidate.index,
            direction=candidate.direction,
            setup=candidate.setup,
            fill_status="no_fill",
            no_fill_reason="候选点后没有下一可成交时段",
            execution_reason="没有后续有效交易时段，不能假设成交",
            horizon=selected_horizon,
        )
    execution_row = sample.bars[execution_index]
    entry = _execution_price(execution_row, candidate.direction, protocol.pessimistic_slippage_pct)
    if entry <= 0:
        return TradeLabel(
            code=candidate.code,
            name=candidate.name,
            trade_date=candidate.trade_date,
            candidate_time=candidate.time,
            candidate_index=candidate.index,
            direction=candidate.direction,
            setup=candidate.setup,
            execution_index=execution_index,
            execution_time=str(rows[execution_index].get("time") or ""),
            fill_status="no_fill",
            no_fill_reason="下一时段没有有效价格",
            execution_reason="下一可成交时段价格无效",
            horizon=selected_horizon,
        )
    t_plus_one_blocked = bool(
        candidate.direction == "reverse_t"
        and (
            sample.t_plus_one_restricted
            or (
                sample.position_known
                and sample.position_quantity > 0
                and sample.available_quantity <= 0
            )
        )
    )
    if candidate.direction == "reverse_t" and sample.position_known and sample.position_quantity <= 0:
        return TradeLabel(
            code=candidate.code,
            name=candidate.name,
            trade_date=candidate.trade_date,
            candidate_time=candidate.time,
            candidate_index=candidate.index,
            direction=candidate.direction,
            setup=candidate.setup,
            execution_index=execution_index,
            execution_time=str(rows[execution_index].get("time") or ""),
            fill_status="no_fill",
            no_fill_reason="没有可用于反T的底仓",
            execution_reason="持仓上下文明确且底仓数量为0",
            t_plus_one_blocked=False,
            horizon=selected_horizon,
        )
    if t_plus_one_blocked:
        return TradeLabel(
            code=candidate.code,
            name=candidate.name,
            trade_date=candidate.trade_date,
            candidate_time=candidate.time,
            candidate_index=candidate.index,
            direction=candidate.direction,
            setup=candidate.setup,
            execution_index=execution_index,
            execution_time=str(rows[execution_index].get("time") or ""),
            execution_price=round(entry, 6),
            fill_status="no_fill",
            no_fill_reason="T+1或可卖数量限制，反T先卖不可成交",
            execution_reason="研究标签保留，但当前持仓不可执行反T卖出",
            t_plus_one_blocked=True,
            horizon=selected_horizon,
        )
    if sample.one_word:
        return TradeLabel(
            code=candidate.code,
            name=candidate.name,
            trade_date=candidate.trade_date,
            candidate_time=candidate.time,
            candidate_index=candidate.index,
            direction=candidate.direction,
            setup=candidate.setup,
            execution_index=execution_index,
            execution_time=str(rows[execution_index].get("time") or ""),
            execution_price=round(entry, 6),
            fill_status="no_fill",
            no_fill_reason="一字板/无可成交路径，样本保留但不删除",
            execution_reason="涨跌停路径不对成交做乐观假设",
            path_quality="close_only",
            t_plus_one_blocked=t_plus_one_blocked,
            horizon=selected_horizon,
        )

    support, resistance, risk_distance = _structural_levels(rows, candidate.index, protocol)
    if candidate.direction == "positive_t":
        invalidation = min(entry, support - risk_distance) if support > 0 else entry - risk_distance
        if invalidation <= 0 or invalidation >= entry:
            invalidation = entry - risk_distance
        target = entry + max(risk_distance * protocol.target_r_multiple, entry * 0.001)
    else:
        invalidation = entry + risk_distance
        target = max(entry - max(risk_distance * protocol.target_r_multiple, entry * 0.001), 0.0001)
    risk_pct = abs(_pct(entry, invalidation))
    max_end = min(len(rows), execution_index + max(1, selected_horizon) + 1)
    future_rows = sample.bars[execution_index + 1: max_end]
    path_quality = "ohlc" if any(
        _positive(row.get("high")) > 0 or _positive(row.get("low")) > 0
        for row in future_rows
    ) else "close_only"
    target_first: bool | None = None
    exit_price = entry
    exit_index = execution_index
    mfe = 0.0
    mae = 0.0
    for offset, row in enumerate(future_rows, start=execution_index + 1):
        close = _bar_price(row, entry)
        high = _bar_high(row, close)
        low = _bar_low(row, close)
        if candidate.direction == "positive_t":
            favorable = _pct(high, entry)
            adverse = _pct(low, entry)
            mfe = max(mfe, favorable)
            mae = min(mae, adverse)
            hit_target = high >= target
            hit_stop = low <= invalidation
        else:
            favorable = _pct(entry, low)
            adverse = _pct(entry, high)
            mfe = max(mfe, favorable)
            mae = min(mae, adverse)
            hit_target = low <= target
            hit_stop = high >= invalidation
        if hit_target or hit_stop:
            # If a minute contains both extremes, choose the adverse side first
            # because OHLC/close data cannot establish the intraminute order.
            target_first = bool(hit_target and not hit_stop)
            exit_price = invalidation if hit_stop else target
            exit_index = offset
            break
        exit_price = close
        exit_index = offset
    if target_first is None and future_rows:
        target_first = False if candidate.direction == "positive_t" and mae <= -risk_pct else None
    if candidate.direction == "positive_t":
        gross = _pct(exit_price, entry)
        net = gross - protocol.friction_pct - protocol.pessimistic_slippage_pct * 2
    else:
        gross = _pct(entry, exit_price)
        net = gross - protocol.friction_pct - protocol.pessimistic_slippage_pct * 2
    net_r = net / risk_pct if risk_pct > EPSILON else 0.0
    return TradeLabel(
        code=candidate.code,
        name=candidate.name,
        trade_date=candidate.trade_date,
        candidate_time=candidate.time,
        candidate_index=candidate.index,
        direction=candidate.direction,
        setup=candidate.setup,
        execution_time=str(rows[execution_index].get("time") or ""),
        execution_index=execution_index,
        execution_price=round(entry, 6),
        sell_price=round(entry, 6) if candidate.direction == "reverse_t" else 0.0,
        buyback_price=round(exit_price, 6) if candidate.direction == "reverse_t" else 0.0,
        target_price=round(target, 6),
        invalidation_price=round(invalidation, 6),
        risk_pct=round(risk_pct, 6),
        gross_return_pct=round(gross, 6),
        net_return_pct=round(net, 6),
        net_r=round(net_r, 6),
        target_first=target_first,
        mfe_pct=round(mfe, 6),
        mae_pct=round(mae, 6),
        holding_bars=max(0, exit_index - execution_index),
        fill_status="filled",
        execution_reason=(
            "理论标签：未提供持仓上下文"
            if candidate.direction == "reverse_t" and not sample.position_known
            else "下一可成交时段按悲观滑点成交"
        ),
        t_plus_one_blocked=False,
        path_quality=path_quality,
        horizon=selected_horizon,
    )


def label_candidates(
    sample: ResearchSample,
    candidates: Sequence[EventCandidate],
    *,
    features: Sequence[Mapping[str, Any]] | None = None,
    config: ProtocolConfig | None = None,
) -> list[TradeLabel]:
    protocol = config or ProtocolConfig()
    rows = list(features or extract_point_features(
        sample.bars,
        sample.transactions,
        market_bars=sample.market_bars,
        sector_bars=sample.sector_bars,
        prev_close=_float(sample.metadata.get("prev_close")),
        auction_prior=sample.auction_prior,
        daily_regime=sample.metadata.get("daily_regime"),
    ))
    labels: list[TradeLabel] = []
    for candidate in candidates:
        for horizon in protocol.outcome_horizons:
            labels.append(label_candidate(sample, candidate, features=rows, config=protocol, horizon=horizon))
    return labels


def outcomes_from_labels(
    candidates: Sequence[EventCandidate],
    labels: Sequence[TradeLabel],
    *,
    source_quality: str = "",
) -> list[TradeOutcome]:
    # Do not collapse labels into a dictionary keyed only by index/direction.
    # Two state transitions can happen on the same minute (for example an
    # absorption setup and a retest setup), and each transition has several
    # horizons.  Group by setup and consume in original order so every label is
    # retained without inventing a candidate id or reordering the tape.
    label_groups: dict[tuple[int, str, str], list[TradeLabel]] = defaultdict(list)
    for label in labels:
        label_groups[(label.candidate_index, label.direction, label.setup)].append(label)
    candidate_groups: dict[tuple[int, str, str], list[EventCandidate]] = defaultdict(list)
    for candidate in candidates:
        candidate_groups[(candidate.index, candidate.direction, candidate.setup)].append(candidate)
    group_offsets: dict[tuple[int, str, str], int] = defaultdict(int)
    result: list[TradeOutcome] = []
    for candidate_ordinal, candidate in enumerate(candidates):
        key = (candidate.index, candidate.direction, candidate.setup)
        group = label_groups.get(key, [])
        candidates_in_group = candidate_groups.get(key, [])
        if len(candidates_in_group) <= 1:
            assigned = group
        else:
            # Labels are emitted candidate-major by ``label_candidates``.  If
            # duplicate transitions share all three fields, divide the group
            # into equal horizon blocks; any remainder stays with the last
            # candidate rather than being silently discarded.
            offset = group_offsets[key]
            block = max(1, len(group) // len(candidates_in_group))
            position = next(
                (item_index for item_index, item in enumerate(candidates_in_group) if item is candidate),
                0,
            )
            start = position * block
            end = (position + 1) * block if position < len(candidates_in_group) - 1 else len(group)
            assigned = group[start:end]
            group_offsets[key] = max(group_offsets[key], end)
        for label in assigned:
            result.append(
                TradeOutcome(
                    code=candidate.code,
                    trade_date=candidate.trade_date,
                    candidate_time=candidate.time,
                    candidate_index=candidate.index,
                    candidate_ordinal=candidate_ordinal,
                    direction=candidate.direction,
                    setup=candidate.setup,
                    hypothesis_id=candidate.hypothesis_id,
                    target_first=label.target_first,
                    net_return_pct=label.net_return_pct,
                    net_r=label.net_r,
                    risk_pct=label.risk_pct,
                    mfe_pct=label.mfe_pct,
                    mae_pct=label.mae_pct,
                    holding_bars=label.holding_bars,
                    fill_status=label.fill_status,
                    no_fill_reason=label.no_fill_reason,
                    source_quality=source_quality or candidate.source_quality,
                    horizon=label.horizon,
                )
            )
    return result


def build_data_manifest(sample: ResearchSample, *, expected_minute_count: int = 240) -> DataManifest:
    times = [_bar_time(row) for row in sample.bars if _bar_time(row)]
    gaps: list[str] = []
    for previous, current in zip(times, times[1:]):
        previous_number = _session_minute_number(previous)
        current_number = _session_minute_number(current)
        if previous_number is not None and current_number is not None and current_number > previous_number + 1:
            missing = current_number - previous_number - 1
            gaps.append(f"{previous}->{current}（缺{missing}分钟）")
        elif _time_key(current) <= _time_key(previous):
            gaps.append(f"{previous}->{current}")
    grouped = _transaction_rows_by_time(sample.transactions)
    direction_counts = {"buy": 0, "sell": 0, "neutral": 0, "special_neutral": 0}
    for rows in grouped.values():
        for row in rows:
            direction = row.get("_direction", 0)
            quality = str(row.get("_direction_quality") or "")
            if direction > 0:
                direction_counts["buy"] += 1
            elif direction < 0:
                direction_counts["sell"] += 1
            else:
                direction_counts["special_neutral" if quality == "special_neutral" else "neutral"] += 1
    minute_count = len(sample.bars)
    transaction_count = len(sample.transactions)
    transaction_minutes = {
        _transaction_minute_label(row)
        for row in sample.transactions
        if _session_minute_number(_transaction_minute_label(row)) is not None
    }
    ordered_transaction_minutes = sorted(transaction_minutes, key=_time_key)
    transaction_gaps: list[str] = []
    for previous, current in zip(ordered_transaction_minutes, ordered_transaction_minutes[1:]):
        previous_number = _session_minute_number(previous)
        current_number = _session_minute_number(current)
        if previous_number is not None and current_number is not None and current_number > previous_number + 1:
            transaction_gaps.append(
                f"{previous}->{current}（缺{current_number - previous_number - 1}分钟）"
            )

    expected_minutes = _expected_session_minutes(expected_minute_count)
    transaction_missing_minutes = tuple(
        minute for minute in expected_minutes if minute not in transaction_minutes
    )
    raw_times = [
        str(row.get("raw_time") or row.get("time") or row.get("datetime") or "")
        for row in sample.transactions
    ]
    transaction_times = [
        _transaction_minute_label(row)
        for row in sample.transactions
        if _transaction_minute_label(row)
    ]

    def optional_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    pages = [
        page
        for page in (optional_int(row.get("source_page")) for row in sample.transactions)
        if page is not None
    ]
    sequences = [
        sequence
        for sequence in (optional_int(row.get("source_sequence")) for row in sample.transactions)
        if sequence is not None
    ]
    complete_metadata_count = sum(
        1
        for row in sample.transactions
        if row.get("source_page") is not None
        and row.get("source_sequence") is not None
        and bool(str(row.get("raw_time") or ""))
    )
    chronological_rows = sorted(
        sample.transactions,
        key=lambda row: (
            _time_key(_transaction_minute_label(row)),
            optional_int(row.get("source_sequence"))
            if optional_int(row.get("source_sequence")) is not None
            else 10**15,
        ),
    )
    first_raw_time = (
        str(chronological_rows[0].get("raw_time") or chronological_rows[0].get("time") or "")
        if chronological_rows
        else ""
    )
    last_raw_time = (
        str(chronological_rows[-1].get("raw_time") or chronological_rows[-1].get("time") or "")
        if chronological_rows
        else ""
    )
    return DataManifest(
        trade_date=sample.trade_date,
        code=str(sample.code).zfill(6),
        minute_count=minute_count,
        expected_minute_count=expected_minute_count,
        minute_coverage=round(minute_count / max(expected_minute_count, 1), 6),
        transaction_count=transaction_count,
        transaction_coverage=round(
            len(transaction_minutes) / max(expected_minute_count, 1),
            6,
        )
        if transaction_count
        else 0.0,
        transaction_minute_count=len(transaction_minutes),
        transaction_expected_minute_count=expected_minute_count,
        transaction_first_time=min(transaction_times, key=_time_key) if transaction_times else "",
        transaction_last_time=max(transaction_times, key=_time_key) if transaction_times else "",
        transaction_first_raw_time=first_raw_time,
        transaction_last_raw_time=last_raw_time,
        transaction_page_count=len(set(pages)),
        transaction_page_numbers=tuple(sorted(set(pages))),
        transaction_sequence_count=len(sequences),
        transaction_sequence_first=min(sequences) if sequences else None,
        transaction_sequence_last=max(sequences) if sequences else None,
        transaction_raw_time_count=sum(bool(value) for value in raw_times),
        transaction_metadata_coverage=(
            round(complete_metadata_count / transaction_count, 6)
            if transaction_count
            else 0.0
        ),
        transaction_sequence_ordered=bool(
            sequences and all(current >= previous for previous, current in zip(sequences, sequences[1:]))
        ),
        transaction_time_gaps=tuple(transaction_gaps),
        transaction_missing_minutes=transaction_missing_minutes,
        time_gaps=tuple(gaps),
        direction_counts=direction_counts,
        source_quality=sample.source_quality or ("l1_transaction" if transaction_count else "minute_proxy"),
        auction_available=bool(sample.auction_prior.get("available")),
        one_word=sample.one_word,
        notes=tuple(
            item for item in (
                "逐笔原始顺序保留" if transaction_count else "无历史逐笔成交",
                "逐笔含页/序号/原始时间元数据" if complete_metadata_count == transaction_count and transaction_count else (
                    "逐笔缺少完整页/序号元数据" if transaction_count else ""
                ),
                f"逐笔交易分钟缺口{len(transaction_missing_minutes)}个" if transaction_count and transaction_missing_minutes else "",
                "无真实集合竞价数据" if not sample.auction_prior.get("available") else "含竞价先验",
                "一字板样本保留并标记no_fill" if sample.one_word else "",
            )
            if item
        ),
    )


def _cluster_key(outcome: Mapping[str, Any] | TradeOutcome) -> tuple[str, str]:
    if isinstance(outcome, TradeOutcome):
        return outcome.code, outcome.trade_date
    return str(outcome.get("code") or ""), str(outcome.get("trade_date") or "")


def _outcome_value(outcome: Mapping[str, Any] | TradeOutcome, key: str) -> float:
    value = getattr(outcome, key, None) if isinstance(outcome, TradeOutcome) else outcome.get(key)
    return _float(value)


def clustered_bootstrap_ci(
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
    *,
    metric: str = "net_r",
    statistic: str = "mean",
    iterations: int = 400,
    seed: int = 20260809,
    confidence: float = 0.90,
) -> dict[str, Any]:
    """Bootstrap whole stock-day clusters, never individual minute events."""

    clusters: dict[tuple[str, str], list[Mapping[str, Any] | TradeOutcome]] = {}
    for outcome in outcomes:
        clusters.setdefault(_cluster_key(outcome), []).append(outcome)
    values = [_outcome_value(item, metric) for item in outcomes]

    def calculate(items: Sequence[Mapping[str, Any] | TradeOutcome]) -> float:
        numbers = [_outcome_value(item, metric) for item in items]
        if not numbers:
            return 0.0
        if statistic == "median":
            return float(statistics.median(numbers))
        return float(statistics.mean(numbers))

    observed = calculate(outcomes)
    if not clusters or iterations <= 0:
        return {"estimate": round(observed, 6), "low": None, "high": None, "clusters": len(clusters), "iterations": 0}
    rng = random.Random(seed)
    cluster_values = list(clusters.values())
    samples: list[float] = []
    for _ in range(int(iterations)):
        drawn: list[Mapping[str, Any] | TradeOutcome] = []
        for _cluster in range(len(cluster_values)):
            drawn.extend(rng.choice(cluster_values))
        samples.append(calculate(drawn))
    samples.sort()
    alpha = max(0.0, min(0.49, (1.0 - confidence) / 2.0))
    low_index = int(alpha * max(0, len(samples) - 1))
    high_index = int((1.0 - alpha) * max(0, len(samples) - 1))
    return {
        "estimate": round(observed, 6),
        "low": round(samples[low_index], 6) if samples else None,
        "high": round(samples[high_index], 6) if samples else None,
        "clusters": len(clusters),
        "observations": len(values),
        "iterations": int(iterations),
        "confidence": confidence,
        "metric": metric,
        "statistic": statistic,
    }


def summarize_outcomes(
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
    *,
    group_key: str | None = None,
    bootstrap_iterations: int = 400,
    seed: int = 20260809,
) -> dict[str, Any]:
    """Return multi-objective metrics for one group."""

    if group_key:
        # This function accepts one group at a time; retaining the key in the
        # result makes report assembly explicit and avoids hidden filtering.
        group_value = getattr(outcomes[0], group_key, "") if outcomes else ""
    else:
        group_value = None
    filled = [item for item in outcomes if str(getattr(item, "fill_status", item.get("fill_status") if isinstance(item, Mapping) else "") or "") == "filled"]
    no_fill = len(outcomes) - len(filled)
    nets = [_outcome_value(item, "net_return_pct") for item in filled]
    rs = [_outcome_value(item, "net_r") for item in filled]
    target_flags = [item.target_first if isinstance(item, TradeOutcome) else item.get("target_first") for item in filled]
    target_values = [bool(value) for value in target_flags if value is not None]
    maes = [_outcome_value(item, "mae_pct") for item in filled]
    mfes = [_outcome_value(item, "mfe_pct") for item in filled]
    wins = [value for value in rs if value > 0]
    losses = [abs(value) for value in rs if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    result = {
        "group": group_value,
        "count": len(outcomes),
        "filled_count": len(filled),
        "no_fill_count": no_fill,
        "fill_rate_pct": round(len(filled) / len(outcomes) * 100, 3) if outcomes else 0.0,
        "target_observed_count": len(target_values),
        "target_first_probability_pct": round(sum(target_values) / len(target_values) * 100, 3) if target_values else None,
        "mean_net_r": round(statistics.mean(rs), 6) if rs else None,
        "median_net_r": round(statistics.median(rs), 6) if rs else None,
        "mean_net_return_pct": round(statistics.mean(nets), 6) if nets else None,
        "median_net_return_pct": round(statistics.median(nets), 6) if nets else None,
        "mean_mfe_pct": round(statistics.mean(mfes), 6) if mfes else None,
        "mean_mae_pct": round(statistics.mean(maes), 6) if maes else None,
        "mae_p90_pct": round(sorted(maes)[min(len(maes) - 1, int(len(maes) * 0.90))], 6) if maes else None,
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > EPSILON else (None if not wins else "inf"),
        "avg_win_r": round(statistics.mean(wins), 6) if wins else None,
        "avg_loss_r": round(statistics.mean(losses), 6) if losses else None,
        "direction_counts": {},
        "setup_counts": {},
    }
    for item in outcomes:
        direction = str(getattr(item, "direction", item.get("direction") if isinstance(item, Mapping) else "") or "none")
        setup = str(getattr(item, "setup", item.get("setup") if isinstance(item, Mapping) else "") or "")
        result["direction_counts"][direction] = result["direction_counts"].get(direction, 0) + 1
        result["setup_counts"][setup] = result["setup_counts"].get(setup, 0) + 1
    result["bootstrap_mean_net_r"] = clustered_bootstrap_ci(
        outcomes,
        metric="net_r",
        iterations=bootstrap_iterations,
        seed=seed,
    )
    return result


def summarize_by(
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
    key: str,
    *,
    bootstrap_iterations: int = 400,
    seed: int = 20260809,
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any] | TradeOutcome]] = {}
    for item in outcomes:
        value = getattr(item, key, None) if isinstance(item, TradeOutcome) else item.get(key)
        groups.setdefault(str(value or ""), []).append(item)
    return [
        summarize_outcomes(items, group_key=key, bootstrap_iterations=bootstrap_iterations, seed=seed + idx)
        for idx, items in sorted(enumerate(groups.values()), key=lambda pair: str(getattr(pair[1][0], key, "") if isinstance(pair[1][0], TradeOutcome) else pair[1][0].get(key, "")))
    ]


def independent_outcomes(
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
) -> list[Mapping[str, Any] | TradeOutcome]:
    """Collapse measurement horizons without collapsing distinct events.

    A candidate observed at 09:35 can have 5/15/30-minute labels, but those
    labels are three views of one trade, not three independent samples.  The
    longest pre-registered horizon is the primary validation label.  Shorter
    horizons remain in the raw report for path and timing analysis.
    """

    grouped: dict[tuple[str, ...], Mapping[str, Any] | TradeOutcome] = {}
    order: list[tuple[str, ...]] = []
    for occurrence, item in enumerate(outcomes):
        def field(name: str, default: Any = "") -> Any:
            if isinstance(item, TradeOutcome):
                return getattr(item, name, default)
            return item.get(name, default)

        ordinal = int(_float(field("candidate_ordinal", occurrence), occurrence))
        key = (
            str(field("counterfactual", "observed") or "observed"),
            str(field("trade_date") or ""),
            str(field("code") or "").zfill(6),
            str(field("candidate_index", field("candidate_time", ""))),
            str(field("direction") or "none"),
            str(field("setup") or ""),
            str(field("hypothesis_id") or ""),
            str(ordinal),
        )
        if key not in grouped:
            order.append(key)
            grouped[key] = item
            continue
        current = grouped[key]
        current_horizon = int(
            _float(
                current.horizon
                if isinstance(current, TradeOutcome)
                else current.get("horizon", 0)
            )
        )
        if int(_float(field("horizon", 0))) >= current_horizon:
            grouped[key] = item
    return [grouped[key] for key in order]


def outcomes_with_extra_friction(
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
    *,
    extra_cost_pct: float,
) -> list[Mapping[str, Any] | TradeOutcome]:
    """Apply an additional round-trip cost without rerunning path labels."""

    extra = max(0.0, _float(extra_cost_pct))
    result: list[Mapping[str, Any] | TradeOutcome] = []
    for item in outcomes:
        fill_status = str(
            item.fill_status if isinstance(item, TradeOutcome) else item.get("fill_status", "")
        )
        if fill_status != "filled" or extra <= 0:
            result.append(item)
            continue
        risk_pct = _outcome_value(item, "risk_pct")
        adjusted_return = _outcome_value(item, "net_return_pct") - extra
        adjusted_r = (
            adjusted_return / risk_pct
            if risk_pct > EPSILON
            else _outcome_value(item, "net_r") - extra
        )
        if isinstance(item, TradeOutcome):
            result.append(
                replace(
                    item,
                    net_return_pct=round(adjusted_return, 6),
                    net_r=round(adjusted_r, 6),
                )
            )
        else:
            payload = dict(item)
            payload.update(
                {
                    "net_return_pct": round(adjusted_return, 6),
                    "net_r": round(adjusted_r, 6),
                    "extra_friction_pct": extra,
                }
            )
            result.append(payload)
    return result


def _date_stability(
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
) -> dict[str, Any]:
    by_date: dict[str, list[Mapping[str, Any] | TradeOutcome]] = defaultdict(list)
    for item in outcomes:
        fill_status = str(
            item.fill_status if isinstance(item, TradeOutcome) else item.get("fill_status", "")
        )
        if fill_status != "filled":
            continue
        trade_date = str(
            item.trade_date if isinstance(item, TradeOutcome) else item.get("trade_date", "")
        )
        if trade_date:
            by_date[trade_date].append(item)
    rows = [
        {
            "trade_date": trade_date,
            "filled_count": len(items),
            "mean_net_r": round(
                statistics.mean(_outcome_value(item, "net_r") for item in items),
                6,
            ),
        }
        for trade_date, items in sorted(by_date.items())
        if items
    ]
    absolute_total = sum(abs(_float(item["mean_net_r"])) for item in rows)
    largest_share = (
        max(abs(_float(item["mean_net_r"])) for item in rows) / absolute_total * 100
        if rows and absolute_total > EPSILON
        else None
    )
    positive_count = sum(_float(item["mean_net_r"]) > 0 for item in rows)
    return {
        "date_count": len(rows),
        "positive_date_count": positive_count,
        "positive_date_rate_pct": round(positive_count / len(rows) * 100, 3) if rows else None,
        "largest_absolute_date_share_pct": round(largest_share, 3) if largest_share is not None else None,
        "daily": rows,
    }


def walk_forward_evaluation(
    *,
    dates: Sequence[str],
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
    config: ProtocolConfig | None = None,
) -> dict[str, Any]:
    """Run expanding-window, one-session-ahead validation folds.

    The first ``minimum_days`` sessions are available only for hypothesis and
    broad-bin discovery.  Every later session is evaluated once using only
    earlier dates as its training history.  No event appears in more than one
    OOS fold, and 5/15/30-minute labels are counted as one event.
    """

    protocol = config or ProtocolConfig()
    ordered_dates = sorted({str(value) for value in dates if str(value)})
    primary = independent_outcomes(outcomes)
    first_oos_index = max(1, int(protocol.minimum_days))
    extra_cost = max(0.0, _float(protocol.pessimistic_slippage_pct) * 2.0)
    folds: list[dict[str, Any]] = []
    oos_rows: list[Mapping[str, Any] | TradeOutcome] = []
    if len(ordered_dates) > first_oos_index:
        for test_index in range(first_oos_index, len(ordered_dates)):
            training_dates = ordered_dates[:test_index]
            test_date = ordered_dates[test_index]
            train = [item for item in primary if str(getattr(item, "trade_date", item.get("trade_date") if isinstance(item, Mapping) else "")) in set(training_dates)]
            test = [item for item in primary if str(getattr(item, "trade_date", item.get("trade_date") if isinstance(item, Mapping) else "")) == test_date]
            oos_rows.extend(test)
            fold_directions: dict[str, Any] = {}
            for direction in ("positive_t", "reverse_t"):
                direction_rows = [
                    item
                    for item in test
                    if str(getattr(item, "direction", item.get("direction") if isinstance(item, Mapping) else "")) == direction
                ]
                fold_directions[direction] = summarize_outcomes(
                    direction_rows,
                    bootstrap_iterations=0,
                    seed=protocol.seed + test_index,
                )
            folds.append(
                {
                    "fold": len(folds) + 1,
                    "training_start": training_dates[0],
                    "training_end": training_dates[-1],
                    "training_date_count": len(training_dates),
                    "test_date": test_date,
                    "train_independent_event_count": len(train),
                    "oos_independent_event_count": len(test),
                    "base": summarize_outcomes(test, bootstrap_iterations=0),
                    "pessimistic": summarize_outcomes(
                        outcomes_with_extra_friction(test, extra_cost_pct=extra_cost),
                        bootstrap_iterations=0,
                    ),
                    "direction": fold_directions,
                }
            )
    initial_training_dates = ordered_dates[: min(first_oos_index, len(ordered_dates))]
    initial_training = [
        item
        for item in primary
        if str(getattr(item, "trade_date", item.get("trade_date") if isinstance(item, Mapping) else ""))
        in set(initial_training_dates)
    ]
    aggregate_direction: dict[str, Any] = {}
    for direction in ("positive_t", "reverse_t"):
        direction_rows = [
            item
            for item in oos_rows
            if str(getattr(item, "direction", item.get("direction") if isinstance(item, Mapping) else "")) == direction
        ]
        aggregate_direction[direction] = {
            "base": summarize_outcomes(
                direction_rows,
                bootstrap_iterations=protocol.bootstrap_iterations,
                seed=protocol.seed + (11 if direction == "positive_t" else 17),
            ),
            "pessimistic": summarize_outcomes(
                outcomes_with_extra_friction(direction_rows, extra_cost_pct=extra_cost),
                bootstrap_iterations=protocol.bootstrap_iterations,
                seed=protocol.seed + (19 if direction == "positive_t" else 23),
            ),
            "date_stability": _date_stability(direction_rows),
        }
    return {
        "method": "expanding_window_one_day_ahead",
        "event_unit": "one candidate; longest pre-registered horizon is the primary label",
        "minimum_training_days": first_oos_index,
        "required_total_days": max(first_oos_index + 1, int(protocol.out_of_sample_days)),
        "date_count": len(ordered_dates),
        "initial_training_dates": initial_training_dates,
        "initial_training_independent_event_count": len(initial_training),
        "fold_count": len(folds),
        "oos_dates": [item["test_date"] for item in folds],
        "oos_independent_event_count": len(oos_rows),
        "extra_pessimistic_round_trip_cost_pct": round(extra_cost, 6),
        "base": summarize_outcomes(
            oos_rows,
            bootstrap_iterations=protocol.bootstrap_iterations,
            seed=protocol.seed + 29,
        ),
        "pessimistic": summarize_outcomes(
            outcomes_with_extra_friction(oos_rows, extra_cost_pct=extra_cost),
            bootstrap_iterations=protocol.bootstrap_iterations,
            seed=protocol.seed + 31,
        ),
        "date_stability": _date_stability(oos_rows),
        "direction": aggregate_direction,
        "folds": folds,
        "available": bool(folds),
        "complete": bool(
            len(ordered_dates) >= max(first_oos_index + 1, int(protocol.out_of_sample_days))
            and len(folds) >= max(1, int(protocol.out_of_sample_days) - first_oos_index)
        ),
    }


def counterfactual_transactions(
    transactions: Sequence[Mapping[str, Any]],
    *,
    seed: int = 20260809,
) -> list[dict[str, Any]]:
    """Shuffle print order within each minute while preserving all prints."""

    rng = random.Random(seed)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in transactions:
        time_label = _transaction_minute_label(row)
        if not time_label:
            continue
        groups.setdefault(time_label, [])
        groups[time_label].append(dict(row))
    result: list[dict[str, Any]] = []
    for time_label in sorted(groups, key=_time_key):
        rows = groups[time_label]
        rng.shuffle(rows)
        result.extend(rows)
    return result


def counterfactual_context(
    rows: Sequence[Mapping[str, Any]],
    *,
    shift: int = 1,
) -> list[dict[str, Any]]:
    """Shift context timing for a lead/lag control without changing values."""

    if not rows:
        return []
    amount = max(0, int(shift))
    result: list[dict[str, Any]] = []
    for index in range(len(rows)):
        source = rows[max(0, index - amount)]
        result.append(dict(source))
    return result


def ablate_features(features: Sequence[Mapping[str, Any]], name: str) -> list[dict[str, Any]]:
    """Build named controls used by the protocol report."""

    result = [dict(item) for item in features]
    if name in {"minute_price_only", "direction_only"}:
        for item in result:
            for key in list(item):
                if name == "minute_price_only" and (
                    "flow" in key
                    or "transaction" in key
                    or "direction" in key
                    or key in {
                        "absorption",
                        "response_improving",
                        "buy_amount",
                        "sell_amount",
                        "neutral_amount",
                        "buy_count",
                        "sell_count",
                        "neutral_count",
                        "special_neutral_count",
                        "size_proxy_imbalance",
                    }
                ):
                    item[key] = 0.0 if isinstance(item[key], (int, float)) else False
                if name == "direction_only" and key in {
                    "price_response",
                    "price_efficiency",
                    "response_efficiency",
                    "extension_pct",
                    "slope_1",
                    "slope_3",
                    "price_acceleration",
                    "breakout",
                    "first_retest",
                    "exhaustion",
                }:
                    item[key] = 0.0
    elif name in {"context_ablation", "matched_regime_control"}:
        for item in result:
            for key in list(item):
                if key.startswith("market_") or key.startswith("sector_") or key in {"market_improving", "sector_improving"}:
                    item[key] = 0.0 if isinstance(item[key], (int, float)) else False
    elif name in {"formula_ablation", "opening_prior_ablation"}:
        for item in result:
            for key in list(item):
                if name == "formula_ablation" and key.startswith("formula_"):
                    item[key] = 0.0 if isinstance(item[key], (int, float)) else False
                if name == "opening_prior_ablation" and key.startswith("auction_"):
                    item[key] = 0.0 if isinstance(item[key], (int, float)) else False
    return result


def pareto_frontier(
    rows: Sequence[Mapping[str, Any]],
    *,
    maximize: Sequence[str] = ("mean_net_r", "target_first_probability_pct", "mae_p90_pct"),
    minimize: Sequence[str] = ("no_fill_count",),
) -> list[dict[str, Any]]:
    """Return non-dominated rows; less-negative MAE is better than deep MAE."""

    def value(row: Mapping[str, Any], key: str) -> float:
        raw = row.get(key)
        if raw is None:
            return -math.inf if key in maximize else math.inf
        number = _float(raw)
        return number

    frontier: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        dominated = False
        for other_index, other in enumerate(rows):
            if index == other_index:
                continue
            at_least_one_better = False
            no_worse = True
            for key in maximize:
                other_value, current_value = value(other, key), value(row, key)
                if other_value < current_value:
                    no_worse = False
                if other_value > current_value:
                    at_least_one_better = True
            for key in minimize:
                other_value, current_value = value(other, key), value(row, key)
                if other_value > current_value:
                    no_worse = False
                if other_value < current_value:
                    at_least_one_better = True
            if no_worse and at_least_one_better:
                dominated = True
                break
        if not dominated:
            frontier.append(dict(row))
    return frontier


def quantile_bins(values: Sequence[float], bins: int = 3) -> list[dict[str, Any]]:
    """Describe broad training-set quantile bins, useful for plateau checks."""

    clean = sorted(_float(value) for value in values if math.isfinite(_float(value)))
    if not clean:
        return []
    count = max(2, int(bins))
    result: list[dict[str, Any]] = []
    for index in range(count):
        lower_index = int(index * len(clean) / count)
        upper_index = max(lower_index, int((index + 1) * len(clean) / count) - 1)
        result.append(
            {
                "bin": index + 1,
                "lower": clean[lower_index],
                "upper": clean[upper_index],
                "count": upper_index - lower_index + 1,
            }
        )
    return result


def _candidate_outcome_pairs(
    candidates: Sequence[EventCandidate],
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
) -> list[tuple[EventCandidate, Mapping[str, Any] | TradeOutcome]]:
    primary = independent_outcomes(outcomes)

    def outcome_key(item: Mapping[str, Any] | TradeOutcome) -> tuple[str, ...]:
        if isinstance(item, TradeOutcome):
            return (
                item.trade_date,
                str(item.code).zfill(6),
                str(item.candidate_index),
                item.direction,
                item.setup,
                item.hypothesis_id,
            )
        return (
            str(item.get("trade_date") or ""),
            str(item.get("code") or "").zfill(6),
            str(item.get("candidate_index", item.get("candidate_time", ""))),
            str(item.get("direction") or "none"),
            str(item.get("setup") or ""),
            str(item.get("hypothesis_id") or ""),
        )

    lookup: defaultdict[tuple[str, ...], list[Mapping[str, Any] | TradeOutcome]] = defaultdict(list)
    for item in primary:
        lookup[outcome_key(item)].append(item)
    offsets: defaultdict[tuple[str, ...], int] = defaultdict(int)
    pairs: list[tuple[EventCandidate, Mapping[str, Any] | TradeOutcome]] = []
    for candidate in candidates:
        key = (
            candidate.trade_date,
            str(candidate.code).zfill(6),
            str(candidate.index),
            candidate.direction,
            candidate.setup,
            candidate.hypothesis_id,
        )
        offset = offsets[key]
        values = lookup.get(key, [])
        if offset < len(values):
            pairs.append((candidate, values[offset]))
            offsets[key] += 1
    return pairs


def feature_platform_analysis(
    candidates: Sequence[EventCandidate],
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
    *,
    feature_names: Sequence[str],
    config: ProtocolConfig | None = None,
) -> dict[str, Any]:
    """Measure broad adjacent feature bins on training data only."""

    protocol = config or ProtocolConfig()
    pairs = _candidate_outcome_pairs(candidates, outcomes)
    extra_cost = max(0.0, protocol.pessimistic_slippage_pct * 2.0)
    features: dict[str, Any] = {}
    stable_platforms: list[dict[str, Any]] = []
    for feature_name in feature_names:
        feature_pairs = [
            (candidate, outcome, _float(candidate.features.get(feature_name)))
            for candidate, outcome in pairs
            if feature_name in candidate.features
            and candidate.features.get(feature_name) is not None
        ]
        boundaries = quantile_bins(
            [value for _, _, value in feature_pairs],
            bins=protocol.quantile_bins,
        )
        grouped: list[list[Mapping[str, Any] | TradeOutcome]] = [
            [] for _ in boundaries
        ]
        for _candidate, outcome, value in feature_pairs:
            for index, boundary in enumerate(boundaries):
                if value <= _float(boundary.get("upper")) or index == len(boundaries) - 1:
                    grouped[index].append(outcome)
                    break
        bin_rows: list[dict[str, Any]] = []
        for index, (boundary, items) in enumerate(zip(boundaries, grouped)):
            base = summarize_outcomes(items, bootstrap_iterations=0)
            pessimistic = summarize_outcomes(
                outcomes_with_extra_friction(items, extra_cost_pct=extra_cost),
                bootstrap_iterations=0,
            )
            bin_rows.append(
                {
                    **boundary,
                    "bin": index + 1,
                    "independent_event_count": len(items),
                    "base": base,
                    "pessimistic": pessimistic,
                    "date_stability": _date_stability(items),
                }
            )
        adjacent: list[dict[str, Any]] = []
        for left, right in zip(bin_rows, bin_rows[1:]):
            minimum_bin_events = max(5, min(30, int(protocol.minimum_events)))
            stable = bool(
                int(left["base"].get("filled_count") or 0) >= minimum_bin_events
                and int(right["base"].get("filled_count") or 0) >= minimum_bin_events
                and _float(left["base"].get("mean_net_r")) > 0
                and _float(right["base"].get("mean_net_r")) > 0
                and _float(left["pessimistic"].get("mean_net_r")) > 0
                and _float(right["pessimistic"].get("mean_net_r")) > 0
            )
            item = {
                "feature": feature_name,
                "bins": [left["bin"], right["bin"]],
                "lower": left["lower"],
                "upper": right["upper"],
                "stable_positive": stable,
                "selected": False,
                "reason": (
                    "相邻宽区间在基础与额外悲观摩擦下均为正"
                    if stable
                    else "未形成相邻宽区间的稳定正净R平台"
                ),
            }
            adjacent.append(item)
            if stable:
                stable_platforms.append(item)
        features[feature_name] = {
            "bins": bin_rows,
            "adjacent_pairs": adjacent,
        }
    return {
        "feature_count": len(features),
        "paired_independent_event_count": len(pairs),
        "features": features,
        "stable_positive_platforms": stable_platforms,
        "selection_applied": False,
    }


def evaluate_feature_platforms(
    candidates: Sequence[EventCandidate],
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
    platforms: Sequence[Mapping[str, Any]],
    *,
    config: ProtocolConfig | None = None,
) -> list[dict[str, Any]]:
    """Evaluate train-discovered broad platforms on later dates unchanged."""

    protocol = config or ProtocolConfig()
    pairs = _candidate_outcome_pairs(candidates, outcomes)
    extra_cost = max(0.0, protocol.pessimistic_slippage_pct * 2.0)
    result: list[dict[str, Any]] = []
    for platform in platforms:
        feature_name = str(platform.get("feature") or "")
        lower = _float(platform.get("lower"), -math.inf)
        upper = _float(platform.get("upper"), math.inf)
        items = [
            outcome
            for candidate, outcome in pairs
            if feature_name in candidate.features
            and lower <= _float(candidate.features.get(feature_name)) <= upper
        ]
        result.append(
            {
                **dict(platform),
                "oos_independent_event_count": len(items),
                "base": summarize_outcomes(
                    items,
                    bootstrap_iterations=protocol.bootstrap_iterations,
                    seed=protocol.seed + len(result) + 211,
                ),
                "pessimistic": summarize_outcomes(
                    outcomes_with_extra_friction(items, extra_cost_pct=extra_cost),
                    bootstrap_iterations=protocol.bootstrap_iterations,
                    seed=protocol.seed + len(result) + 307,
                ),
                "date_stability": _date_stability(items),
                "selected": False,
            }
        )
    return result


def validation_status(
    *,
    dates: Sequence[str],
    outcomes: Sequence[Mapping[str, Any] | TradeOutcome],
    out_of_sample_outcomes: Sequence[Mapping[str, Any] | TradeOutcome] = (),
    out_of_sample_dates: Sequence[str] = (),
    walk_forward: Mapping[str, Any] | None = None,
    config: ProtocolConfig | None = None,
) -> dict[str, Any]:
    protocol = config or ProtocolConfig()
    date_count = len({str(date) for date in dates if date})
    primary_outcomes = independent_outcomes(outcomes)
    filled = [
        item
        for item in primary_outcomes
        if str(
            getattr(
                item,
                "fill_status",
                item.get("fill_status") if isinstance(item, Mapping) else "",
            )
            or ""
        )
        == "filled"
    ]
    oos = list(independent_outcomes(out_of_sample_outcomes))
    inferred_oos_dates = {
        str(getattr(item, "trade_date", item.get("trade_date") if isinstance(item, Mapping) else "") or "")
        for item in oos
    }
    oos_dates = {
        str(value) for value in (out_of_sample_dates or tuple(inferred_oos_dates)) if value
    }
    oos_filled = [
        item
        for item in oos
        if str(
            getattr(
                item,
                "fill_status",
                item.get("fill_status") if isinstance(item, Mapping) else "",
            )
            or ""
        )
        == "filled"
    ]
    reasons: list[str] = []
    status = "research_only"
    wf = dict(walk_forward or {})
    base_metrics = (
        dict(wf.get("base") or {})
        if wf.get("available")
        else summarize_outcomes(oos, bootstrap_iterations=0)
    )
    pessimistic_metrics = (
        dict(wf.get("pessimistic") or {})
        if wf.get("available")
        else summarize_outcomes(
            outcomes_with_extra_friction(
                oos,
                extra_cost_pct=max(0.0, protocol.pessimistic_slippage_pct * 2.0),
            ),
            bootstrap_iterations=0,
        )
    )
    if date_count < protocol.minimum_days or len(filled) < protocol.minimum_events:
        status = "sample_insufficient"
        reasons.append(
            f"当前{date_count}个交易日/{len(filled)}个独立可成交事件，"
            f"低于{protocol.minimum_days}日/{protocol.minimum_events}事件门槛"
        )
    elif date_count < protocol.out_of_sample_days:
        status = "sample_insufficient"
        reasons.append(
            f"当前连续研究范围{date_count}日，尚未达到{protocol.out_of_sample_days}日滚动样本外验证门槛"
        )
    elif wf and not bool(wf.get("complete")):
        status = "out_of_sample_pending"
        reasons.append("滚动样本外折叠不完整，不能用一次性留出集替代")
    elif not oos or not oos_dates:
        status = "out_of_sample_pending"
        reasons.append("尚未提供独立样本外事件")
    elif len(oos_filled) < protocol.minimum_events:
        status = "out_of_sample_pending"
        reasons.append(
            f"样本外仅{len(oos_filled)}个可成交事件，低于{protocol.minimum_events}事件门槛"
        )
    else:
        base_mean = _float(base_metrics.get("mean_net_r"))
        pessimistic_mean = _float(pessimistic_metrics.get("mean_net_r"))
        if base_mean > 0 and pessimistic_mean > 0:
            status = "candidate"
            reasons.append(
                "滚动样本外基础与额外悲观摩擦下净R均为正；"
                "candidate仍不可执行，且需检查日期贡献和相邻分位平台"
            )
        else:
            status = "research_only"
            reasons.append("滚动样本外基础或额外悲观摩擦净R未保持为正，保留研究模式")
    return {
        "status": status,
        "validation_status": status,
        "date_count": date_count,
        "raw_label_count": len(outcomes),
        "independent_event_count": len(primary_outcomes),
        "filled_event_count": len(filled),
        "oos_event_count": len(oos),
        "oos_filled_event_count": len(oos_filled),
        "oos_date_count": len(oos_dates),
        "minimum_days": protocol.minimum_days,
        "minimum_validation_days": protocol.out_of_sample_days,
        "minimum_events": protocol.minimum_events,
        "base_oos_metrics": base_metrics,
        "pessimistic_oos_metrics": pessimistic_metrics,
        "walk_forward_complete": bool(wf.get("complete")) if wf else False,
        "deployable": False,
        "reasons": reasons,
    }


def protocol_study(
    samples: Sequence[ResearchSample],
    *,
    config: ProtocolConfig | None = None,
) -> dict[str, Any]:
    """Run the finite first-pass protocol over supplied samples.

    The returned report deliberately includes observed and control variants,
    data manifests, and all labels (including ``no_fill``).  It is valid for a
    two-day exploration run but will state that it is insufficient for use.
    """

    protocol = config or ProtocolConfig()
    candidates: list[EventCandidate] = []
    labels: list[TradeLabel] = []
    manifests: list[DataManifest] = []
    daily_regimes: list[dict[str, Any]] = []
    by_variant: dict[str, list[TradeOutcome]] = {
        "observed": [],
        "baseline_four_factor": [],
        "shuffle_same_minute_transactions": [],
        "shuffle_core_stock_timing": [],
        "context_ablation": [],
        "minute_price_only": [],
        "direction_only": [],
        "formula_ablation": [],
        "opening_prior_ablation": [],
        "matched_same_sector_time": [],
    }
    regenerated_by_variant: dict[str, list[TradeOutcome]] = {
        name: [] for name in by_variant
    }
    variant_notes: dict[str, str] = {
        "observed": "候选状态转移，保留先手、确认和失败事件",
        "baseline_four_factor": "预先登记的四因子共振对照，不是部署门槛",
        "shuffle_same_minute_transactions": "保留每分钟成交集合，打乱原始成交顺序",
        "shuffle_core_stock_timing": "将市场/板块上下文延后一根，检验先后关系增量",
        "context_ablation": "移除市场与板块上下文，保留个股路径",
        "minute_price_only": "移除逐笔/方向相关特征，只保留分钟量价代理",
        "direction_only": "移除价格响应特征，观察单独成交方向的结果",
        "formula_ablation": "移除公式启发特征，只保留其他已登记观察变量",
        "opening_prior_ablation": "移除集合竞价先验，检验其是否有增量信息",
        "matched_same_sector_time": "同交易日、同板块、同时间位置的跨股票匹配控制",
    }

    # Keep the feature cache point-in-time and reuse it across the named
    # variants.  Recomputing it for every control is both slow and makes an
    # audit of the exact input less transparent.
    sample_by_key: dict[tuple[str, str], ResearchSample] = {}
    feature_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    candidate_by_key: dict[tuple[str, str], list[EventCandidate]] = {}
    matched_control_requests: list[tuple[ResearchSample, EventCandidate, ResearchSample, list[dict[str, Any]]]] = []
    matched_unavailable: defaultdict[str, int] = defaultdict(int)
    variant_candidate_diagnostics: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {
            "reference_event_count": 0,
            "same_event_count": 0,
            "regenerated_event_count": 0,
            "regenerated_time_overlap_count": 0,
            "missing_reference_feature_count": 0,
        }
    )

    def run_variant(
        sample: ResearchSample,
        name: str,
        variant_features: Sequence[Mapping[str, Any]],
        source_quality: str,
        *,
        reference_candidates: Sequence[EventCandidate] | None = None,
    ) -> tuple[list[EventCandidate], list[TradeLabel], list[TradeOutcome]]:
        regenerated_candidates = generate_candidates(
            sample,
            features=variant_features,
            config=protocol,
        )
        if reference_candidates is None:
            variant_candidates = regenerated_candidates
            reference_count = len(regenerated_candidates)
            missing_reference_count = 0
        else:
            variant_candidates = []
            missing_reference_count = 0
            for candidate in reference_candidates:
                if candidate.index < 0 or candidate.index >= len(variant_features):
                    missing_reference_count += 1
                    continue
                variant_candidates.append(
                    EventCandidate(
                        code=candidate.code,
                        name=candidate.name,
                        trade_date=candidate.trade_date,
                        index=candidate.index,
                        time=candidate.time,
                        direction=candidate.direction,
                        setup=candidate.setup,
                        hypothesis_id=candidate.hypothesis_id,
                        features=dict(variant_features[candidate.index]),
                        evidence_sequence=candidate.evidence_sequence,
                        source_quality=source_quality,
                        validation_status="research_only",
                        executable=False,
                    )
                )
            reference_count = len(reference_candidates)
        regenerated_times = {
            (item.index, item.direction) for item in regenerated_candidates
        }
        diagnostics = variant_candidate_diagnostics[name]
        diagnostics["reference_event_count"] += reference_count
        diagnostics["same_event_count"] += len(variant_candidates)
        diagnostics["regenerated_event_count"] += len(regenerated_candidates)
        diagnostics["regenerated_time_overlap_count"] += sum(
            (item.index, item.direction) in regenerated_times
            for item in variant_candidates
        )
        diagnostics["missing_reference_feature_count"] += missing_reference_count
        variant_labels = label_candidates(sample, variant_candidates, features=variant_features, config=protocol)
        variant_outcomes = outcomes_from_labels(
            variant_candidates,
            variant_labels,
            source_quality=source_quality,
        )
        by_variant.setdefault(name, []).extend(variant_outcomes)
        if reference_candidates is None:
            regenerated_outcomes = variant_outcomes
        else:
            regenerated_labels = label_candidates(
                sample,
                regenerated_candidates,
                features=variant_features,
                config=protocol,
            )
            regenerated_outcomes = outcomes_from_labels(
                regenerated_candidates,
                regenerated_labels,
                source_quality=f"{source_quality}_regenerated",
            )
        regenerated_by_variant.setdefault(name, []).extend(regenerated_outcomes)
        return variant_candidates, variant_labels, variant_outcomes

    for sample_index, sample in enumerate(samples):
        features = extract_point_features(
            sample.bars,
            sample.transactions,
            market_bars=sample.market_bars,
            sector_bars=sample.sector_bars,
            prev_close=_float(sample.metadata.get("prev_close")),
            auction_prior=sample.auction_prior,
            daily_regime=sample.metadata.get("daily_regime"),
        )
        sample_candidates, sample_labels, observed = run_variant(
            sample,
            "observed",
            features,
            sample.source_quality,
        )
        candidates.extend(sample_candidates)
        labels.extend(sample_labels)
        manifests.append(build_data_manifest(sample))
        sample_key = sample.key()
        sample_by_key[sample_key] = sample
        feature_by_key[sample_key] = [dict(item) for item in features]
        candidate_by_key[sample_key] = list(sample_candidates)

        # The regime is an observation attached to the day, not a label derived
        # from the day's close.  It is useful for stratification and remains
        # point-in-time relative to the last available bar.
        last = features[-1] if features else {}
        market_regime = (
            "improving" if _float(last.get("market_slope")) > 0
            else "weak" if _float(last.get("market_slope")) < 0
            else "mixed"
        )
        sector_regime = (
            "improving" if bool(last.get("sector_improving"))
            else "weak" if _float(last.get("sector_slope")) < 0
            else "mixed"
        )
        daily_regimes.append(
            {
                "trade_date": sample.trade_date,
                "code": sample.code,
                "sector_name": sample.sector_name,
                "regime": f"{market_regime}/{sector_regime}",
                "market_regime": market_regime,
                "sector_regime": sector_regime,
                "daily_regime": str(
                    (sample.metadata.get("daily_regime") or {}).get("regime") or "unknown"
                ),
                "daily_regime_status": str(
                    (sample.metadata.get("daily_regime") or {}).get("status")
                    or "insufficient_history"
                ),
                "daily_observations": int(
                    (sample.metadata.get("daily_regime") or {}).get("observations") or 0
                ),
                "daily_ma20_distance_pct": (sample.metadata.get("daily_regime") or {}).get(
                    "ma20_distance_pct"
                ),
                "daily_macd": (sample.metadata.get("daily_regime") or {}).get("macd"),
                "daily_macd_signal": (sample.metadata.get("daily_regime") or {}).get("macd_signal"),
                "daily_adx": (sample.metadata.get("daily_regime") or {}).get("adx"),
                "daily_bollinger_width_pct": (sample.metadata.get("daily_regime") or {}).get(
                    "bollinger_width_pct"
                ),
                "daily_price_basis": str(
                    (sample.metadata.get("daily_regime") or {}).get("price_basis") or "unavailable"
                ),
                "source_quality": sample.source_quality,
                "strategy_version": protocol.protocol_version,
            }
        )

        baseline_candidates = [item for item in sample_candidates if _is_four_factor_baseline(item)]
        if baseline_candidates:
            baseline_labels = label_candidates(sample, baseline_candidates, features=features, config=protocol)
            baseline_outcomes = outcomes_from_labels(
                baseline_candidates,
                baseline_labels,
                source_quality="baseline_four_factor",
            )
            by_variant["baseline_four_factor"].extend(baseline_outcomes)
            regenerated_by_variant["baseline_four_factor"].extend(baseline_outcomes)

        if sample.transactions:
            shuffled = counterfactual_transactions(sample.transactions, seed=protocol.seed + sample_index)
            shuffled_features = extract_point_features(
                sample.bars,
                shuffled,
                market_bars=sample.market_bars,
                sector_bars=sample.sector_bars,
                prev_close=_float(sample.metadata.get("prev_close")),
                auction_prior=sample.auction_prior,
                daily_regime=sample.metadata.get("daily_regime"),
            )
            run_variant(
                sample,
                "shuffle_same_minute_transactions",
                shuffled_features,
                "counterfactual_same_minute_order",
                reference_candidates=sample_candidates,
            )

        shifted_market = counterfactual_context(sample.market_bars, shift=1)
        shifted_sector = [counterfactual_context(rows, shift=1) for rows in sample.sector_bars]
        shifted_features = extract_point_features(
            sample.bars,
            sample.transactions,
            market_bars=shifted_market,
            sector_bars=shifted_sector,
            prev_close=_float(sample.metadata.get("prev_close")),
            auction_prior=sample.auction_prior,
            daily_regime=sample.metadata.get("daily_regime"),
        )
        run_variant(
            sample,
            "shuffle_core_stock_timing",
            shifted_features,
            "counterfactual_context_shift",
            reference_candidates=sample_candidates,
        )

        # Named ablations make it possible to tell whether direction, context,
        # or ordinary minute momentum explains an observed result.
        run_variant(
            sample,
            "context_ablation",
            ablate_features(features, "context_ablation"),
            "counterfactual_context_ablation",
            reference_candidates=sample_candidates,
        )
        run_variant(
            sample,
            "minute_price_only",
            ablate_features(features, "minute_price_only"),
            "counterfactual_minute_price_only",
            reference_candidates=sample_candidates,
        )
        run_variant(
            sample,
            "direction_only",
            ablate_features(features, "direction_only"),
            "counterfactual_direction_only",
            reference_candidates=sample_candidates,
        )
        run_variant(
            sample,
            "formula_ablation",
            ablate_features(features, "formula_ablation"),
            "counterfactual_formula_ablation",
            reference_candidates=sample_candidates,
        )
        run_variant(
            sample,
            "opening_prior_ablation",
            ablate_features(features, "opening_prior_ablation"),
            "counterfactual_opening_prior_ablation",
            reference_candidates=sample_candidates,
        )

    # Build the matched control only after every sample's point-in-time
    # feature stream is available.  The peer is selected by date/sector/code
    # and event position, never by its later return, so the control remains an
    # ex-ante comparison.  A resource cap is deterministic and reported below.
    peers_by_date_sector: defaultdict[tuple[str, str], list[ResearchSample]] = defaultdict(list)
    for sample in samples:
        sector_name = str(sample.sector_name or "").strip()
        if sector_name:
            peers_by_date_sector[(str(sample.trade_date), sector_name)].append(sample)
    for peer_group in peers_by_date_sector.values():
        peer_group.sort(key=lambda item: str(item.code).zfill(6))

    for sample in samples:
        source_key = sample.key()
        source_candidates = candidate_by_key.get(source_key, [])
        sector_name = str(sample.sector_name or "").strip()
        if not sector_name:
            matched_unavailable["缺少板块标识"] += len(source_candidates)
            continue
        peers = [
            item
            for item in peers_by_date_sector.get((str(sample.trade_date), sector_name), [])
            if item.key() != source_key
        ]
        if not peers:
            matched_unavailable["同日同板块没有其他样本"] += len(source_candidates)
            continue
        for candidate in source_candidates:
            valid_peers = [
                peer
                for peer in peers
                if candidate.index < len(feature_by_key.get(peer.key(), []))
            ]
            if not valid_peers:
                matched_unavailable["匹配股票缺少同时间分钟"] += 1
                continue
            # Deterministic rotation prevents the first code in a sector from
            # becoming the control for every event while remaining independent
            # of target-day outcomes.
            code_seed = sum(ord(char) for char in str(sample.code))
            peer = valid_peers[(code_seed + candidate.index) % len(valid_peers)]
            matched_control_requests.append(
                (
                    sample,
                    candidate,
                    peer,
                    feature_by_key[peer.key()],
                )
            )

    requested_control_count = len(matched_control_requests)
    control_cap = max(0, int(protocol.max_matched_control_events))
    if control_cap and requested_control_count > control_cap:
        if control_cap == 1:
            selected_control_requests = [matched_control_requests[0]]
        else:
            selected_control_requests = [
                matched_control_requests[
                    round(index * (requested_control_count - 1) / (control_cap - 1))
                ]
                for index in range(control_cap)
            ]
    else:
        selected_control_requests = matched_control_requests

    matched_control_records: list[dict[str, Any]] = []
    for source_sample, source_candidate, peer, peer_features in selected_control_requests:
        index = source_candidate.index
        if index >= len(peer_features):
            matched_unavailable["匹配股票缺少同时间分钟"] += 1
            continue
        control_feature = dict(peer_features[index])
        match_key = "|".join(
            [
                str(source_sample.trade_date),
                str(source_sample.sector_name),
                str(source_candidate.time),
                str(source_sample.code).zfill(6),
                str(peer.code).zfill(6),
                str(source_candidate.direction),
                str(source_candidate.setup),
                str(index),
            ]
        )
        control_feature.update(
            {
                "matched_control": True,
                "match_key": match_key,
                "matched_source_code": str(source_sample.code).zfill(6),
                "matched_source_setup": source_candidate.setup,
            }
        )
        control_candidate = EventCandidate(
            code=str(peer.code).zfill(6),
            name=peer.name,
            trade_date=peer.trade_date,
            index=index,
            time=str(control_feature.get("time") or source_candidate.time),
            direction=source_candidate.direction,
            setup=f"同板块同时间控制/{source_candidate.setup}",
            hypothesis_id=source_candidate.hypothesis_id,
            features=control_feature,
            evidence_sequence=("同板块同时间匹配控制",),
            source_quality="matched_same_sector_time",
            validation_status="research_only",
            executable=False,
        )
        control_labels = label_candidates(
            peer,
            [control_candidate],
            features=peer_features,
            config=protocol,
        )
        control_outcomes = outcomes_from_labels(
            [control_candidate],
            control_labels,
            source_quality="matched_same_sector_time",
        )
        by_variant["matched_same_sector_time"].extend(control_outcomes)
        regenerated_by_variant["matched_same_sector_time"].extend(control_outcomes)
        matched_control_records.append(
            {
                "match_key": match_key,
                "trade_date": source_sample.trade_date,
                "sector_name": source_sample.sector_name,
                "source_code": str(source_sample.code).zfill(6),
                "control_code": str(peer.code).zfill(6),
                "time": str(control_feature.get("time") or source_candidate.time),
                "direction": source_candidate.direction,
                "setup": source_candidate.setup,
            }
        )

    observed = by_variant["observed"]
    generated_at = datetime.now().isoformat(timespec="seconds")
    # The first screening window is training-only.  Every later date is used
    # exactly once by the expanding-window walk-forward evaluator below.
    dates = sorted({sample.trade_date for sample in samples if sample.trade_date})
    split_at = min(len(dates), max(1, int(protocol.minimum_days)))
    train_dates = set(dates[:split_at])
    oos_dates = set(dates[split_at:])
    train = [item for item in observed if item.trade_date in train_dates]
    oos = [item for item in observed if item.trade_date in oos_dates]
    train_candidates = [item for item in candidates if item.trade_date in train_dates]
    oos_candidates = [item for item in candidates if item.trade_date in oos_dates]
    walk_forward = walk_forward_evaluation(
        dates=dates,
        outcomes=observed,
        config=protocol,
    )
    validation = validation_status(
        dates=dates,
        outcomes=observed,
        out_of_sample_outcomes=oos,
        out_of_sample_dates=sorted(oos_dates),
        walk_forward=walk_forward,
        config=protocol,
    )
    direction_validation: dict[str, Any] = {}
    for direction in ("positive_t", "reverse_t"):
        direction_all = [item for item in observed if item.direction == direction]
        direction_oos = [item for item in oos if item.direction == direction]
        direction_walk_forward = walk_forward_evaluation(
            dates=dates,
            outcomes=direction_all,
            config=protocol,
        )
        direction_validation[direction] = validation_status(
            dates=dates,
            outcomes=direction_all,
            out_of_sample_outcomes=direction_oos,
            out_of_sample_dates=sorted(oos_dates),
            walk_forward=direction_walk_forward,
            config=protocol,
        )

    variants: dict[str, Any] = {}
    for index, (name, items) in enumerate(by_variant.items()):
        primary_items = independent_outcomes(items)
        primary_metrics = summarize_outcomes(
            primary_items,
            bootstrap_iterations=protocol.bootstrap_iterations,
            seed=protocol.seed + index,
        )
        regenerated_items = regenerated_by_variant.get(name, [])
        regenerated_primary = independent_outcomes(regenerated_items)
        regenerated_metrics = (
            primary_metrics
            if name in {"observed", "baseline_four_factor", "matched_same_sector_time"}
            else summarize_outcomes(
                regenerated_primary,
                bootstrap_iterations=protocol.bootstrap_iterations,
                seed=protocol.seed + index + 101,
            )
        )
        variants[name] = {
            "outcomes": len(items),
            "independent_outcomes": len(primary_items),
            "metrics": primary_metrics,
            "same_event": {
                "outcomes": len(items),
                "independent_outcomes": len(primary_items),
                "metrics": primary_metrics,
                "estimand": "固定观察组候选时点，只替换当时可见特征",
            },
            "regenerated": {
                "outcomes": len(regenerated_items),
                "independent_outcomes": len(regenerated_primary),
                "metrics": regenerated_metrics,
                "estimand": "删除或扰动因子后，从头重建候选集合",
            },
            "note": variant_notes.get(name, ""),
            "counterfactual": name not in {"observed", "baseline_four_factor"},
            "candidate_comparison": dict(variant_candidate_diagnostics.get(name, {})),
            "comparison_basis": (
                "same_observed_event_times"
                if name not in {"observed", "baseline_four_factor", "matched_same_sector_time"}
                else "native_candidates"
            ),
            "available_estimands": (
                ["same_observed_event_times", "regenerated_candidate_set"]
                if name not in {"observed", "baseline_four_factor", "matched_same_sector_time"}
                else ["native_candidates"]
            ),
        }

    continuous_features = (
        "direction_imbalance",
        "flow_order_shift",
        "transaction_price_response_pct",
        "size_proxy_imbalance",
        "price_efficiency",
        "vwap_distance_pct",
        "extension_pct",
        "amount_ratio",
        "sector_slope",
        "market_slope",
        "formula_trend_score",
        "formula_support_score",
        "formula_exhaustion_score",
        "formula_position_pct",
        "formula_bollinger_position",
        "daily_ma20_distance_pct",
        "daily_adx",
    )
    training_platforms = feature_platform_analysis(
        train_candidates,
        train,
        feature_names=continuous_features,
        config=protocol,
    )
    oos_platforms = evaluate_feature_platforms(
        oos_candidates,
        oos,
        training_platforms.get("stable_positive_platforms", []),
        config=protocol,
    )
    parameter_discovery = {
        "method": "训练集连续特征分位数分箱；只寻找相邻区间的稳定平台，不选择单点峰值",
        "continuous_features": list(continuous_features),
        "quantile_bins": {
            feature: quantile_bins(
                [_float(candidate.features.get(feature)) for candidate in train_candidates],
                bins=protocol.quantile_bins,
            )
            for feature in continuous_features
        },
        "tested_ranges": ["低分位", "中分位", "高分位"],
        "plateau_rule": "相邻分位区间方向一致且悲观净R不被单日贡献主导，才进入候选规则",
        "feature_performance": training_platforms,
        "oos_platform_evaluation": oos_platforms,
        "status": "exploratory_only" if not walk_forward.get("complete") else "oos_review_ready",
        "training_dates": sorted(train_dates),
        "oos_dates_not_used_for_bins": sorted(oos_dates),
    }
    daily_status_counts: defaultdict[str, int] = defaultdict(int)
    for sample in samples:
        daily_status_counts[
            str((sample.metadata.get("daily_regime") or {}).get("status") or "insufficient_history")
        ] += 1
    direction_totals: defaultdict[str, int] = defaultdict(int)
    for manifest in manifests:
        for name, value in manifest.direction_counts.items():
            direction_totals[name] += int(value)
    data_quality = {
        "sample_count": len(samples),
        "minute_coverage_mean": round(
            statistics.mean([item.minute_coverage for item in manifests]), 6
        )
        if manifests
        else 0.0,
        "transaction_coverage_mean": round(
            statistics.mean([item.transaction_coverage for item in manifests]), 6
        )
        if manifests
        else 0.0,
        "transaction_minute_coverage_mean": round(
            statistics.mean(
                [
                    item.transaction_minute_count
                    / max(item.transaction_expected_minute_count, 1)
                    for item in manifests
                    if item.transaction_count
                ]
            ),
            6,
        )
        if any(item.transaction_count for item in manifests)
        else 0.0,
        "transaction_metadata_coverage_mean": round(
            statistics.mean(
                [
                    item.transaction_metadata_coverage
                    for item in manifests
                    if item.transaction_count
                ]
            ),
            6,
        )
        if any(item.transaction_count for item in manifests)
        else 0.0,
        "time_gap_sample_count": sum(1 for item in manifests if item.time_gaps),
        "transaction_gap_sample_count": sum(
            1 for item in manifests if item.transaction_time_gaps
        ),
        "transaction_page_metadata_sample_count": sum(
            1 for item in manifests if item.transaction_page_count > 0
        ),
        "transaction_sequence_ordered_sample_count": sum(
            1 for item in manifests if item.transaction_sequence_ordered
        ),
        "direction_counts": dict(sorted(direction_totals.items())),
        "daily_regime_status_counts": dict(sorted(daily_status_counts.items())),
        "auction_available_count": sum(1 for item in manifests if item.auction_available),
        "level2_available": False,
        "transaction_source": "get_history_transaction_data" if any(item.transaction_count for item in manifests) else "unavailable",
        "status": (
            "usable_with_limitations"
            if manifests and not any(item.time_gaps for item in manifests)
            else "quality_review_required"
        ),
    }
    matched_control_summary = {
        "requested_event_count": requested_control_count,
        "selected_event_count": len(selected_control_requests),
        "outcome_count": len(by_variant["matched_same_sector_time"]),
        "matched_record_count": len(matched_control_records),
        "unavailable_event_count": sum(matched_unavailable.values()),
        "unavailable_reasons": dict(sorted(matched_unavailable.items())),
        "cap": control_cap or None,
        "truncated": bool(control_cap and requested_control_count > control_cap),
        "selection_method": "按候选事件顺序等距抽样控制资源上限，不查看目标结果",
        "records": matched_control_records[:1000],
    }
    return {
        "protocol_version": protocol.protocol_version,
        "study_name": protocol.study_name,
        "run_id": hashlib.sha1(f"{protocol.protocol_version}|{generated_at}|{','.join(dates)}".encode()).hexdigest()[:16],
        "generated_at": generated_at,
        "hypotheses": registered_hypotheses(),
        "config": asdict(protocol),
        "sample": {
            "sample_count": len(samples),
            "stock_day_count": len({sample.key() for sample in samples}),
            "date_count": len(dates),
            "dates": dates,
            "one_word_count": sum(1 for sample in samples if sample.one_word),
            "no_fill_retained": True,
            "auction_available_count": sum(1 for sample in samples if sample.auction_prior.get("available")),
            "transaction_sample_count": sum(1 for sample in samples if sample.transactions),
            "daily_regime_available_count": sum(
                1
                for sample in samples
                if bool((sample.metadata.get("daily_regime") or {}).get("available"))
            ),
        },
        "data_boundary": {
            "trade_dates": dates,
            "selection_date_rule": "strictly_before_each_trade_date",
            "feature_cutoff": "candidate minute inclusive",
            "entry_time_rule": "next executable minute",
            "market_data_source": data_quality["transaction_source"],
            "flow_label": "L1_transaction_tape" if any(item.transaction_count for item in manifests) else "minute_price_amount_proxy",
            "level2_available": False,
            "target_day_exclusion_policy": "one_word_and_unfillable_retained_as_no_fill",
        },
        "leakage_checks": {
            "features_use_visible_prefix_only": True,
            "entry_uses_next_executable_bar": True,
            "daily_regime_uses_prior_dates_only": True,
            "parameter_bins_use_training_dates_only": True,
            "oos_dates_used_for_bins": False,
            "target_day_full_ohlc_used_for_sample_selection": False,
            "multi_horizon_labels_counted_as_independent_events": False,
        },
        "execution_model": {
            "entry_fill": "next minute with adverse slippage",
            "same_bar_stop_target": "adverse path first",
            "friction_pct": protocol.friction_pct,
            "slippage_pct_per_side": protocol.pessimistic_slippage_pct,
            "base_round_trip_cost_pct": round(
                protocol.friction_pct + protocol.pessimistic_slippage_pct * 2.0,
                6,
            ),
            "extra_pessimistic_round_trip_cost_pct": round(
                protocol.pessimistic_slippage_pct * 2.0,
                6,
            ),
            "limit_locked_policy": "no_fill",
            "t_plus_one_enforced_for_executable_reverse_t": True,
        },
        "data_quality": data_quality,
        "data_manifests": [item.to_dict() for item in manifests],
        "daily_regimes": daily_regimes,
        "candidates": [item.to_dict() for item in candidates],
        "labels": [item.to_dict() for item in labels],
        "outcomes": [item.to_dict() for item in observed],
        "split": {"train_dates": sorted(train_dates), "oos_dates": sorted(oos_dates)},
        "walk_forward": walk_forward,
        "variants": variants,
        "validation": {
            **validation,
            "direction": direction_validation,
            "train_label_count": len(train),
            "train_event_count": len(independent_outcomes(train)),
            "oos_date_count": len(oos_dates),
        },
        "parameter_discovery": parameter_discovery,
        "matched_control": matched_control_summary,
        "counterfactuals": {
            name: {
                "note": payload.get("note", ""),
                "outcome_count": payload.get("outcomes", 0),
                "metrics": payload.get("metrics", {}),
                "same_event": payload.get("same_event", {}),
                "regenerated": payload.get("regenerated", {}),
                "candidate_comparison": payload.get("candidate_comparison", {}),
                "comparison_basis": payload.get("comparison_basis", ""),
                "available_estimands": payload.get("available_estimands", []),
            }
            for name, payload in variants.items()
            if name != "observed"
        },
        "pareto": pareto_frontier(
            [
                {"variant": name, **payload["metrics"]}
                for name, payload in variants.items()
                if payload["metrics"].get("filled_count", 0) > 0
            ]
        ),
        "bias_register": [
            {
                "bias": "within_stock_day_dependence",
                "control": "bootstrap resamples complete code-date clusters",
                "status": "controlled_in_confidence_interval",
            },
            {
                "bias": "multi_horizon_pseudoreplication",
                "control": "longest pre-registered horizon is one primary event label",
                "status": "controlled",
            },
            {
                "bias": "future_universe_selection",
                "control": "each target date receives a prior-session ex-ante sample",
                "status": "caller_verified_by_selection_schedule",
            },
            {
                "bias": "transaction_direction_is_not_order_intent",
                "control": "combine L1 direction with price response and keep special values neutral",
                "status": "known_limitation",
            },
            {
                "bias": "sample_sector_is_not_full_market_sector",
                "control": "report sector context as sampled-peer proxy",
                "status": "known_limitation",
            },
        ],
        "limitations": [
            "当前输出是研究事件，不是自动交易信号",
            "easy_tdx/TDX L1没有历史队列数据；逐笔方向是L1成交代理",
            "分钟数据没有完整逐笔撮合顺序时，单根K线内先后按悲观路径处理",
            (
                "同板块同时间匹配控制受研究资源上限截取；需结合matched_control.truncated解释"
                if matched_control_summary["truncated"]
                else "若同日同板块样本不足，matched_same_sector_time会记录不可用原因，不能把空对照解释为无效"
            ),
            "未达到20日/60日样本外标准前不得固化阈值或部署",
        ],
    }


# Backwards/consumer-friendly aliases used by research notebooks and tests.
extract_features = extract_point_features
build_candidates = generate_candidates
label_event = label_candidate
clustered_bootstrap = clustered_bootstrap_ci
compute_statistics = summarize_outcomes
run_protocol = protocol_study


__all__ = [
    "FORMULA_FEATURE_KEYS",
    "DAILY_REGIME_FEATURE_KEYS",
    "HYPOTHESES",
    "HYPOTHESIS_REGISTRY",
    "HypothesisSpec",
    "ProtocolConfig",
    "ResearchSample",
    "EventCandidate",
    "TradeLabel",
    "TradeOutcome",
    "DataManifest",
    "registered_hypotheses",
    "get_hypothesis",
    "normalize_transaction_direction",
    "compute_daily_regime",
    "extract_point_features",
    "extract_features",
    "generate_candidates",
    "build_candidates",
    "label_candidate",
    "label_event",
    "label_candidates",
    "outcomes_from_labels",
    "build_data_manifest",
    "clustered_bootstrap_ci",
    "clustered_bootstrap",
    "summarize_outcomes",
    "compute_statistics",
    "summarize_by",
    "independent_outcomes",
    "outcomes_with_extra_friction",
    "walk_forward_evaluation",
    "feature_platform_analysis",
    "evaluate_feature_platforms",
    "counterfactual_transactions",
    "counterfactual_context",
    "ablate_features",
    "pareto_frontier",
    "quantile_bins",
    "validation_status",
    "protocol_study",
    "run_protocol",
]

