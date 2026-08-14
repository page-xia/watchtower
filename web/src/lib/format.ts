// A股惯例：红涨绿跌

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "--"
  const sign = v > 0 ? "+" : ""
  return `${sign}${v.toFixed(digits)}%`
}

export function fmtPrice(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "--"
  return v.toFixed(digits)
}

export function fmtAmount(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return "--"
  const abs = Math.abs(v)
  if (abs >= 1e12) return `${(v / 1e12).toFixed(2)}万亿`
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(1)}万`
  return v.toFixed(0)
}

export function fmtVolume(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "--"
  // 后端 vol 单位为手
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿手`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(1)}万手`
  return `${v.toFixed(0)}手`
}

/** 涨跌语义色 class：v>0 红，v<0 绿，其余灰 */
export function pctClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return "text-flat"
  return v > 0 ? "text-up" : "text-down"
}

export type SignalTone = "buy" | "sell" | "watch" | "none"

export function signalTone(signal: string | null | undefined): SignalTone {
  if (!signal) return "none"
  if (signal.includes("买") && !signal.includes("卖")) return "buy"
  if (signal.includes("卖") || signal.includes("减")) return "sell"
  if (signal.includes("观察")) return "watch"
  return "none"
}

export const TONE_STYLES: Record<SignalTone, { text: string; chip: string; dot: string }> = {
  buy: {
    text: "text-up",
    chip: "bg-[hsl(var(--up-dim))] text-[hsl(var(--up))] border border-[hsl(var(--up)/0.45)]",
    dot: "bg-up",
  },
  sell: {
    text: "text-down",
    chip: "bg-[hsl(var(--down-dim))] text-[hsl(var(--down))] border border-[hsl(var(--down)/0.45)]",
    dot: "bg-down",
  },
  watch: {
    text: "text-[hsl(var(--gold))]",
    chip: "bg-[hsl(var(--gold)/0.12)] text-[hsl(var(--gold))] border border-[hsl(var(--gold)/0.4)]",
    dot: "bg-[hsl(var(--gold))]",
  },
  none: {
    text: "text-muted-foreground",
    chip: "bg-muted text-muted-foreground border border-border",
    dot: "bg-muted-foreground",
  },
}

export function heatColor(score: number): string {
  // 板块热度 0-100 → 蓝→紫→红 渐变
  const s = Math.max(0, Math.min(100, score))
  const hue = 220 - (s / 100) * 220 // 220(蓝) → 0(红)
  return `hsl(${hue} 80% 55%)`
}

export function timeShort(iso: string | null | undefined): string {
  if (!iso) return "--"
  const t = iso.includes("T") ? iso.split("T")[1] : iso
  return t.slice(0, 8)
}

export function dateShort(iso: string | null | undefined): string {
  if (!iso) return "--"
  return iso.slice(0, 10)
}
