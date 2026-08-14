from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any, Callable

from app.data_sources import _market_id_for_tdx_code


LEVEL2_FIELDS = {
    "bid_queue",
    "ask_queue",
    "bid_order_queue",
    "ask_order_queue",
    "entrust_buy_count",
    "entrust_sell_count",
    "order_queue",
    "order_detail",
}


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _keys(row: Any) -> set[str]:
    if isinstance(row, dict):
        return {str(key) for key in row.keys()}
    if is_dataclass(row):
        return {field.name for field in fields(row)}
    if hasattr(row, "keys"):
        try:
            return {str(key) for key in row.keys()}
        except Exception:
            pass
    return {key for key in dir(row) if not key.startswith("_")}


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if is_dataclass(row):
        return {field.name: getattr(row, field.name) for field in fields(row)}
    if hasattr(row, "_asdict"):
        try:
            return dict(row._asdict())
        except Exception:
            pass
    if hasattr(row, "to_dict"):
        try:
            payload = row.to_dict()
            if isinstance(payload, dict):
                return dict(payload)
        except Exception:
            pass
    if hasattr(row, "__dict__"):
        return {key: value for key, value in vars(row).items() if not key.startswith("_")}
    return {"value": row}


def _records(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if hasattr(payload, "empty") and bool(getattr(payload, "empty")):
        return []
    if hasattr(payload, "to_dict") and hasattr(payload, "columns"):
        return [dict(row) for row in payload.to_dict(orient="records")]
    if isinstance(payload, (list, tuple)):
        return [_row_dict(row) for row in payload]
    return [_row_dict(payload)]


def _jsonable(value: Any, max_text: int = 240) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if value != value else round(value, 6)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    text = str(value).strip()
    if len(text) > max_text:
        return f"{text[:max_text - 3]}..."
    return text


def _time_label(row: Any) -> str:
    raw = _value(row, "time_label") or _value(row, "time") or _value(row, "datetime")
    if raw:
        if hasattr(raw, "strftime"):
            try:
                return raw.strftime("%H:%M")
            except Exception:
                pass
        text = str(raw)
        if " " in text:
            text = text.rsplit(" ", 1)[-1]
        if "T" in text:
            text = text.rsplit("T", 1)[-1]
        if len(text) >= 5 and text[2] == ":":
            return text[:5]
        if len(text) >= 4 and text[:4].isdigit():
            return f"{text[:2]}:{text[2:4]}"
        return text[:5]
    hour = _value(row, "hour")
    minute = _value(row, "minute")
    try:
        return f"{int(hour):02d}:{int(minute):02d}"
    except (TypeError, ValueError):
        return ""


def summarize_quote_fields(rows: list[Any]) -> dict[str, Any]:
    keys = sorted({key for row in rows for key in _keys(row)})
    max_bid_level = 0
    max_ask_level = 0
    for row in rows:
        buy_levels = list(_value(row, "buy_levels", ()) or ())
        sell_levels = list(_value(row, "sell_levels", ()) or ())
        if buy_levels:
            max_bid_level = max(max_bid_level, len(buy_levels))
        if sell_levels:
            max_ask_level = max(max_ask_level, len(sell_levels))
        row_keys = _keys(row)
        for level in range(1, 11):
            if f"bid{level}" in row_keys and f"bid_vol{level}" in row_keys:
                max_bid_level = max(max_bid_level, level)
            if f"ask{level}" in row_keys and f"ask_vol{level}" in row_keys:
                max_ask_level = max(max_ask_level, level)
    depth_levels = min(max_bid_level, max_ask_level)
    ten_level_fields = sorted(
        key for key in keys if any(key.startswith(prefix) for prefix in ("bid6", "bid7", "bid8", "bid9", "bid10", "ask6", "ask7", "ask8", "ask9", "ask10"))
    )
    aggregate = sorted(
        key
        for key in keys
        if key
        in {
            "amount",
            "b_vol",
            "cur_vol",
            "current_hand",
            "inside_dish",
            "open_amount",
            "open_amount_yuan",
            "outer_disc",
            "s_vol",
            "total_hand",
            "vol",
        }
    )
    level2 = sorted(LEVEL2_FIELDS.intersection(keys))
    return {
        "quote_keys": keys,
        "quote_depth_levels": depth_levels,
        "five_level_available": depth_levels >= 5,
        "quote_depth_available": depth_levels >= 1,
        "ten_level_quote_depth": depth_levels >= 10,
        "ten_level_fields": ten_level_fields,
        "aggregate_flow_fields": aggregate,
        "level2_fields": level2,
        "level2_available": bool(level2),
        "auction_proxy_possible": bool(
            {"open_amount", "trading_status", "server_time"}.intersection(keys)
            or {"price", "cur_vol"}.issubset(keys)
        ),
    }


def summarize_transaction_fields(rows: list[Any]) -> dict[str, Any]:
    keys = sorted({key for row in rows for key in _keys(row)})
    times = [_time_label(row) for row in rows]
    times = [time for time in times if time]
    direction_counts: dict[str, int] = {}
    direction_tick_matrix: dict[str, dict[str, int]] = {}
    previous_price = 0.0
    for row in rows:
        raw_direction = _value(row, "buyorsell")
        if raw_direction in {None, ""}:
            raw_direction = _value(row, "side")
        if raw_direction in {None, ""}:
            raw_direction = _value(row, "bs_flag")
        if raw_direction in {None, ""}:
            raw_direction = _value(row, "nature")
        direction_key = str(raw_direction) if raw_direction not in {None, ""} else "missing"
        direction_counts[direction_key] = direction_counts.get(direction_key, 0) + 1
        try:
            price = float(_value(row, "price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if previous_price <= 0 or price <= 0:
            tick = "first"
        elif price > previous_price:
            tick = "up"
        elif price < previous_price:
            tick = "down"
        else:
            tick = "flat"
        bucket = direction_tick_matrix.setdefault(direction_key, {})
        bucket[tick] = bucket.get(tick, 0) + 1
        if price > 0:
            previous_price = price
    has_time = bool(times or {"time_label", "time", "datetime", "hour", "minute"}.intersection(keys))
    has_volume = bool({"volume", "vol"}.intersection(keys))
    direction_field = ""
    for field_name in ("buyorsell", "side", "bs_flag", "nature"):
        if field_name in keys:
            direction_field = field_name
            break
    return {
        "transaction_keys": keys,
        "transaction_tape_available": bool(has_time and "price" in keys and has_volume),
        "direction_field": direction_field,
        "transaction_first_time": times[0] if times else "",
        "transaction_last_time": times[-1] if times else "",
        "transaction_time_ascending": all(left <= right for left, right in zip(times, times[1:])),
        "direction_value_counts": direction_counts,
        "direction_tick_matrix": direction_tick_matrix,
    }


def summarize_auction_fields(rows: list[Any]) -> dict[str, Any]:
    keys = sorted({key for row in rows for key in _keys(row)})
    times = [_time_label(row) for row in rows]
    times = [time for time in times if time]
    return {
        "auction_keys": keys,
        "auction_series_available": bool(rows),
        "auction_series_point_count": len(rows),
        "auction_actual_fields": bool(
            {"price", "matched", "unmatched"}.issubset(keys)
            or {"price", "matched_volume", "unmatched_volume"}.issubset(keys)
        ),
        "auction_first_time": times[0] if times else "",
        "auction_last_time": times[-1] if times else "",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="探测 easy_tdx TDX L1 A 股行情能力")
    parser.add_argument("--host", default="", help="优先探测的标准 TDX 行情主机，格式可为 IP 或 IP:PORT")
    parser.add_argument("--mac-host", default="", help="优先探测的 easy_tdx MAC 主机，格式可为 IP 或 IP:PORT")
    parser.add_argument("--port", type=int, default=7709)
    parser.add_argument(
        "--codes",
        default="300476,300308,000001",
        help="逗号分隔的股票代码，最多请求 20 只",
    )
    parser.add_argument("--transaction-count", type=int, default=40)
    parser.add_argument("--date", default="", help="历史成交明细日期 YYYYMMDD；不填则请求当前成交明细")
    parser.add_argument("--timeout", type=float, default=2.0)
    return parser.parse_args()


def _normalize_host(host: str, port: int) -> str:
    host = str(host or "").strip()
    if host and ":" not in host:
        return f"{host}:{port}"
    return host


def _split_host(host: str, port: int) -> tuple[str | None, int | None]:
    normalized = _normalize_host(host, port)
    if not normalized:
        return None, None
    ip, port_text = normalized.split(":", 1)
    return ip, int(port_text)


def _open_tdx_client(host: str, port: int, timeout: float) -> Any:
    from easy_tdx import TdxClient

    ip, resolved_port = _split_host(host, port)
    if ip:
        return TdxClient(host=ip, port=resolved_port, timeout=timeout, heartbeat_interval=15.0)
    return TdxClient(timeout=timeout, heartbeat_interval=15.0)


def _open_mac_client(host: str, port: int, timeout: float) -> Any:
    from easy_tdx import MacClient

    ip, resolved_port = _split_host(host, port)
    if ip:
        return MacClient(host=ip, port=resolved_port, timeout=timeout, heartbeat_interval=15.0)
    return MacClient(timeout=timeout, heartbeat_interval=15.0)


def _quote_request(client: Any, codes: list[str]) -> list[dict[str, Any]]:
    from easy_tdx import Market

    requests = [(Market(_market_id_for_tdx_code(code)), code) for code in codes]
    return _records(client.get_security_quotes(requests))


def _transaction_request(client: Any, code: str, trade_date: str, count: int) -> list[dict[str, Any]]:
    from easy_tdx import Market

    market = Market(_market_id_for_tdx_code(code))
    if trade_date:
        return _records(client.get_history_transaction_data(market, code, int(trade_date), start=0, count=count))
    return _records(client.get_transaction_data(market, code, start=0, count=count))


def _history_0925_proxy_rows(client: Any, code: str, trade_date: str) -> list[dict[str, Any]]:
    from easy_tdx import Market

    market = Market(_market_id_for_tdx_code(code))
    rows: list[dict[str, Any]] = []
    page_size = 800
    for page_index in range(8):
        batch = _records(
            client.get_history_transaction_data(
                market,
                code,
                int(trade_date),
                start=page_index * page_size,
                count=page_size,
            )
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
    return [row for row in rows if _time_label(row) == "09:25"]


def _auction_request(client: Any, code: str) -> list[dict[str, Any]]:
    return _records(client.get_auction(_market_id_for_tdx_code(code), code))


def _safe(name: str, fn: Callable[[], Any]) -> tuple[Any, str]:
    try:
        return fn(), ""
    except Exception as exc:  # pragma: no cover - network/server dependent
        return None, f"{type(exc).__name__}: {exc}"


def _basic_table_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = sorted({key for row in rows for key in row.keys()})
    return {
        "available": bool(rows),
        "row_count": len(rows),
        "columns": keys,
        "sample": rows[-1] if rows else {},
    }


def _detail_capability_probe(tdx_client: Any, mac_client: Any, code: str) -> dict[str, Any]:
    from easy_tdx import Adjust, Market, Period
    from easy_tdx.chanlun.analyser import ChanlunAnalyser

    result: dict[str, Any] = {}
    finance_rows, finance_error = _safe(
        "finance_info",
        lambda: _records(tdx_client.get_finance_info(Market(_market_id_for_tdx_code(code)), code)),
    )
    company_rows, company_error = _safe(
        "company_info_category",
        lambda: _records(tdx_client.get_company_info_category(Market(_market_id_for_tdx_code(code)), code)),
    )
    result["fundamentals_f10"] = {
        "finance_info": _basic_table_summary(finance_rows or []),
        "company_info_category": _basic_table_summary(company_rows or []),
        "errors": [error for error in (finance_error, company_error) if error],
    }

    capital_rows, capital_error = _safe(
        "capital_flow",
        lambda: _records(mac_client.get_capital_flow(_market_id_for_tdx_code(code), code)),
    )
    result["capital_flow"] = {
        **_basic_table_summary(capital_rows or []),
        "error": capital_error,
    }

    indicator_rows, indicator_error = _safe(
        "technical_indicators",
        lambda: _records(
            mac_client.get_stock_kline_with_indicators(
                _market_id_for_tdx_code(code),
                code,
                ["MACD", "KDJ", "RSI", "BOLL", "OBV", "ATR"],
                period=Period.DAILY,
                count=120,
                adjust=Adjust.QFQ,
            )
        ),
    )
    result["technical_indicators"] = {
        **_basic_table_summary(indicator_rows or []),
        "error": indicator_error,
    }

    def _chanlun_probe() -> dict[str, Any]:
        frame = mac_client.get_stock_kline(
            _market_id_for_tdx_code(code),
            code,
            period=Period.DAILY,
            count=800,
            adjust=Adjust.QFQ,
        )
        if getattr(frame, "empty", True):
            return {"available": False, "kline_count": 0}
        prefix = "SH" if _market_id_for_tdx_code(code) == 1 else "BJ" if _market_id_for_tdx_code(code) == 2 else "SZ"
        analysed = ChanlunAnalyser(f"{prefix}{code}", "DAILY").process_klines(frame)
        payload = analysed.to_dict() if hasattr(analysed, "to_dict") else {}
        return {
            "available": bool(payload),
            "kline_count": payload.get("kline_count"),
            "bi_count": payload.get("bi_count"),
            "zs_count": payload.get("zs_count"),
            "xd_count": payload.get("xd_count"),
            "mmd_count": payload.get("mmd_count"),
            "bc_count": payload.get("bc_count"),
        }

    chanlun_summary, chanlun_error = _safe("chanlun", _chanlun_probe)
    result["chanlun"] = {
        **(chanlun_summary or {"available": False}),
        "error": chanlun_error,
    }
    return result


def main() -> int:
    args = parse_args()
    codes = [str(code).strip().zfill(6) for code in args.codes.split(",") if str(code).strip()][:20]
    if not codes:
        print(json.dumps({"ok": False, "error": "codes不能为空"}, ensure_ascii=False, indent=2))
        return 2
    host = _normalize_host(args.host, args.port) if args.host else ""
    mac_host = _normalize_host(args.mac_host, args.port) if args.mac_host else ""

    try:
        import easy_tdx
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"easy_tdx不可用：{exc}"}, ensure_ascii=False, indent=2))
        return 2

    quote_rows: list[dict[str, Any]] = []
    transaction_rows: list[dict[str, Any]] = []
    auction_rows: list[dict[str, Any]] = []
    auction_0925_rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    detail_capabilities: dict[str, Any] = {}

    tdx_client = _open_tdx_client(host, args.port, args.timeout)
    mac_client = _open_mac_client(mac_host, args.port, args.timeout)
    try:
        with tdx_client as client:
            quote_rows, errors["quote"] = _safe("quote", lambda: _quote_request(client, codes))
            transaction_rows, errors["transaction"] = _safe(
                "transaction",
                lambda: _transaction_request(
                    client,
                    codes[0],
                    args.date,
                    max(1, min(args.transaction_count, 800)),
                ),
            )
            if args.date:
                auction_0925_rows, errors["auction_0925_proxy"] = _safe(
                    "auction_0925_proxy",
                    lambda: _history_0925_proxy_rows(client, codes[0], args.date),
                )
            with mac_client as mclient:
                auction_rows, errors["auction"] = _safe("auction", lambda: _auction_request(mclient, codes[0]))
                detail_capabilities = _detail_capability_probe(client, mclient, codes[0])
    except Exception as exc:  # pragma: no cover - network/server dependent
        errors["connection"] = f"{type(exc).__name__}: {exc}"

    quote_rows = quote_rows or []
    transaction_rows = transaction_rows or []
    auction_rows = auction_rows or []
    auction_0925_rows = auction_0925_rows or []
    errors = {key: value for key, value in errors.items() if value}
    quote_summary = summarize_quote_fields(quote_rows)
    transaction_summary = summarize_transaction_fields(transaction_rows)
    auction_summary = summarize_auction_fields(auction_rows)
    auction_0925_summary = {
        "auction_0925_direct_available": False,
        "auction_0925_direct_method": "",
        "auction_0925_direct_rows": [],
        "auction_0925_transaction_proxy_available": bool(auction_0925_rows),
        "auction_0925_proxy_transaction_count": len(auction_0925_rows),
        "auction_0925_strategy": (
            "easy_tdx公开接口不承诺历史09:25直接竞价快照；本探针使用 "
            "get_history_transaction_data 扫描09:25成交作为proxy回填。"
        ),
    }
    payload = {
        "ok": True,
        "package": "easy-tdx",
        "package_version": getattr(easy_tdx, "__version__", ""),
        "observed_at": datetime.now().isoformat(timespec="seconds"),
        "host": host or "package_default",
        "mac_host": mac_host or "package_default",
        "codes": codes,
        "quote_count": len(quote_rows),
        **quote_summary,
        "transaction_sample_code": codes[0],
        "transaction_trade_date": args.date or "current",
        "transaction_count": len(transaction_rows),
        **transaction_summary,
        **auction_summary,
        **auction_0925_summary,
        "fundamentals_f10": detail_capabilities.get("fundamentals_f10", {}),
        "capital_flow": detail_capabilities.get("capital_flow", {}),
        "technical_indicators": detail_capabilities.get("technical_indicators", {}),
        "chanlun": detail_capabilities.get("chanlun", {}),
        "errors": errors,
        "conclusion": {
            "quote_depth": f"实际探测到 {quote_summary['quote_depth_levels']} 档；系统只接入五档盘口。",
            "ten_level_quote_depth": "未发现公开十档盘口字段，ten_level_quote_depth=false。",
            "auction": "当前日集合竞价可用 MacClient.get_auction；历史09:25用历史成交明细09:25 proxy，不使用 easy_tdx 之外的数据源兜底。",
            "tdx_l1": "五档盘口、逐笔成交、集合竞价均按 TDX L1/代理数据处理，不称为委托队列或隐藏主单。",
            "transaction": (
                "可读取成交明细 L1；优先使用 buyorsell/side/bs_flag 字段，特殊状态按中性"
                if transaction_summary["transaction_tape_available"]
                else "未读取到成交明细"
            ),
            "license": "仅限个人非商业研究用途，不用于商业、付费或生产服务。",
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_jsonable))
    return 0


if __name__ == "__main__":
    sys.exit(main())

