import { memo } from "react"
import { Crown, Gem, AlertTriangle, Diamond, X } from "lucide-react"
import type { BoardItem, OpeningMarkerEvent, OrderFlow } from "@/types/api"
import { fmtAmount, fmtPrice, pctClass, fmtPct } from "@/lib/format"
import { SignalBadge, Sparkline } from "@/components/widgets"
import { cn } from "@/lib/utils"

/**
 * 榜单行单元格：活跃股榜单（StockBoard）与右栏（机会队列/自选）共用同一套
 * 单元格组件，保证两处的排版语言完全一致；右栏在此之上叠加独有数据
 * （菱形买卖点标记、L1 分笔净买比、盘口主动买/卖手数）。
 */

/** 高位放量滞涨预警（托而不举）：反弹一段 + 放量 + 价格推不动 + 大单仍净流入 */
export function stagnationRisk(item: BoardItem): boolean {
  const rebound = item.rebound_from_low_pct ?? 0
  const pullback = Math.abs(item.pullback_from_high_pct ?? 0)
  const ratio = item.minute_amount_ratio ?? 0
  const flow = item.order_flow
  const bigOrderInflow = !!flow?.available && (flow.active_imbalance_pct ?? 0) >= 10
  return rebound >= 4 && ratio >= 1.5 && (item.change_pct <= 1.0 || pullback >= 1.5) && bigOrderInflow
}

// ------------------------------------------------------------- 菱形标记
export function markerSideClasses(marker: OpeningMarkerEvent): { row: string; chip: string; icon: string } {
  return marker.side === "buy"
    ? { row: "bg-up/5", chip: "bg-up/15 text-up", icon: "text-up fill-up" }
    : { row: "bg-down/5", chip: "bg-down/15 text-down", icon: "text-down fill-down" }
}

export function DiamondIcon({ marker, className }: { marker: OpeningMarkerEvent; className?: string }) {
  const warn = marker.state === "warn"
  const classes = markerSideClasses(marker)
  return (
    <Diamond
      className={cn(
        "h-3 w-3 shrink-0",
        marker.side === "buy" ? "text-up" : "text-down",
        !warn && classes.icon,
        warn && "animate-pulse",
        className,
      )}
    />
  )
}

/** 菱形信号 chip：买/卖标签 + 确认中状态 */
export function MarkerChip({ marker }: { marker: OpeningMarkerEvent }) {
  const classes = markerSideClasses(marker)
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-1 text-[10px] font-semibold leading-tight whitespace-nowrap",
        classes.chip,
        marker.state === "warn" && "border border-dashed border-current/40",
      )}
    >
      {marker.label}
      {marker.state === "warn" && "·确认中"}
    </span>
  )
}

// ------------------------------------------------------------- 共用单元格
export function IdentityCell({ item, marker }: { item: BoardItem; marker?: OpeningMarkerEvent }) {
  return (
    <td className="min-w-0 py-1.5 pr-2">
      <div className="flex items-center gap-1">
        {marker && <DiamondIcon marker={marker} />}
        <span className="truncate text-[13px] font-semibold">{item.name}</span>
        {item.leader && <Crown className="h-3 w-3 shrink-0 text-gold" />}
        {item.core && <Gem className="h-3 w-3 shrink-0 text-[hsl(var(--cyan))]" />}
        {item.position && <span className="shrink-0 rounded bg-primary/20 px-1 text-[9px] text-primary">持仓</span>}
        {stagnationRisk(item) && (
          <span
            className="flex shrink-0 items-center gap-0.5 rounded border border-[hsl(var(--gold)/0.5)] bg-[hsl(var(--gold)/0.12)] px-1 text-[9px] font-semibold text-gold"
            title="高位放量滞涨：大单密集但价格推不动，警惕资金反手砸盘"
          >
            <AlertTriangle className="h-2.5 w-2.5" />
            滞涨
          </span>
        )}
      </div>
      <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
        <span className="num">{item.code}</span>
        <span className="truncate">{item.sector}</span>
        {(item.stock_tags ?? []).slice(0, 2).map((t) => (
          <span key={t} className="shrink-0 rounded bg-muted px-1 text-[9px] text-muted-foreground">{t}</span>
        ))}
      </div>
    </td>
  )
}

export function PriceCell({ price, changePct }: { price: number; changePct: number }) {
  return (
    <td className="w-[72px] pr-2 text-right">
      <div className={cn("num text-[13px] font-bold", pctClass(changePct))}>{fmtPrice(price)}</div>
      <div className={cn("num text-[11px]", pctClass(changePct))}>{fmtPct(changePct)}</div>
    </td>
  )
}

export function SparklineCell({ mini, empty }: { mini: BoardItem["mini_chart"]; empty?: React.ReactNode }) {
  return (
    <td className="w-[120px] px-1">
      {mini && mini.price_pcts && mini.price_pcts.length >= 2 ? (
        <Sparkline mini={mini} />
      ) : (
        <div className="flex h-[34px] w-[112px] items-center justify-center text-[10px] text-muted-foreground/50">
          {empty ?? "无分时"}
        </div>
      )}
    </td>
  )
}

export function SignalCell({ item, marker }: { item: BoardItem; marker?: OpeningMarkerEvent }) {
  return (
    <td className="w-[86px] px-1 text-center">
      <div className="flex flex-col items-center gap-0.5">
        {item.signal ? (
          <SignalBadge signal={item.signal} score={item.signal_score} />
        ) : (
          !marker && <span className="text-[11px] text-muted-foreground/50">--</span>
        )}
        {marker && <MarkerChip marker={marker} />}
        <div className="num text-[9px] text-muted-foreground/70">{item.signal_time || marker?.time || ""}</div>
      </div>
    </td>
  )
}

export function RatioCell({ value }: { value: number | undefined }) {
  return (
    <td className="w-[56px] px-1 text-right">
      {value == null || Number.isNaN(value) ? (
        <span className="text-[11px] text-muted-foreground/50">--</span>
      ) : (
        <span className={cn("num text-[12px] font-semibold", value >= 2 ? "text-up" : value >= 1 ? "text-foreground" : "text-muted-foreground")}>
          {value.toFixed(1)}x
        </span>
      )}
    </td>
  )
}

/** 活跃股榜单版：单行的主动大单偏离 */
export function OrderFlowCell({ flow }: { flow: OrderFlow | undefined }) {
  return (
    <td
      className="w-[60px] px-1 text-right"
      title={flow?.available ? `主动买 ${flow.active_buy_volume}手 / 主动卖 ${flow.active_sell_volume}手` : "盘口数据不可用"}
    >
      {flow?.available ? (
        <span className={cn("num text-[12px] font-semibold", pctClass(flow.active_imbalance_pct))}>
          {fmtPct(flow.active_imbalance_pct, 0)}
        </span>
      ) : (
        <span className="text-[11px] text-muted-foreground/50">--</span>
      )}
    </td>
  )
}

/** 手数缩写：1200 → 1.2k */
function fmtHands(v: number | undefined): string {
  if (!v) return "0"
  return v >= 10000 ? `${(v / 10000).toFixed(1)}w` : v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(Math.round(v))
}

/**
 * 右栏独有：大单双行。
 * 第一行：盘口主动大单偏离（同活跃股榜单口径）；
 * 第二行：菱形标记的 L1 大单净买比（开盘窗口只统计大单印花，
 * 单笔成交额 ≥ max(50万, 5×中位数)），无标记时退化为盘口主动买/卖手数。
 */
export function TapeFlowCell({ flow, marker }: { flow: OrderFlow | undefined; marker?: OpeningMarkerEvent }) {
  const tape = marker?.tape_net_ratio
  const title = marker
    ? marker.reasons.join("\n")
    : flow?.available
      ? `主动买 ${flow.active_buy_volume}手 / 主动卖 ${flow.active_sell_volume}手（L1 口径）`
      : "盘口数据不可用"
  return (
    <td className="w-[84px] px-1 text-right" title={title}>
      <div className="num text-[12px] font-semibold">
        {flow?.available ? (
          <span className={pctClass(flow.active_imbalance_pct)}>{fmtPct(flow.active_imbalance_pct, 0)}</span>
        ) : (
          <span className="text-[11px] text-muted-foreground/50">--</span>
        )}
      </div>
      <div className="num mt-0.5 text-[9px] leading-tight text-muted-foreground">
        {typeof tape === "number" ? (
          <span className={pctClass(tape)}>大单 {tape >= 0 ? "+" : ""}{tape.toFixed(1)}%</span>
        ) : flow?.available ? (
          <span>
            <span className="text-up">{fmtHands(flow.active_buy_volume)}</span>
            {" / "}
            <span className="text-down">{fmtHands(flow.active_sell_volume)}</span>手
          </span>
        ) : (
          ""
        )}
      </div>
    </td>
  )
}

export function DayPositionCell({ rebound, pullback }: { rebound: number | undefined; pullback: number | undefined }) {
  const hasRebound = rebound != null && !Number.isNaN(rebound)
  const hasPullback = pullback != null && !Number.isNaN(pullback)
  const reboundValue = hasRebound ? rebound : undefined
  const pullbackValue = hasPullback ? pullback : undefined
  return (
    <td className="w-[96px] px-2">
      {!hasRebound && !hasPullback ? (
        <span className="text-[11px] text-muted-foreground/50">--</span>
      ) : (
        <>
          <div className="flex items-center gap-1 text-[10px]">
            <span className="w-8 text-muted-foreground">反弹</span>
            <span className={cn("num w-11 text-right", pctClass(reboundValue))}>{fmtPct(reboundValue, 1)}</span>
          </div>
          <div className="flex items-center gap-1 text-[10px]">
            <span className="w-8 text-muted-foreground">回撤</span>
            <span className="num w-11 text-right text-muted-foreground">
              {pullbackValue == null ? "--" : `-${Math.abs(pullbackValue).toFixed(1)}%`}
            </span>
          </div>
        </>
      )}
    </td>
  )
}

export function HeatCell({ score }: { score: number | undefined }) {
  return (
    <td className="w-[50px] px-1 text-right">
      <span className={cn("num text-[12px] font-semibold", (score ?? 0) >= 80 ? "text-up" : (score ?? 0) >= 60 ? "text-gold" : "text-muted-foreground")}>
        {score ?? "--"}
      </span>
    </td>
  )
}

export function AmountCell({ amount }: { amount: number | undefined }) {
  return (
    <td className="w-[70px] pl-1 pr-2 text-right">
      {amount ? (
        <span className="num text-[12px] text-foreground/85">{fmtAmount(amount)}</span>
      ) : (
        <span className="text-[11px] text-muted-foreground/50">--</span>
      )}
    </td>
  )
}

// ------------------------------------------------------------- 右栏紧凑行
/** 右栏表头列定义（与 RailStockRow 的单元格一一对应） */
export const RAIL_COLUMNS: { label: string; className: string }[] = [
  { label: "股票", className: "py-1 pr-2" },
  { label: "现价/涨幅", className: "w-[72px] pr-2 text-right" },
  { label: "分时", className: "w-[120px] px-1" },
  { label: "信号", className: "w-[86px] px-1 text-center" },
  { label: "大单", className: "w-[84px] px-1 text-right" },
  { label: "日内位置", className: "w-[96px] px-2" },
  { label: "成交额", className: "w-[70px] pl-1 pr-2 text-right" },
]

/** 右栏紧凑行：活跃股榜单单元格的子集 + 菱形标记 + 分笔详情 */
export const RailStockRow = memo(function RailStockRow({
  item,
  marker,
  fresh,
  onOpen,
  onRemove,
}: {
  item: BoardItem
  marker?: OpeningMarkerEvent
  fresh?: boolean
  onOpen: () => void
  /** 提供时行尾追加移除按钮列（自选列表用），点击不触发行打开 */
  onRemove?: () => void
}) {
  const markerClasses = marker ? markerSideClasses(marker) : null
  return (
    <tr
      className={cn(
        "group cursor-pointer border-b border-border/50 transition-colors hover:bg-accent/40",
        markerClasses?.row,
        fresh && "animate-pulse",
      )}
      onClick={onOpen}
      title={
        marker
          ? [
              `◇ ${marker.first_seen || marker.time} 触发｜信号价 ${fmtPrice(marker.price)}（${fmtPct(marker.change_pct)}）`,
              ...marker.reasons,
            ].join("\n")
          : undefined
      }
    >
      <IdentityCell item={item} marker={marker} />
      <PriceCell price={item.price} changePct={item.change_pct} />
      <SparklineCell mini={item.mini_chart} empty="--" />
      <SignalCell item={item} marker={marker} />
      <TapeFlowCell flow={item.order_flow} marker={marker} />
      <DayPositionCell rebound={item.rebound_from_low_pct} pullback={item.pullback_from_high_pct} />
      <AmountCell amount={item.amount} />
      {onRemove && (
        <td className="w-[28px] pl-1 pr-1.5 text-center">
          <button
            type="button"
            title="移出自选"
            className="rounded p-0.5 text-muted-foreground/40 opacity-0 transition-all hover:bg-down/15 hover:text-down group-hover:opacity-100"
            onClick={(e) => {
              e.stopPropagation()
              onRemove()
            }}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </td>
      )}
    </tr>
  )
})
