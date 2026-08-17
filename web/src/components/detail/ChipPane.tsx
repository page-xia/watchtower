import { useMemo } from "react"
import type { ChipDaily, ChipIntraday } from "@/types/api"
import { chartPalette, useTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"

/**
 * 筹码峰：价格→筹码/成交量 的横向直方图。
 * 日K模式：历史筹码分布（换手衰减模型），红=获利盘（现价以下），青=套牢盘。
 * 分时模式：当日分钟量价分布，红=VWAP 以下成交，绿=VWAP 以上成交。
 */

const ROWS_DAILY = 32
const ROWS_INTRADAY = 28

interface DisplayRow {
  price: number
  /** 该行覆盖的价格区间 [lo, hi]，用于 hover 详情 */
  lo: number
  hi: number
  value: number
  isPeak: boolean
}

function aggregate(bins: { price: number; value: number }[], lo: number, hi: number, rowCount: number): DisplayRow[] {
  if (!bins.length || hi <= lo) return []
  const rows: DisplayRow[] = []
  const width = (hi - lo) / rowCount
  for (let i = 0; i < rowCount; i++) {
    const rowHi = hi - i * width
    rows.push({ price: rowHi - width / 2, lo: rowHi - width, hi: rowHi, value: 0, isPeak: false })
  }
  for (const bin of bins) {
    const idx = Math.max(0, Math.min(rowCount - 1, Math.floor((hi - bin.price) / width)))
    rows[idx].value += bin.value
    if (bin.value > 0) rows[idx].price = bin.price
  }
  const max = Math.max(...rows.map((r) => r.value), 0)
  if (max > 0) {
    for (let i = 1; i < rowCount - 1; i++) {
      if (rows[i].value >= rows[i - 1].value && rows[i].value >= rows[i + 1].value && rows[i].value >= max * 0.55) {
        rows[i].isPeak = true
      }
    }
  }
  return rows
}

function PriceLines({
  lo,
  hi,
  lines,
}: {
  lo: number
  hi: number
  lines: { price: number; color: string; label: string; dashed?: boolean }[]
}) {
  if (hi <= lo) return null
  return (
    <>
      {lines.map((line) => {
        const top = ((hi - line.price) / (hi - lo)) * 100
        if (top < 0 || top > 100) return null
        return (
          <div key={line.label} className="pointer-events-none absolute left-0 right-0" style={{ top: `${top}%` }}>
            <div
              className="w-full"
              style={{ borderTop: `1px ${line.dashed === false ? "solid" : "dashed"} ${line.color}` }}
            />
            <span
              className="num absolute left-0 -translate-y-1/2 rounded bg-card/85 px-1 text-[9px] font-semibold"
              style={{ color: line.color }}
            >
              {line.label} {line.price.toFixed(2)}
            </span>
          </div>
        )
      })}
    </>
  )
}

function ChipBars({
  rows,
  splitPrice,
  upColor,
  downColor,
  mutedColor,
  total,
  unitLabel,
}: {
  rows: DisplayRow[]
  splitPrice: number
  upColor: string
  downColor: string
  mutedColor: string
  /** 总量（占比分母）；缺省用行合计 */
  total?: number
  /** hover 详情里的单位说明，如 "筹码占比" / "成交量" */
  unitLabel: string
}) {
  const max = Math.max(...rows.map((r) => r.value), 1e-9)
  const sum = total ?? rows.reduce((acc, r) => acc + r.value, 0)
  return (
    <div className="flex h-full flex-col">
      {rows.map((row, i) => {
        const share = sum > 0 ? (row.value / sum) * 100 : 0
        return (
          <div
            key={i}
            className="relative flex min-h-0 flex-1 cursor-default items-center"
            title={`${row.lo.toFixed(2)} ~ ${row.hi.toFixed(2)}\n${unitLabel} ${share.toFixed(2)}%${row.isPeak ? " · 峰值区" : ""}`}
          >
            {row.value > 0 && (
              <div
                className="absolute inset-y-[15%] left-0 rounded-r-[2px]"
                style={{
                  width: `${(row.value / max) * 100}%`,
                  background: row.price <= splitPrice ? upColor : downColor,
                  opacity: row.isPeak ? 1 : 0.82,
                }}
              />
            )}
            {/* 行尾数字：占比%，所有行右对齐成一列，峰值行加 ▲ */}
            <span
              className="num absolute -translate-y-1/2 text-[8px] leading-none"
              style={{ right: row.isPeak ? 14 : 4, top: "50%", color: row.value > 0 ? mutedColor : "transparent" }}
            >
              {row.value > 0 ? `${share >= 0.1 ? share.toFixed(1) : share.toFixed(2)}%` : ""}
            </span>
            {row.isPeak && (
              <span className="num absolute -translate-y-1/2 text-[8px]" style={{ right: 3, top: "50%", color: mutedColor }}>
                ▲
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function StatChip({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="rounded border border-border/60 bg-background/40 px-1.5 py-1">
      <div className="text-[8px] text-muted-foreground">{label}</div>
      <div className={cn("num text-[10px] font-semibold", className)}>{value}</div>
    </div>
  )
}

export function ChipDailyPane({ chip }: { chip: ChipDaily | null | undefined }) {
  const theme = useTheme()
  const pal = useMemo(() => chartPalette(theme), [theme])
  const rows = useMemo(() => {
    if (!chip?.available || !chip.bins?.length) return []
    return aggregate(
      chip.bins.map((b) => ({ price: b.price, value: b.weight ?? 0 })),
      chip.price_low ?? 0,
      chip.price_high ?? 0,
      ROWS_DAILY,
    )
  }, [chip])

  if (!chip?.available || !rows.length) {
    return <div className="p-8 text-center text-xs text-muted-foreground">{chip?.note || "暂无筹码分布数据"}</div>
  }
  // 注意：根节点必须 h-full，否则内部 flex-1 直方图区域高度塌为 0
  const lo = chip.price_low ?? 0
  const hi = chip.price_high ?? 0
  const current = chip.current_price ?? 0
  const winnerPct = (chip.winner_pct ?? 0) * 100

  const lines = [
    { price: current, color: pal.up, label: "现价", dashed: false },
    ...(chip.avg_cost ? [{ price: chip.avg_cost, color: pal.gold, label: "平均成本", dashed: true }] : []),
  ]

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap gap-1 px-3 pt-2.5">
        <StatChip label="获利比例" value={`${winnerPct.toFixed(1)}%`} className={winnerPct >= 50 ? "text-up" : "text-down"} />
        <StatChip label="平均成本" value={chip.avg_cost?.toFixed(2) ?? "--"} />
        <StatChip
          label="90%成本区间"
          value={chip.cost90 ? `${chip.cost90[0].toFixed(2)}~${chip.cost90[1].toFixed(2)}` : "--"}
        />
        <StatChip
          label="90%集中度"
          value={chip.concentration90 != null ? `${(chip.concentration90 * 100).toFixed(1)}%` : "--"}
        />
        <StatChip
          label="70%成本区间"
          value={chip.cost70 ? `${chip.cost70[0].toFixed(2)}~${chip.cost70[1].toFixed(2)}` : "--"}
        />
        <StatChip
          label="70%集中度"
          value={chip.concentration70 != null ? `${(chip.concentration70 * 100).toFixed(1)}%` : "--"}
        />
      </div>
      <div className="relative mx-3 mt-2 min-h-0 flex-1">
        <ChipBars
          rows={rows}
          splitPrice={current}
          upColor={pal.upA(0.85)}
          downColor="hsl(187 85% 53% / 0.7)"
          mutedColor={pal.textMuted}
          total={1}
          unitLabel="筹码占比"
        />
        <PriceLines lo={lo} hi={hi} lines={lines} />
        {/* 成本区间右缘标记 */}
        {chip.cost70 && (
          <div
            className="pointer-events-none absolute right-0 w-1 rounded-l"
            style={{
              top: `${((hi - chip.cost70[1]) / (hi - lo)) * 100}%`,
              height: `${((chip.cost70[1] - chip.cost70[0]) / (hi - lo)) * 100}%`,
              background: pal.goldA(0.55),
            }}
          />
        )}
        {chip.cost90 && (
          <div
            className="pointer-events-none absolute right-0 w-0.5 rounded-l"
            style={{
              top: `${((hi - chip.cost90[1]) / (hi - lo)) * 100}%`,
              height: `${((chip.cost90[1] - chip.cost90[0]) / (hi - lo)) * 100}%`,
              background: pal.flatA(0.5),
            }}
          />
        )}
      </div>
      <div className="flex items-center justify-between px-3 py-1.5 text-[9px] text-muted-foreground">
        <span>
          主峰 {chip.peaks?.slice(0, 3).map((p) => p.price.toFixed(2)).join(" / ") || "--"}
        </span>
        <span>
          {chip.bars_used}根日K · 截至 {chip.as_of}
          {chip.quality === "estimated_turnover" ? " · 换手率为估算" : ""}
        </span>
      </div>
    </div>
  )
}

export function ChipIntradayPane({ chip }: { chip: ChipIntraday | null | undefined }) {
  const theme = useTheme()
  const pal = useMemo(() => chartPalette(theme), [theme])
  const rows = useMemo(() => {
    if (!chip?.available || !chip.bins?.length) return []
    const prices = chip.bins.map((b) => b.price)
    return aggregate(
      chip.bins.map((b) => ({ price: b.price, value: b.vol ?? 0 })),
      Math.min(...prices),
      Math.max(...prices),
      ROWS_INTRADAY,
    )
  }, [chip])

  if (!chip?.available || !rows.length) {
    return <div className="p-8 text-center text-xs text-muted-foreground">{chip?.note || "暂无当日量价分布"}</div>
  }
  const prices = chip.bins!.map((b) => b.price)
  const lo = Math.min(...prices)
  const hi = Math.max(...prices)
  const vwap = chip.vwap ?? 0
  const current = chip.current_price ?? 0
  const aboveVwap = current >= vwap

  const lines = [
    ...(chip.prev_close ? [{ price: chip.prev_close, color: pal.markLine, label: "昨收", dashed: true }] : []),
    { price: vwap, color: pal.gold, label: "VWAP", dashed: true },
    { price: current, color: aboveVwap ? pal.up : pal.down, label: "现价", dashed: false },
  ]

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap gap-1 px-3 pt-2.5">
        <StatChip label="分时VWAP" value={vwap.toFixed(2)} className="text-gold" />
        <StatChip
          label="现价位置"
          value={aboveVwap ? "VWAP上方" : "VWAP下方"}
          className={aboveVwap ? "text-up" : "text-down"}
        />
        <StatChip label="成交密集价" value={chip.peak_price?.toFixed(2) ?? "--"} />
        {/* easy_tdx 分钟量单位是手：万股 = 手 / 100 */}
        <StatChip label="累计成交量" value={chip.total_vol != null ? `${(chip.total_vol / 100).toFixed(0)}万股` : "--"} />
      </div>
      <div className="relative mx-3 mt-2 min-h-0 flex-1">
        <ChipBars
          rows={rows}
          splitPrice={vwap}
          upColor={pal.upA(0.8)}
          downColor={pal.downA(0.8)}
          mutedColor={pal.textMuted}
          unitLabel="分钟成交量占比"
        />
        <PriceLines lo={lo} hi={hi} lines={lines} />
      </div>
      <div className="flex items-center justify-between px-3 py-1.5 text-[9px] text-muted-foreground">
        <span>当日分钟量价分布（按分钟收盘价分桶）</span>
        <span>截至 {chip.as_of || "--"}</span>
      </div>
    </div>
  )
}
