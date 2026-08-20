import { useCallback, useEffect, useMemo, useState } from "react"
import { X, Star, Sparkles } from "lucide-react"
import { getDetailExtras, runAiAnalysis } from "@/lib/api"
import { useLiveChannel } from "@/hooks/useLiveChannel"
import { usePolling } from "@/hooks/usePolling"
import { fmtPct, fmtPrice, pctClass } from "@/lib/format"
import { SignalBadge } from "@/components/widgets"
import { Button } from "@/components/ui/button"
import { MinuteChart } from "@/components/detail/MinuteChart"
import { DailyKChart, type DailySubView } from "@/components/detail/DailyKChart"
import { TapePanel } from "@/components/detail/TapePanel"
import { ConfluenceCard, FormulaCard, RiskRewardCard } from "@/components/detail/DetailSide"
import { DetailTabs } from "@/components/detail/DetailTabs"
import { cn } from "@/lib/utils"
import type { DailyDetailResponse, DailyMainFormula, SignalChartResponse, SignalOverlayResponse, StockTags } from "@/types/api"

interface StockDetailProps {
  code: string
  onClose: () => void
  onToggleWatch: (code: string, name: string, watchlisted: boolean) => void
  watchlisted: boolean
  watchlistCodes: string[]
}

export function StockDetail({ code, onClose, onToggleWatch, watchlisted, watchlistCodes }: StockDetailProps) {
  const watchlistKey = watchlistCodes.join(",")
  const chartState = useLiveChannel<SignalChartResponse>("detail_chart", { code, watchlistCodes })
  const overlayState = useLiveChannel<SignalOverlayResponse>("detail_overlay", { code, watchlistCodes })
  const coreExtrasState = usePolling(
    () =>
      getDetailExtras(code, watchlistCodes, {
        includeAuctionHistory: false,
        includeCapitalFlow: false,
        includeFundamentals: false,
        includeIndicators: false,
        includeChanlun: false,
        includeMessages: false,
      }),
    0,
    [code, watchlistKey],
  )
  // 分时 / 日K 主图视图 + 日K副图选择；日K载荷（公式/筹码/题材标签）走 30s WS 频道
  const [chartView, setChartView] = useState<"minute" | "daily">("minute")
  const [subView, setSubView] = useState<DailySubView>("resonance")
  // 日K向前拖动动态加载：count 240 → 480 → 720 → 800（后端上限 800）
  const [dailyCount, setDailyCount] = useState(240)
  const dailyState = useLiveChannel<DailyDetailResponse>("detail_daily", { code, count: dailyCount })
  const dailyLoadingMore = dailyState.loading && dailyState.data != null
  const handleNeedMoreHistory = useCallback(() => {
    setDailyCount((c) => (c >= 800 ? c : Math.min(800, c + 240)))
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  const detail = chartState.data
  const overlay = overlayState.data
  // 只加载轻量核心 extras（星球消息 + AI 分析）；竞价/资金流/指标/缠论等重载荷
  // 由 DetailTabs 在切换到对应 tab 时按需懒加载。
  const extras = coreExtrasState.data
  const signal = detail?.current_signal
  const name = detail?.name ?? code
  const daily = dailyState.data ?? null
  const mainFormula = daily?.formulas?.main
  const trendLatest = mainFormula?.trend_latest ?? null
  // 低吸区间：短期/长期线低者为底（-1%）、高者为顶（+3%）；现价落区间内时买点改金色圆点。
  // 分时图辅助线只传原始线值，由 MinuteChart 按「现价贴近 ±3% 才显示」决定是否绘制。
  const currentPrice = signal?.price ?? 0
  const trendLines = useMemo(() => {
    if (!trendLatest) return null
    const short = trendLatest.zx_trend ?? null
    const long = trendLatest.zx_duokong ?? null
    if (!(short != null && short > 0) && !(long != null && long > 0)) return null
    return { short, long }
  }, [trendLatest])
  const nearLine = useMemo(() => {
    if (!trendLines || !(currentPrice > 0)) return false
    const lines = [trendLines.short, trendLines.long].filter((v): v is number => v != null && v > 0)
    return Math.min(...lines) * 0.99 <= currentPrice && currentPrice <= Math.max(...lines) * 1.03
  }, [trendLines, currentPrice])

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
                .then(() => coreExtrasState.refresh())
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
            <div className="terminal-panel flex min-h-0 flex-[5] flex-col">
              {/* 图表工具条：左侧 分时/日K 切换 + 日K副图选择，右侧 题材/概念/板块标签 */}
              <div className="flex shrink-0 items-center gap-2 border-b border-border px-2 py-1">
                <div className="inline-flex shrink-0 rounded-md border border-border bg-muted/40 p-0.5">
                  {([
                    { key: "minute", label: "分时" },
                    { key: "daily", label: "日K" },
                  ] as const).map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      onClick={() => setChartView(item.key)}
                      className={cn(
                        "whitespace-nowrap rounded px-3 py-0.5 text-[11px] font-semibold transition-colors",
                        chartView === item.key ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-background/70",
                      )}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
                {chartView === "daily" && (
                  <div className="inline-flex shrink-0 rounded-md border border-border bg-muted/40 p-0.5">
                    {([
                      { key: "resonance", label: "双共振" },
                      { key: "trend", label: "主力动向" },
                      { key: "trendline", label: "趋势线" },
                    ] as const).map((item) => (
                      <button
                        key={item.key}
                        type="button"
                        onClick={() => setSubView(item.key)}
                        className={cn(
                          "whitespace-nowrap rounded px-2 py-0.5 text-[10px] font-semibold transition-colors",
                          subView === item.key ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-background/70",
                        )}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                )}
                <div className="flex-1" />
                <TagStrip tags={daily?.tags} />
              </div>
              <div className="relative min-h-0 flex-1">
                {chartView === "minute" ? (
                  chartState.error ? (
                    <div className="flex h-full items-center justify-center text-xs text-destructive">{chartState.error}</div>
                  ) : (
                    <MinuteChart
                      chart={detail?.chart ?? null}
                      markers={overlay?.markers ?? []}
                      overlay={detail?.formula_overlay ?? null}
                      nearLine={nearLine}
                      trendLines={trendLines}
                    />
                  )
                ) : (
                  <>
                    <DailyKChart
                      payload={daily}
                      subView={subView}
                      onNeedMoreHistory={handleNeedMoreHistory}
                      loadingMore={dailyLoadingMore}
                    />
                    {mainFormula?.available && <DailyInfoOverlay main={mainFormula} />}
                  </>
                )}
              </div>
            </div>

            {/* 公式 / 共振 / 盈亏比 / 原因 */}
            <div className="grid h-[172px] shrink-0 grid-cols-2 gap-2 xl:grid-cols-4">
              <FormulaCard formula={overlay?.formula_state ?? detail?.formula_state ?? null} trend={trendLatest} />
              <ConfluenceCard confluence={overlay?.confluence_snapshot ?? detail?.confluence_snapshot ?? null} />
              <RiskRewardCard rr={signal?.risk_reward} />
              <div className="terminal-panel min-h-0 overflow-y-auto p-2.5">
                <div className="panel-title mb-1.5">信号依据</div>
                <div className="space-y-1">
                  {(signal?.reasons ?? []).slice(0, 4).map((r, i) => (
                    <div key={i} className="text-[10px] leading-snug text-foreground/80">· {r}</div>
                  ))}
                  {(signal?.risks ?? []).slice(0, 3).map((r, i) => (
                    <div key={i} className="text-[10px] leading-snug text-gold/80">⚠ {r}</div>
                  ))}
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

          {/* 右列：筹码 / 星球消息 / AI / 竞价 / 资金流 / 指标 / F10 / 缠论（全高加宽） */}
          <div className="col-span-12 min-h-0 max-lg:h-[480px] lg:col-span-5">
            <DetailTabs
              coreExtras={extras}
              error={coreExtrasState.error}
              code={code}
              watchlistCodes={watchlistCodes}
              chipDaily={daily?.chip ?? null}
              chipIntraday={daily?.chip_intraday ?? null}
              chartView={chartView}
            />
          </div>
        </div>
      </div>
    </div>
  )
}

/** 题材/概念/板块标签条：行业（官方申万口径优先）+ 概念 chips。
 *  尽量全部展示（可换行）；概念超过 10 个才折叠 +N，悬浮可见完整列表。 */
function TagStrip({ tags }: { tags?: StockTags | null }) {
  if (!tags?.available) return null
  const industry = tags.industry_official || tags.industry || ""
  const concepts = (tags.concepts ?? []).filter((c) => c !== industry)
  const MAX_SHOWN = 10
  const shown = concepts.slice(0, MAX_SHOWN)
  const rest = concepts.slice(MAX_SHOWN)
  const styleRegion = [...(tags.regions ?? []), ...(tags.styles ?? [])]
  const allTooltip = [
    industry ? `行业：${[tags.industry_official, tags.industry].filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).join(" / ")}` : "",
    concepts.length ? `概念：${concepts.join("、")}` : "",
    styleRegion.length ? `地域/风格：${styleRegion.join("、")}` : "",
    tags.stale ? "（缓存数据，待刷新）" : "",
  ]
    .filter(Boolean)
    .join("\n")
  return (
    <div className="flex max-h-[42px] min-w-0 max-w-[60%] flex-wrap content-start items-center justify-end gap-1 overflow-hidden" title={allTooltip}>
      {industry && (
        <span className="shrink-0 rounded bg-[hsl(var(--gold)/0.15)] px-1.5 py-0.5 text-[10px] font-semibold text-gold">
          {industry}
        </span>
      )}
      {shown.map((c) => (
        <span key={c} className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
          {c}
        </span>
      ))}
      {rest.length > 0 && (
        <span className="shrink-0 cursor-help rounded bg-muted/60 px-1.5 py-0.5 text-[10px] text-muted-foreground/80">
          +{rest.length}
        </span>
      )}
    </div>
  )
}

const TONE_CLASS: Record<string, string> = {
  up: "text-up",
  down: "text-down",
  flat: "text-gold",
}

/** 日K 主图信息浮层（左上）：量化评分H + 个股/大盘/量变提示 + 明日价位 */
function DailyInfoOverlay({ main }: { main: DailyMainFormula }) {
  const tips = main.tips
  const tomorrow = main.tomorrow
  return (
    <div className="pointer-events-none absolute left-1.5 top-1.5 max-w-[62%] space-y-0.5 rounded bg-card/70 px-2 py-1.5 backdrop-blur-[2px]">
      <div className="flex items-center gap-2 text-[10px]">
        <span className="font-semibold text-muted-foreground">AI主力狙击 · 量化评分H</span>
        <span
          className={cn(
            "num font-bold",
            (main.score_h ?? 0) >= 60 ? "text-up" : (main.score_h ?? 0) >= 40 ? "text-gold" : "text-muted-foreground",
          )}
        >
          {main.score_h ?? "--"}/{main.score_h_max ?? 80}
        </span>
      </div>
      {tips?.stock && (
        <div className={cn("text-[9px] leading-snug", TONE_CLASS[tips.stock.tone] ?? "text-muted-foreground")}>
          【个股】{tips.stock.text}
        </div>
      )}
      {tips?.market && (
        <div className={cn("text-[9px] leading-snug", TONE_CLASS[tips.market.tone] ?? "text-muted-foreground")}>
          【大盘】{tips.market.text}
        </div>
      )}
      {tips?.volume && (
        <div className={cn("text-[9px] leading-snug", TONE_CLASS[tips.volume.tone] ?? "text-muted-foreground")}>
          【量变】{tips.volume.text}
        </div>
      )}
      {tomorrow && (
        <div className="border-t border-border/50 pt-0.5 text-[9px] leading-snug">
          <span className="num text-down">阻力 {tomorrow.resistance.toFixed(2)} · 突破 {tomorrow.breakthrough.toFixed(2)}</span>
          <span className="text-muted-foreground/60">&nbsp;</span>
          <span className="num text-up">支撑 {tomorrow.support.toFixed(2)} · 反转 {tomorrow.reverse.toFixed(2)}</span>
        </div>
      )}
      {main.quality && (!main.quality.index_close || !main.quality.float_shares) && (
        <div className="text-[8px] text-muted-foreground/70">
          {!main.quality.index_close ? "大盘指数缺失，提示已降级 · " : ""}
          {!main.quality.float_shares ? "流通股本缺失，成本线为估算" : ""}
        </div>
      )}
    </div>
  )
}
