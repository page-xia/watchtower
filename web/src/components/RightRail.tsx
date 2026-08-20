import { useMemo, useState } from "react"
import { Activity, Star } from "lucide-react"
import type { BoardItem } from "@/types/api"
import { signalTone } from "@/lib/format"
import { RailStockRow, RAIL_COLUMNS } from "@/components/boardCells"
import { cn } from "@/lib/utils"

type RailTab = "queue" | "watch"
type QueueTab = "buy" | "watch" | "sell"

interface RightRailProps {
  items: BoardItem[]
  watchlist: BoardItem[]
  onOpenDetail: (code: string) => void
  onRemoveWatch: (code: string) => void
}

function RailTable({ rows, emptyText, columns }: { rows: React.ReactNode[]; emptyText: string; columns?: typeof RAIL_COLUMNS }) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto overflow-x-auto">
      <table className="w-full min-w-[620px] border-collapse">
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

/** 机会队列：买T/观察/减T卖T 按信号引擎信号侧分组 */
function QueueList({ items, onOpenDetail }: { items: BoardItem[]; onOpenDetail: (code: string) => void }) {
  const [tab, setTab] = useState<QueueTab>("buy")

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

  const rows: React.ReactNode[] = groups[tab].map((it) => (
    <RailStockRow key={it.code} item={it} onOpen={() => onOpenDetail(it.code)} />
  ))

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 gap-1 border-b border-border p-1.5">
        {(["buy", "watch", "sell"] as const).map((k) => {
          const label = k === "buy" ? "买T" : k === "sell" ? "减T/卖T" : "观察"
          const count = groups[k].length
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

/** 自选：与机会队列同一套行组件，行尾悬停显示移除按钮 */
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

export function RightRail({ items, watchlist, onOpenDetail, onRemoveWatch }: RightRailProps) {
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
      {tab === "queue" && <QueueList items={items} onOpenDetail={onOpenDetail} />}
      {tab === "watch" && <WatchList items={watchlist} onOpenDetail={onOpenDetail} onRemove={onRemoveWatch} />}
    </section>
  )
}
