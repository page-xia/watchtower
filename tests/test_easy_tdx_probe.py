from types import SimpleNamespace

from scripts.probe_easy_tdx_capabilities import (
    summarize_auction_fields,
    summarize_quote_fields,
    summarize_transaction_fields,
)


def test_probe_distinguishes_quote_depth_from_transaction_tape_and_auction_capabilities() -> None:
    summary = summarize_quote_fields(
        [
            SimpleNamespace(
                buy_levels=[SimpleNamespace(price=10.0, volume=100)] * 5,
                sell_levels=[SimpleNamespace(price=10.1, volume=80)] * 5,
                price=10.0,
                cur_vol=12,
                last_price=10.0,
                current_hand=12,
                outer_disc=60,
                inside_dish=40,
            )
        ]
    )

    assert summary["five_level_available"] is True
    assert summary["quote_depth_available"] is True
    assert summary["ten_level_quote_depth"] is False
    assert summary["level2_available"] is False
    assert summary["auction_proxy_possible"] is True


def test_probe_recognizes_l1_transaction_tape() -> None:
    summary = summarize_transaction_fields(
        [SimpleNamespace(time_label="09:31", price=10.0, volume=100, side="sell")]
    )

    assert summary["transaction_tape_available"] is True
    assert summary["direction_field"] == "side"
    assert summary["transaction_first_time"] == "09:31"
    assert summary["transaction_last_time"] == "09:31"
    assert summary["transaction_time_ascending"] is True
    assert summary["direction_value_counts"] == {"sell": 1}


def test_probe_recognizes_current_auction_series_fields() -> None:
    summary = summarize_auction_fields(
        [SimpleNamespace(time_label="09:24:57", price=10.2, matched_volume=1000, unmatched_volume=300)]
    )

    assert summary["auction_actual_fields"] is True
    assert summary["auction_series_point_count"] == 1
    assert summary["auction_first_time"] == "09:24"
    assert summary["auction_last_time"] == "09:24"

