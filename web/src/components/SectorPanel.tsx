import { memo, useState } from "react"
import { ChevronDown, Crown } from "lucide-react"
import type { SectorRank } from "@/types/api"
import { fmtPct, pctClass } from "@/lib/format"
import { HeatBar } from "@/components/widgets"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"

interface SectorPanelProps {
  sectors: SectorRank[]
  selected: string | null
  onSelect: (name: string | null) => void
  boardLevel: number
  onBoardLevel: (level: number) => void
}

// 板块口径菜单：1/2/3 = 申万行业级别；4/5/6 = 通达信概念(GN)/风格(FG)/地区(DQ)
const LEVEL_LABELS: Record<number, string> = {
  1: "板块 · 1级",
  2: "板块 · 2级",
  3: "板块 · 3级",
  4: "概念",
  5: "风格",
  6: "地区",
}

const PANEL_TITLES: Record<number, string> = {
  4: "概念强弱",
  5: "风格强弱",
  6: "地区强弱",
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
  const [menuOpen, setMenuOpen] = useState(false)
  // 口径 + 级别收成一个菜单：悬停或点击展开，避免头部 tab 越塞越多
  const currentLabel = LEVEL_LABELS[boardLevel] ?? `板块 · ${boardLevel}级`
  const pick = (level: number) => {
    if (level !== boardLevel) onBoardLevel(level)
    setMenuOpen(false)
  }
  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-2">
        <div>
          <div className="panel-title">板块扫描</div>
          <h2 className="text-sm font-bold">{PANEL_TITLES[boardLevel] ?? "板块强弱"}</h2>
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
          <div onMouseEnter={() => setMenuOpen(true)} onMouseLeave={() => setMenuOpen(false)}>
            <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen} modal={false}>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="flex h-6 items-center gap-0.5 rounded border border-input bg-secondary px-1.5 text-[10px] font-semibold text-foreground outline-none hover:bg-accent"
                  title="切换板块口径 / 级别"
                >
                  {currentLabel}
                  <ChevronDown className="h-3 w-3 text-muted-foreground" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" sideOffset={4} className="min-w-[120px]">
                <DropdownMenuLabel className="text-[10px] text-muted-foreground">官方板块（申万）</DropdownMenuLabel>
                {[1, 2, 3].map((lv) => (
                  <DropdownMenuItem
                    key={lv}
                    className={cn("text-[11px]", boardLevel === lv && "bg-accent font-semibold")}
                    onSelect={() => pick(lv)}
                  >
                    {LEVEL_LABELS[lv]}
                  </DropdownMenuItem>
                ))}
                <DropdownMenuSeparator />
                <DropdownMenuLabel className="text-[10px] text-muted-foreground">通达信</DropdownMenuLabel>
                {[4, 5, 6].map((lv) => (
                  <DropdownMenuItem
                    key={lv}
                    className={cn("text-[11px]", boardLevel === lv && "bg-accent font-semibold")}
                    onSelect={() => pick(lv)}
                  >
                    {LEVEL_LABELS[lv]}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
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
        {sorted.length === 0 && (
          <div className="p-4 text-center text-xs text-muted-foreground">
            暂无{PANEL_TITLES[boardLevel] ? PANEL_TITLES[boardLevel].replace("强弱", "") : "板块"}数据
          </div>
        )}
      </div>
    </section>
  )
}
