import { useState } from "react"
import { X } from "lucide-react"
import { useLiveChannel } from "@/hooks/useLiveChannel"
import { fmtAmount, fmtPct, pctClass } from "@/lib/format"
import { cn } from "@/lib/utils"
import type {
  DarkPoolAbsorbRow,
  DarkPoolBlockRow,
  DarkPoolEmRow,
  DarkPoolInstRow,
  DarkPoolNorthRow,
  DarkPoolSectorBucket,
  DarkPoolPayload,
} from "@/types/api"

function TagChip({ text, tone }: { text: string; tone: "up" | "down" | "gold" | "mute" }) {
  const cls =
    tone === "up"
      ? "border-[hsl(var(--up)/0.45)] bg-[hsl(var(--up-dim))] text-[hsl(var(--up))]"
      : tone === "down"
        ? "border-[hsl(var(--down)/0.45)] bg-[hsl(var(--down-dim))] text-[hsl(var(--down))]"
        : tone === "gold"
          ? "border-[hsl(var(--gold)/0.4)] bg-[hsl(var(--gold)/0.12)] text-[hsl(var(--gold))]"
          : "border-border bg-muted text-muted-foreground"
  return <span className={`shrink-0 rounded border px-1 text-[9px] leading-4 ${cls}`}>{text}</span>
}

function EmptyHint({ text }: { text: string }) {
  return (
    <div className="flex min-h-[40px] items-center justify-center px-2 py-3 text-center text-[10px] leading-snug text-muted-foreground/70">
      {text}
    </div>
  )
}

type EmLevel = "l1" | "l2" | "l3"

const LEVEL_LABELS: Record<EmLevel, string> = { l1: "1级", l2: "2级", l3: "3级" }
const LEVEL_NUM: Record<EmLevel, number> = { l1: 1, l2: 2, l3: 3 }

function LevelToggle({ mode, onChange }: { mode: EmLevel; onChange: (m: EmLevel) => void }) {
  return (
    <span className="ml-auto flex shrink-0 overflow-hidden rounded border border-border text-[9px]">
      {(["l1", "l2", "l3"] as const).map((m) => (
        <button
          key={m}
          type="button"
          onClick={() => onChange(m)}
          className={`px-1.5 py-0.5 ${mode === m ? "bg-muted text-foreground" : "text-muted-foreground/70 hover:text-muted-foreground"}`}
        >
          {LEVEL_LABELS[m]}
        </button>
      ))}
    </span>
  )
}

/** 卡片头部 tab（对齐 RightRail：active bg-accent，标签按资金方向着色，计数小徽章） */
function CardTab({
  label,
  count,
  tone,
  active,
  onClick,
  title,
}: {
  label: string
  count?: number
  tone?: "up" | "down" | "gold"
  active: boolean
  onClick: () => void
  title?: string
}) {
  const color = tone === "up" ? "text-up" : tone === "down" ? "text-down" : tone === "gold" ? "text-gold" : ""
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(
        "flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold transition-colors",
        active ? "bg-accent text-foreground" : "text-muted-foreground/70 hover:bg-muted hover:text-muted-foreground",
      )}
    >
      <span className={color}>{label}</span>
      {typeof count === "number" && <span className={cn("num rounded bg-muted px-1 text-[9px]", color)}>{count}</span>}
    </button>
  )
}

function StockName({ name, code, sector }: { name: string; code: string; sector?: string }) {
  return (
    <span className="min-w-0 flex-1 truncate">
      {name || code}
      {sector && <span className="ml-1 text-[9px] text-muted-foreground/60">{sector}</span>}
    </span>
  )
}

/** 暗吸/暗派行：多日同向净额 + 价格背离（「暗」= 资金连续动、价格没动） */
function AbsorbRow({ row, side }: { row: DarkPoolAbsorbRow; side: "in" | "out" }) {
  const sameDays = side === "in" ? row.pos_days : row.neg_days
  return (
    <div
      className="flex items-center gap-1.5 px-2 py-[3px] text-[11px] hover:bg-muted/40"
      title={`${row.code} · 近${row.days}日净额 ${fmtAmount(row.net_window)} · 区间${fmtPct(row.window_chg_pct)} · 日均换手 ${row.turnover_avg}%`}
    >
      <StockName name={row.name} code={row.code} sector={row.sector} />
      <span className="shrink-0 text-[9px] text-muted-foreground/80">
        {sameDays}/{row.days}天 · 价{fmtPct(row.window_chg_pct)}
      </span>
      <span className={`num shrink-0 ${pctClass(row.net_window)}`}>{fmtAmount(row.net_window)}</span>
    </div>
  )
}

function NorthRow({ row }: { row: DarkPoolNorthRow }) {
  return (
    <div
      className="flex items-center gap-1.5 px-2 py-[3px] text-[11px] hover:bg-muted/40"
      title={`${row.code} · 北向成交额（无买卖方向口径）`}
    >
      <StockName name={row.name} code={row.code} sector={row.sector} />
      <span className="num w-12 shrink-0 text-right text-muted-foreground/80">{fmtPct(row.change_pct)}</span>
      <span className="num shrink-0 text-foreground/90">{fmtAmount(row.amount)}</span>
    </div>
  )
}

function BlockRow({ row }: { row: DarkPoolBlockRow }) {
  const discount = row.premium_pct < 0
  return (
    <div
      className="flex items-center gap-1.5 px-2 py-[3px] text-[11px] hover:bg-muted/40"
      title={`${row.code} 成交价 ${row.price} · 收盘 ${row.close}`}
    >
      <StockName name={row.name} code={row.code} sector={row.sector} />
      <span className="num shrink-0 text-foreground/90">{fmtAmount(row.amount)}</span>
      <TagChip
        text={`${discount ? "折价" : "溢价"}${Math.abs(row.premium_pct).toFixed(1)}%`}
        tone={discount ? "down" : "up"}
      />
      {row.on_top_list && <TagChip text="龙虎榜" tone="gold" />}
    </div>
  )
}

function InstRow({ row }: { row: DarkPoolInstRow }) {
  return (
    <div
      className="flex items-center gap-1.5 px-2 py-[3px] text-[11px] hover:bg-muted/40"
      title={`${row.code} · 机构席位净买 ${fmtAmount(row.inst_net)} · 全部席位净额 ${fmtAmount(row.total_net)}（${row.seats} 席）`}
    >
      <StockName name={row.name} code={row.code} sector={row.sector} />
      <span className={`num shrink-0 ${pctClass(row.inst_net)}`}>{fmtAmount(row.inst_net)}</span>
      <TagChip text={`${row.seats}席`} tone="gold" />
    </div>
  )
}

function EmStockRow({ row }: { row: DarkPoolEmRow }) {
  return (
    <div
      className="flex items-center gap-1.5 px-2 py-[3px] text-[11px] hover:bg-muted/40"
      title={`${row.code} · 主力净占比 ${row.main_pct}% · 超大单 ${fmtAmount(row.elg_net)}`}
    >
      <StockName name={row.name} code={row.code} sector={row.sector} />
      <span className="num w-12 shrink-0 text-right text-muted-foreground/80">{fmtPct(row.change_pct)}</span>
      <span className={`num shrink-0 ${pctClass(row.main_net)}`}>{fmtAmount(row.main_net)}</span>
    </div>
  )
}

/** 左侧指标轨里的全宽指标卡 */
function RailChip({ label, value, tone, title }: { label: string; value: string; tone?: "up" | "down" | "flat"; title?: string }) {
  return (
    <div className="shrink-0 rounded border border-border/60 bg-background/40 px-1.5 pb-1 pt-[3px]" title={title}>
      <span className="mb-0.5 block text-[10px] leading-[11px] text-muted-foreground">{label}</span>
      <div
        className={cn(
          "num text-[11px] font-semibold leading-tight",
          tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-foreground/90",
        )}
      >
        {value}
      </div>
    </div>
  )
}

interface DarkPoolPanelProps {
  /** 首页当前选中板块（联动过滤个股级榜单） */
  selected?: string | null
  /** 首页板块口径：1/2/3 申万行业，4/5/6 概念/风格/地区 */
  boardLevel?: number
  /** 点击板块桶 → 联动首页选中对应级别板块；name=null 表示清除过滤 */
  onSelectSector?: (level: number, name: string | null) => void
}

/**
 * 暗盘资金：左侧指标轨纵排，右侧三张卡片并排占满高度，每张卡片头部 tab 切换子榜单。
 * ① 暗吸/暗派：Tushare moneyflow 多日窗口 × 价格背离（核心）；
 * ② 大手场外：北向十大成交（仅成交额）/ 大宗交易折溢价 / 龙虎榜机构席位；
 * ③ 盘中资金地图：东财 push2 全市场资金流（推断口径），板块归组走 easy_tdx 申万映射。
 * 走持久 /ws/live 的 dark_pool 频道（60s 服务端节奏），后端只读缓存/本地库。
 */
export function DarkPoolPanel({ selected = null, boardLevel = 3, onSelectSector }: DarkPoolPanelProps) {
  const { data } = useLiveChannel<DarkPoolPayload>("dark_pool", { sector: selected, boardLevel })
  const [absTab, setAbsTab] = useState<"in" | "out">("in")
  const [offTab, setOffTab] = useState<"north" | "block" | "inst">("north")
  const [emTab, setEmTab] = useState<"sector" | "in" | "out">("sector")
  const [emLevel, setEmLevel] = useState<EmLevel>("l3")
  const market = data?.market
  const absorb = data?.absorb
  const offmarket = data?.offmarket
  const em = data?.em
  const sectorFilter = data?.sector_filter ?? null
  const filtering = !!selected && (sectorFilter?.member_count ?? 0) > 0

  const emBuckets = (em?.sector_rollup_by_level?.[emLevel] ?? []) as DarkPoolSectorBucket[]

  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[150px_1fr_1fr_1.15fr]">
        {/* 左侧指标轨：面板名 + 联动 chip + 5 张指标卡纵排 */}
        <div className="flex min-h-0 flex-col gap-1 overflow-y-auto border-b border-border/60 p-1.5 md:border-b-0 md:border-r">
          <div className="flex shrink-0 items-center gap-1 px-0.5">
            <span className="panel-title">暗盘资金</span>
            {selected && (
              <button
                type="button"
                onClick={() => onSelectSector?.(boardLevel, null)}
                className="flex min-w-0 items-center gap-0.5 rounded border border-[hsl(var(--gold)/0.4)] bg-[hsl(var(--gold)/0.12)] px-1 text-[9px] leading-4 text-gold hover:bg-[hsl(var(--gold)/0.2)]"
                title="清除板块联动过滤"
              >
                <span className="truncate">联动:{selected}</span>
                {sectorFilter && <span className="num shrink-0">{sectorFilter.member_count}只</span>}
                <X className="h-2.5 w-2.5 shrink-0" />
              </button>
            )}
          </div>
          <RailChip
            label={`官方主力净额 ${market?.trade_date ?? "--"}`}
            value={market?.main_net_amount != null ? fmtAmount(market.main_net_amount) : "--"}
            tone={market?.main_net_amount == null ? "flat" : market.main_net_amount >= 0 ? "up" : "down"}
            title="Tushare moneyflow 全市场主力净额合计（收盘口径）"
          />
          <RailChip
            label={`盘中主力净额 ${em?.as_of ?? "--"}`}
            value={market?.em_main_net != null ? fmtAmount(market.em_main_net) : "--"}
            tone={market?.em_main_net == null ? "flat" : market.em_main_net >= 0 ? "up" : "down"}
            title="东财 push2 全市场主力净额合计（推断口径，盘中实时/收盘终值）"
          />
          <RailChip
            label={`北向成交额 ${market?.north_trade_date ?? "--"}`}
            value={market?.north_turnover != null ? fmtAmount(market.north_turnover) : "--"}
            title="沪深股通成交总额；2024-08 起交易所不再披露北向净额与个股方向"
          />
          <RailChip
            label={`融资余额Δ ${market?.margin_trade_date ?? "--"}`}
            value={market?.margin_change != null ? fmtAmount(market.margin_change) : "--"}
            tone={market?.margin_change == null ? "flat" : market.margin_change >= 0 ? "up" : "down"}
            title={`两市融资余额日变化（T+1 落地）；余额 ${market?.margin_balance != null ? fmtAmount(market.margin_balance) : "--"}`}
          />
          <RailChip
            label={`大宗成交 ${market?.trade_date ?? "--"}`}
            value={market?.block_amount != null ? fmtAmount(market.block_amount) : "--"}
            title="当日大宗交易成交总额（场外大手）"
          />
          <span className="mt-auto shrink-0 px-0.5 pt-1 text-[8px] leading-snug text-muted-foreground/60">
            暗吸=Tushare多日 · 场外=大宗/北向 · 地图=东财推断{data?.as_of ? ` · ${data.as_of}` : ""}
          </span>
        </div>

        {/* ① 暗吸 / 暗派：多日同向净额 + 价格滞涨抗跌 */}
        <div className="flex min-h-0 flex-col border-b border-border/60 md:border-b-0 md:border-r">
          <div className="flex shrink-0 items-center gap-0.5 border-b border-border/60 px-1.5 py-1">
            <CardTab
              label="暗吸"
              tone="up"
              count={absorb?.available ? (absorb.inflow ?? []).length : undefined}
              active={absTab === "in"}
              onClick={() => setAbsTab("in")}
              title="疑似暗吸 · 资金连续进 / 价格没动"
            />
            <CardTab
              label="暗派"
              tone="down"
              count={absorb?.available ? (absorb.outflow ?? []).length : undefined}
              active={absTab === "out"}
              onClick={() => setAbsTab("out")}
              title="疑似暗派 · 资金连续出 / 价格抗跌"
            />
            <span className="ml-auto shrink-0 truncate pl-1 text-[9px] text-muted-foreground/60">
              {absorb?.window_days ? `近${absorb.window_days}日` : ""}
              {filtering ? " · 已联动" : ""}
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto pb-0.5" title={absorb?.rule}>
            {absorb?.available ? (
              absTab === "in" ? (
                (absorb.inflow ?? []).length ? (
                  (absorb.inflow ?? []).map((r) => <AbsorbRow key={r.code} row={r} side="in" />)
                ) : (
                  <EmptyHint text="窗口内无暗吸标的" />
                )
              ) : (absorb.outflow ?? []).length ? (
                (absorb.outflow ?? []).map((r) => <AbsorbRow key={r.code} row={r} side="out" />)
              ) : (
                <EmptyHint text="窗口内无暗派标的" />
              )
            ) : (
              <EmptyHint text={absorb?.note || "等待收盘管线多日数据"} />
            )}
          </div>
        </div>

        {/* ② 大手场外：北向十大 / 大宗折溢价 / 龙虎榜机构 */}
        <div className="flex min-h-0 flex-col border-b border-border/60 md:border-b-0 md:border-r">
          <div className="flex shrink-0 items-center gap-0.5 border-b border-border/60 px-1.5 py-1">
            <CardTab
              label="北向"
              count={offmarket?.available ? (offmarket.north_top10 ?? []).length : undefined}
              active={offTab === "north"}
              onClick={() => setOffTab("north")}
              title={`北向十大成交 ${offmarket?.north_trade_date ?? ""}（仅成交额，无方向）`}
            />
            <CardTab
              label="大宗"
              count={offmarket?.available ? (offmarket.blocks ?? []).length : undefined}
              active={offTab === "block"}
              onClick={() => setOffTab("block")}
              title="大宗交易（折溢价）"
            />
            <CardTab
              label="龙虎榜"
              count={offmarket?.available ? (offmarket.top_inst ?? []).length : undefined}
              active={offTab === "inst"}
              onClick={() => setOffTab("inst")}
              title={`龙虎榜机构席位净买 ${offmarket?.inst_trade_date ?? ""}`}
            />
            <span className="ml-auto shrink-0 truncate pl-1 text-[9px] text-muted-foreground/60">
              {offmarket?.trade_date ?? ""}
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto pb-0.5">
            {offmarket?.available ? (
              offTab === "north" ? (
                (offmarket.north_top10 ?? []).length ? (
                  (offmarket.north_top10 ?? []).map((r) => <NorthRow key={r.code} row={r} />)
                ) : (
                  <EmptyHint text="当日无北向十大成交数据" />
                )
              ) : offTab === "block" ? (
                (offmarket.blocks ?? []).length ? (
                  (offmarket.blocks ?? []).map((r, i) => <BlockRow key={`${r.code}-${i}`} row={r} />)
                ) : (
                  <EmptyHint text="当日无大宗交易" />
                )
              ) : (offmarket.top_inst ?? []).length ? (
                (offmarket.top_inst ?? []).map((r) => <InstRow key={r.code} row={r} />)
              ) : (
                <EmptyHint text="当日无机构席位上榜" />
              )
            ) : (
              <EmptyHint text="等待收盘管线数据" />
            )}
          </div>
        </div>

        {/* ③ 盘中资金地图：板块桶可点击联动首页；流入/流出为个股榜 */}
        <div className="flex min-h-0 flex-col">
          <div className="flex shrink-0 items-center gap-0.5 border-b border-border/60 px-1.5 py-1">
            <CardTab
              label="板块"
              count={em?.available ? emBuckets.length : undefined}
              active={emTab === "sector"}
              onClick={() => setEmTab("sector")}
              title="主力净额板块桶（easy_tdx 申万映射归组）"
            />
            <CardTab
              label="流入"
              tone="up"
              count={em?.available ? (em.inflow ?? []).length : undefined}
              active={emTab === "in"}
              onClick={() => setEmTab("in")}
              title="主力净买 top（东财推断口径）"
            />
            <CardTab
              label="流出"
              tone="down"
              count={em?.available ? (em.outflow ?? []).length : undefined}
              active={emTab === "out"}
              onClick={() => setEmTab("out")}
              title="主力净卖 top（东财推断口径）"
            />
            {emTab === "sector" ? (
              <LevelToggle mode={emLevel} onChange={setEmLevel} />
            ) : (
              <span className="ml-auto shrink-0 truncate pl-1 text-[9px] text-muted-foreground/60">{em?.as_of ?? ""}</span>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto pb-0.5">
            {em?.available ? (
              emTab === "sector" ? (
                emBuckets.length ? (
                  emBuckets.map((b) => {
                    const level = LEVEL_NUM[emLevel]
                    const active = !!selected && boardLevel === level && selected === b.sector
                    return (
                      <button
                        key={b.sector}
                        type="button"
                        disabled={!onSelectSector}
                        onClick={() => onSelectSector?.(level, b.sector)}
                        className={cn(
                          "flex w-full items-center gap-1.5 px-2 py-[3px] text-left text-[11px]",
                          onSelectSector && "hover:bg-muted/40",
                          active && "bg-accent/70",
                        )}
                        title={`龙头 ${b.top_name || "--"} ${fmtAmount(b.top_net)}${onSelectSector ? "\n点击联动首页选中该板块" : ""}`}
                      >
                        <span className="min-w-0 flex-1 truncate">
                          {b.sector}
                          <span className="ml-1 text-[9px] text-muted-foreground/60">{b.stock_count}只</span>
                        </span>
                        <span className={`num shrink-0 ${pctClass(b.net_amount)}`}>{fmtAmount(b.net_amount)}</span>
                      </button>
                    )
                  })
                ) : (
                  <EmptyHint text={`${LEVEL_LABELS[emLevel]}板块暂无归组数据`} />
                )
              ) : emTab === "in" ? (
                (em.inflow ?? []).length ? (
                  (em.inflow ?? []).map((r) => <EmStockRow key={r.code} row={r} />)
                ) : (
                  <EmptyHint text="暂无主力净买标的" />
                )
              ) : (em.outflow ?? []).length ? (
                (em.outflow ?? []).map((r) => <EmStockRow key={r.code} row={r} />)
              ) : (
                <EmptyHint text="暂无主力净卖标的" />
              )
            ) : (
              <EmptyHint text={em?.note || "东财快照拉取中"} />
            )}
            {em?.stale_error && <div className="px-2 py-0.5 text-[9px] text-gold/80">快照降级中：{em.stale_error}</div>}
          </div>
        </div>
      </div>
    </section>
  )
}
