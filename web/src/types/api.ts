// 后端 API 类型定义 —— 与 app/main.py + services 返回结构对齐
// 数据口径注意：easy_tdx 提供五档/逐笔成交 L1，不提供委托队列。

export interface IndexQuote {
  code: string
  name: string
  price: number
  prev_close: number
  open: number
  high: number
  low: number
  change_pct: number
  rebound_from_low_pct: number
  minute_amount_ratio: number
  amount: number
}

export interface MarketState {
  trend: string
  emotion_score: number
  breadth_pct: number
  index_turning: boolean
  amount_expanding: boolean
  mainline: string
  indices: IndexQuote[]
  reasons: string[]
  updated_at: string
  up_count: number
  down_count: number
  flat_count: number
  limit_up_count: number
  limit_down_count: number
  opened_limit_count: number
  total_amount: number
  frozen: boolean
  auction_ready: boolean
  auction_status: string
  decision_stage?: string
  index_turning_mode?: string
  index_slope_pct?: number
  market_pulse_points?: number
  order_book_available_count?: number
}

export interface SectorRank {
  name: string
  heat_score: number
  avg_change_pct: number
  up_count: number
  down_count: number
  total_count: number
  limit_up_count: number
  opened_limit_count: number
  core_attack: boolean
  core_codes: string[]
  leader_code: string
  leader_name: string
  reasons: string[]
  rank_change: number
  flow_delta: number
  amount: number
  board_level?: number
  auction_confirmed?: boolean
}

export interface OrderFlowLevel {
  side?: string
  label?: string
  price: number
  volume: number
  amount?: number
}

export interface OrderFlow {
  available: boolean
  data_quality: string
  direction: string
  score: number
  confidence: string
  bid_depth_amount: number
  ask_depth_amount: number
  imbalance_pct: number
  active_buy_volume: number
  active_sell_volume: number
  active_imbalance_pct: number
  minute_amount_ratio: number
  evidence: string[]
  levels: OrderFlowLevel[]
  disclaimer?: string
}

export interface AuctionInfo {
  available: boolean
  data_quality: string
  status: string
  change_pct: number
  volume_ratio: number
  trajectory: string
  phase: string
  confidence: string
  price: number
}

export interface MiniChart {
  times: string[]
  price_pcts: number[]
  vwap_pcts: number[]
  volume_ratios: number[]
  markers: unknown[]
  latest_change_pct: number
  source_quality: string
  point_count: number
}

export interface RiskReward {
  available: boolean
  favorable: boolean
  status: string
  context: string
  structure: string
  direction: string
  action: string
  entry_price: number
  target_price: number
  invalidation_price: number
  support_price: number
  risk_pct: number
  expected_reward_pct: number
  reward_risk_ratio: number
  min_required_ratio: number
  reasons: string[]
  risks: string[]
}

/** 做T公式榜单紧凑快照：趋势多空 / 资金态度（外盘/内盘口径）/ 评分建议 */
export interface TAnalysis {
  available: boolean
  trend_text: string
  trend_bull: boolean | null
  fund_pct: number
  fund_text: string
  fund_attitude: string
  fund_available: boolean
  position_text: string
  vwap_relation: string
  score: number
  advice: string
  advice_label: string
  advice_detail: string
}

export interface BoardItem {
  code: string
  name: string
  themes: string[]
  sector: string
  price: number
  change_pct: number
  amount: number
  minute_amount_ratio: number
  rebound_from_low_pct: number
  pullback_from_high_pct: number
  /** 做T当日常量：阻力/支撑（0 或缺省 = 不可用） */
  resistance?: number
  support?: number
  limit_up: boolean
  limit_down: boolean
  opened_limit: boolean
  signal: string
  signal_score: number
  signal_time?: string
  /** 当天最近一次买T/卖T信号方向（买T / 减T/卖T），空 = 当天尚无买卖信号 */
  last_action?: string
  /** 最近一次买/卖信号的触发价（距买卖点 ±% 展示用） */
  last_action_price?: number
  /** 最近一次买/卖信号的触发时间（HH:MM） */
  last_action_time?: string
  /** 现价是否落在低吸区间（底线-1% ~ 顶线+3%） */
  near_zone?: boolean
  signal_grade?: string
  stock_type: string
  stock_tags: string[]
  activity_score: number
  sector_heat_score: number
  sector_rank: number
  leader: boolean
  core: boolean
  watchlisted: boolean
  position: boolean
  updated_at: string
  phase?: string
  factor_flags?: string[]
  opening_action?: string
  opening_score?: number
  source_quality?: string
  t_plus_one_restricted?: boolean
  order_flow?: OrderFlow
  auction?: AuctionInfo
  risk_reward?: RiskReward
  mini_chart?: MiniChart
  t_analysis?: TAnalysis
}

export interface StockBoard {
  scope: string
  selected_sector: string | null
  board_level: number
  sort: string
  page: number
  page_size: number
  total: number
  updated_at: string
  data_mode: string
  frozen: boolean
  items: BoardItem[]
  available_sorts: string[]
  /** 低吸机会过滤：短期/长期线低者为底（-1%）、高者为顶（+3%），现价落区间内 */
  near_trend?: boolean
  /** 已有当天线值的代码数 */
  near_trend_ready?: number
  /** 线值后台补算中的代码数 */
  near_trend_pending?: number
  /** 置顶买点：最近买点 ±1% 内的票稳定排到最前 */
  pin_buy?: boolean
}

export interface WatchlistEntry {
  code: string
  name: string
  themes: string[]
  core: boolean
  position: boolean
  notes: string
}

export interface SourceStatus {
  data_mode?: string
  scan_scope?: string
  analysis_provider?: string
  full_market_refresh_seconds?: number
  market_session?: string
  is_trading_window?: boolean
  refresh_policy?: MarketRefreshPolicy
  [key: string]: unknown
}

export interface MarketRefreshPolicy {
  market_session?: string
  is_trading_window?: boolean
  traffic_mode?: string
  should_poll?: boolean
  should_stream?: boolean
  poll_interval_ms?: number | null
  stream_interval_seconds?: number | null
  final_refresh?: boolean
  next_open_at?: string
  [key: string]: unknown
}

export interface SectorFlowSeries {
  name: string
  heat_score: number
  final_value: number
  change_pct: number
  leader_code?: string | null
  leader_name?: string | null
  points: { time: string; value: number }[]
  flow_basis?: string
}

export interface TerminalPayload {
  market: MarketState
  sectors: SectorRank[]
  sector_flow: SectorFlowSeries[]
  stock_board: StockBoard
  watchlist: WatchlistEntry[]
  watchlist_preview: BoardItem[]
  positions_preview: unknown[]
  data_mode: string
  source_status: SourceStatus
  selected_sector: string | null
  board_level: number
  board_source: string
  watchlist_codes: string[]
}

// ---- 个股详情 ----

export interface MinuteChartData {
  times: string[]
  prices: number[]
  vwaps: number[]
  volumes: number[]
  change_pcts: number[]
  amount_ratios: number[]
  flow_scores: number[]
  prev_close: number
  source_quality: string
  point_count: number
  start_time: string
  end_time: string
  latest_price: number
  latest_change_pct: number
}

export interface FormulaState {
  // 均线系统
  ma30: number
  qs: number
  vwap: number
  price: number
  // 阻力/支撑/中轴（当日常量）
  resistance: number
  support: number
  mid: number
  // 机构资金统计（万元）
  big_buy_amount: number
  big_sell_amount: number
  fund_flow: number
  // 买卖净（万元，按内外盘拆分当日总额）
  buy_amount_wan: number
  sell_amount_wan: number
  net_amount_wan: number
  buy_pct: number
  sell_pct: number
  // 个股基础数据
  change_pct: number
  volume_ratio: number
  turnover_rate?: number | null
  // 分析文字
  trend_text: string
  volume_text: string
  fund_text: string
  fund_attitude: string
  position_text: string
  vwap_relation: string
  line_a: string
  line_b: string
  // 综合评分与建议
  score: number
  advice: string
  advice_detail: string
  // 最新分钟买卖信号
  buy_signal: boolean
  sell_signal: boolean
  source_quality: string
  point_count: number
}

export interface FormulaOverlay {
  available: boolean
  resistance_pct?: number | null
  support_pct?: number | null
  resistance: number
  support: number
}

export interface ConfluenceFactor {
  available?: boolean
  label: string
  score?: number
  support?: boolean
  sell_pressure?: boolean
  imbalance_pct?: number
  latest_ratio?: number
  expanding?: boolean
  turning?: boolean
  amount_expanding?: boolean
  core_attack?: boolean
  heat_score?: number
}

export interface ConfluenceSnapshot {
  score: number
  summary: string[]
  l1_transaction_flow: ConfluenceFactor
  intraday_volume: ConfluenceFactor
  sector_attack: ConfluenceFactor
  index_turning: ConfluenceFactor
  source_quality: string
  updated_at: string
}

export interface CurrentSignal {
  code: string
  name: string
  signal: string
  score: number
  sector: string
  price: number
  change_pct: number
  rebound_from_low_pct: number
  minute_amount_ratio: number
  reasons: string[]
  risks: string[]
  updated_at: string
  watchlist_tags: string[]
  factor_flags: string[]
  phase?: string
  executable?: boolean
  execution_reason?: string
  invalidation_price?: number
  risk_reward?: RiskReward
}

export interface SignalChartResponse {
  code: string
  name: string
  sector: string
  trade_date: string
  market: MarketState
  sector_snapshot: SectorRank & { mini_chart?: MiniChart }
  current_signal: CurrentSignal
  summary: string[]
  chart: MinuteChartData
  order_flow: OrderFlow
  formula_state: FormulaState
  formula_overlay: FormulaOverlay
  confluence_snapshot: ConfluenceSnapshot
  watchlisted: boolean
  watchlist_tags: string[]
}

export interface OverlayMarker {
  id: string
  time: string
  signal: string
  price: number
  change_pct: number
  phase: string
  reasons: string[]
  risks: string[]
  score: number
  invalidation_price: number
  source_quality: string
  direction: string
  action: string
  executable: boolean
  execution_reason?: string
}

export interface TradePrint {
  time: string
  price: number
  volume: number
  amount: number
  side: string
  side_label: string
  large: boolean
}

export interface TransactionFlow {
  available: boolean
  source: string
  data_quality: string
  trade_date: string
  first_time: string
  as_of: string
  full_session: boolean
  count: number
  buy_volume: number
  sell_volume: number
  buy_amount: number
  sell_amount: number
  imbalance_pct: number
  large_trade_threshold_amount: number
  large_buy_count: number
  large_sell_count: number
  large_buy_amount: number
  large_sell_amount: number
  large_imbalance_pct: number
  score: number
  confidence: string
  evidence: string[]
  recent_trades: TradePrint[]
  note: string
}

export interface SignalOverlayResponse {
  code: string
  name: string
  markers: OverlayMarker[]
  transaction_flow: TransactionFlow
  formula_state: FormulaState
  confluence_snapshot: ConfluenceSnapshot
  /** 盘后/休市冻结标记：true 时前端停止轮询 */
  frozen?: boolean
}

export interface MessageEvidence {
  source: string
  topic_id: string
  topic_title: string
  topic_content: string
  display_text?: string
  create_time: string
  owner_name: string
  likes: number
  readers: number
  comments?: number
  has_files?: boolean
  has_images?: boolean
  media_summary?: string
  event_id: string
  event_title: string
  event_summary: string
  event_type: string
  direction: string
  confidence: number
  impact_strength: number
  keywords: string[]
  entity_type?: string
  code?: string
  role: string
  relevance: number
  impact?: number
  match_scope: string
  name?: string
}

export interface MessageDetailTopic {
  topic_id: string
  title: string
  content: string
  create_time: string
  owner_name: string
  likes: number
  readers: number
  comments: number
  has_files: boolean
  has_images: boolean
  media_kind: string
  media_summary: string
  source: string
}

export interface MessageDetailEvent {
  event_id: string
  topic_id: string
  title: string
  summary: string
  event_type: string
  direction: string | number | null
  confidence: number
  impact_strength: number
  valid_from: string
  expires_at: string
  keywords: string[]
}

export interface MessageDetailLink {
  event_id: string
  entity_type: string
  code: string
  name: string
  role: string
  relevance: number
  impact: number
}

export interface MessageDetailResponse {
  topic: MessageDetailTopic
  event: MessageDetailEvent
  links: MessageDetailLink[]
  sync?: unknown
}

export interface AnalysisRecord {
  status: string
  provider?: string
  model?: string
  generated_at?: string
  raw_text?: string
  result?: unknown
}

export interface DataTable {
  title: string
  columns: string[]
  rows: Record<string, unknown>[]
  row_count: number
}

export interface ExtrasSection {
  available: boolean
  source?: string
  summary?: Record<string, unknown>
  tables?: DataTable[]
  sections?: { title: string; [k: string]: unknown }[]
  note?: string
}

export interface AuctionSnapshot {
  trade_date: string
  as_of: string
  price: number
  volume: number
  amount: number
  imbalance: number
  source: string
  data_quality: string
}

export interface DetailExtrasResponse {
  code: string
  name: string
  trade_date: string
  message_evidence: { stock: MessageEvidence[]; sector: MessageEvidence[] }
  message_status: { topic_count?: number; event_count?: number; latest_event_time?: string }
  analysis: AnalysisRecord | null
  fundamentals: ExtrasSection
  capital_flow: ExtrasSection
  technical_indicators: ExtrasSection
  chanlun: ExtrasSection
  auction_history: AuctionSnapshot[]
}

// ---- 聚合 F10（tushare_pro + easy_tdx） ----

export interface F10Field {
  label: string
  value: unknown
  raw_key: string
}

export interface F10Section {
  key: string
  title: string
  available: boolean
  status?: string
  field_count: number
  row_count: number
  fields: F10Field[]
  tables: DataTable[]
}

export interface F10Category {
  key: string
  title: string
  available: boolean
  error?: string
  source: string
  sections: F10Section[]
}

export interface F10Response {
  available: boolean
  source: string
  code: string
  ts_code: string
  name: string
  fetched_at: string
  category_count: number
  expected_category_count: number
  categories: F10Category[]
  note?: string
}

export interface StockSearchResult {
  code: string
  name: string
  sector?: string
  [k: string]: unknown
}

// ---- 指数分钟共振 ----

export interface IndexMinuteSeries {
  code: string
  name: string
  change_pct: number
  rebound_from_low_pct: number
  points: { time: string; change_pct: number; vol?: number }[]
}

export interface IndexMinutesResponse {
  trade_date: string
  index_turning: boolean
  index_turning_mode: string
  index_slope_pct: number
  amount_expanding: boolean
  indices: IndexMinuteSeries[]
  market_session?: string
  is_trading_window?: boolean
  refresh_policy?: MarketRefreshPolicy
}

// ---- 暗盘资金（2026-08-18 重构：暗吸暗派 / 大手场外 / 东财盘中资金地图） ----

export interface DarkPoolSectorBucket {
  sector: string
  net_amount: number
  stock_count: number
  top_name: string
  top_net: number
}

/** 暗吸/暗派榜行：多日同向净额 + 价格滞涨/抗跌 */
export interface DarkPoolAbsorbRow {
  code: string
  name: string
  sector?: string
  net_window: number
  pos_days: number
  neg_days: number
  days: number
  window_chg_pct: number
  turnover_avg: number
  close: number
}

export interface DarkPoolNorthRow {
  code: string
  name: string
  sector?: string
  change_pct: number
  amount: number
}

export interface DarkPoolBlockRow {
  code: string
  name: string
  sector?: string
  price: number
  close: number
  amount: number
  premium_pct: number
  on_top_list: boolean
}

export interface DarkPoolInstRow {
  code: string
  name: string
  sector?: string
  inst_net: number
  total_net: number
  seats: number
}

export interface DarkPoolEmRow {
  code: string
  name: string
  sector?: string
  change_pct: number
  main_net: number
  main_pct: number
  elg_net: number
}

export interface DarkPoolMarketStrip {
  available: boolean
  trade_date?: string
  main_net_amount?: number
  em_main_net?: number
  em_as_of?: string
  north_turnover?: number
  north_trade_date?: string
  margin_balance?: number
  margin_change?: number
  margin_trade_date?: string
  block_amount?: number
}

export interface DarkPoolSectorFilter {
  sector: string
  board_level: number
  member_count: number
}

export interface DarkPoolPayload {
  as_of: string
  session: string
  enabled: boolean
  is_trading_window?: boolean
  refresh_policy?: MarketRefreshPolicy
  market: DarkPoolMarketStrip
  absorb: {
    available: boolean
    note?: string
    window_dates?: string[]
    window_days?: number
    rule?: string
    source?: string
    inflow?: DarkPoolAbsorbRow[]
    outflow?: DarkPoolAbsorbRow[]
  }
  offmarket: {
    available: boolean
    trade_date?: string
    north_trade_date?: string
    inst_trade_date?: string
    north_note?: string
    north_top10?: DarkPoolNorthRow[]
    blocks?: DarkPoolBlockRow[]
    top_inst?: DarkPoolInstRow[]
  }
  em: {
    available: boolean
    note?: string
    as_of?: string
    stock_count?: number
    total_main_net?: number
    source?: string
    stale_error?: string
    inflow?: DarkPoolEmRow[]
    outflow?: DarkPoolEmRow[]
    sector_rollup_by_level?: { l1?: DarkPoolSectorBucket[]; l2?: DarkPoolSectorBucket[]; l3?: DarkPoolSectorBucket[] }
  }
  sector_filter?: DarkPoolSectorFilter | null
}

// ---- 个股暗盘资金摘要（详情页右栏） ----

export interface DarkPoolStockFlowDay {
  trade_date: string
  net: number
  close: number
  turnover: number
}

export interface DarkPoolStockVerdict {
  label: string
  net_window: number
  pos_days: number
  neg_days: number
  days: number
  window_chg_pct: number
}

export interface DarkPoolStockBlock {
  trade_date: string
  price: number
  close: number
  amount: number
  premium_pct: number
}

export interface DarkPoolStockPayload {
  available: boolean
  code: string
  name: string
  as_of: string
  note?: string
  eod_available?: boolean
  pending?: boolean
  trade_date?: string
  flow_10d?: DarkPoolStockFlowDay[]
  verdict?: DarkPoolStockVerdict
  ths?: { net_today: number; net_d5: number }
  dc?: { net_today: number }
  em?: { as_of: string; main_net: number; main_pct: number; elg_net: number; lg_net: number }
  north_top10?: { trade_date: string; amount: number }
  blocks?: DarkPoolStockBlock[]
  margin?: { trade_date: string; rzye: number; rzye_change?: number }
  top_list?: { trade_date: string; reason: string }[]
}

// ---- 日K详情（AI主力狙击公式 + 筹码峰 + 题材概念） ----

export interface DailyBar {
  date: string
  open: number
  high: number
  low: number
  close: number
  vol: number
  amount: number
}

export interface DailyFormulaTip {
  text: string
  tone: "up" | "down" | "flat"
}

export interface DailyMainFormula {
  available: boolean
  swl?: (number | null)[]
  sws?: (number | null)[]
  ljx?: (number | null)[]
  cost_line?: (number | null)[]
  /** 趋势公式.md：知行短期趋势线 / 知行多空线 + 简单均线（日线级别） */
  zx_trend?: (number | null)[]
  zx_duokong?: (number | null)[]
  ma5?: (number | null)[]
  ma10?: (number | null)[]
  ma20?: (number | null)[]
  trend_latest?: {
    ma5?: number | null
    ma10?: number | null
    ma20?: number | null
    zx_trend?: number | null
    zx_duokong?: number | null
  }
  candle_state?: string[] // "hold" | "watch" | "normal"
  markers?: {
    short_buy: number[]
    white_exit: number[]
    crash: number[]
    limit_up: number[]
    limit_up20: number[]
    lianban: { index: number; count: number }[]
    broken: number[]
    baotuan: number[]
    gaowei: number[]
  }
  strong_support?: number | null
  tomorrow?: { resistance: number; support: number; breakthrough: number; reverse: number }
  score_h?: number
  score_h_max?: number
  tips?: {
    stock?: DailyFormulaTip | null
    market?: DailyFormulaTip | null
    volume?: DailyFormulaTip | null
    yunvx?: number
  }
  quality?: { float_shares: boolean; index_close: boolean; winner: boolean }
}

export interface DailySubResonance {
  available: boolean
  z7?: (number | null)[]
  z8?: (number | null)[]
  www?: (number | null)[]
  tdxlfxj?: (number | null)[]
  kongqi?: (number | null)[]
  strip_weak?: number[]
  strip_mid?: number[]
  strip_top?: number[]
  markers?: { reversal: number[]; start: number[]; kongqi_cross: number[]; red_light: number[] }
  latest?: { red_light: boolean; www: number; tdxlfxj: number }
}

export interface DailySubTrend {
  available: boolean
  trend_line?: (number | null)[]
  trend_accum?: (number | null)[]
  main_accum?: (number | null)[]
  rich_accum?: (number | null)[]
  chongding?: (number | null)[]
  markers?: {
    niu: number[]
    shao: number[]
    yellow_pin: number[]
    pink_pin: number[]
    jigou_chu: number[]
    zhuli_chu: number[]
    red_hat: number[]
    red_triangle: number[]
  }
  latest?: { jigou_chu: boolean; zhuli_chu: boolean; niu: boolean }
}

export interface DailyMainTrend {
  available: boolean
  zx_trend?: (number | null)[]
  zx_duokong?: (number | null)[]
  deviation?: (number | null)[]
  candle_state?: string[]
  markers?: { atr_high: { index: number; price: number }[]; atr_low: { index: number; price: number }[] }
  latest?: { deviation: number; zx_trend: number; zx_duokong: number | null }
}

export interface DailySubBrick {
  available: boolean
  brick?: (number | null)[]
  markers?: { short_buy: number[]; exit: number[] }
  latest?: { brick: number; short_buy: boolean; exit: boolean }
}

export interface DailyFormulas {
  main: DailyMainFormula
  sub_resonance: DailySubResonance
  sub_trend: DailySubTrend
  main_trend?: DailyMainTrend
  sub_brick?: DailySubBrick
}

export interface ChipBin {
  price: number
  weight?: number
  vol?: number
}

export interface ChipDaily {
  available: boolean
  note?: string
  price_low?: number
  price_high?: number
  current_price?: number
  as_of?: string
  bars_used?: number
  bins?: ChipBin[]
  winner_pct?: number
  avg_cost?: number
  cost90?: [number, number]
  cost70?: [number, number]
  concentration90?: number
  concentration70?: number
  peaks?: { price: number; share: number }[]
  quality?: string
}

export interface ChipIntraday {
  available: boolean
  note?: string
  current_price?: number
  prev_close?: number | null
  vwap?: number
  total_vol?: number
  bins?: ChipBin[]
  peak_price?: number | null
  as_of?: string
}

export interface StockTags {
  available: boolean
  industry_official?: string
  industry?: string
  concepts?: string[]
  styles?: string[]
  regions?: string[]
  source?: string
  stale?: boolean
}

export interface DailyDetailResponse {
  code: string
  name: string
  sector: string
  trade_date: string
  prev_close: number
  count: number
  bars: DailyBar[]
  formulas: DailyFormulas
  chip: ChipDaily
  chip_intraday: ChipIntraday
  tags: StockTags
  generated_at: string
}
