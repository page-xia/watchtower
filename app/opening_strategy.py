from __future__ import annotations

"""Opening-window decision logic for the 09:33-09:37 workflow.

The online terminal and the offline event study both use this module.  It is
intentionally a rules engine rather than a prediction model: auction data can
raise a candidate's score, but an opening decision still needs market, sector
and stock confirmation after the continuous session starts.

The easy_tdx TDX L1 quote packet does not provide historical queue data.
Consequently ``flow_score`` is treated as an L1/five-level or minute
price-amount proxy and is never described as a true large-order feed.
"""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.models import (
    AuctionSnapshot,
    IndexSnapshot,
    MarketState,
    OpeningAction,
    OpeningDecisionItem,
    OpeningDecisionPayload,
    Quote,
    SectorFlowSeries,
    SectorSnapshot,
    TradeSignal,
    WatchlistItem,
)


CHECKPOINTS = ("09:33", "09:35", "09:37")


@dataclass(frozen=True)
class OpeningStage:
    key: str
    label: str
    checkpoint: str
    active: bool
    can_execute: bool


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是", "有"}
    return bool(value)


def _pct(current: float, reference: float) -> float:
    return (current - reference) / reference * 100 if reference else 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _clock_minutes(value: str | None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    # Accept both HH:MM and HH:MM:SS, and tolerate an ISO timestamp.
    if "T" in text:
        text = text.split("T", 1)[1]
    text = text.replace(" ", ":") if text.count(":") == 0 else text
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0][-2:])
        minute = int(parts[1][:2])
        second = int(parts[2][:2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute + second / 60


def _normal_breadth(value: Any) -> float:
    """Return breadth as a percentage regardless of the caller's convention."""
    breadth = _number(value)
    if 0 <= breadth <= 1:
        breadth *= 100
    return _clamp(breadth, 0, 100)


class OpeningStrategy:
    """Score the first seven minutes using three explicit confirmation gates."""

    DEFAULTS: dict[str, float | int | bool] = {
        # These are starting hypotheses.  They are deliberately not fitted to
        # the two currently available study days.
        "opening_market_breadth_min": 52,
        "opening_market_breadth_risk": 42,
        "opening_market_volume_ratio_min": 1.08,
        "opening_market_score_min": 55,
        "opening_sector_heat_min": 55,
        "opening_sector_breadth_min": 52,
        "opening_sector_score_min": 58,
        "opening_stock_volume_ratio_min": 1.20,
        "opening_stock_same_minute_ratio_min": 1.15,
        "opening_stock_score_min": 58,
        "opening_stock_relative_lag_max": 1.20,
        "opening_stock_slope_min_pct": 0.05,
        "opening_flow_support_min": 12,
        "opening_flow_pressure_max": -25,
        "opening_auction_change_min": 0.50,
        "opening_auction_imbalance_min": 8,
        "opening_chase_gap_max": 5.0,
        "opening_chase_rebound_max": 6.5,
        "opening_defense_rebound_min": 7.5,
        "opening_persistence_min": 0.0,
        "opening_top_candidates": 12,
        "opening_top_defense": 10,
        "opening_cache_records": 12,
    }

    def __init__(
        self,
        rules: Mapping[str, Any] | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        raw: Mapping[str, Any] = rules or {}
        if isinstance(raw.get("thresholds"), Mapping):
            raw = raw["thresholds"]  # type: ignore[assignment]
        self.rules: dict[str, Any] = {**self.DEFAULTS, **dict(raw)}
        self.persist_path = Path(persist_path) if persist_path else None
        self._latest_payload: OpeningDecisionPayload | None = None
        self._latest_items: dict[str, OpeningDecisionItem] = {}
        self._latest_date = ""

    def stage_for(self, clock_label: str | None, *, frozen: bool = False) -> OpeningStage:
        minutes = _clock_minutes(clock_label)
        if minutes is None:
            return OpeningStage("unavailable", "无开盘时间", "", False, False)
        if frozen:
            return OpeningStage("closed", "开盘窗口结束", "", False, False)
        if minutes < 9 * 60 + 15:
            return OpeningStage("auction", "竞价准备", "", True, False)
        if minutes < 9 * 60 + 30:
            return OpeningStage("auction", "集合竞价", "", True, False)
        if minutes < 9 * 60 + 33:
            return OpeningStage("sampling", "开盘采样", "", True, False)
        if minutes < 9 * 60 + 35:
            return OpeningStage("screen", "09:33 初筛", "09:33", True, False)
        if minutes < 9 * 60 + 37:
            return OpeningStage("confirm", "09:35 确认", "09:35", True, True)
        if minutes < 9 * 60 + 38:
            return OpeningStage("review", "09:37 复核", "09:37", True, True)
        return OpeningStage("closed", "开盘窗口结束", "", False, False)

    def evaluate(
        self,
        *,
        trade_date: str,
        clock_label: str | None,
        data_mode: str,
        frozen: bool,
        quotes: Sequence[Quote],
        indices: Sequence[IndexSnapshot],
        market: MarketState | Mapping[str, Any] | None,
        sectors: Sequence[SectorSnapshot],
        sector_flow: Sequence[SectorFlowSeries] | None = None,
        signals: Sequence[TradeSignal] | None = None,
        watchlist: Sequence[WatchlistItem] | None = None,
    ) -> OpeningDecisionPayload:
        """Evaluate a live snapshot.

        A closed daily snapshot is never used to infer an opening signal.  If a
        real opening checkpoint was persisted earlier in the same trade date,
        that checkpoint is returned for replay instead.
        """
        normalized_date = str(trade_date or "").strip()
        stage = self.stage_for(clock_label, frozen=frozen)
        if frozen or str(data_mode or "").lower() in {"closed_static", "unavailable"} or stage.key == "closed":
            cached = self._load_latest(normalized_date)
            if cached is not None:
                return self._closed_replay(cached, normalized_date, clock_label)
            return self._unavailable_payload(
                normalized_date,
                clock_label,
                "收盘快照没有保存的09:33/09:35/09:37开盘采样，不能用全天数据反推开盘买卖点",
            )
        if not normalized_date or not quotes or not indices or not sectors:
            return self._unavailable_payload(normalized_date, clock_label, "开盘快照字段不足，暂不生成决策")

        market_result = self._score_market(market, indices)
        sector_results = {
            str(_value(sector, "name", "未归类")): self._score_sector(
                sector,
                flow=self._flow_for_sector(sector_flow or [], str(_value(sector, "name", ""))),
            )
            for sector in sectors
        }
        signal_by_code = {str(_value(signal, "code", "")).zfill(6): signal for signal in (signals or [])}
        watch_codes = {str(_value(item, "code", "")).zfill(6) for item in (watchlist or [])}
        items: dict[str, OpeningDecisionItem] = {}
        for quote in quotes:
            code = str(_value(quote, "code", "")).zfill(6)
            sector = self._best_sector(quote, sectors)
            sector_name = str(_value(sector, "name", "未归类")) if sector else "未归类"
            sector_result = sector_results.get(sector_name, self._empty_sector_result())
            item = self._evaluate_stock(
                quote,
                sector_name=sector_name,
                sector_result=sector_result,
                market_result=market_result,
                stage=stage,
                signal=signal_by_code.get(code),
                position=code in watch_codes,
            )
            items[code] = item

        self._latest_date = normalized_date
        self._latest_items = items
        candidates = sorted(
            [item for item in items.values() if item.action in {OpeningAction.BUY, OpeningAction.SCREEN, OpeningAction.AUCTION, OpeningAction.WATCH}],
            key=lambda item: (-item.score, item.code),
        )
        defense = sorted(
            [item for item in items.values() if item.action in {OpeningAction.AVOID, OpeningAction.REDUCE}],
            key=lambda item: (-item.score, item.code),
        )
        market_gate = "风险" if market_result["risk"] else "通过" if market_result["gate"] else "等待"
        payload = OpeningDecisionPayload(
            trade_date=normalized_date,
            updated_at=str(clock_label or ""),
            stage=stage.key,
            stage_label=stage.label,
            checkpoint=stage.checkpoint,
            active=stage.active,
            can_execute=stage.can_execute and bool(market_result["gate"]),
            frozen=False,
            scope="full_market",
            total=len(quotes),
            market_gate=market_gate,
            market_score=int(round(market_result["score"])),
            candidate_count=sum(1 for item in items.values() if item.action in {OpeningAction.AUCTION, OpeningAction.SCREEN}),
            buy_count=sum(1 for item in items.values() if item.action == OpeningAction.BUY),
            defense_count=len(defense),
            market_reasons=list(market_result["reasons"]),
            top_candidates=candidates[: self._int("opening_top_candidates", 12)],
            top_defense=defense[: self._int("opening_top_defense", 10)],
            methodology=self.methodology(),
            data_quality=self._data_quality(data_mode, quotes),
            data_note=self._data_note(data_mode, quotes, stage),
            research={"checkpoints": list(CHECKPOINTS), "thresholds_are_hypotheses": True},
        )
        self._latest_payload = payload
        if stage.checkpoint:
            self._persist(payload, items)
        return payload

    def item_for(self, code: str) -> OpeningDecisionItem | None:
        return self._latest_items.get(str(code or "").zfill(6))

    def latest_payload(self) -> OpeningDecisionPayload | None:
        return self._latest_payload

    def methodology(self) -> list[str]:
        return [
            "09:33只做初筛，不直接提示买T；竞价只作为候选加分，不替代开盘后确认",
            "09:35必须同时通过市场、板块、个股三层门槛，才输出确认买T",
            "市场层：指数拐头/低位恢复 + 开盘短周期量能 + 上涨宽度；风险状态暂停买T",
            "板块层：热度、上涨宽度、核心容量票进攻或上板/回封，并观察动能是否持续",
            "个股层：站上开盘参考价/VWAP代理、分时放量、相对板块不掉队，明显抛压一票否决",
            "09:37复核：保留共振、撤销失效候选；高位反弹和量价衰竭转为回避/减T",
            "没有队列数据时，大单观察使用L1五档/逐笔成交或分钟成交额代理，绝不伪装成真实逐笔大单",
        ]

    def evaluate_historical_point(
        self,
        *,
        trade_date: str,
        checkpoint: str,
        stock: Mapping[str, Any],
        market: Mapping[str, Any],
        sector: Mapping[str, Any],
        sector_name: str = "未归类",
        position: bool = False,
        auction: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply the same gates to a point-in-time historical feature set.

        ``stock``, ``market`` and ``sector`` must contain only values through
        ``checkpoint``.  This small adapter keeps the research code independent
        from online Pydantic models and makes the information boundary obvious.
        """
        stage = self._stage_from_checkpoint(checkpoint)
        market_result = self._score_market_features(market)
        sector_result = self._score_sector_features(sector)
        item = self._evaluate_feature_stock(
            stock,
            sector_name=sector_name,
            sector_result=sector_result,
            market_result=market_result,
            stage=stage,
            position=position,
            auction=auction,
        )
        return {
            "trade_date": trade_date,
            "checkpoint": checkpoint,
            "stage": stage.key,
            "market": market_result,
            "sector": sector_result,
            "item": item.model_dump(mode="json"),
        }

    def _stage_from_checkpoint(self, checkpoint: str) -> OpeningStage:
        value = str(checkpoint or "")[:5]
        if value == "09:33":
            return OpeningStage("screen", "09:33 初筛", "09:33", True, False)
        if value == "09:35":
            return OpeningStage("confirm", "09:35 确认", "09:35", True, True)
        if value == "09:37":
            return OpeningStage("review", "09:37 复核", "09:37", True, True)
        return OpeningStage("unavailable", "无开盘时间", value, False, False)

    def _evaluate_stock(
        self,
        quote: Quote,
        *,
        sector_name: str,
        sector_result: dict[str, Any],
        market_result: dict[str, Any],
        stage: OpeningStage,
        signal: TradeSignal | None,
        position: bool,
    ) -> OpeningDecisionItem:
        auction = _value(quote, "auction", None)
        flow = _value(quote, "order_flow", None)
        stock = {
            "price": _number(_value(quote, "price")),
            "prev_close": _number(_value(quote, "prev_close")),
            "open": _number(_value(quote, "open")),
            "change_pct": _number(_value(quote, "change_pct")),
            "minute_amount_ratio": _number(_value(quote, "minute_amount_ratio"), 1),
            "rebound": self._rebound(quote),
            "pullback": self._pullback(quote),
            "flow_score": _number(_value(flow, "score")),
            "flow_direction": str(_value(flow, "direction", "")),
            "flow_available": _bool(_value(flow, "available")),
            "auction": auction,
            "core": _bool(_value(quote, "core")),
            "limit_up": _bool(_value(quote, "limit_up")),
            "opened_limit": _bool(_value(quote, "opened_limit")),
        }
        return self._evaluate_feature_stock(
            stock,
            sector_name=sector_name,
            sector_result=sector_result,
            market_result=market_result,
            stage=stage,
            position=position,
            auction=auction,
            code=str(_value(quote, "code", "")).zfill(6),
            name=str(_value(quote, "name", "")),
            updated_at=str(_value(quote, "updated_at", "")),
            data_quality="live_l1" if stock["flow_available"] else "live_minute_proxy",
        )

    def _evaluate_feature_stock(
        self,
        stock: Mapping[str, Any],
        *,
        sector_name: str,
        sector_result: dict[str, Any],
        market_result: dict[str, Any],
        stage: OpeningStage,
        position: bool,
        auction: Mapping[str, Any] | AuctionSnapshot | None = None,
        code: str = "",
        name: str = "",
        updated_at: str = "",
        data_quality: str = "historical_proxy",
    ) -> OpeningDecisionItem:
        price = _number(_value(stock, "price"))
        reference = _number(_value(stock, "vwap")) or _number(_value(stock, "open")) or _number(_value(stock, "prev_close"))
        open_price = _number(_value(stock, "open")) or _number(_value(stock, "prev_close"))
        change_pct = _number(_value(stock, "change_pct"))
        from_open = _pct(price, open_price)
        volume_ratio = _number(_value(stock, "minute_amount_ratio", _value(stock, "amount_ratio", 1)), 1)
        same_minute = _number(_value(stock, "same_minute_amount_ratio", volume_ratio), volume_ratio)
        slope = _number(_value(stock, "slope3", _value(stock, "price_slope3", from_open)))
        relative = change_pct - _number(_value(sector_result, "avg_change"), _number(_value(sector_result, "avg_change_pct")))
        flow_score = _number(_value(stock, "flow_score"))
        flow_available = _bool(_value(stock, "flow_available"))
        flow_direction = str(_value(stock, "flow_direction", ""))
        large_imbalance = _number(_value(stock, "large_imbalance", _value(stock, "large_imbalance_pct", 0)))
        formula_support_score = _number(_value(stock, "formula_support_score"))
        formula_exhaustion_score = _number(_value(stock, "formula_exhaustion_score"))
        formula_position_pct = _number(_value(stock, "formula_position_pct"))
        price_ok = bool(price > 0 and reference > 0 and price >= reference * (1 - 0.0005))
        formula_support = bool(
            _bool(_value(stock, "formula_support"))
            or formula_support_score >= 55
            or (price_ok and slope >= 0 and volume_ratio >= 1)
        )
        formula_exhaustion = bool(
            _bool(_value(stock, "formula_exhaustion"))
            or formula_exhaustion_score >= 78
            or (formula_position_pct >= 80 and slope <= 0)
        )
        flow_context = self._opening_flow_context(
            flow_score=flow_score,
            large_imbalance=large_imbalance,
            flow_available=flow_available,
            flow_direction=flow_direction,
            price_ok=price_ok,
            slope=slope,
            change_pct=change_pct,
            formula_support=formula_support,
            formula_exhaustion=formula_exhaustion,
        )
        flow_pressure = bool(flow_context["pressure"])
        flow_support = bool(flow_context["support"])
        slope_ok = slope >= self._number_rule("opening_stock_slope_min_pct", 0.05)
        volume_ok = bool(
            volume_ratio >= self._number_rule("opening_stock_volume_ratio_min", 1.2)
            or same_minute >= self._number_rule("opening_stock_same_minute_ratio_min", 1.15)
        )
        relative_ok = relative >= -self._number_rule("opening_stock_relative_lag_max", 1.2)
        chase_gap = max(0.0, _number(_value(stock, "open_gap", from_open)))
        rebound = _number(_value(stock, "rebound"))
        chase = bool(
            chase_gap >= self._number_rule("opening_chase_gap_max", 5.0)
            or rebound >= self._number_rule("opening_chase_rebound_max", 6.5)
        )
        auction_change, auction_imbalance, auction_good = self._auction_values(auction)
        if auction_good:
            auction_bonus = 8
        elif auction is not None and _bool(_value(auction, "available")):
            auction_bonus = 2
        else:
            auction_bonus = 0

        score = 0.0
        reasons: list[str] = []
        risks: list[str] = []
        if price_ok:
            score += 18
            reasons.append(f"价格站上开盘参考/VWAP代理（{reference:.2f}）")
        else:
            risks.append("价格仍在开盘参考价下方")
        if slope_ok:
            score += 18
            reasons.append(f"开盘短斜率转正（{slope:+.2f}%）")
        if volume_ok:
            score += 24
            reasons.append(f"分时放量（局部{volume_ratio:.2f}倍/同比分钟{same_minute:.2f}倍）")
        else:
            risks.append("分时量能尚未放大")
        if relative_ok:
            score += 16
            reasons.append(f"相对板块不掉队（差值{relative:+.2f}%）")
        else:
            risks.append(f"相对板块掉队（差值{relative:+.2f}%）")
        if flow_context["support"]:
            score += int(flow_context["score_delta"])
            reasons.extend(flow_context["reasons"])
        if flow_context["pressure"]:
            score -= int(abs(flow_context["score_delta"]))
            risks.extend(flow_context["risks"])
        if auction_good:
            score += auction_bonus
            reasons.append(f"竞价偏强加分（涨跌{auction_change:+.2f}%，深度差{auction_imbalance:+.1f}%）")
        if _bool(_value(stock, "core")):
            score += 4
            reasons.append("配置/板块核心容量票")
        if _bool(_value(stock, "limit_up")) or _bool(_value(stock, "opened_limit")):
            score += 4
            reasons.append("板块强确认：上板/回封或炸板承接代理")
        if chase:
            score -= 12
            risks.append(f"开盘追涨风险：开盘强度/低点反弹{max(chase_gap, rebound):.1f}%")
        if formula_support and not formula_exhaustion:
            score += 4
            reasons.append("公式位置：低位承接/短趋势转强")
        if formula_exhaustion:
            score -= 5
            risks.append("短线高位钝化，需防量价衰竭")
        score = int(round(_clamp(score, 0, 100)))

        market_gate = bool(market_result.get("gate"))
        sector_gate = bool(sector_result.get("gate"))
        stock_gate = bool(
            price_ok
            and slope_ok
            and volume_ok
            and relative_ok
            and not flow_pressure
            and not formula_exhaustion
            and score >= self._number_rule("opening_stock_score_min", 58)
        )
        partial = bool(price_ok and (slope_ok or volume_ok) and not flow_pressure and not formula_exhaustion)
        risk_market = bool(market_result.get("risk"))
        action = OpeningAction.WATCH
        can_execute = False
        if stage.key == "auction":
            action = OpeningAction.AUCTION if auction_good and not risk_market else OpeningAction.WATCH
        elif stage.key in {"sampling", "screen"}:
            action = OpeningAction.SCREEN if partial and not risk_market and sector_gate else OpeningAction.WATCH
            if flow_pressure or risk_market:
                action = OpeningAction.AVOID
        elif stage.key in {"confirm", "review"}:
            if risk_market:
                action = OpeningAction.REDUCE if position and (rebound >= self._number_rule("opening_defense_rebound_min", 7.5) or flow_pressure) else OpeningAction.AVOID
            elif market_gate and sector_gate and stock_gate:
                action = OpeningAction.BUY
                can_execute = True
            elif flow_pressure:
                action = OpeningAction.REDUCE if position else OpeningAction.AVOID
            elif partial or (market_gate and sector_gate):
                action = OpeningAction.WATCH
        elif stage.key == "closed":
            action = OpeningAction.CLOSED
        else:
            action = OpeningAction.UNAVAILABLE

        if stage.key == "review" and action == OpeningAction.BUY and (chase or rebound >= self._number_rule("opening_defense_rebound_min", 7.5)):
            action = OpeningAction.REDUCE if position else OpeningAction.AVOID
            can_execute = False
            risks.append("09:37复核进入高位兑现/追涨风险区")

        if not reasons:
            reasons.append("三层条件尚未形成共振")
        if stage.key == "screen":
            reasons.insert(0, "09:33初筛：只列入候选，不直接执行")
        elif stage.key == "confirm" and action == OpeningAction.BUY:
            reasons.insert(0, "09:35确认：市场+板块+个股三层门槛通过")
        elif stage.key == "review":
            reasons.insert(0, "09:37复核：检查共振是否延续和是否进入兑现区")

        return OpeningDecisionItem(
            code=code or str(_value(stock, "code", "")).zfill(6),
            name=name or str(_value(stock, "name", code or "")),
            sector=sector_name or "未归类",
            action=action,
            score=score,
            stage=stage.key,
            stage_label=stage.label,
            checkpoint=stage.checkpoint,
            can_execute=can_execute,
            market_score=int(round(_number(market_result.get("score")))),
            sector_score=int(round(_number(sector_result.get("score")))),
            stock_score=score,
            market_gate=market_gate,
            sector_gate=sector_gate,
            stock_gate=stock_gate,
            flow_pressure=flow_pressure,
            position=position,
            reasons=reasons[:8],
            risks=risks[:6],
            data_quality=data_quality,
            updated_at=updated_at,
            price=round(price, 4),
            change_pct=round(change_pct, 3),
            minute_amount_ratio=round(volume_ratio, 3),
            flow_direction=flow_context["label"] or flow_direction or ("L1/量价代理" if flow_available else "无盘口"),
            auction_change_pct=round(auction_change, 3),
        )

    def _score_market(self, market: Any, indices: Sequence[IndexSnapshot]) -> dict[str, Any]:
        values = {
            "breadth": _normal_breadth(_value(market, "breadth_pct", 0)),
            "turning": _bool(_value(market, "index_turning", False)),
            "amount_expanding": _bool(_value(market, "amount_expanding", False)),
            "emotion": _number(_value(market, "emotion_score", 50), 50),
        }
        if indices:
            primary = next((item for item in indices if str(_value(item, "code", "")) == "000001"), indices[0])
            values.update(
                {
                    "index_change": _number(_value(primary, "change_pct")),
                    "index_rebound": _number(_value(primary, "rebound_from_low_pct")),
                    "index_volume_ratio": max(
                        [_number(_value(item, "minute_amount_ratio"), 1) for item in indices],
                        default=1,
                    ),
                    "index_recovering": _number(_value(primary, "price")) >= _number(_value(primary, "open")) and _number(_value(primary, "change_pct")) >= -0.15,
                }
            )
        return self._score_market_features(values)

    def _score_market_features(self, values: Mapping[str, Any]) -> dict[str, Any]:
        breadth = _normal_breadth(values.get("breadth", values.get("breadth_pct", 0)))
        volume_ratio = _number(values.get("index_volume_ratio", values.get("amount_ratio", 1)), 1)
        turning = _bool(values.get("turning", values.get("index_turning", False)))
        recovering = _bool(values.get("index_recovering", False)) or _number(values.get("rebound", values.get("index_rebound", 0))) >= self._number_rule("opening_index_rebound_min", 0.08)
        index_change = _number(values.get("index_change", values.get("change_pct", 0)))
        emotion = _number(values.get("emotion", values.get("emotion_score", 50)), 50)
        amount_expanding = _bool(values.get("amount_expanding")) or volume_ratio >= self._number_rule("opening_market_volume_ratio_min", 1.08)
        score = (
            _clamp((breadth - 35) / 30 * 30, 0, 30)
            + _clamp((volume_ratio - 0.85) / 0.55 * 25, 0, 25)
            + (24 if turning or recovering else 0)
            + _clamp(emotion / 100 * 15, 0, 15)
            + (6 if index_change >= 0 else 0)
        )
        risk = bool(
            (breadth < self._number_rule("opening_market_breadth_risk", 42) and not (turning or recovering))
            or (index_change <= -0.8 and not (turning or recovering) and volume_ratio < 1.0)
        )
        gate = bool(
            not risk
            and breadth >= self._number_rule("opening_market_breadth_min", 52)
            and volume_ratio >= self._number_rule("opening_market_volume_ratio_min", 1.08)
            and (turning or recovering)
            and score >= self._number_rule("opening_market_score_min", 55)
        )
        reasons: list[str] = [f"市场上涨宽度{breadth:.0f}%", f"指数开盘量能{volume_ratio:.2f}倍"]
        if turning or recovering:
            reasons.append("指数出现拐头/低位恢复")
        else:
            reasons.append("指数拐头尚未确认")
        if amount_expanding:
            reasons.append("短周期成交额放大")
        if risk:
            reasons.append("市场处于风险分歧，暂停买T")
        return {
            "score": int(round(_clamp(score, 0, 100))),
            "breadth": round(breadth, 2),
            "volume_ratio": round(volume_ratio, 3),
            "turning": turning,
            "recovering": recovering,
            "amount_expanding": amount_expanding,
            "risk": risk,
            "gate": gate,
            "reasons": reasons,
        }

    def _score_sector(self, sector: Any, *, flow: Any = None) -> dict[str, Any]:
        return self._score_sector_features(
            {
                "heat": _number(_value(sector, "heat_score")),
                "breadth": _normal_breadth(
                    _number(_value(sector, "up_count")) / max(1, _number(_value(sector, "total_count"))) * 100
                ),
                "avg_change": _number(_value(sector, "avg_change_pct")),
                "core_attack": _bool(_value(sector, "core_attack")),
                "limit_up_count": _number(_value(sector, "limit_up_count")),
                "opened_limit_count": _number(_value(sector, "opened_limit_count")),
                "flow_delta": _number(_value(sector, "flow_delta")),
                "auction_confirmed": _bool(_value(sector, "auction_confirmed")),
                "flow_persistence": self._flow_persistence(flow),
            }
        )

    def _score_sector_features(self, values: Mapping[str, Any]) -> dict[str, Any]:
        heat = _number(values.get("heat", values.get("heat_score")), 0)
        breadth = _normal_breadth(values.get("breadth", values.get("breadth_pct", 0)))
        avg_change = _number(values.get("avg_change", values.get("avg_change_pct")), 0)
        core_attack = _bool(values.get("core_attack"))
        limit_count = _number(values.get("limit_up_count"))
        opened_count = _number(values.get("opened_limit_count"))
        flow_delta = _number(values.get("flow_delta"))
        persistent = _bool(values.get("flow_persistence")) or flow_delta >= self._number_rule("opening_persistence_min", 0)
        score = (
            _clamp(heat * 0.38, 0, 38)
            + _clamp((breadth - 35) / 30 * 25, 0, 25)
            + _clamp((avg_change + 1) / 4 * 15, 0, 15)
            + (14 if core_attack else 0)
            + min(8, limit_count * 4)
            + (4 if persistent else 0)
        )
        score = int(round(_clamp(score, 0, 100)))
        gate = bool(
            score >= self._number_rule("opening_sector_score_min", 58)
            and heat >= self._number_rule("opening_sector_heat_min", 55)
            and breadth >= self._number_rule("opening_sector_breadth_min", 52)
            and (core_attack or limit_count >= 1)
            and not (avg_change < -0.5 and opened_count > limit_count)
        )
        reasons = [f"板块热度{heat:.0f}分", f"上涨宽度{breadth:.0f}%", f"平均涨跌{avg_change:+.2f}%"]
        if core_attack:
            reasons.append("核心容量票出现进攻")
        if limit_count:
            reasons.append(f"上板/涨停{int(limit_count)}只")
        if opened_count:
            reasons.append(f"炸板/开板{int(opened_count)}只")
        if persistent:
            reasons.append("板块动能不是单点脉冲")
        return {
            "score": score,
            "heat": round(heat, 2),
            "breadth": round(breadth, 2),
            "avg_change": round(avg_change, 3),
            "core_attack": core_attack,
            "limit_up_count": int(limit_count),
            "opened_limit_count": int(opened_count),
            "persistent": persistent,
            "gate": gate,
            "reasons": reasons,
        }

    def _flow_for_sector(self, series: Sequence[SectorFlowSeries], name: str) -> Any:
        for item in series:
            if str(_value(item, "name", "")) == name:
                return item
        return None

    def _flow_persistence(self, flow: Any) -> bool:
        points = list(_value(flow, "points", []) or []) if flow is not None else []
        values = [_number(_value(item, "value")) for item in points[-4:]]
        if len(values) < 2:
            return _number(_value(flow, "final_value")) > 0 if flow is not None else False
        return values[-1] >= values[0] and sum(1 for a, b in zip(values, values[1:]) if b >= a) >= max(1, len(values) // 2)

    def _best_sector(self, quote: Any, sectors: Sequence[Any]) -> Any:
        themes = {str(item).strip() for item in (_value(quote, "themes", []) or []) if str(item).strip()}
        matches = [sector for sector in sectors if str(_value(sector, "name", "")) in themes]
        if matches:
            return max(matches, key=lambda item: _number(_value(item, "heat_score")))
        return max(sectors, key=lambda item: _number(_value(item, "heat_score")), default=None)

    def _auction_values(self, auction: Any) -> tuple[float, float, bool]:
        if auction is None or not _bool(_value(auction, "available")):
            return 0.0, 0.0, False
        change = _number(_value(auction, "change_pct"))
        imbalance = _number(_value(auction, "order_imbalance_pct"))
        good = bool(
            change >= self._number_rule("opening_auction_change_min", 0.5)
            or imbalance >= self._number_rule("opening_auction_imbalance_min", 8)
            or "上修" in str(_value(auction, "trajectory", ""))
        )
        return change, imbalance, good

    def _opening_flow_context(
        self,
        *,
        flow_score: float,
        large_imbalance: float,
        flow_available: bool,
        flow_direction: str,
        price_ok: bool,
        slope: float,
        change_pct: float,
        formula_support: bool,
        formula_exhaustion: bool,
    ) -> dict[str, Any]:
        support_min = self._number_rule("opening_flow_support_min", 12)
        pressure_max = self._number_rule("opening_flow_pressure_max", -25)
        buy_dominant = bool(
            flow_score >= support_min
            or large_imbalance >= support_min
            or "买盘增强" in flow_direction
            or "放量承接" in flow_direction
        )
        sell_dominant = bool(
            flow_score <= pressure_max
            or large_imbalance <= pressure_max
            or "抛压" in flow_direction
            or "卖盘增强" in flow_direction
        )
        response_up = bool(price_ok and (slope >= 0 or change_pct >= 0))
        response_down = bool((not price_ok) or slope <= -0.03 or change_pct <= -0.2)
        support = False
        pressure = False
        score_delta = 0
        reasons: list[str] = []
        risks: list[str] = []
        label = "无盘口"
        source = "L1" if flow_available else "分钟量价"
        if buy_dominant and response_up:
            support = True
            score_delta = 14
            label = "多头赢"
            reasons.append(f"分笔买盘先赢且价格响应确认（{flow_score:+.0f}分，{source}）")
            reasons.append("买盘推进得到价格和量能配合")
        elif sell_dominant and ("卖盘增强" in flow_direction or "抛压" in flow_direction or response_down):
            pressure = True
            score_delta = 16
            label = "空头赢"
            risks.append(f"明显抛压：卖盘持续压价且价格响应转弱（{flow_score:+.0f}分）")
        elif sell_dominant and not response_down and (formula_support or slope >= -0.02):
            support = True
            score_delta = 10
            label = "卖压被吸收"
            reasons.append(f"卖压出现但价格不再继续破位（{flow_score:+.0f}分，{source}）")
            reasons.append("承接优先于单纯方向")
        elif buy_dominant and (not response_up or formula_exhaustion):
            pressure = True
            score_delta = 12
            label = "买盘堆积但价格不跟"
            risks.append("买单增多但推进效率差，谨防高位分配")
        elif flow_score >= 6:
            support = True
            score_delta = 8
            label = "买盘偏强"
            reasons.append(f"买盘偏强（{flow_score:+.0f}分，{source}）")
        elif flow_score <= -6:
            pressure = True
            score_delta = 10
            label = "卖盘偏强"
            risks.append(f"卖盘偏强（{flow_score:+.0f}分，{source}）")
        else:
            label = flow_direction or source
        if formula_support and support and not pressure:
            reasons.append("公式位置：低位承接/短趋势转强")
        if formula_exhaustion and not support:
            risks.append("公式位置：高位钝化/兑现压力")
        return {
            "support": support,
            "pressure": pressure,
            "score_delta": score_delta,
            "reasons": reasons[:2],
            "risks": risks[:2],
            "label": label,
        }

    def _rebound(self, quote: Any) -> float:
        low = _number(_value(quote, "day_low"))
        price = _number(_value(quote, "price"))
        return _pct(price, low) if low else 0.0

    def _pullback(self, quote: Any) -> float:
        high = _number(_value(quote, "day_high"))
        price = _number(_value(quote, "price"))
        return _pct(high, price) if high else 0.0

    @staticmethod
    def _empty_sector_result() -> dict[str, Any]:
        return {"score": 0, "breadth": 0, "avg_change": 0, "gate": False, "reasons": ["未找到板块确认"]}

    def _data_quality(self, data_mode: str, quotes: Sequence[Any]) -> str:
        if str(data_mode).lower() == "replay":
            return "synthetic_replay"
        if any(_bool(_value(_value(item, "order_flow", None), "available")) for item in quotes):
            return "live_l1"
        return "live_minute_proxy"

    def _data_note(self, data_mode: str, quotes: Sequence[Any], stage: OpeningStage) -> str:
        if str(data_mode).lower() == "replay":
            return "当前为内置回放，仅用于验证链路，不代表实时行情"
        if stage.key == "auction":
            return "竞价只做候选加分；09:30后必须等待连续竞价的量价和板块确认"
        if any(_bool(_value(_value(item, "auction", None), "available")) for item in quotes):
            return "已收到竞价快照；盘口大单仍为L1/分钟代理，不是队列数据"
        return "当前无真实集合竞价数据，开盘决策仅使用连续竞价后的量价与板块状态"

    def _number_rule(self, key: str, default: float) -> float:
        return _number(self.rules.get(key), default)

    def _int(self, key: str, default: int) -> int:
        return max(1, int(_number(self.rules.get(key), default)))

    def _unavailable_payload(self, trade_date: str, clock_label: str | None, note: str) -> OpeningDecisionPayload:
        self._latest_date = trade_date
        self._latest_items = {}
        payload = OpeningDecisionPayload(
            trade_date=trade_date,
            updated_at=str(clock_label or ""),
            stage="unavailable",
            stage_label="无开盘快照",
            active=False,
            can_execute=False,
            frozen=True,
            scope="full_market",
            total=0,
            market_gate="不可用",
            methodology=self.methodology(),
            data_quality="unavailable",
            data_note=note,
            research={"checkpoints": list(CHECKPOINTS), "thresholds_are_hypotheses": True},
        )
        self._latest_payload = payload
        return payload

    def _closed_replay(self, cached: OpeningDecisionPayload, trade_date: str, clock_label: str | None) -> OpeningDecisionPayload:
        # ``_load_latest`` restores the retained item map before returning the
        # payload.  Keep that map available to the terminal stock board after
        # the session closes; falling back to the two visible queues preserves
        # compatibility with older cache records.
        items = list(self._latest_items.values())
        if not items:
            items = list(cached.top_candidates) + list(cached.top_defense)
        self._latest_date = trade_date
        self._latest_items = {item.code: item for item in items}
        payload = cached.model_copy(
            update={
                "updated_at": str(clock_label or cached.updated_at or "15:00:00"),
                "stage": "closed",
                "stage_label": "开盘窗口结束 · 可复盘",
                "checkpoint": cached.checkpoint or "09:37",
                "active": False,
                "can_execute": False,
                "frozen": True,
                "data_note": f"已加载{cached.checkpoint or '09:37'}开盘决策快照；当前仅用于复盘，不产生新动作",
            }
        )
        self._latest_payload = payload
        return payload

    def _persist(self, payload: OpeningDecisionPayload, items: Mapping[str, OpeningDecisionItem]) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            # Keep only actionable and highest-scoring observations in the
            # durable file.  The full item map remains in memory during the
            # session, while the file stays small enough for a local terminal.
            ordered = sorted(items.values(), key=lambda item: (-item.score, item.code))
            retained = [item for item in ordered if item.action != OpeningAction.WATCH][:500]
            retained.extend(item for item in ordered if item.action == OpeningAction.WATCH and item.score >= 55)
            record = {
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "trade_date": payload.trade_date,
                "checkpoint": payload.checkpoint,
                "payload": payload.model_dump(mode="json"),
                "items": [item.model_dump(mode="json") for item in retained[:700]],
            }
            records: list[dict[str, Any]] = []
            if self.persist_path.exists():
                for line in self.persist_path.read_text(encoding="utf-8").splitlines()[-self._int("opening_cache_records", 12) * 2 :]:
                    try:
                        value = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, dict):
                        records.append(value)
            records = [item for item in records if not (item.get("trade_date") == payload.trade_date and item.get("checkpoint") == payload.checkpoint)]
            records.append(record)
            records = records[-self._int("opening_cache_records", 12) :]
            self.persist_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n", encoding="utf-8")
        except Exception:
            # Persistence must never block market scanning.
            return

    def _load_latest(self, trade_date: str) -> OpeningDecisionPayload | None:
        if not self.persist_path or not trade_date or not self.persist_path.exists():
            return None
        records: list[dict[str, Any]] = []
        try:
            for line in self.persist_path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict) and str(value.get("trade_date")) == trade_date:
                    records.append(value)
            if not records:
                return None
            record = max(records, key=lambda item: str(item.get("checkpoint") or ""))
            payload_value = record.get("payload")
            if not isinstance(payload_value, Mapping):
                return None
            payload = OpeningDecisionPayload.model_validate(payload_value)
            saved_items = record.get("items") or []
            parsed_items: dict[str, OpeningDecisionItem] = {}
            for item in saved_items:
                if isinstance(item, Mapping):
                    parsed = OpeningDecisionItem.model_validate(item)
                    parsed_items[parsed.code] = parsed
            self._latest_items = parsed_items
            return payload
        except Exception:
            return None


__all__ = ["CHECKPOINTS", "OpeningStage", "OpeningStrategy"]

