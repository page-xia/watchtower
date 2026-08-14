#!/usr/bin/env python
"""Opening-window extended-rules event study (historical replay).

Replays the three research_only diamond rules (high-avoid / VWAP-pullback /
low-open-recovery) over locally collected trajectory data and measures
forward returns, so thresholds can be tuned on evidence instead of guesswork.

Data sources:
- price-side ticks: ``stock_features`` snapshots in the trajectory sqlite
  (backend collects every ~10s, close to the live 6s engine cadence);
- tape stats: easy_tdx ``get_history_transaction_data`` per stock-day via the
  data router (explicit historical trade_date, never the intraday endpoint),
  cached under data/runtime/opening-research/tape/ to make re-runs cheap.

Usage (project venv):

    .\\.venv\\Scripts\\python.exe scripts\\opening_window_research.py --days 20 --top 30
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.data_sources import MarketDataRouter  # noqa: E402
from app.trajectory_store import IntradayWatchtowerStore  # noqa: E402

decode_payload = IntradayWatchtowerStore.decode_payload
from app.opening_window_rules import (  # noqa: E402
    EXTENDED_END,
    EXTENDED_START,
    RECOVERY_CUTOFF,
    RuleInput,
    evaluate_high_avoid,
    evaluate_low_open_recovery,
    evaluate_vwap_pullback_buy,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("opening_window_research")

TAPE_CACHE_DIR = settings.data_dir / "runtime" / "opening-research" / "tape"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


# ---------------------------------------------------------------- price side
def load_trade_dates(db: sqlite3.Connection, days: int) -> list[str]:
    # market_trajectory 是小表（每天 ~1400 行）；stock_features 是 80G 大表，
    # 没有 trade_date 单列索引，DISTINCT 会全表扫描，绝不能用它取日期。
    rows = db.execute(
        "SELECT DISTINCT trade_date FROM market_trajectory ORDER BY trade_date DESC LIMIT ?",
        (days,),
    ).fetchall()
    return sorted(row[0] for row in rows)


def load_universe_codes(db: sqlite3.Connection, trade_date: str, top: int) -> list[str]:
    """Universe from sector trajectory (leaders + core codes of hot sectors).

    sector_trajectory is small and indexed; sector payloads carry
    leader_code/core_codes which mirror the live pool's sector-member idea.
    """
    rows = db.execute(
        "SELECT payload_json FROM sector_trajectory WHERE trade_date = ? "
        "AND captured_at = (SELECT MAX(captured_at) FROM sector_trajectory WHERE trade_date = ?)",
        (trade_date, trade_date),
    ).fetchall()
    sectors = []
    for (payload_json,) in rows:
        payload = decode_payload(payload_json)
        if not payload:
            continue
        sectors.append(payload)
    sectors.sort(key=lambda p: _f(p.get("heat_score")), reverse=True)
    codes: list[str] = []
    for payload in sectors[:5]:
        for code in [payload.get("leader_code"), *(payload.get("core_codes") or [])]:
            code = str(code or "").strip()
            if code and code not in codes:
                codes.append(code)
            if len(codes) >= top:
                return codes
    return codes


def load_day_ticks(db: sqlite3.Connection, trade_date: str, codes: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Return per-code tick series (idx on (code, trade_date, captured_at))."""
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = db.execute(
        f"SELECT captured_at, code, price, payload_json FROM stock_features "
        f"WHERE trade_date = ? AND code IN ({placeholders}) ORDER BY captured_at",
        (trade_date, *codes),
    ).fetchall()
    by_code: dict[str, list[dict[str, Any]]] = {}
    for captured_at, code, price, payload_json in rows:
        clock = str(captured_at)[-8:-3] if len(str(captured_at)) >= 8 else ""
        if not clock or clock < "09:29" or clock > "15:05":
            continue
        try:
            payload = decode_payload(payload_json)
        except Exception:
            payload = {}
        by_code.setdefault(code, []).append(
            {
                "clock": clock,
                "price": _f(price),
                "open": _f(payload.get("open")),
                "prev_close": _f(payload.get("prev_close")),
                "high": _f(payload.get("high")),
                "amount": _f(payload.get("amount")),
                "volume": _f(payload.get("volume")),
                "limit_up": bool(payload.get("limit_up")),
            }
        )
    return by_code


# ------------------------------------------------------------------ tape side
def load_tape_points(router: MarketDataRouter, code: str, trade_date: str) -> list[dict[str, float]]:
    """Per-minute buy/sell aggregates from the historical L1 tape (cached)."""
    cache = TAPE_CACHE_DIR / trade_date / f"{code}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    observation = router.fetch_transaction_flow(code, trade_date=trade_date, full_session=True)
    points = []
    for point in getattr(observation, "points", []) or []:
        label = str(getattr(point, "time", "") or "")[:5]
        if len(label) != 5 or label == "09:25":
            continue
        points.append(
            {
                "time": label,
                "buy_amount": _f(getattr(point, "buy_amount", 0)),
                "sell_amount": _f(getattr(point, "sell_amount", 0)),
            }
        )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(points, ensure_ascii=False), encoding="utf-8")
    return points


def cumulative_ratios(points: list[dict[str, float]]) -> dict[str, float]:
    """Cumulative net buy ratio at each minute label."""
    buy = sell = 0.0
    out: dict[str, float] = {}
    for point in sorted(points, key=lambda p: p["time"]):
        buy += point["buy_amount"]
        sell += point["sell_amount"]
        if buy + sell > 0:
            out[point["time"]] = (buy - sell) / (buy + sell) * 100
    return out


def ratio_at(ratios: dict[str, float], clock: str) -> float | None:
    eligible = [minute for minute in ratios if minute <= clock]
    if not eligible:
        return None
    return ratios[max(eligible)]


# ------------------------------------------------------------------ replay
def replay_code_day(ticks: list[dict[str, Any]], ratios: dict[str, float]) -> list[dict[str, Any]]:
    """Replay the three rules; return first triggers with forward outcomes."""
    triggers: dict[str, dict[str, Any]] = {}
    session_high = 0.0
    prev_high = 0.0
    max_excess = 0.0
    final_price = next((t["price"] for t in reversed(ticks) if t["price"] > 0), 0.0)

    for index, tick in enumerate(ticks):
        clock = tick["clock"]
        price, prev_close, open_price = tick["price"], tick["prev_close"], tick["open"]
        if price <= 0 or prev_close <= 0:
            continue
        high = tick["high"] or price
        prev_high = session_high or high
        session_high = max(session_high, high)
        vwap = tick["amount"] / (tick["volume"] * 100) if tick["amount"] > 0 and tick["volume"] > 0 else 0.0
        if vwap > 0:
            max_excess = max(max_excess, (price / vwap - 1) * 100)
        if not (EXTENDED_START <= clock <= EXTENDED_END) or tick["limit_up"]:
            continue
        net = ratio_at(ratios, clock)
        inp = RuleInput(
            code="", clock=clock, price=price, open_price=open_price, prev_close=prev_close,
            session_high=session_high, prev_session_high=prev_high, vwap=vwap,
            tape_net_ratio=net, tape_ready=net is not None,
        )
        candidates = [
            evaluate_high_avoid(inp),
            evaluate_vwap_pullback_buy(inp, max_vwap_excess_pct=max_excess),
            evaluate_low_open_recovery(inp) if clock <= RECOVERY_CUTOFF else None,
        ]
        for candidate in candidates:
            if candidate is None or candidate.rule in triggers:
                continue
            # 前向收益：触发 → 10:00 / → 收盘
            price_1000 = next((t["price"] for t in ticks[index:] if t["clock"] >= "10:00" and t["price"] > 0), None)
            triggers[candidate.rule] = {
                "rule": candidate.rule,
                "side": candidate.side,
                "clock": clock,
                "price": candidate.price,
                "change_pct": candidate.change_pct,
                "tape_net_ratio": candidate.tape_net_ratio,
                "fwd_to_1000_pct": round((price_1000 / candidate.price - 1) * 100, 2) if price_1000 else None,
                "fwd_to_close_pct": round((final_price / candidate.price - 1) * 100, 2) if final_price > 0 else None,
            }
    return list(triggers.values())


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_rule.setdefault(record["rule"], []).append(record)
    summary: dict[str, Any] = {}
    for rule, items in sorted(by_rule.items()):
        def stats(key: str) -> dict[str, float | int | None]:
            values = [i[key] for i in items if i.get(key) is not None]
            if not values:
                return {"n": 0, "mean": None, "win_rate": None}
            wins = sum(1 for v in values if (v < 0 if items[0]["side"] == "sell" else v > 0))
            return {"n": len(values), "mean": round(sum(values) / len(values), 2), "win_rate": round(wins / len(values), 3)}

        summary[rule] = {
            "triggers": len(items),
            "side": items[0]["side"],
            "to_1000": stats("fwd_to_1000_pct"),
            "to_close": stats("fwd_to_close_pct"),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=20, help="最近 N 个有轨迹的交易日")
    parser.add_argument("--top", type=int, default=30, help="每日按成交额取前 N 只")
    parser.add_argument("--out", type=str, default="", help="结果 JSON 输出路径")
    args = parser.parse_args()

    db_path = settings.intraday_watchtower_db_file
    if not db_path.exists():
        logger.error("轨迹库不存在：%s", db_path)
        sys.exit(1)
    db = sqlite3.connect(str(db_path))
    router = MarketDataRouter(settings)

    dates = load_trade_dates(db, args.days)
    logger.info("replay %d trade days: %s .. %s", len(dates), dates[0] if dates else "-", dates[-1] if dates else "-")

    records: list[dict[str, Any]] = []
    for trade_date in dates:
        codes = load_universe_codes(db, trade_date, args.top)
        day_ticks = load_day_ticks(db, trade_date, codes)
        day_count = 0
        for code, ticks in day_ticks.items():
            try:
                points = load_tape_points(router, code, trade_date)
            except Exception as exc:
                logger.warning("tape fetch failed %s %s: %s", trade_date, code, exc)
                continue
            if not points:
                continue
            ratios = cumulative_ratios(points)
            for trigger in replay_code_day(ticks, ratios):
                trigger["trade_date"] = trade_date
                trigger["code"] = code
                records.append(trigger)
                day_count += 1
        logger.info("%s: %d triggers across %d codes", trade_date, day_count, len(day_ticks))

    result = {
        "sample": {"days": len(dates), "top_per_day": args.top, "triggers": len(records)},
        "summary": summarize(records),
        "records": records,
        "notes": [
            "卖出类规则（高点回避）的 win_rate 按前向收益为负计胜。",
            "分笔为 L1 成交明细（get_history_transaction_data），非委托队列。",
            "价格侧为 10s 轨迹快照，接近线上 6s tick，但非逐秒。",
        ],
    }
    out = args.out or str(settings.data_dir / "runtime" / "strategy-research" / "opening_window_latest.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("written %s", out)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
