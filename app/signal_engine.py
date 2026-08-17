from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime
from math import isfinite
from typing import Any, Callable, Mapping, Sequence

from app.models import (
    EventItem,
    IndexSnapshot,
    MarketState,
    PositionRecord,
    Quote,
    SectorFlowPoint,
    SectorFlowSeries,
    ReplayMarker,
    ReplayPoint,
    SectorSnapshot,
    SignalPhase,
    SignalType,
    TradeAction,
    TradeDirection,
    TradeSignal,
    TransactionFlowObservation,
    TrendState,
    WatchlistItem,
)
from app.formula_engine import (
    SIGNAL_VERSION,
    ZuoTDayContext,
    compute_zuot_series,
    latest_zuot_event,
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct(part: float, whole: float) -> float:
    if not whole:
        return 0
    return part / whole * 100


def rebound_from_low(quote: Quote) -> float:
    if quote.day_low <= 0:
        return 0
    return (quote.price - quote.day_low) / quote.day_low * 100


def pullback_from_high(quote: Quote) -> float:
    if quote.day_high <= 0:
        return 0
    return (quote.day_high - quote.price) / quote.day_high * 100


class SignalEngine:
    def __init__(self, rules: dict) -> None:
        thresholds = rules.get("thresholds", {})
        self.sector_watch_score = float(thresholds.get("sector_watch_score", 55))
        self.buy_volume_ratio_min = float(thresholds.get("buy_volume_ratio_min", 1.25))
        self.core_attack_volume_ratio = float(thresholds.get("core_attack_volume_ratio", 1.5))
        self.order_flow_attack_score = float(thresholds.get("order_flow_attack_score", 25))
        self.order_flow_pressure_score = float(thresholds.get("order_flow_pressure_score", -25))
        self.low_support_rebound_min = float(thresholds.get("low_support_rebound_min", 1.0))
        self.pressure_pullback_pct = float(thresholds.get("pressure_pullback_pct", 3.5))
        self.dashboard_formula_recent_bars = max(1, int(thresholds.get("dashboard_formula_recent_bars", 3)))
        self.sector_flow_top_n = int(thresholds.get("sector_flow_top_n", 10))
        self.sector_flow_rep_codes = int(thresholds.get("sector_flow_rep_codes", 3))
        self.sector_flow_gap_weight = float(thresholds.get("sector_flow_gap_weight", 0.15))
        self.sector_flow_momentum_weight = float(thresholds.get("sector_flow_momentum_weight", 0.55))
        self.sector_flow_vwap_weight = float(thresholds.get("sector_flow_vwap_weight", 0.30))
        self.sector_flow_gap_scale_pct = float(thresholds.get("sector_flow_gap_scale_pct", 4.0))
        self.sector_flow_momentum_scale_pct = float(thresholds.get("sector_flow_momentum_scale_pct", 0.20))
        self.sector_flow_vwap_scale_pct = float(thresholds.get("sector_flow_vwap_scale_pct", 0.50))
        self.sector_flow_scale = float(thresholds.get("sector_flow_scale", 1.0))
        self.buy_index_rebound_pct = float(thresholds.get("buy_index_rebound_pct", 0.15))
        self.buy_index_volume_ratio_min = float(thresholds.get("buy_index_volume_ratio_min", 1.08))
        self.strategy_version = SIGNAL_VERSION
        self.auction_buy_change_pct_min = float(thresholds.get("auction_buy_change_pct_min", 0.5))
        self.auction_volume_ratio_min = float(thresholds.get("auction_volume_ratio_min", 1.1))
        self.auction_market_breadth_min = float(thresholds.get("auction_market_breadth_min", 0.52))
        self.auction_order_imbalance_min = float(thresholds.get("auction_order_imbalance_min", 8))
        self.auction_proxy_slope_min_pct = float(thresholds.get("auction_proxy_slope_min_pct", 0.08))
        self.auction_proxy_imbalance_min_pct = float(
            thresholds.get("auction_proxy_imbalance_min_pct", self.auction_order_imbalance_min)
        )
        self.auction_proxy_min_snapshots = int(thresholds.get("auction_proxy_min_snapshots", 2))
        self.auction_actual_min_amount = float(thresholds.get("auction_actual_min_amount", 1_000_000))
        self.post_open_confirm_minute = int(thresholds.get("post_open_confirm_minute", 3))
        self.buy_sector_core_attack_min = int(thresholds.get("buy_sector_core_attack_min", 1))
        self.sector_new_listing_exclude_pct = float(thresholds.get("sector_new_listing_exclude_pct", 20.0))
        self.sector_extreme_change_exclude_pct = float(thresholds.get("sector_extreme_change_exclude_pct", 35.0))
        # Live snapshots arrive every few seconds, while an intraday turn is
        # a minute-level state change.  Keep a short rolling pulse so a single
        # high/low snapshot cannot be mistaken for a confirmed reversal.
        self._market_pulse: deque[dict[str, float | str]] = deque(maxlen=24)
        self._market_pulse_session = ""
        self._market_pulse_last_minute = ""
        # Doing-T lifecycle state was removed from this engine.  Trade markers
        # now come only from app.formula_engine on visible minute prefixes.

    @staticmethod
    def _has_new_listing_prefix(name: str) -> bool:
        value = str(name or "").strip().upper()
        for prefix in ("XD", "XR", "DR"):
            if value.startswith(prefix):
                value = value[len(prefix):].strip()
                break
        return bool(
            len(value) > 1
            and value[0] in {"N", "C"}
            and (not value[1].isascii() or value[1] in {"*", " "})
        )

    def is_sector_distortion_quote(self, quote: Quote) -> bool:
        change = abs(float(quote.change_pct or 0))
        return bool(
            change >= self.sector_extreme_change_exclude_pct
            or (
                self._has_new_listing_prefix(quote.name)
                and change >= self.sector_new_listing_exclude_pct
            )
        )

    def sector_metric_quotes(self, quotes: list[Quote]) -> tuple[list[Quote], list[Quote]]:
        excluded = [quote for quote in quotes if self.is_sector_distortion_quote(quote)]
        excluded_codes = {quote.code for quote in excluded}
        included = [quote for quote in quotes if quote.code not in excluded_codes]
        if included:
            return included, excluded
        return list(quotes), []

    @staticmethod
    def sector_exclusion_reason(excluded: list[Quote]) -> str:
        if not excluded:
            return ""
        names = "、".join(quote.name for quote in excluded[:3])
        suffix = "等" if len(excluded) > 3 else ""
        return f"新股扰动剔除{len(excluded)}只：{names}{suffix}"

    def build_market_state(
        self,
        indices: list[IndexSnapshot],
        quotes: list[Quote],
        clock_label: str | None = None,
        frozen: bool = False,
    ) -> MarketState:
        now = clock_label or datetime.now().strftime("%H:%M:%S")
        closed = bool(frozen or self._is_closed_clock(now))
        up_count = sum(1 for quote in quotes if quote.change_pct > 0)
        down_count = sum(1 for quote in quotes if quote.change_pct < 0)
        flat_count = max(0, len(quotes) - up_count - down_count)
        limit_up_count = sum(1 for quote in quotes if quote.limit_up)
        limit_down_count = sum(1 for quote in quotes if quote.limit_down)
        opened_limit_count = sum(1 for quote in quotes if quote.opened_limit)
        total_amount = sum(max(float(quote.amount or 0), 0) for quote in quotes)
        breadth_pct = pct(up_count, len(quotes))
        pulse = self._observe_market_pulse(indices, now, closed)
        amount_expanding = bool(pulse["amount_expanding"])
        index_turning = bool(pulse["turning"])
        attack_count = sum(1 for quote in quotes if quote.change_pct >= 1.5 and quote.minute_amount_ratio >= 1.5)
        auction_quotes = [quote for quote in quotes if quote.auction.available]
        auction_positive_count = sum(1 for quote in auction_quotes if quote.auction.change_pct > 0)
        auction_negative_count = sum(1 for quote in auction_quotes if quote.auction.change_pct < 0)
        auction_avg_change_pct = (
            sum(quote.auction.change_pct for quote in auction_quotes) / len(auction_quotes)
            if auction_quotes
            else 0
        )
        auction_amount = sum(max(float(quote.auction.amount or 0), 0) for quote in auction_quotes)
        auction_quality = self._auction_quality(auction_quotes)
        auction_ready = bool(
            auction_quotes
            and auction_positive_count / len(auction_quotes) >= self.auction_market_breadth_min
            and any(self._auction_support(quote) for quote in auction_quotes)
        )

        score = int(
            clamp(
                breadth_pct * 0.35
                + (18 if amount_expanding else 0)
                + (18 if index_turning else 0)
                + clamp(attack_count * 4, 0, 24),
                0,
                100,
            )
        )
        if index_turning and amount_expanding and score >= 58:
            trend = TrendState.TURNING_UP
        elif score >= 62:
            trend = TrendState.STRONG
        elif score <= 35:
            trend = TrendState.WEAK
        else:
            trend = TrendState.MIXED

        reasons: list[str] = []
        if index_turning:
            if closed:
                reasons.append("收盘快照显示指数日内曾从低位拐头（历史代理，不生成新买点）")
            elif str(pulse["mode"]).startswith("rolling"):
                reasons.append(
                    f"指数先弱后强拐头（近{int(pulse['points'])}个分钟状态，斜率{float(pulse['slope']):+.3f}%）"
                )
            else:
                reasons.append("指数从日内低位拐头（单快照代理，等待下一分钟确认）")
        if amount_expanding:
            reasons.append("短周期成交额放大")
        if attack_count:
            reasons.append(f"{attack_count}只跟踪股出现量能进攻")
        if auction_quotes:
            reasons.append(
                f"{'历史' if closed else ''}竞价{auction_positive_count}/{len(auction_quotes)}偏强，均值{auction_avg_change_pct:+.2f}%"
            )
            if auction_quality == "proxy":
                reasons.append("竞价字段为 TDX L1 指示价/五档代理")
            elif auction_quality == "actual":
                reasons.append("收到显式竞价字段")
        if not reasons:
            reasons.append("市场仍在等待方向确认")

        return MarketState(
            trend=trend,
            emotion_score=score,
            breadth_pct=round(breadth_pct, 1),
            index_turning=index_turning,
            amount_expanding=amount_expanding,
            mainline="等待板块确认",
            indices=indices,
            reasons=reasons,
            updated_at=now,
            up_count=up_count,
            down_count=down_count,
            flat_count=flat_count,
            limit_up_count=limit_up_count,
            limit_down_count=limit_down_count,
            opened_limit_count=opened_limit_count,
            total_amount=round(total_amount, 2),
            auction_available_count=len(auction_quotes),
            auction_positive_count=auction_positive_count,
            auction_negative_count=auction_negative_count,
            auction_avg_change_pct=round(auction_avg_change_pct, 2),
            auction_amount=round(auction_amount, 2),
            auction_ready=auction_ready,
            auction_status=(
                "历史竞价回填，仅供复盘"
                if closed and auction_quotes
                else "收盘复盘，无竞价数据"
                if closed
                else "竞价偏强，可生成开盘候选"
                if auction_ready
                else "竞价分歧，仅观察"
                if auction_quotes
                else "暂无真实竞价数据"
            ),
            auction_data_quality=auction_quality,
            auction_snapshot_count=max(
                (quote.auction.snapshot_count for quote in auction_quotes),
                default=0,
            ),
            order_book_available_count=sum(1 for quote in quotes if quote.order_flow.available),
            level2_available_count=sum(1 for quote in quotes if quote.order_flow.level2_available),
            decision_stage=self._decision_stage(now, frozen=closed),
            index_turning_mode=str(pulse["mode"]),
            index_slope_pct=round(float(pulse["slope"]), 4),
            index_prior_slope_pct=round(float(pulse["prior_slope"]), 4),
            market_pulse_points=int(pulse["points"]),
        )

    def _observe_market_pulse(
        self,
        indices: list[IndexSnapshot],
        clock_label: str,
        closed: bool,
    ) -> dict[str, float | str]:
        """Return a minute-level index turn state from real quote snapshots.

        The fallback for the first observation is intentionally labelled as a
        snapshot proxy. Once two or more minute observations exist, a turn
        requires a prior non-positive slope followed by a positive slope (or a
        clear rebound from the observed local low) and current volume
        expansion. This is a state aid for the strategy, not a prediction.
        """
        fallback_rebound = max(
            (float(index.rebound_from_low_pct or 0) for index in indices),
            default=0.0,
        )
        current_ratio = max(
            (float(index.minute_amount_ratio or 0) for index in indices),
            default=1.0,
        )
        fallback = {
            "turning": fallback_rebound >= self.buy_index_rebound_pct,
            "amount_expanding": current_ratio >= self.buy_index_volume_ratio_min,
            "slope": 0.0,
            "prior_slope": 0.0,
            "points": 0.0,
            "mode": "snapshot_rebound_proxy",
        }
        if closed or not indices:
            return fallback

        minute = str(clock_label or "")[:5]
        parsed = self._clock_minutes(clock_label)
        if parsed is None or not minute:
            return fallback
        session = datetime.now().strftime("%Y%m%d")
        if (
            self._market_pulse_session != session
            or (
                self._market_pulse_last_minute
                and minute < self._market_pulse_last_minute
                and self._clock_minutes(self._market_pulse_last_minute)
                and self._clock_minutes(self._market_pulse_last_minute)[0] * 60
                + self._clock_minutes(self._market_pulse_last_minute)[1]
                - (parsed[0] * 60 + parsed[1])
                > 30
            )
        ):
            self._market_pulse.clear()
            self._market_pulse_session = session
        normalized_price = sum(
            (index.price / index.prev_close)
            for index in indices
            if index.price > 0 and index.prev_close > 0
        )
        valid_count = sum(1 for index in indices if index.price > 0 and index.prev_close > 0)
        if not valid_count:
            return fallback
        normalized_price /= valid_count
        point = {
            "minute": minute,
            "price": normalized_price,
            "ratio": current_ratio,
            "rebound": fallback_rebound,
        }
        if self._market_pulse and str(self._market_pulse[-1].get("minute")) == minute:
            self._market_pulse[-1] = point
        else:
            self._market_pulse.append(point)
        self._market_pulse_last_minute = minute

        points = len(self._market_pulse)
        if points < 2:
            return {
                **fallback,
                "points": float(points),
                "mode": "snapshot_rebound_proxy",
            }
        previous = self._market_pulse[-2]
        current = self._market_pulse[-1]
        prior = self._market_pulse[-3] if points >= 3 else None
        previous_price = float(previous.get("price") or 0)
        current_price = float(current.get("price") or 0)
        prior_price = float(prior.get("price") or 0) if prior else previous_price
        slope = (current_price - previous_price) / previous_price * 100 if previous_price else 0
        prior_slope = (previous_price - prior_price) / prior_price * 100 if prior_price else 0
        local_rebound = float(current.get("rebound") or 0)
        had_weakness = prior_slope <= 0 or previous_price <= prior_price
        turning = bool(
            slope >= 0.01
            and had_weakness
            and (
                local_rebound >= self.buy_index_rebound_pct
                or slope >= self.buy_index_rebound_pct * 0.12
            )
        )
        return {
            "turning": turning,
            "amount_expanding": current_ratio >= self.buy_index_volume_ratio_min,
            "slope": slope,
            "prior_slope": prior_slope,
            "points": float(points),
            "mode": "rolling_turn" if turning else "rolling_no_turn",
        }

    def rank_sectors(
        self,
        quotes: list[Quote],
        theme_defs: list[dict],
        market: MarketState,
    ) -> list[SectorSnapshot]:
        by_code = {quote.code: quote for quote in quotes}
        sector_members: dict[str, set[str]] = defaultdict(set)
        sector_core_codes: dict[str, list[str]] = defaultdict(list)
        sectors: list[SectorSnapshot] = []

        for quote in quotes:
            for theme in quote.themes:
                name = str(theme).strip()
                if name:
                    sector_members[name].add(quote.code)

        for theme in theme_defs:
            name = str(theme.get("name", "")).strip()
            if not name:
                continue
            members = {str(code).zfill(6) for code in theme.get("members", [])}
            core_codes = [str(code).zfill(6) for code in theme.get("core_codes", [])]
            sector_members[name].update(members)
            sector_members[name].update(core_codes)
            sector_core_codes[name].extend(core_codes)

        for name, members in sector_members.items():
            sector_quotes = [by_code[code] for code in members if code in by_code]
            if not sector_quotes:
                continue

            metric_quotes, excluded_quotes = self.sector_metric_quotes(sector_quotes)
            metric_codes = {quote.code for quote in metric_quotes}
            core_codes = list(dict.fromkeys([code for code in sector_core_codes.get(name, []) if code in metric_codes]))
            if not core_codes:
                core_codes = [quote.code for quote in metric_quotes if quote.core]
            metric_total = len(metric_quotes)
            up_count = sum(1 for quote in metric_quotes if quote.change_pct > 0)
            down_count = sum(1 for quote in metric_quotes if quote.change_pct < 0)
            limit_up_count = sum(1 for quote in metric_quotes if quote.limit_up)
            opened_limit_count = sum(1 for quote in metric_quotes if quote.opened_limit)
            auction_available_count = sum(1 for quote in metric_quotes if quote.auction.available)
            auction_positive_count = sum(1 for quote in metric_quotes if quote.auction.available and quote.auction.change_pct > 0)
            avg_change_pct = sum(quote.change_pct for quote in metric_quotes) / metric_total
            flow_delta = sum(
                max(float(quote.amount or 0), 0)
                * (1 if quote.change_pct > 0 else -1 if quote.change_pct < 0 else 0)
                for quote in metric_quotes
            ) / 100_000_000
            leader = max(metric_quotes, key=lambda quote: (quote.change_pct, quote.minute_amount_ratio))
            # 引擎不读市值库：中军用成交额第一作代理（services 层有 daily_basic 时用流通市值）
            capacity_leader = max(metric_quotes, key=lambda quote: float(quote.amount or 0))
            core_attack = any(
                by_code[code].change_pct >= 1.5
                and by_code[code].minute_amount_ratio >= self.core_attack_volume_ratio
                and (
                    not by_code[code].order_flow.available
                    or by_code[code].order_flow.score >= self.order_flow_attack_score
                )
                for code in core_codes
            )
            auction_confirmed = bool(
                auction_available_count
                and auction_positive_count / auction_available_count >= self.auction_market_breadth_min
                and avg_change_pct >= 0
                and any(self._auction_support(by_code[code]) for code in metric_codes if code in by_code)
            )

            heat_score = int(
                clamp(
                    18
                    + pct(up_count, metric_total) * 0.28
                    + clamp((avg_change_pct + 1.5) * 5.2, 0, 28)
                    + clamp(limit_up_count * 8 + opened_limit_count * 4, 0, 20)
                    + (18 if core_attack else 0)
                    + (8 if market.index_turning else 0),
                    0,
                    100,
                )
            )

            reasons = [f"{up_count}/{metric_total}上涨", f"均涨{avg_change_pct:+.2f}%"]
            exclusion_reason = self.sector_exclusion_reason(excluded_quotes)
            if exclusion_reason:
                reasons.insert(0, exclusion_reason)
            if core_attack:
                strongest_core = max((by_code[code] for code in core_codes), key=lambda quote: quote.minute_amount_ratio)
                reasons.append(f"核心票{strongest_core.name}量能{strongest_core.minute_amount_ratio:.1f}倍")
            if limit_up_count:
                reasons.append(f"{limit_up_count}只涨停确认")
            if opened_limit_count:
                reasons.append(f"{opened_limit_count}只炸板/开板承接")
            if market.index_turning:
                reasons.append("指数拐头共振")
            if auction_confirmed:
                strongest_auction = max(
                    (by_code[code] for code in metric_codes if code in by_code and by_code[code].auction.available),
                    key=lambda quote: (quote.auction.price_slope_pct, quote.auction.order_imbalance_pct),
                    default=None,
                )
                detail = f"竞价{auction_positive_count}/{auction_available_count}偏强"
                if strongest_auction:
                    detail += f" · {strongest_auction.auction.trajectory}"
                reasons.append(detail)

            sectors.append(
                SectorSnapshot(
                    name=name,
                    heat_score=heat_score,
                    avg_change_pct=round(avg_change_pct, 2),
                    up_count=up_count,
                    down_count=down_count,
                    total_count=metric_total,
                    limit_up_count=limit_up_count,
                    opened_limit_count=opened_limit_count,
                    core_attack=core_attack,
                    core_codes=core_codes,
                    leader_code=leader.code,
                    leader_name=leader.name,
                    capacity_leader_code=capacity_leader.code,
                    capacity_leader_name=capacity_leader.name,
                    reasons=reasons,
                    flow_delta=round(flow_delta, 2),
                    raw_total_count=len(sector_quotes),
                    new_listing_excluded_count=len(excluded_quotes),
                    auction_positive_count=auction_positive_count,
                    auction_available_count=auction_available_count,
                    auction_confirmed=auction_confirmed,
                )
            )

        sectors.sort(key=lambda sector: sector.heat_score, reverse=True)
        for rank, sector in enumerate(sectors, start=1):
            sector.rank_change = 0
        if sectors:
            market.mainline = sectors[0].name if sectors[0].heat_score >= self.sector_watch_score else "等待板块确认"
        return sectors

    def build_signals(
        self,
        quotes: list[Quote],
        watchlist: list[WatchlistItem],
        sectors: list[SectorSnapshot],
        market: MarketState,
        clock_label: str | None = None,
        preferred_sector_names: set[str] | None = None,
        positions: dict[str, PositionRecord] | None = None,
        formula_rows_by_code: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        sector_name_mapper: Callable[[Quote, SectorSnapshot], str] | None = None,
    ) -> list[TradeSignal]:
        """Return board signals from cached formula rows when available.

        Dashboard refresh must not fan out upstream minute-series or L1
        transaction reads.  It can consume already persisted stock feature
        rows and promote only recent formula primitives into BUY_T/SELL_T.
        """
        by_code = {quote.code: quote for quote in quotes}
        sector_by_name = {sector.name: sector for sector in sectors}
        position_by_code = positions or {}
        formula_rows_by_code = formula_rows_by_code or {}
        now = clock_label or (
            str(quotes[0].updated_at)
            if quotes and str(quotes[0].updated_at or "")
            else datetime.now().strftime("%H:%M:%S")
        )
        preopen = self._is_preopen_clock(now)
        signals: list[TradeSignal] = []

        for index, item in enumerate(watchlist):
            # 全市场扫描（数千票）是秒级 CPU 循环；周期性出让 GIL，
            # 避免后台刷新把详情/写自选等前台请求饿死。
            if index and index % 512 == 0:
                time.sleep(0)
            quote = by_code.get(item.code)
            if not quote:
                continue
            sector = self._best_sector_for_quote(
                quote,
                sector_by_name,
                preferred_sector_names=preferred_sector_names,
            )
            low_rebound = rebound_from_low(quote)
            pullback = pullback_from_high(quote)
            flow_score = int(quote.order_flow.score or 0) if quote.order_flow.available else 0
            sector_score = sector.heat_score if sector else 0
            score = int(
                clamp(
                    sector_score * 0.50
                    + min(max(quote.minute_amount_ratio, 0), 8) * 7
                    + min(max(quote.change_pct, -10), 10) * 1.8
                    + (10 if quote.core else 0)
                    + (8 if quote.limit_up or quote.opened_limit else 0)
                    + clamp(flow_score * 0.12, -8, 8),
                    5,
                    95,
                )
            )
            reasons: list[str] = []
            risks: list[str] = []
            factor_flags: list[str] = ["公式唯一来源"]
            if sector:
                # 板块名统一走展示名映射：内部行业代码（X250602 等）映射成官方板块名
                display_sector_name = (
                    sector_name_mapper(quote, sector) if sector_name_mapper else sector.name
                )
                reasons.append(f"{display_sector_name}热度{sector.heat_score}分")
                if sector.core_attack:
                    reasons.append("板块核心进攻")
                    factor_flags.append("板块进攻")
            if market.index_turning:
                reasons.append("指数拐头观察")
                factor_flags.append("指数拐头")
            if market.amount_expanding:
                reasons.append("市场量能放大")
            if quote.minute_amount_ratio >= self.buy_volume_ratio_min:
                reasons.append(f"分钟量能{quote.minute_amount_ratio:.1f}倍")
                factor_flags.append("分时放量")
            if quote.order_flow.available:
                if flow_score >= self.order_flow_attack_score:
                    reasons.append("L1五档/成交代理买盘偏强")
                    factor_flags.append("L1承接")
                elif flow_score <= self.order_flow_pressure_score:
                    risks.append("L1五档/成交代理抛压偏强")
                    factor_flags.append("L1抛压")
            if preopen and self._auction_support(quote):
                reasons.append("竞价先验偏强，仅用于排序观察")
                factor_flags.append("竞价先验")
            position = position_by_code.get(quote.code)
            t_plus_one_restricted = bool(position and position.quantity > 0 and position.available_quantity <= 0)
            if t_plus_one_restricted:
                risks.append("持仓可卖数量为0，卖T受T+1限制")
            risks.append("首页只消费本地分钟特征缓存；不逐行读取上游分钟线或L1逐笔")
            if not reasons:
                reasons.append("等待详情分钟公式确认")

            signal_type = SignalType.WATCH
            signal_phase = SignalPhase.OBSERVE.value
            signal_action = TradeAction.OBSERVE.value
            signal_direction = TradeDirection.NONE.value
            signal_setup = "formula_cached_watch"
            signal_grade = "观察"
            signal_source = "tdx_formula_cached_features"
            source_quality = self._signal_source(quote)
            executable = False
            execution_reason = "首页排序观察；公式候选来自本地分钟特征缓存"
            evidence_sequence = ["首页观察排序", "本地分钟特征缓存"]
            invalidation_price = 0.0
            factor_scores: dict[str, float] = {
                "sector_heat": float(sector_score),
                "minute_amount_ratio": round(float(quote.minute_amount_ratio or 0), 2),
                "l1_snapshot_score": float(flow_score),
                "low_rebound_pct": round(float(low_rebound), 2),
                "pullback_pct": round(float(pullback), 2),
            }
            formula_event = self._latest_cached_zuot_event(
                formula_rows_by_code.get(quote.code) or formula_rows_by_code.get(str(quote.code).zfill(6)) or [],
                quote,
            )
            if formula_event is not None:
                event_signal = formula_event["signal"]
                event_reasons = list(formula_event["reasons"])
                signal_type = event_signal
                signal_phase = (
                    SignalPhase.CONFIRM.value
                    if event_signal == SignalType.BUY_T
                    else SignalPhase.SELL_CONFIRM.value
                )
                signal_action = (
                    TradeAction.BUY_T.value
                    if event_signal == SignalType.BUY_T
                    else TradeAction.SELL_BASE.value
                )
                signal_direction = (
                    TradeDirection.POSITIVE_T.value
                    if event_signal == SignalType.BUY_T
                    else TradeDirection.REVERSE_T.value
                )
                signal_setup = "zuot_support_rebound" if event_signal == SignalType.BUY_T else "zuot_resistance_fade"
                signal_grade = "公式买T" if event_signal == SignalType.BUY_T else "公式卖T"
                signal_source = "zuot_tdx_cached_stock_features"
                source_quality = "trajectory_stock_features+zuot_tdx_levels"
                score = max(score, 78 if event_signal == SignalType.BUY_T else 74)
                reasons = list(dict.fromkeys([*event_reasons, *reasons[:3]]))
                risks = [risk for risk in risks if "等待详情分钟公式确认" not in risk]
                risks.append("首页未读取L1逐笔；L1抛压只在详情中确认")
                if event_signal == SignalType.SELL_T:
                    if t_plus_one_restricted:
                        risks.append("A股T+1限制：当前持仓可卖数量为0")
                    elif not position:
                        risks.append("未录入本地持仓，绿色卖T仅作风险提示")
                factor_flags = list(
                    dict.fromkeys(
                        [
                            "公式买入原语" if event_signal == SignalType.BUY_T else "公式卖出原语",
                            *factor_flags,
                        ]
                    )
                )
                factor_flags = [item for item in factor_flags if item]
                executable = False
                execution_reason = "本地分钟特征缓存出现做T公式信号；执行前打开详情确认L1成交流"
                evidence_sequence = list(dict.fromkeys(event_reasons))
                invalidation_price = float(formula_event.get("invalidation_price") or 0)
                factor_scores.update(
                    {
                        "总评分": float(formula_event.get("score") or 0),
                        "资金流向_万": float(formula_event.get("fund_flow") or 0),
                        "阻力": float(formula_event.get("resistance") or 0),
                        "支撑": float(formula_event.get("support") or 0),
                    }
                )

            signals.append(
                TradeSignal(
                    code=quote.code,
                    name=quote.name,
                    signal=signal_type,
                    score=score,
                    sector=sector.name if sector else "未归类",
                    price=quote.price,
                    change_pct=quote.change_pct,
                    rebound_from_low_pct=round(low_rebound, 2),
                    minute_amount_ratio=round(quote.minute_amount_ratio, 2),
                    reasons=list(dict.fromkeys(reasons)),
                    risks=list(dict.fromkeys(risks)),
                    updated_at=str(formula_event.get("time") or now) if formula_event else now,
                    factor_flags=list(dict.fromkeys(factor_flags)),
                    signal_source=signal_source,
                    auction=quote.auction,
                    order_flow=quote.order_flow,
                    decision_stage=self._decision_stage(now, frozen=bool(market.frozen)),
                    direction=signal_direction,
                    action=signal_action,
                    setup=signal_setup,
                    regime=str(getattr(market.trend, "value", market.trend)),
                    executable=executable,
                    execution_reason=execution_reason,
                    evidence_sequence=evidence_sequence,
                    validation_status="research_only",
                    hypothesis_id="tdx_formula_runtime_v1",
                    strategy_version=self.strategy_version,
                    signal_grade=signal_grade,
                    confluence_window_bars=0,
                    phase=signal_phase,
                    invalidation_price=round(invalidation_price, 2),
                    trigger_price=round(float(formula_event.get("price") or 0), 3) if formula_event else 0.0,
                    source_quality=source_quality,
                    factor_scores=factor_scores,
                    exit_score=0,
                    t_plus_one_restricted=t_plus_one_restricted,
                    action_size_pct=0,
                )
            )

        signals.sort(key=lambda signal: (not signal.pinned, -signal.score, signal.code))
        return signals

    def _latest_cached_zuot_event(
        self,
        rows: Sequence[Mapping[str, Any]],
        quote: Quote,
        recent_bars: int | None = None,
    ) -> dict[str, Any] | None:
        """从本地分钟特征缓存检测最新做T买/卖信号（新做T公式，支撑/阻力穿越）。

        recent_bars 缺省用首页提升窗口（最近几根分钟才算数）；传大窗口（如全天）
        可拿「当天最近一次买卖点」，供榜单距买卖点 ±% 展示。
        """
        formula_rows = self._cached_formula_rows(rows, quote)
        if len(formula_rows) < 2:
            return None
        day = ZuoTDayContext(
            prev_close=float(quote.prev_close or 0),
            day_high=float(quote.day_high or 0),
            day_low=float(quote.day_low or 0),
            change_pct=float(quote.change_pct or 0),
            volume_ratio=float(quote.minute_amount_ratio or 1) or 1.0,
            total_amount=float(quote.amount or 0),
            outer_volume=float(quote.order_flow.active_buy_volume or 0),
            inner_volume=float(quote.order_flow.active_sell_volume or 0),
        )
        try:
            result = compute_zuot_series(formula_rows, day)
        except Exception:
            return None
        event = latest_zuot_event(
            result.states,
            recent_bars=recent_bars if recent_bars is not None else self.dashboard_formula_recent_bars,
        )
        if event is None:
            return None
        latest = result.latest
        return {
            "index": event["index"],
            "signal": SignalType.BUY_T if event["signal"] == "buy" else SignalType.SELL_T,
            "time": event["time"],
            "price": float(event.get("price") or 0),
            "reasons": list(event["reasons"]),
            "invalidation_price": event.get("invalidation_price") or 0,
            "score": latest.get("score") or 0,
            "fund_flow": latest.get("fund_flow") or 0,
            "resistance": latest.get("resistance") or 0,
            "support": latest.get("support") or 0,
        }

    def _cached_formula_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        quote: Quote,
    ) -> list[dict[str, Any]]:
        by_minute: dict[str, Mapping[str, Any]] = {}
        order: list[str] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            time_label = self._formula_time_label(row, index)
            if not time_label:
                continue
            if time_label not in by_minute:
                order.append(time_label)
            by_minute[time_label] = row
        output: list[dict[str, Any]] = []
        previous_price = float(quote.prev_close or quote.open or quote.price or 0)
        for time_label in order:
            row = by_minute[time_label]
            price = self._row_number(row, "close", "price", "last", default=previous_price)
            if price <= 0:
                continue
            open_price = previous_price if previous_price > 0 else price
            output.append(
                {
                    "time": time_label,
                    "open": open_price,
                    "high": max(open_price, price),
                    "low": min(open_price, price),
                    "close": price,
                    "price": price,
                    "amount": self._row_number(row, "minute_amount", "amount", default=0.0),
                    "vol": self._row_number(row, "vol", "volume", default=0.0),
                }
            )
            previous_price = price
        return output

    @staticmethod
    def _row_number(row: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                return number if isfinite(number) else default
        return default

    @staticmethod
    def _formula_time_label(row: Mapping[str, Any], index: int) -> str:
        raw = str(row.get("time") or row.get("captured_at") or row.get("updated_at") or "").strip()
        if "T" in raw:
            raw = raw.rsplit("T", 1)[-1]
        if " " in raw:
            raw = raw.rsplit(" ", 1)[-1]
        if ":" in raw:
            parts = raw.split(":")
            try:
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
            except Exception:
                return raw[:5]
        fallback_hour = 9
        fallback_minute = 31 + index
        return f"{fallback_hour + fallback_minute // 60:02d}:{fallback_minute % 60:02d}"

    def _auction_support(self, quote: Quote) -> bool:
        auction = quote.auction
        if not auction.available or auction.data_quality not in {"actual", "proxy"}:
            return False
        if auction.change_pct < self.auction_buy_change_pct_min:
            return False
        if auction.data_quality == "actual":
            return bool(
                auction.volume_ratio >= self.auction_volume_ratio_min
                or auction.amount >= self.auction_actual_min_amount
                or auction.order_imbalance_pct >= self.auction_order_imbalance_min
            )
        # A proxy needs either a meaningful five-level imbalance or a
        # multi-snapshot upward revision.  Merely having a positive amount is
        # not enough because TDX L1 current volume is not guaranteed to be matched
        # auction volume.
        return bool(
            auction.order_imbalance_pct >= self.auction_proxy_imbalance_min_pct
            or (
                auction.snapshot_count >= self.auction_proxy_min_snapshots
                and auction.price_slope_pct >= self.auction_proxy_slope_min_pct
            )
        )

    @staticmethod
    def _auction_quality(quotes: list[Quote]) -> str:
        qualities = {quote.auction.data_quality for quote in quotes if quote.auction.available}
        if "actual" in qualities:
            return "actual"
        if "proxy" in qualities:
            return "proxy"
        return "unavailable"

    def _decision_stage(self, clock_label: str, frozen: bool = False) -> str:
        if frozen:
            return "收盘复盘"
        parsed = self._clock_minutes(clock_label)
        if parsed is None:
            return "观察"
        hour, minute = parsed
        current = hour * 60 + minute
        if 9 * 60 + 15 <= current < 9 * 60 + 30:
            return "竞价预案"
        if 9 * 60 + 30 <= current < 9 * 60 + 30 + max(1, self.post_open_confirm_minute):
            return "开盘观察"
        if current >= 15 * 60:
            return "收盘复盘"
        return "盘中共振"

    @classmethod
    def _is_closed_clock(cls, clock_label: str) -> bool:
        parsed = cls._clock_minutes(clock_label)
        if parsed is None:
            return False
        hour, minute = parsed
        return hour * 60 + minute >= 15 * 60

    @staticmethod
    def _signal_source(quote: Quote) -> str:
        if quote.order_flow.level2_available:
            return "easy_tdx_l1_expanded"
        if quote.order_flow.data_quality == "l1_five_level_transaction":
            return "easy_tdx_l1_five_level_transaction"
        if quote.order_flow.data_quality == "l1_transaction":
            return "easy_tdx_l1_transaction"
        if quote.order_flow.available:
            return "easy_tdx_l1_five_level"
        if quote.auction.data_quality == "actual":
            return "auction_snapshot"
        if quote.auction.data_quality == "proxy":
            return "easy_tdx_preopen_proxy"
        return "close_snapshot_proxy"

    @staticmethod
    def _clock_minutes(clock_label: str) -> tuple[int, int] | None:
        try:
            parts = str(clock_label).split(":")
            return int(parts[0]), int(parts[1])
        except (TypeError, ValueError, IndexError):
            return None

    @staticmethod
    def _is_preopen_clock(clock_label: str) -> bool:
        parsed = SignalEngine._clock_minutes(clock_label)
        if parsed is None:
            return False
        hour, minute = parsed
        current = hour * 60 + minute
        return 9 * 60 + 15 <= current < 9 * 60 + 30

    def build_events(
        self,
        market: MarketState,
        sectors: list[SectorSnapshot],
        signals: list[TradeSignal],
        clock_label: str | None = None,
    ) -> list[EventItem]:
        now = clock_label or datetime.now().strftime("%H:%M:%S")
        events: list[EventItem] = []
        if market.index_turning:
            events.append(EventItem(time=now, level="market", title="指数拐头", detail=" / ".join(market.reasons)))
        if sectors and sectors[0].heat_score >= self.sector_watch_score:
            top = sectors[0]
            events.append(EventItem(time=now, level="sector", title=f"{top.name}板块点火", detail=" / ".join(top.reasons[:3])))
        for sector in sectors[:4]:
            if sector.core_attack:
                events.append(
                    EventItem(
                        time=now,
                        level="core",
                        title=f"{sector.name}核心票进攻",
                        detail=" / ".join(sector.reasons),
                    )
                )
        for signal in signals[:6]:
            if signal.signal != SignalType.WATCH:
                events.append(
                    EventItem(
                        time=signal.updated_at,
                        level="signal",
                        title=f"{signal.name} {signal.signal.value}",
                        detail=" / ".join(signal.reasons[:3]),
                    )
                )
        return events[:12]

    def build_sector_flow(
        self,
        sectors: list[SectorSnapshot],
        quotes: list[Quote],
        minute_series_map: dict[str, list[dict]],
        sector_member_codes: dict[str, list[str] | set[str]] | None = None,
        limit: int | None = None,
    ) -> list[SectorFlowSeries]:
        by_code = {quote.code: quote for quote in quotes}
        if not sectors or not minute_series_map:
            return []

        max_len = max((len(rows) for rows in minute_series_map.values()), default=0)
        if max_len <= 0:
            return []
        times = self._session_times(max_len)

        flow_series: list[SectorFlowSeries] = []
        for sector in sectors:
            member_codes = sector_member_codes.get(sector.name) if sector_member_codes is not None else None
            candidate_codes = self._sector_flow_codes(sector, by_code, member_codes=member_codes)
            sector_quotes = [by_code[code] for code in candidate_codes if code in by_code]
            if not sector_quotes:
                continue
            points = self._sector_flow_points(sector, sector_quotes, minute_series_map, times)
            if not points:
                continue
            flow_series.append(
                SectorFlowSeries(
                    name=sector.name,
                    heat_score=sector.heat_score,
                    final_value=round(points[-1].value, 2),
                    change_pct=round(sector.avg_change_pct, 2),
                    leader_code=sector.leader_code,
                    leader_name=sector.leader_name,
                    core_codes=list(sector.core_codes[:6]),
                    reasons=list(dict.fromkeys(sector.reasons[:4])),
                    points=points,
                    sample_codes=candidate_codes,
                ),
            )

        flow_series.sort(key=lambda series: (series.final_value, series.heat_score), reverse=True)
        return flow_series[: max(1, int(limit or self.sector_flow_top_n))]

    def sector_flow_codes(
        self,
        sector: SectorSnapshot,
        quotes: list[Quote],
        member_codes: list[str] | set[str] | None = None,
    ) -> list[str]:
        return self._sector_flow_codes(sector, {quote.code: quote for quote in quotes}, member_codes=member_codes)

    def _normalize_replay_prices(
        self,
        raw_prices: list[float],
        prev_close: float,
        day_low: float = 0,
        day_high: float = 0,
    ) -> list[float]:
        fallback = prev_close if prev_close > 0 else 1.0
        prices = [price if isfinite(price) and price > 0 else fallback for price in raw_prices]
        if not prices:
            return []

        if prev_close > 0:
            usable_prices = sorted(price for price in prices if price > 0)
            median_price = usable_prices[len(usable_prices) // 2] if usable_prices else fallback
            if median_price > prev_close * 20:
                prices = [price / 100 for price in prices]

        cleaned: list[float] = []
        last_valid = fallback
        lower_candidates = [prev_close * 0.65] if prev_close > 0 else []
        upper_candidates = [prev_close * 1.6] if prev_close > 0 else []
        if day_low > 0:
            lower_candidates.append(day_low * 0.98)
        if day_high > 0:
            upper_candidates.append(day_high * 1.02)
        lower_bound = max(lower_candidates) if lower_candidates else 0
        upper_bound = min(upper_candidates) if upper_candidates else float("inf")
        if upper_bound <= lower_bound and prev_close > 0:
            lower_bound = prev_close * 0.65
            upper_bound = prev_close * 1.6
        for price in prices:
            if not isfinite(price) or price <= 0:
                cleaned.append(round(last_valid, 4))
                continue
            if prev_close > 0 and (price < lower_bound or price > upper_bound):
                cleaned.append(round(last_valid, 4))
                continue
            last_valid = price
            cleaned.append(round(price, 4))
        return cleaned

    def _session_times(self, count: int) -> list[str]:
        times: list[str] = []
        hour, minute = 9, 31
        while hour < 11 or (hour == 11 and minute <= 30):
            times.append(f"{hour:02d}:{minute:02d}")
            minute += 1
            if minute >= 60:
                hour += 1
                minute = 0
        hour, minute = 13, 1
        while hour < 15 or (hour == 15 and minute <= 0):
            times.append(f"{hour:02d}:{minute:02d}")
            minute += 1
            if minute >= 60:
                hour += 1
                minute = 0
            if hour > 15:
                break
        return times[:count]

    def _sector_flow_codes(
        self,
        sector: SectorSnapshot,
        by_code: dict[str, Quote],
        member_codes: list[str] | set[str] | None = None,
    ) -> list[str]:
        member_set = (
            {str(code or "").strip().zfill(6) for code in member_codes if str(code or "").strip()}
            if member_codes is not None
            else None
        )
        candidates: list[str] = []
        for code in sector.core_codes:
            if code in by_code and code not in candidates and (member_set is None or code in member_set):
                candidates.append(code)

        if (
            sector.leader_code in by_code
            and sector.leader_code not in candidates
            and (member_set is None or sector.leader_code in member_set)
        ):
            candidates.append(sector.leader_code)

        if len(candidates) < self.sector_flow_rep_codes:
            if member_set is None:
                pool = (
                    quote
                    for quote in by_code.values()
                    if sector.name in quote.themes and quote.code not in candidates
                )
            else:
                pool = (
                    by_code[code]
                    for code in member_set
                    if code in by_code and code not in candidates
                )
            pool_list = list(pool)
            filtered_pool = [quote for quote in pool_list if not self.is_sector_distortion_quote(quote)]
            ranked_pool = filtered_pool or pool_list
            ranked = sorted(ranked_pool, key=lambda quote: self._sector_flow_candidate_score(sector, quote), reverse=True)
            candidates.extend(quote.code for quote in ranked[: self.sector_flow_rep_codes - len(candidates)])

        return candidates[: max(1, self.sector_flow_rep_codes)]

    def _sector_flow_candidate_score(self, sector: SectorSnapshot, quote: Quote) -> float:
        amount_yi = min(max(quote.amount, 0) / 100_000_000, 80)
        return (
            amount_yi * 1.4
            + max(quote.change_pct, -6)
            + min(max(quote.minute_amount_ratio, 0), 5) * 1.2
            + (12 if quote.code == sector.leader_code else 0)
            + (10 if quote.code in sector.core_codes or quote.core else 0)
            + (8 if quote.limit_up else 0)
            + (4 if quote.opened_limit else 0)
        )

    def _sector_flow_points(
        self,
        sector: SectorSnapshot,
        quotes: list[Quote],
        minute_series_map: dict[str, list[dict]],
        times: list[str],
    ) -> list[SectorFlowPoint]:
        cumulative = 0.0
        points: list[SectorFlowPoint] = []
        prev_prices: dict[str, float] = {
            quote.code: (quote.prev_close if quote.prev_close > 0 else quote.price or 1.0)
            for quote in quotes
        }
        running_amounts: dict[str, float] = {quote.code: 0.0 for quote in quotes}
        running_volumes: dict[str, float] = {quote.code: 0.0 for quote in quotes}

        for idx, time_label in enumerate(times):
            step_total = 0.0
            active_codes = 0
            for quote in quotes:
                rows = minute_series_map.get(quote.code) or []
                if not rows:
                    continue
                row = rows[idx] if idx < len(rows) else rows[-1]
                price = float(row.get("price") or prev_prices[quote.code] or quote.price or 0)
                vol = max(float(row.get("vol") or 0), 0)
                if price <= 0 or vol <= 0:
                    prev_prices[quote.code] = price or prev_prices[quote.code]
                    continue

                prev_price = prev_prices[quote.code] or price
                minute_amount = vol * price * 100
                running_amounts[quote.code] += minute_amount
                running_volumes[quote.code] += vol
                vwap = (
                    running_amounts[quote.code] / (running_volumes[quote.code] * 100)
                    if running_volumes[quote.code] > 0
                    else price
                )
                step_total += self.sector_flow_bar_step(sector, quote, price, prev_price, minute_amount, vwap)
                prev_prices[quote.code] = price
                active_codes += 1

            if active_codes:
                cumulative += step_total / active_codes * self.sector_flow_scale
            points.append(SectorFlowPoint(time=time_label, value=round(cumulative, 2)))

        return points

    def sector_flow_bar_step(
        self,
        sector: SectorSnapshot,
        quote: Quote,
        price: float,
        prev_price: float,
        minute_amount: float,
        vwap: float,
    ) -> float:
        """单根（分钟线或快照 tick）的成交额加权动能步长，供分钟曲线与快照代理共用。"""
        gap_pct = ((price - quote.prev_close) / quote.prev_close * 100) if quote.prev_close else 0
        momentum_pct = ((price - prev_price) / prev_price * 100) if prev_price else 0
        vwap_pct = ((price - vwap) / vwap * 100) if vwap else 0
        momentum_signal = clamp(momentum_pct / max(self.sector_flow_momentum_scale_pct, 0.01), -1, 1)
        vwap_signal = clamp(vwap_pct / max(self.sector_flow_vwap_scale_pct, 0.01), -1, 1)
        gap_signal = clamp(gap_pct / max(self.sector_flow_gap_scale_pct, 0.1), -1, 1)
        bias = clamp(
            momentum_signal * self.sector_flow_momentum_weight
            + vwap_signal * self.sector_flow_vwap_weight
            + gap_signal * self.sector_flow_gap_weight,
            -1,
            1,
        )
        if abs(momentum_pct) < 0.02 and abs(vwap_pct) < 0.05:
            bias *= 0.35
        code_weight = 1.0 + (0.12 if quote.code in sector.core_codes else 0.0)
        minute_amount_billion = minute_amount / 100_000_000
        return minute_amount_billion * bias * code_weight

    def _best_sector_for_quote(
        self,
        quote: Quote,
        sector_by_name: dict[str, SectorSnapshot],
        preferred_sector_names: set[str] | None = None,
    ) -> SectorSnapshot | None:
        candidates = [sector_by_name[name] for name in quote.themes if name in sector_by_name]
        if not candidates:
            return None
        if preferred_sector_names:
            preferred = [sector for sector in candidates if sector.name in preferred_sector_names]
            if preferred:
                return max(preferred, key=lambda sector: sector.heat_score)
        return max(candidates, key=lambda sector: sector.heat_score)


def group_quotes_by_theme(quotes: list[Quote]) -> dict[str, list[Quote]]:
    grouped: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        for theme in quote.themes:
            grouped[theme].append(quote)
    return grouped


