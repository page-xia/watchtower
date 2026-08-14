from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


def test_research_status_endpoint_returns_aggregate_state_only(monkeypatch) -> None:
    class FakeService:
        def research_status(self):
            return {
                "available": True,
                "research_status": "sample_insufficient",
                "validation_status": "sample_insufficient",
                "sample": {"date_count": 2, "stock_day_count": 200},
                "data_quality": {"flow_mode": "easy_tdx_history_transaction", "level2_available": False},
                "limitations": ["样本不足"],
            }

    monkeypatch.setattr(main_module, "service", FakeService())
    payload = TestClient(app).get("/api/research/status")
    assert payload.status_code == 200
    body = payload.json()
    assert body["research_status"] == "sample_insufficient"
    assert body["data_quality"]["level2_available"] is False
    assert "WATCH_INGEST_TOKEN" not in json.dumps(body, ensure_ascii=False)


def test_research_protocol_endpoint_keeps_hypotheses_and_controls(monkeypatch) -> None:
    class FakeService:
        def research_protocol(self):
            return {
                "available": True,
                "research_status": "sample_insufficient",
                "hypotheses": [{"hypothesis_id": "H1", "title": "核心先动后跟随"}],
                "counterfactuals": {"shuffle_same_minute_transactions": {"outcome_count": 3}},
                "parameter_discovery": {"status": "exploratory_only"},
            }

    monkeypatch.setattr(main_module, "service", FakeService())
    response = TestClient(app).get("/api/research/protocol")
    assert response.status_code == 200
    body = response.json()
    assert body["hypotheses"][0]["hypothesis_id"] == "H1"
    assert "shuffle_same_minute_transactions" in body["counterfactuals"]
    assert body["parameter_discovery"]["status"] == "exploratory_only"


