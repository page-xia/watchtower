import { useCallback, useEffect, useRef, useState } from "react"
import type { BoardItem, StockBoard, TerminalPayload } from "@/types/api"
import { liveSocket, type LiveChannelPayload } from "@/lib/liveSocket"
import { useLiveStatus } from "@/hooks/useLiveChannel"
import { getClientId } from "@/lib/clientIdentity"

export interface TerminalStreamParams {
  sector?: string | null
  boardLevel?: number
  sort?: string
  page?: number
  pageSize?: number
  nearTrend?: boolean
  pinBuy?: boolean
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

/**
 * 终端数据流：全页共用一条持久 /ws/live 连接。
 * 连接后先收全量快照，之后只收变化分区（榜单按 code upsert/remove/order）。
 * 板块/排序/翻页等交互只发送 subscribe 控制消息，服务端切换 StreamHub 频道，
 * 不关闭浏览器 WebSocket；真实网络断开才由管理器指数退避重连。
 */
export function useTerminalStream(params: TerminalStreamParams): TerminalStreamState {
  const [data, setData] = useState<TerminalPayload | null>(null)
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null)
  const dataRef = useRef<TerminalPayload | null>(null)
  const status = useLiveStatus()
  const paramsKey = JSON.stringify(params)

  const refresh = useCallback(() => liveSocket.refresh("terminal"), [])

  useEffect(() => {
    const handleMessage = (message: LiveChannelPayload) => {
      if (message.type === "snapshot" && message.data) {
        dataRef.current = message.data as TerminalPayload
        setData(message.data as TerminalPayload)
        setLastMessageAt(Date.now())
      } else if (message.type === "delta" && message.sections && dataRef.current) {
        const merged = applyDelta(
          dataRef.current,
          message.sections as NonNullable<DeltaMessage["sections"]>,
        )
        dataRef.current = merged
        setData(merged)
        setLastMessageAt(Date.now())
      }
    }

    const streamParams = { ...(JSON.parse(paramsKey) as TerminalStreamParams), client_id: getClientId() }
    return liveSocket.subscribe(
      "terminal",
      streamParams as unknown as Record<string, unknown>,
      handleMessage,
    )
  }, [paramsKey])

  return { data, connected: status.connected, error: status.error, lastMessageAt, refresh }
}
