export type LiveChannelName =
  | "terminal"
  | "index_minutes"
  | "dark_pool"
  | "detail_chart"
  | "detail_overlay"
  | "detail_daily"
  | "dark_pool_stock"

export interface LiveChannelPayload {
  type: "snapshot" | "delta"
  seq: number
  data?: unknown
  sections?: unknown
}

export interface MarketPhaseMessage {
  type: "market_phase"
  market_session?: string
  traffic_mode?: string
  refresh_policy?: Record<string, unknown>
}

export interface LiveSocketStatus {
  connected: boolean
  error: string | null
  marketPhase: MarketPhaseMessage | null
}

interface ChannelEnvelope {
  type: "channel"
  channel: LiveChannelName
  message: LiveChannelPayload
}

interface Listener {
  id: number
  channel: LiveChannelName
  params: Record<string, unknown>
  onMessage: (message: LiveChannelPayload) => void
}

interface ActiveChannel {
  key: string
  params: Record<string, unknown>
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null"
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`
  const record = value as Record<string, unknown>
  const keys = Object.keys(record).sort()
  return `{${keys.map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`).join(",")}}`
}

class LiveSocketManager {
  private ws: WebSocket | null = null
  private listeners = new Map<number, Listener>()
  private activeChannels = new Map<LiveChannelName, ActiveChannel>()
  private statusListeners = new Set<(status: LiveSocketStatus) => void>()
  private nextId = 1
  private reconnectAttempts = 0
  private reconnectTimer: number | undefined
  private closed = false
  private status: LiveSocketStatus = {
    connected: false,
    error: null,
    marketPhase: null,
  }

  constructor() {
    if (typeof window !== "undefined") {
      window.addEventListener("beforeunload", () => this.close())
    }
  }

  subscribe(
    channel: LiveChannelName,
    params: Record<string, unknown>,
    onMessage: (message: LiveChannelPayload) => void,
  ): () => void {
    if (this.closed) this.closed = false
    const id = this.nextId++
    this.listeners.set(id, { id, channel, params, onMessage })
    this.connect()
    this.syncSubscriptions()

    return () => {
      this.listeners.delete(id)
      this.syncSubscriptions()
    }
  }

  onStatus(listener: (status: LiveSocketStatus) => void): () => void {
    this.statusListeners.add(listener)
    listener(this.status)
    return () => this.statusListeners.delete(listener)
  }

  refresh(channel: LiveChannelName): void {
    this.send({ type: "refresh", channel })
  }

  private connect(): void {
    if (this.ws || this.closed) return
    const protocol = location.protocol === "https:" ? "wss:" : "ws:"
    const ws = new WebSocket(`${protocol}//${location.host}/ws/live`)
    this.ws = ws

    ws.onopen = () => {
      this.reconnectAttempts = 0
      this.setStatus({ connected: true, error: null, marketPhase: this.status.marketPhase })
      this.syncSubscriptions(true)
    }
    ws.onmessage = (event) => this.handleMessage(event.data)
    ws.onerror = () => {
      this.setStatus({ connected: false, error: "WebSocket 连接异常", marketPhase: this.status.marketPhase })
    }
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null
      this.setStatus({ connected: false, error: this.status.error, marketPhase: this.status.marketPhase })
      if (!this.closed) this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== undefined) window.clearTimeout(this.reconnectTimer)
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 10000)
    this.reconnectAttempts += 1
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = undefined
      this.connect()
    }, delay)
  }

  private handleMessage(raw: unknown): void {
    let parsed: unknown
    try {
      parsed = JSON.parse(String(raw))
    } catch {
      return
    }
    if (typeof parsed !== "object" || parsed === null) return
    const message = parsed as Record<string, unknown>
    if (message.type === "market_phase") {
      this.setStatus({
        connected: this.status.connected,
        error: this.status.error,
        marketPhase: message as unknown as MarketPhaseMessage,
      })
      return
    }
    if (message.type !== "channel" || typeof message.channel !== "string") return
    const envelope = message as unknown as ChannelEnvelope

    for (const listener of this.listeners.values()) {
      if (listener.channel === envelope.channel) listener.onMessage(envelope.message)
    }
  }

  private syncSubscriptions(force = false): void {
    const next = new Map<LiveChannelName, ActiveChannel>()
    for (const listener of this.listeners.values()) {
      next.set(listener.channel, {
        key: stableStringify(listener.params),
        params: listener.params,
      })
    }

    for (const [channel, entry] of next) {
      const current = this.activeChannels.get(channel)
      if (!force && current?.key === entry.key) continue
      this.send({ type: "subscribe", channel, params: entry.params })
    }
    for (const channel of this.activeChannels.keys()) {
      if (!next.has(channel)) this.send({ type: "unsubscribe", channel })
    }
    this.activeChannels = next
  }

  private send(message: Record<string, unknown>): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    try {
      this.ws.send(JSON.stringify(message))
    } catch {
      // The close handler owns reconnect scheduling; a failed send is transient.
    }
  }

  private setStatus(status: LiveSocketStatus): void {
    this.status = status
    for (const listener of this.statusListeners) listener(status)
  }

  private close(): void {
    this.closed = true
    if (this.reconnectTimer !== undefined) window.clearTimeout(this.reconnectTimer)
    this.ws?.close()
    this.ws = null
  }
}

export const liveSocket = new LiveSocketManager()
