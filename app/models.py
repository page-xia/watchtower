from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.formula_engine import SIGNAL_VERSION


class SignalType(str, Enum):
    BUY_T = "买T"
    WATCH = "观察"
    SELL_T = "减T/卖T"


class SignalPhase(str, Enum):
    """The lifecycle shown on the intraday chart.

    ``SignalType`` remains the coarse compatibility field used by the board
    and existing clients.  A phase carries the actionable state transition so
    an early warning is not confused with a confirmed buy or sell.
    """

    PRE_ALERT = "先手预警"
    CONFIRM = "低风险确认"
    RETEST_ADD = "回踩加仓"
    REDUCE_ALERT = "减T预警"
    SELL_CONFIRM = "卖T确认"
    DEFENSE = "风险防守"
    CANCEL = "撤销"
    OBSERVE = "观察"


class TradeDirection(str, Enum):
    """Direction of a research event or T plan.

    These values are deliberately English identifiers on the wire.  They are
    stable for storage/API consumers while the UI can render Chinese labels.
    """

    POSITIVE_T = "positive_t"
    REVERSE_T = "reverse_t"
    NONE = "none"


class TradeAction(str, Enum):
    """Concrete action represented by a chart marker."""

    BUY_T = "buy_t"
    SELL_OLD = "sell_old"
    SELL_BASE = "sell_base"
    BUYBACK = "buyback"
    RISK_REBUY = "risk_rebuy"
    OBSERVE = "observe"


class OpeningAction(str, Enum):
    WAIT = "等待采样"
    AUCTION = "竞价候选"
    SCREEN = "初筛候选"
    BUY = "确认买T"
    WATCH = "观察"
    AVOID = "回避"
    REDUCE = "减T"
    CLOSED = "窗口结束"
    UNAVAILABLE = "无开盘快照"


class TrendState(str, Enum):
    TURNING_UP = "分歧转强"
    STRONG = "震荡偏强"
    MIXED = "分歧震荡"
    WEAK = "回落转弱"


class WatchlistItem(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    name: str
    themes: list[str] = Field(default_factory=list)
    core: bool = False
    position: bool = True
    notes: str = ""

    @field_validator("themes", mode="before")
    @classmethod
    def normalize_themes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return list(value)


class PositionRecord(BaseModel):
    """Local position context used only to personalize T observations."""

    code: str = Field(pattern=r"^\d{6}$")
    name: str = ""
    cost: float = Field(default=0, ge=0)
    quantity: float = Field(default=0, ge=0)
    available_quantity: float = Field(default=0, ge=0)
    t_allocation_pct: float = Field(default=100, ge=0, le=100)
    entry_date: str = ""
    updated_at: str = ""
    notes: str = ""

    @model_validator(mode="after")
    def available_cannot_exceed_quantity(self) -> "PositionRecord":
        if self.available_quantity > self.quantity:
            raise ValueError("可卖数量不能大于持仓数量")
        return self


class OrderBookLevel(BaseModel):
    side: str
    level: int
    price: float = 0
    volume: float = 0
    amount: float = 0


class OrderFlowObservation(BaseModel):
    """Transparent order-flow observation.

    The easy_tdx TDX L1 A-share quote packet contains five displayed levels and
    aggregate active buy/sell volume, but it does not contain queue data or
    trade-by-trade order classification. Keep that distinction in the
    payload so a consumer cannot mistake the proxy for a queue feed.
    """

    available: bool = False
    source: str = ""
    data_quality: str = "unavailable"
    level2_available: bool = False
    as_of: str = ""
    direction: str = "无盘口"
    score: int = 0
    confidence: str = "不可用"
    bid_depth_amount: float = 0
    ask_depth_amount: float = 0
    imbalance_pct: float = 0
    active_buy_volume: float = 0
    active_sell_volume: float = 0
    active_imbalance_pct: float = 0
    minute_amount_ratio: float = 1
    evidence: list[str] = Field(default_factory=list)
    levels: list[OrderBookLevel] = Field(default_factory=list)
    disclaimer: str = "成交额/五档代理，不是委托队列或逐笔委托"


class TransactionFlowPoint(BaseModel):
    """Point-in-time minute aggregate derived from easy_tdx transaction prints."""

    time: str
    count: int = 0
    buy_amount: float = 0
    sell_amount: float = 0
    neutral_amount: float = 0
    imbalance_pct: float = 0
    large_buy_amount: float = 0
    large_sell_amount: float = 0
    large_imbalance_pct: float = 0
    amount_ratio: float = 1
    score: int = 0
    rolling_window_minutes: int = 3
    rolling_count: int = 0
    rolling_buy_amount: float = 0
    rolling_sell_amount: float = 0
    rolling_large_buy_amount: float = 0
    rolling_large_sell_amount: float = 0
    rolling_imbalance_pct: float = 0
    rolling_large_imbalance_pct: float = 0
    rolling_score: int = 0


class TransactionTapePrint(BaseModel):
    """Recent L1 transaction print for user-facing tape display."""

    time: str
    price: float = 0
    volume: float = 0
    amount: float = 0
    side: str = "neutral"
    side_label: str = "中性"
    large: bool = False


class TransactionFlowObservation(BaseModel):
    """逐笔成交摘要。

    easy_tdx 的成交明细接口返回成交回报，而不是交易所委托队列。
    这里单独建模，避免把成交方向代理误标成委托队列。
    """

    available: bool = False
    source: str = ""
    data_quality: str = "unavailable"
    trade_date: str = ""
    first_time: str = ""
    as_of: str = ""
    full_session: bool = False
    count: int = 0
    buy_volume: float = 0
    sell_volume: float = 0
    neutral_volume: float = 0
    buy_amount: float = 0
    sell_amount: float = 0
    neutral_amount: float = 0
    imbalance_pct: float = 0
    large_trade_threshold_amount: float = 0
    large_buy_count: int = 0
    large_sell_count: int = 0
    large_buy_amount: float = 0
    large_sell_amount: float = 0
    large_imbalance_pct: float = 0
    score: int = 0
    confidence: str = "不可用"
    evidence: list[str] = Field(default_factory=list)
    recent_trades: list[TransactionTapePrint] = Field(default_factory=list)
    points: list[TransactionFlowPoint] = Field(default_factory=list)
    note: str = "无逐笔成交数据；不生成委托队列结论"


class AuctionSnapshot(BaseModel):
    """集合竞价快照；没有真实数据时保持不可用，不用开盘价冒充竞价。"""

    available: bool = False
    source: str = ""
    data_quality: str = "unavailable"
    trade_date: str = ""
    as_of: str = ""
    price: float = 0
    prev_close: float = 0
    change_pct: float = 0
    volume: float = 0
    amount: float = 0
    volume_ratio: float = 0
    order_imbalance_pct: float = 0
    unmatched_buy_volume: float = 0
    unmatched_sell_volume: float = 0
    bid_depth_volume: float = 0
    ask_depth_volume: float = 0
    snapshot_count: int = 0
    price_slope_pct: float = 0
    price_change_from_first_pct: float = 0
    volume_delta: float = 0
    imbalance_delta_pct: float = 0
    trajectory: str = "暂无轨迹"
    phase: str = "unavailable"
    indicative: bool = False
    status: str = "暂无竞价数据"
    confidence: str = "不可用"
    note: str = "没有接收到真实集合竞价数据"


class Quote(BaseModel):
    code: str
    name: str
    themes: list[str] = Field(default_factory=list)
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    day_high: float
    day_low: float
    change_pct: float
    volume: float = 0
    amount: float = 0
    minute_amount: float = 0
    minute_amount_ratio: float = 1
    limit_up: bool = False
    limit_down: bool = False
    opened_limit: bool = False
    core: bool = False
    updated_at: str
    order_flow: OrderFlowObservation = Field(default_factory=OrderFlowObservation)
    auction: AuctionSnapshot = Field(default_factory=AuctionSnapshot)


class IndexSnapshot(BaseModel):
    code: str
    name: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    change_pct: float
    rebound_from_low_pct: float
    minute_amount_ratio: float = 1
    amount: float = 0


class MarketState(BaseModel):
    trend: TrendState
    emotion_score: int
    breadth_pct: float
    index_turning: bool
    amount_expanding: bool
    mainline: str
    indices: list[IndexSnapshot]
    reasons: list[str]
    updated_at: str
    up_count: int = 0
    down_count: int = 0
    flat_count: int = 0
    limit_up_count: int = 0
    limit_down_count: int = 0
    opened_limit_count: int = 0
    total_amount: float = 0
    frozen: bool = False
    auction_available_count: int = 0
    auction_positive_count: int = 0
    auction_negative_count: int = 0
    auction_avg_change_pct: float = 0
    auction_amount: float = 0
    auction_ready: bool = False
    auction_status: str = "暂无竞价数据"
    auction_data_quality: str = "unavailable"
    auction_snapshot_count: int = 0
    order_book_available_count: int = 0
    level2_available_count: int = 0
    decision_stage: str = "收盘/等待"
    index_turning_mode: str = "snapshot_rebound_proxy"
    index_slope_pct: float = 0
    index_prior_slope_pct: float = 0
    market_pulse_points: int = 0


class MiniIntradayMarker(BaseModel):
    time: str
    signal: SignalType
    price: float = 0
    change_pct: float = 0
    reasons: list[str] = Field(default_factory=list)


class MiniIntradaySeries(BaseModel):
    times: list[str] = Field(default_factory=list)
    price_pcts: list[float] = Field(default_factory=list)
    vwap_pcts: list[float] = Field(default_factory=list)
    volume_ratios: list[float] = Field(default_factory=list)
    markers: list[MiniIntradayMarker] = Field(default_factory=list)
    latest_change_pct: float = 0
    source_quality: str = "unavailable"
    point_count: int = 0


class SectorSnapshot(BaseModel):
    name: str
    heat_score: int
    avg_change_pct: float
    up_count: int
    total_count: int
    limit_up_count: int
    opened_limit_count: int
    core_attack: bool
    core_codes: list[str]
    leader_code: str | None = None
    leader_name: str | None = None
    # 板块中军：成分股里流通市值第一（tushare daily_basic 快照；无数据回退成交额第一）
    capacity_leader_code: str | None = None
    capacity_leader_name: str | None = None
    reasons: list[str]
    rank_change: int = 0
    flow_delta: float = 0
    down_count: int = 0
    raw_total_count: int = 0
    new_listing_excluded_count: int = 0
    amount: float = 0
    main_net_amount: float = 0
    board_code: str = ""
    board_level: int = 0
    board_source: str = ""
    auction_positive_count: int = 0
    auction_available_count: int = 0
    auction_confirmed: bool = False
    mini_chart: MiniIntradaySeries = Field(default_factory=MiniIntradaySeries)


class SectorFlowPoint(BaseModel):
    time: str
    value: float


class SectorFlowSeries(BaseModel):
    name: str
    heat_score: int
    final_value: float
    change_pct: float
    leader_code: str | None = None
    leader_name: str | None = None
    core_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    points: list[SectorFlowPoint] = Field(default_factory=list)
    flow_basis: str = "分钟成交额加权资金动能代理"
    sample_codes: list[str] = Field(default_factory=list)


class RiskRewardPlan(BaseModel):
    """Point-in-time trade plan used to judge whether a setup is still worth taking."""

    available: bool = False
    favorable: bool = False
    context: str = "等待盘面"
    structure: str = "暂无结构"
    status: str = "不参与"
    direction: str = TradeDirection.NONE.value
    action: str = TradeAction.OBSERVE.value
    entry_price: float = 0
    sell_price: float = 0
    buyback_price: float = 0
    support_price: float = 0
    invalidation_price: float = 0
    target_price: float = 0
    risk_pct: float = 0
    expected_reward_pct: float = 0
    reward_risk_ratio: float = 0
    min_required_ratio: float = 0
    current_r_multiple: float = 0
    max_favorable_r: float = 0
    room_to_day_high_pct: float = 0
    friction_pct: float = 0
    net_edge_pct: float = 0
    execution_rr: float = 0
    target_first_probability: float = 0
    fill_status: str = "unknown"
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class TradeSignal(BaseModel):
    code: str
    name: str
    signal: SignalType
    score: int
    sector: str
    price: float
    change_pct: float
    rebound_from_low_pct: float
    minute_amount_ratio: float
    reasons: list[str]
    risks: list[str] = Field(default_factory=list)
    updated_at: str
    pinned: bool = False
    watchlist_tags: list[str] = Field(default_factory=list)
    factor_flags: list[str] = Field(default_factory=list)
    signal_source: str = "snapshot"
    auction: AuctionSnapshot = Field(default_factory=AuctionSnapshot)
    order_flow: OrderFlowObservation = Field(default_factory=OrderFlowObservation)
    decision_stage: str = "观察"
    direction: str = TradeDirection.NONE.value
    action: str = TradeAction.OBSERVE.value
    setup: str = ""
    regime: str = ""
    executable: bool = False
    execution_reason: str = ""
    evidence_sequence: list[str] = Field(default_factory=list)
    validation_status: str = "research_only"
    hypothesis_id: str = ""
    strategy_version: str = SIGNAL_VERSION
    signal_grade: str = "观察"
    confluence_window_bars: int = 0
    phase: str = SignalPhase.OBSERVE.value
    invalidation_price: float = 0
    # 做T公式信号触发那根分钟的价格（0 = 非公式信号/无触发价）；price 字段是每轮刷新的现价
    trigger_price: float = 0
    source_quality: str = "snapshot"
    factor_scores: dict[str, float] = Field(default_factory=dict)
    exit_score: int = 0
    t_plus_one_restricted: bool = False
    action_size_pct: int = 0
    risk_reward: RiskRewardPlan = Field(default_factory=RiskRewardPlan)


class OpeningDecisionItem(BaseModel):
    code: str
    name: str
    sector: str = "未归类"
    action: OpeningAction = OpeningAction.WATCH
    score: int = 0
    stage: str = ""
    stage_label: str = ""
    checkpoint: str = ""
    can_execute: bool = False
    market_score: int = 0
    sector_score: int = 0
    stock_score: int = 0
    market_gate: bool = False
    sector_gate: bool = False
    stock_gate: bool = False
    flow_pressure: bool = False
    position: bool = False
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    data_quality: str = "unavailable"
    updated_at: str = ""
    price: float = 0
    change_pct: float = 0
    minute_amount_ratio: float = 1
    flow_direction: str = ""
    auction_change_pct: float = 0


class OpeningDecisionPayload(BaseModel):
    trade_date: str = ""
    updated_at: str = ""
    stage: str = "closed"
    stage_label: str = "开盘窗口结束"
    checkpoint: str = ""
    active: bool = False
    can_execute: bool = False
    frozen: bool = False
    scope: str = "full_market"
    selected_sector: str | None = None
    total: int = 0
    market_gate: str = "等待"
    market_score: int = 0
    candidate_count: int = 0
    buy_count: int = 0
    defense_count: int = 0
    market_reasons: list[str] = Field(default_factory=list)
    top_candidates: list[OpeningDecisionItem] = Field(default_factory=list)
    top_defense: list[OpeningDecisionItem] = Field(default_factory=list)
    methodology: list[str] = Field(default_factory=list)
    data_quality: str = "unavailable"
    data_note: str = ""
    research: dict[str, Any] = Field(default_factory=dict)


class BoardTAnalysis(BaseModel):
    """做T公式（做T公式.md）的榜单紧凑快照：趋势多空 / 资金态度 / 评分建议。

    全部由榜单既有数据派生（分钟涨幅缩略序列 + 五档外盘/内盘 + 做T压/支），
    不新增逐笔轮询；大单分钟 A2/A3 口径仍只在详情页 formula_state 展示。
    """

    available: bool = False
    trend_text: str = ""  # 多头强势/多头震荡/空头震荡/空头弱势
    trend_bull: bool | None = None  # EMA30 > EMA900（强弱线）
    fund_pct: float = 0  # (外盘-内盘)/(外盘+内盘)×100
    fund_text: str = ""  # +12%
    fund_attitude: str = ""  # 积极做多/偏多/偏空/积极做空
    fund_available: bool = False
    position_text: str = ""  # 突破阻力/强势区/弱势区/跌破支撑
    vwap_relation: str = ""  # ↑均价/↓均价/≈均价
    score: int = 0
    advice: str = ""  # ★强推(85) 积极做多
    advice_label: str = ""  # 强推/关注/观望/减仓/规避
    advice_detail: str = ""


class StockBoardItem(BaseModel):
    code: str
    name: str
    themes: list[str] = Field(default_factory=list)
    sector: str = "未归类"
    price: float = 0
    change_pct: float = 0
    amount: float = 0
    minute_amount_ratio: float = 1
    rebound_from_low_pct: float = 0
    pullback_from_high_pct: float = 0
    # 做T当日常量：阻力=L1+P1*7/8、支撑=L1+P1*0.5/8（L1=min(昨收,最低)，P1=H1-L1）
    resistance: float = 0
    support: float = 0
    limit_up: bool = False
    limit_down: bool = False
    opened_limit: bool = False
    signal: SignalType = SignalType.WATCH
    signal_score: int = 0
    # 当天最近一次买T/卖T信号（方向 + 触发价 + 触发时间）：榜单展示「距买卖点 ±%」用；
    # 按全天窗口从分钟特征缓存计算，信号回「观察」后仍可显示
    last_action: str = ""
    last_action_price: float = 0
    last_action_time: str = ""
    # 现价是否落在低吸区间（短期/长期线低者为底-1%、高者为顶+3%）
    near_zone: bool = False
    stock_type: str = "普通成员"
    stock_tags: list[str] = Field(default_factory=list)
    activity_score: float = 0
    sector_heat_score: int = 0
    sector_rank: int = 0
    leader: bool = False
    core: bool = False
    watchlisted: bool = False
    position: bool = False
    updated_at: str = ""
    order_flow: OrderFlowObservation = Field(default_factory=OrderFlowObservation)
    auction: AuctionSnapshot = Field(default_factory=AuctionSnapshot)
    factor_flags: list[str] = Field(default_factory=list)
    signal_grade: str = "观察"
    opening_action: OpeningAction = OpeningAction.UNAVAILABLE
    opening_score: int = 0
    opening_stage: str = ""
    opening_checkpoint: str = ""
    opening_reasons: list[str] = Field(default_factory=list)
    phase: str = SignalPhase.OBSERVE.value
    signal_time: str = ""
    invalidation_price: float = 0
    source_quality: str = "snapshot"
    exit_score: int = 0
    t_plus_one_restricted: bool = False
    risk_reward: RiskRewardPlan = Field(default_factory=RiskRewardPlan)
    mini_chart: MiniIntradaySeries = Field(default_factory=MiniIntradaySeries)
    t_analysis: BoardTAnalysis = Field(default_factory=BoardTAnalysis)


class StockBoardPayload(BaseModel):
    scope: str = "full_market"
    selected_sector: str | None = None
    board_level: int = 3
    board_source: str = ""
    sort: str = "activity"
    page: int = 1
    page_size: int = 80
    total: int = 0
    updated_at: str = ""
    data_mode: str = ""
    frozen: bool = False
    items: list[StockBoardItem] = Field(default_factory=list)
    # 低吸机会过滤：短期/长期线低者为底（-1%）、高者为顶（+3%），现价落区间内
    near_trend: bool = False
    near_trend_ready: int = 0
    near_trend_pending: int = 0
    # 置顶买点：当天最近买点 ±1% 内的票稳定排到榜单最前（不过滤其余票）
    pin_buy: bool = False
    available_sorts: list[str] = Field(
        default_factory=lambda: ["activity", "change", "amount", "volume_ratio", "order_flow", "signal"]
    )


class ReplayPoint(BaseModel):
    time: str
    price: float
    change_pct: float
    rebound_from_low_pct: float
    pullback_from_high_pct: float
    volume: float
    minute_amount_ratio: float
    signal: SignalType
    reasons: list[str] = Field(default_factory=list)
    signal_score: int = 0
    factor_flags: list[str] = Field(default_factory=list)
    vwap: float = 0
    flow_score: int = 0
    auction_confirmed: bool = False
    strategy_version: str = SIGNAL_VERSION
    signal_grade: str = "观察"
    confluence_window_bars: int = 0
    phase: str = SignalPhase.OBSERVE.value
    risks: list[str] = Field(default_factory=list)
    invalidation_price: float = 0
    source_quality: str = "minute_proxy"
    factor_scores: dict[str, float] = Field(default_factory=dict)
    exit_score: int = 0
    market_event: str = ""
    sector_event: str = ""
    stock_event: str = ""
    flow_event: str = ""
    t_plus_one_restricted: bool = False
    direction: str = TradeDirection.NONE.value
    action: str = TradeAction.OBSERVE.value
    setup: str = ""
    regime: str = ""
    executable: bool = False
    execution_reason: str = ""
    evidence_sequence: list[str] = Field(default_factory=list)
    validation_status: str = "research_only"
    hypothesis_id: str = ""
    risk_reward: RiskRewardPlan = Field(default_factory=RiskRewardPlan)


class ReplayMarker(BaseModel):
    time: str
    signal: SignalType
    price: float
    change_pct: float
    reasons: list[str] = Field(default_factory=list)
    score: int = 0
    factor_flags: list[str] = Field(default_factory=list)
    strategy_version: str = SIGNAL_VERSION
    signal_grade: str = "观察"
    confluence_window_bars: int = 0
    phase: str = SignalPhase.OBSERVE.value
    risks: list[str] = Field(default_factory=list)
    invalidation_price: float = 0
    source_quality: str = "minute_proxy"
    factor_scores: dict[str, float] = Field(default_factory=dict)
    exit_score: int = 0
    market_event: str = ""
    sector_event: str = ""
    stock_event: str = ""
    flow_event: str = ""
    t_plus_one_restricted: bool = False
    action_size_pct: int = 0
    direction: str = TradeDirection.NONE.value
    action: str = TradeAction.OBSERVE.value
    setup: str = ""
    regime: str = ""
    executable: bool = False
    execution_reason: str = ""
    evidence_sequence: list[str] = Field(default_factory=list)
    validation_status: str = "research_only"
    hypothesis_id: str = ""
    risk_reward: RiskRewardPlan = Field(default_factory=RiskRewardPlan)


class AnalysisRecord(BaseModel):
    code: str
    name: str
    trade_date: str
    generated_at: str
    provider: str
    model: str | None = None
    status: str
    source: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    raw_text: str = ""


class MessageTopic(BaseModel):
    topic_id: str
    title: str = ""
    content: str = ""
    create_time: str = ""
    owner_name: str = ""
    likes: int = 0
    readers: int = 0
    comments: int = 0
    has_files: bool = False
    has_images: bool = False
    media_kind: str = "text"
    media_summary: str = ""
    source: str = "zsxq"


class MessageEvent(BaseModel):
    event_id: str
    topic_id: str
    title: str = ""
    summary: str = ""
    event_type: str = ""
    direction: str | int | float | None = None
    confidence: float = 0
    impact_strength: float = 0
    valid_from: str = ""
    expires_at: str = ""
    keywords: list[str] = Field(default_factory=list)

    @field_validator("keywords", mode="before")
    @classmethod
    def normalize_keywords(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            try:
                import json

                parsed = json.loads(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return [str(value).strip()]


class MessageEventLink(BaseModel):
    event_id: str
    entity_type: str
    code: str = ""
    name: str = ""
    role: str = ""
    relevance: float = 0
    impact: float = 0


class ZsxqMessageIngestRequest(BaseModel):
    source: str = "zsxq"
    run_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    start: str | None = None
    end: str | None = None
    upstream_latest_at: str | None = None
    status: str = "success"
    error: str | None = None
    reported_topic_count: int | None = Field(default=None, ge=0)
    reported_event_count: int | None = Field(default=None, ge=0)
    reported_link_count: int | None = Field(default=None, ge=0)
    topics: list[MessageTopic] = Field(default_factory=list)
    events: list[MessageEvent] = Field(default_factory=list)
    links: list[MessageEventLink] = Field(default_factory=list)


class ZsxqMessageIngestResponse(BaseModel):
    ok: bool
    source: str
    run_id: str
    topic_count: int
    event_count: int
    link_count: int


class MessageSyncRunStatus(BaseModel):
    run_id: str = ""
    source: str = ""
    started_at: str = ""
    finished_at: str = ""
    start: str = ""
    end: str = ""
    topic_count: int = 0
    event_count: int = 0
    link_count: int = 0
    upstream_latest_at: str = ""
    status: str = ""
    error: str = ""


class MessageStoreStatus(BaseModel):
    db_file: str
    ingest_enabled: bool = False
    topic_count: int = 0
    event_count: int = 0
    link_count: int = 0
    latest_topic_time: str = ""
    latest_event_time: str = ""
    latest_run: MessageSyncRunStatus | None = None


class MessageEvidence(BaseModel):
    source: str = "zsxq"
    topic_id: str
    topic_title: str = ""
    topic_content: str = ""
    display_text: str = ""
    create_time: str = ""
    owner_name: str = ""
    likes: int = 0
    readers: int = 0
    comments: int = 0
    has_files: bool = False
    has_images: bool = False
    media_summary: str = ""
    event_id: str
    event_title: str = ""
    event_summary: str = ""
    event_type: str = ""
    direction: str = ""
    confidence: float = 0
    impact_strength: float = 0
    valid_from: str = ""
    expires_at: str = ""
    keywords: list[str] = Field(default_factory=list)
    entity_type: str = ""
    code: str = ""
    name: str = ""
    role: str = ""
    relevance: float = 0
    impact: float = 0
    match_scope: str = ""


class MessageEvidenceBundle(BaseModel):
    stock: list[MessageEvidence] = Field(default_factory=list)
    sector: list[MessageEvidence] = Field(default_factory=list)


class DetailChartSeries(BaseModel):
    """Columnar minute series used by the fast chart-first detail path."""

    times: list[str] = Field(default_factory=list)
    prices: list[float] = Field(default_factory=list)
    vwaps: list[float] = Field(default_factory=list)
    volumes: list[float] = Field(default_factory=list)
    change_pcts: list[float] = Field(default_factory=list)
    amount_ratios: list[float] = Field(default_factory=list)
    flow_scores: list[int] = Field(default_factory=list)
    prev_close: float = 0
    source_quality: str = "minute_proxy"
    point_count: int = 0
    start_time: str = ""
    end_time: str = ""
    latest_price: float = 0
    latest_change_pct: float = 0


class FormulaState(BaseModel):
    """做T公式（做T公式.md）最新分钟状态：均线/阻力支撑/资金/评分/建议。"""

    # 均线系统
    ma30: float = 0
    qs: float = 0  # 强弱线 EMA(C,900)
    vwap: float = 0  # 均价 SUM(V*C,0)/SUM(V,0)
    price: float = 0  # 现价
    # 阻力/支撑/中轴（当日常量）
    resistance: float = 0
    support: float = 0
    mid: float = 0
    # 机构资金统计（万元）
    big_buy_amount: float = 0  # A2
    big_sell_amount: float = 0  # A3
    fund_flow: float = 0  # A2-A3
    # 买卖净（万元，按内外盘拆分当日总额）
    buy_amount_wan: float = 0
    sell_amount_wan: float = 0
    net_amount_wan: float = 0
    buy_pct: float = 0
    sell_pct: float = 0
    # 个股基础数据
    change_pct: float = 0
    volume_ratio: float = 1
    turnover_rate: float | None = None
    # 分析文字
    trend_text: str = ""
    volume_text: str = ""
    fund_text: str = ""
    fund_attitude: str = ""
    position_text: str = ""
    vwap_relation: str = ""
    line_a: str = ""
    line_b: str = ""
    # 综合评分与建议
    score: int = 0
    advice: str = ""
    advice_detail: str = ""
    # 最新分钟买卖信号
    buy_signal: bool = False
    sell_signal: bool = False
    source_quality: str = "unavailable"
    point_count: int = 0


class ConfluenceSnapshot(BaseModel):
    score: int = 0
    summary: list[str] = Field(default_factory=list)
    l1_transaction_flow: dict[str, Any] = Field(default_factory=dict)
    intraday_volume: dict[str, Any] = Field(default_factory=dict)
    sector_attack: dict[str, Any] = Field(default_factory=dict)
    index_turning: dict[str, Any] = Field(default_factory=dict)
    source_quality: str = "minute_proxy"
    updated_at: str = ""


class FormulaOverlay(BaseModel):
    """做T公式分时叠层：阻力/支撑两条关键价位线（相对昨收涨幅 + 绝对价）。

    MA30/强弱线只在公式卡片呈现数值，不上分时图，避免曲线过多干扰读价。
    """

    available: bool = False
    resistance_pct: float | None = None
    support_pct: float | None = None
    resistance: float = 0
    support: float = 0


class SignalDetailChartPayload(BaseModel):
    code: str
    name: str
    sector: str
    trade_date: str
    selected_sector: str | None = None
    market: MarketState
    sector_snapshot: SectorSnapshot | None = None
    current_signal: TradeSignal
    summary: list[str] = Field(default_factory=list)
    chart: DetailChartSeries = Field(default_factory=DetailChartSeries)
    formula_overlay: "FormulaOverlay" = Field(default_factory=lambda: FormulaOverlay())
    order_flow: OrderFlowObservation = Field(default_factory=OrderFlowObservation)
    watchlisted: bool = False
    watchlist_tags: list[str] = Field(default_factory=list)
    position: PositionRecord | None = None
    formula_state: FormulaState = Field(default_factory=FormulaState)
    confluence_snapshot: ConfluenceSnapshot = Field(default_factory=ConfluenceSnapshot)


class SignalDetailOverlayMarker(BaseModel):
    id: str
    time: str
    signal: SignalType
    price: float
    change_pct: float
    phase: str = SignalPhase.OBSERVE.value
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    score: int = 0
    exit_score: int = 0
    invalidation_price: float = 0
    source_quality: str = "minute_proxy"
    action_size_pct: int = 0
    direction: str = TradeDirection.NONE.value
    action: str = TradeAction.OBSERVE.value
    setup: str = ""
    regime: str = ""
    executable: bool = False
    execution_reason: str = ""
    validation_status: str = "research_only"
    hypothesis_id: str = ""
    t_plus_one_restricted: bool = False
    risk_reward: RiskRewardPlan = Field(default_factory=RiskRewardPlan)
    market_event: str = ""
    sector_event: str = ""
    stock_event: str = ""
    flow_event: str = ""


class SignalDetailOverlayPayload(BaseModel):
    code: str
    name: str
    sector: str
    trade_date: str
    selected_sector: str | None = None
    # 盘后/休市冻结标记：前端据此停掉 overlay 轮询（chart 载荷自带 market.frozen）。
    frozen: bool = False
    markers: list[SignalDetailOverlayMarker] = Field(default_factory=list)
    transaction_flow: dict[str, Any] = Field(default_factory=dict)
    formula_state: FormulaState = Field(default_factory=FormulaState)
    confluence_snapshot: ConfluenceSnapshot = Field(default_factory=ConfluenceSnapshot)


class FundamentalField(BaseModel):
    label: str
    value: Any = None
    raw_key: str = ""


class FundamentalTable(BaseModel):
    title: str = ""
    columns: list[str] = Field(default_factory=list)
    raw_columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0


class FundamentalSection(BaseModel):
    key: str
    title: str
    available: bool = False
    status: str = ""
    entry: str = ""
    error: str = ""
    field_count: int = 0
    row_count: int = 0
    fields: list[FundamentalField] = Field(default_factory=list)
    tables: list[FundamentalTable] = Field(default_factory=list)


class FundamentalPayload(BaseModel):
    available: bool = False
    source: str = "easy_tdx_f10_7615"
    code: str = ""
    fetched_at: str = ""
    section_count: int = 0
    expected_section_count: int = 21
    sections: list[FundamentalSection] = Field(default_factory=list)
    note: str = "easy_tdx F10/财务数据，仅作个人非商业研究展示"


class F10Category(BaseModel):
    key: str
    title: str
    available: bool = False
    error: str = ""
    source: str = "tushare_pro"
    sections: list[FundamentalSection] = Field(default_factory=list)


class F10Payload(BaseModel):
    available: bool = False
    source: str = "tushare_pro + easy_tdx_f10_7615"
    code: str = ""
    ts_code: str = ""
    name: str = ""
    fetched_at: str = ""
    category_count: int = 0
    expected_category_count: int = 13
    categories: list[F10Category] = Field(default_factory=list)
    note: str = "F10 聚合：tushare_pro 为主，easy_tdx 股本结构补充；仅作个人非商业研究展示"


class DetailDataTable(BaseModel):
    title: str = ""
    columns: list[str] = Field(default_factory=list)
    raw_columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0


class DetailDataPayload(BaseModel):
    available: bool = False
    source: str = ""
    code: str = ""
    fetched_at: str = ""
    summary: dict[str, Any] = Field(default_factory=dict)
    tables: list[DetailDataTable] = Field(default_factory=list)
    note: str = ""


class SignalDetailExtrasPayload(BaseModel):
    code: str
    name: str
    sector: str
    trade_date: str
    selected_sector: str | None = None
    watchlisted: bool = False
    watchlist_tags: list[str] = Field(default_factory=list)
    position: PositionRecord | None = None
    auction_history: list[dict[str, Any]] = Field(default_factory=list)
    message_evidence: MessageEvidenceBundle = Field(default_factory=MessageEvidenceBundle)
    message_status: MessageStoreStatus | None = None
    analysis: AnalysisRecord | None = None
    fundamentals: FundamentalPayload = Field(default_factory=FundamentalPayload)
    capital_flow: DetailDataPayload = Field(default_factory=DetailDataPayload)
    technical_indicators: DetailDataPayload = Field(default_factory=DetailDataPayload)
    chanlun: DetailDataPayload = Field(default_factory=DetailDataPayload)


class MessageDetailPayload(BaseModel):
    topic: MessageTopic
    event: MessageEvent
    links: list[MessageEventLink] = Field(default_factory=list)
    sync: MessageStoreStatus | None = None


class SignalReplayDetail(BaseModel):
    code: str
    name: str
    sector: str
    trade_date: str
    selected_sector: str | None = None
    market: MarketState
    sector_snapshot: SectorSnapshot | None = None
    current_signal: TradeSignal
    replay_points: list[ReplayPoint]
    signal_timeline: list[ReplayMarker] = Field(default_factory=list)
    markers: list[ReplayMarker]
    summary: list[str]
    analysis: AnalysisRecord | None = None
    message_evidence: MessageEvidenceBundle = Field(default_factory=MessageEvidenceBundle)
    message_status: MessageStoreStatus | None = None
    auction_history: list[dict[str, Any]] = Field(default_factory=list)
    watchlisted: bool = False
    watchlist_tags: list[str] = Field(default_factory=list)
    order_flow: OrderFlowObservation = Field(default_factory=OrderFlowObservation)
    transaction_flow: TransactionFlowObservation = Field(default_factory=TransactionFlowObservation)
    position: PositionRecord | None = None
    decision_markers: list[ReplayMarker] = Field(default_factory=list)
    formula_state: FormulaState = Field(default_factory=FormulaState)
    confluence_snapshot: ConfluenceSnapshot = Field(default_factory=ConfluenceSnapshot)


class IndexReplayDetail(BaseModel):
    code: str
    name: str
    trade_date: str
    market: MarketState
    current_index: IndexSnapshot
    replay_points: list[ReplayPoint]
    markers: list[ReplayMarker] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)


class EventItem(BaseModel):
    time: str
    level: str
    title: str
    detail: str


class TerminalPayload(BaseModel):
    market: MarketState
    sectors: list[SectorSnapshot]
    sector_flow: list[SectorFlowSeries] = Field(default_factory=list)
    stock_board: StockBoardPayload
    watchlist: list[WatchlistItem] = Field(default_factory=list)
    watchlist_preview: list[dict[str, Any]] = Field(default_factory=list)
    positions_preview: list[dict[str, Any]] = Field(default_factory=list)
    data_mode: str
    source_status: dict[str, Any]
    selected_sector: str | None = None
    sector_focus: SectorSnapshot | None = None
    board_level: int = 3
    board_source: str = ""
    watchlist_codes: list[str] = Field(default_factory=list)
    personalization_status: str = "missing_identity"
    personalization_revision: int = 0


class DashboardPayload(BaseModel):
    market: MarketState
    sectors: list[SectorSnapshot]
    sector_flow: list[SectorFlowSeries] = Field(default_factory=list)
    signals: list[TradeSignal]
    core_watch: list[TradeSignal]
    events: list[EventItem]
    watchlist: list[WatchlistItem]
    data_mode: str
    source_status: dict[str, Any]
    selected_sector: str | None = None
    sector_focus: SectorSnapshot | None = None
    watchlist_codes: list[str] = Field(default_factory=list)
    personalization_status: str = "missing_identity"
    personalization_revision: int = 0

