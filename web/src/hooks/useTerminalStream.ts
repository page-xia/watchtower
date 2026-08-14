import { useCallback, useEffect, useRef, useState } from "react"
import type { BoardItem, StockBoard, TerminalPayload } from "@/types/api"

export interface TerminalStreamParams {
  sector?: string | null
  boardLevel?: number
  sort?: string
  page?: number
  pageSize?: number
  watchlistCodes?: string[]
}

export interface TerminalStreamState {
  data: TerminalPayload | null
  connected: boolean
  error: string | null
  lastMessageAt: number | null
  refresh: () => void
}

interface BoardDelta {
  meta?: Partial<StockBoard>
  upsert?: BoardItem[]
  remove?: string[]
  order?: string[]
}

interface DeltaMessage {
  type: "snapshot" | "delta"
  seq: number
  data?: TerminalPayload
  sections?: Record<string, unknown> & { board?: BoardDelta }
}

const REPLACE_SECTIONS = [
  "market",
  "sectors",
  "sector_flow",
  "watchlist",
  "watchlist_preview",
  "positions_preview",
  "opening_markers",
] as const

function applyDelta(prev: TerminalPayload, sections: NonNullable<DeltaMessage["sections"]>): TerminalPayload {
  const next = { ...prev }
  for (const key of REPLACE_SECTIONS) {
    const value = sections[key]
    if (value !== undefined) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ;(next as any)[key] = value
    }
  }
  const meta = sections.meta as Partial<TerminalPayload> | undefined
  if (meta) Object.assign(next, meta)

  const boardDelta = sections.board
  if (boardDelta) {
    const prevBoard = prev.stock_board
    const board: StockBoard = { ...prevBoard, ...(boardDelta.meta ?? {}) }
    const map = new Map<string, BoardItem>(prevBoard.items.map((item) => [item.code, item]))
    for (const item of boardDelta.upsert ?? []) map.set(item.code, item)
    for (const code of boardDelta.remove ?? []) map.delete(code)
    const order = boardDelta.order ?? prevBoard.items.map((item) => item.code)
    const items: BoardItem[] = []
    for (const code of order) {
      const item = map.get(code)
      if (item) items.push(item)
    }
    board.items = items
    next.stock_board = board
  }
  return next
}

function buildStreamUrl(params: TerminalStreamParams): string {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:"
  const q = new URLSearchParams()
  q.set("view", "terminal")
  q.set("format", "delta")
  q.set("board_level", String(params.boardLevel ?? 3))
  q.set("sort", params.sort ?? "activity")
  q.set("page", String(params.page ?? 1))
  q.set("page_size", String(params.pageSize ?? 40))
  if (params.sector) q.set("sector", params.sector)
  q.set("watchlist_codes", (params.watchlistCodes ?? []).join(","))
  return `${protocol}//${location.host}/ws/stream?${q.toString()}`
}

/**
 * 终端数据流：连接时收一次全量快照，之后只收变化分区（榜单按 code 增量）。
 * 未变化的榜单行保持对象引用不变，配合 React.memo 避免整表重绘。
 * 参数变化（板块/排序/翻页）时重连并重新拿快照；断线指数退避重连。
 */
export function useTerminalStream(params: TerminalStreamParams): TerminalStreamState {
  const [data, setData] = useState<TerminalPayload | null>(null)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null)
  const [nonce, setNonce] = useState(0)
  const dataRef = useRef<TerminalPayload | null>(null)
  dataRef.current = data

  const refresh = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let retry = 0
    let retryTimer: number | undefined

    const connect = () => {
      if (closed) return
      ws = new WebSocket(buildStreamUrl(params))
      ws.onopen = () => {
        retry = 0
        setConnected(true)
        setError(null)
      }
      ws.onmessage = (event) => {
        let message: DeltaMessage
        try {
          message = JSON.parse(event.data as string) as DeltaMessage
        } catch {
          return
        }
        if (message.type === "snapshot" && message.data) {
          dataRef.current = message.data
          setData(message.data)
          setLastMessageAt(Date.now())
        } else if (message.type === "delta" && message.sections && dataRef.current) {
          const merged = applyDelta(dataRef.current, message.sections)
          dataRef.current = merged
          setData(merged)
          setLastMessageAt(Date.now())
        }
      }
      ws.onerror = () => {
        setError("WebSocket 连接异常")
      }
      ws.onclose = () => {
        setConnected(false)
        if (closed) return
        const delay = Math.min(1000 * 2 ** retry, 10000)
        retry += 1
        retryTimer = window.setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      closed = true
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
      ws?.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.sector, params.boardLevel, params.sort, params.page, params.pageSize, params.watchlistCodes?.join(","), nonce])

  return { data, connected, error, lastMessageAt, refresh }
}
