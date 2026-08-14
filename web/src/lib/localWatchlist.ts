import type { BoardItem, TerminalPayload, WatchlistEntry } from "@/types/api"

const STORAGE_KEY = "watchtower.watchlist.v1"

export type LocalWatchlistEntry = Pick<WatchlistEntry, "code" | "name" | "themes" | "core" | "position" | "notes">

function normalizeCode(value: unknown): string | null {
  const text = String(value ?? "").trim()
  if (!/^\d{1,6}$/.test(text)) return null
  return text.padStart(6, "0")
}

function normalizeThemes(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => String(item ?? "").trim()).filter(Boolean)
}

function normalizeEntry(value: unknown): LocalWatchlistEntry | null {
  if (!value || typeof value !== "object") return null
  const record = value as Record<string, unknown>
  const code = normalizeCode(record.code)
  if (!code) return null
  return {
    code,
    name: String(record.name ?? "").trim(),
    themes: normalizeThemes(record.themes),
    core: Boolean(record.core),
    position: false,
    notes: String(record.notes ?? ""),
  }
}

export function loadLocalWatchlist(): LocalWatchlistEntry[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    if (!Array.isArray(parsed)) return []
    const items: LocalWatchlistEntry[] = []
    const seen = new Set<string>()
    for (const rawItem of parsed) {
      const item = normalizeEntry(rawItem)
      if (!item || seen.has(item.code)) continue
      seen.add(item.code)
      items.push(item)
    }
    return items
  } catch (error) {
    console.warn("读取本地自选失败", error)
    return []
  }
}

export function saveLocalWatchlist(items: LocalWatchlistEntry[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items))
  } catch (error) {
    console.warn("保存本地自选失败", error)
  }
}

export function watchlistCodes(items: LocalWatchlistEntry[]): string[] {
  return items.map((item) => item.code)
}

export function upsertLocalWatchlist(items: LocalWatchlistEntry[], item: Pick<BoardItem, "code" | "name"> & Partial<BoardItem>): LocalWatchlistEntry[] {
  const normalized = normalizeEntry({
    code: item.code,
    name: item.name,
    themes: item.themes ?? [],
    core: item.core ?? false,
    position: false,
    notes: "",
  })
  if (!normalized) return items
  return [...items.filter((entry) => entry.code !== normalized.code), normalized]
}

export function removeLocalWatchlist(items: LocalWatchlistEntry[], code: string): LocalWatchlistEntry[] {
  const normalized = normalizeCode(code)
  if (!normalized) return items
  return items.filter((entry) => entry.code !== normalized)
}

export function applyLocalWatchlistToBoardItems(items: BoardItem[], localItems: LocalWatchlistEntry[]): BoardItem[] {
  if (items.length === 0) return items
  const codes = new Set(localItems.map((item) => item.code))
  let changed = false
  const next = items.map((item) => {
    const watchlisted = codes.has(item.code)
    if (item.watchlisted === watchlisted) return item
    changed = true
    return { ...item, watchlisted }
  })
  return changed ? next : items
}

function watchlistPreviewFromPayload(payload: TerminalPayload, localItems: LocalWatchlistEntry[]): BoardItem[] {
  const codes = new Set(localItems.map((item) => item.code))
  if (codes.size === 0) return []
  const existing = new Map<string, BoardItem>()
  for (const item of payload.watchlist_preview ?? []) {
    if (codes.has(item.code)) existing.set(item.code, { ...item, watchlisted: true })
  }
  for (const item of payload.stock_board?.items ?? []) {
    if (codes.has(item.code) && !existing.has(item.code)) existing.set(item.code, { ...item, watchlisted: true })
  }
  return localItems.flatMap((item) => {
    const preview = existing.get(item.code)
    return [preview ?? localWatchlistPlaceholder(item)]
  })
}

function localWatchlistPlaceholder(item: LocalWatchlistEntry): BoardItem {
  return {
    code: item.code,
    name: item.name || item.code,
    themes: item.themes,
    sector: item.themes[0] ?? "",
    price: Number.NaN,
    change_pct: Number.NaN,
    amount: 0,
    minute_amount_ratio: Number.NaN,
    rebound_from_low_pct: Number.NaN,
    pullback_from_high_pct: Number.NaN,
    limit_up: false,
    limit_down: false,
    opened_limit: false,
    signal: "",
    signal_score: 0,
    stock_type: "",
    stock_tags: [],
    activity_score: 0,
    sector_heat_score: 0,
    sector_rank: 0,
    leader: false,
    core: item.core,
    watchlisted: true,
    position: item.position,
    updated_at: "",
  }
}

export function localWatchlistPlaceholders(items: LocalWatchlistEntry[]): BoardItem[] {
  return items.map((item) => localWatchlistPlaceholder(item))
}

export function applyLocalWatchlistToPayload(
  payload: TerminalPayload | null,
  localItems: LocalWatchlistEntry[],
): TerminalPayload | null {
  if (!payload) return payload
  const localCodes = watchlistCodes(localItems)
  const boardItems = payload.stock_board ? applyLocalWatchlistToBoardItems(payload.stock_board.items, localItems) : undefined
  const previewItems = watchlistPreviewFromPayload(payload, localItems)
  if (
    boardItems === payload.stock_board?.items
    && previewItems.length === (payload.watchlist_preview ?? []).length
    && previewItems.every((item, index) => item === payload.watchlist_preview[index])
    && localCodes.join(",") === (payload.watchlist_codes ?? []).join(",")
  ) {
    return payload
  }
  return {
    ...payload,
    stock_board: payload.stock_board && boardItems ? { ...payload.stock_board, items: boardItems } : payload.stock_board,
    watchlist: localItems,
    watchlist_preview: previewItems,
    watchlist_codes: localCodes,
  }
}
