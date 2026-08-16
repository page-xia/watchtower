from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from pathlib import Path
import re
import threading
from typing import NamedTuple

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.data_sources import china_now, is_trading_window, market_session, normalize_board_level
from app.config import ROOT_DIR, settings
from app.market_schedule import market_refresh_policy
from app.models import (
    PositionRecord,
    ReplayMarker,
    ReplayPoint,
    RiskRewardPlan,
    WatchlistItem,
    ZsxqMessageIngestRequest,
)
from app.services import DashboardService
from app.stream_delta import TerminalDeltaTracker
from app.stream_hub import RESYNC, ChannelLimitExceeded, ChannelSpec, StreamHub
from app.trajectory_store import IntradayCollector


WEB_DIST_DIR = ROOT_DIR / "web" / "dist"

stream_hub = StreamHub(
    queue_size=settings.stream_queue_size,
    channel_idle_seconds=settings.stream_channel_idle_seconds,
    max_channels=settings.stream_max_channels,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    collector: IntradayCollector | None = None
    collector_thread: threading.Thread | None = None
    warmup_thread: threading.Thread | None = None
    service.start_trajectory_cleanup_thread(reason="startup")
    service.start_opening_window_engine()
    # 暗盘资金监控随后端启动拉起：线程自行休眠至 09:30 交易窗口开始首轮磁带周期，
    # 盘中数据积累不依赖「有人打开页面触发接口」。
    service.dark_pool_payload()
    if settings.background_collector_enabled:
        collector = IntradayCollector(service, settings.background_collector_seconds)
        collector_thread = threading.Thread(
            target=collector.run,
            name="intraday-watchtower-collector",
            daemon=True,
        )
        collector_thread.start()
        if is_trading_window():
            def warmup() -> None:
                try:
                    service.collect_once()
                except Exception:
                    return

            warmup_thread = threading.Thread(
                target=warmup,
                name="intraday-watchtower-initial-warmup",
                daemon=True,
            )
            warmup_thread.start()
    try:
        yield
    finally:
        if collector is not None:
            collector.stop()
        if collector_thread is not None:
            await asyncio.to_thread(collector_thread.join, 10)
        if warmup_thread is not None:
            await asyncio.to_thread(warmup_thread.join, 2)
        cleanup_thread = getattr(service, "_trajectory_cleanup_thread", None)
        if cleanup_thread is not None:
            await asyncio.to_thread(cleanup_thread.join, 2)
        await stream_hub.aclose()
        service.close()


app = FastAPI(title="日内宏观盯盘", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def no_store_api_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Surrogate-Control"] = "no-store"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8788",
        "http://localhost:8788",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
# 前端唯一入口：web/ React 应用的构建产物（web/dist）。assets 带内容哈希可长缓存；
# dist 不存在时（未跑 npm run build）不挂载，根路由会返回构建指引。
if (WEB_DIST_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST_DIR / "assets"), name="assets")

service = DashboardService(settings)


_COMPACT_REPLAY_POINT_FIELDS = {
    "time",
    "price",
    "change_pct",
    "rebound_from_low_pct",
    "volume",
    "minute_amount_ratio",
    "factor_flags",
    "vwap",
    "flow_score",
    "source_quality",
    "market_event",
    "sector_event",
    "stock_event",
    "flow_event",
}
_COMPACT_REPLAY_POINT_EXCLUDE = set(ReplayPoint.model_fields) - _COMPACT_REPLAY_POINT_FIELDS

_COMPACT_MARKER_FIELDS = {
    "time",
    "price",
    "change_pct",
    "reasons",
    "score",
    "phase",
    "risks",
    "invalidation_price",
    "source_quality",
    "exit_score",
    "market_event",
    "sector_event",
    "stock_event",
    "flow_event",
    "t_plus_one_restricted",
    "action_size_pct",
    "direction",
    "action",
    "setup",
    "executable",
    "validation_status",
    "risk_reward",
    "gold_resonance",
    "resonance_reasons",
}
_COMPACT_MARKER_EXCLUDE = {
    field: True for field in set(ReplayMarker.model_fields) - _COMPACT_MARKER_FIELDS
}
_COMPACT_MARKER_RISK_REWARD_FIELDS = {
    "available",
    "invalidation_price",
    "target_price",
    "buyback_price",
    "risk_pct",
    "expected_reward_pct",
    "reward_risk_ratio",
    "min_required_ratio",
    "execution_rr",
}
_COMPACT_MARKER_EXCLUDE["risk_reward"] = {
    field: True
    for field in set(RiskRewardPlan.model_fields) - _COMPACT_MARKER_RISK_REWARD_FIELDS
}


def _serialize_signal_detail(detail, *, compact: bool) -> dict:
    """Serialize the browser detail view without repeated per-minute audit fields."""

    if not compact:
        return detail.model_dump(mode="json")

    transaction_point_count = len(detail.transaction_flow.points)
    payload = detail.model_dump(
        mode="json",
        exclude={
            "signal_timeline": True,
            "markers": True,
            "replay_points": {"__all__": _COMPACT_REPLAY_POINT_EXCLUDE},
            "decision_markers": {"__all__": _COMPACT_MARKER_EXCLUDE},
            "transaction_flow": {"points": True},
        },
    )
    payload["transaction_flow"]["point_count"] = transaction_point_count
    return payload


def _mini_marker_signature(item: dict) -> str:
    chart = item.get("mini_chart") if isinstance(item, dict) else None
    markers = chart.get("markers") if isinstance(chart, dict) else []
    if not isinstance(markers, list):
        return ""
    return ",".join(
        ":".join(
            [
                str(marker.get("time") or "")[:5],
                str(marker.get("signal") or ""),
                "1" if marker.get("gold_resonance") else "0",
            ]
        )
        for marker in markers[:4]
        if isinstance(marker, dict)
    )


def _mini_chart_signature(item: dict) -> str:
    chart = item.get("mini_chart") if isinstance(item, dict) else {}
    if not isinstance(chart, dict):
        chart = {}
    return ":".join(
        [
            str(chart.get("point_count") or ""),
            str(chart.get("latest_change_pct") or ""),
            str(chart.get("source_quality") or ""),
            _mini_marker_signature(item),
        ]
    )


def _preview_item_signature(item: dict, *, include_position: bool = False) -> str:
    parts = [
        str(item.get("code") or ""),
        str(item.get("sector") or ""),
        str(item.get("price") or ""),
        str(item.get("change_pct") or ""),
        str(item.get("phase") or ""),
        str(item.get("signal") or ""),
        str(item.get("signal_score") or ""),
        str(item.get("signal_time") or ""),
        str(item.get("signal_grade") or ""),
        str(item.get("order_flow", {}).get("score") if isinstance(item.get("order_flow"), dict) else ""),
        _mini_chart_signature(item),
    ]
    if include_position:
        parts.append(str(item.get("available_quantity") or ""))
    return ":".join(parts)


def _sector_flow_signature(series: list[dict]) -> str:
    def signature_value(value: object) -> str:
        return "" if value is None else str(value)

    parts: list[str] = []
    for item in series[:10]:
        points = item.get("points") if isinstance(item, dict) else []
        if not isinstance(points, list):
            points = []
        first = points[0] if points and isinstance(points[0], dict) else {}
        last = points[-1] if points and isinstance(points[-1], dict) else {}
        parts.append(
            ":".join(
                [
                    str(item.get("name") or "") if isinstance(item, dict) else "",
                    str(len(points)),
                    str(first.get("time") or ""),
                    signature_value(first.get("value")),
                    str(last.get("time") or ""),
                    signature_value(last.get("value")),
                    signature_value(item.get("final_value")) if isinstance(item, dict) else "",
                ]
            )
        )
    return "/".join(parts)


def require_ingest_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.message_ingest_token
    if not expected:
        raise HTTPException(status_code=503, detail="消息接收 token 未配置")
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), expected):
        raise HTTPException(status_code=403, detail="Bearer token 无效")


def _parse_watchlist_codes(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    codes: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;，；]+", str(raw)):
        value = token.strip()
        if not value or not value.isdigit() or len(value) > 6:
            continue
        code = value.zfill(6)
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return tuple(codes)


def _client_watchlist_from_codes(codes: tuple[str, ...]) -> list[WatchlistItem]:
    return [
        WatchlistItem(code=code, name="", themes=[], core=False, position=False, notes="")
        for code in codes
    ]


def _client_watchlist_kwargs(raw: str | None) -> dict[str, list[WatchlistItem]]:
    if raw is None:
        return {}
    return {"client_watchlist": _client_watchlist_from_codes(_parse_watchlist_codes(raw))}


def _stream_client_watchlist_kwargs(params: StreamParams) -> dict[str, list[WatchlistItem]]:
    if not params.watchlist_codes_provided:
        return {}
    return {"client_watchlist": _client_watchlist_from_codes(params.watchlist_codes)}


@app.get("/")
def index() -> FileResponse:
    index_file = WEB_DIST_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(
            status_code=503,
            detail="前端构建产物缺失：请先在 web/ 目录执行 npm run build（开发调试可用 npm run dev，端口 7100）。",
        )
    return FileResponse(
        index_file,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "intraday-watchtower", "secret_safe": True}


@app.get("/api/config/public")
def public_config() -> dict:
    return settings.public_source_status


@app.get("/api/market/capabilities")
def market_capabilities() -> dict:
    """Expose data quality/capability labels, never credentials."""
    now = china_now()
    payload = service.market_capabilities()
    payload.update(
        {
            "server_clock": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_session": market_session(now),
            "is_trading_window": is_trading_window(now),
        }
    )
    return payload


@app.get("/api/messages/status")
def messages_status() -> dict:
    return service.message_status()


@app.post("/api/ingest/zsxq/messages")
def ingest_zsxq_messages(
    payload: ZsxqMessageIngestRequest,
    _: None = Depends(require_ingest_token),
) -> dict:
    return service.ingest_zsxq_messages(payload).model_dump(mode="json")


@app.post("/api/messages/evidence/prebuild")
def prebuild_message_evidence(_: None = Depends(require_ingest_token)) -> dict:
    """后台全量预建星球消息物化证据（全部申万板块词 + 自选/持仓个股）。"""
    return service.prebuild_message_evidence()


@app.get("/api/dashboard")
def dashboard(
    sector: str | None = None,
    view: str | None = None,
    board_level: int = 3,
    sort: str = "activity",
    page: int = 1,
    page_size: int = 80,
    fast: bool = False,
    watchlist_codes: str | None = None,
) -> dict:
    client_watchlist_kwargs = _client_watchlist_kwargs(watchlist_codes)
    if view == "terminal":
        return service.terminal(
            sector=sector,
            board_level=board_level,
            sort=sort,
            page=page,
            page_size=page_size,
            fast=fast,
            **client_watchlist_kwargs,
        ).model_dump(mode="json")
    return service.dashboard(sector=sector, **client_watchlist_kwargs).model_dump(mode="json")


@app.get("/api/stocks/board")
def stocks_board(
    sector: str | None = None,
    board_level: int = 3,
    sort: str = "activity",
    page: int = 1,
    page_size: int = 80,
    watchlist_codes: str | None = None,
) -> dict:
    return service.stock_board(
        sector=sector,
        board_level=board_level,
        sort=sort,
        page=page,
        page_size=page_size,
        **_client_watchlist_kwargs(watchlist_codes),
    ).model_dump(mode="json")


@app.get("/api/stocks/search")
def search_stocks(q: str = "", limit: int = 12, watchlist_codes: str | None = None) -> list[dict]:
    query = str(q or "").strip()
    if not query:
        return []
    bounded_limit = max(1, min(int(limit or 12), 50))
    return service.search_stocks(
        query,
        limit=bounded_limit,
        **_client_watchlist_kwargs(watchlist_codes),
    )


@app.get("/api/market/state")
def market_state() -> dict:
    return service.dashboard().market.model_dump(mode="json")


@app.get("/api/opening/decision")
def opening_decision(sector: str | None = None) -> dict:
    """Return the 09:33/09:35/09:37 opening-window decision snapshot."""
    return service.opening_decision(sector=sector).model_dump(mode="json")


@app.get("/api/opening/markers")
def opening_markers(
    trade_date: str | None = None,
    offset: int = 0,
    limit: int = 20,
    side: str | None = None,
) -> dict:
    """开盘窗口菱形买卖点（机会队列流）：最新在前，游标分页加载更多，可按买/卖侧过滤。"""
    return service.opening_markers_page(trade_date=trade_date, offset=offset, limit=limit, side=side)


@app.get("/api/opening/research")
def opening_research() -> dict:
    """Expose the latest local research summary without returning credentials."""
    path = settings.data_dir / "runtime" / "strategy-research" / "latest_l1.json"
    if not path.exists():
        path = settings.data_dir / "runtime" / "strategy-research" / "latest.json"
    if not path.exists():
        return {"available": False, "note": "暂无开盘窗口研究报告"}
    try:
        import json

        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "note": f"研究报告读取失败：{exc}"}
    return {
        "available": True,
        "generated_at": report.get("generated_at", ""),
        "sample": report.get("sample", {}),
        "dates": report.get("date_summaries", []),
        "opening": report.get("opening", {}),
        "data_quality": report.get("data_quality", {}),
        "limitations": report.get("methodology", {}).get("limitations", []),
    }


@app.get("/api/research/status")
def research_status() -> dict:
    """Return aggregate research validation state without raw labels or secrets."""
    return service.research_status()


@app.get("/api/research/protocol")
def research_protocol() -> dict:
    """Return registered hypotheses, controls and aggregate protocol results."""
    return service.research_protocol()


@app.get("/api/sectors/rank")
def sectors_rank(board_level: int = 3, watchlist_codes: str | None = None) -> list[dict]:
    return [
        sector.model_dump(mode="json")
        for sector in service.sector_rank(
            board_level=board_level,
            **_client_watchlist_kwargs(watchlist_codes),
        )
    ]


@app.get("/api/dark-pool")
def dark_pool() -> dict:
    """暗盘资金面板：盘中大单推断（缓存）+ Tushare 收盘官方口径（本地库）。

    只读内存缓存与本地 SQLite，绝不在请求路径上发行情网络请求，
    保证不拖慢首页 5 秒刷新链路。
    """
    return service.dark_pool_payload()


@app.get("/api/signals")
def signals(sector: str | None = None) -> list[dict]:
    return [signal.model_dump(mode="json") for signal in service.dashboard(sector=sector).signals]


@app.get("/api/signals/{code}/detail")
def signal_detail(
    code: str,
    sector: str | None = None,
    trade_date: str | None = None,
    fast: bool = False,
    compact: bool = False,
    watchlist_codes: str | None = None,
) -> dict:
    try:
        detail = service.signal_detail(
            code,
            sector=sector,
            trade_date=trade_date,
            fast=fast,
            **_client_watchlist_kwargs(watchlist_codes),
        )
        return _serialize_signal_detail(detail, compact=compact)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/signals/{code}/detail/chart")
def signal_detail_chart(
    code: str,
    sector: str | None = None,
    trade_date: str | None = None,
    watchlist_codes: str | None = None,
) -> dict:
    try:
        return service.signal_detail_chart(
            code,
            sector=sector,
            trade_date=trade_date,
            **_client_watchlist_kwargs(watchlist_codes),
        ).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/signals/{code}/detail/overlay")
def signal_detail_overlay(
    code: str,
    sector: str | None = None,
    trade_date: str | None = None,
    watchlist_codes: str | None = None,
) -> dict:
    try:
        return service.signal_detail_overlay(
            code,
            sector=sector,
            trade_date=trade_date,
            **_client_watchlist_kwargs(watchlist_codes),
        ).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/signals/{code}/detail/extras")
def signal_detail_extras(
    code: str,
    sector: str | None = None,
    trade_date: str | None = None,
    include_fundamentals: bool = False,
    include_capital_flow: bool = False,
    include_indicators: bool = False,
    include_chanlun: bool = False,
    include_auction_history: bool = True,
    watchlist_codes: str | None = None,
) -> dict:
    try:
        return service.signal_detail_extras(
            code,
            sector=sector,
            trade_date=trade_date,
            include_fundamentals=include_fundamentals,
            include_capital_flow=include_capital_flow,
            include_indicators=include_indicators,
            include_chanlun=include_chanlun,
            include_auction_history=include_auction_history,
            **_client_watchlist_kwargs(watchlist_codes),
        ).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/messages/{event_id}")
def message_detail(event_id: str) -> dict:
    try:
        return service.message_detail(event_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/indices/minutes")
def indices_minutes(trade_date: str | None = None) -> dict:
    return service.index_minutes(trade_date=trade_date)


@app.get("/api/indices/{code}/detail")
def index_detail(code: str, trade_date: str | None = None) -> dict:
    try:
        return service.index_detail(code, trade_date=trade_date).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/auction/{code}")
def auction_history(code: str, trade_date: str | None = None) -> dict:
    return service.auction_history(code, trade_date=trade_date)


@app.get("/api/transactions/{code}")
def transaction_flow(code: str, trade_date: str | None = None, count: int = 240) -> dict:
    """Return a bounded easy_tdx transaction summary, never raw credentials."""
    return service.transaction_flow(
        code,
        trade_date=trade_date,
        count=count,
    ).model_dump(mode="json")


@app.get("/api/watchlist")
def list_watchlist() -> list[dict]:
    return [item.model_dump(mode="json") for item in service.list_watchlist()]


@app.post("/api/watchlist")
def create_watchlist_item(item: WatchlistItem) -> dict:
    return service.upsert_watchlist(item).model_dump(mode="json")


@app.put("/api/watchlist/{code}")
def update_watchlist_item(code: str, item: WatchlistItem) -> dict:
    if code != item.code:
        raise HTTPException(status_code=400, detail="路径代码和请求体代码不一致")
    return service.upsert_watchlist(item).model_dump(mode="json")


@app.delete("/api/watchlist/{code}")
def delete_watchlist_item(code: str) -> dict:
    deleted = service.delete_watchlist(code)
    if not deleted:
        raise HTTPException(status_code=404, detail="自选股不存在")
    return {"deleted": True, "code": code}


@app.get("/api/positions")
def list_positions() -> list[dict]:
    return [item.model_dump(mode="json") for item in service.list_positions()]


@app.post("/api/positions")
def create_position(item: PositionRecord) -> dict:
    return service.upsert_position(item).model_dump(mode="json")


@app.put("/api/positions/{code}")
def update_position(code: str, item: PositionRecord) -> dict:
    if code != item.code:
        raise HTTPException(status_code=400, detail="路径代码和请求体代码不一致")
    return service.upsert_position(item).model_dump(mode="json")


@app.delete("/api/positions/{code}")
def delete_position(code: str) -> dict:
    if not service.delete_position(code):
        raise HTTPException(status_code=404, detail="持仓不存在")
    return {"deleted": True, "code": code}


@app.get("/api/watchlist/{code}/analysis")
def get_watchlist_analysis(code: str, trade_date: str | None = None) -> dict:
    record = service.load_analysis(code, trade_date)
    if record is None:
        raise HTTPException(status_code=404, detail="暂无AI分析记录")
    return record.model_dump(mode="json")


@app.post("/api/watchlist/{code}/analysis")
def analyze_watchlist_item(code: str, sector: str | None = None, trade_date: str | None = None) -> dict:
    return service.analyze_watchlist_item(code, sector=sector, trade_date=trade_date).model_dump(mode="json")


@app.websocket("/ws/stream")
async def stream(websocket: WebSocket) -> None:
    await websocket.accept()
    params = _stream_params(websocket)
    if not _stream_refresh_policy()["should_stream"]:
        await _stream_send_once_and_close(websocket, params)
        return
    if settings.stream_broadcaster_enabled:
        # 广播模式：同一参数组合共享一个频道（单发布者），构建/序列化/delta
        # 计算全局一次，每连接只从队列取文本发送。频道数超限回退旧实现。
        key = _stream_channel_key(params)
        try:
            sub = stream_hub.subscribe(key, lambda: _stream_channel_spec(params))
        except ChannelLimitExceeded:
            sub = None
        if sub is not None:
            try:
                snapshot = sub.snapshot_text()
                if snapshot:
                    await websocket.send_text(snapshot)
                while True:
                    if not _stream_refresh_policy()["should_stream"]:
                        await _stream_send_static_notice(websocket, params)
                        await websocket.close(code=1000)
                        return
                    try:
                        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if item is RESYNC:
                        # 队列溢出掉队：重发最新全量快照，之后的增量继续干净应用
                        text = sub.snapshot_text()
                        if text:
                            await websocket.send_text(text)
                        continue
                    await websocket.send_text(item)
            except (WebSocketDisconnect, RuntimeError):
                return
            finally:
                stream_hub.unsubscribe(sub)
    await _stream_legacy(websocket, params)


class StreamParams(NamedTuple):
    sector: str | None
    view: str
    sort: str
    fast: bool
    delta_format: bool
    board_level: int
    page: int
    page_size: int
    watchlist_codes: tuple[str, ...]
    watchlist_codes_provided: bool


def _stream_params(websocket: WebSocket) -> StreamParams:
    sector = websocket.query_params.get("sector")
    view = websocket.query_params.get("view", "legacy")
    sort = websocket.query_params.get("sort", "activity")
    fast = websocket.query_params.get("fast", "0").lower() in {"1", "true", "yes"}
    # format=delta opts into the snapshot/delta protocol; the legacy static
    # page keeps receiving full terminal payloads without it.
    delta_format = websocket.query_params.get("format", "") == "delta"
    try:
        board_level = int(websocket.query_params.get("board_level", "3"))
    except ValueError:
        board_level = 3
    try:
        page = int(websocket.query_params.get("page", "1"))
    except ValueError:
        page = 1
    try:
        page_size = int(websocket.query_params.get("page_size", "80"))
    except ValueError:
        page_size = 80
    watchlist_codes_provided = "watchlist_codes" in websocket.query_params
    watchlist_codes = _parse_watchlist_codes(websocket.query_params.get("watchlist_codes"))
    return StreamParams(
        sector=sector,
        view=view,
        sort=sort,
        fast=fast,
        delta_format=delta_format,
        board_level=board_level,
        page=page,
        page_size=page_size,
        watchlist_codes=watchlist_codes,
        watchlist_codes_provided=watchlist_codes_provided,
    )


def _stream_channel_key(params: StreamParams) -> tuple:
    # 与 service 层规范化口径对齐，避免同一视图裂成两个频道
    normalized_page_size = max(20, min(int(params.page_size or 80), 240))
    normalized_sort = (
        params.sort
        if params.sort in {"activity", "change", "amount", "volume_ratio", "order_flow", "signal"}
        else "activity"
    )
    return (
        params.view,
        params.delta_format and params.view == "terminal",
        str(params.sector or ""),
        int(normalize_board_level(params.board_level)),
        normalized_sort,
        max(1, int(params.page or 1)),
        normalized_page_size,
        bool(params.fast),
        bool(params.watchlist_codes_provided),
        params.watchlist_codes,
    )


def _stream_interval() -> float:
    policy = _stream_refresh_policy()
    return float(policy["stream_interval_seconds"] or settings.stream_static_interval_seconds)


def _stream_refresh_policy() -> dict:
    return market_refresh_policy(
        live_interval_seconds=settings.stream_live_interval_seconds,
        static_interval_seconds=settings.stream_static_interval_seconds,
    )


async def _stream_send_static_notice(websocket: WebSocket, params: StreamParams) -> None:
    policy = _stream_refresh_policy()
    notice = {
        "type": "market_phase",
        "market_session": policy["market_session"],
        "traffic_mode": policy["traffic_mode"],
        "refresh_policy": policy,
    }
    await websocket.send_json(notice)


async def _stream_send_once_and_close(websocket: WebSocket, params: StreamParams) -> None:
    if params.view == "terminal":
        payload = (
            await asyncio.to_thread(
                service.terminal,
                sector=params.sector,
                board_level=params.board_level,
                sort=params.sort,
                page=params.page,
                page_size=params.page_size,
                fast=params.fast,
                **_stream_client_watchlist_kwargs(params),
            )
        ).model_dump(mode="json")
        if params.delta_format:
            tracker = TerminalDeltaTracker()
            message = tracker.next_message(payload)
            if message is not None:
                await websocket.send_json(message)
        else:
            await websocket.send_json(payload)
    else:
        payload = (
            await asyncio.to_thread(
                service.dashboard,
                sector=params.sector,
                **_stream_client_watchlist_kwargs(params),
            )
        ).model_dump(mode="json")
        await websocket.send_json(payload)
    await websocket.close(code=1000)


def _terminal_payload_key(payload: dict) -> str:
    board_items = payload.get("stock_board", {}).get("items", [])
    board_signature = "/".join(
        ":".join(
            [
                str(item.get("code") or ""),
                str(item.get("sector") or ""),
                str(item.get("price") or ""),
                str(item.get("change_pct") or ""),
                str(item.get("amount") or ""),
                str(item.get("updated_at") or ""),
                str(item.get("phase") or ""),
                str(item.get("signal_score") or ""),
                _mini_chart_signature(item),
            ]
        )
        for item in board_items
    )
    watch_signature = "/".join(
        _preview_item_signature(item)
        for item in payload.get("watchlist_preview", [])
    )
    position_signature = "/".join(
        _preview_item_signature(item, include_position=True)
        for item in payload.get("positions_preview", [])
    )
    sector_flow_signature = _sector_flow_signature(payload.get("sector_flow", []))
    return "|".join(
        [
            str(payload.get("data_mode", "")),
            str(payload.get("market", {}).get("updated_at", "")),
            str(payload.get("stock_board", {}).get("updated_at", "")),
            str(payload.get("stock_board", {}).get("total", "")),
            str(payload.get("selected_sector", "")),
            str(payload.get("board_level", "")),
            board_signature,
            sector_flow_signature,
            watch_signature,
            position_signature,
        ]
    )


def _stream_channel_spec(params: StreamParams) -> ChannelSpec:
    """按连接参数生成频道构建逻辑：每 tick 一次构建 + 一次编码，全体订阅者共享。"""
    if params.view == "terminal":
        tracker = TerminalDeltaTracker() if params.delta_format else None
        last_key = ""

        async def build_terminal() -> tuple[dict, str | None]:
            nonlocal last_key
            payload = (
                await asyncio.to_thread(
                    service.terminal,
                    sector=params.sector,
                    board_level=params.board_level,
                    sort=params.sort,
                    page=params.page,
                    page_size=params.page_size,
                    fast=params.fast,
                    **_stream_client_watchlist_kwargs(params),
                )
            ).model_dump(mode="json")
            if tracker is not None:
                message = tracker.next_message(payload)
                return payload, json.dumps(message) if message is not None else None
            key = _terminal_payload_key(payload)
            if key == last_key:
                return payload, None
            last_key = key
            return payload, json.dumps(payload)

        def terminal_snapshot(payload: dict) -> str:
            if tracker is not None:
                return json.dumps({"type": "snapshot", "seq": tracker.seq, "data": payload})
            return json.dumps(payload)

        return ChannelSpec(build=build_terminal, snapshot_text=terminal_snapshot, interval=_stream_interval)

    last_key = ""

    async def build_dashboard() -> tuple[dict, str | None]:
        nonlocal last_key
        payload = (
            await asyncio.to_thread(
                service.dashboard,
                sector=params.sector,
                **_stream_client_watchlist_kwargs(params),
            )
        ).model_dump(mode="json")
        key = str(payload.get("source_status", {}).get("updated_at", ""))
        if key == last_key:
            return payload, None
        last_key = key
        return payload, json.dumps(payload)

    return ChannelSpec(
        build=build_dashboard,
        snapshot_text=lambda payload: json.dumps(payload),
        interval=_stream_interval,
    )


async def _stream_legacy(websocket: WebSocket, params: StreamParams) -> None:
    """每连接轮询旧实现：WATCH_STREAM_BROADCASTER=0 或频道数超限时使用。"""
    last_payload_key = ""
    tracker = TerminalDeltaTracker() if params.delta_format and params.view == "terminal" else None
    sent_payload = False
    try:
        while True:
            if sent_payload and not _stream_refresh_policy()["should_stream"]:
                await _stream_send_static_notice(websocket, params)
                await websocket.close(code=1000)
                return
            if params.view == "terminal":
                payload_model = await asyncio.to_thread(
                    service.terminal,
                    sector=params.sector,
                    board_level=params.board_level,
                    sort=params.sort,
                    page=params.page,
                    page_size=params.page_size,
                    fast=params.fast,
                    **_stream_client_watchlist_kwargs(params),
                )
                payload = payload_model.model_dump(mode="json")
                if tracker is not None:
                    message = tracker.next_message(payload)
                    if message is not None:
                        await websocket.send_json(message)
                        sent_payload = True
                    await asyncio.sleep(_stream_interval())
                    continue
                payload_key = _terminal_payload_key(payload)
            else:
                payload = (
                    await asyncio.to_thread(
                        service.dashboard,
                        sector=params.sector,
                        **_stream_client_watchlist_kwargs(params),
                    )
                ).model_dump(mode="json")
                payload_key = str(payload.get("source_status", {}).get("updated_at", ""))
            if payload_key != last_payload_key:
                await websocket.send_json(payload)
                last_payload_key = payload_key
                sent_payload = True
            await asyncio.sleep(_stream_interval())
    except WebSocketDisconnect:
        return


def app_path() -> Path:
    return ROOT_DIR
