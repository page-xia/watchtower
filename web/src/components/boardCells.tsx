import { memo } from "react"
import { Crown, Gem, AlertTriangle, X } from "lucide-react"
import type { BoardItem, OrderFlow } from "@/types/api"
import { fmtPrice, pctClass, fmtPct, signalTone } from "@/lib/format"
import { SignalBadge, Sparkline } from "@/components/widgets"
import { cn } from "@/lib/utils"

/**
 * 榜单行单元格：活跃股榜单（StockBoard）与右栏（机会队列/自选）共用同一套
 * 单元格组件，保证两处的排版语言完全一致；右栏在此之上叠加独有数据
 * （L1 分笔净买比、盘口主动买/卖手数）。
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

// ------------------------------------------------------------- 共用单元格
export function IdentityCell({ item }: { item: BoardItem }) {
  return (
    <td className="min-w-0 py-1.5 pr-2">
      <div className="flex items-center gap-1">
        <span className="truncate text-[13px] font-semibold">{item.name}</span>
        {item.sector && (
          <span className="truncate text-[10px] leading-[13px] text-muted-foreground/70">{item.sector}</span>
        )}
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

export function SignalCell({ item }: { item: BoardItem }) {
  return (
    <td className="w-[86px] px-1 text-center">
      <div className="flex flex-col items-center gap-0.5">
        {item.signal ? (
          <SignalBadge signal={item.signal} score={item.signal_score} />
        ) : (
          <span className="text-[11px] text-muted-foreground/50">--</span>
        )}
        <div className="num text-[9px] text-muted-foreground/70">{item.signal_time || ""}</div>
      </div>
    </td>
  )
}

/** 做T紧凑分析（做T公式.md 榜单口径）：资金（外盘/内盘）+ 趋势多空 + 评分建议，
 *  一列替代原「量比/大单/成交额」，在列表上一眼看强弱；评分明细与建议详情收进 title。 */
export function TAnalysisCell({ item }: { item: BoardItem }) {
  const t = item.t_analysis
  if (!t?.available) {
    return (
      <td className="w-[122px] px-1" title="做T分析加载中（与分时缩略图同生命周期，下一轮刷新补齐）">
        <span className="text-[11px] text-muted-foreground/50">--</span>
      </td>
    )
  }
  const adviceSymbol = t.score >= 70 ? "★" : t.score >= 50 ? "☆" : t.score >= 30 ? "△" : t.score >= 15 ? "○" : "X"
  const adviceClass =
    t.score >= 70
      ? "bg-up-dim text-up"
      : t.score >= 50
        ? "bg-[hsl(var(--gold)/0.12)] text-gold"
        : t.score >= 30
          ? "bg-muted text-muted-foreground"
          : "bg-down-dim text-down"
  const fundClass = !t.fund_available
    ? "text-muted-foreground/50"
    : t.fund_pct > 0
      ? "text-up"
      : t.fund_pct < 0
        ? "text-down"
        : "text-muted-foreground"
  const tooltip = [
    "做T分析（分钟EMA30/强弱线 + 外盘/内盘口径）",
    [t.trend_text, t.position_text, t.vwap_relation].filter(Boolean).join(" | "),
    t.fund_available ? `资金 ${t.fund_text} ${t.fund_attitude}` : "资金：盘口不可用",
    `${t.advice} — ${t.advice_detail}`,
  ]
    .filter(Boolean)
    .join("\n")
  return (
    <td className="w-[122px] px-1" title={tooltip}>
      <div className="flex items-center gap-1 whitespace-nowrap leading-tight">
        <span className="text-[10px] text-muted-foreground">资</span>
        <span className={cn("num text-[12px] font-semibold", fundClass)}>
          {t.fund_available ? t.fund_text : "--"}
        </span>
        {t.fund_attitude && (
          <span className="text-[9px] text-muted-foreground">{t.fund_attitude}</span>
        )}
      </div>
      <div className="mt-0.5 flex items-center gap-1 whitespace-nowrap leading-tight">
        <span
          className={cn(
            "text-[10px] font-semibold",
            t.trend_bull == null ? "text-muted-foreground/50" : t.trend_bull ? "text-up" : "text-down",
          )}
        >
          {t.trend_text || "--"}
        </span>
        <span className={cn("num rounded px-1 text-[9px] font-bold", adviceClass)}>
          {adviceSymbol}{t.advice_label} {t.score}
        </span>
      </div>
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
 * 第二行：盘口主动买/卖手数。
 */
export function TapeFlowCell({ flow }: { flow: OrderFlow | undefined }) {
  const title = flow?.available
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
        {flow?.available ? (
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

/** 日内压力位/支撑位（做T当日常量）：两行布局——
 *  压行尾部放金色「低吸」徽标（现价在低吸区间时），支行尾部放距当天最近买/卖点的 ±%。
 *  现价距压/支的百分比收进 title 悬浮提示，避免一列塞三行。 */
export function DayPositionCell({
  price,
  resistance,
  support,
  nearZone,
  lastAction,
  lastActionPrice,
  lastActionTime,
}: {
  price?: number
  resistance?: number
  support?: number
  nearZone?: boolean
  lastAction?: string
  lastActionPrice?: number
  lastActionTime?: string
}) {
  const hasR = resistance != null && resistance > 0
  const hasS = support != null && support > 0
  const rPct = hasR && price ? (resistance / price - 1) * 100 : null
  const sPct = hasS && price ? (support / price - 1) * 100 : null
  const tone = signalTone(lastAction)
  const actionDist =
    (tone === "buy" || tone === "sell") && lastActionPrice != null && lastActionPrice > 0 && price
      ? (price / lastActionPrice - 1) * 100
      : null
  const tooltip = [
    "做T当日压力位/支撑位",
    rPct != null ? `压：现价距压力 ${rPct >= 0 ? "+" : ""}${rPct.toFixed(1)}%` : "",
    sPct != null ? `支：现价高出支撑 ${sPct >= 0 ? "+" : ""}${sPct.toFixed(1)}%` : "",
    nearZone ? "金色「低吸」= 现价在低吸区间（底线-1% ~ 顶线+3%）内" : "",
    actionDist != null
      ? `当天最近${tone === "buy" ? "买" : "卖"}点 ${lastActionPrice?.toFixed(2)}${lastActionTime ? `（${lastActionTime}）` : ""}，现价距其 ${actionDist >= 0 ? "+" : ""}${actionDist.toFixed(1)}%`
      : "当天尚无买/卖信号",
  ]
    .filter(Boolean)
    .join("\n")
  if (!hasR && !hasS && !nearZone && actionDist == null) {
    return (
      <td className="w-[110px] px-2" title={tooltip}>
        <span className="text-[11px] text-muted-foreground/50">--</span>
      </td>
    )
  }
  return (
    <td className="w-[110px] px-2" title={tooltip}>
      <div className="flex items-center gap-1 whitespace-nowrap text-[10px]">
        <span className="text-muted-foreground">压</span>
        <span className="num text-down">{hasR ? fmtPrice(resistance) : "--"}</span>
        {nearZone && (
          <span className="rounded border border-[hsl(var(--gold)/0.55)] bg-[hsl(var(--gold)/0.14)] px-1 text-[9px] font-bold text-gold">
            低吸
          </span>
        )}
      </div>
      <div className="flex items-center gap-1 whitespace-nowrap text-[10px]">
        <span className="text-muted-foreground">支</span>
        <span className="num text-up">{hasS ? fmtPrice(support) : "--"}</span>
        {actionDist != null && (
          <span className={cn("num text-[9px] font-semibold", tone === "buy" ? "text-up" : "text-down")}>
            {tone === "buy" ? "买" : "卖"}{actionDist >= 0 ? "+" : ""}{actionDist.toFixed(1)}%
          </span>
        )}
      </div>
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

// ------------------------------------------------------------- 右栏紧凑行
/** 右栏表头列定义（与 RailStockRow 的单元格一一对应） */
export const RAIL_COLUMNS: { label: string; className: string }[] = [
  { label: "股票", className: "py-1 pr-2" },
  { label: "现价/涨幅", className: "w-[72px] pr-2 text-right" },
  { label: "分时", className: "w-[120px] px-1" },
  { label: "信号", className: "w-[86px] px-1 text-center" },
  { label: "大单", className: "w-[84px] px-1 text-right" },
  { label: "压/支", className: "w-[110px] px-2" },
  { label: "做T分析", className: "w-[122px] px-1" },
]

/** 右栏紧凑行：活跃股榜单单元格的子集 + 分笔详情 */
export const RailStockRow = memo(function RailStockRow({
  item,
  onOpen,
  onRemove,
}: {
  item: BoardItem
  onOpen: () => void
  /** 提供时行尾追加移除按钮列（自选列表用），点击不触发行打开 */
  onRemove?: () => void
}) {
  return (
    <tr
      className="group cursor-pointer border-b border-border/50 transition-colors hover:bg-accent/40"
      onClick={onOpen}
    >
      <IdentityCell item={item} />
      <PriceCell price={item.price} changePct={item.change_pct} />
      <SparklineCell mini={item.mini_chart} empty="--" />
      <SignalCell item={item} />
      <TapeFlowCell flow={item.order_flow} />
      <DayPositionCell
        price={item.price}
        resistance={item.resistance}
        support={item.support}
        nearZone={item.near_zone}
        lastAction={item.last_action}
        lastActionPrice={item.last_action_price}
        lastActionTime={item.last_action_time}
      />
      <TAnalysisCell item={item} />
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
