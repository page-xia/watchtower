import { memo } from "react"
import { Star, ChevronLeft, ChevronRight, Crown } from "lucide-react"
import type { BoardItem, SectorRank, StockBoard } from "@/types/api"
import { SignalBadge } from "@/components/widgets"
import {
  AmountCell,
  DayPositionCell,
  HeatCell,
  IdentityCell,
  OrderFlowCell,
  PriceCell,
  RatioCell,
  SparklineCell,
} from "@/components/boardCells"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "activity", label: "综合活跃度" },
  { value: "change", label: "涨跌幅" },
  { value: "amount", label: "成交额" },
  { value: "volume_ratio", label: "分钟量能" },
  { value: "order_flow", label: "盘口倾向" },
  { value: "signal", label: "买卖信号" },
]

interface StockBoardProps {
  board: StockBoard | null
  loading: boolean
  sort: string
  onSort: (s: string) => void
  page: number
  onPage: (p: number) => void
  nearTrend: boolean
  onToggleNearTrend: () => void
  pinBuy: boolean
  onTogglePinBuy: () => void
  onOpenDetail: (code: string) => void
  onToggleWatch: (item: BoardItem) => void
  sectorAnchor?: SectorRank | null
}

const StockRow = memo(function StockRow({
  item,
  onOpen,
  onToggleWatch,
}: {
  item: BoardItem
  onOpen: () => void
  onToggleWatch: () => void
}) {
  // 与「置顶买点」过滤同口径：最近信号为买T 且现价 ≤ 买点+1% 即金色高亮整行
  const nearBuyPoint =
    item.last_action === "买T" &&
    (item.last_action_price ?? 0) > 0 &&
    item.price > 0 &&
    item.price <= (item.last_action_price ?? 0) * 1.01
  return (
    <tr
      className={cn(
        "group cursor-pointer border-b border-border/50 transition-colors hover:bg-accent/40",
        item.watchlisted && "bg-[hsl(var(--gold)/0.04)]",
        nearBuyPoint && "bg-[hsl(var(--gold)/0.1)] hover:bg-[hsl(var(--gold)/0.16)]",
      )}
      title={nearBuyPoint ? `贴近买点 ${item.last_action_time || ""} @ ${item.last_action_price}（现价 ≤ 买点+1%）` : undefined}
      onClick={onOpen}
    >
      {/* 自选 */}
      <td className="w-7 pl-2">
        <button
          type="button"
          className="text-muted-foreground/40 transition-colors hover:text-gold"
          onClick={(e) => {
            e.stopPropagation()
            onToggleWatch()
          }}
          title={item.watchlisted ? "移出自选" : "加入自选"}
        >
          <Star className={cn("h-3.5 w-3.5", item.watchlisted && "fill-gold text-gold")} />
        </button>
      </td>
      {/* 股票 */}
      <IdentityCell item={item} />
      {/* 现价 */}
      <PriceCell price={item.price} changePct={item.change_pct} />
      {/* 分时：SVG 实际 112px，列宽声明需匹配（96px 会被内容撑开导致整表溢出） */}
      <SparklineCell mini={item.mini_chart} />
      {/* 信号 */}
      <td className="w-[86px] px-1 text-center">
        <SignalBadge signal={item.signal} score={item.signal_score} />
        <div className="num mt-0.5 text-[9px] text-muted-foreground/70">{item.signal_time || ""}</div>
      </td>
      {/* 量比 */}
      <RatioCell value={item.minute_amount_ratio} />
      {/* 大单 */}
      <OrderFlowCell flow={item.order_flow} />
      {/* 压力/支撑（含低吸徽标 + 距最近买卖点 ±%） */}
      <DayPositionCell
        price={item.price}
        resistance={item.resistance}
        support={item.support}
        nearZone={item.near_zone}
        lastAction={item.last_action}
        lastActionPrice={item.last_action_price}
        lastActionTime={item.last_action_time}
      />
      {/* 板块热度 */}
      <HeatCell score={item.sector_heat_score} />
      {/* 成交额 */}
      <AmountCell amount={item.amount} />
    </tr>
  )
})

export function StockBoardPanel({
  board,
  loading,
  sort,
  onSort,
  page,
  onPage,
  nearTrend,
  onToggleNearTrend,
  pinBuy,
  onTogglePinBuy,
  onOpenDetail,
  onToggleWatch,
  sectorAnchor,
}: StockBoardProps) {
  const items = board?.items ?? []
  const total = board?.total ?? 0
  const pageSize = board?.page_size ?? 40
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-2">
        <div className="flex items-center gap-2">
          <div>
            <div className="panel-title">全市场扫描</div>
            <h2 className="text-sm font-bold">
              {board?.selected_sector ? `${board.selected_sector} · 成分股` : "活跃股榜单"}
            </h2>
          </div>
          <span className="rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            扫描 <span className="num">{total}</span>
          </span>
          {sectorAnchor?.leader_name && (
            <span
              className="flex items-center gap-1 rounded border border-[hsl(var(--gold)/0.45)] bg-[hsl(var(--gold)/0.1)] px-1.5 py-0.5 text-[10px] text-gold"
              title={`板块领涨锚：${sectorAnchor.leader_name} · 以它为锚判断板块强弱`}
            >
              <Crown className="h-3 w-3" />
              领涨锚 {sectorAnchor.leader_name}
            </span>
          )}
          {board?.frozen && <span className="rounded border border-[hsl(var(--cyan)/0.4)] px-1.5 py-0.5 text-[10px] text-[hsl(var(--cyan))]">冻结</span>}
        </div>
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          {nearTrend && (board?.near_trend_pending ?? 0) > 0 && (
            <span className="num text-[10px] text-muted-foreground/80" title="短期/长期线值后台补算中，命中结果会随刷新逐步补齐">
              线值计算中 {board?.near_trend_pending}
            </span>
          )}
          <button
            type="button"
            onClick={onToggleNearTrend}
            title="低吸机会过滤：短期/长期线低者为底（-1%）、高者为顶（+3%），现价落在区间内"
            className={cn(
              "h-6 rounded border px-2 text-[11px] font-semibold transition-colors",
              nearTrend
                ? "border-[hsl(var(--gold)/0.55)] bg-[hsl(var(--gold)/0.12)] text-gold"
                : "border-input bg-secondary text-muted-foreground hover:text-foreground",
            )}
          >
            低吸机会{nearTrend && <span className="num"> · {board?.total ?? 0}</span>}
          </button>
          <button
            type="button"
            onClick={onTogglePinBuy}
            title="置顶买点过滤：只保留当天最近信号为买T、且现价 ≤ 买点+1% 的票"
            className={cn(
              "h-6 rounded border px-2 text-[11px] font-semibold transition-colors",
              pinBuy
                ? "border-[hsl(var(--gold)/0.55)] bg-[hsl(var(--gold)/0.12)] text-gold"
                : "border-input bg-secondary text-muted-foreground hover:text-foreground",
            )}
          >
            置顶买点{pinBuy && <span className="num"> · {board?.total ?? 0}</span>}
          </button>
          <span>更新 <span className="num">{board?.updated_at || "--"}</span></span>
          <select
            value={sort}
            onChange={(e) => onSort(e.target.value)}
            className="h-6 rounded border border-input bg-secondary px-1.5 text-[11px] text-foreground outline-none"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-auto">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 z-10 bg-card">
            <tr className="border-b border-border text-left text-[10px] text-muted-foreground">
              <th className="w-7 pl-2 font-normal" />
              <th className="py-1 pr-2 font-normal">股票</th>
              <th className="w-[72px] pr-2 text-right font-normal">现价/涨幅</th>
              <th className="w-[120px] px-1 font-normal">分时</th>
              <th className="w-[86px] px-1 text-center font-normal">信号</th>
              <th className="w-[56px] px-1 text-right font-normal">量比</th>
              <th className="w-[60px] px-1 text-right font-normal">大单</th>
              <th className="w-[110px] px-2 font-normal">压/支</th>
              <th className="w-[50px] px-1 text-right font-normal">板块热</th>
              <th className="w-[70px] pl-1 pr-2 text-right font-normal">成交额</th>
            </tr>
          </thead>
          <tbody className={cn(loading && items.length === 0 && "opacity-40")}>
            {items.map((item) => (
              <StockRow
                key={item.code}
                item={item}
                onOpen={() => onOpenDetail(item.code)}
                onToggleWatch={() => onToggleWatch(item)}
              />
            ))}
            {items.length === 0 && !loading && (
              <tr>
                <td colSpan={10} className="p-8 text-center text-xs text-muted-foreground">
                  无真实数据
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <footer className="flex shrink-0 items-center justify-between border-t border-border px-3 py-1.5 text-[11px] text-muted-foreground">
        <span>
          {pinBuy ? "贴近买点" : nearTrend ? "低吸命中" : "全市场扫描"} <span className="num">{total}</span> 只 · 当前页 {items.length} 只
        </span>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="sm" className="h-6 w-6 p-0" disabled={page <= 1} onClick={() => onPage(page - 1)}>
            <ChevronLeft className="h-3.5 w-3.5" />
          </Button>
          <span className="num px-1">{page} / {totalPages}</span>
          <Button variant="outline" size="sm" className="h-6 w-6 p-0" disabled={page >= totalPages} onClick={() => onPage(page + 1)}>
            <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </div>
      </footer>
    </section>
  )
}
