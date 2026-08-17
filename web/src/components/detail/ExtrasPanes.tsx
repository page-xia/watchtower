import { useMemo } from "react"
import type { ReactNode } from "react"
import type { EChartsOption } from "echarts"
import type { ExtrasSection } from "@/types/api"
import { fmtAmount, fmtPrice } from "@/lib/format"
import { chartPalette, useTheme } from "@/lib/theme"
import { useECharts } from "@/hooks/useECharts"
import { cn } from "@/lib/utils"

/** 资金流/技术指标/缠论三个详情面板的专用排版。
 *  设计原则：摘要卡（结论先行）→ 趋势图（历史形态）→ 明细表（原始数据），
 *  配色一律走主题 CSS 变量 / chartPalette，红=流入/涨/买点，绿=流出/跌/卖点。
 */

type Row = Record<string, unknown>

function num(v: unknown): number | null {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

function str(v: unknown): string {
  return v == null || v === "" ? "--" : String(v)
}

/** 净流入红 / 净流出绿 / 零灰 */
function netClass(v: number | null): string {
  if (v == null || v === 0) return "text-flat"
  return v > 0 ? "text-up" : "text-down"
}

function NetCard({ label, value, sub }: { label: string; value: number | null; sub?: string }) {
  return (
    <div className="rounded-md border border-border/60 bg-background/40 px-2.5 py-1.5">
      <div className="text-[9px] text-muted-foreground">{label}</div>
      <div className={cn("num text-[13px] font-bold", netClass(value))}>
        {value == null ? "--" : fmtAmount(value)}
      </div>
      {sub && <div className="text-[9px] text-muted-foreground/70">{sub}</div>}
    </div>
  )
}

function BlockTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-1.5 flex items-center justify-between gap-2">
      <span className="panel-title">{children}</span>
      {right}
    </div>
  )
}

function EmptyState({ text }: { text: string }) {
  return <div className="p-8 text-center text-xs text-muted-foreground">{text}</div>
}

// ---------------------------------------------------------------------------
// 资金流
// ---------------------------------------------------------------------------

export function CapitalFlowPane({ section }: { section: ExtrasSection | null | undefined }) {
  const theme = useTheme()
  const pal = useMemo(() => chartPalette(theme), [theme])

  const summary = section?.summary ?? {}
  const tables = section?.tables ?? []
  const latestRow: Row = tables[0]?.rows?.[tables[0].rows.length - 1] ?? {}
  const history: Row[] = useMemo(() => tables[1]?.rows ?? [], [tables])

  const option = useMemo<EChartsOption | null>(() => {
    if (!history.length) return null
    const dates = history.map((r) => str(r.date).slice(5))
    const nets = history.map((r) => num(r.main_net) ?? 0)
    return {
      grid: { left: 44, right: 8, top: 10, bottom: 18 },
      tooltip: {
        trigger: "axis",
        backgroundColor: pal.tooltipBg,
        borderColor: pal.tooltipBorder,
        textStyle: { color: pal.tooltipText, fontSize: 10 },
        valueFormatter: (v: unknown) => fmtAmount(Number(v)),
      },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: pal.grid } },
        axisLabel: { color: pal.axis, fontSize: 9, interval: Math.ceil(dates.length / 8) },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: pal.grid } },
        axisLabel: { color: pal.axis, fontSize: 9, formatter: (v: number) => fmtAmount(v) },
      },
      series: [
        {
          type: "bar",
          name: "主力净额",
          data: nets.map((v) => ({
            value: v,
            itemStyle: { color: v >= 0 ? pal.up : pal.down },
          })),
          barMaxWidth: 10,
        },
      ],
    }
  }, [history, pal])
  const chartRef = useECharts(option)

  if (!section || !section.available) {
    return <EmptyState text={section?.note || "暂无资金流数据"} />
  }

  const mainIn = num(latestRow.main_in)
  const mainOut = num(latestRow.main_out)
  const inOutTotal = (mainIn ?? 0) + (mainOut ?? 0)

  return (
    <div className="space-y-4 p-3">
      {/* 第一层：最新一期结论卡 */}
      <section>
        <BlockTitle right={<span className="text-[9px] text-muted-foreground">easy_tdx · {str(summary.history_latest_date ?? summary.latest_date)}</span>}>
          最新一期资金结构
        </BlockTitle>
        <div className="grid grid-cols-4 gap-2 max-md:grid-cols-2">
          <NetCard label="主力净额" value={num(summary.main_net)} />
          <NetCard label="大单净额" value={num(summary.large_net)} />
          <NetCard label="中单净额" value={num(summary.mid_net)} />
          <NetCard label="小单净额" value={num(summary.small_net)} />
        </div>
        {inOutTotal > 0 && (
          <div className="mt-2 space-y-1">
            {[
              { label: "主力流入", value: mainIn, cls: "bg-up" },
              { label: "主力流出", value: mainOut, cls: "bg-down" },
            ].map((bar) => (
              <div key={bar.label} className="flex items-center gap-2 text-[10px]">
                <span className="w-14 shrink-0 text-muted-foreground">{bar.label}</span>
                <div className="h-2 min-w-0 flex-1 rounded bg-muted/60">
                  <div
                    className={cn("h-full rounded", bar.cls)}
                    style={{ width: `${Math.max(2, ((bar.value ?? 0) / inOutTotal) * 100)}%` }}
                  />
                </div>
                <span className="num w-16 shrink-0 text-right">{fmtAmount(bar.value)}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 第二层：历史趋势 */}
      {history.length > 0 && (
        <section>
          <BlockTitle right={<span className="text-[9px] text-muted-foreground">tushare moneyflow · 主力=特大单+大单</span>}>
            历史主力动向
          </BlockTitle>
          <div className="mb-2 grid grid-cols-3 gap-2">
            <NetCard label="近5日主力净额" value={num(summary.main_net_5d)} />
            <NetCard label="近10日主力净额" value={num(summary.main_net_10d)} />
            <div className="rounded-md border border-border/60 bg-background/40 px-2.5 py-1.5">
              <div className="text-[9px] text-muted-foreground">近10日净流入天数</div>
              <div className="num text-[13px] font-bold text-foreground">
                {str(summary.inflow_days_10d)}<span className="text-[9px] text-muted-foreground"> / 10 天</span>
              </div>
            </div>
          </div>
          <div ref={chartRef} className="h-[150px] w-full" />
          <div className="mt-2 overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border text-[9px] text-muted-foreground">
                  <th className="py-1 text-left font-normal">日期</th>
                  <th className="text-right font-normal">主力净额</th>
                  <th className="text-right font-normal">特大单</th>
                  <th className="text-right font-normal">大单</th>
                  <th className="text-right font-normal">中单</th>
                  <th className="text-right font-normal">小单</th>
                </tr>
              </thead>
              <tbody>
                {[...history].reverse().slice(0, 15).map((r, i) => (
                  <tr key={i} className="border-b border-border/40 text-[10px]">
                    <td className="num py-1 text-muted-foreground">{str(r.date)}</td>
                    {(["main_net", "elg_net", "lg_net", "md_net", "sm_net"] as const).map((k) => (
                      <td key={k} className={cn("num py-1 text-right", netClass(num(r[k])), k === "main_net" && "font-semibold")}>
                        {fmtAmount(num(r[k]))}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {section.note && <p className="text-[9px] leading-relaxed text-muted-foreground/60">{section.note}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 技术指标
// ---------------------------------------------------------------------------

function IndicatorCard({ title, values, status, statusCls }: {
  title: string
  values: { label: string; value: string }[]
  status?: string
  statusCls?: string
}) {
  return (
    <div className="rounded-md border border-border/60 bg-background/40 px-2.5 py-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold text-foreground/90">{title}</span>
        {status && <span className={cn("rounded px-1 py-0.5 text-[9px] font-semibold", statusCls)}>{status}</span>}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
        {values.map((v) => (
          <span key={v.label} className="text-[10px] text-muted-foreground">
            {v.label} <b className="num font-semibold text-foreground">{v.value}</b>
          </span>
        ))}
      </div>
    </div>
  )
}

export function IndicatorsPane({ section }: { section: ExtrasSection | null | undefined }) {
  const theme = useTheme()
  const pal = useMemo(() => chartPalette(theme), [theme])
  const rows: Row[] = useMemo(() => section?.tables?.[0]?.rows ?? [], [section])

  const latest = rows[rows.length - 1] ?? {}
  const prev = rows[rows.length - 2] ?? {}
  const dif = num(latest.MACD_DIF)
  const dea = num(latest.MACD_DEA)
  const prevDif = num(prev.MACD_DIF)
  const prevDea = num(prev.MACD_DEA)
  const k = num(latest.KDJ_K)
  const d = num(latest.KDJ_D)
  const j = num(latest.KDJ_J)
  const rsi = num(latest.RSI)
  const close = num(latest.close)
  const bollUp = num(latest.BOLL_UPPER)
  const bollMid = num(latest.BOLL_MID)
  const bollLow = num(latest.BOLL_LOWER)

  const macdGolden = dif != null && dea != null && prevDif != null && prevDea != null && prevDif <= prevDea && dif > dea
  const macdDead = dif != null && dea != null && prevDif != null && prevDea != null && prevDif >= prevDea && dif < dea
  const macdStatus = macdGolden ? "金叉" : macdDead ? "死叉" : dif != null && dea != null ? (dif > dea ? "多头" : "空头") : undefined
  const kdjStatus = j != null ? (j >= 90 ? "超买" : j <= 10 ? "超卖" : k != null && d != null ? (k > d ? "向上" : "向下") : undefined) : undefined
  const rsiStatus = rsi != null ? (rsi >= 70 ? "偏热" : rsi <= 30 ? "超卖" : "中性") : undefined
  const pctB = close != null && bollUp != null && bollLow != null && bollUp !== bollLow
    ? (close - bollLow) / (bollUp - bollLow)
    : null
  const bollStatus = pctB != null ? (pctB >= 1 ? "破上轨" : pctB <= 0 ? "破下轨" : pctB >= 0.8 ? "近上轨" : pctB <= 0.2 ? "近下轨" : "轨道内") : undefined

  const macdOption = useMemo<EChartsOption | null>(() => {
    if (!rows.length) return null
    const view = rows.slice(-60)
    const dates = view.map((r) => str(r.datetime).slice(5, 10))
    return {
      grid: { left: 40, right: 8, top: 18, bottom: 16 },
      legend: { textStyle: { color: pal.axis, fontSize: 9 }, top: 0, itemWidth: 10, itemHeight: 2 },
      tooltip: { trigger: "axis", backgroundColor: pal.tooltipBg, borderColor: pal.tooltipBorder, textStyle: { color: pal.tooltipText, fontSize: 10 } },
      xAxis: { type: "category", data: dates, axisLine: { lineStyle: { color: pal.grid } }, axisLabel: { color: pal.axis, fontSize: 9, interval: Math.ceil(dates.length / 6) } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: pal.grid } }, axisLabel: { color: pal.axis, fontSize: 9 } },
      series: [
        {
          type: "bar", name: "HIST",
          data: view.map((r) => { const v = num(r.MACD_HIST) ?? 0; return { value: v, itemStyle: { color: v >= 0 ? pal.upA(0.7) : pal.downA(0.7) } } }),
          barMaxWidth: 6,
        },
        { type: "line", name: "DIF", data: view.map((r) => num(r.MACD_DIF)), showSymbol: false, lineStyle: { width: 1, color: pal.gold } },
        { type: "line", name: "DEA", data: view.map((r) => num(r.MACD_DEA)), showSymbol: false, lineStyle: { width: 1, color: pal.cyan } },
      ],
    }
  }, [rows, pal])
  const kdjOption = useMemo<EChartsOption | null>(() => {
    if (!rows.length) return null
    const view = rows.slice(-60)
    const dates = view.map((r) => str(r.datetime).slice(5, 10))
    return {
      grid: { left: 32, right: 8, top: 18, bottom: 16 },
      legend: { textStyle: { color: pal.axis, fontSize: 9 }, top: 0, itemWidth: 10, itemHeight: 2 },
      tooltip: { trigger: "axis", backgroundColor: pal.tooltipBg, borderColor: pal.tooltipBorder, textStyle: { color: pal.tooltipText, fontSize: 10 } },
      xAxis: { type: "category", data: dates, axisLine: { lineStyle: { color: pal.grid } }, axisLabel: { color: pal.axis, fontSize: 9, interval: Math.ceil(dates.length / 6) } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: pal.grid } }, axisLabel: { color: pal.axis, fontSize: 9 } },
      series: [
        { type: "line", name: "K", data: view.map((r) => num(r.KDJ_K)), showSymbol: false, lineStyle: { width: 1, color: pal.up } },
        { type: "line", name: "D", data: view.map((r) => num(r.KDJ_D)), showSymbol: false, lineStyle: { width: 1, color: pal.cyan } },
        { type: "line", name: "J", data: view.map((r) => num(r.KDJ_J)), showSymbol: false, lineStyle: { width: 1, color: pal.gold } },
      ],
    }
  }, [rows, pal])
  const macdRef = useECharts(macdOption)
  const kdjRef = useECharts(kdjOption)

  if (!section || !section.available || !rows.length) {
    return <EmptyState text={section?.note || "暂无技术指标数据"} />
  }

  const fx = (v: number | null, digits = 2) => (v == null ? "--" : v.toFixed(digits))

  return (
    <div className="space-y-4 p-3">
      {/* 第一层：最新状态卡 */}
      <section>
        <BlockTitle right={<span className="num text-[9px] text-muted-foreground">{str(latest.datetime).slice(0, 10)} · 收盘 {fmtPrice(close)}</span>}>
          指标状态
        </BlockTitle>
        <div className="grid grid-cols-2 gap-2">
          <IndicatorCard
            title="MACD" status={macdStatus}
            statusCls={macdStatus === "金叉" || macdStatus === "多头" ? "bg-up-dim text-up" : "bg-down-dim text-down"}
            values={[{ label: "DIF", value: fx(dif, 3) }, { label: "DEA", value: fx(dea, 3) }, { label: "柱", value: fx(num(latest.MACD_HIST), 3) }]}
          />
          <IndicatorCard
            title="KDJ" status={kdjStatus}
            statusCls={kdjStatus === "超买" ? "bg-down-dim text-down" : kdjStatus === "超卖" ? "bg-up-dim text-up" : "bg-muted text-muted-foreground"}
            values={[{ label: "K", value: fx(k) }, { label: "D", value: fx(d) }, { label: "J", value: fx(j) }]}
          />
          <IndicatorCard
            title="RSI" status={rsiStatus}
            statusCls={rsiStatus === "偏热" ? "bg-down-dim text-down" : rsiStatus === "超卖" ? "bg-up-dim text-up" : "bg-muted text-muted-foreground"}
            values={[{ label: "RSI", value: fx(rsi) }, { label: "OBV", value: fmtAmount(num(latest.OBV)) }, { label: "ATR", value: fx(num(latest.ATR)) }]}
          />
          <IndicatorCard
            title="BOLL" status={bollStatus}
            statusCls={bollStatus === "破上轨" || bollStatus === "近上轨" ? "bg-up-dim text-up" : bollStatus === "破下轨" || bollStatus === "近下轨" ? "bg-down-dim text-down" : "bg-muted text-muted-foreground"}
            values={[{ label: "上", value: fx(bollUp) }, { label: "中", value: fx(bollMid) }, { label: "下", value: fx(bollLow) }, { label: "%B", value: pctB == null ? "--" : `${(pctB * 100).toFixed(0)}%` }]}
          />
        </div>
      </section>

      {/* 第二层：近 60 日形态 */}
      <section>
        <BlockTitle>近 60 日形态</BlockTitle>
        <div className="grid grid-cols-2 gap-2 max-lg:grid-cols-1">
          <div className="rounded-md border border-border/60 bg-background/40 p-1.5">
            <div className="px-1 text-[9px] text-muted-foreground">MACD</div>
            <div ref={macdRef} className="h-[140px] w-full" />
          </div>
          <div className="rounded-md border border-border/60 bg-background/40 p-1.5">
            <div className="px-1 text-[9px] text-muted-foreground">KDJ</div>
            <div ref={kdjRef} className="h-[140px] w-full" />
          </div>
        </div>
      </section>

      {/* 第三层：明细 */}
      <section>
        <BlockTitle>近 10 日明细</BlockTitle>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border text-[9px] text-muted-foreground">
                {["日期", "收盘", "DIF", "DEA", "柱", "K", "D", "J", "RSI"].map((c) => (
                  <th key={c} className="py-1 pr-2 text-right font-normal first:text-left">{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[...rows].reverse().slice(0, 10).map((r, i) => (
                <tr key={i} className="border-b border-border/40 text-[10px]">
                  <td className="num py-1 pr-2 text-muted-foreground">{str(r.datetime).slice(0, 10)}</td>
                  <td className="num py-1 pr-2 text-right">{fx(num(r.close))}</td>
                  <td className="num py-1 pr-2 text-right">{fx(num(r.MACD_DIF), 3)}</td>
                  <td className="num py-1 pr-2 text-right">{fx(num(r.MACD_DEA), 3)}</td>
                  <td className={cn("num py-1 pr-2 text-right", netClass(num(r.MACD_HIST)))}>{fx(num(r.MACD_HIST), 3)}</td>
                  <td className="num py-1 pr-2 text-right">{fx(num(r.KDJ_K))}</td>
                  <td className="num py-1 pr-2 text-right">{fx(num(r.KDJ_D))}</td>
                  <td className={cn("num py-1 pr-2 text-right", (num(r.KDJ_J) ?? 50) >= 90 ? "text-down" : (num(r.KDJ_J) ?? 50) <= 10 ? "text-up" : "")}>{fx(num(r.KDJ_J))}</td>
                  <td className="num py-1 pr-2 text-right">{fx(num(r.RSI))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {section.note && <p className="text-[9px] leading-relaxed text-muted-foreground/60">{section.note}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 缠论
// ---------------------------------------------------------------------------

const MMD_LABEL: Record<string, string> = {
  "1buy": "一买", "2buy": "二买", "3buy": "三买",
  "1sell": "一卖", "2sell": "二卖", "3sell": "三卖",
}

function mmdBadge(type: unknown): { label: string; cls: string } {
  const t = str(type)
  const label = MMD_LABEL[t] ?? t
  const isBuy = t.toLowerCase().includes("buy")
  return { label, cls: isBuy ? "bg-up-dim text-up" : "bg-down-dim text-down" }
}

export function ChanlunPane({ section }: { section: ExtrasSection | null | undefined }) {
  const summary = section?.summary ?? {}
  const tables = section?.tables ?? []
  const byTitle = useMemo(() => {
    const map: Record<string, Row[]> = {}
    for (const t of tables) map[t.title] = t.rows ?? []
    return map
  }, [tables])

  if (!section || !section.available) {
    return <EmptyState text={section?.note || "暂无缠论数据"} />
  }

  const mmds = [...(byTitle["买卖点"] ?? [])].reverse()
  const bcs = [...(byTitle["背驰"] ?? [])].reverse()
  const bis = [...(byTitle["笔"] ?? [])].reverse()
  const zss = [...(byTitle["中枢"] ?? [])].reverse()
  const latestBi = bis[0]
  const latestZs = zss[0]
  const unconfirmed = num(summary.unconfirmed_kline_count) ?? 0

  return (
    <div className="space-y-4 p-3">
      {/* 第一层：当前结构状态 */}
      <section>
        <BlockTitle right={<span className="num text-[9px] text-muted-foreground">K线至 {str(summary.kline_latest_date)} · 收盘 {fmtPrice(num(summary.kline_latest_close))}</span>}>
          当前结构
        </BlockTitle>
        <div className="grid grid-cols-3 gap-2 max-lg:grid-cols-1">
          <div className="rounded-md border border-border/60 bg-background/40 px-2.5 py-1.5">
            <div className="text-[9px] text-muted-foreground">最新一笔</div>
            {latestBi ? (
              <div className="mt-0.5 text-[11px]">
                <span className={cn("rounded px-1 py-0.5 text-[10px] font-bold", str(latestBi.direction) === "up" ? "bg-up-dim text-up" : "bg-down-dim text-down")}>
                  {str(latestBi.direction) === "up" ? "向上笔" : "向下笔"}
                </span>
                <span className="num ml-1.5 text-muted-foreground">{str(latestBi.start_date)} → {str(latestBi.end_date)}</span>
                <div className="num mt-0.5 text-[10px] text-muted-foreground">
                  {fmtPrice(num(latestBi.low))} ~ {fmtPrice(num(latestBi.high))}
                  {latestBi.done === false && <span className="ml-1 text-gold">· 未完成</span>}
                </div>
              </div>
            ) : <div className="mt-0.5 text-[11px] text-muted-foreground">--</div>}
          </div>
          <div className="rounded-md border border-border/60 bg-background/40 px-2.5 py-1.5">
            <div className="text-[9px] text-muted-foreground">最新中枢</div>
            {latestZs ? (
              <div className="mt-0.5 text-[11px]">
                <span className="num font-semibold text-foreground">{fmtPrice(num(latestZs.zd))} ~ {fmtPrice(num(latestZs.zg))}</span>
                <div className="num mt-0.5 text-[10px] text-muted-foreground">
                  {str(latestZs.start_date)} → {str(latestZs.end_date)} · {str(latestZs.line_count)} 笔
                </div>
              </div>
            ) : <div className="mt-0.5 text-[11px] text-muted-foreground">--</div>}
          </div>
          <div className="rounded-md border border-border/60 bg-background/40 px-2.5 py-1.5">
            <div className="text-[9px] text-muted-foreground">结构确认进度</div>
            <div className="num mt-0.5 text-[11px] text-foreground">确认至 {str(summary.structure_latest_date)}</div>
            {unconfirmed > 0 && (
              <div className="mt-0.5 text-[10px] text-gold">{unconfirmed} 根新K线待确认</div>
            )}
          </div>
        </div>
      </section>

      {/* 第二层：最新买卖点（最重要的交易信号，单独突出） */}
      <section>
        <BlockTitle>最新买卖点</BlockTitle>
        {mmds.length === 0 ? (
          <div className="text-[10px] text-muted-foreground">近 800 根K线内无买卖点</div>
        ) : (
          <div className="space-y-1.5">
            {mmds.slice(0, 4).map((m, i) => {
              const badge = mmdBadge(m.type)
              return (
                <div key={i} className={cn("flex items-start gap-2 rounded-md border px-2.5 py-1.5", i === 0 ? "border-primary/50 bg-primary/5" : "border-border/60 bg-background/40")}>
                  <span className={cn("mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold", badge.cls)}>{badge.label}</span>
                  <div className="min-w-0">
                    <span className="num text-[10px] text-muted-foreground">{str(m.date)}</span>
                    <div className="text-[11px] leading-snug text-foreground/90">{str(m.msg)}</div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>

      {/* 第三层：结构统计 + 明细 */}
      <section>
        <BlockTitle>结构统计（近800根K线）</BlockTitle>
        <div className="flex flex-wrap gap-1.5">
          {([
            ["笔", summary.bi_count], ["线段", summary.xd_count], ["中枢", summary.zs_count],
            ["买卖点", summary.mmd_count], ["背驰", summary.bc_count], ["分型", summary.fractal_count],
          ] as [string, unknown][]).map(([label, v]) => (
            <span key={label} className="rounded bg-muted px-2 py-1 text-[10px] text-muted-foreground">
              {label} <b className="num text-foreground">{str(v)}</b>
            </span>
          ))}
        </div>
      </section>

      <section className="grid grid-cols-2 gap-3 max-lg:grid-cols-1">
        <div>
          <BlockTitle>背驰记录</BlockTitle>
          <div className="space-y-1">
            {bcs.slice(0, 5).map((b, i) => (
              <div key={i} className="rounded border border-border/40 bg-background/30 px-2 py-1 text-[10px]">
                <span className="num text-muted-foreground">{str(b.curr_date)}</span>
                <span className="ml-1.5 text-foreground/80">{str(b.msg)}</span>
              </div>
            ))}
            {bcs.length === 0 && <div className="text-[10px] text-muted-foreground">无背驰记录</div>}
          </div>
        </div>
        <div>
          <BlockTitle>近期笔</BlockTitle>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border text-[9px] text-muted-foreground">
                  <th className="py-1 text-left font-normal">方向</th>
                  <th className="text-left font-normal">起点</th>
                  <th className="text-left font-normal">终点</th>
                  <th className="text-right font-normal">低~高</th>
                </tr>
              </thead>
              <tbody>
                {bis.slice(0, 8).map((b, i) => (
                  <tr key={i} className="border-b border-border/40 text-[10px]">
                    <td className={cn("py-1 font-semibold", str(b.direction) === "up" ? "text-up" : "text-down")}>
                      {str(b.direction) === "up" ? "↑" : "↓"}
                    </td>
                    <td className="num text-muted-foreground">{str(b.start_date).slice(5)}</td>
                    <td className="num text-muted-foreground">{str(b.end_date).slice(5)}</td>
                    <td className="num text-right">{fmtPrice(num(b.low))}~{fmtPrice(num(b.high))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
      {section.note && <p className="text-[9px] leading-relaxed text-muted-foreground/60">{section.note}</p>}
    </div>
  )
}
