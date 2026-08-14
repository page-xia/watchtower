from pathlib import Path

from app.models import (
    IndexSnapshot,
    MarketState,
    OpeningAction,
    OrderFlowObservation,
    Quote,
    SectorSnapshot,
    TrendState,
)
from app.opening_strategy import OpeningStrategy


def market_features(**updates):
    values = {
        "breadth": 70,
        "index_volume_ratio": 1.25,
        "turning": True,
        "index_recovering": True,
        "index_change": 0.2,
        "emotion": 70,
    }
    values.update(updates)
    return values


def sector_features(**updates):
    values = {
        "heat": 86,
        "breadth": 72,
        "avg_change": 1.8,
        "core_attack": True,
        "limit_up_count": 1,
        "opened_limit_count": 0,
        "flow_delta": 12,
        "flow_persistence": True,
    }
    values.update(updates)
    return values


def stock_features(**updates):
    values = {
        "price": 10.25,
        "open": 10.0,
        "prev_close": 10.0,
        "vwap": 10.1,
        "change_pct": 2.5,
        "minute_amount_ratio": 1.5,
        "same_minute_amount_ratio": 1.35,
        "slope3": 0.20,
        "flow_score": 22,
        "flow_direction": "买盘增强",
        "flow_available": True,
        "core": True,
        "limit_up": False,
        "opened_limit": False,
        "rebound": 2.0,
    }
    values.update(updates)
    return values


def evaluate(checkpoint="09:35", *, position=False, market=None, stock=None, sector=None):
    return OpeningStrategy().evaluate_historical_point(
        trade_date="20260807",
        checkpoint=checkpoint,
        market=market or market_features(),
        sector=sector or sector_features(),
        stock=stock or stock_features(),
        sector_name="PCB",
        position=position,
    )["item"]


def test_0933_is_screening_only_even_when_all_gates_pass():
    item = evaluate("09:33")

    assert item["action"] == OpeningAction.SCREEN.value
    assert item["can_execute"] is False
    assert item["market_gate"] is True
    assert item["sector_gate"] is True
    assert item["stock_gate"] is True
    assert item["reasons"][0].startswith("09:33初筛")


def test_0935_confirms_buy_only_after_three_gates_pass():
    item = evaluate("09:35")

    assert item["action"] == OpeningAction.BUY.value
    assert item["can_execute"] is True
    assert item["market_gate"] and item["sector_gate"] and item["stock_gate"]
    assert item["reasons"][0].startswith("09:35确认")
    assert any("分时放量" in reason for reason in item["reasons"])


def test_l1_pressure_vetoes_buy_signal():
    item = evaluate("09:35", stock=stock_features(flow_score=-35, flow_direction="卖盘增强"))

    assert item["action"] == OpeningAction.AVOID.value
    assert item["can_execute"] is False
    assert item["flow_pressure"] is True
    assert any("明显抛压" in risk for risk in item["risks"])


def test_risk_market_reduces_existing_position_instead_of_buying():
    item = evaluate(
        "09:35",
        position=True,
        market=market_features(
            breadth=34,
            index_volume_ratio=0.92,
            turning=False,
            index_recovering=False,
            index_change=-1.1,
        ),
        stock=stock_features(rebound=8.2),
    )

    assert item["action"] == OpeningAction.REDUCE.value
    assert item["can_execute"] is False


def test_0937_rechecks_chase_risk_and_changes_buy_to_reduce():
    item = evaluate("09:37", position=True, stock=stock_features(rebound=8.0))

    assert item["action"] == OpeningAction.REDUCE.value
    assert item["can_execute"] is False
    assert any("09:37复核" in reason for reason in item["reasons"])
    assert any("兑现" in risk for risk in item["risks"])


def test_close_without_saved_opening_snapshot_never_backfills_from_daily_data(tmp_path: Path):
    strategy = OpeningStrategy(persist_path=tmp_path / "opening.jsonl")
    payload = strategy.evaluate(
        trade_date="20260807",
        clock_label="15:00:00",
        data_mode="closed_static",
        frozen=True,
        quotes=[],
        indices=[],
        market=None,
        sectors=[],
    )

    assert payload.stage == "unavailable"
    assert payload.can_execute is False
    assert payload.data_quality == "unavailable"
    assert "不能用全天数据反推" in payload.data_note


def _live_quote(code="300476"):
    return Quote(
        code=code,
        name="胜宏科技",
        themes=["PCB"],
        price=10.25,
        prev_close=10.0,
        open=10.0,
        high=10.3,
        low=10.0,
        day_high=10.3,
        day_low=10.0,
        change_pct=2.5,
        amount=20_000_000,
        minute_amount=2_000_000,
        minute_amount_ratio=1.5,
        updated_at="09:35:00",
        core=True,
        order_flow=OrderFlowObservation(
            available=True,
            data_quality="l1_five_level",
            direction="买盘增强",
            score=22,
            confidence="中",
        ),
    )


def _live_index():
    return IndexSnapshot(
        code="000001",
        name="上证指数",
        price=3310,
        prev_close=3300,
        open=3295,
        high=3312,
        low=3290,
        change_pct=0.3,
        rebound_from_low_pct=0.6,
        minute_amount_ratio=1.25,
    )


def _live_market():
    return MarketState(
        trend=TrendState.TURNING_UP,
        emotion_score=70,
        breadth_pct=70,
        index_turning=True,
        amount_expanding=True,
        mainline="PCB",
        indices=[_live_index()],
        reasons=["指数拐头"],
        updated_at="09:35:00",
    )


def _live_sector():
    return SectorSnapshot(
        name="PCB",
        heat_score=86,
        avg_change_pct=1.8,
        up_count=72,
        total_count=100,
        limit_up_count=1,
        opened_limit_count=0,
        core_attack=True,
        core_codes=["300476"],
        leader_code="300476",
        leader_name="胜宏科技",
        reasons=["核心容量票进攻"],
    )


def test_saved_checkpoint_is_replayed_after_close(tmp_path: Path):
    path = tmp_path / "opening.jsonl"
    live = OpeningStrategy(persist_path=path)
    live_payload = live.evaluate(
        trade_date="20260807",
        clock_label="09:35:00",
        data_mode="live",
        frozen=False,
        quotes=[_live_quote()],
        indices=[_live_index()],
        market=_live_market(),
        sectors=[_live_sector()],
    )
    assert live_payload.buy_count == 1

    replay = OpeningStrategy(persist_path=path)
    closed = replay.evaluate(
        trade_date="20260807",
        clock_label="15:00:00",
        data_mode="closed_static",
        frozen=True,
        quotes=[],
        indices=[],
        market=None,
        sectors=[],
    )

    assert closed.stage == "closed"
    assert closed.frozen is True
    assert closed.buy_count == 1
    assert replay.item_for("300476") is not None
    assert replay.item_for("300476").action == OpeningAction.BUY
