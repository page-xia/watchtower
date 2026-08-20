import type {
  DailyDetailResponse,
  DarkPoolPayload,
  DarkPoolStockPayload,
  DetailExtrasResponse,
  F10Response,
  IndexMinutesResponse,
  MessageDetailResponse,
  SignalChartResponse,
  SignalOverlayResponse,
  StockBoard,
  StockSearchResult,
  TerminalPayload,
  TransactionFlow,
  WatchlistEntry,
} from "@/types/api"
import { getClientId } from "@/lib/clientIdentity"

const BASE = ""

export function clientHeaders(extra: HeadersInit = {}): Headers {
  const headers = new Headers(extra)
  headers.set("X-Client-ID", getClientId())
  return headers
}

async function doFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, { ...init, headers: clientHeaders(init?.headers) })
  if (!resp.ok) {
    const text = await resp.text().catch(() => "")
    throw new Error(`${resp.status} ${resp.statusText}${text ? ` · ${text.slice(0, 120)}` : ""}`)
  }
  return (await resp.json()) as T
}

// In-flight dedup for GET requests: React StrictMode remounts, fast detail
// switching and overlapping polls can all fire identical GETs concurrently.
// Coalesce them onto one in-flight promise so the network only sees one.
const inflightGet = new Map<string, Promise<unknown>>()

async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const isGet = !init || !init.method || init.method.toUpperCase() === "GET"
  if (!isGet) return doFetch<T>(path, init)
  const key = `${path}::${getClientId()}`
  const existing = inflightGet.get(key)
  if (existing) return existing as Promise<T>
  const promise = doFetch<T>(path, init).finally(() => {
    inflightGet.delete(key)
  })
  inflightGet.set(key, promise)
  return promise
}

export interface TerminalParams {
  sector?: string | null
  boardLevel?: number
  sort?: string
  page?: number
  pageSize?: number
  nearTrend?: boolean
  pinBuy?: boolean
  watchlistCodes?: string[]
}

export function getTerminal(params: TerminalParams = {}): Promise<TerminalPayload> {
  const q = new URLSearchParams()
  q.set("view", "terminal")
  q.set("board_level", String(params.boardLevel ?? 3))
  q.set("sort", params.sort ?? "activity")
  q.set("page", String(params.page ?? 1))
  q.set("page_size", String(params.pageSize ?? 40))
  if (params.sector) q.set("sector", params.sector)
  if (params.nearTrend) q.set("near_trend", "1")
  if (params.pinBuy) q.set("pin_buy", "1")
  return fetchJSON<TerminalPayload>(`/api/dashboard?${q.toString()}`)
}

export function getStockBoard(params: TerminalParams = {}): Promise<StockBoard> {
  const q = new URLSearchParams()
  q.set("board_level", String(params.boardLevel ?? 3))
  q.set("sort", params.sort ?? "activity")
  q.set("page", String(params.page ?? 1))
  q.set("page_size", String(params.pageSize ?? 40))
  if (params.sector) q.set("sector", params.sector)
  if (params.nearTrend) q.set("near_trend", "1")
  if (params.pinBuy) q.set("pin_buy", "1")
  return fetchJSON<StockBoard>(`/api/stocks/board?${q.toString()}`)
}

export interface PersonalStateEnvelope<T> {
  items: T[]
  revision: number
  personalization_status: "ready" | "missing_identity" | "unavailable" | string
}

export function getSignalChart(code: string, _watchlistCodes: string[] = []): Promise<SignalChartResponse> {
  void _watchlistCodes
  const q = new URLSearchParams()
  const suffix = q.toString() ? `?${q.toString()}` : ""
  return fetchJSON(`/api/signals/${code}/detail/chart${suffix}`)
}

export function getSignalOverlay(code: string, _watchlistCodes: string[] = []): Promise<SignalOverlayResponse> {
  void _watchlistCodes
  const q = new URLSearchParams()
  const suffix = q.toString() ? `?${q.toString()}` : ""
  return fetchJSON(`/api/signals/${code}/detail/overlay${suffix}`)
}

export interface DetailExtrasOptions {
  includeFundamentals?: boolean
  includeCapitalFlow?: boolean
  includeIndicators?: boolean
  includeChanlun?: boolean
  includeAuctionHistory?: boolean
  includeMessages?: boolean
}

export function getTransactions(code: string, count = 240): Promise<TransactionFlow> {
  return fetchJSON(`/api/transactions/${code}?count=${count}`)
}

export function getDetailF10(code: string, refresh = false): Promise<F10Response> {
  return fetchJSON(`/api/signals/${code}/detail/f10${refresh ? "?refresh=1" : ""}`)
}

export function getDetailDaily(code: string, count = 240): Promise<DailyDetailResponse> {
  return fetchJSON(`/api/signals/${code}/detail/daily?count=${count}`)
}

export function getDetailExtras(
  code: string,
  _watchlistCodes: string[] = [],
  options: DetailExtrasOptions = {},
): Promise<DetailExtrasResponse> {
  void _watchlistCodes
  const {
    includeFundamentals = true,
    includeCapitalFlow = true,
    includeIndicators = true,
    includeChanlun = true,
    includeAuctionHistory = true,
    includeMessages = true,
  } = options
  const q = new URLSearchParams()
  q.set("include_capital_flow", String(includeCapitalFlow))
  q.set("include_fundamentals", String(includeFundamentals))
  q.set("include_indicators", String(includeIndicators))
  q.set("include_chanlun", String(includeChanlun))
  q.set("include_auction_history", String(includeAuctionHistory))
  q.set("include_messages", String(includeMessages))
  return fetchJSON(`/api/signals/${code}/detail/extras?${q.toString()}`)
}

export function getMessageDetail(eventId: string): Promise<MessageDetailResponse> {
  return fetchJSON(`/api/messages/${encodeURIComponent(eventId)}`)
}

export function searchStocks(q: string, _watchlistCodes: string[] = []): Promise<StockSearchResult[]> {
  void _watchlistCodes
  const params = new URLSearchParams()
  params.set("q", q)
  params.set("limit", "12")
  return fetchJSON(`/api/stocks/search?${params.toString()}`)
}

export function getIndexMinutes(): Promise<IndexMinutesResponse> {
  return fetchJSON(`/api/indices/minutes`)
}

export function getDarkPool(sector?: string | null, boardLevel?: number): Promise<DarkPoolPayload> {
  const params = new URLSearchParams()
  if (sector) params.set("sector", sector)
  if (boardLevel != null) params.set("board_level", String(boardLevel))
  const qs = params.toString()
  return fetchJSON(`/api/dark-pool${qs ? `?${qs}` : ""}`)
}

export function getDarkPoolStock(code: string): Promise<DarkPoolStockPayload> {
  return fetchJSON(`/api/dark-pool/stock/${code}`)
}

export function addWatchlist(item: { code: string; name: string; themes?: string[] }, expectedRevision?: number): Promise<PersonalStateEnvelope<WatchlistEntry>> {
  return fetchJSON(`/api/watchlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(expectedRevision == null ? {} : { "X-Expected-Revision": String(expectedRevision) }) },
    body: JSON.stringify({ code: item.code, name: item.name, themes: item.themes ?? [], core: false, position: false, notes: "" }),
  })
}

export function removeWatchlist(code: string, expectedRevision?: number): Promise<PersonalStateEnvelope<WatchlistEntry> & { deleted?: boolean; code?: string }> {
  return fetchJSON(`/api/watchlist/${code}`, { method: "DELETE", headers: expectedRevision == null ? undefined : { "X-Expected-Revision": String(expectedRevision) } })
}

export function listWatchlist(): Promise<PersonalStateEnvelope<WatchlistEntry>> {
  return fetchJSON(`/api/watchlist`)
}

export function importLegacyWatchlist(items: WatchlistEntry[]): Promise<PersonalStateEnvelope<WatchlistEntry> & { migration?: { applied: boolean; reason: string } }> {
  return fetchJSON(`/api/watchlist/import-legacy`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) })
}

export function runAiAnalysis(code: string): Promise<unknown> {
  return fetchJSON(`/api/watchlist/${code}/analysis`, { method: "POST" })
}
