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
  limit_up: boolean
  limit_down: boolean
  opened_limit: boolean
  signal: string
  signal_score: number
  signal_time?: string
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
  opening_markers: OpeningMarkerEvent[]
}

// ---- 开盘窗口菱形买卖点（机会队列） ----

export interface OpeningMarkerEvent {
  id: string
  trade_date: string
  code: string
  name: string
  sector: string
  time: string
  first_seen: string
  side: "buy" | "sell"
  rule: string
  label: string
  price: number
  change_pct: number
  /** 实时行情回填（队列行显示用）；price/change_pct 保持信号时刻快照语义 */
  live_price?: number
  live_change_pct?: number
  live_amount?: number
  regime?: string
  reasons: string[]
  tape_net_ratio?: number | null
  /** warn=空心预警（确认中）；confirmed=实心确认 */
  state: "warn" | "confirmed"
  confirmed_at?: string
  source_quality: string
  validation_status: string
  executable: boolean
}

export interface OpeningMarkersPage {
  trade_date: string
  total: number
  offset: number
  limit: number
  items: OpeningMarkerEvent[]
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
  duo_strength: number
  kong_strength: number
  main_absorption: number
  fast_trigger: boolean
  protection_price: number
  white_line: number
  yellow_line: number
  white_distance_pct: number
  yellow_distance_pct: number
  trend_distance_pct: number
  near_trend_line: boolean
  near_trend_line_name: string
  gold_resonance: boolean
  resonance_reasons: string[]
  source_quality: string
  trigger_note?: string
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
  confluence_snapshot: ConfluenceSnapshot
  watchlisted: boolean
  watchlist_tags: string[]
  research_status?: string
  research_note?: string
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
  opening_markers: OverlayMarker[]
  transaction_flow: TransactionFlow
  formula_state: FormulaState
  confluence_snapshot: ConfluenceSnapshot
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

// ---- 暗盘资金 ----

export interface DarkPoolIntradayRow {
  code: string
  name: string
  sector?: string
  change_pct: number
  large_buy_amount: number
  large_sell_amount: number
  net_amount: number
  net_ratio_pct: number
  tag: string
}

export interface DarkPoolSectorBucket {
  sector: string
  net_amount: number
  stock_count: number
  top_name: string
  top_net: number
}

export interface DarkPoolEodRow {
  code: string
  name: string
  sector?: string
  net_mf_amount: number
  on_top_list: boolean
}

export interface DarkPoolBlockRow {
  code: string
  name: string
  sector?: string
  price: number
  vol: number
  amount: number
  close: number
  premium_pct: number
  on_top_list: boolean
}

export interface DarkPoolPayload {
  as_of: string
  session: string
  enabled: boolean
  is_trading_window?: boolean
  refresh_policy?: MarketRefreshPolicy
  intraday: {
    available: boolean
    refreshed_at?: string
    pool_size?: number
    errors?: number
    source?: string
    note?: string
    rows?: DarkPoolIntradayRow[]
    sector_rollup?: DarkPoolSectorBucket[]
    sector_rollup_by_level?: { l1?: DarkPoolSectorBucket[]; l2?: DarkPoolSectorBucket[]; l3?: DarkPoolSectorBucket[] }
  }
  eod: {
    available: boolean
    trade_date?: string
    source?: string
    note?: string
    main_inflow?: DarkPoolEodRow[]
    main_outflow?: DarkPoolEodRow[]
    block_trades?: DarkPoolBlockRow[]
    sector_rollup?: DarkPoolSectorBucket[]
    sector_rollup_by_level?: { l1?: DarkPoolSectorBucket[]; l2?: DarkPoolSectorBucket[]; l3?: DarkPoolSectorBucket[] }
  }
}
