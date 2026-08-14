from __future__ import annotations

from app.research_artifacts import build_compact_research_report


def test_compact_research_report_omits_raw_event_payloads() -> None:
    report = {
        "schema_version": "full-v1",
        "research_status": "sample_insufficient",
        "all_candidates": [{"code": "300476"}],
        "research_protocol": {
            "run_id": "run-1",
            "validation": {"status": "sample_insufficient"},
            "candidates": [{"event_key": "candidate"}],
            "labels": [{"event_key": "label"}],
            "outcomes": [{"event_key": "outcome"}],
            "data_manifests": [
                {
                    "minute_coverage": 1.0,
                    "transaction_coverage": 0.9,
                    "source_quality": "l1_transaction_tape",
                }
            ],
            "parameter_discovery": {
                "status": "exploratory_only",
                "feature_performance": {
                    "feature_count": 1,
                    "features": {
                        "flow": {
                            "bins": [{"independent_event_count": 12, "raw": [1, 2]}],
                            "adjacent_pairs": [],
                        }
                    },
                    "stable_positive_platforms": [],
                },
            },
            "matched_control": {
                "matched_record_count": 1,
                "records": [{"source_code": "300476", "control_code": "300308"}],
            },
        },
    }

    compact = build_compact_research_report(report)
    protocol = compact["research_protocol"]

    assert compact["schema_version"] == "research_api_summary_v1"
    assert protocol["run_id"] == "run-1"
    assert "candidates" not in protocol
    assert "labels" not in protocol
    assert "outcomes" not in protocol
    assert "records" not in protocol["matched_control"]
    assert protocol["data_manifest_summary"]["count"] == 1
    assert "features" not in protocol["parameter_discovery"]["feature_performance"]
