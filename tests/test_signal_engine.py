from app.models import (
    AuctionSnapshot,
    IndexSnapshot,
    OrderFlowObservation,
    PositionRecord,
    Quote,
    SectorSnapshot,
    SignalPhase,
    SignalType,
    TradeAction,
    TradeDirection,
    TransactionFlowObservation,
    TransactionFlowPoint,
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


def near_trend_states_for_bars(bars: list[dict[str, float | str]]) -> list[dict[str, float | str | bool]]:
    return [
        {
            "time": str(bar["time"]),
            "white_line": float(bar["close"]),
            "yellow_line": float(bar["close"]) * 0.98,
            "trend_source_quality": "unit_daily_trend",
        }
        for bar in bars
    ]


def buy_formula_prices() -> list[float]:
    prices = [10.0 - 0.01 * index for index in range(20)]
    prices.extend([prices[-1] + 0.01 * step for step in range(1, 3)])
    return prices


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


def test_replay_markers_use_formula_buy_primitive_and_gold_resonance() -> None:
    engine = SignalEngine(RULES)
    quote = q("300308", "中际旭创", ["AI硬件"], 9.83, 10.0, 10.0, 9.75, 10.05, 1.0, True)
    market = engine.build_market_state(index_turning(), [quote])
    bars = bars_from_prices(buy_formula_prices())

    replay_points, markers, timeline, summary = engine.build_replay_detail(
        quote,
        bars,
        market,
        sector_snapshot(),
        trend_states=near_trend_states_for_bars(bars),
    )

    buy_markers = [marker for marker in markers if marker.signal == SignalType.BUY_T]
    assert replay_points[0].time == "09:31"
    assert timeline[0].signal == SignalType.WATCH
    assert buy_markers
    assert buy_markers[0].phase == SignalPhase.CONFIRM.value
    assert buy_markers[0].action == TradeAction.BUY_T.value
    assert buy_markers[0].direction == TradeDirection.POSITIVE_T.value
    assert buy_markers[0].gold_resonance is True
    assert any("CROSS(多方力度,6.78)" in reason for reason in buy_markers[0].reasons)
    assert any("接近" in reason for reason in buy_markers[0].resonance_reasons)
    assert all(marker.exit_score == 0 for marker in markers)
    assert any("做T买卖点唯一来源" in item for item in summary)


def test_replay_green_sell_marker_comes_from_formula_rsi_cross() -> None:
    engine = SignalEngine(RULES)
    quote = q("300308", "中际旭创", ["AI硬件"], 12.0, 10.0, 10.0, 9.8, 12.6, 1.0, True)
    market = engine.build_market_state(index_turning(), [quote])
    prices = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 12.0]

    _, markers, timeline, _ = engine.build_replay_detail(
        quote,
        bars_from_prices(prices),
        market,
        sector_snapshot(),
    )

    sell_markers = [marker for marker in markers if marker.signal == SignalType.SELL_T]
    assert sell_markers
    assert sell_markers[0].phase == SignalPhase.SELL_CONFIRM.value
    assert sell_markers[0].action == TradeAction.SELL_BASE.value
    assert sell_markers[0].direction == TradeDirection.REVERSE_T.value
    assert "DRAWICON(CROSS(88.8,RSI),90,15)" in sell_markers[0].reasons
    assert sell_markers[0].exit_score == 0
    assert any(marker.signal == SignalType.SELL_T for marker in timeline)


def test_l1_sell_pressure_vetoes_gold_but_keeps_red_formula_buy_marker() -> None:
    engine = SignalEngine(RULES)
    quote = q("300308", "中际旭创", ["AI硬件"], 9.83, 10.0, 10.0, 9.75, 10.05, 1.0, True)
    bars = bars_from_prices(buy_formula_prices())
    pressure_flow = TransactionFlowObservation(
        available=True,
        source="easy_tdx_history_transaction_data",
        data_quality="l1_transaction",
        trade_date="20260807",
        full_session=True,
        points=[
            TransactionFlowPoint(
                time=str(bar["time"]),
                rolling_count=30,
                rolling_imbalance_pct=-35,
                rolling_large_imbalance_pct=-45,
                rolling_score=-40,
            )
            for bar in bars
        ],
    )

    _, markers, _, summary = engine.build_replay_detail(
        quote,
        bars,
        engine.build_market_state(index_turning(), [quote]),
        sector_snapshot(),
        transaction_flow=pressure_flow,
        trend_states=near_trend_states_for_bars(bars),
    )

    buy_marker = next(marker for marker in markers if marker.signal == SignalType.BUY_T)
    assert buy_marker.gold_resonance is False
    assert "L1明显抛压否决" in buy_marker.resonance_reasons
    assert any("红色公式买候选保留" in risk for risk in buy_marker.risks)
    assert any("只增强理由或否决金点" in item for item in summary)


def test_l1_buy_support_only_enhances_existing_gold_marker() -> None:
    engine = SignalEngine(RULES)
    quote = q("300308", "中际旭创", ["AI硬件"], 9.83, 10.0, 10.0, 9.75, 10.05, 1.0, True)
    bars = bars_from_prices(buy_formula_prices())
    support_flow = TransactionFlowObservation(
        available=True,
        source="easy_tdx_history_transaction_data",
        data_quality="l1_transaction",
        trade_date="20260807",
        full_session=True,
        points=[
            TransactionFlowPoint(
                time=str(bar["time"]),
                rolling_count=30,
                rolling_imbalance_pct=32,
                rolling_large_imbalance_pct=18,
                rolling_score=36,
            )
            for bar in bars
        ],
    )

    _, markers, _, _ = engine.build_replay_detail(
        quote,
        bars,
        engine.build_market_state(index_turning(), [quote]),
        sector_snapshot(),
        transaction_flow=support_flow,
        trend_states=near_trend_states_for_bars(bars),
    )
    _, flat_markers, _, _ = engine.build_replay_detail(
        quote,
        bars_from_prices([10.0] * 10),
        engine.build_market_state(index_turning(), [quote]),
        sector_snapshot(),
        transaction_flow=support_flow,
    )

    buy_marker = next(marker for marker in markers if marker.signal == SignalType.BUY_T)
    assert buy_marker.gold_resonance is True
    assert "L1逐笔买盘支持" in buy_marker.resonance_reasons
    assert flat_markers == []


def test_replay_gold_resonance_rejects_far_daily_trend_lines() -> None:
    engine = SignalEngine(RULES)
    prices = [round(price * 5.35, 2) for price in buy_formula_prices()]
    bars = bars_from_prices(prices)
    quote = q("600206", "有研新材", ["稀土"], 52.63, 53.52, 52.0, 50.8, 55.2, 1.0, True)
    far_trend_states = [
        {
            "time": str(bar["time"]),
            "white_line": 42.59,
            "yellow_line": 41.06,
            "trend_source_quality": "unit_daily_trend",
        }
        for bar in bars
    ]

    _, markers, _, _ = engine.build_replay_detail(
        quote,
        bars,
        engine.build_market_state(index_turning(), [quote]),
        sector_snapshot(),
        trend_states=far_trend_states,
    )

    buy_marker = next(marker for marker in markers if marker.signal == SignalType.BUY_T)
    assert buy_marker.gold_resonance is False
    assert not any("接近" in reason for reason in buy_marker.resonance_reasons)


def test_formula_sell_marker_is_risk_context_when_position_is_t_plus_one_restricted() -> None:
    engine = SignalEngine(RULES)
    quote = q("300308", "中际旭创", ["AI硬件"], 12.0, 10.0, 10.0, 9.8, 12.6, 1.0, True)
    position = PositionRecord(code=quote.code, name=quote.name, cost=10.5, quantity=1000, available_quantity=0)

    _, markers, _, _ = engine.build_replay_detail(
        quote,
        bars_from_prices([10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 12.0]),
        engine.build_market_state(index_turning(), [quote]),
        sector_snapshot(),
        position=position,
    )

    sell_marker = next(marker for marker in markers if marker.signal == SignalType.SELL_T)
    assert sell_marker.executable is False
    assert sell_marker.t_plus_one_restricted is True
    assert any("T+1" in risk for risk in sell_marker.risks)
