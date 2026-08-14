import json

from app.config import AppSettings
from app.data_sources import EasyTdxDailyDataSource
from app.models import RiskRewardPlan
from app.risk_reward import RiskRewardEvaluator
from app.strategy_research import HistoricalFeed, ResearchConfig, StockSeries, StrategyResearcher


def _researcher() -> StrategyResearcher:
    researcher = object.__new__(StrategyResearcher)
    researcher.config = ResearchConfig()
    return researcher


def test_history_transaction_normalization_keeps_direction_fields() -> None:
    row = HistoricalFeed._normalize_transaction(
        {"time": "09:31:27", "price": "10.2", "vol": "300", "buyorsell": 0}
    )

    assert row == {"time": "09:31", "price": 10.2, "vol": 300.0, "buyorsell": 0}


def test_history_transaction_normalization_keeps_page_sequence_and_raw_seconds() -> None:
    row = HistoricalFeed._normalize_transaction(
        {"time": "09:31:27", "price": "10.2", "vol": "300", "buyorsell": 0},
        source_sequence=1805,
        source_page=1,
    )

    assert row["time"] == "09:31"
    assert row["raw_time"] == "09:31:27"
    assert row["source_page"] == 1
    assert row["source_sequence"] == 1805


def test_history_cache_requires_current_schema(tmp_path) -> None:
    path = tmp_path / "rows.json"
    payload = {"trade_date": "20260807", "code": "300476", "rows": [{"price": 10}]}
    path.write_text(json.dumps({**payload, "schema_version": 3}), encoding="utf-8")

    assert HistoricalFeed._read_rows(path, "20260807", "300476") is None

    path.write_text(json.dumps({**payload, "schema_version": 4}), encoding="utf-8")
    assert HistoricalFeed._read_rows(path, "20260807", "300476") == [{"price": 10}]


def test_transaction_metrics_does_not_deduplicate_identical_prints() -> None:
    researcher = _researcher()
    rows = [
        {"time": "09:31", "price": 10.0, "vol": 100, "buyorsell": 0},
        {"time": "09:31", "price": 10.0, "vol": 100, "buyorsell": 0},
        {"time": "09:31", "price": 10.0, "vol": 50, "buyorsell": 2},
    ]

    metrics = researcher._transaction_metrics(rows, [10.0], [100_000.0])

    assert metrics[0]["transaction_count"] == 3
    assert metrics[0]["buy_amount"] == 200_000.0
    assert metrics[0]["neutral_amount"] == 50_000.0
    assert metrics[0]["l1_buy_support"] is True
    assert metrics[0]["l1_sell_pressure"] is False


def test_stock_metrics_exports_formula_engine_fields() -> None:
    researcher = _researcher()
    prices = [10.0, 9.92, 9.86, 9.80, 9.78, 9.90, 10.05, 10.16]
    bars = [
        {
            "time": f"09:{31 + index:02d}",
            "price": price,
            "vol": 1000,
            "amount": price * 100_000,
        }
        for index, price in enumerate(prices)
    ]
    series = StockSeries(
        code="300476",
        name="胜宏科技",
        industry="PCB",
        market="创业板",
        prev_close=10.0,
        previous_day_amount=100_000_000,
        previous_bars=bars,
        bars=bars,
        transactions=[],
        trade_date="20260807",
        daily_history=[
            {"date": f"2025{1 + index // 20:02d}{1 + index % 20:02d}", "close": 9.0 + index * 0.01}
            for index in range(120)
        ],
    )

    latest = researcher._stock_metrics(series)[-1]

    assert latest["formula_validation_status"] == "research_only"
    assert latest["formula_white_line"] > 0
    assert latest["formula_yellow_line"] > 0
    assert latest["formula_trend_distance_pct"] == min(
        latest["formula_white_distance_pct"],
        latest["formula_yellow_distance_pct"],
    )


def test_formula_grid_reports_trend_thresholds_and_l1_combinations() -> None:
    researcher = _researcher()
    base_outcome = {
        "ret_5m": 0.4,
        "mfe_5m": 0.8,
        "mae_5m": -0.1,
        "ret_15m": 0.7,
        "mfe_15m": 1.1,
        "mae_15m": -0.2,
        "ret_30m": 1.0,
        "mfe_30m": 1.6,
        "mae_30m": -0.3,
        "net_30m": 0.8,
        "good_30": True,
    }
    supported = {
        "trade_date": "20260807",
        "code": "300476",
        "index": 10,
        "strict_four_factor": False,
        "strategy_v2": {"hard_ready": False, "flow_support": False, "flow_pressure": True},
        "metrics": {
            "formula_quick_entry": False,
            "formula_main_absorption": 0.0,
            "formula_buy_candidate": False,
            "formula_trend_distance_pct": 0.34,
            "formula_white_distance_pct": 0.34,
            "formula_yellow_distance_pct": 0.80,
            "flow_is_l1": True,
            "flow_score": -99,
            "large_imbalance": -99,
            "l1_indicators_present": True,
            "l1_buy_support": True,
            "l1_sell_pressure": False,
        },
        "outcome": base_outcome,
    }
    pressured = {
        **supported,
        "code": "300308",
        "strategy_v2": {"hard_ready": False, "flow_support": True, "flow_pressure": False},
        "metrics": {
            **supported["metrics"],
            "formula_trend_distance_pct": 0.20,
            "formula_white_distance_pct": 0.20,
            "flow_score": 99,
            "large_imbalance": 99,
            "l1_buy_support": False,
            "l1_sell_pressure": True,
        },
        "outcome": {**base_outcome, "ret_30m": -0.2, "net_30m": -0.4, "good_30": False},
    }
    quick_absorption = {
        "trade_date": "20260807",
        "code": "600118",
        "index": 50,
        "strict_four_factor": False,
        "strategy_v2": {"hard_ready": False, "eligible": False},
        "metrics": {
            "formula_quick_entry": True,
            "formula_main_absorption": 1.2,
            "formula_trend_distance_pct": 5.0,
            "l1_indicators_present": False,
            "l1_buy_support": False,
            "l1_sell_pressure": False,
        },
        "outcome": base_outcome,
    }
    missing_formula = {
        "strategy_v2": {"hard_ready": True, "eligible": True},
        "strict_four_factor": True,
        "metrics": {},
        "outcome": base_outcome,
    }

    grid = researcher._formula_grid(
        [supported, pressured, quick_absorption, missing_formula]
    )
    rows = {(item["trend_near_threshold_pct"], item["l1_rule"]): item for item in grid["rows"]}

    assert grid["status"] == "research_only"
    assert grid["thresholds_pct"] == [0.25, 0.35, 0.50, 0.70, 1.00, 3.00]
    assert grid["outcome_horizons_minutes"] == [5, 15, 30]
    assert grid["missing_formula_fields"] == 1
    assert len(grid["l1_rules"]) == 3
    assert len(grid["rows"]) == 18
    assert {item["status"] for item in grid["rows"]} == {"research_only"}
    assert all(item["selected"] is False for item in grid["rows"])
    assert all(item["status"] == "research_only" for item in grid["plateaus"])
    assert all(item["selected"] is False for item in grid["plateaus"])
    assert rows[(0.25, "ignore_l1")]["count"] == 2
    assert rows[(0.25, "veto_l1_pressure")]["count"] == 1
    assert rows[(0.35, "require_l1_buy_support")]["count"] == 1
    assert rows[(0.35, "require_l1_buy_support")]["clustered"]["avg_net_5m_pct"] == 0.2
    assert rows[(0.35, "require_l1_buy_support")]["clustered"]["avg_mfe_15m_pct"] == 1.1
    assert rows[(0.35, "require_l1_buy_support")]["clustered"]["avg_mae_30m_pct"] == -0.3
    assert grid["validation_status"] == "sample_insufficient"
    assert grid["sample_sufficiency"]["observed_trading_days"] == 1
    assert grid["selected_threshold_pct"] is None
    assert grid["selection"]["selected"] is False
    assert "不选择最优阈值" in grid["selection"]["reason"]


def test_formula_event_collection_does_not_require_legacy_factors() -> None:
    researcher = _researcher()
    metrics = []
    for index in range(40):
        metrics.append(
            {
                "time": f"09:{31 + index:02d}",
                "price": 10.0 + index * 0.01,
                "formula_quick_entry": index == 0,
                "formula_main_absorption": 1.0 if index == 0 else 0.0,
                "formula_trend_distance_pct": 5.0,
                "l1_indicators_present": True,
                "l1_available": True,
                "l1_buy_support": index == 0,
                "l1_sell_pressure": False,
            }
        )
    series = StockSeries(
        code="300476",
        name="胜宏科技",
        industry="PCB",
        market="创业板",
        prev_close=10.0,
        previous_day_amount=100_000_000,
        previous_bars=[],
        bars=[],
        transactions=[],
        metrics=metrics,
    )

    events = researcher._formula_events_for_day(
        "20260807",
        "20260806",
        {series.code: series},
        eligible_codes={series.code},
    )

    assert len(events) == 1
    assert events[0]["index"] == 0
    assert events[0]["research_status"] == "research_only"
    assert "strategy_v2" not in events[0]
    assert "strict_four_factor" not in events[0]
    assert events[0]["metrics"]["l1_buy_support"] is True
    assert events[0]["outcome"]["ret_30m"] is not None


def test_formula_grid_does_not_select_from_many_events_on_one_day() -> None:
    researcher = _researcher()
    outcome = {
        "ret_5m": 0.4,
        "mfe_5m": 0.7,
        "mae_5m": -0.1,
        "ret_15m": 0.6,
        "mfe_15m": 1.0,
        "mae_15m": -0.2,
        "ret_30m": 0.8,
        "mfe_30m": 1.3,
        "mae_30m": -0.3,
        "good_30": True,
    }
    candidates = [
        {
            "trade_date": "20260807",
            "code": f"30{index:04d}",
            "index": 10,
            "metrics": {
                "formula_quick_entry": False,
                "formula_main_absorption": 0.0,
                "formula_trend_distance_pct": 0.20,
                "l1_indicators_present": True,
                "l1_buy_support": True,
                "l1_sell_pressure": False,
            },
            "outcome": outcome,
        }
        for index in range(35)
    ]

    grid = researcher._formula_grid(candidates)

    assert grid["sample_sufficiency"]["max_independent_outcome_count"] == 35
    assert grid["sample_sufficiency"]["observed_trading_days"] == 1
    assert grid["validation_status"] == "sample_insufficient"
    assert grid["selection"]["selected"] is False
    assert grid["selected_threshold_pct"] is None


def test_strategy_v2_uses_l1_flow_as_grade_and_pressure_veto() -> None:
    researcher = _researcher()
    base = {
        "market_recent": True,
        "sector_recent": True,
        "volume_factor": True,
        "stock_setup": True,
        "flow_is_l1": True,
        "flow_score": 28,
        "large_imbalance": 20,
        "formula_exhaustion": False,
    }

    supported = researcher._strategy_v2_state(base)
    pressured = researcher._strategy_v2_state(
        {**base, "flow_score": -40, "large_imbalance": -45}
    )

    assert supported["eligible"] is True
    assert supported["grade"] == "A"
    assert supported["flow_role"] == "支持"
    assert pressured["eligible"] is False
    assert pressured["grade"] == "观察"
    assert pressured["flow_pressure"] is True

    incomplete = researcher._strategy_v2_state({**base, "market_recent": False})
    assert incomplete["eligible"] is False
    assert incomplete["grade"] == "观察"


def test_strategy_v2_without_l1_is_explicit_proxy_b_grade() -> None:
    researcher = _researcher()
    decision = researcher._strategy_v2_state(
        {
            "market_recent": True,
            "sector_recent": True,
            "volume_factor": True,
            "stock_setup": True,
            "flow_is_l1": False,
            "flow_score": 42,
            "large_imbalance": 0,
            "formula_exhaustion": True,
        }
    )

    assert decision["eligible"] is True
    assert decision["grade"] == "B"
    assert decision["flow_mode"] == "minute_price_amount_proxy"
    assert decision["flow_role"] == "分钟量价代理"
    assert decision["formula_exhaustion_warning"] is True


def test_pressure_comparison_only_contains_hard_ready_candidates() -> None:
    researcher = _researcher()
    base = {
        "outcome": {"ret_30m": 0.5, "net_30m": 0.3, "mfe_30m": 1.0, "mae_15m": -0.2},
        "strategy_v2": {"hard_ready": True, "flow_pressure": True, "eligible": False, "grade": "观察"},
        "trade_date": "20260806",
        "code": "300308",
        "index": 10,
    }
    incomplete = {
        **base,
        "code": "300476",
        "strategy_v2": {"hard_ready": False, "flow_pressure": True, "eligible": False, "grade": "观察"},
    }

    groups = {item["group"]: item for item in researcher._strategy_v2_stats([base, incomplete])}

    assert groups["V2暂缓（硬门槛+明显L1抛压）"]["count"] == 1


def test_strategy_diagnostics_keeps_predefined_rule_variants() -> None:
    researcher = _researcher()
    candidates = [
        {
            "trade_date": "20260806",
            "code": "300308",
            "time": "10:16",
            "index": 45,
            "metrics": {
                "rebound": 4.0,
                "sector_score": 82.0,
                "index_window_max_amount_ratio": 1.2,
                "amount_ratio": 1.4,
            },
            "strategy_v2": {"eligible": True},
            "outcome": {"good_30": True, "net_30m": 0.5},
        }
    ]

    diagnostics = researcher._strategy_diagnostics(candidates)
    variants = {item["variant"]: item for item in diagnostics["rule_variants"]}

    assert variants["上午核心执行区组合"]["count"] == 1
    assert diagnostics["core_variant_by_date"][0]["trade_date"] == "20260806"


def test_report_data_quality_contract_never_calls_proxy_l1() -> None:
    researcher = _researcher()
    l1 = researcher._data_quality_contract()
    assert l1["flow_mode"] == "easy_tdx_history_transaction"
    assert "历史逐笔成交" in l1["note"]
    assert "分钟量价" not in l1["note"]

    researcher.config = ResearchConfig(include_transactions=False)
    proxy = researcher._data_quality_contract()
    assert proxy["flow_mode"] == "minute_price_amount_proxy"
    assert proxy["historical_transactions_requested"] is False
    assert "未读取历史逐笔成交" in proxy["note"]
    assert "流向只由分钟价格方向" in proxy["note"]


def test_protocol_selection_schedule_uses_each_target_prior_session(monkeypatch) -> None:
    researcher = _researcher()
    priors = {"20260806": "20260805", "20260807": "20260806"}
    calls: list[str] = []

    monkeypatch.setattr(researcher, "_previous_date", lambda value: priors[value])

    def fake_select(selection_date=None):
        calls.append(str(selection_date))
        suffix = "6" if selection_date == "20260806" else "5"
        return {
            "selection_date": selection_date,
            "codes": [f"30000{suffix}"],
            "pool_codes": [f"30000{suffix}", "300999"],
            "items": [{"code": f"30000{suffix}"}],
        }

    monkeypatch.setattr(researcher, "select_sample", fake_select)

    schedule = researcher._protocol_selection_schedule(["20260806", "20260807"])
    summary = researcher._protocol_selection_summary(schedule)

    assert calls == ["20260805", "20260806"]
    assert schedule["20260806"]["selection_date"] == "20260805"
    assert schedule["20260807"]["selection_date"] == "20260806"
    assert schedule["20260806"]["codes"] != schedule["20260807"]["codes"]
    assert summary["future_filter_used"] is False
    assert summary["stock_day_count"] == 2


def test_prepare_daily_history_loads_only_sessions_before_first_target(
    tmp_path,
    monkeypatch,
) -> None:
    researcher = _researcher()
    researcher.config = ResearchConfig(daily_history_sessions=20)
    researcher.settings = AppSettings()
    researcher.settings.data_dir = tmp_path
    researcher.daily = {}
    researcher.daily_history_status = {}
    expected = [f"202607{index:02d}" for index in range(1, 21)]
    loaded: list[str] = []

    monkeypatch.setattr(
        EasyTdxDailyDataSource,
        "trade_dates_before",
        lambda self, before_date, sessions: expected,
    )

    def fake_ensure(trade_date: str) -> None:
        loaded.append(trade_date)
        researcher.daily[trade_date] = {"300476": {"close": 10}}

    monkeypatch.setattr(researcher, "_ensure_daily", fake_ensure)

    status = researcher._prepare_daily_history(["20260806", "20260807"])

    assert loaded == expected
    assert status["status"] == "available"
    assert status["loaded_sessions"] == 20
    assert status["strictly_before"] == "20260806"
    assert all(value < "20260806" for value in status["dates"])


def test_daily_history_uses_official_returns_across_ex_right_price_jump() -> None:
    rows = [
        {
            "trade_date": "20260701",
            "open": 99,
            "high": 101,
            "low": 98,
            "close": 100,
            "pre_close": 100,
            "pct_chg": 0,
        },
        {
            "trade_date": "20260702",
            "open": 50,
            "high": 51,
            "low": 49,
            "close": 50,
            "pre_close": 50,
            "pct_chg": 0,
        },
        {
            "trade_date": "20260703",
            "open": 50,
            "high": 56,
            "low": 49,
            "close": 55,
            "pre_close": 50,
            "pct_chg": 10,
        },
    ]

    adjusted = StrategyResearcher._return_adjusted_daily_history(rows)

    assert [round(row["adj_close"], 2) for row in adjusted] == [100, 100, 110]
    assert round(adjusted[1]["adj_high"], 2) == 102
    assert adjusted[1]["adjustment_method"] == "official_pct_chg_return_chain"


def test_stock_time_index_rejects_auction_and_non_session_prints() -> None:
    assert StrategyResearcher is not None
    assert HistoricalFeed is not None
    from app.strategy_research import _stock_time_index

    assert _stock_time_index("09:25") is None
    assert _stock_time_index("09:30") == 0
    assert _stock_time_index("09:31") == 1
    assert _stock_time_index("11:31") is None
    assert _stock_time_index("13:00") == 120
    assert _stock_time_index("13:01") == 121
    assert _stock_time_index("15:00") == 240
    assert _stock_time_index("15:01") is None


def test_bar_arrays_prefers_explicit_amount_and_falls_back_to_hands() -> None:
    researcher = _researcher()

    prices, amounts = researcher._bar_arrays(
        [
            {"price": 10.0, "vol": 20, "amount": 12345},
            {"price": 10.2, "vol": 30, "amount": 0},
        ],
        index=False,
    )

    assert prices == [10.0, 10.2]
    assert amounts == [12345.0, 30600.0]


def test_market_turn_and_volume_are_combined_across_adjacent_minutes() -> None:
    researcher = _researcher()
    metrics = [
        {"time": "10:15", "turning": True, "amount_expanding": False, "amount_ratio": 0.96},
        {"time": "10:16", "turning": False, "amount_expanding": True, "amount_ratio": 1.15},
    ]

    state = researcher._market_confluence_state(metrics, 1)

    assert state["market_recent"] is True
    assert state["market_turn_time"] == "10:15"
    assert state["market_volume_time"] == "10:16"
    assert state["index_window_max_amount_ratio"] == 1.15


def test_first_full_v2_candidate_is_not_hidden_by_weaker_candidate_cooldown() -> None:
    researcher = _researcher()
    researcher.config = ResearchConfig(outcome_horizons=(5,), candidate_cooldown=8)
    researcher.manual_theme_members = {}
    researcher.manual_theme_core = {}
    metrics = []
    for idx in range(20):
        active = idx >= 5
        metrics.append(
            {
                "price": 10.0 + idx * 0.01,
                "vwap": 10.0,
                "rebound": 1.0 if active else 0.0,
                "pullback": 0.0,
                "slope3": 0.10 if active else 0.0,
                "amount_ratio": 1.5 if active else 1.0,
                "same_minute_amount_ratio": 1.0,
                "cumulative_amount_ratio": 1.0,
                "flow_score": 20.0 if active else 0.0,
                "large_imbalance": 0.0,
                "flow_source": "minute_price_amount_proxy",
                "flow_available": False,
                "transaction_count": 0,
                "change_pct": 1.0 if active else 0.0,
                "formula_support": False,
                "formula_exhaustion": False,
                "limit_up": False,
            }
        )
    def make_series(code: str, name: str, amount: float) -> StockSeries:
        return StockSeries(
            code=code,
            name=name,
            industry="通信设备",
            market="创业板",
            prev_close=10.0,
            previous_day_amount=amount,
            previous_bars=[],
            bars=[],
            transactions=[],
            metrics=[dict(item) for item in metrics],
        )

    series = make_series("300308", "中际旭创", 1_000_000_000)
    peer_a = make_series("300502", "新易盛", 900_000_000)
    peer_b = make_series("300394", "天孚通信", 800_000_000)
    market_metrics = [
        {
            "time": f"09:{31 + idx:02d}",
            "turning": idx == 6,
            "amount_expanding": idx == 6,
            "amount_ratio": 1.2 if idx == 6 else 1.0,
            "slope3": 0.1 if idx == 6 else 0.0,
        }
        for idx in range(20)
    ]

    candidates, _, _ = researcher._study_day(
        "20260806",
        "20260805",
        {item.code: item for item in (series, peer_a, peer_b)},
        market_metrics,
        eligible_codes={series.code},
    )

    by_index = {item["index"]: item for item in candidates}
    assert 5 in by_index
    assert 6 in by_index
    assert by_index[5]["strategy_v2"]["hard_ready"] is False
    assert by_index[6]["strategy_v2"]["hard_ready"] is True


def test_single_member_industry_cannot_confirm_itself() -> None:
    researcher = _researcher()
    metrics = [
        {
            "change_pct": 2.0,
            "amount_ratio": 2.0,
            "slope3": 0.2,
            "limit_up": False,
        }
        for _ in range(5)
    ]
    series = StockSeries(
        code="300308",
        name="中际旭创",
        industry="通信设备",
        market="创业板",
        prev_close=10.0,
        previous_day_amount=1_000_000_000,
        previous_bars=[],
        bars=[],
        transactions=[],
        metrics=metrics,
    )

    rows = researcher._sector_metrics([series], len(metrics))

    assert all(not row.get("confirmed") for row in rows)


def test_execution_reprice_preserves_planned_risk_instead_of_creating_fake_rr() -> None:
    researcher = _researcher()
    researcher.risk_reward = RiskRewardEvaluator(
        {"rr_min_risk_pct": 0.5, "rr_max_risk_pct": 2.2}
    )
    series = StockSeries(
        code="600118",
        name="中国卫星",
        industry="航空",
        market="主板",
        prev_close=100.0,
        previous_day_amount=1_000_000_000,
        previous_bars=[],
        bars=[],
        transactions=[],
        metrics=[{"price": 100.0}, {"price": 99.1}],
    )
    candidate_plan = RiskRewardPlan(
        available=True,
        favorable=True,
        structure="突破前置",
        entry_price=100.0,
        invalidation_price=99.0,
        target_price=103.0,
        min_required_ratio=1.15,
    )
    execution_plan = RiskRewardPlan(
        available=True,
        favorable=True,
        structure="回踩承接",
        entry_price=99.1,
        invalidation_price=98.8,
        target_price=102.0,
        min_required_ratio=1.15,
    )
    researcher._v3_entry_plan = lambda *args, **kwargs: (execution_plan, "航空")

    terms = researcher._v3_execution_terms(
        series=series,
        candidate={"index": 0, "strategy_v2": {}},
        candidate_plan=candidate_plan,
        entry_idx=1,
        market_metrics=[],
        sector_metrics={},
    )

    assert terms["accepted"] is True
    assert terms["risk_pct"] >= 0.99
    assert terms["execution_rr"] < 10


def test_capacity_pilot_discount_keeps_300476_style_early_entry_inside_limit() -> None:
    researcher = _researcher()
    researcher.risk_reward = RiskRewardEvaluator(
        {
            "rr_min_risk_pct": 0.5,
            "rr_max_risk_pct": 2.2,
            "rr_pilot_discount": 0.2,
            "rr_pilot_floor": 1.15,
        }
    )
    series = StockSeries(
        code="300476",
        name="胜宏科技",
        industry="元器件",
        market="创业板",
        prev_close=230.0,
        previous_day_amount=10_000_000_000,
        previous_bars=[],
        bars=[],
        transactions=[],
        metrics=[
            {"price": 243.0},
            {
                "price": 244.47,
                "flow_available": True,
                "flow_score": 34.0,
                "large_imbalance": 32.0,
                "amount_ratio": 1.4,
                "rebound": 3.5,
            },
        ],
    )
    candidate_plan = RiskRewardPlan(
        available=True,
        favorable=True,
        structure="突破前置",
        entry_price=243.0,
        invalidation_price=239.71,
        target_price=250.31,
        min_required_ratio=1.15,
    )
    execution_plan = candidate_plan.model_copy(
        update={"entry_price": 244.47, "structure": "突破前置"}
    )
    researcher._v3_entry_plan = lambda *args, **kwargs: (execution_plan, "PCB")

    terms = researcher._v3_execution_terms(
        series=series,
        candidate={
            "index": 0,
            "strategy_v2": {"entry_archetype": "容量核心先点火"},
            "metrics": {
                "sector_is_manual": True,
                "sector_ignition_age": 2,
                "context_breadth": 0.33,
                "context_strong_breadth": 0.17,
            },
        },
        candidate_plan=candidate_plan,
        entry_idx=1,
        market_metrics=[],
        sector_metrics={},
    )

    assert terms["entry_limit_price"] > 244.47
    assert terms["accepted"] is True
    assert terms["execution_rr"] >= 1.15


def test_scene_veto_does_not_let_broad_market_saturation_rescue_a_breakout() -> None:
    researcher = _researcher()
    series = StockSeries(
        code="600378",
        name="昊华科技",
        industry="化工原料",
        market="主板",
        prev_close=40.0,
        previous_day_amount=1_000_000_000,
        previous_bars=[],
        bars=[],
        transactions=[],
        metrics=[
            {
                "price": 42.0,
                "flow_available": True,
                "flow_score": 45.0,
                "large_imbalance": 42.0,
                "amount_ratio": 1.8,
                "rebound": 4.0,
            }
        ],
    )
    candidate = {
        "strategy_v2": {"entry_archetype": "容量核心先点火"},
        "metrics": {
            "sector_is_manual": False,
            "sector_ignition_age": 1,
            "independent_up_count_recent": 3,
            "context_breadth": 0.66,
            "context_strong_breadth": 0.53,
        },
    }

    scene = researcher._v3_scene_decision(
        series=series,
        candidate=candidate,
        entry_idx=0,
        execution_structure="突破前置",
    )

    assert scene["accepted"] is False
    assert "宽度" in scene["reason"]


def test_pulse_dedup_chooses_role_and_time_without_using_outcome() -> None:
    trades = [
        {
            "trade_date": "20260807",
            "sector_name": "玻璃",
            "sector_impulse_id": 2,
            "entry_index": 27,
            "entry_archetype": "核心带动板块传导",
            "code": "301526",
            "net_return_pct": 9.0,
        },
        {
            "trade_date": "20260807",
            "sector_name": "玻璃",
            "sector_impulse_id": 2,
            "entry_index": 27,
            "entry_archetype": "容量核心先点火",
            "code": "002080",
            "net_return_pct": -1.0,
        },
    ]

    selected = StrategyResearcher._deduplicate_v3_pulses(trades)

    assert len(selected) == 1
    assert selected[0]["code"] == "002080"


def test_scene_threshold_sensitivity_reports_plateaus_without_optimizing() -> None:
    researcher = _researcher()
    candidates = [
        {
            "strategy_v2": {"eligible": True},
            "metrics": {
                "context_breadth": 0.62,
                "context_strong_breadth": 0.47,
                "rebound": 5.2,
            },
            "outcome": {"good_30": True, "net_30m": 0.6},
        },
        {
            "strategy_v2": {"eligible": True},
            "metrics": {
                "context_breadth": 0.57,
                "context_strong_breadth": 0.42,
                "rebound": 4.8,
            },
            "outcome": {"good_30": False, "net_30m": -0.2},
        },
    ]

    rows = researcher._scene_threshold_sensitivity(candidates)

    saturation = [item for item in rows if item["kind"] == "market_saturation_veto"]
    extension = [item for item in rows if item["kind"] == "late_extension_veto"]
    assert [item["count"] for item in saturation] == [2, 1, 0]
    assert [item["count"] for item in extension] == [2, 1, 0]


def test_v3_report_is_not_deployable_with_two_days_and_five_trades() -> None:
    researcher = _researcher()
    report = researcher._build_report(
        selection={"count": 100},
        date_summaries=[{"trade_date": "20260806"}, {"trade_date": "20260807"}],
        skipped=[],
        candidates=[],
        sell_zones=[],
        trades=[],
        v3_trades=[{"net_return_pct": 0.1}] * 5,
        v3_rejections=[],
    )

    assert report["strategy_v3"]["validation_status"] == "sample_insufficient"
    assert report["strategy_v3"]["deployable"] is False
    assert len(report["strategy_v3"]["validation_reasons"]) == 2


def test_markdown_report_leads_with_research_protocol_not_fixed_opening_rules(tmp_path) -> None:
    report = {
        "generated_at": "2026-08-09T21:00:00",
        "research_protocol": {
            "generated_at": "2026-08-09T21:00:00",
            "validation": {
                "status": "sample_insufficient",
                "raw_label_count": 300,
                "independent_event_count": 100,
                "filled_event_count": 95,
                "reasons": ["样本不足"],
                "direction": {
                    "positive_t": {"status": "sample_insufficient"},
                    "reverse_t": {"status": "sample_insufficient"},
                },
            },
            "sample": {
                "dates": ["20260806", "20260807"],
                "date_count": 2,
                "stock_day_count": 200,
                "transaction_sample_count": 200,
                "selection": {
                    "method": "逐日事前选样",
                    "stock_day_count": 200,
                },
            },
            "data_quality": {
                "minute_coverage_mean": 1.0,
                "transaction_minute_coverage_mean": 1.0,
            },
            "walk_forward": {
                "method": "expanding_window_one_day_ahead",
                "fold_count": 0,
                "complete": False,
                "required_total_days": 60,
            },
            "execution_model": {
                "base_round_trip_cost_pct": 0.18,
                "extra_pessimistic_round_trip_cost_pct": 0.08,
            },
            "hypotheses": [],
            "counterfactuals": {
                "direction_only": {
                    "same_event": {"independent_outcomes": 100, "metrics": {}},
                    "regenerated": {"independent_outcomes": 40, "metrics": {}},
                }
            },
            "parameter_discovery": {
                "status": "exploratory_only",
                "feature_performance": {
                    "feature_count": 17,
                    "stable_positive_platforms": [],
                },
            },
            "bias_register": [],
            "limitations": ["不得部署"],
        },
    }
    path = tmp_path / "report.md"

    StrategyResearcher._write_markdown(path, report)
    text = path.read_text(encoding="utf-8")

    assert text.startswith("# 日内正T/反T研究协议报告")
    assert "原始标签：300；独立候选事件：100" in text
    assert "固定事件时点估计量" in text
    assert "09:33/09:35/09:37" not in text
    assert "当前可落地策略骨架" not in text
