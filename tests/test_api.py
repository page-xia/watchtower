import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.models import (
    MarketState,
    OpeningAction,
    OpeningDecisionItem,
    OpeningDecisionPayload,
    PositionRecord,
    ReplayMarker,
    ReplayPoint,
    RiskRewardPlan,
    SignalPhase,
    SignalReplayDetail,
    SignalType,
    TradeAction,
    TradeDirection,
    TradeSignal,
    TransactionFlowPoint,
    TransactionFlowObservation,
    TrendState,
)


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["secret_safe"] is True


def test_api_responses_disable_cache() -> None:
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert response.headers["surrogate-control"] == "no-store"


def test_index_html_disables_cache(monkeypatch, tmp_path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<!doctype html><div id=\"root\"></div>", encoding="utf-8")
    monkeypatch.setattr(main_module, "WEB_DIST_DIR", dist_dir)

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"


def test_dashboard_does_not_expose_secret_values() -> None:
    client = TestClient(app)
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    payload = json.dumps(response.json(), ensure_ascii=False)

    assert "deepseek-key" not in payload
    assert "zhipu_key" not in payload
    assert "bailian_key" not in payload
    assert "huoshan_secret_key" not in payload
    assert "cf_key" not in payload
    assert "sk-" not in payload
    assert "AKLT" not in payload


def test_public_config_defaults_to_full_market() -> None:
    client = TestClient(app)
    response = client.get("/api/config/public")
    assert response.status_code == 200
    payload = response.json()

    assert payload["scan_scope"] == "full_market"
    assert payload["include_watchlist_in_scan"] is False


def test_stream_preview_signature_includes_watch_marker() -> None:
    item = {
        "code": "600206",
        "sector": "半导体设备",
        "price": 52.63,
        "change_pct": 9.9,
        "phase": "低风险确认",
        "signal": "买T",
        "signal_score": 84,
        "signal_time": "13:26",
        "signal_grade": "公式买T",
        "order_flow": {"score": 6},
        "mini_chart": {
            "point_count": 48,
            "latest_change_pct": 9.9,
            "source_quality": "trajectory_stock_features",
            "markers": [
                {"time": "13:26", "signal": "买T", "gold_resonance": False},
            ],
        },
    }

    signature = main_module._preview_item_signature(item)

    assert "13:26:买T:0" in signature
    assert "公式买T" in signature
    assert "半导体设备" in signature
    assert "48:9.9:trajectory_stock_features" in signature


def test_stream_sector_flow_signature_changes_when_flow_is_built() -> None:
    empty_signature = main_module._sector_flow_signature([])
    built_signature = main_module._sector_flow_signature(
        [
            {
                "name": "半导体设备",
                "final_value": 6.99,
                "points": [
                    {"time": "09:31", "value": 0},
                    {"time": "09:42", "value": 6.99},
                ],
            }
        ]
    )

    assert empty_signature != built_signature
    assert "半导体设备:2:09:31:0:09:42:6.99:6.99" == built_signature


def test_market_capabilities_are_explicit_about_easy_tdx_tdx_l1() -> None:
    client = TestClient(app)

    response = client.get("/api/market/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "easy_tdx / TDX L1"
    assert payload["provider"] == "easy_tdx"
    assert payload["quote_depth"] is True
    assert payload["transaction_tape"] is True
    assert payload["auction_series"] is True
    assert payload["auction_0925"] is True
    assert payload["auction_0925_direct"] is False
    assert "不是委托队列" in payload["transaction_data_note"]
    assert payload["level2_available"] is False
    assert "委托队列" in payload["level2_note"]


def test_transaction_endpoint_returns_l1_summary_without_queue_claim(monkeypatch) -> None:
    client = TestClient(app)

    class FakeService:
        def transaction_flow(self, code, trade_date=None, count=None):
            assert code == "300476"
            assert trade_date == "20260807"
            assert count == 240
            return TransactionFlowObservation(
                available=True,
                source="easy_tdx_history_transaction_data",
                data_quality="l1_transaction",
                trade_date=trade_date,
                count=240,
                imbalance_pct=18.5,
                note="L1成交方向；不是委托队列",
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    response = client.get("/api/transactions/300476?trade_date=20260807&count=240")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["data_quality"] == "l1_transaction"
    assert "不是委托队列" in payload["note"]


def test_signal_detail_extras_endpoint_can_request_f10_fundamentals(monkeypatch) -> None:
    client = TestClient(app)

    class FakeService:
        def signal_detail_extras(
            self,
            code,
            sector=None,
            trade_date=None,
            include_fundamentals=False,
            include_capital_flow=False,
            include_indicators=False,
            include_chanlun=False,
        ):
            assert code == "300476"
            assert sector == "PCB"
            assert trade_date == "20260807"
            assert include_fundamentals is True
            assert include_capital_flow is False
            assert include_indicators is False
            assert include_chanlun is False
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "code": code,
                    "name": "胜宏科技",
                    "sector": sector,
                    "trade_date": trade_date,
                    "fundamentals": {
                        "available": True,
                        "source": "easy_tdx_f10_7615",
                        "section_count": 21,
                        "expected_section_count": 21,
                        "sections": [{"key": "stock_info", "title": "股票基础", "available": True}],
                    },
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    response = client.get(
        "/api/signals/300476/detail/extras?sector=PCB&trade_date=20260807&include_fundamentals=true"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["fundamentals"]["source"] == "easy_tdx_f10_7615"
    assert payload["fundamentals"]["section_count"] == 21


def test_message_detail_endpoint_returns_full_message_payload(monkeypatch) -> None:
    client = TestClient(app)

    class FakeService:
        def message_detail(self, event_id):
            assert event_id == "event-1"
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "topic": {
                        "topic_id": "topic-1",
                        "title": "胜宏科技订单跟踪",
                        "content": "服务器PCB订单继续改善。",
                    },
                    "event": {
                        "event_id": "event-1",
                        "title": "PCB订单改善",
                        "summary": "需求改善带动PCB景气度上修。",
                    },
                    "links": [{"entity_type": "stock", "code": "300476", "name": "胜宏科技"}],
                    "sync": {"topic_count": 1, "event_count": 1},
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    response = client.get("/api/messages/event-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["event"]["summary"] == "需求改善带动PCB景气度上修。"
    assert payload["links"][0]["code"] == "300476"


def test_signal_detail_compact_payload_keeps_chart_evidence_without_repeated_audit_fields(
    monkeypatch,
) -> None:
    market = MarketState(
        trend=TrendState.TURNING_UP,
        emotion_score=68,
        breadth_pct=56,
        index_turning=True,
        amount_expanding=True,
        mainline="PCB",
        indices=[],
        reasons=["指数拐头"],
        updated_at="10:05:00",
    )
    signal = TradeSignal(
        code="300476",
        name="胜宏科技",
        signal=SignalType.BUY_T,
        score=76,
        sector="PCB",
        price=88.5,
        change_pct=3.2,
        rebound_from_low_pct=4.5,
        minute_amount_ratio=2.1,
        reasons=["板块与个股共振"],
        updated_at="10:05:00",
    )
    point = ReplayPoint(
        time="10:05",
        price=88.5,
        change_pct=3.2,
        rebound_from_low_pct=4.5,
        pullback_from_high_pct=0.4,
        volume=120_000,
        minute_amount_ratio=2.1,
        signal=SignalType.BUY_T,
        reasons=["每分钟重复的审计原因"],
        factor_flags=["板块点火"],
        vwap=87.9,
        flow_score=22,
        factor_scores={"market": 18},
        market_event="指数跌速收敛",
        sector_event="PCB扩散",
        stock_event="突破后承接",
        flow_event="成交方向改善",
        risk_reward=RiskRewardPlan(available=True, target_price=90, invalidation_price=87),
    )
    marker = ReplayMarker(
        time="10:05",
        signal=SignalType.BUY_T,
        price=88.5,
        change_pct=3.2,
        reasons=["目标先于失效的赔率可接受"],
        score=76,
        phase=SignalPhase.CONFIRM,
        action=TradeAction.BUY_T,
        direction=TradeDirection.POSITIVE_T,
        factor_scores={"market": 18},
        evidence_sequence=["核心先动", "个股跟随"],
        market_event="指数拐头",
        sector_event="PCB扩散",
        stock_event="结构转强",
        flow_event="L1成交响应改善",
        risk_reward=RiskRewardPlan(
            available=True,
            invalidation_price=87,
            target_price=90,
            risk_pct=1.7,
            expected_reward_pct=1.9,
            reward_risk_ratio=1.12,
            min_required_ratio=1.0,
            execution_rr=1.08,
            context="不需要在浏览器重复传输",
        ),
    )
    detail = SignalReplayDetail(
        code="300476",
        name="胜宏科技",
        sector="PCB",
        trade_date="20260807",
        market=market,
        current_signal=signal,
        replay_points=[point],
        signal_timeline=[marker],
        markers=[marker],
        summary=["测试详情"],
        decision_markers=[marker],
        transaction_flow=TransactionFlowObservation(
            available=True,
            count=3,
            points=[TransactionFlowPoint(time="10:05", count=3)],
        ),
    )

    class FakeService:
        def signal_detail(self, code, sector=None, trade_date=None, fast=False):
            assert code == "300476"
            assert trade_date == "20260807"
            assert fast is True
            return detail

    monkeypatch.setattr(main_module, "service", FakeService())
    client = TestClient(app)

    compact = client.get(
        "/api/signals/300476/detail?trade_date=20260807&fast=true&compact=true"
    )
    legacy = client.get("/api/signals/300476/detail?trade_date=20260807&fast=true")

    assert compact.status_code == 200
    payload = compact.json()
    assert "signal_timeline" not in payload
    assert "markers" not in payload
    assert payload["replay_points"][0]["market_event"] == "指数跌速收敛"
    assert payload["replay_points"][0]["factor_flags"] == ["板块点火"]
    assert "reasons" not in payload["replay_points"][0]
    assert "factor_scores" not in payload["replay_points"][0]
    assert "risk_reward" not in payload["replay_points"][0]
    assert payload["decision_markers"][0]["action"] == "buy_t"
    assert payload["decision_markers"][0]["risk_reward"]["target_price"] == 90
    assert "context" not in payload["decision_markers"][0]["risk_reward"]
    assert "factor_scores" not in payload["decision_markers"][0]
    assert "evidence_sequence" not in payload["decision_markers"][0]
    assert payload["transaction_flow"]["point_count"] == 1
    assert "points" not in payload["transaction_flow"]
    assert legacy.status_code == 200
    assert legacy.json()["signal_timeline"]
    assert legacy.json()["markers"]
    assert legacy.json()["transaction_flow"]["points"]


def test_dashboard_scans_full_market_not_watchlist(monkeypatch) -> None:
    class EmptyWatchlistStore:
        def list_items(self):
            return []

    isolated_service = main_module.DashboardService(
        main_module.settings,
        watchlist_store=EmptyWatchlistStore(),
    )
    monkeypatch.setattr(main_module, "service", isolated_service)
    client = TestClient(app)
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    payload = response.json()
    watchlist_codes = {item["code"] for item in payload["watchlist"]}

    assert payload["source_status"]["signal_scope"] == "full_market"
    assert payload["source_status"]["include_watchlist_in_scan"] is False
    assert payload["source_status"]["quote_count"] >= 5000
    assert payload["source_status"]["sector_count"] > 4
    assert isinstance(payload["sector_flow"], list)
    assert payload["signals"]
    assert not watchlist_codes


def test_terminal_board_reports_full_market_total_and_public_fields() -> None:
    client = TestClient(app)
    response = client.get("/api/dashboard?view=terminal&page_size=20")
    assert response.status_code == 200
    payload = response.json()
    board = payload["stock_board"]

    assert board["scope"] == "full_market"
    assert board["total"] >= payload["source_status"]["quote_count"] - 1
    assert len(board["items"]) <= 20
    assert "order_flow" in board["items"][0]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "deepseek-key" not in serialized
    assert "zhipu_key" not in serialized
    assert "bailian_key" not in serialized
    assert "huoshan_secret_key" not in serialized
    assert "WATCH_INGEST_TOKEN" not in serialized
    assert "opening" not in payload


def test_board_level_query_is_forwarded_to_terminal_endpoints(monkeypatch) -> None:
    client = TestClient(app)

    calls: list[tuple[str, dict]] = []

    class FakeService:
        def terminal(self, **kwargs):
            calls.append(("terminal", kwargs))
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "market": {"frozen": True},
                    "stock_board": {"items": [], "total": 0, "page": 1, "page_size": kwargs.get("page_size", 80), "board_level": kwargs.get("board_level", 3)},
                    "data_mode": "closed_static",
                    "source_status": {"updated_at": "15:00:00"},
                    "board_level": kwargs.get("board_level", 3),
                }
            )

        def stock_board(self, **kwargs):
            calls.append(("stock_board", kwargs))
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "scope": "full_market",
                    "selected_sector": kwargs.get("sector"),
                    "board_level": kwargs.get("board_level", 3),
                    "page": kwargs.get("page", 1),
                    "page_size": kwargs.get("page_size", 80),
                    "total": 0,
                    "updated_at": "15:00:00",
                    "data_mode": "closed_static",
                    "frozen": True,
                    "items": [],
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    terminal_response = client.get("/api/dashboard?view=terminal&board_level=2&page_size=20&fast=true")
    board_response = client.get("/api/stocks/board?sector=PCB&board_level=1&page=2&page_size=20")

    assert terminal_response.status_code == 200
    assert terminal_response.json()["board_level"] == 2
    assert terminal_response.json()["stock_board"]["board_level"] == 2
    assert board_response.status_code == 200
    assert board_response.json()["board_level"] == 1
    assert calls[0][0] == "terminal"
    assert calls[0][1]["board_level"] == 2
    assert calls[0][1]["fast"] is True
    assert calls[1][0] == "stock_board"
    assert calls[1][1]["board_level"] == 1


def test_client_watchlist_codes_are_forwarded_to_terminal_endpoints(monkeypatch) -> None:
    client = TestClient(app)

    calls: list[tuple[str, dict]] = []

    class FakeService:
        def terminal(self, **kwargs):
            calls.append(("terminal", kwargs))
            watchlist = kwargs.get("client_watchlist") or []
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "market": {"frozen": True},
                    "stock_board": {
                        "items": [],
                        "total": 0,
                        "page": 1,
                        "page_size": kwargs.get("page_size", 80),
                    },
                    "watchlist_codes": [item.code for item in watchlist],
                    "data_mode": "closed_static",
                    "source_status": {"updated_at": "15:00:00"},
                }
            )

        def stock_board(self, **kwargs):
            calls.append(("stock_board", kwargs))
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "scope": "full_market",
                    "selected_sector": kwargs.get("sector"),
                    "page": kwargs.get("page", 1),
                    "page_size": kwargs.get("page_size", 80),
                    "total": 0,
                    "updated_at": "15:00:00",
                    "data_mode": "closed_static",
                    "frozen": True,
                    "items": [],
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    terminal_response = client.get("/api/dashboard?view=terminal&watchlist_codes=300476,1,abc,300476")
    board_response = client.get("/api/stocks/board?watchlist_codes=300308,000001")

    assert terminal_response.status_code == 200
    assert terminal_response.json()["watchlist_codes"] == ["300476", "000001"]
    assert board_response.status_code == 200
    assert calls[0][0] == "terminal"
    assert [item.code for item in calls[0][1]["client_watchlist"]] == ["300476", "000001"]
    assert calls[1][0] == "stock_board"
    assert [item.code for item in calls[1][1]["client_watchlist"]] == ["300308", "000001"]


def test_sector_rank_endpoint_uses_official_board_rank(monkeypatch) -> None:
    client = TestClient(app)
    calls: list[dict] = []

    class FakeService:
        def sector_rank(self, **kwargs):
            calls.append(kwargs)
            return [
                SimpleNamespace(
                    model_dump=lambda mode="json": {
                        "name": "官方板块",
                        "board_source": "easy_tdx_cached_members_local_quote_aggregation",
                    }
                )
            ]

    monkeypatch.setattr(main_module, "service", FakeService())

    response = client.get("/api/sectors/rank?board_level=2&watchlist_codes=300476")

    assert response.status_code == 200
    assert response.json()[0]["name"] == "官方板块"
    assert calls[0]["board_level"] == 2
    assert [item.code for item in calls[0]["client_watchlist"]] == ["300476"]


def test_explicit_empty_watchlist_codes_forwards_empty_client_watchlist(monkeypatch) -> None:
    client = TestClient(app)
    calls: list[dict] = []

    class FakeService:
        def terminal(self, **kwargs):
            calls.append(kwargs)
            watchlist = kwargs.get("client_watchlist")
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "market": {"frozen": True},
                    "stock_board": {"items": [], "total": 0, "page": 1, "page_size": 40},
                    "watchlist_codes": [item.code for item in (watchlist or [])],
                    "data_mode": "closed_static",
                    "source_status": {"updated_at": "15:00:00"},
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    response = client.get("/api/dashboard?view=terminal&watchlist_codes=")

    assert response.status_code == 200
    assert "client_watchlist" in calls[0]
    assert calls[0]["client_watchlist"] == []
    assert response.json()["watchlist_codes"] == []


def test_terminal_stream_forwards_current_page(monkeypatch) -> None:
    client = TestClient(app)
    calls: list[dict] = []

    class FakeService:
        def terminal(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "market": {"updated_at": "10:00:01", "frozen": False},
                    "stock_board": {
                        "items": [
                            {
                                "code": "000123",
                                "price": 12.3,
                                "change_pct": 1.2,
                                "amount": 1000000,
                                "updated_at": "10:00:01",
                                "phase": "观察",
                                "signal_score": 10,
                            }
                        ],
                        "total": 100,
                        "page": kwargs.get("page", 1),
                        "page_size": kwargs.get("page_size", 80),
                    },
                    "watchlist_preview": [],
                    "positions_preview": [],
                    "sector_flow": [],
                    "data_mode": "live",
                    "selected_sector": kwargs.get("sector"),
                    "board_level": kwargs.get("board_level", 3),
                    "source_status": {"updated_at": "10:00:01"},
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    with client.websocket_connect("/ws/stream?view=terminal&fast=1&page=3&page_size=20") as websocket:
        payload = websocket.receive_json()

    assert payload["stock_board"]["page"] == 3
    assert calls[0]["page"] == 3
    assert calls[0]["page_size"] == 20
    assert calls[0]["fast"] is True


def test_terminal_stream_forwards_client_watchlist_codes(monkeypatch) -> None:
    client = TestClient(app)
    calls: list[dict] = []

    class FakeService:
        def terminal(self, **kwargs):
            calls.append(kwargs)
            watchlist = kwargs.get("client_watchlist") or []
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "market": {"updated_at": "10:00:01", "frozen": False},
                    "stock_board": {
                        "items": [],
                        "total": 0,
                        "page": kwargs.get("page", 1),
                        "page_size": kwargs.get("page_size", 80),
                    },
                    "watchlist_preview": [],
                    "positions_preview": [],
                    "sector_flow": [],
                    "watchlist_codes": [item.code for item in watchlist],
                    "data_mode": "live",
                    "selected_sector": kwargs.get("sector"),
                    "source_status": {"updated_at": "10:00:01"},
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    with client.websocket_connect("/ws/stream?view=terminal&fast=1&page_size=20&watchlist_codes=300476,000001") as websocket:
        payload = websocket.receive_json()

    assert payload["watchlist_codes"] == ["300476", "000001"]
    assert [item.code for item in calls[0]["client_watchlist"]] == ["300476", "000001"]


def test_stock_board_endpoint_keeps_sector_paging_contract() -> None:
    client = TestClient(app)
    response = client.get("/api/stocks/board?sector=PCB&page=1&page_size=20&sort=activity")
    assert response.status_code == 200
    payload = response.json()

    assert payload["scope"] == "full_market"
    assert payload["selected_sector"] == "PCB"
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert payload["total"] >= len(payload["items"])
    assert all(item["sector"] == "PCB" for item in payload["items"])


def test_stock_search_endpoint_caps_limit_and_skips_empty_query(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    class FakeService:
        def search_stocks(self, query, limit=12):
            calls.append((query, limit))
            return [
                {
                    "code": "300476",
                    "name": "胜宏科技",
                    "sector": "PCB",
                    "themes": ["PCB"],
                    "watchlisted": False,
                    "source": "current_context",
                }
            ]

    monkeypatch.setattr(main_module, "service", FakeService())
    client = TestClient(app)

    response = client.get("/api/stocks/search?q=300&limit=500")
    empty = client.get("/api/stocks/search?q=   ")

    assert response.status_code == 200
    assert response.json()[0]["code"] == "300476"
    assert calls == [("300", 50)]
    assert empty.status_code == 200
    assert empty.json() == []


def test_position_crud_endpoints_keep_personal_context_local(monkeypatch) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.items: dict[str, PositionRecord] = {}

        def list_positions(self):
            return list(self.items.values())

        def upsert_position(self, item):
            self.items[item.code] = item
            return item

        def delete_position(self, code):
            return self.items.pop(code, None) is not None

    fake = FakeService()
    monkeypatch.setattr(main_module, "service", fake)
    client = TestClient(app)
    payload = {
        "code": "300308",
        "name": "中际旭创",
        "cost": 102.5,
        "quantity": 1200,
        "available_quantity": 800,
        "t_allocation_pct": 25,
    }

    created = client.post("/api/positions", json=payload)
    listed = client.get("/api/positions")
    mismatch = client.put("/api/positions/300476", json=payload)
    deleted = client.delete("/api/positions/300308")

    assert created.status_code == 200
    assert created.json()["available_quantity"] == 800
    assert listed.status_code == 200
    assert listed.json()[0]["code"] == "300308"
    assert mismatch.status_code == 400
    assert deleted.status_code == 200
    assert client.get("/api/positions").json() == []


def test_opening_decision_endpoint_returns_checkpoint_and_reasons(monkeypatch) -> None:
    class FakeService:
        def opening_decision(self, sector=None):
            assert sector == "PCB"
            return OpeningDecisionPayload(
                trade_date="20260807",
                updated_at="09:35:00",
                stage="confirm",
                stage_label="09:35 确认",
                checkpoint="09:35",
                active=True,
                can_execute=True,
                scope="full_market",
                total=5200,
                market_gate="通过",
                market_score=78,
                candidate_count=12,
                buy_count=1,
                market_reasons=["指数拐头", "短周期成交额放大"],
                top_candidates=[
                    OpeningDecisionItem(
                        code="300476",
                        name="胜宏科技",
                        sector="PCB",
                        action=OpeningAction.BUY,
                        score=86,
                        checkpoint="09:35",
                        can_execute=True,
                        reasons=["三层门槛通过", "分时放量"],
                    )
                ],
            ).model_copy(update={"selected_sector": sector})

    monkeypatch.setattr(main_module, "service", FakeService())
    response = TestClient(app).get("/api/opening/decision?sector=PCB")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stage"] == "confirm"
    assert payload["checkpoint"] == "09:35"
    assert payload["can_execute"] is True
    assert payload["top_candidates"][0]["action"] == "确认买T"
    assert "分时放量" in payload["top_candidates"][0]["reasons"]


def test_opening_research_endpoint_reads_local_report_without_credentials(monkeypatch, tmp_path) -> None:
    report_path = tmp_path / "runtime" / "strategy-research" / "latest_l1.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-07T16:00:00",
                "sample": {"size": 80},
                "date_summaries": [{"date": "20260807", "opening_buy_events": 0}],
                "opening": {
                    "checkpoints": ["09:33", "09:35", "09:37"],
                    "records_count": 240,
                    "action_counts": {"确认买T": 0},
                },
                "data_quality": {"level2_available": False},
                "methodology": {"limitations": ["样本较少"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main_module.settings, "data_dir", tmp_path)

    response = TestClient(app).get("/api/opening/research")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["opening"]["checkpoints"] == ["09:33", "09:35", "09:37"]
    assert payload["limitations"] == ["样本较少"]
    assert "WATCH_INGEST_TOKEN" not in json.dumps(payload, ensure_ascii=False)


def test_dashboard_sector_filter_limits_signals_to_selected_board() -> None:
    client = TestClient(app)
    response = client.get("/api/dashboard?sector=PCB")
    assert response.status_code == 200
    payload = response.json()

    assert payload["selected_sector"] == "PCB"
    assert payload["signals"]
    assert all(signal["sector"] == "PCB" for signal in payload["signals"])


def test_index_detail_endpoint_returns_index_payload(monkeypatch) -> None:
    client = TestClient(app)

    class FakeService:
        def index_detail(self, code, trade_date=None):
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "code": code,
                    "name": "上证指数",
                    "trade_date": trade_date,
                    "market": {"trend": "分歧转强"},
                    "current_index": {"code": code, "name": "上证指数", "price": 3310.0},
                    "replay_points": [],
                    "markers": [],
                    "summary": ["大盘盘口分时，不生成个股买卖信号"],
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())

    response = client.get("/api/indices/000001/detail?trade_date=20260806")
    assert response.status_code == 200
    payload = response.json()

    assert payload["code"] == "000001"
    assert payload["name"] == "上证指数"
    assert payload["summary"][0].startswith("大盘盘口分时")


def test_zsxq_ingest_requires_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv("WATCH_INGEST_TOKEN", "unit-token")
    client = TestClient(app)

    missing = client.post("/api/ingest/zsxq/messages", json={"topics": [], "events": [], "links": []})
    invalid = client.post(
        "/api/ingest/zsxq/messages",
        headers={"Authorization": "Bearer wrong-token"},
        json={"topics": [], "events": [], "links": []},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 403


def test_zsxq_ingest_accepts_valid_token_without_exposing_secret(monkeypatch) -> None:
    monkeypatch.setenv("WATCH_INGEST_TOKEN", "unit-token")

    class FakeService:
        def ingest_zsxq_messages(self, payload):
            assert payload.topics[0].topic_id == "topic-1"
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "ok": True,
                    "source": payload.source,
                    "run_id": "unit-run",
                    "topic_count": len(payload.topics),
                    "event_count": len(payload.events),
                    "link_count": len(payload.links),
                }
            )

    monkeypatch.setattr(main_module, "service", FakeService())
    client = TestClient(app)
    response = client.post(
        "/api/ingest/zsxq/messages",
        headers={"Authorization": "Bearer unit-token"},
        json={
            "source": "zsxq",
            "topics": [
                {
                    "topic_id": "topic-1",
                    "title": "胜宏科技消息",
                    "content": "PCB订单改善",
                    "create_time": "2026-08-06T09:45:00+08:00",
                }
            ],
            "events": [],
            "links": [],
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["topic_count"] == 1
    assert "unit-token" not in json.dumps(payload, ensure_ascii=False)
