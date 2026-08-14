from __future__ import annotations

from dataclasses import replace

from app.research_protocol import (
    EventCandidate,
    ProtocolConfig,
    ResearchSample,
    TradeLabel,
    TradeOutcome,
    build_data_manifest,
    clustered_bootstrap_ci,
    compute_daily_regime,
    counterfactual_transactions,
    extract_point_features,
    label_candidate,
    normalize_transaction_direction,
    outcomes_from_labels,
    independent_outcomes,
    outcomes_with_extra_friction,
    walk_forward_evaluation,
    pareto_frontier,
    protocol_study,
    registered_hypotheses,
    validation_status,
)


def _bars(prices: list[float], *, start: int = 31) -> list[dict[str, float | str]]:
    return [
        {
            "time": f"09:{start + index:02d}",
            "price": price,
            "high": price * 1.002,
            "low": price * 0.998,
            "vol": 100 + index,
        }
        for index, price in enumerate(prices)
    ]


def _candidate(index: int, direction: str, setup: str = "结构") -> EventCandidate:
    return EventCandidate(
        code="300476",
        name="胜宏科技",
        trade_date="20260807",
        index=index,
        time="09:31",
        direction=direction,
        setup=setup,
        hypothesis_id="H1",
        features={},
    )


def test_protocol_registers_all_pre_registered_hypotheses() -> None:
    assert [item["hypothesis_id"] for item in registered_hypotheses()] == [
        "H1",
        "H2",
        "H3",
        "H4",
        "H5",
        "H6",
        "H7",
    ]


def test_special_transaction_direction_is_neutral_and_duplicates_are_retained() -> None:
    assert normalize_transaction_direction({"buyorsell": 7, "price": 10}) == (0, "special_neutral")
    rows = [
        {"time": "09:31:01", "price": 10, "vol": 100, "buyorsell": 0},
        {"time": "09:31:02", "price": 10, "vol": 100, "buyorsell": 0},
        {"time": "09:31:03", "price": 10, "vol": 50, "buyorsell": 7},
    ]
    features = extract_point_features(_bars([10, 10.01]), rows, prev_close=10)
    assert features[1]["transaction_count"] == 3
    assert features[1]["buy_amount"] == 200_000
    assert features[1]["neutral_amount"] == 50_000


def test_future_bars_cannot_change_prefix_features() -> None:
    base = _bars([10, 10.1, 10.05, 10.2])
    future = _bars([99, 1, 88], start=35)
    short = extract_point_features(base, prev_close=10)
    extended = extract_point_features(base + future, prev_close=10)
    assert short == extended[: len(short)]


def test_positive_and_reverse_labels_are_directionally_independent() -> None:
    config = ProtocolConfig(outcome_horizons=(2,), friction_pct=0, pessimistic_slippage_pct=0)
    rising = ResearchSample(
        code="300476",
        name="胜宏科技",
        trade_date="20260807",
        bars=_bars([10, 10.1, 10.3, 10.6, 10.7]),
        metadata={"prev_close": 10},
    )
    falling = ResearchSample(
        code="300476",
        name="胜宏科技",
        trade_date="20260807",
        bars=_bars([10, 9.9, 9.7, 9.4, 9.3]),
        metadata={"prev_close": 10},
    )
    positive = label_candidate(rising, _candidate(1, "positive_t"), config=config, horizon=2)
    reverse = label_candidate(falling, _candidate(1, "reverse_t"), config=config, horizon=2)
    assert positive.direction == "positive_t"
    assert reverse.direction == "reverse_t"
    assert positive.target_price > positive.execution_price
    assert reverse.target_price < reverse.execution_price
    assert reverse.execution_reason.startswith("理论标签")


def test_reverse_label_records_t_plus_one_and_no_next_fill() -> None:
    config = ProtocolConfig(outcome_horizons=(2,), friction_pct=0, pessimistic_slippage_pct=0)
    sample = ResearchSample(
        code="300476",
        trade_date="20260807",
        bars=_bars([10, 9.9, 9.8]),
        position_known=True,
        position_quantity=100,
        available_quantity=0,
    )
    blocked = label_candidate(sample, _candidate(0, "reverse_t"), config=config, horizon=2)
    assert blocked.fill_status == "no_fill"
    assert blocked.t_plus_one_blocked is True
    assert "T+1" in blocked.no_fill_reason

    terminal = label_candidate(replace(sample, available_quantity=100), _candidate(2, "positive_t"), config=config, horizon=2)
    assert terminal.fill_status == "no_fill"
    assert "下一可成交" in terminal.no_fill_reason


def test_outcomes_do_not_overwrite_same_minute_different_setups() -> None:
    candidates = [
        _candidate(4, "positive_t", "卖压吸收"),
        _candidate(4, "positive_t", "首次回踩"),
    ]
    labels = [
        TradeLabel(
            code="300476",
            name="胜宏科技",
            trade_date="20260807",
            candidate_time="09:35",
            candidate_index=4,
            direction="positive_t",
            setup="卖压吸收",
            horizon=5,
            fill_status="filled",
            net_r=1,
        ),
        TradeLabel(
            code="300476",
            name="胜宏科技",
            trade_date="20260807",
            candidate_time="09:35",
            candidate_index=4,
            direction="positive_t",
            setup="卖压吸收",
            horizon=15,
            fill_status="filled",
            net_r=2,
        ),
        TradeLabel(
            code="300476",
            name="胜宏科技",
            trade_date="20260807",
            candidate_time="09:35",
            candidate_index=4,
            direction="positive_t",
            setup="首次回踩",
            horizon=5,
            fill_status="filled",
            net_r=3,
        ),
    ]
    outcomes = outcomes_from_labels(candidates, labels)
    assert len(outcomes) == 3
    assert {item.setup for item in outcomes} == {"卖压吸收", "首次回踩"}


def test_clustered_bootstrap_is_reproducible_and_pareto_keeps_non_dominated_rows() -> None:
    outcomes = [
        TradeOutcome(code="300476", trade_date="20260806", candidate_time="09:35", direction="positive_t", setup="a", net_r=1),
        TradeOutcome(code="300476", trade_date="20260806", candidate_time="09:36", direction="positive_t", setup="a", net_r=-0.2),
        TradeOutcome(code="300308", trade_date="20260807", candidate_time="09:35", direction="positive_t", setup="a", net_r=0.5),
    ]
    first = clustered_bootstrap_ci(outcomes, iterations=50, seed=42)
    second = clustered_bootstrap_ci(outcomes, iterations=50, seed=42)
    assert first == second
    frontier = pareto_frontier(
        [
            {"variant": "a", "mean_net_r": 1, "target_first_probability_pct": 60, "mae_p90_pct": -2, "no_fill_count": 1},
            {"variant": "b", "mean_net_r": 0.5, "target_first_probability_pct": 50, "mae_p90_pct": -1, "no_fill_count": 2},
            {"variant": "dominated", "mean_net_r": 0.1, "target_first_probability_pct": 40, "mae_p90_pct": -4, "no_fill_count": 3},
        ]
    )
    assert {row["variant"] for row in frontier} == {"a", "b"}


def test_manifest_reports_session_gaps_and_validation_stays_research_only() -> None:
    sample = ResearchSample(
        code="300476",
        trade_date="20260807",
        bars=[{"time": "09:31", "price": 10}, {"time": "09:33", "price": 10.1}],
    )
    manifest = build_data_manifest(sample)
    assert manifest.time_gaps
    status = validation_status(
        dates=["20260806", "20260807"],
        outcomes=[],
        config=ProtocolConfig(minimum_days=20, minimum_events=30),
    )
    assert status["status"] == "sample_insufficient"


def test_protocol_report_exposes_controls_without_claiming_deployability() -> None:
    sample = ResearchSample(
        code="300476",
        name="胜宏科技",
        trade_date="20260807",
        bars=_bars([10 + index * 0.01 for index in range(20)]),
        market_bars=_bars([3000 + index for index in range(20)]),
        sector_bars=[_bars([20 + index * 0.02 for index in range(20)]) for _ in range(2)],
        metadata={"prev_close": 10},
    )
    report = protocol_study(
        [sample],
        config=ProtocolConfig(minimum_days=20, minimum_events=30, bootstrap_iterations=0),
    )
    assert report["validation"]["status"] == "sample_insufficient"
    assert report["validation"]["direction"]["positive_t"]["status"] == "sample_insufficient"
    assert "baseline_four_factor" in report["variants"]
    assert "shuffle_core_stock_timing" in report["counterfactuals"]
    assert report["parameter_discovery"]["status"] == "exploratory_only"
    assert report["validation"]["raw_label_count"] >= report["validation"]["independent_event_count"]
    assert (
        report["validation"]["independent_event_count"]
        == report["variants"]["observed"]["independent_outcomes"]
    )
    assert report["walk_forward"]["fold_count"] == 0
    assert report["walk_forward"]["complete"] is False
    assert report["leakage_checks"]["multi_horizon_labels_counted_as_independent_events"] is False
    assert "same_event" in report["counterfactuals"]["direction_only"]
    assert "regenerated" in report["counterfactuals"]["direction_only"]
    feature_performance = report["parameter_discovery"]["feature_performance"]
    assert feature_performance["feature_count"] > 0
    assert feature_performance["selection_applied"] is False
    assert all(
        item["selected"] is False
        for item in feature_performance["stable_positive_platforms"]
    )


def test_transaction_seconds_stay_in_their_minute_and_boundaries_align() -> None:
    bars = [
        {"time": "09:31", "price": 10, "vol": 100},
        {"time": "13:01", "price": 10.1, "vol": 100},
    ]
    transactions = [
        {"time": "09:30:57", "price": 10, "vol": 10, "buyorsell": 0},
        {"time": "09:30:59", "price": 10, "vol": 20, "buyorsell": 1},
        {"time": "13:00:02", "price": 10.1, "vol": 30, "buyorsell": 0},
    ]

    features = extract_point_features(bars, transactions, prev_close=10)
    shuffled = counterfactual_transactions(transactions, seed=7)

    assert [item["transaction_count"] for item in features] == [2, 1]
    assert [item["time"][:5] for item in shuffled] == ["09:30", "09:30", "13:00"]


def test_all_transaction_minutes_align_to_close_labelled_bars() -> None:
    bars = [
        {"time": "09:31", "price": 10, "vol": 100},
        {"time": "09:32", "price": 10.1, "vol": 100},
        {"time": "13:01", "price": 10.2, "vol": 100},
        {"time": "13:02", "price": 10.3, "vol": 100},
    ]
    transactions = [
        {"time": "09:30", "price": 10, "vol": 10, "buyorsell": 0},
        {"time": "09:31", "price": 10.1, "vol": 10, "buyorsell": 0},
        {"time": "13:00", "price": 10.2, "vol": 10, "buyorsell": 1},
        {"time": "13:01", "price": 10.3, "vol": 10, "buyorsell": 1},
    ]

    features = extract_point_features(bars, transactions, prev_close=10)

    assert [item["transaction_count"] for item in features] == [1, 1, 1, 1]


def test_manifest_exposes_transaction_page_sequence_raw_time_and_gaps() -> None:
    sample = ResearchSample(
        code="300476",
        trade_date="20260807",
        bars=[{"time": "09:31", "price": 10}, {"time": "13:01", "price": 10.1}],
        transactions=[
            {
                "time": "09:31",
                "raw_time": "09:30:58",
                "price": 10,
                "vol": 10,
                "buyorsell": 0,
                "source_page": 0,
                "source_sequence": 0,
            },
            {
                "time": "13:01",
                "raw_time": "13:00:02",
                "price": 10.1,
                "vol": 10,
                "buyorsell": 1,
                "source_page": 1,
                "source_sequence": 1800,
            },
        ],
    )

    manifest = build_data_manifest(sample)
    payload = manifest.to_dict()

    assert payload["transaction_page_numbers"] == [0, 1]
    assert payload["transaction_sequence_count"] == 2
    assert payload["transaction_raw_time_count"] == 2
    assert payload["transaction_metadata_coverage"] == 1
    assert payload["transaction_first_raw_time"] == "09:30:58"
    assert payload["transaction_last_raw_time"] == "13:00:02"
    assert payload["transaction_time_gaps"]
    assert "09:32" in payload["transaction_missing_minutes"]


def test_daily_regime_requires_prior_history_and_uses_only_supplied_rows() -> None:
    insufficient = compute_daily_regime(
        [{"trade_date": f"202607{index:02d}", "close": 10 + index * 0.01} for index in range(1, 20)]
    )
    available = compute_daily_regime(
        [
            {
                "trade_date": f"202607{index:02d}",
                "close": 10 + index * 0.05,
                "high": 10.2 + index * 0.05,
                "low": 9.8 + index * 0.05,
            }
            for index in range(1, 31)
        ]
    )

    assert insufficient["status"] == "insufficient_history"
    assert available["status"] == "available"
    assert available["observations"] == 30
    assert available["ma20"] is not None


def test_daily_regime_uses_adjusted_ohlc_on_one_continuous_scale() -> None:
    rows = [
        {
            "trade_date": f"202607{index:02d}",
            "close": 50 if index > 15 else 100,
            "high": 51 if index > 15 else 101,
            "low": 49 if index > 15 else 99,
            "adj_close": 100 + index,
            "adj_high": 101 + index,
            "adj_low": 99 + index,
            "adjustment_method": "official_pct_chg_return_chain",
        }
        for index in range(1, 31)
    ]

    regime = compute_daily_regime(rows)

    assert regime["available"] is True
    assert regime["price_basis"] == "official_pct_chg_return_chain"
    assert regime["ma20"] > 100


def test_counterfactuals_compare_the_same_observed_event_times() -> None:
    prices = [10, 9.99, 9.98, 9.97, 9.96, 9.95, 9.96, 9.98, 10.02, 10.05, 10.04, 10.07, 10.1, 10.08, 10.12]
    transactions = []
    for index, price in enumerate(prices):
        transactions.extend(
            [
                {"time": f"09:{31 + index:02d}:01", "price": price, "vol": 100, "buyorsell": 1 if index < 6 else 0},
                {"time": f"09:{31 + index:02d}:40", "price": price + 0.001, "vol": 150, "buyorsell": 0},
            ]
        )
    sample = ResearchSample(
        code="300476",
        trade_date="20260807",
        bars=_bars(prices),
        transactions=transactions,
        market_bars=_bars([3000 + index for index in range(len(prices))]),
        sector_bars=[_bars([20 + index * 0.01 for index in range(len(prices))])],
        sector_name="PCB",
        metadata={"prev_close": 10},
    )

    report = protocol_study(
        [sample],
        config=ProtocolConfig(warmup_bars=2, bootstrap_iterations=0),
    )
    observed_count = len(report["candidates"])
    direction_control = report["counterfactuals"]["direction_only"]

    assert observed_count > 0
    assert direction_control["comparison_basis"] == "same_observed_event_times"
    assert direction_control["candidate_comparison"]["same_event_count"] == observed_count
    assert direction_control["candidate_comparison"]["regenerated_event_count"] == 0
    assert direction_control["outcome_count"] == report["variants"]["observed"]["outcomes"]


def test_one_word_sample_is_retained_as_no_fill() -> None:
    sample = ResearchSample(
        code="300476",
        trade_date="20260807",
        bars=_bars([10, 10.1, 10.2, 10.3]),
        one_word=True,
    )
    label = label_candidate(
        sample,
        _candidate(1, "positive_t"),
        config=ProtocolConfig(outcome_horizons=(2,)),
        horizon=2,
    )

    assert label.fill_status == "no_fill"
    assert "一字板" in label.no_fill_reason


def test_matched_sector_control_reports_available_and_unavailable_cases() -> None:
    prices = [10, 9.99, 9.98, 9.97, 9.96, 9.95, 9.96, 9.98, 10.02, 10.05, 10.04, 10.07, 10.1, 10.08, 10.12]
    sample = ResearchSample(
        code="300476",
        trade_date="20260807",
        bars=_bars(prices),
        market_bars=_bars([3000 + index for index in range(len(prices))]),
        sector_bars=[_bars([20 + index * 0.01 for index in range(len(prices))])],
        sector_name="PCB",
        metadata={"prev_close": 10},
    )
    peer = replace(
        sample,
        code="300308",
        name="同板块控制",
        bars=_bars([price * 0.99 for price in prices]),
    )
    config = ProtocolConfig(warmup_bars=2, bootstrap_iterations=0)

    matched = protocol_study([sample, peer], config=config)["matched_control"]
    unavailable = protocol_study([sample], config=config)["matched_control"]

    assert matched["requested_event_count"] > 0
    assert matched["outcome_count"] > 0
    assert unavailable["outcome_count"] == 0
    assert unavailable["unavailable_reasons"]["同日同板块没有其他样本"] > 0


def test_validation_requires_sixty_day_range_and_candidate_is_not_deployable() -> None:
    outcome = TradeOutcome(
        code="300476",
        trade_date="20260860",
        candidate_time="09:35",
        direction="positive_t",
        setup="卖压吸收",
        fill_status="filled",
        target_first=True,
        net_r=1,
    )
    config = ProtocolConfig(minimum_days=20, out_of_sample_days=60, minimum_events=1)

    short = validation_status(
        dates=[f"202607{index:02d}" for index in range(1, 21)],
        outcomes=[outcome],
        out_of_sample_outcomes=[outcome],
        out_of_sample_dates=["20260860"],
        config=config,
    )
    complete = validation_status(
        dates=[f"day-{index:02d}" for index in range(60)],
        outcomes=[outcome],
        out_of_sample_outcomes=[outcome],
        out_of_sample_dates=["20260860"],
        config=config,
    )

    assert short["status"] == "sample_insufficient"
    assert short["minimum_validation_days"] == 60
    assert complete["status"] == "candidate"
    assert complete["deployable"] is False


def test_multi_horizon_labels_count_as_one_independent_event() -> None:
    outcomes = [
        TradeOutcome(
            code="300476",
            trade_date="20260807",
            candidate_time="09:35",
            candidate_index=4,
            candidate_ordinal=1,
            direction="positive_t",
            setup="卖压吸收",
            horizon=horizon,
            fill_status="filled",
            net_r=float(horizon),
        )
        for horizon in (5, 15, 30)
    ]

    primary = independent_outcomes(outcomes)

    assert len(primary) == 1
    assert primary[0].horizon == 30
    assert primary[0].net_r == 30


def test_extra_friction_reduces_return_and_r() -> None:
    outcome = TradeOutcome(
        code="300476",
        trade_date="20260807",
        candidate_time="09:35",
        candidate_index=4,
        direction="positive_t",
        setup="卖压吸收",
        horizon=30,
        fill_status="filled",
        net_return_pct=1.0,
        net_r=1.0,
        risk_pct=1.0,
    )

    stressed = outcomes_with_extra_friction([outcome], extra_cost_pct=0.2)[0]

    assert stressed.net_return_pct == 0.8
    assert stressed.net_r == 0.8


def test_walk_forward_uses_each_later_date_once_after_training_window() -> None:
    dates = [f"day-{index:02d}" for index in range(1, 7)]
    outcomes = [
        TradeOutcome(
            code=f"{300000 + index:06d}",
            trade_date=trade_date,
            candidate_time="09:35",
            candidate_index=4,
            direction="positive_t" if index % 2 else "reverse_t",
            setup="测试",
            horizon=30,
            fill_status="filled",
            target_first=True,
            net_return_pct=0.5,
            net_r=0.5,
            risk_pct=1.0,
        )
        for index, trade_date in enumerate(dates, start=1)
    ]
    config = ProtocolConfig(
        minimum_days=3,
        out_of_sample_days=6,
        minimum_events=1,
        bootstrap_iterations=0,
    )

    result = walk_forward_evaluation(dates=dates, outcomes=outcomes, config=config)

    assert result["complete"] is True
    assert result["fold_count"] == 3
    assert result["oos_dates"] == dates[3:]
    assert result["oos_independent_event_count"] == 3
    for fold in result["folds"]:
        assert fold["test_date"] > fold["training_end"]
