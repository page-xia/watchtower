import { useMemo } from "react"
import { useLiveChannel } from "@/hooks/useLiveChannel"
import { fmtAmount, fmtPct, pctClass } from "@/lib/format"
import { chartPalette, useTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"
import type { DarkPoolStockPayload } from "@/types/api"

const POLL_SECONDS = 5 // 本地库/缓存读取很轻；payload 不变时服务端不会发送消息

const VERDICT_TONE: Record<string, "up" | "down" | "gold" | "mute"> = {
  疑似暗吸: "up",
  疑似暗派: "down",
  主力净入: "gold",
  主力净出: "gold",
}

function VerdictChip({ label }: { label: string }) {
  const tone = VERDICT_TONE[label] ?? "mute"
  const cls =
    tone === "up"
      ? "border-[hsl(var(--up)/0.45)] bg-[hsl(var(--up-dim))] text-[hsl(var(--up))]"
      : tone === "down"
        ? "border-[hsl(var(--down)/0.45)] bg-[hsl(var(--down-dim))] text-[hsl(var(--down))]"
        : tone === "gold"
          ? "border-[hsl(var(--gold)/0.4)] bg-[hsl(var(--gold)/0.12)] text-[hsl(var(--gold))]"
          : "border-border bg-muted text-muted-foreground"
  return <span className={cn("shrink-0 rounded border px-1.5 text-[10px] font-semibold leading-5", cls)}>{label}</span>
}

function StatChip({ label, value, className, title }: { label: string; value: string; className?: string; title?: string }) {
  return (
    <div className="rounded border border-border/60 bg-background/40 px-1.5 py-1" title={title}>
      <div className="text-[8px] text-muted-foreground">{label}</div>
      <div className={cn("num text-[10px] font-semibold", className)}>{value}</div>
    </div>
  )
}

/** 近 10 日主力净额迷你柱图：红=净入 绿=净出，零轴居中 */
function FlowBars({ data }: { data: NonNullable<DarkPoolStockPayload["flow_10d"]> }) {
  const theme = useTheme()
  const pal = useMemo(() => chartPalette(theme), [theme])
  if (!data.length) return null
  const W = 320
  const H = 54
  const zeroY = H / 2
  const max = Math.max(...data.map((d) => Math.abs(d.net)), 1)
  const slot = W / data.length
  const barW = Math.max(4, slot * 0.55)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block h-[54px] w-full" preserveAspectRatio="none">
      <line x1={0} x2={W} y1={zeroY} y2={zeroY} stroke={pal.zeroLine} strokeWidth={1} strokeDasharray="3 3" />
      {data.map((d, i) => {
        const h = (Math.abs(d.net) / max) * (H / 2 - 3)
        const up = d.net >= 0
        return (
          <rect
            key={d.trade_date}
            x={i * slot + (slot - barW) / 2}
            y={up ? zeroY - h : zeroY}
            width={barW}
            height={Math.max(1, h)}
            rx={1}
            fill={up ? pal.upA(0.85) : pal.downA(0.85)}
          >
            <title>{`${d.trade_date} 主力净额 ${fmtAmount(d.net)} · 收 ${d.close || "--"} · 换手 ${d.turnover || "--"}%`}</title>
          </rect>
        )
      })}
    </svg>
  )
}

/**
 * 个股暗盘资金（详情页右栏，筹码峰下方）：
 * 多日资金流判定（暗吸/暗派）+ 三口径交叉（Tushare/同花顺/东财）+ 场外大手
 * （大宗/北向十大/龙虎榜）+ 两融。全部本地库 + 东财快照，零行情请求。
 */
export function DarkPoolStockPane({ code }: { code: string }) {
  const { data } = useLiveChannel<DarkPoolStockPayload>("dark_pool_stock", {
    code,
    intervalSeconds: POLL_SECONDS,
  })

  if (!data) {
    return <div className="p-3 text-center text-[10px] text-muted-foreground">暗盘资金加载中…</div>
  }
  if (!data.available || data.eod_available === false) {
    return (
      <div className="p-3 text-center text-[10px] text-muted-foreground">
        {data.note || "暗盘资金暂无数据"}
        {data.pending && <div className="mt-1 text-muted-foreground/70">等待本地管线推送，自动刷新中…</div>}
      </div>
    )
  }

  const verdict = data.verdict
  const flow = data.flow_10d ?? []
  const blocks = data.blocks ?? []
  const topList = data.top_list ?? []

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 px-3 py-1.5">
        <span className="panel-title">暗盘资金</span>
        {verdict && <VerdictChip label={verdict.label} />}
        <span className="ml-auto text-[9px] text-muted-foreground/80">{data.trade_date} 收盘口径</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-2">
        {verdict && (
          <div className="mb-1 text-[9px] leading-snug text-muted-foreground">
            近{verdict.days}日净额{" "}
            <span className={cn("num font-semibold", pctClass(verdict.net_window))}>{fmtAmount(verdict.net_window)}</span>
            （{verdict.pos_days} 天净入）· 区间 {fmtPct(verdict.window_chg_pct)}
          </div>
        )}
        <FlowBars data={flow} />
        <div className="mt-1.5 flex flex-wrap gap-1">
          {data.em && (
            <StatChip
              label={`盘中主力 ${data.em.as_of}`}
              value={fmtAmount(data.em.main_net)}
              className={pctClass(data.em.main_net)}
              title={`东财盘中口径（推断）：主力净占比 ${data.em.main_pct}% · 超大单 ${fmtAmount(data.em.elg_net)}`}
            />
          )}
          {data.ths && (
            <StatChip
              label="同花顺 5 日净额"
              value={fmtAmount(data.ths.net_d5)}
              className={pctClass(data.ths.net_d5)}
              title={`同花顺口径：当日 ${fmtAmount(data.ths.net_today)}`}
            />
          )}
          {data.dc && (
            <StatChip
              label="东财 EOD 净额"
              value={fmtAmount(data.dc.net_today)}
              className={pctClass(data.dc.net_today)}
              title="东财官方收盘口径，与盘中快照互验"
            />
          )}
          {data.north_top10 && (
            <StatChip
              label={`北向十大 ${data.north_top10.trade_date}`}
              value={fmtAmount(data.north_top10.amount)}
              title="上榜北向十大成交股（仅成交额，2024-08 起无方向口径）"
            />
          )}
          {data.margin && (
            <StatChip
              label={`融资余额Δ ${data.margin.trade_date}`}
              value={data.margin.rzye_change != null ? fmtAmount(data.margin.rzye_change) : "--"}
              className={pctClass(data.margin.rzye_change ?? 0)}
              title={`融资余额 ${fmtAmount(data.margin.rzye)}（T+1 落地）`}
            />
          )}
        </div>
        {blocks.length > 0 && (
          <div className="mt-1.5">
            <div className="text-[9px] font-semibold text-muted-foreground/70">大宗交易（近 5 笔）</div>
            {blocks.map((b, i) => (
              <div key={`${b.trade_date}-${i}`} className="flex items-center gap-1.5 py-[2px] text-[10px]">
                <span className="num text-muted-foreground">{b.trade_date}</span>
                <span className="num min-w-0 flex-1">{fmtAmount(b.amount)}</span>
                <span className={cn("num shrink-0", b.premium_pct < 0 ? "text-down" : "text-up")}>
                  {b.premium_pct < 0 ? "折价" : "溢价"}
                  {Math.abs(b.premium_pct).toFixed(1)}%
                </span>
              </div>
            ))}
          </div>
        )}
        {topList.length > 0 && (
          <div className="mt-1.5">
            <div className="text-[9px] font-semibold text-muted-foreground/70">近期龙虎榜</div>
            {topList.map((t, i) => (
              <div key={`${t.trade_date}-${i}`} className="flex items-center gap-1.5 py-[2px] text-[10px]">
                <span className="num shrink-0 text-muted-foreground">{t.trade_date}</span>
                <span className="min-w-0 flex-1 truncate text-foreground/80" title={t.reason}>
                  {t.reason || "上榜"}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
