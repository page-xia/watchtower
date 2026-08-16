from __future__ import annotations

"""Opening-7-minute regime-stratified buy/sell markers.

Rule base: docs/opening7_research.md (event study on the pool universe,
2026-05-15..2026-08-11, 930 stock-days).  The engine is a pure function over
point-in-time inputs so the same code path works live and in historical replay:

- 09:31 (k=1): classify the index opening regime and run the sell gate
  (position holders only): gap-up >= 2% with L1 transaction-tape distribution
  (net buy ratio <= -10%) -> sell first.  The fade played out over the full
  day in the study (mean close -1.2~-1.7%).
- 09:33 (k=3): regime-stratified buy decision.  In the study the 09:33 call
  agreed with the 09:37 call on ~95% of stock-days, and waiting cost only
  ~0.1-0.2% on average, so 09:33 is the primary buy checkpoint.
- 09:35/09:37 stay review checkpoints in the terminal; this engine does not
  emit fresh entries there.

Everything here is a research signal (validation_status="research_only"),
never an auto-execution instruction.  Tick flow is the easy_tdx L1
transaction tape, minute-stamped, not order-queue data.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# Decision minutes (clock labels).  k=1 uses minute bar "09:30" plus tape
# stamped 09:25/09:30; k=3 uses bars 09:30..09:32 plus tape stamped <= 09:32.
SELL_CLOCK = "09:31"
BUY_CLOCK = "09:33"

REGIME_STRONG_LOW = "strong_low_open"   # idx <= -0.8%
REGIME_LOW = "low_open"                 # -0.8% < idx <= -0.3%
REGIME_FLAT = "flat_open"               # -0.3% < idx <= 0
REGIME_HIGH = "high_open"               # idx > 0

REGIME_LABELS = {
    REGIME_STRONG_LOW: "强低开",
    REGIME_LOW: "低开",
    REGIME_FLAT: "平稳开",
    REGIME_HIGH: "高开",
}

# Thresholds from the event study; deliberately few and transparent.
RULES: dict[str, float] = {
    "regime_strong_low_pct": -0.8,
    "regime_low_pct": -0.3,
    "sell_gate_gap_min_pct": 2.0,
    "sell_gate_net_max_pct": -10.0,
    "buy_low_open_net_min_pct": -10.0,   # low-open regime: tolerate mild selling
    "buy_flat_net_min_pct": 10.0,        # flat regime: demand real net buying
    "buy_chase_gap_max_pct": 5.0,
    "buy_chase_from_open_max_pct": 6.5,
    "low_open_fallback_from_open_min_pct": -1.5,  # no-tape fallback: price holding
}


@dataclass(frozen=True)
class OpeningMarker:
    time: str                 # chart minute label, e.g. "09:33"
    side: str                 # "buy" | "sell"
    price: float
    change_pct: float
    regime: str
    rule: str
    reasons: list[str] = field(default_factory=list)
    source_quality: str = "minute_proxy"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _minute_map(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in rows or []:
        label = str(row.get("time") or "")[:5]
        if len(label) == 5 and label[2] == ":":
            out[label] = {"price": _f(row.get("price")), "vol": _f(row.get("vol", row.get("volume")))}
    return out


def _bars(minute_rows: Sequence[Mapping[str, Any]], k: int) -> list[dict[str, float]]:
    """First k morning bars (09:30..09:30+k-1) that actually exist."""
    mm = _minute_map(minute_rows)
    return [mm[f"09:{30 + i:02d}"] for i in range(k) if f"09:{30 + i:02d}" in mm]


def _tape_stats(flow_points: Sequence[Any], upto_minute: str) -> dict[str, float] | None:
    """Cumulative L1 tape stats for prints stamped <= upto_minute (inclusive).

    ``flow_points`` are per-minute aggregates (time, buy_amount, sell_amount)
    derived from the easy_tdx L1 transaction tape.  Returns None when no
    opening tape exists at all.
    """
    buy = sell = 0.0
    seen = False
    for point in flow_points or []:
        label = str(getattr(point, "time", None) or (point.get("time") if isinstance(point, Mapping) else "") or "")[:5]
        if not label or label == "09:25":
            # The 09:25 print is the opening auction; direction is neutral by
            # construction, so it must not enter the buy/sell ratio.
            continue
        if label < "09:30" or label > upto_minute:
            continue
        buy += _f(getattr(point, "buy_amount", None) if not isinstance(point, Mapping) else point.get("buy_amount"))
        sell += _f(getattr(point, "sell_amount", None) if not isinstance(point, Mapping) else point.get("sell_amount"))
        seen = True
    if not seen or buy + sell <= 0:
        return None
    return {
        "buy_amount": buy,
        "sell_amount": sell,
        "net_ratio": (buy - sell) / (buy + sell) * 100,
    }


def classify_regime(index_change_pct: float) -> str:
    if index_change_pct <= RULES["regime_strong_low_pct"]:
        return REGIME_STRONG_LOW
    if index_change_pct <= RULES["regime_low_pct"]:
        return REGIME_LOW
    if index_change_pct <= 0.0:
        return REGIME_FLAT
    return REGIME_HIGH


def _index_change(index_rows: Sequence[Mapping[str, Any]], index_prev_close: float, k: int) -> float | None:
    bars = _bars(index_rows, k)
    if len(bars) < k or index_prev_close <= 0:
        return None
    return (bars[k - 1]["price"] / index_prev_close - 1) * 100


def opening_decision_markers(
    *,
    minute_rows: Sequence[Mapping[str, Any]],
    index_rows: Sequence[Mapping[str, Any]] | None,
    index_prev_close: float,
    prev_close: float,
    open_price: float = 0.0,
    position: bool = False,
    flow_points: Sequence[Any] | None = None,
    sell_gate_for_all: bool = False,
) -> list[OpeningMarker]:
    """Evaluate the opening window and return buy/sell markers (0..2).

    Uses only data visible at each decision minute, so recomputation later in
    the day reproduces the same markers (deterministic replay).

    ``sell_gate_for_all`` lifts the position requirement on the 09:31 sell
    gate: non-holders then get an avoid-chase marker (same trigger, different
    semantics — do not buy the opening high) used by the opportunity queue.
    """
    markers: list[OpeningMarker] = []
    if prev_close <= 0:
        return markers

    bars1 = _bars(minute_rows, 1)
    if not bars1:
        return markers
    open_ref = open_price if open_price > 0 else bars1[0]["price"]
    gap_pct = (open_ref / prev_close - 1) * 100

    idx_chg_1 = _index_change(index_rows or [], index_prev_close, 1)

    # ---- 09:31 sell gate (holders) / avoid-chase warning (non-holders) ----
    tape_1 = _tape_stats(flow_points or [], "09:30")
    if (
        gap_pct >= RULES["sell_gate_gap_min_pct"]
        and tape_1 is not None
        and tape_1["net_ratio"] <= RULES["sell_gate_net_max_pct"]
        and (position or sell_gate_for_all)
    ):
        if position:
            gate_rule = "opening7_sell_gate"
            gate_reasons = [
                f"开盘卖出闸门：高开 {gap_pct:+.2f}% 且分笔净买比 {tape_1['net_ratio']:+.1f}%（09:31）",
                "研究样本：该组合全天收阴率约6成、收盘均值-1.2~-1.7%（震荡市样本）",
            ]
        else:
            gate_rule = "opening7_avoid_chase"
            gate_reasons = [
                f"开盘高点回避：高开 {gap_pct:+.2f}% 且分笔净买比 {tape_1['net_ratio']:+.1f}%（09:31），无持仓不追，等回落再看",
                "研究样本：该组合全天收阴率约6成、收盘均值-1.2~-1.7%（震荡市样本）",
            ]
        markers.append(OpeningMarker(
            time=SELL_CLOCK,
            side="sell",
            price=bars1[0]["price"],
            change_pct=(bars1[0]["price"] / prev_close - 1) * 100,
            regime=classify_regime(idx_chg_1) if idx_chg_1 is not None else "",
            rule=gate_rule,
            reasons=gate_reasons,
            source_quality="live_l1" if tape_1 is not None else "minute_proxy",
        ))

    # ---- 09:33 buy decision ----
    bars3 = _bars(minute_rows, 3)
    if len(bars3) < 3:
        return markers
    price_3 = bars3[2]["price"]
    from_open = (price_3 / open_ref - 1) * 100
    if gap_pct >= RULES["buy_chase_gap_max_pct"] or from_open >= RULES["buy_chase_from_open_max_pct"]:
        return markers  # chase exclusion: no buy marker at all
    idx_chg_3 = _index_change(index_rows or [], index_prev_close, 3)
    if idx_chg_3 is None:
        return markers
    regime = classify_regime(idx_chg_3)
    tape_3 = _tape_stats(flow_points or [], "09:32")
    # tape-informed VWAP proxy: cumulative tape amount / cumulative bar volume
    vwap_dev_ok: bool | None = None
    if tape_3 is not None:
        total_vol = sum(bar["vol"] for bar in bars3)
        total_amt = tape_3["buy_amount"] + tape_3["sell_amount"]
        if total_vol > 0 and total_amt > 0:
            vwap_dev_ok = price_3 >= total_amt / (total_vol * 100)

    buy = False
    reason = ""
    if regime == REGIME_STRONG_LOW:
        buy = True
        reason = f"强低开抢反弹：指数 {idx_chg_3:+.2f}%（研究样本该制度池票30分钟均值+3.1%）"
    elif regime == REGIME_LOW:
        if tape_3 is not None:
            buy = tape_3["net_ratio"] > RULES["buy_low_open_net_min_pct"]
            reason = (
                f"低开修复：指数 {idx_chg_3:+.2f}%，分笔净买比 {tape_3['net_ratio']:+.1f}% 无重抛压"
                if buy else ""
            )
        else:
            buy = from_open > RULES["low_open_fallback_from_open_min_pct"]
            reason = f"低开修复（无分笔，用价格承接代理）：指数 {idx_chg_3:+.2f}%，开盘跌幅 {from_open:+.2f}%" if buy else ""
    elif regime == REGIME_FLAT:
        if tape_3 is not None and vwap_dev_ok is not None:
            buy = tape_3["net_ratio"] >= RULES["buy_flat_net_min_pct"] and vwap_dev_ok
            reason = (
                f"平稳开精选：分笔净买比 {tape_3['net_ratio']:+.1f}% 且站上分笔VWAP"
                if buy else ""
            )
        # flat regime without tape: no buy (study showed no edge)
    # high_open: never chase

    if buy:
        reasons = [f"09:33买入决策（{REGIME_LABELS[regime]}制度）：{reason}"]
        if tape_3 is not None:
            reasons.append(f"分笔净买比 {tape_3['net_ratio']:+.1f}%（L1成交明细，非委托队列）")
        markers.append(OpeningMarker(
            time=BUY_CLOCK,
            side="buy",
            price=price_3,
            change_pct=(price_3 / prev_close - 1) * 100,
            regime=regime,
            rule=f"opening7_buy_{regime}",
            reasons=reasons,
            source_quality="live_l1" if tape_3 is not None else "minute_proxy",
        ))
    return markers


__all__ = [
    "BUY_CLOCK",
    "OpeningMarker",
    "REGIME_LABELS",
    "RULES",
    "SELL_CLOCK",
    "classify_regime",
    "opening_decision_markers",
]
