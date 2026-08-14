import { TrendingUp, TrendingDown, Flame, Zap } from "lucide-react"
import type { MarketState, IndexQuote, IndexMinutesResponse } from "@/types/api"
import { fmtAmount, fmtPct, fmtPrice, pctClass } from "@/lib/format"
import { chartPalette, useTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"

/** 指数分时线 + 量柱：量柱半透明铺满走势区高度，画在走势线下面，走势线置顶 */
function IndexMinuteLine({ points }: { points: { time: string; change_pct: number; vol?: number }[] }) {
  const theme = useTheme()
  const pal = chartPalette(theme)
  if (!points || points.length < 2) {
    return <div className="flex h-full items-center justify-center text-[9px] text-muted-foreground/50">分时加载中…</div>
  }
  const W = 200
  const H = 46
  const values = points.map((p) => p.change_pct)
  const lo = Math.min(...values, 0)
  const hi = Math.max(...values, 0)
  const span = hi - lo || 1
  const y = (v: number) => ((hi - v) / span) * (H - 4) + 2
  const step = W / (values.length - 1)
  const path = values.map((v, i) => `${i === 0 ? "M" : "L"} ${(i * step).toFixed(1)} ${y(v).toFixed(1)}`).join(" ")
  const latest = values[values.length - 1]
  const color = latest >= 0 ? pal.up : pal.down
  const zeroY = y(0)
  const vols = points.map((p) => Math.max(p.vol ?? 0, 0))
  const maxV = Math.max(...vols, 1)
  const bw = W / vols.length
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-full w-full">
      {/* 量柱：半透明、满高度，先画在底层；量能越大颜色越深 */}
      {vols.map((v, i) => {
        const ratio = v / maxV
        const h = Math.max(ratio * H, v > 0 ? 1 : 0)
        const alpha = 0.14 + 0.4 * ratio
        const up = i === 0 ? points[0].change_pct >= 0 : points[i].change_pct >= points[i - 1].change_pct
        return (
          <rect
            key={i}
            x={i * bw}
            y={H - h}
            width={Math.max(bw * 0.8, 0.5)}
            height={h}
            fill={up ? pal.upA(Number(alpha.toFixed(2))) : pal.downA(Number(alpha.toFixed(2)))}
          />
        )
      })}
      <line x1="0" y1={zeroY} x2={W} y2={zeroY} stroke={pal.axis} strokeWidth="0.6" strokeDasharray="3 3" />
      <path d={path} fill="none" stroke={color} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

/** 单只指数卡：现价 / 涨跌幅 / 分时线 / 分时量柱 / 日内区间位置条 */
function IndexCard({ idx, minutes }: { idx: IndexQuote; minutes?: { time: string; change_pct: number; vol?: number }[] }) {
  const range = idx.high - idx.low
  const pos = range > 0 ? ((idx.price - idx.low) / range) * 100 : 50
  const up = idx.change_pct >= 0
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1 rounded-md border border-border bg-card px-3 py-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-[11px] text-muted-foreground">{idx.name}</span>
        <span className={cn("num text-[11px]", pctClass(idx.change_pct))}>{fmtPct(idx.change_pct)}</span>
      </div>
      <div className={cn("num text-lg font-bold leading-tight", pctClass(idx.change_pct))}>
        {fmtPrice(idx.price)}
      </div>
      <div className="min-h-0 flex-1">
        <IndexMinuteLine points={minutes ?? []} />
      </div>
      <div>
        <div className="relative h-1 rounded-full bg-muted">
          <div
            className={cn("absolute top-1/2 h-2.5 w-[3px] -translate-y-1/2 rounded-full", up ? "bg-up" : "bg-down")}
            style={{ left: `calc(${pos.toFixed(1)}% - 1px)` }}
          />
        </div>
        <div className="num mt-0.5 flex justify-between text-[9px] text-muted-foreground/70">
          <span>{fmtPrice(idx.low)}</span>
          <span>{fmtPrice(idx.high)}</span>
        </div>
      </div>
    </div>
  )
}

/** 情绪分仪表（半环） */
function EmotionGauge({ score }: { score: number }) {
  const theme = useTheme()
  const pal = chartPalette(theme)
  const v = Math.max(0, Math.min(100, score))
  const angle = (v / 100) * 180
  const rad = ((180 - angle) * Math.PI) / 180
  const r = 30
  const cx = 36
  const cy = 34
  const nx = cx + r * Math.cos(rad)
  const ny = cy - r * Math.sin(rad)
  // 情绪分 0(绿冷) → 100(红热)
  const color = `hsl(${152 + (v / 100) * 202} 80% 55%)`
  return (
    <svg width="72" height="40" viewBox="0 0 72 40" className="block">
      <path d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`} fill="none" stroke={pal.gaugeTrack} strokeWidth="5" strokeLinecap="round" />
      <path
        d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${nx} ${ny}`}
        fill="none"
        stroke={color}
        strokeWidth="5"
        strokeLinecap="round"
      />
      <text x={cx} y={cy - 4} textAnchor="middle" style={{ fontSize: 15, fontWeight: 700, fill: pal.textStrong }}>
        {v}
      </text>
      <text x={cx} y={cy + 10} textAnchor="middle" style={{ fontSize: 8, fill: pal.textMuted }}>
        情绪分
      </text>
    </svg>
  )
}

export function MarketStrip({ market, indexMinutes }: { market: MarketState | null; indexMinutes?: IndexMinutesResponse | null }) {
  if (!market) {
    return <div className="h-[132px] shrink-0 animate-pulse rounded-lg bg-card/50" />
  }
  const total = market.up_count + market.down_count + market.flat_count || 1
  const minuteByCode = new Map((indexMinutes?.indices ?? []).map((s) => [s.code, s.points]))

  return (
    <div className="flex h-[132px] shrink-0 items-stretch gap-2">
      {/* 市场节奏 */}
      <div className="flex w-[300px] shrink-0 items-center gap-3 rounded-lg border border-border bg-card px-3 py-1.5">
        <EmotionGauge score={market.emotion_score} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            {market.trend.includes("强") || market.trend.includes("转强") ? (
              <TrendingUp className="h-3.5 w-3.5 text-up" />
            ) : (
              <TrendingDown className="h-3.5 w-3.5 text-down" />
            )}
            <span className="truncate text-sm font-bold">{market.trend}</span>
          </div>
          <div className="mt-0.5 flex items-center gap-1 text-[10px] text-muted-foreground">
            <Flame className="h-3 w-3 text-gold" />
            <span className="truncate">主线 {market.mainline || "--"}</span>
          </div>
          <div className="mt-0.5 truncate text-[10px] text-muted-foreground/80">
            {(market.reasons ?? []).slice(0, 2).join(" · ") || "等待行情数据"}
          </div>
        </div>
      </div>

      {/* 指数（含分时线） */}
      {(market.indices ?? []).map((idx) => (
        <IndexCard key={idx.code} idx={idx} minutes={minuteByCode.get(idx.code)} />
      ))}

      {/* 涨跌宽度 + 涨停梯队 + 成交额 */}
      <div className="flex w-[340px] shrink-0 flex-col justify-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5">
        {/* 涨跌宽度长条：分段宽度 = 家数 / 两市总家数（涨停/炸板/跌停为细分小段） */}
        <div className="flex h-3.5 w-full overflow-hidden rounded-full bg-muted text-[9px] font-semibold leading-[14px]">
          {[
            { key: "up", value: market.up_count, cls: "bg-[hsl(var(--up)/0.55)] text-white/90", showAt: 10 },
            { key: "limitUp", value: market.limit_up_count, cls: "bg-up text-white", showAt: 6 },
            { key: "opened", value: market.opened_limit_count, cls: "bg-[hsl(var(--gold)/0.85)] text-black/85", showAt: 6 },
            { key: "limitDown", value: market.limit_down_count, cls: "bg-down text-white", showAt: 6 },
            { key: "down", value: market.down_count, cls: "bg-[hsl(var(--down)/0.55)] text-white/90", showAt: 10 },
          ].map((seg) => {
            const w = (seg.value / total) * 100
            return (
              <div key={seg.key} className={cn("text-center", seg.cls)} style={{ width: `${w}%` }}>
                {w > seg.showAt && seg.value}
              </div>
            )
          })}
        </div>
        <div className="flex items-center justify-between text-[10px]">
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground">
              涨 <span className="num font-semibold text-up">{market.up_count}</span>
            </span>
            <span className="text-muted-foreground">
              跌 <span className="num font-semibold text-down">{market.down_count}</span>
            </span>
            <span className="text-muted-foreground">
              涨停 <span className="num font-semibold text-up">{market.limit_up_count}</span>
            </span>
            <span className="text-muted-foreground">
              炸板 <span className="num font-semibold text-gold">{market.opened_limit_count}</span>
            </span>
            <span className="text-muted-foreground">
              跌停 <span className="num font-semibold text-down">{market.limit_down_count}</span>
            </span>
          </div>
        </div>
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
            <Zap className="h-3 w-3 text-primary" />
            两市成交
            {market.amount_expanding && <span className="rounded bg-up-dim px-1 text-[9px] text-up">放量</span>}
          </span>
          <span className="num text-sm font-bold text-foreground">{fmtAmount(market.total_amount)}</span>
        </div>
      </div>
    </div>
  )
}
