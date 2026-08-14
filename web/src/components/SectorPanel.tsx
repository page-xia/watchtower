import { memo } from "react"
import { Crown } from "lucide-react"
import type { SectorRank } from "@/types/api"
import { fmtPct, pctClass } from "@/lib/format"
import { HeatBar } from "@/components/widgets"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

interface SectorPanelProps {
  sectors: SectorRank[]
  selected: string | null
  onSelect: (name: string | null) => void
  boardLevel: number
  onBoardLevel: (level: number) => void
}

const SectorRow = memo(function SectorRow({
  sector,
  active,
  onClick,
}: {
  sector: SectorRank
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "group w-full rounded-md border border-transparent px-2 py-1.5 text-left transition-colors hover:border-border hover:bg-accent/50",
        active && "border-primary/50 bg-primary/10",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className={cn("num w-6 shrink-0 text-right text-[11px] font-bold", sector.heat_score >= 80 ? "text-up" : sector.heat_score >= 60 ? "text-gold" : "text-muted-foreground")}>
            {sector.heat_score}
          </span>
          <span className="truncate text-[12px] font-medium">{sector.name}</span>
          {sector.core_attack && <Crown className="h-3 w-3 shrink-0 text-gold" />}
        </div>
        <span className={cn("num shrink-0 text-[12px] font-bold", pctClass(sector.avg_change_pct))}>
          {fmtPct(sector.avg_change_pct)}
        </span>
      </div>
      <div className="mt-1 flex items-center gap-2">
        <HeatBar score={sector.heat_score} className="flex-1" />
        <span className="num shrink-0 text-[10px] text-muted-foreground">
          {sector.up_count}/{sector.total_count}
        </span>
      </div>
      <div className="mt-0.5 flex items-center justify-between text-[10px] text-muted-foreground/80">
        <span className="truncate">
          {sector.leader_name ? `龙头 ${sector.leader_name}` : (sector.reasons ?? [])[0] ?? ""}
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {sector.limit_up_count > 0 && <span className="num text-up">涨停{sector.limit_up_count}</span>}
          <span
            className={cn("num font-semibold", pctClass(sector.flow_delta))}
            title="方向加权成交额（净流入代理，单位亿）：共振看金额，不看百分比"
          >
            {sector.flow_delta > 0 ? "+" : ""}{sector.flow_delta?.toFixed(1)}亿
          </span>
        </span>
      </div>
    </button>
  )
})

export function SectorPanel({ sectors, selected, onSelect, boardLevel, onBoardLevel }: SectorPanelProps) {
  const sorted = [...sectors].sort((a, b) => b.heat_score - a.heat_score)
  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-2">
        <div>
          <div className="panel-title">板块扫描</div>
          <h2 className="text-sm font-bold">板块强弱</h2>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className={cn(
              "h-6 w-6 px-0 text-[10px] font-semibold",
              selected ? "text-muted-foreground hover:bg-muted" : "bg-primary/20 text-primary",
            )}
            title="切回全市场"
            aria-label="切回全市场"
            aria-pressed={!selected}
            onClick={() => onSelect(null)}
          >
            全
          </Button>
          {[1, 2, 3].map((lv) => (
            <button
              key={lv}
              type="button"
              onClick={() => onBoardLevel(lv)}
              className={cn(
                "rounded px-1.5 py-0.5 text-[10px]",
                boardLevel === lv ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-muted",
              )}
            >
              {lv}级
            </button>
          ))}
        </div>
      </header>
      <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-1.5">
        {sorted.map((s) => (
          <SectorRow
            key={s.name}
            sector={s}
            active={selected === s.name}
            onClick={() => onSelect(selected === s.name ? null : s.name)}
          />
        ))}
        {sorted.length === 0 && <div className="p-4 text-center text-xs text-muted-foreground">暂无板块数据</div>}
      </div>
    </section>
  )
}
