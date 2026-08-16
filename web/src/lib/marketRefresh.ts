export type MarketTrafficMode = "realtime" | "reduced" | "finalizing" | "static"

export interface MarketRefreshPolicy {
  market_session?: string
  is_trading_window?: boolean
  traffic_mode?: MarketTrafficMode | string
  should_poll?: boolean
  should_stream?: boolean
  poll_interval_ms?: number | null
  stream_interval_seconds?: number | null
  final_refresh?: boolean
  next_open_at?: string
  [key: string]: unknown
}

interface RefreshSource {
  refresh_policy?: MarketRefreshPolicy
  market_session?: string
  session?: string
  frozen?: boolean
  market?: { frozen?: boolean }
  source_status?: {
    refresh_policy?: MarketRefreshPolicy
    market_session?: string
    frozen?: boolean
    [key: string]: unknown
  }
}

export interface PollingDecisionInput {
  baseIntervalMs: number
  session?: string | null
  source?: RefreshSource | null
  policy?: MarketRefreshPolicy | null
  hasData: boolean
  documentHidden: boolean
}

export interface PollingDecision {
  enabled: boolean
  intervalMs: number | null
}

const REDUCED_INTERVAL_MS = 30_000
const HIDDEN_INTERVAL_MS = 60_000

export function trafficModeFromSession(session?: string | null): MarketTrafficMode {
  switch (session) {
    case "preopen":
    case "morning":
    case "afternoon":
      return "realtime"
    case "lunch_break":
      return "reduced"
    case "closing_buffer":
      return "finalizing"
    case "pre_market":
    case "post_close":
    case "closed_day":
      return "static"
    default:
      return "realtime"
  }
}

function validMode(value: unknown): MarketTrafficMode | null {
  return value === "realtime" || value === "reduced" || value === "finalizing" || value === "static" ? value : null
}

export function refreshPolicyFromPayload(source?: RefreshSource | null): MarketRefreshPolicy | null {
  if (!source) return null
  if (source.refresh_policy) return source.refresh_policy
  if (source.source_status?.refresh_policy) return source.source_status.refresh_policy

  const session = source.market_session ?? source.session ?? source.source_status?.market_session
  const frozen = Boolean(source.market?.frozen ?? source.frozen ?? source.source_status?.frozen)
  if (!session && !frozen) return null

  const trafficMode = frozen ? "static" : trafficModeFromSession(session)
  return {
    market_session: session,
    traffic_mode: trafficMode,
    should_poll: trafficMode !== "static",
    should_stream: trafficMode !== "static",
  }
}

export function pollingDecision(input: PollingDecisionInput): PollingDecision {
  const policy = input.policy ?? refreshPolicyFromPayload(input.source)
  const session = input.session ?? policy?.market_session
  const mode = validMode(policy?.traffic_mode) ?? trafficModeFromSession(session)
  const baseIntervalMs = Math.max(0, Math.floor(input.baseIntervalMs))

  if (baseIntervalMs <= 0) {
    return { enabled: !input.hasData, intervalMs: null }
  }

  if (policy?.should_poll === false || mode === "static") {
    return { enabled: !input.hasData, intervalMs: null }
  }
  if (mode === "finalizing" && policy?.final_refresh && input.hasData) {
    return { enabled: false, intervalMs: null }
  }

  const policyInterval = typeof policy?.poll_interval_ms === "number" ? Math.max(0, policy.poll_interval_ms) : null
  let intervalMs = baseIntervalMs
  if (mode === "reduced" || mode === "finalizing") {
    intervalMs = Math.max(intervalMs, policyInterval ?? REDUCED_INTERVAL_MS)
  } else if (policyInterval !== null) {
    intervalMs = Math.max(intervalMs, policyInterval)
  }
  if (input.documentHidden) {
    intervalMs = Math.max(intervalMs, HIDDEN_INTERVAL_MS)
  }
  return { enabled: true, intervalMs }
}

export function shouldReconnectTerminalStream(source?: RefreshSource | null, policy?: MarketRefreshPolicy | null): boolean {
  const effectivePolicy = policy ?? refreshPolicyFromPayload(source)
  if (effectivePolicy?.should_stream === false) return false
  const mode = validMode(effectivePolicy?.traffic_mode) ?? trafficModeFromSession(effectivePolicy?.market_session)
  if (mode === "static") return false
  if (source?.market?.frozen && mode !== "realtime") return false
  return true
}
