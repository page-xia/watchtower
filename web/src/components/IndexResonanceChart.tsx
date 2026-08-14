import { useMemo } from "react"
import type { EChartsOption } from "echarts"
import { useECharts } from "@/hooks/useECharts"
import type { IndexMinutesResponse } from "@/types/api"
import { fmtPct, pctClass } from "@/lib/format"
import { cn } from "@/lib/utils"

const COLORS = ["hsl(40 95% 55%)", "hsl(187 85% 53%)", "hsl(320 70% 62%)", "hsl(265 80% 65%)"]
const GRID = "hsl(220 15% 14%)"
const AXIS = "hsl(220 10% 45%)"

/**
 * 指数共振图：三指数分钟涨跌幅叠加。
 * 拐头共振 = 指数在低点拐头向上（index_turning），配合板块资金/个股放量确认买入。
 */
export function IndexResonanceChart({ data }: { data: IndexMinutesResponse | null }) {
  const usable = (data?.indices ?? []).filter((s) => s.points.length >= 2)

  const option = useMemo<EChartsOption | null>(() => {
    if (!usable.length) return null
    const times = usable[0].points.map((p) => p.time)
    return {
      // 合并更新关闭补间动画：序列数量变化时 ECharts 插值会抛异常，就地更新本身无闪烁
      animation: false,
      animationDuration: 0,
      animationDurationUpdate: 0,
      backgroundColor: "transparent",
      grid: { left: 38, right: 10, top: 10, bottom: 20 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "hsl(222 28% 9%)",
        borderColor: "hsl(220 15% 20%)",
        textStyle: { color: "hsl(220 15% 85%)", fontSize: 10 },
        valueFormatter: (v: unknown) => (typeof v === "number" ? `${v > 0 ? "+" : ""}${v.toFixed(2)}%` : "--"),
      },
      xAxis: {
        type: "category",
        data: times,
        axisLine: { lineStyle: { color: GRID } },
        axisTick: { show: false },
        axisLabel: { color: AXIS, fontSize: 9, interval: Math.ceil(times.length / 6) },
        splitLine: { show: false },
      },
      yAxis: {
        scale: true,
        axisLabel: { color: AXIS, fontSize: 9, formatter: (v: number) => `${v.toFixed(1)}%` },
        splitLine: { lineStyle: { color: GRID, type: "dashed" } },
      },
      series: usable.map((s, i) => ({
        name: s.name,
        type: "line" as const,
        data: s.points.map((p) => p.change_pct),
        showSymbol: false,
        smooth: 0.2,
        lineStyle: { color: COLORS[i % COLORS.length], width: 1.4 },
        // 禁用悬停高亮态：当前 ECharts 版本 emphasis 重绘会丢折线描边
        emphasis: { disabled: true },
        markLine:
          i === 0
            ? {
                silent: true,
                symbol: "none",
                label: { show: false },
                lineStyle: { color: "hsl(220 10% 38%)", type: "solid" as const, width: 1 },
                data: [{ yAxis: 0 }],
              }
            : undefined,
      })),
    }
  }, [usable])

  const ref = useECharts(option)
  const turning = data?.index_turning
  const slope = data?.index_slope_pct ?? 0

  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
        <div className="panel-title">指数共振 · 拐头确认</div>
        <div className="flex items-center gap-1.5">
          {data?.amount_expanding && (
            <span className="rounded bg-up-dim px-1.5 py-0.5 text-[9px] font-semibold text-up">放量</span>
          )}
          <span
            className={cn(
              "rounded px-1.5 py-0.5 text-[9px] font-semibold",
              turning ? "bg-up-dim text-up" : "bg-muted text-muted-foreground",
            )}
            title={turning ? `拐头模式 ${data?.index_turning_mode || "--"} · 斜率 ${slope.toFixed(2)}%` : "指数尚未拐头"}
          >
            {turning ? `拐头${data?.index_turning_mode ? `·${data.index_turning_mode}` : ""}` : "未拐头"}
          </span>
        </div>
      </header>
      <div className="min-h-0 flex-1">
        {usable.length ? (
          <div ref={ref} className="h-full w-full" />
        ) : (
          <div className="flex h-full items-center justify-center px-3 text-center text-[10px] leading-snug text-muted-foreground/70">
            等待指数分钟数据
          </div>
        )}
      </div>
      {usable.length > 0 && (
        <div className="flex shrink-0 flex-wrap gap-x-3 gap-y-0.5 border-t border-border px-2 py-1">
          {usable.map((s, i) => (
            <span key={s.code} className="flex items-center gap-1 text-[9px] text-muted-foreground">
              <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: COLORS[i % COLORS.length] }} />
              {s.name}
              <span className={cn("num font-semibold", pctClass(s.change_pct))}>{fmtPct(s.change_pct)}</span>
              <span className="num text-muted-foreground/70">反弹{fmtPct(s.rebound_from_low_pct, 1)}</span>
            </span>
          ))}
        </div>
      )}
    </section>
  )
}
