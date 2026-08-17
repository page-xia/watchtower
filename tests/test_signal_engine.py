from app.models import (
    AuctionSnapshot,
    IndexSnapshot,
    OrderFlowObservation,
    Quote,
    SectorSnapshot,
    SignalPhase,
    SignalType,
    TradeAction,
    TradeDirection,
    WatchlistItem,
)
from app.signal_engine import SignalEngine


RULES = {
    "thresholds": {
        "sector_watch_score": 55,
        "buy_volume_ratio_min": 1.25,
        "core_attack_volume_ratio": 1.5,
        "order_flow_attack_score": 25,
        "order_flow_pressure_score": -25,
    }
}


THEMES = [
    {
        "name": "AI硬件",
        "core_codes": ["300308", "300476"],
        "members": ["300308", "300476", "002428"],
    },
    {
        "name": "PCB",
        "core_codes": ["300476"],
        "members": ["300476", "002428"],
    },
]


def q(
    code: str,
    name: str,
    themes: list[str],
    price: float,
    prev_close: float,
    open_price: float,
    day_low: float,
    day_high: float,
    minute_ratio: float,
    core: bool = False,
) -> Quote:
    change_pct = round((price - prev_close) / prev_close * 100, 2)
    return Quote(
        code=code,
        name=name,
        themes=themes,
        price=price,
        prev_close=prev_close,
        open=open_price,
        high=day_high,
        low=day_low,
        day_high=day_high,
        day_low=day_low,
        change_pct=change_pct,
        volume=1_000_000,
        amount=100_000_000,
        minute_amount=2_000_000,
        minute_amount_ratio=minute_ratio,
        limit_up=change_pct >= 9.8,
        opened_limit=day_high >= prev_close * 1.095 and change_pct < 9.3,
        core=core,
        updated_at="10:12:00",
    )


def index_turning() -> list[IndexSnapshot]:
    return [
        IndexSnapshot(
            code="000001",
            name="上证指数",
            price=3298,
            prev_close=3300,
            open=3272,
            high=3302,
            low=3268,
            change_pct=-0.06,
            rebound_from_low_pct=0.92,
            minute_amount_ratio=1.25,
            amount=500_000_000_000,
        )
    ]


def sector_snapshot() -> SectorSnapshot:
    return SectorSnapshot(
        name="AI硬件",
        heat_score=88,
        avg_change_pct=4.2,
        up_count=1,
        total_count=1,
        limit_up_count=1,
        opened_limit_count=0,
        core_attack=True,
        core_codes=["300308"],
        leader_code="300308",
        leader_name="中际旭创",
        reasons=["AI硬件强确认"],
    )


def bars_from_prices(prices: list[float]) -> list[dict[str, float | str]]:
    return [
        {
            "time": f"09:{31 + index:02d}",
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price,
            "price": price,
            "vol": 100,
            "amount": price * 10_000,
        }
        for index, price in enumerate(prices)
    ]






def test_board_signals_remain_observation_even_when_old_confluence_is_strong() -> None:
    engine = SignalEngine(RULES)
    flow = OrderFlowObservation(
        available=True,
        source="easy_tdx_realtime",
        data_quality="l1_five_level",
        direction="买盘增强",
        score=42,
        confidence="中等",
        minute_amount_ratio=2.4,
    )
    quote = q("300308", "中际旭创", ["CPO", "AI硬件"], 98.2, 100, 94.0, 93.7, 99.5, 2.4, True)
    quote = quote.model_copy(update={"order_flow": flow})
    peer = q("300476", "胜宏科技", ["PCB", "AI硬件"], 63.8, 60, 60.0, 59.7, 64.2, 3.5, True)

    market = engine.build_market_state(index_turning(), [quote, peer], clock_label="09:33:00")
    sectors = engine.rank_sectors([quote, peer], THEMES, market)
    signals = engine.build_signals(
        [quote, peer],
        [WatchlistItem(code="300308", name="中际旭创", themes=["CPO", "AI硬件"], core=True)],
        sectors,
        market,
        clock_label="09:33:00",
    )

    assert signals[0].signal == SignalType.WATCH
    assert signals[0].phase == SignalPhase.OBSERVE.value
    assert signals[0].action == TradeAction.OBSERVE.value
    assert signals[0].direction == TradeDirection.NONE.value
    assert signals[0].exit_score == 0
    assert signals[0].signal_source == "tdx_formula_cached_features"
    assert any("首页只消费本地分钟特征缓存" in risk for risk in signals[0].risks)


def test_preopen_auction_is_only_sorting_context_not_a_buy_signal() -> None:
    engine = SignalEngine(RULES)
    quote = q("300476", "胜宏科技", ["PCB"], 63.8, 60.0, 60.0, 59.7, 64.2, 3.5, True)
    quote = quote.model_copy(
        update={
            "auction": AuctionSnapshot(
                available=True,
                source="easy_tdx_preopen_quote",
                data_quality="proxy",
                trade_date="20260806",
                as_of="09:24:00",
                price=63.0,
                prev_close=60.0,
                change_pct=5.0,
                amount=50_000_000,
                order_imbalance_pct=26.0,
                status="实时竞价预估",
            )
        }
    )
    market = engine.build_market_state(index_turning(), [quote], clock_label="09:24:00")
    sectors = engine.rank_sectors(
        [quote],
        [{"name": "PCB", "core_codes": ["300476"], "members": ["300476"]}],
        market,
    )
    signals = engine.build_signals(
        [quote],
        [WatchlistItem(code="300476", name="胜宏科技", themes=["PCB"], core=True)],
        sectors,
        market,
        clock_label="09:24:00",
    )

    assert signals[0].signal == SignalType.WATCH
    assert "竞价先验" in signals[0].factor_flags
    assert any("仅用于排序观察" in reason for reason in signals[0].reasons)


def test_market_pulse_distinguishes_prior_weakness_from_single_snapshot_rebound() -> None:
    engine = SignalEngine(RULES)

    def index(price: float, low: float, ratio: float) -> IndexSnapshot:
        return IndexSnapshot(
            code="000001",
            name="上证指数",
            price=price,
            prev_close=3300,
            open=3300,
            high=max(3300, price),
            low=low,
            change_pct=(price - 3300) / 3300 * 100,
            rebound_from_low_pct=(price - low) / low * 100,
            minute_amount_ratio=ratio,
            amount=100,
        )

    first = engine.build_market_state([index(3288, 3285, 1.2)], [], clock_label="09:30:00")
    second = engine.build_market_state([index(3284, 3280, 1.1)], [], clock_label="09:31:00")
    third = engine.build_market_state([index(3292, 3280, 1.25)], [], clock_label="09:32:00")

    assert first.index_turning_mode == "snapshot_rebound_proxy"
    assert second.index_turning is False
    assert third.index_turning is True
    assert third.index_turning_mode == "rolling_turn"
    assert third.index_prior_slope_pct < 0
    assert third.market_pulse_points == 3


def test_rank_sectors_includes_all_quote_themes() -> None:
    engine = SignalEngine(RULES)
    quotes = [
        q("300308", "中际旭创", ["CPO", "通信设备"], 98.2, 100, 94.0, 93.7, 99.5, 2.4, True),
        q("300476", "胜宏科技", ["PCB", "元器件"], 63.8, 60, 60.0, 59.7, 64.2, 3.5, True),
    ]

    market = engine.build_market_state(index_turning(), quotes)
    sectors = engine.rank_sectors(
        quotes,
        [{"name": "PCB", "core_codes": ["300476"], "members": ["300476"]}],
        market,
    )

    names = {sector.name for sector in sectors}
    assert {"CPO", "通信设备", "元器件", "PCB"}.issubset(names)


def test_rank_sectors_excludes_new_listing_distortion_from_strength_metrics() -> None:
    engine = SignalEngine(RULES)
    new_listing = q("301717", "N超纯", ["半导体设备"], 762.24, 100, 300, 300, 800, 3.5)
    weak_core = q("688012", "中微公司", ["半导体设备"], 93.10, 100, 100, 92, 101, 1.2, True)
    active_peer = q("688120", "华海清科", ["半导体设备"], 101.82, 100, 100, 99, 102, 1.8, True)

    market = engine.build_market_state(index_turning(), [new_listing, weak_core, active_peer])
    sectors = engine.rank_sectors(
        [new_listing, weak_core, active_peer],
        [{"name": "半导体设备", "core_codes": ["301717", "688012", "688120"], "members": ["301717", "688012", "688120"]}],
        market,
    )
    sector = next(item for item in sectors if item.name == "半导体设备")

    assert sector.new_listing_excluded_count == 1
    assert sector.raw_total_count == 3
    assert sector.total_count == 2
    assert sector.avg_change_pct == -2.54
    assert sector.leader_code == "688120"
    assert sector.limit_up_count == 0
    assert any("新股扰动剔除1只：N超纯" in reason for reason in sector.reasons)


def test_engine_strategy_version_is_zuot_formula() -> None:
    engine = SignalEngine(RULES)
    assert engine.strategy_version == "zuot_tdx_levels_v1"


def test_zuot_event_detects_support_reclaim_buy() -> None:
    """看板提升路径：LONGCROSS(支撑,现价,2) 跌破支撑 → 买T 事件。"""
    engine = SignalEngine(RULES)
    # prev_close=10, day_high=10.8, day_low=9.8 → 支撑=9.8625, 阻力=10.675
    quote = q("300308", "中际旭创", ["AI硬件"], 9.9, 10.0, 10.0, 9.8, 10.8, 1.5, True)
    bars = bars_from_prices([10.0, 10.05, 9.80])

    event = engine._latest_cached_zuot_event(bars, quote)

    assert event is not None
    assert event["signal"] == SignalType.BUY_T
    assert event["time"] == "09:33"
    assert abs(event["invalidation_price"] - 9.8625) < 0.01
    assert any("LONGCROSS(支撑,现价,2)" in reason for reason in event["reasons"])


def test_zuot_event_detects_resistance_breakout_sell() -> None:
    """看板提升路径：LONGCROSS(现价,阻力,2) 突破阻力 → 卖T 事件。"""
    engine = SignalEngine(RULES)
    quote = q("300308", "中际旭创", ["AI硬件"], 10.7, 10.0, 10.0, 9.8, 10.8, 1.5, True)
    bars = bars_from_prices([10.5, 10.6, 10.70])

    event = engine._latest_cached_zuot_event(bars, quote)

    assert event is not None
    assert event["signal"] == SignalType.SELL_T
    assert event["time"] == "09:33"
    assert abs(event["invalidation_price"] - 10.675) < 0.01
    assert any("LONGCROSS(现价,阻力,2)" in reason for reason in event["reasons"])


def test_zuot_event_none_when_price_stays_inside_channel() -> None:
    engine = SignalEngine(RULES)
    quote = q("300308", "中际旭创", ["AI硬件"], 10.2, 10.0, 10.0, 9.8, 10.8, 1.5, True)
    bars = bars_from_prices([10.1, 10.2, 10.15, 10.25])

    assert engine._latest_cached_zuot_event(bars, quote) is None
