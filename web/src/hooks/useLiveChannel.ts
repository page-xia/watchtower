import { useCallback, useEffect, useState } from "react"
import {
  liveSocket,
  type LiveChannelName,
  type LiveChannelPayload,
  type LiveSocketStatus,
} from "@/lib/liveSocket"

export interface LiveChannelState<T> {
  data: T | null
  connected: boolean
  error: string | null
  loading: boolean
  lastMessageAt: number | null
  refresh: () => void
}

export function useLiveStatus(): LiveSocketStatus {
  const [status, setStatus] = useState<LiveSocketStatus>({
    connected: false,
    error: null,
    marketPhase: null,
  })

  useEffect(() => liveSocket.onStatus(setStatus), [])
  return status
}

export function useLiveChannel<T>(
  channel: LiveChannelName,
  params: Record<string, unknown>,
): LiveChannelState<T> {
  const [data, setData] = useState<T | null>(null)
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null)
  const status = useLiveStatus()
  const paramsKey = JSON.stringify(params)

  useEffect(() => {
    const handleMessage = (message: LiveChannelPayload) => {
      if (message.type !== "snapshot" && message.type !== "delta") return
      if (message.data === undefined) return
      setData(message.data as T)
      setLastMessageAt(Date.now())
    }

    const parsedParams = JSON.parse(paramsKey) as Record<string, unknown>
    return liveSocket.subscribe(channel, parsedParams, handleMessage)
  }, [channel, paramsKey])

  const refresh = useCallback(() => liveSocket.refresh(channel), [channel])

  return {
    data,
    connected: status.connected,
    error: status.error,
    loading: data === null && status.connected,
    lastMessageAt,
    refresh,
  }
}
