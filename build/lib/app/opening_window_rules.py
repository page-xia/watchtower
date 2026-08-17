"""Opening-window extended rules (09:35-10:00), evaluated every engine tick.

These are NEW hypotheses beyond the validated opening7 09:31/09:33 events.
All three stay ``validation_status="research_only"`` until the trajectory
event study (scripts/opening_window_research.py) confirms thresholds.

Every rule is a pure function over point-in-time inputs so the exact same
code path runs live (6s ticks) and in historical research replay.  Price-side
state comes from the local quote trajectory; tape stats are cumulative L1
transaction-print aggregates (minute granularity), never order-queue data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Window bounds (clock labels, HH:MM)
EXTENDED_START = "09:35"
EXTENDED_END = "10:00"
RECOVERY_CUTOFF = "09:50"

RULE_HIGH_AVOID = "ow_high_avoid"
RULE_VWAP_PULLBACK = "ow_vwap_pullback_buy"
RULE_LOW_OPEN_RECOVERY = "ow_low_open_recovery"

RULE_LABELS = {
    RULE_HIGH_AVOID: "高点回避",
    RULE_VWAP_PULLBACK: "回踩VWAP",
    RULE_LOW_OPEN_RECOVERY: "低开修复",
}

# Initial thresholds; to be tuned by the trajectory event study.
EXTENDED_RULES: dict[str, float] = {
    "high_avoid_min_change_pct": 2.0,      # 至少有冲高幅度才谈回避
    "high_avoid_net_max_pct": -5.0,        # 创新高时分笔累计净买比转负
    "vwap_pullback_prior_excess_pct": 2.0, # 之前曾高出 VWAP 2%（确实冲过）
    "vwap_pullback_band_pct": 0.3,         # 当前回落到 VWAP ±0.3% 带内
    "vwap_pullback_net_min_pct": 5.0,      # 回踩时分笔净买比转正
    "recovery_gap_max_pct": -1.0,          # 低开至少 1%
    "recovery_net_min_pct": 0.0,           # 收复时无净抛压
}


@dataclass
class RuleInput:
    """Point-in-time per-stock inputs for extended rule evaluation."""

    code: str
    clock: str                # "HH:MM" of the evaluation tick
    price: float
    open_price: float
    prev_close: float
    session_high: float       # day high so far
    prev_session_high: float  # day high before this tick (engine-tracked)
    vwap: float               # day VWAP from quote amount/volume
    tape_net_ratio: float | None = None   # cumulative L1 net buy ratio, pct
    tape_ready: bool = False


@dataclass
class RuleCandidate:
    rule: str
    side: str                 # "buy" | "sell"
    price: float
    change_pct: float
    reasons: list[str] = field(default_factory=list)
    tape_net_ratio: float | None = None
    source_quality: str = "live_l1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def evaluate_high_avoid(inp: RuleInput) -> RuleCandidate | None:
    """New session high while the cumulative tape turns to net distribution.

    Chasing a high that the tape does not support is the mirror image of the
    09:31 sell gate, applied through the whole opening half hour.
    """
    if inp.prev_close <= 0 or inp.price <= 0:
        return None
    if not (EXTENDED_START <= inp.clock <= EXTENDED_END):
        return None
    if inp.session_high <= inp.prev_session_high:
        return None  # 必须创出本轮新高
    change_pct = (inp.price / inp.prev_close - 1) * 100
    if change_pct < EXTENDED_RULES["high_avoid_min_change_pct"]:
        return None
    if not inp.tape_ready or inp.tape_net_ratio is None:
        return None
    if inp.tape_net_ratio > EXTENDED_RULES["high_avoid_net_max_pct"]:
        return None
    return RuleCandidate(
        rule=RULE_HIGH_AVOID,
        side="sell",
        price=inp.price,
        change_pct=change_pct,
        reasons=[
            f"高点回避：创日内新高 {change_pct:+.2f}% 但分笔累计净买比 {inp.tape_net_ratio:+.1f}%，冲高缺乏承接",
        ],
        tape_net_ratio=inp.tape_net_ratio,
        source_quality="live_l1",
    )


def evaluate_vwap_pullback_buy(inp: RuleInput, *, max_vwap_excess_pct: float) -> RuleCandidate | None:
    """Pullback into the VWAP band after a real morning push, tape supportive.

    ``max_vwap_excess_pct`` is the engine-tracked maximum (price/VWAP-1)*100
    seen so far this session; the rule requires a prior push above the band.
    """
    if inp.prev_close <= 0 or inp.vwap <= 0 or inp.price <= 0:
        return None
    if not (EXTENDED_START <= inp.clock <= EXTENDED_END):
        return None
    if max_vwap_excess_pct < EXTENDED_RULES["vwap_pullback_prior_excess_pct"]:
        return None  # 没有真冲高，回踩无从谈起
    deviation = (inp.price / inp.vwap - 1) * 100
    if abs(deviation) > EXTENDED_RULES["vwap_pullback_band_pct"]:
        return None
    if inp.price < inp.open_price:
        return None  # 仍在开盘价下方，结构偏弱
    if not inp.tape_ready or inp.tape_net_ratio is None:
        return None
    if inp.tape_net_ratio < EXTENDED_RULES["vwap_pullback_net_min_pct"]:
        return None
    change_pct = (inp.price / inp.prev_close - 1) * 100
    return RuleCandidate(
        rule=RULE_VWAP_PULLBACK,
        side="buy",
        price=inp.price,
        change_pct=change_pct,
        reasons=[
            f"回踩VWAP：早盘冲高 {max_vwap_excess_pct:+.1f}% 后回落至 VWAP（偏离 {deviation:+.2f}%）企稳，分笔净买比 {inp.tape_net_ratio:+.1f}%",
        ],
        tape_net_ratio=inp.tape_net_ratio,
        source_quality="live_l1",
    )


def evaluate_low_open_recovery(inp: RuleInput) -> RuleCandidate | None:
    """Low-open name reclaims the open price and VWAP before the cutoff."""
    if inp.prev_close <= 0 or inp.open_price <= 0 or inp.price <= 0:
        return None
    if not (EXTENDED_START <= inp.clock <= RECOVERY_CUTOFF):
        return None
    gap_pct = (inp.open_price / inp.prev_close - 1) * 100
    if gap_pct > EXTENDED_RULES["recovery_gap_max_pct"]:
        return None  # 低开幅度不足
    if inp.price < inp.open_price:
        return None  # 尚未收复开盘价
    if inp.vwap > 0 and inp.price < inp.vwap:
        return None  # 尚未站上 VWAP
    if not inp.tape_ready or inp.tape_net_ratio is None:
        return None
    if inp.tape_net_ratio < EXTENDED_RULES["recovery_net_min_pct"]:
        return None
    change_pct = (inp.price / inp.prev_close - 1) * 100
    return RuleCandidate(
        rule=RULE_LOW_OPEN_RECOVERY,
        side="buy",
        price=inp.price,
        change_pct=change_pct,
        reasons=[
            f"低开修复：低开 {gap_pct:+.2f}% 后收复开盘价并站上 VWAP，分笔净买比 {inp.tape_net_ratio:+.1f}%",
        ],
        tape_net_ratio=inp.tape_net_ratio,
        source_quality="live_l1",
    )


__all__ = [
    "EXTENDED_END",
    "EXTENDED_RULES",
    "EXTENDED_START",
    "RECOVERY_CUTOFF",
    "RULE_HIGH_AVOID",
    "RULE_LABELS",
    "RULE_LOW_OPEN_RECOVERY",
    "RULE_VWAP_PULLBACK",
    "RuleCandidate",
    "RuleInput",
    "evaluate_high_avoid",
    "evaluate_low_open_recovery",
    "evaluate_vwap_pullback_buy",
]
