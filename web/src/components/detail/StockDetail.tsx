import { useEffect, useState } from "react"
import { X, Star, Sparkles } from "lucide-react"
import { getDetailExtras, getSignalChart, getSignalOverlay, runAiAnalysis } from "@/lib/api"
import { usePolling } from "@/hooks/usePolling"
import { fmtPct, fmtPrice, pctClass } from "@/lib/format"
import { SignalBadge } from "@/components/widgets"
import { Button } from "@/components/ui/button"
import { MinuteChart } from "@/components/detail/MinuteChart"
import { TapePanel } from "@/components/detail/TapePanel"
import { ConfluenceCard, FormulaCard, RiskRewardCard } from "@/components/detail/DetailSide"
import { DetailTabs } from "@/components/detail/DetailTabs"
import { cn } from "@/lib/utils"
import type { DetailExtrasResponse } from "@/types/api"

interface StockDetailProps {
  code: string
  onClose: () => void
  onToggleWatch: (code: string, name: string, watchlisted: boolean) => void
  watchlisted: boolean
  watchlistCodes: string[]
}

export function StockDetail({ code, onClose, onToggleWatch, watchlisted, watchlistCodes }: StockDetailProps) {
  const watchlistKey = watchlistCodes.join(",")
  const chartState = usePolling(() => getSignalChart(code, watchlistCodes), 10000, [code, watchlistKey])
  const overlayState = usePolling(() => getSignalOverlay(code, watchlistCodes), 10000, [code, watchlistKey])
  const coreExtrasState = usePolling(
    () =>
      getDetailExtras(code, watchlistCodes, {
        includeAuctionHistory: false,
        includeCapitalFlow: false,
        includeFundamentals: false,
        includeIndicators: false,
        includeChanlun: false,
      }),
    0,
    [code, watchlistKey],
  )
  const [richExtras, setRichExtras] = useState<DetailExtrasResponse | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  useEffect(() => {
    setRichExtras(null)
  }, [code, watchlistKey])

  useEffect(() => {
    if (!coreExtrasState.data) return
    let cancelled = false
    const timer = window.setTimeout(() => {
      void getDetailExtras(code, watchlistCodes)
        .then((payload) => {
          if (!cancelled) setRichExtras(payload)
        })
        .catch(() => {
          if (!cancelled) setRichExtras(null)
        })
    }, 120)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [code, watchlistKey, coreExtrasState.lastOkAt])

  const detail = chartState.data
  const overlay = overlayState.data
  const extras = richExtras ?? coreExtrasState.data
  const signal = detail?.current_signal
  const name = detail?.name ?? code

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />
      <div className="terminal-panel relative flex h-[94vh] w-[min(1560px,97vw)] flex-col overflow-hidden shadow-2xl">
        {/* 头部 */}
        <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-2.5">
          <div className="flex items-baseline gap-2">
            <h2 className="text-lg font-bold">{name}</h2>
            <span className="num text-xs text-muted-foreground">{code}</span>
            {detail?.sector && (
              <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">{detail.sector_snapshot?.name ?? detail.sector}</span>
            )}
          </div>
          <div className="flex items-baseline gap-2">
            <span className={cn("num text-2xl font-bold", pctClass(signal?.change_pct))}>
              {fmtPrice(signal?.price)}
            </span>
            <span className={cn("num text-sm font-bold", pctClass(signal?.change_pct))}>
              {fmtPct(signal?.change_pct)}
            </span>
          </div>
          {signal && <SignalBadge signal={signal.signal} score={signal.score} className="text-xs" />}
          {(signal?.watchlist_tags ?? []).map((t) => (
            <span key={t} className="rounded bg-[hsl(var(--gold)/0.15)] px-1.5 py-0.5 text-[10px] text-gold">{t}</span>
          ))}
          <span className="text-[10px] text-muted-foreground">
            {detail?.trade_date} · 更新 {signal?.updated_at || "--"}
          </span>
          {signal?.executable === false && signal.execution_reason && (
            <span className="max-w-[280px] truncate text-[10px] text-muted-foreground/70" title={signal.execution_reason}>
              {signal.execution_reason}
            </span>
          )}
          <div className="flex-1" />
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1 text-xs"
            onClick={() =>
              void runAiAnalysis(code)
                .then(() => {
                  setRichExtras(null)
                  coreExtrasState.refresh()
                })
                .catch(() => undefined)
            }
            title="调用后端 AI 接口生成分析"
          >
            <Sparkles className="h-3 w-3" /> AI分析
          </Button>
          <Button
            variant="outline"
            size="sm"
            className={cn("h-7 gap-1 text-xs", watchlisted && "border-[hsl(var(--gold)/0.5)] text-gold")}
            onClick={() => onToggleWatch(code, name, watchlisted)}
          >
            <Star className={cn("h-3 w-3", watchlisted && "fill-gold")} />
            {watchlisted ? "已自选" : "加自选"}
          </Button>
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </header>

        {/* 主体：左侧图表区（分时图缩小+逐笔下移），右侧星球/扩展 tabs 全高加宽 */}
        <div className="grid min-h-0 flex-1 grid-cols-12 gap-2 p-2">
          {/* 左列：分时图 → 公式卡片 → 逐笔成交 */}
          <div className="col-span-12 flex min-h-0 flex-col gap-2 lg:col-span-7">
            <div className="terminal-panel min-h-0 flex-[5]">
              {chartState.error ? (
                <div className="flex h-full items-center justify-center text-xs text-destructive">{chartState.error}</div>
              ) : (
                <MinuteChart
                  chart={detail?.chart ?? null}
                  markers={overlay?.markers ?? []}
                  openingMarkers={overlay?.opening_markers ?? []}
                />
              )}
            </div>

            {/* 公式 / 共振 / 盈亏比 / 原因 */}
            <div className="grid h-[128px] shrink-0 grid-cols-2 gap-2 xl:grid-cols-4">
              <FormulaCard formula={overlay?.formula_state ?? detail?.formula_state ?? null} />
              <ConfluenceCard confluence={overlay?.confluence_snapshot ?? detail?.confluence_snapshot ?? null} />
              <RiskRewardCard rr={signal?.risk_reward} />
              <div className="terminal-panel min-h-0 overflow-y-auto p-2.5">
                <div className="panel-title mb-1.5">信号依据 / 风险</div>
                <div className="space-y-1">
                  {(signal?.reasons ?? []).slice(0, 4).map((r, i) => (
                    <div key={i} className="text-[10px] leading-snug text-foreground/80">· {r}</div>
                  ))}
                  {(signal?.risks ?? []).slice(0, 3).map((r, i) => (
                    <div key={i} className="text-[10px] leading-snug text-gold/80">⚠ {r}</div>
                  ))}
                  {detail?.research_note && (
                    <div className="mt-1 border-t border-border/60 pt-1 text-[9px] text-muted-foreground/70">
                      {detail.research_note}
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* 逐笔成交 · L1（搬到分时图下方） */}
            <div className="terminal-panel min-h-0 flex-[3]">
              <div className="flex h-full min-h-0 flex-col">
                <div className="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
                  <span className="panel-title">逐笔成交 · L1</span>
                  <span className="num text-[9px] text-muted-foreground">
                    {overlay?.transaction_flow?.as_of ? `截至 ${overlay.transaction_flow.as_of}` : ""}
                  </span>
                </div>
                <div className="min-h-0 flex-1">
                  <TapePanel flow={overlay?.transaction_flow ?? null} />
                </div>
              </div>
            </div>
          </div>

          {/* 右列：星球消息 / AI / 竞价 / 资金流 / F10 / 缠论（全高加宽） */}
          <div className="col-span-12 min-h-0 max-lg:h-[480px] lg:col-span-5">
            <DetailTabs extras={extras} error={coreExtrasState.error} />
          </div>
        </div>
      </div>
    </div>
  )
}
