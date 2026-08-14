"""Opening-window engine + extended rules + avoid-chase sell gate tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app.opening7 import opening_decision_markers
from app.opening_window_engine import OpeningWindowEngine, TapeAccumulator
from app.opening_window_rules import (
    RULE_HIGH_AVOID,
    RULE_LOW_OPEN_RECOVERY,
    RULE_VWAP_PULLBACK,
    RuleInput,
    evaluate_high_avoid,
    evaluate_low_open_recovery,
    evaluate_vwap_pullback_buy,
)
from tests.test_intraday_storage import MemoryStateStore


# ----------------------------------------------------------------- opening7
def _minute_rows() -> list[dict]:
    return [
        {"time": "09:30", "price": 10.6, "vol": 1000},
        {"time": "09:31", "price": 10.5, "vol": 800},
        {"time": "09:32", "price": 10.4, "vol": 600},
    ]


def _tape_points() -> list[dict]:
    return [{"time": "09:30", "buy_amount": 40.0, "sell_amount": 100.0}]


def test_sell_gate_requires_position_by_default() -> None:
    markers = opening_decision_markers(
        minute_rows=_minute_rows(),
        index_rows=[{"time": "09:30", "price": 99.0}],
        index_prev_close=100.0,
        prev_close=10.0,
        open_price=10.5,
        position=False,
        flow_points=_tape_points(),
    )
    assert not [m for m in markers if m.side == "sell"]


def test_sell_gate_for_all_emits_avoid_chase_for_non_holders() -> None:
    markers = opening_decision_markers(
        minute_rows=_minute_rows(),
        index_rows=[{"time": "09:30", "price": 99.0}],
        index_prev_close=100.0,
        prev_close=10.0,
        open_price=10.5,
        position=False,
        flow_points=_tape_points(),
        sell_gate_for_all=True,
    )
    sells = [m for m in markers if m.side == "sell"]
    assert len(sells) == 1
    assert sells[0].rule == "opening7_avoid_chase"
    assert "回避" in sells[0].reasons[0]


def test_sell_gate_for_all_keeps_holder_semantics() -> None:
    markers = opening_decision_markers(
        minute_rows=_minute_rows(),
        index_rows=[{"time": "09:30", "price": 99.0}],
        index_prev_close=100.0,
        prev_close=10.0,
        open_price=10.5,
        position=True,
        flow_points=_tape_points(),
        sell_gate_for_all=True,
    )
    sells = [m for m in markers if m.side == "sell"]
    assert len(sells) == 1
    assert sells[0].rule == "opening7_sell_gate"


# ---------------------------------------------------------- tape accumulator
def _point(minute: str, buy: float, sell: float) -> SimpleNamespace:
    return SimpleNamespace(time=minute, buy_amount=buy, sell_amount=sell)


def test_tape_accumulator_cumulative_and_partial_minute() -> None:
    acc = TapeAccumulator()
    acc.update([_point("09:30", 100, 50), _point("09:31", 60, 40)])
    assert acc.processed_minutes == {"09:30"}
    assert acc.cur_minute == "09:31"
    # 下一个 tick：09:31 闭分钟被累计，09:32 成为新的当前分钟
    acc.update([_point("09:30", 100, 50), _point("09:31", 60, 40), _point("09:32", 30, 70)])
    assert acc.processed_minutes == {"09:30", "09:31"}
    assert acc.cum_buy == 160 and acc.cum_sell == 90
    ratio = acc.net_ratio()
    assert ratio is not None
    assert abs(ratio - ((190 - 160) / 350 * 100)) < 1e-6


def test_tape_accumulator_ignores_auction_print() -> None:
    acc = TapeAccumulator()
    acc.update([_point("09:25", 500, 500), _point("09:30", 100, 0)])
    assert acc.cum_buy == 0 and acc.cur_minute == "09:30"


def test_tape_accumulator_upto_excludes_open_minute() -> None:
    acc = TapeAccumulator()
    acc.update([_point("09:30", 100, 50), _point("09:31", 100, 0)])
    # upto=09:30 只统计 09:30 闭分钟，不含进行中的 09:31
    ratio = acc.net_ratio(upto="09:30")
    assert ratio is not None and abs(ratio - (50 / 150 * 100)) < 1e-6


def test_tape_accumulator_prefers_large_prints() -> None:
    """上游拆出大单字段时只统计大单印花，小单噪声不进净买比。"""
    acc = TapeAccumulator()
    acc.update(
        [
            SimpleNamespace(time="09:30", buy_amount=1000.0, sell_amount=900.0, large_buy_amount=300.0, large_sell_amount=100.0),
            SimpleNamespace(time="09:31", buy_amount=0.0, sell_amount=0.0, large_buy_amount=0.0, large_sell_amount=0.0),
        ]
    )
    ratio = acc.net_ratio()
    assert ratio is not None and abs(ratio - 50.0) < 1e-6  # (300-100)/(300+100)


# ----------------------------------------------------------- extended rules
def _input(**kwargs) -> RuleInput:
    base = dict(
        code="300476",
        clock="09:40",
        price=10.5,
        open_price=10.2,
        prev_close=10.0,
        session_high=10.6,
        prev_session_high=10.55,
        vwap=10.4,
        tape_net_ratio=-8.0,
        tape_ready=True,
    )
    base.update(kwargs)
    return RuleInput(**base)


def test_high_avoid_triggers_on_new_high_with_distribution() -> None:
    candidate = evaluate_high_avoid(_input())
    assert candidate is not None
    assert candidate.rule == RULE_HIGH_AVOID
    assert candidate.side == "sell"


def test_high_avoid_requires_new_high() -> None:
    assert evaluate_high_avoid(_input(session_high=10.55, prev_session_high=10.55)) is None


def test_high_avoid_requires_tape_distribution() -> None:
    assert evaluate_high_avoid(_input(tape_net_ratio=3.0)) is None


def test_high_avoid_outside_window() -> None:
    assert evaluate_high_avoid(_input(clock="10:30")) is None


def test_vwap_pullback_buy() -> None:
    candidate = evaluate_vwap_pullback_buy(
        _input(price=10.41, vwap=10.4, tape_net_ratio=8.0, session_high=10.8, open_price=10.2),
        max_vwap_excess_pct=3.5,
    )
    assert candidate is not None
    assert candidate.rule == RULE_VWAP_PULLBACK
    assert candidate.side == "buy"


def test_vwap_pullback_requires_prior_push() -> None:
    assert evaluate_vwap_pullback_buy(_input(price=10.41), max_vwap_excess_pct=0.5) is None


def test_low_open_recovery() -> None:
    candidate = evaluate_low_open_recovery(
        _input(open_price=9.8, price=9.85, vwap=9.8, tape_net_ratio=2.0)
    )
    assert candidate is not None
    assert candidate.rule == RULE_LOW_OPEN_RECOVERY


def test_low_open_recovery_cutoff() -> None:
    assert (
        evaluate_low_open_recovery(_input(clock="09:55", open_price=9.8, price=9.85, vwap=9.8, tape_net_ratio=2.0))
        is None
    )


# -------------------------------------------------------------- engine state
def _engine(tmp_path: Path) -> OpeningWindowEngine:
    settings = SimpleNamespace(
        opening_window_tick_seconds=6,
        opening_window_tape_count=1200,
        opening_window_tape_workers=4,
        opening_window_pool_sector_top=5,
        opening_window_pool_sector_members=3,
        opening_window_pool_board_top=20,
        opening_window_warn_confirm_ticks=2,
    )
    return OpeningWindowEngine(
        settings,
        context_provider=lambda: None,
        tape_fetcher=lambda code, date, count=0: None,
        position_checker=lambda code: False,
        data_dir=tmp_path,
    )


def _quote(price: float, high: float) -> SimpleNamespace:
    return SimpleNamespace(
        code="300476",
        name="胜宏科技",
        price=price,
        open=10.2,
        prev_close=10.0,
        high=high,
        amount=1.04e8,
        volume=1e6,
        limit_up=False,
    )


def _pool_entry() -> dict:
    return {"code": "300476", "name": "胜宏科技", "sector": "半导体", "origins": ["board_top"]}


def test_engine_warn_then_confirm_then_keep(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._trade_date = "20260812"
    # 分笔净卖压就绪
    acc = TapeAccumulator()
    acc.update([_point("09:35", 40, 100)])
    engine._tape["300476"] = acc
    quotes = {"300476": _quote(10.5, 10.6)}
    engine._price["300476"] = engine._price.get("300476") or type("PS", (), {})()
    from app.opening_window_engine import PriceState

    state = PriceState(prev_session_high=10.55, session_high=10.6)
    engine._price["300476"] = state

    # 第一次命中 → 预警
    engine._eval_extended("09:40", [_pool_entry()], quotes)
    marker_id = "20260812|300476|ow_high_avoid"
    assert engine._markers[marker_id]["state"] == "warn"

    # 第二次连续命中 → 确认
    engine._eval_extended("09:41", [_pool_entry()], quotes)
    assert engine._markers[marker_id]["state"] == "confirmed"
    assert "confirmed_at" in engine._markers[marker_id]


def test_engine_warn_removed_when_condition_fades(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._trade_date = "20260812"
    acc = TapeAccumulator()
    acc.update([_point("09:35", 40, 100)])
    engine._tape["300476"] = acc
    from app.opening_window_engine import PriceState

    engine._price["300476"] = PriceState(prev_session_high=10.55, session_high=10.6)
    engine._eval_extended("09:40", [_pool_entry()], {"300476": _quote(10.5, 10.6)})
    marker_id = "20260812|300476|ow_high_avoid"
    assert marker_id in engine._markers

    # 下一轮：不再创新高（state 未变），候选消失 → 预警移除
    engine._price["300476"] = PriceState(prev_session_high=10.6, session_high=10.6)
    engine._eval_extended("09:41", [_pool_entry()], {"300476": _quote(10.5, 10.6)})
    assert marker_id not in engine._markers


def test_engine_dedup_one_marker_per_rule(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._trade_date = "20260812"
    acc = TapeAccumulator()
    acc.update([_point("09:35", 40, 100)])
    engine._tape["300476"] = acc
    from app.opening_window_engine import PriceState

    engine._price["300476"] = PriceState(prev_session_high=10.55, session_high=10.6)
    engine._eval_extended("09:40", [_pool_entry()], {"300476": _quote(10.5, 10.6)})
    engine._eval_extended("09:41", [_pool_entry()], {"300476": _quote(10.5, 10.6)})
    engine._eval_extended("09:42", [_pool_entry()], {"300476": _quote(10.5, 10.6)})
    highs = [m for m in engine._markers.values() if m["rule"] == "ow_high_avoid"]
    assert len(highs) == 1
    assert highs[0]["state"] == "confirmed"


def test_engine_query_pagination_and_persist(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._trade_date = "20260812"
    engine._markers["20260812|300476|ow_high_avoid"] = {
        "id": "20260812|300476|ow_high_avoid",
        "code": "300476",
        "first_seen": "09:40:00",
        "state": "confirmed",
    }
    engine._dirty = False
    engine._persist()
    page = engine.query(None, offset=0, limit=20)
    assert page["total"] == 1
    assert page["items"][0]["code"] == "300476"
    # 换一天查询 → 读取持久化文件
    engine._trade_date = "20260813"
    engine._markers = {}
    page = engine.query("20260812", offset=0, limit=20)
    assert page["total"] == 1


def test_engine_latest_persisted_date_fallback(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._trade_date = ""
    (engine._store_dir / "20260811.json").write_text('{"markers": []}', encoding="utf-8")
    page = engine.query(None, offset=0, limit=20)
    assert page["trade_date"] == "20260811"


def test_engine_query_loads_markers_from_cloud_state(tmp_path: Path) -> None:
    cloud = MemoryStateStore()
    engine = _engine(tmp_path)
    engine.state_store = cloud
    engine._trade_date = "20260812"
    engine._markers["m1"] = {
        "id": "m1",
        "trade_date": "20260812",
        "code": "300476",
        "side": "buy",
        "first_seen": "09:35:00",
        "state": "confirmed",
    }
    engine._persist()
    engine._day_file("20260812").unlink()

    restored = _engine(tmp_path)
    restored.state_store = cloud
    page = restored.query("20260812", offset=0, limit=20)

    assert page["total"] == 1
    assert page["items"][0]["id"] == "m1"


def test_engine_tick_skips_confirmed_codes(tmp_path: Path) -> None:
    """已出确认态菱形的票进入去重组：不再拉分笔，也不再参与规则评估。"""
    calls: list[str] = []

    def fetch(code: str, date: str, count: int = 0) -> None:
        calls.append(code)
        return None

    quote = _quote(10.5, 10.6)
    other = _quote(9.9, 10.0)
    other.code = "000002"
    context = SimpleNamespace(
        market=SimpleNamespace(frozen=False, indices=[]),
        snapshot=SimpleNamespace(data_mode="live", quotes=[quote, other]),
        source_status={"trade_date": "20260813"},
        signals_all=[],
        sectors=[],
        watchlist=[],
    )
    engine = _engine(tmp_path)
    engine._context_provider = lambda: context
    engine._tape_fetcher = fetch
    engine._trade_date = "20260813"
    engine._markers["20260813|300476|ow_high_avoid"] = {
        "id": "20260813|300476|ow_high_avoid",
        "code": "300476",
        "first_seen": "09:40:00",
        "state": "confirmed",
    }
    engine._tick(datetime(2026, 8, 13, 9, 40))
    assert set(calls) == {"000002"}  # 已确认的 300476 不再拉分笔
    assert not [m for m in engine._markers.values() if m.get("code") == "000002"]


def test_engine_query_drops_limit_up_buy_markers(tmp_path: Path) -> None:
    """涨停票的买入菱形不进买T队列；卖出菱形保留；历史日不按今天涨停过滤。"""
    engine = _engine(tmp_path)
    engine._trade_date = "20260813"
    engine._limit_ups = {"300476"}  # 本 tick 快照里 300476 已涨停
    engine._markers.update(
        {
            "m1": {"id": "m1", "code": "300476", "side": "buy", "first_seen": "09:40:00", "state": "confirmed"},
            "m2": {"id": "m2", "code": "300476", "side": "sell", "first_seen": "09:31:00", "state": "confirmed"},
            "m3": {"id": "m3", "code": "000002", "side": "buy", "first_seen": "09:45:00", "state": "confirmed"},
        }
    )

    page = engine.query("20260813", offset=0, limit=20)
    pairs = {(m["code"], m["side"]) for m in page["items"]}
    assert ("300476", "buy") not in pairs  # 涨停票的买T被过滤
    assert ("300476", "sell") in pairs  # 卖出菱形保留
    assert ("000002", "buy") in pairs  # 非涨停票的买T保留
    assert page["total"] == 2

    latest_pairs = {(m["code"], m["side"]) for m in engine.latest(20)}
    assert ("300476", "buy") not in latest_pairs

    # 历史日翻页不按「今天」的涨停状态过滤（引擎已切到新的交易日）
    engine._dirty = False
    engine._persist()
    engine._trade_date = "20260814"
    engine._markers = {}
    page = engine.query("20260813", offset=0, limit=20)
    assert page["total"] == 3
