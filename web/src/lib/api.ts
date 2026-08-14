import type {
  DarkPoolPayload,
  DetailExtrasResponse,
  IndexMinutesResponse,
  OpeningMarkersPage,
  SignalChartResponse,
  SignalOverlayResponse,
  StockBoard,
  StockSearchResult,
  TerminalPayload,
  TransactionFlow,
  WatchlistEntry,
} from "@/types/api"

const BASE = ""

async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, init)
  if (!resp.ok) {
    const text = await resp.text().catch(() => "")
    throw new Error(`${resp.status} ${resp.statusText}${text ? ` · ${text.slice(0, 120)}` : ""}`)
  }
  return (await resp.json()) as T
}

export interface TerminalParams {
  sector?: string | null
  boardLevel?: number
  sort?: string
  page?: number
  pageSize?: number
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
  q.set("watchlist_codes", (params.watchlistCodes ?? []).join(","))
  return fetchJSON<TerminalPayload>(`/api/dashboard?${q.toString()}`)
}

export function getStockBoard(params: TerminalParams = {}): Promise<StockBoard> {
  const q = new URLSearchParams()
  q.set("board_level", String(params.boardLevel ?? 3))
  q.set("sort", params.sort ?? "activity")
  q.set("page", String(params.page ?? 1))
  q.set("page_size", String(params.pageSize ?? 40))
  if (params.sector) q.set("sector", params.sector)
  q.set("watchlist_codes", (params.watchlistCodes ?? []).join(","))
  return fetchJSON<StockBoard>(`/api/stocks/board?${q.toString()}`)
}

export function getSignalChart(code: string, watchlistCodes: string[] = []): Promise<SignalChartResponse> {
  const q = new URLSearchParams()
  q.set("watchlist_codes", watchlistCodes.join(","))
  const suffix = q.toString() ? `?${q.toString()}` : ""
  return fetchJSON(`/api/signals/${code}/detail/chart${suffix}`)
}

export function getSignalOverlay(code: string, watchlistCodes: string[] = []): Promise<SignalOverlayResponse> {
  const q = new URLSearchParams()
  q.set("watchlist_codes", watchlistCodes.join(","))
  const suffix = q.toString() ? `?${q.toString()}` : ""
  return fetchJSON(`/api/signals/${code}/detail/overlay${suffix}`)
}

export function getTransactions(code: string, count = 240): Promise<TransactionFlow> {
  return fetchJSON(`/api/transactions/${code}?count=${count}`)
}

export function getDetailExtras(code: string, watchlistCodes: string[] = []): Promise<DetailExtrasResponse> {
  const q = new URLSearchParams()
  q.set("include_capital_flow", "true")
  q.set("include_fundamentals", "true")
  q.set("include_indicators", "true")
  q.set("include_chanlun", "true")
  q.set("watchlist_codes", watchlistCodes.join(","))
  return fetchJSON(`/api/signals/${code}/detail/extras?${q.toString()}`)
}

export function searchStocks(q: string, watchlistCodes: string[] = []): Promise<StockSearchResult[]> {
  const params = new URLSearchParams()
  params.set("q", q)
  params.set("limit", "12")
  params.set("watchlist_codes", watchlistCodes.join(","))
  return fetchJSON(`/api/stocks/search?${params.toString()}`)
}

export function getIndexMinutes(): Promise<IndexMinutesResponse> {
  return fetchJSON(`/api/indices/minutes`)
}

export function getDarkPool(): Promise<DarkPoolPayload> {
  return fetchJSON(`/api/dark-pool`)
}

export function getOpeningMarkers(offset = 0, limit = 20, tradeDate?: string, side?: "buy" | "sell"): Promise<OpeningMarkersPage> {
  const q = new URLSearchParams()
  q.set("offset", String(offset))
  q.set("limit", String(limit))
  if (tradeDate) q.set("trade_date", tradeDate)
  if (side) q.set("side", side)
  return fetchJSON(`/api/opening/markers?${q.toString()}`)
}

export function addWatchlist(item: { code: string; name: string; themes?: string[] }): Promise<unknown> {
  return fetchJSON(`/api/watchlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: item.code, name: item.name, themes: item.themes ?? [], core: false, position: false, notes: "" }),
  })
}

export function removeWatchlist(code: string): Promise<unknown> {
  return fetchJSON(`/api/watchlist/${code}`, { method: "DELETE" })
}

export function listWatchlist(): Promise<WatchlistEntry[]> {
  return fetchJSON(`/api/watchlist`)
}

export function runAiAnalysis(code: string): Promise<unknown> {
  return fetchJSON(`/api/watchlist/${code}/analysis`, { method: "POST" })
}
