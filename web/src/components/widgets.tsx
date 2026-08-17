import { memo } from "react"
import { cn } from "@/lib/utils"
import { signalTone, TONE_STYLES, pctClass, fmtPct } from "@/lib/format"
import { chartPalette, useTheme } from "@/lib/theme"
import type { MiniChart } from "@/types/api"

/** 信号徽章：买T 红 / 减T卖T 绿 / 观察 金 */
export function SignalBadge({ signal, score, className }: { signal: string; score?: number; className?: string }) {
  const tone = signalTone(signal)
  const styles = TONE_STYLES[tone]
  return (
    <span className={cn("inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold leading-none whitespace-nowrap", styles.chip, className)}>
      {signal || "--"}
      {typeof score === "number" && score > 0 && <span className="num opacity-80">{score}</span>}
    </span>
  )
}

export function PctText({ value, className, digits = 2 }: { value: number | null | undefined; className?: string; digits?: number }) {
  return <span className={cn("num", pctClass(value), className)}>{fmtPct(value, digits)}</span>
}

/** 迷你分时线（SVG，供榜单每行使用，比 ECharts 实例便宜得多） */
export const Sparkline = memo(function Sparkline({
  mini,
  width = 112,
  height = 34,
}: {
  mini: MiniChart | undefined
  width?: number
  height?: number
}) {
  const theme = useTheme()
  const pal = chartPalette(theme)
  if (!mini || !mini.price_pcts || mini.price_pcts.length < 2) {
    return <div style={{ width, height }} className="flex items-center justify-center text-[10px] text-muted-foreground/50">无分时</div>
  }
  const pts = mini.price_pcts
  const vwaps = mini.vwap_pcts ?? []
  const all = [...pts, ...vwaps, 0]
  const min = Math.min(...all)
  const max = Math.max(...all)
  const span = max - min || 1
  // X 轴按「交易时间」映射而非点位序号：后端抽样偏向波动大的时段（上午密、
  // 下午稀），按序号铺开会把下午行情挤到右边缘，看起来像只显示了半天。
  // A 股交易时段 09:30-11:30 / 13:00-15:00 共 240 分钟，午休压缩。
  const times = mini.times ?? []
  const toTradingMinute = (t: string): number | null => {
    const m = /^(\d{1,2}):(\d{2})/.exec(t ?? "")
    if (!m) return null
    const mins = Number(m[1]) * 60 + Number(m[2])
    if (mins <= 11 * 60 + 30) return mins - 570 // 09:30 -> 0
    if (mins >= 13 * 60) return mins - 660 // 13:00 -> 120, 15:00 -> 240
    return 120
  }
  const minuteAxis =
    times.length === pts.length ? times.map(toTradingMinute) : []
  const useTimeAxis = minuteAxis.length === pts.length && minuteAxis.every((v) => v !== null)
  const x = (i: number) =>
    useTimeAxis
      ? ((minuteAxis[i] as number) / 240) * (width - 2) + 1
      : (i / (pts.length - 1)) * (width - 2) + 1
  const y = (v: number) => height - 2 - ((v - min) / span) * (height - 4)
  const line = pts.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")
  const up = (mini.latest_change_pct ?? pts[pts.length - 1]) >= 0
  const stroke = up ? pal.up : pal.down
  const zeroY = y(0)
  const areaPts = `1,${zeroY.toFixed(1)} ${line} ${(width - 1).toFixed(1)},${zeroY.toFixed(1)}`
  const vwapLine = vwaps.length === pts.length ? vwaps.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ") : null
  const gid = `sg-${up ? "u" : "d"}`
  return (
    <svg width={width} height={height} className="block shrink-0" aria-hidden>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity="0.25" />
          <stop offset="100%" stopColor={stroke} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      <line x1="0" y1={zeroY} x2={width} y2={zeroY} stroke={pal.zeroLine} strokeDasharray="2 3" strokeWidth="0.6" />
      <polygon points={areaPts} fill={`url(#${gid})`} stroke="none" />
      {vwapLine && <polyline points={vwapLine} fill="none" stroke={pal.goldA(0.55)} strokeWidth="0.8" strokeDasharray="2 2" />}
      <polyline points={line} fill="none" stroke={stroke} strokeWidth="1.2" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
})

/** 0-100 评分小仪表条 */
export function ScoreMeter({ value, color, className }: { value: number; color?: string; className?: string }) {
  const v = Math.max(0, Math.min(100, value))
  return (
    <div className={cn("h-1 w-full overflow-hidden rounded-full bg-muted", className)}>
      <div
        className="h-full rounded-full transition-all duration-500"
        style={{ width: `${v}%`, background: color ?? `hsl(${220 - (v / 100) * 220} 80% 55%)` }}
      />
    </div>
  )
}

/** 板块热度条 */
export function HeatBar({ score, className }: { score: number; className?: string }) {
  const v = Math.max(0, Math.min(100, score))
  const hue = 220 - (v / 100) * 220
  return (
    <div className={cn("relative h-1.5 w-full overflow-hidden rounded-full bg-muted", className)}>
      <div
        className="h-full rounded-full transition-all duration-500"
        style={{ width: `${v}%`, background: `linear-gradient(90deg, hsl(${hue} 70% 45%), hsl(${hue} 85% 58%))` }}
      />
    </div>
  )
}

/** 状态点：ok=绿，warning=黄（休市休息中），其余=红（连接异常） */
export function StatusDot({ ok, warning, className }: { ok: boolean; warning?: boolean; className?: string }) {
  return (
    <span
      className={cn(
        "relative inline-flex h-2 w-2 rounded-full",
        ok ? "bg-down" : warning ? "bg-gold" : "bg-destructive",
        className,
      )}
    >
      {ok && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-down opacity-60" />}
    </span>
  )
}
