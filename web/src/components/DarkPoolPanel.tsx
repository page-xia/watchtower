import { useState } from "react"
import { usePolling } from "@/hooks/usePolling"
import { getDarkPool } from "@/lib/api"
import { fmtAmount, fmtPct, pctClass } from "@/lib/format"
import type {
  DarkPoolBlockRow,
  DarkPoolEodRow,
  DarkPoolIntradayRow,
  DarkPoolSectorBucket,
} from "@/types/api"

const REFRESH_MS = 60000 // 独立慢轮询：后端本身 120s 一轮磁带，前端 60s 取缓存即可

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
    <div className="flex h-full min-h-[40px] items-center justify-center px-2 text-center text-[10px] leading-snug text-muted-foreground/70">
      {text}
    </div>
  )
}

type ViewMode = "l1" | "l2" | "l3" | "stock"

const VIEW_LABELS: Record<ViewMode, string> = { l1: "1级", l2: "2级", l3: "3级", stock: "个股" }

type RollupByLevel = { l1?: DarkPoolSectorBucket[]; l2?: DarkPoolSectorBucket[]; l3?: DarkPoolSectorBucket[] }

function rollupFor(mode: ViewMode, byLevel: RollupByLevel | undefined, fallback: DarkPoolSectorBucket[] | undefined): DarkPoolSectorBucket[] {
  if (mode === "stock") return []
  return (byLevel?.[mode] ?? fallback ?? []) as DarkPoolSectorBucket[]
}

function ViewToggle({ mode, onChange }: { mode: ViewMode; onChange: (m: ViewMode) => void }) {
  return (
    <span className="ml-auto flex shrink-0 overflow-hidden rounded border border-border text-[9px]">
      {(["l1", "l2", "l3", "stock"] as const).map((m) => (
        <button
          key={m}
          type="button"
          onClick={() => onChange(m)}
          className={`px-1.5 py-0.5 ${mode === m ? "bg-muted text-foreground" : "text-muted-foreground/70 hover:text-muted-foreground"}`}
        >
          {VIEW_LABELS[m]}
        </button>
      ))}
    </span>
  )
}

function ColHead({ title, note, mode, onMode }: { title: string; note?: string; mode?: ViewMode; onMode?: (m: ViewMode) => void }) {
  return (
    <div className="flex shrink-0 items-center gap-1.5 border-b border-border/60 px-2 py-1 text-[10px] font-semibold text-muted-foreground">
      <span className="truncate">{title}</span>
      {note && <span className="shrink-0 font-normal text-muted-foreground/60">{note}</span>}
      {mode && onMode && <ViewToggle mode={mode} onChange={onMode} />}
    </div>
  )
}

function IntradayRow({ row }: { row: DarkPoolIntradayRow }) {
  const tone = row.tag === "疑似暗吸" ? "up" : row.tag === "疑似派发" ? "down" : "mute"
  return (
    <div className="flex items-center gap-1.5 px-2 py-[3px] text-[11px] hover:bg-muted/40" title={`${row.code} 大单净占比 ${row.net_ratio_pct}%`}>
      <span className="min-w-0 flex-1 truncate">
        {row.name || row.code}
        {row.sector && <span className="ml-1 text-[9px] text-muted-foreground/60">{row.sector}</span>}
      </span>
      <span className={`num shrink-0 ${pctClass(row.net_amount)}`}>{fmtAmount(row.net_amount)}</span>
      <span className="num w-12 shrink-0 text-right text-muted-foreground/80">{fmtPct(row.change_pct)}</span>
      <TagChip text={row.tag} tone={tone} />
    </div>
  )
}

function SectorRow({ bucket }: { bucket: DarkPoolSectorBucket }) {
  return (
    <div
      className="flex items-center gap-1.5 px-2 py-[3px] text-[11px] hover:bg-muted/40"
      title={`龙头 ${bucket.top_name || "--"} ${fmtAmount(bucket.top_net)}`}
    >
      <span className="min-w-0 flex-1 truncate">
        {bucket.sector}
        <span className="ml-1 text-[9px] text-muted-foreground/60">{bucket.stock_count}只</span>
      </span>
      <span className={`num shrink-0 ${pctClass(bucket.net_amount)}`}>{fmtAmount(bucket.net_amount)}</span>
    </div>
  )
}

function EodRow({ row }: { row: DarkPoolEodRow }) {
  return (
    <div className="flex items-center gap-1.5 px-2 py-[3px] text-[11px] hover:bg-muted/40" title={row.code}>
      <span className="min-w-0 flex-1 truncate">
        {row.name || row.code}
        {row.sector && <span className="ml-1 text-[9px] text-muted-foreground/60">{row.sector}</span>}
      </span>
      <span className={`num shrink-0 ${pctClass(row.net_mf_amount)}`}>{fmtAmount(row.net_mf_amount)}</span>
      {row.on_top_list && <TagChip text="龙虎榜" tone="gold" />}
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
      <span className="min-w-0 flex-1 truncate">
        {row.name || row.code}
        {row.sector && <span className="ml-1 text-[9px] text-muted-foreground/60">{row.sector}</span>}
      </span>
      <span className="num shrink-0 text-foreground/90">{fmtAmount(row.amount)}</span>
      <TagChip
        text={`${discount ? "折价" : "溢价"}${Math.abs(row.premium_pct).toFixed(1)}%`}
        tone={discount ? "down" : "up"}
      />
      {row.on_top_list && <TagChip text="龙虎榜" tone="gold" />}
    </div>
  )
}

/**
 * 暗盘资金：盘中 L1 磁带大单推断（非隐藏单真值）+ 盘后 Tushare 官方口径校准。
 * 两列资金视图均支持 个股 / 板块 汇总切换；板块口径为板块内个股净额求和。
 * 独立 60s 轮询 /api/dark-pool，后端只读缓存/本地库，不进主流刷新链路。
 */
export function DarkPoolPanel() {
  const { data } = usePolling(() => getDarkPool(), REFRESH_MS, [])
  const [intradayMode, setIntradayMode] = useState<ViewMode>("l3")
  const [eodMode, setEodMode] = useState<ViewMode>("l1")
  const intraday = data?.intraday
  const eod = data?.eod
  const intradayRows = intraday?.rows ?? []
  const intradaySectors = rollupFor(intradayMode, intraday?.sector_rollup_by_level, intraday?.sector_rollup)
  const eodRows = [...(eod?.main_inflow ?? []), ...(eod?.main_outflow ?? [])]
  const eodSectors = rollupFor(eodMode, eod?.sector_rollup_by_level, eod?.sector_rollup)
  const blocks = eod?.block_trades ?? []

  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
        <div className="panel-title">暗盘资金</div>
        <span className="text-[9px] text-muted-foreground/80">
          大单印花拆出 · 盘后 Tushare 校准{data?.as_of ? ` · ${data.as_of}` : ""}
        </span>
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-1 divide-y divide-border/60 md:grid-cols-3 md:divide-x md:divide-y-0">
        <div className="flex min-h-0 flex-col">
          <ColHead
            title={`盘中大单推断${intraday?.refreshed_at ? ` · ${intraday.refreshed_at}` : ""}`}
            note={intraday?.pool_size ? `池${intraday.pool_size}只` : undefined}
            mode={intradayMode}
            onMode={setIntradayMode}
          />
          <div className="min-h-0 flex-1 overflow-y-auto py-0.5">
            {intradayMode === "stock" ? (
              intradayRows.length ? (
                intradayRows.slice(0, 10).map((r) => <IntradayRow key={r.code} row={r} />)
              ) : (
                <EmptyHint text={intraday?.note || "盘中每 2 分钟一轮，等待数据"} />
              )
            ) : intradaySectors.length ? (
              intradaySectors.map((b) => <SectorRow key={b.sector} bucket={b} />)
            ) : (
              <EmptyHint text={intraday?.note || "盘中每 2 分钟一轮，等待数据"} />
            )}
          </div>
        </div>
        <div className="flex min-h-0 flex-col">
          <ColHead
            title={`官方口径${eod?.trade_date ? ` · ${eod.trade_date}收盘` : ""}`}
            mode={eodMode}
            onMode={setEodMode}
          />
          <div className="min-h-0 flex-1 overflow-y-auto py-0.5">
            {eodMode === "stock" ? (
              eodRows.length ? (
                eodRows.map((r) => <EodRow key={`${r.code}-${r.net_mf_amount}`} row={r} />)
              ) : (
                <EmptyHint text={eod?.note || "等待收盘管线数据"} />
              )
            ) : eodSectors.length ? (
              eodSectors.map((b) => <SectorRow key={b.sector} bucket={b} />)
            ) : (
              <EmptyHint text={eod?.note || "等待收盘管线数据"} />
            )}
          </div>
        </div>
        <div className="flex min-h-0 flex-col">
          <ColHead title="大宗交易" note="暗盘成交" />
          <div className="min-h-0 flex-1 overflow-y-auto py-0.5">
            {blocks.length ? (
              blocks.map((r, i) => <BlockRow key={`${r.code}-${i}`} row={r} />)
            ) : (
              <EmptyHint text={eod?.note || "等待收盘管线数据"} />
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
