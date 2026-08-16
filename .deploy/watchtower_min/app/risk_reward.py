from __future__ import annotations

"""Point-in-time risk/reward evaluation for the intraday T strategy.

The evaluator deliberately uses only the prices and context supplied by the
caller up to the current bar.  It is a trade-location filter, not a promise
that a projected target will be reached.
"""

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from app.models import RiskRewardPlan


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if isfinite(result) else fallback


def _pct(current: float, reference: float) -> float:
    return (current - reference) / reference * 100 if reference else 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class RiskRewardEvaluator:
    """Turn a setup into a bounded, explainable trade plan."""

    def __init__(self, rules: Mapping[str, Any] | None = None) -> None:
        values = dict(rules or {})
        self.window = max(6, int(_number(values.get("rr_structure_window", 12))))
        self.support_buffer_pct = max(0.05, _number(values.get("rr_support_buffer_pct", 0.20)))
        self.min_risk_pct = max(0.20, _number(values.get("rr_min_risk_pct", 0.35)))
        self.max_risk_pct = max(self.min_risk_pct, _number(values.get("rr_max_risk_pct", 1.80)))
        self.min_reward_pct = max(0.20, _number(values.get("rr_min_reward_pct", 0.65)))
        self.target_cap_pct = max(self.min_reward_pct, _number(values.get("rr_target_cap_pct", 3.50)))
        self.strong_rr = max(1.0, _number(values.get("rr_min_strong", 1.35)))
        self.sector_lead_rr = max(self.strong_rr, _number(values.get("rr_min_sector_lead", 1.55)))
        self.weak_rr = max(self.sector_lead_rr, _number(values.get("rr_min_weak", 1.90)))
        self.afternoon_add = max(0.0, _number(values.get("rr_afternoon_add", 0.25)))
        self.chase_add = max(0.0, _number(values.get("rr_chase_add", 0.25)))
        self.pilot_rr_discount = max(0.0, _number(values.get("rr_pilot_discount", 0.20)))
        self.pilot_rr_floor = max(1.0, _number(values.get("rr_pilot_floor", 1.15)))
        self.late_rebound_pct = _number(values.get("strategy_late_rebound_warning_pct", 5.0))
        self.min_tick_pct = max(0.02, _number(values.get("rr_min_tick_pct", 0.08)))

    def evaluate(
        self,
        *,
        prices: Sequence[float],
        idx: int | None = None,
        price: float | None = None,
        vwap: float = 0.0,
        prev_close: float = 0.0,
        open_price: float = 0.0,
        running_high: float = 0.0,
        rebound_pct: float = 0.0,
        minute_index: int | None = None,
        market_resonance: bool = False,
        market_accelerating: bool = False,
        sector_accelerating: bool = False,
        flow_positive: bool = False,
        flow_pressure: bool = False,
        limit_pct: float = 10.0,
        required_rr_discount: float = 0.0,
        required_rr_floor: float = 1.0,
    ) -> RiskRewardPlan:
        visible = [value for value in (_number(item) for item in prices) if value > 0]
        if idx is not None:
            visible = visible[: max(0, min(int(idx) + 1, len(visible)))]
        current = _number(price, visible[-1] if visible else 0.0)
        if current <= 0:
            return RiskRewardPlan(status="缺少价格", risks=["没有可用的分钟价格"])
        if not visible or visible[-1] != current:
            visible.append(current)

        prior = visible[:-1]
        recent = visible[-self.window :]
        prior_recent = prior[-self.window :] if prior else []
        previous = visible[-2] if len(visible) >= 2 else current
        prior_high = max(prior) if prior else max(_number(open_price), current)
        session_high = max(_number(running_high), max(visible))
        local_low = min(recent) if recent else current
        prior_local_low = min(prior_recent) if prior_recent else local_low
        rebound = max(_number(rebound_pct), _pct(current, local_low))
        steps = [
            abs(visible[pos] - visible[pos - 1])
            for pos in range(max(1, len(visible) - 8), len(visible))
        ]
        typical_step = sorted(steps)[len(steps) // 2] if steps else 0.0
        typical_step_pct = abs(_pct(current + typical_step, current)) if current else 0.0
        # A stop closer than ordinary one-minute noise creates impressive paper
        # R multiples and poor live entries.  Structural support still matters,
        # but the invalidation must survive roughly two normal minute moves.
        volatility_risk_floor = _clamp(
            typical_step_pct * 1.8,
            self.min_risk_pct,
            min(self.max_risk_pct, 1.35),
        )

        near_prior_high = bool(prior_high and current >= prior_high * 0.997)
        breakout = bool(
            near_prior_high
            and current >= prior_high
            and sector_accelerating
            and (market_accelerating or market_resonance)
            and not flow_pressure
        )
        low_turn = bool(
            len(visible) >= 3
            and local_low >= prior_local_low * 0.998
            and current > previous
            and current >= local_low * 1.002
            and rebound <= self.late_rebound_pct
        )
        reclaimed_vwap = bool(
            _number(vwap) > 0
            and current >= _number(vwap)
            and previous < _number(vwap)
        )
        pullback = bool(
            len(visible) >= 4
            and visible[-2] <= visible[-3]
            and current > previous
            and current >= local_low * 1.002
            and (_number(vwap) <= 0 or current >= _number(vwap) * 0.995)
            and not flow_pressure
        )
        chase = bool(
            rebound >= self.late_rebound_pct
            or (near_prior_high and not breakout)
        )

        if breakout:
            structure = "突破前置"
        elif pullback:
            structure = "回踩承接"
        elif low_turn or reclaimed_vwap:
            structure = "低位拐头"
        elif chase:
            structure = "追涨风险"
        else:
            structure = "结构观察"

        support_candidates: list[tuple[float, str]] = []
        for candidate, label in (
            (local_low, f"近{min(len(recent), self.window)}分钟结构低点"),
            (_number(vwap), "分时均价"),
            (_number(open_price), "开盘参考"),
            (_number(prev_close), "昨收参考"),
        ):
            if 0 < candidate < current * (1 - self.min_tick_pct / 100):
                support_candidates.append((candidate, label))
        pivot_start = max(1, len(visible) - self.window)
        pivots = [
            visible[pos]
            for pos in range(pivot_start, max(pivot_start, len(visible) - 1))
            if visible[pos] <= visible[pos - 1] and visible[pos] <= visible[pos + 1]
        ]
        if pivots:
            support_candidates.append((max(pivots), "最近确认回踩低点"))
        breakout_level = prior_high if 0 < prior_high < current else 0.0
        if breakout_level:
            support_candidates.append((breakout_level, "前高突破位"))

        if support_candidates:
            support, support_label = max(support_candidates, key=lambda item: item[0])
        else:
            support = current * (1 - max(volatility_risk_floor, 0.8) / 100)
            support_label = "短周期波动下沿代理"

        buffer_pct = max(self.support_buffer_pct, typical_step_pct * 0.45)
        invalidation = support * (1 - buffer_pct / 100)
        risk_pct = _pct(current, invalidation)
        if risk_pct < volatility_risk_floor:
            invalidation = current * (1 - volatility_risk_floor / 100)
            risk_pct = volatility_risk_floor

        if market_resonance and sector_accelerating:
            context = "主线共振"
            required_rr = self.strong_rr
        elif sector_accelerating and market_accelerating:
            context = "板块先手"
            required_rr = self.sector_lead_rr
        elif sector_accelerating or market_accelerating:
            context = "弱市修复"
            required_rr = self.weak_rr
        else:
            context = "等待盘面"
            required_rr = self.weak_rr
        if minute_index is not None and minute_index >= 120:
            required_rr += self.afternoon_add
        if rebound >= self.late_rebound_pct:
            required_rr += self.chase_add
        if near_prior_high and not breakout:
            required_rr += self.chase_add
        if breakout and context == "主线共振":
            required_rr = max(1.20, required_rr - 0.10)
        discount = max(0.0, _number(required_rr_discount))
        if discount:
            required_rr = max(max(1.0, _number(required_rr_floor, 1.0)), required_rr - discount)
        required_rr = round(required_rr, 2)

        # Project only the next observable structure.  The target is bounded
        # by a short-term range and the statutory price limit; it is never a
        # free-form percentage chosen to force a desired R multiple.
        span = max(recent) - min(recent) if recent else 0.0
        impulse_window = visible[-6:]
        impulse = max(current - min(impulse_window), 0.0) if impulse_window else 0.0
        projected_move = max(
            current * 0.0045,
            span * 0.55,
            impulse * 0.45,
            typical_step * 3.0,
        )
        context_multiplier = 1.10 if context == "主线共振" else 0.92 if context == "板块先手" else 0.70
        cap_price = current * (1 + self.target_cap_pct / 100)
        if prev_close and limit_pct > 0:
            cap_price = min(cap_price, prev_close * (1 + limit_pct / 100 - 0.15 / 100))

        overhead = [
            candidate
            for candidate in (
                _number(session_high),
                _number(prior_high),
                _number(open_price),
                _number(prev_close),
                _number(vwap),
            )
            if candidate > current * (1 + self.min_tick_pct / 100)
        ]
        nearest_resistance = min(overhead) if overhead else 0.0
        reachable_projection = current + projected_move * context_multiplier
        if nearest_resistance:
            target = min(nearest_resistance, reachable_projection, cap_price)
            # Once a real breakout has happened, the next measured leg is more
            # useful than a resistance that is already behind the price.
            if breakout and target <= current * (1 + self.min_reward_pct / 100):
                breakout_move = max(projected_move, impulse * 1.20)
                target = min(cap_price, current + breakout_move * context_multiplier)
        elif breakout:
            breakout_move = max(projected_move, impulse * 1.20)
            target = min(cap_price, current + breakout_move * context_multiplier)
        else:
            # Without an overhead reference, a weak context has no right to
            # assume a full breakout.  Keep a conservative partial projection.
            target = min(cap_price, current + projected_move * context_multiplier * 0.65)
        target = max(current, target)
        reward_pct = _pct(target, current)
        ratio = reward_pct / risk_pct if risk_pct > 0 else 0.0

        context_ready = bool(
            sector_accelerating and (market_accelerating or market_resonance)
        )
        risks: list[str] = []
        if flow_pressure:
            risks.append("盘口/成交方向明显转为抛压")
        if risk_pct > self.max_risk_pct:
            risks.append(f"失效位距离{risk_pct:.2f}%，超过单笔风险上限")
        if reward_pct < self.min_reward_pct:
            risks.append(f"上方可见空间仅{reward_pct:.2f}%")
        if ratio < required_rr:
            risks.append(f"当前盈亏比{ratio:.2f}R低于{required_rr:.2f}R要求")
        if chase:
            risks.append("价格已接近日内高位，除非板块继续突破不追价")
        if not context_ready:
            risks.append("市场与板块尚未同时进入可试错状态")
        if structure == "结构观察":
            risks.append("尚未出现低位拐头、回踩承接或突破前置结构")

        favorable = bool(
            context_ready
            and not flow_pressure
            and self.min_risk_pct <= risk_pct <= self.max_risk_pct
            and reward_pct >= self.min_reward_pct
            and ratio >= required_rr
            and structure not in {"结构观察", "追涨风险"}
        )
        structural_sample = len(visible) >= 3
        if not structural_sample:
            favorable = False
            risks.append("分钟结构不足3个采样点，暂不把赔率当作确定性结论")
        if favorable:
            status = "赔率可接受"
        elif not structural_sample:
            status = "等待结构采样"
        elif flow_pressure:
            status = "盘口否决"
        elif risk_pct > self.max_risk_pct:
            status = "失效位过远"
        elif reward_pct < self.min_reward_pct or ratio < required_rr:
            status = "空间/赔率不足"
        elif not context_ready:
            status = "等待盘面共振"
        else:
            status = "结构未成"

        reasons = [
            f"{context} · {structure} · 支撑参考{support_label}{support:.2f}",
            f"失效价{invalidation:.2f}（风险{risk_pct:.2f}%）",
            f"目标{target:.2f}（预期空间{reward_pct:.2f}%）",
            f"盈亏比{ratio:.2f}R（要求{required_rr:.2f}R）",
        ]
        if breakout:
            reasons.append("突破发生在板块加速与市场改善背景下，允许前置试错")
        elif low_turn:
            reasons.append("局部低点停止下移，价格正在修复分时均价")
        elif pullback:
            reasons.append("回踩未破结构支撑，抛压没有继续扩大")

        room_to_high = _pct(session_high, current) if session_high > current else 0.0
        return RiskRewardPlan(
            available=structural_sample,
            favorable=favorable,
            context=context,
            structure=structure,
            status=status,
            entry_price=round(current, 4),
            support_price=round(support, 4),
            invalidation_price=round(invalidation, 4),
            target_price=round(target, 4),
            risk_pct=round(risk_pct, 3),
            expected_reward_pct=round(reward_pct, 3),
            reward_risk_ratio=round(ratio, 3),
            min_required_ratio=required_rr,
            room_to_day_high_pct=round(max(room_to_high, 0.0), 3),
            reasons=reasons,
            risks=risks,
        )

    def evaluate_direction(
        self,
        *,
        direction: str,
        prices: Sequence[float],
        idx: int | None = None,
        price: float | None = None,
        vwap: float = 0.0,
        support_price: float = 0.0,
        resistance_price: float = 0.0,
        market_state: str = "mixed",
        sector_state: str = "mixed",
        flow_imbalance: float = 0.0,
        price_efficiency: float = 0.0,
        extension_pct: float = 0.0,
        available_quantity: float = 0.0,
        position_quantity: float = 0.0,
        t_plus_one_restricted: bool = False,
        friction_pct: float | None = None,
        slippage_pct: float | None = None,
    ) -> RiskRewardPlan:
        """Evaluate a direction-aware T plan.

        ``evaluate`` above is the compatibility implementation used by the
        older V3 study.  This method is deliberately separate so reverse-T is
        represented as ``sell base -> buy back lower`` rather than as a
        negated positive-T score.  All levels are derived from the visible
        prefix and supplied structural references; no user-facing fixed
        rebound percentage is used as a trigger.
        """

        normalized_direction = str(direction or "none")
        visible = [_number(item) for item in prices if _number(item) > 0]
        if idx is not None:
            visible = visible[: max(0, min(int(idx) + 1, len(visible)))]
        current = _number(price, visible[-1] if visible else 0.0)
        if current <= 0:
            return RiskRewardPlan(
                direction=normalized_direction,
                status="缺少价格",
                reasons=["没有可用的分钟价格"],
            )
        if not visible or visible[-1] != current:
            visible.append(current)
        recent = visible[-self.window :]
        moves = [abs(recent[pos] - recent[pos - 1]) for pos in range(1, len(recent))]
        typical_move = sorted(moves)[len(moves) // 2] if moves else current * 0.002
        structural_noise = max(typical_move, current * self.min_tick_pct / 100.0)
        friction = self.min_tick_pct if friction_pct is None else max(0.0, _number(friction_pct))
        slippage = 0.0 if slippage_pct is None else max(0.0, _number(slippage_pct))
        support = _number(support_price)
        resistance = _number(resistance_price)
        if support <= 0:
            support = max(min(recent), _number(vwap)) if recent else current - structural_noise
        if resistance <= 0:
            resistance = max(recent) if recent else current + structural_noise

        risk_distance = max(structural_noise * 1.5, current * self.min_tick_pct / 100.0)
        reasons: list[str] = []
        risks: list[str] = []
        if normalized_direction == "positive_t":
            invalidation = min(current - risk_distance, support - max(self.support_buffer_pct / 100.0 * current, structural_noise * 0.35))
            if invalidation <= 0 or invalidation >= current:
                invalidation = current - risk_distance
            risk_pct = _pct(current, invalidation)
            overhead = resistance if resistance > current else 0.0
            projected = current + max(structural_noise * 3.0, current * self.min_reward_pct / 100.0)
            target = min(overhead, projected) if overhead else projected
            target = max(current, target)
            reward_pct = _pct(target, current)
            execution_rr = (reward_pct - friction - slippage * 2.0) / max(risk_pct + friction + slippage, self.min_tick_pct)
            context_ready = market_state in {"improving", "resonant", "strong"} and sector_state in {"improving", "resonant", "strong"}
            response_ready = flow_imbalance >= 0 or price_efficiency > 0.35
            structure_ready = current >= max(_number(vwap), support) and len(recent) >= 3
            favorable = bool(context_ready and response_ready and structure_ready and execution_rr >= self.strong_rr)
            if not context_ready:
                risks.append("市场/板块没有形成同步改善，正T只保留研究观察")
            if flow_imbalance < 0 and price_efficiency < 0:
                risks.append("卖方成交与价格响应同时转弱")
            if extension_pct > 0 and extension_pct > _number(self.late_rebound_pct):
                risks.append("位置已延伸，等待回踩而不是追价")
            reasons.extend([
                "正T：买入T仓，等待惯性冲高卖出老股",
                f"支撑/失效 {support:.2f}/{invalidation:.2f}",
                f"目标 {target:.2f}，可实现空间 {reward_pct:.2f}%",
                f"扣摩擦后执行赔率 {execution_rr:.2f}R",
            ])
            return RiskRewardPlan(
                available=len(recent) >= 3,
                favorable=favorable,
                context="市场板块共振" if context_ready else "条件未齐",
                structure="低位拐头/回踩" if structure_ready else "结构待确认",
                status="研究候选" if favorable else "研究观察",
                direction=normalized_direction,
                action="buy_t",
                entry_price=round(current, 4),
                support_price=round(support, 4),
                invalidation_price=round(invalidation, 4),
                target_price=round(target, 4),
                risk_pct=round(risk_pct, 4),
                expected_reward_pct=round(reward_pct, 4),
                reward_risk_ratio=round(reward_pct / max(risk_pct, self.min_tick_pct), 4),
                min_required_ratio=round(self.strong_rr, 3),
                friction_pct=round(friction + slippage * 2.0, 4),
                net_edge_pct=round(reward_pct - friction - slippage * 2.0, 4),
                execution_rr=round(execution_rr, 4),
                reasons=reasons,
                risks=risks,
                fill_status="research_only",
            )

        if normalized_direction == "reverse_t":
            # Reverse-T has its own gate: a base position and a sellable lot are
            # required for execution.  Research events can still be emitted
            # when these values are absent, but the plan says so explicitly.
            sellable = max(0.0, _number(available_quantity))
            total_position = max(0.0, _number(position_quantity))
            invalidation = max(current + risk_distance, resistance + structural_noise * 0.35)
            target = min(current - max(structural_noise * 3.0, current * self.min_reward_pct / 100.0), support)
            if target <= 0 or target >= current:
                target = max(current - max(structural_noise * 3.0, current * self.min_reward_pct / 100.0), current * 0.001)
            risk_pct = _pct(invalidation, current)
            reward_pct = _pct(current, target)
            execution_rr = (reward_pct - friction - slippage * 2.0) / max(risk_pct + friction + slippage, self.min_tick_pct)
            context_weak = market_state in {"weak", "deteriorating", "mixed"} or sector_state in {"weak", "deteriorating", "mixed"}
            response_weak = flow_imbalance <= 0 or price_efficiency < 0.35
            structure_failed = current <= max(_number(vwap), resistance * 0.999 if resistance > 0 else 0.0)
            favorable = bool(context_weak and response_weak and structure_failed and execution_rr >= self.strong_rr)
            if not total_position:
                risks.append("没有持仓，反T卖底仓不可执行")
            elif not sellable or t_plus_one_restricted:
                risks.append("可卖数量为0或受T+1限制，只保留风险观察")
            if not context_weak:
                risks.append("市场/板块仍偏强，反T需要更强的破位证据")
            if flow_imbalance > 0 and price_efficiency > 0:
                risks.append("成交方向与价格响应仍偏多，不提前卖底仓")
            reasons.extend([
                "反T：先卖出可卖底仓，待下跌惯性衰减后买回",
                f"卖出价 {current:.2f}，上方失效 {invalidation:.2f}",
                f"计划买回 {target:.2f}，可实现空间 {reward_pct:.2f}%",
                f"扣摩擦后执行赔率 {execution_rr:.2f}R",
            ])
            fill_status = "executable" if sellable > 0 and not t_plus_one_restricted else "research_only"
            return RiskRewardPlan(
                available=len(recent) >= 3,
                favorable=favorable,
                context="弱势/转弱" if context_weak else "反T环境未确认",
                structure="破位后回补" if structure_failed else "等待破位",
                status="研究候选" if favorable else "研究观察",
                direction=normalized_direction,
                action="sell_base",
                entry_price=round(current, 4),
                sell_price=round(current, 4),
                buyback_price=round(target, 4),
                support_price=round(support, 4),
                invalidation_price=round(invalidation, 4),
                target_price=round(target, 4),
                risk_pct=round(risk_pct, 4),
                expected_reward_pct=round(reward_pct, 4),
                reward_risk_ratio=round(reward_pct / max(risk_pct, self.min_tick_pct), 4),
                min_required_ratio=round(self.strong_rr, 3),
                friction_pct=round(friction + slippage * 2.0, 4),
                net_edge_pct=round(reward_pct - friction - slippage * 2.0, 4),
                execution_rr=round(execution_rr, 4),
                reasons=reasons,
                risks=risks,
                fill_status=fill_status,
            )

        return RiskRewardPlan(
            direction=normalized_direction,
            action="observe",
            status="研究观察",
            reasons=["未识别正T或反T方向"],
            fill_status="research_only",
        )

    @staticmethod
    def with_position(
        plan: RiskRewardPlan,
        *,
        entry_price: float,
        invalidation_price: float,
        target_price: float,
        current_price: float,
        peak_price: float,
    ) -> RiskRewardPlan:
        entry = _number(entry_price)
        invalidation = _number(invalidation_price)
        target = _number(target_price)
        current = _number(current_price)
        risk = _pct(entry, invalidation) if entry > invalidation > 0 else 0.0
        current_r = _pct(current, entry) / risk if risk > 0 else 0.0
        max_r = _pct(max(_number(peak_price), current), entry) / risk if risk > 0 else 0.0
        return plan.model_copy(
            update={
                "entry_price": round(entry, 4),
                "invalidation_price": round(invalidation, 4),
                "target_price": round(target, 4),
                "risk_pct": round(risk, 3),
                "current_r_multiple": round(current_r, 3),
                "max_favorable_r": round(max(max_r, 0.0), 3),
            }
        )
