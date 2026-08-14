from __future__ import annotations

from app.opening7 import (
    RULES,
    classify_regime,
    opening_decision_markers,
)


def _rows(prices: list[float], start: int = 30, vol: float = 100.0) -> list[dict]:
    return [{"time": f"09:{start + i:02d}", "price": p, "vol": vol} for i, p in enumerate(prices)]


def _points(nets: list[float], start: int = 30) -> list[dict]:
    """nets[i] = net buy ratio pct for minute 09:(start+i); total amount 1e6."""
    out = []
    for i, net in enumerate(nets):
        total = 1_000_000.0
        buy = total * (1 + net / 100) / 2
        sell = total * (1 - net / 100) / 2
        out.append({"time": f"09:{start + i:02d}", "buy_amount": buy, "sell_amount": sell})
    return out


IDX_PREV = 4000.0


def _idx(chg_pct: float, k: int = 3) -> list[dict]:
    # flat index path pinned so that bar k-1 change == chg_pct
    base = IDX_PREV * (1 + chg_pct / 100)
    return _rows([base] * k)


def test_regime_classification_boundaries():
    assert classify_regime(-0.81) == "strong_low_open"
    assert classify_regime(-0.8) == "strong_low_open"
    assert classify_regime(-0.5) == "low_open"
    assert classify_regime(-0.3) == "low_open"
    assert classify_regime(-0.1) == "flat_open"
    assert classify_regime(0.0) == "flat_open"
    assert classify_regime(0.2) == "high_open"


def test_sell_gate_triggers_for_holder_gap_up_distribution():
    markers = opening_decision_markers(
        minute_rows=_rows([105.0, 104.8, 104.6]),
        index_rows=_idx(-0.1),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=105.0,   # gap +5%... exceeds chase gap max; sell gate still applies
        position=True,
        flow_points=_points([-20.0, -20.0, -20.0]),
    )
    sells = [m for m in markers if m.side == "sell"]
    assert len(sells) == 1
    assert sells[0].time == "09:31"
    assert sells[0].rule == "opening7_sell_gate"


def test_sell_gate_ignored_without_position():
    markers = opening_decision_markers(
        minute_rows=_rows([105.0, 104.8, 104.6]),
        index_rows=_idx(-0.1),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=105.0,
        position=False,
        flow_points=_points([-20.0, -20.0, -20.0]),
    )
    assert not [m for m in markers if m.side == "sell"]


def test_sell_gate_needs_tape_confirmation():
    markers = opening_decision_markers(
        minute_rows=_rows([103.0, 102.8, 102.6]),
        index_rows=_idx(-0.1),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=103.0,
        position=True,
        flow_points=[],   # no tape -> no sell marker
    )
    assert not [m for m in markers if m.side == "sell"]


def test_buy_strong_low_open_regime():
    markers = opening_decision_markers(
        minute_rows=_rows([96.0, 96.5, 97.0]),
        index_rows=_idx(-1.2),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=96.0,
        position=True,
        flow_points=_points([-30.0, -30.0, -30.0]),  # even with selling, regime buys
    )
    buys = [m for m in markers if m.side == "buy"]
    assert len(buys) == 1
    assert buys[0].time == "09:33"
    assert buys[0].regime == "strong_low_open"


def test_buy_low_open_requires_no_heavy_selling():
    heavy_sell = opening_decision_markers(
        minute_rows=_rows([97.0, 96.5, 96.2]),
        index_rows=_idx(-0.5),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=97.0,
        flow_points=_points([-25.0, -25.0, -25.0]),
    )
    assert not [m for m in heavy_sell if m.side == "buy"]

    mild_sell = opening_decision_markers(
        minute_rows=_rows([97.0, 96.5, 96.2]),
        index_rows=_idx(-0.5),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=97.0,
        flow_points=_points([-5.0, -5.0, -5.0]),
    )
    assert len([m for m in mild_sell if m.side == "buy"]) == 1


def test_buy_flat_regime_requires_strong_tape_and_vwap():
    weak = opening_decision_markers(
        minute_rows=_rows([100.2, 100.3, 100.4]),
        index_rows=_idx(-0.1),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=100.2,
        flow_points=_points([5.0, 5.0, 5.0]),  # net +5% < 10% required
    )
    assert not [m for m in weak if m.side == "buy"]

    strong = opening_decision_markers(
        minute_rows=_rows([100.2, 100.3, 100.4]),
        index_rows=_idx(-0.1),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=100.2,
        flow_points=_points([15.0, 15.0, 15.0]),
    )
    buys = [m for m in strong if m.side == "buy"]
    assert len(buys) == 1
    assert buys[0].regime == "flat_open"

    no_tape = opening_decision_markers(
        minute_rows=_rows([100.2, 100.3, 100.4]),
        index_rows=_idx(-0.1),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=100.2,
        flow_points=[],
    )
    assert not [m for m in no_tape if m.side == "buy"]


def test_high_open_never_chases():
    markers = opening_decision_markers(
        minute_rows=_rows([100.5, 100.8, 101.2]),
        index_rows=_idx(0.4),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=100.5,
        flow_points=_points([30.0, 30.0, 30.0]),
    )
    assert not [m for m in markers if m.side == "buy"]


def test_chase_exclusion_blocks_buy():
    markers = opening_decision_markers(
        minute_rows=_rows([106.0, 106.5, 107.0]),
        index_rows=_idx(-1.0),  # strong low-open regime would otherwise buy
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=106.0,  # gap +6% >= 5% chase cap
        flow_points=_points([20.0, 20.0, 20.0]),
    )
    assert not [m for m in markers if m.side == "buy"]


def test_deterministic_prefix_property():
    # A full-day row set must produce the same markers as its opening prefix.
    full_rows = _rows([96.0, 96.5, 97.0] + [97.5] * 20)
    prefix_rows = _rows([96.0, 96.5, 97.0])
    kwargs = dict(
        index_rows=_idx(-1.2),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=96.0,
        flow_points=_points([0.0, 0.0, 0.0] + [0.0] * 20),
    )
    full = opening_decision_markers(minute_rows=full_rows, **kwargs)
    prefix = opening_decision_markers(minute_rows=prefix_rows, **kwargs)
    assert [(m.time, m.side, m.rule) for m in full] == [(m.time, m.side, m.rule) for m in prefix]


def test_needs_three_bars_for_buy():
    markers = opening_decision_markers(
        minute_rows=_rows([96.0, 96.5]),  # only 2 bars: 09:33 not reached
        index_rows=_idx(-1.2, k=2),
        index_prev_close=IDX_PREV,
        prev_close=100.0,
        open_price=96.0,
        flow_points=_points([0.0, 0.0]),
    )
    assert markers == []


def test_rules_are_research_defaults():
    assert RULES["sell_gate_gap_min_pct"] == 2.0
    assert RULES["buy_flat_net_min_pct"] == 10.0
