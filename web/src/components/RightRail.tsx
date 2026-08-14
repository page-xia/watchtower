import { useEffect, useMemo, useRef, useState } from "react"
import { Activity, Star } from "lucide-react"
import type { BoardItem, OpeningMarkerEvent } from "@/types/api"
import { signalTone } from "@/lib/format"
import { RailStockRow, RAIL_COLUMNS } from "@/components/boardCells"
import { cn } from "@/lib/utils"

type RailTab = "queue" | "watch"
type QueueTab = "buy" | "watch" | "sell"

interface RightRailProps {
  items: BoardItem[]
  watchlist: BoardItem[]
  openingMarkers: OpeningMarkerEvent[]
  onOpenDetail: (code: string) => void
  onRemoveWatch: (code: string) => void
}

/** 榜单外的独立菱形：行显示用实时行情（live_* 回填），无实时数据时退回信号时刻快照 */
function itemFromMarker(m: OpeningMarkerEvent): BoardItem {
  return {
    code: m.code,
    name: m.name,
    sector: m.sector,
    price: m.live_price ?? m.price,
    change_pct: m.live_change_pct ?? m.change_pct,
    amount: m.live_amount,
    signal_time: m.time,
  } as BoardItem
}

function RailTable({ rows, emptyText, columns }: { rows: React.ReactNode[]; emptyText: string; columns?: typeof RAIL_COLUMNS }) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto overflow-x-auto">
      <table className="w-full min-w-[560px] border-collapse">
        <thead className="sticky top-0 z-10 bg-card">
          <tr className="border-b border-border text-left text-[10px] text-muted-foreground">
            {(columns ?? RAIL_COLUMNS).map((c, i) => (
              <th key={c.label || `col-${i}`} className={cn("font-normal", c.className)}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      {rows.length === 0 && <div className="p-6 text-center text-[11px] text-muted-foreground">{emptyText}</div>}
    </div>
  )
}

function QueueList({ items, openingMarkers, onOpenDetail }: { items: BoardItem[]; openingMarkers: OpeningMarkerEvent[]; onOpenDetail: (code: string) => void }) {
  const [tab, setTab] = useState<QueueTab>("buy")
  const knownIds = useRef<Set<string>>(new Set())
  const [freshIds, setFreshIds] = useState<Set<string>>(new Set())

  // 新出现的菱形短暂高亮
  useEffect(() => {
    const fresh = new Set<string>()
    for (const m of openingMarkers) {
      if (!knownIds.current.has(m.id)) fresh.add(m.id)
    }
    knownIds.current = new Set(openingMarkers.map((m) => m.id))
    if (fresh.size === 0) return
    setFreshIds((prev) => new Set([...prev, ...fresh]))
    const timer = window.setTimeout(() => {
      setFreshIds((prev) => {
        const next = new Set(prev)
        for (const id of fresh) next.delete(id)
        return next
      })
    }, 2600)
    return () => window.clearTimeout(timer)
  }, [openingMarkers])

  // 服务端推当天全量菱形（按 code+rule 去重后有界）；每个 code 保留最早触发的
  // 买点——越早可信度越高，展示的时间就是首次触发时间
  const markerByCode = useMemo(() => {
    const map = new Map<string, OpeningMarkerEvent>()
    for (const m of openingMarkers) {
      const prev = map.get(m.code)
      if (!prev || m.first_seen < prev.first_seen) map.set(m.code, m)
    }
    return map
  }, [openingMarkers])

  const groups = useMemo(() => {
    const buy: BoardItem[] = []
    const watch: BoardItem[] = []
    const sell: BoardItem[] = []
    for (const it of items) {
      const tone = signalTone(it.signal)
      if (tone === "buy") buy.push(it)
      else if (tone === "sell") sell.push(it)
      else watch.push(it)
    }
    return { buy, watch, sell }
  }, [items])

  // 榜单页之外的独立菱形，按侧归入买T/卖T，最早触发在前（越靠后可信度越差）
  const standalone = useMemo(() => {
    const codes = new Set(items.map((it) => it.code))
    const buy: OpeningMarkerEvent[] = []
    const sell: OpeningMarkerEvent[] = []
    const seenCodes = new Set<string>()
    const sorted = [...openingMarkers].sort((a, b) => a.first_seen.localeCompare(b.first_seen))
    for (const m of sorted) {
      if (codes.has(m.code) || seenCodes.has(m.code)) continue
      seenCodes.add(m.code)
      if (m.side === "buy") buy.push(m)
      else sell.push(m)
    }
    return { buy, sell }
  }, [openingMarkers, items])

  const groupCount = (k: QueueTab) => {
    if (k === "buy") return groups.buy.length + standalone.buy.length
    if (k === "sell") return groups.sell.length + standalone.sell.length
    return groups.watch.length
  }

  const rows: React.ReactNode[] = []
  if (tab !== "watch") {
    for (const m of standalone[tab]) {
      rows.push(
        <RailStockRow
          key={m.id}
          item={itemFromMarker(m)}
          marker={m}
          fresh={freshIds.has(m.id)}
          onOpen={() => onOpenDetail(m.code)}
        />,
      )
    }
  }
  for (const it of groups[tab]) {
    const marker = tab === "watch" ? undefined : markerByCode.get(it.code)
    const matched = marker && marker.side === tab ? marker : undefined
    rows.push(
      <RailStockRow
        key={it.code}
        item={it}
        marker={matched}
        fresh={matched ? freshIds.has(matched.id) : false}
        onOpen={() => onOpenDetail(it.code)}
      />,
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 gap-1 border-b border-border p-1.5">
        {(["buy", "watch", "sell"] as const).map((k) => {
          const label = k === "buy" ? "买T" : k === "sell" ? "减T/卖T" : "观察"
          const count = groupCount(k)
          const color = k === "buy" ? "text-up" : k === "sell" ? "text-down" : "text-gold"
          return (
            <button
              key={k}
              type="button"
              onClick={() => setTab(k)}
              className={cn(
                "flex flex-1 items-center justify-center gap-1 rounded px-2 py-1 text-[11px] font-semibold transition-colors",
                tab === k ? "bg-accent" : "text-muted-foreground hover:bg-muted",
              )}
            >
              <span className={color}>{label}</span>
              <span className={cn("num rounded bg-muted px-1 text-[10px]", color)}>{count}</span>
            </button>
          )
        })}
      </div>
      <RailTable
        rows={rows}
        emptyText={`当前页暂无${tab === "buy" ? "买T" : tab === "sell" ? "减T/卖T" : "观察"}标的`}
      />
    </div>
  )
}

/** 自选：与机会队列同一套行组件，不带菱形标记，行尾悬停显示移除按钮 */
function WatchList({ items, onOpenDetail, onRemove }: { items: BoardItem[]; onOpenDetail: (code: string) => void; onRemove: (code: string) => void }) {
  return (
    <RailTable
      columns={[...RAIL_COLUMNS, { label: "", className: "w-[28px] pl-1 pr-1.5" }]}
      rows={items.map((it) => (
        <RailStockRow key={it.code} item={it} onOpen={() => onOpenDetail(it.code)} onRemove={() => onRemove(it.code)} />
      ))}
      emptyText="暂无自选，点榜单行首 ★ 添加"
    />
  )
}

export function RightRail({ items, watchlist, openingMarkers, onOpenDetail, onRemoveWatch }: RightRailProps) {
  const [tab, setTab] = useState<RailTab>("watch")
  const tabs: { key: RailTab; label: string; icon: React.ReactNode; count?: number }[] = [
    { key: "queue", label: "机会队列", icon: <Activity className="h-3 w-3" /> },
    { key: "watch", label: "自选", icon: <Star className="h-3 w-3" />, count: watchlist.length },
  ]
  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center gap-1 border-b border-border px-2 py-1.5">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={cn(
              "flex items-center gap-1 rounded px-2 py-1 text-[11px] font-semibold transition-colors",
              tab === t.key ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-muted",
            )}
          >
            {t.icon}
            {t.label}
            {typeof t.count === "number" && t.count > 0 && (
              <span className="num rounded bg-muted px-1 text-[9px]">{t.count}</span>
            )}
          </button>
        ))}
      </header>
      {tab === "queue" && <QueueList items={items} openingMarkers={openingMarkers} onOpenDetail={onOpenDetail} />}
      {tab === "watch" && <WatchList items={watchlist} onOpenDetail={onOpenDetail} onRemove={onRemoveWatch} />}
    </section>
  )
}
