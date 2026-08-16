from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_METRIC_FIELDS = (
    "count",
    "filled_count",
    "no_fill_count",
    "fill_rate_pct",
    "target_observed_count",
    "target_first_probability_pct",
    "mean_net_r",
    "median_net_r",
    "mean_net_return_pct",
    "median_net_return_pct",
    "mean_mfe_pct",
    "mean_mae_pct",
    "mae_p90_pct",
    "profit_factor",
    "avg_win_r",
    "avg_loss_r",
)

_DATE_STABILITY_FIELDS = (
    "date_count",
    "positive_date_count",
    "positive_date_rate_pct",
    "largest_absolute_date_share_pct",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _metric_summary(value: Any) -> dict[str, Any]:
    metrics = _mapping(value)
    return {key: metrics[key] for key in _METRIC_FIELDS if key in metrics}


def _date_stability_summary(value: Any) -> dict[str, Any]:
    payload = _mapping(value)
    return {key: payload[key] for key in _DATE_STABILITY_FIELDS if key in payload}


def _platform_summary(value: Any) -> dict[str, Any]:
    payload = _mapping(value)
    result = {
        key: payload[key]
        for key in (
            "feature",
            "bins",
            "lower",
            "upper",
            "stable_positive",
            "selected",
            "reason",
            "oos_independent_event_count",
        )
        if key in payload
    }
    if payload.get("base") is not None:
        result["base"] = _metric_summary(payload.get("base"))
    if payload.get("pessimistic") is not None:
        result["pessimistic"] = _metric_summary(payload.get("pessimistic"))
    if payload.get("date_stability") is not None:
        result["date_stability"] = _date_stability_summary(payload.get("date_stability"))
    return result


def summarize_parameter_discovery(value: Any) -> dict[str, Any]:
    """Keep the registered search design without returning every explored bin."""

    payload = _mapping(value)
    performance = _mapping(payload.get("feature_performance"))
    raw_features = _mapping(performance.get("features"))
    existing_summaries = _mapping(performance.get("feature_summaries"))
    feature_summaries: dict[str, Any] = {}
    if existing_summaries:
        feature_summaries = {
            str(name): dict(_mapping(item))
            for name, item in existing_summaries.items()
        }
    else:
        for name, raw_item in raw_features.items():
            item = _mapping(raw_item)
            bins = [entry for entry in _sequence(item.get("bins")) if isinstance(entry, Mapping)]
            adjacent = [
                entry
                for entry in _sequence(item.get("adjacent_pairs"))
                if isinstance(entry, Mapping)
            ]
            feature_summaries[str(name)] = {
                "bin_count": len(bins),
                "adjacent_pair_count": len(adjacent),
                "stable_positive_pair_count": sum(
                    1 for entry in adjacent if bool(entry.get("stable_positive"))
                ),
                "independent_event_count": sum(
                    int(entry.get("independent_event_count") or 0) for entry in bins
                ),
            }

    stable_platforms = [
        _platform_summary(item)
        for item in _sequence(performance.get("stable_positive_platforms"))
        if isinstance(item, Mapping)
    ]
    oos_items = [
        _platform_summary(item)
        for item in _sequence(payload.get("oos_platform_evaluation"))
        if isinstance(item, Mapping)
    ]
    quantile_bins = _mapping(payload.get("quantile_bins"))
    return {
        key: payload[key]
        for key in (
            "method",
            "continuous_features",
            "tested_ranges",
            "plateau_rule",
            "status",
            "training_dates",
            "oos_dates_not_used_for_bins",
        )
        if key in payload
    } | {
        "quantile_feature_count": len(quantile_bins),
        "feature_performance": {
            "feature_count": int(
                performance.get("feature_count")
                or len(feature_summaries)
            ),
            "paired_independent_event_count": int(
                performance.get("paired_independent_event_count") or 0
            ),
            "feature_summaries": feature_summaries,
            "stable_positive_platforms": stable_platforms,
            "selection_applied": bool(performance.get("selection_applied", False)),
        },
        "oos_platform_evaluation_count": len(oos_items),
        "oos_platform_evaluation": oos_items[:100],
        "oos_platform_evaluation_truncated": len(oos_items) > 100,
    }


def summarize_matched_control(value: Any) -> dict[str, Any]:
    """Return matched-control coverage, never per-event control records."""

    payload = _mapping(value)
    result = {
        key: payload[key]
        for key in (
            "requested_event_count",
            "selected_event_count",
            "outcome_count",
            "matched_record_count",
            "unavailable_event_count",
            "unavailable_reasons",
            "cap",
            "truncated",
            "selection_method",
        )
        if key in payload
    }
    records = _sequence(payload.get("records"))
    result["records_omitted"] = True
    result["records_present_in_source"] = len(records)
    return result


def summarize_data_manifests(value: Any) -> dict[str, Any]:
    manifests = [item for item in _sequence(value) if isinstance(item, Mapping)]
    if not manifests:
        return {
            "count": 0,
            "minute_coverage_mean": 0.0,
            "transaction_coverage_mean": 0.0,
            "source_quality_counts": {},
        }
    source_quality_counts: dict[str, int] = {}
    for item in manifests:
        quality = str(item.get("source_quality") or "unavailable")
        source_quality_counts[quality] = source_quality_counts.get(quality, 0) + 1
    return {
        "count": len(manifests),
        "minute_coverage_mean": round(
            sum(float(item.get("minute_coverage") or 0) for item in manifests)
            / len(manifests),
            4,
        ),
        "transaction_coverage_mean": round(
            sum(float(item.get("transaction_coverage") or 0) for item in manifests)
            / len(manifests),
            4,
        ),
        "source_quality_counts": source_quality_counts,
    }


def build_compact_research_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build the API-facing sidecar without raw candidates, labels or outcomes."""

    protocol = _mapping(report.get("research_protocol") or report.get("protocol"))
    compact_protocol: dict[str, Any] = {
        key: protocol[key]
        for key in (
            "protocol_version",
            "study_name",
            "run_id",
            "generated_at",
            "started_at",
            "finished_at",
            "hypotheses",
            "config",
            "sample",
            "data_boundary",
            "leakage_checks",
            "execution_model",
            "data_quality",
            "split",
            "walk_forward",
            "variants",
            "validation",
            "counterfactuals",
            "pareto",
            "bias_register",
            "limitations",
        )
        if key in protocol
    }
    compact_protocol["data_manifest_summary"] = summarize_data_manifests(
        protocol.get("data_manifests")
    )
    compact_protocol["parameter_discovery"] = summarize_parameter_discovery(
        protocol.get("parameter_discovery")
    )
    compact_protocol["matched_control"] = summarize_matched_control(
        protocol.get("matched_control")
    )

    methodology = _mapping(report.get("methodology"))
    return {
        "schema_version": "research_api_summary_v1",
        "source_schema_version": str(report.get("schema_version") or ""),
        "generated_at": str(report.get("generated_at") or protocol.get("generated_at") or ""),
        "research_status": str(
            report.get("research_status")
            or _mapping(protocol.get("validation")).get("status")
            or "research_only"
        ),
        "data_quality": dict(_mapping(report.get("data_quality"))),
        "methodology": {
            "limitations": [
                str(item)
                for item in _sequence(methodology.get("limitations"))
                if str(item)
            ]
        },
        "research_protocol": compact_protocol,
    }


__all__ = [
    "build_compact_research_report",
    "summarize_data_manifests",
    "summarize_matched_control",
    "summarize_parameter_discovery",
]
