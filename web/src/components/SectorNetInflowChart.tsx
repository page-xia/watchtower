import { useCallback, useMemo } from "react"
import type { EChartsOption } from "echarts"
import { useECharts } from "@/hooks/useECharts"
import type { SectorRank } from "@/types/api"
import { fmtPct, pctClass } from "@/lib/format"
import { chartPalette, useTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"

function fmtYi(v: number): string {
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}亿`
}

interface SectorNetInflowChartProps {
  sectors: SectorRank[]
  selected?: string | null
  onSelect?: (name: string | null) => void
}

/**
 * 板块资金净流入榜：按方向加权成交额（flow_delta，亿）的绝对金额排序。
 * 共振口径：大钱 > 百分比——只有真金白银的净流入才能拉动指数拐头。
 * 上方红色为净流入 TOP，下方绿色为净流出 TOP。
 * 点击柱子或板块名可联动左侧「板块强弱」切换选中板块，再次点击取消。
 */
export function SectorNetInflowChart({ sectors, selected = null, onSelect }: SectorNetInflowChartProps) {
  const ranked = useMemo(() => {
    const withFlow = (sectors ?? []).filter((s) => Number.isFinite(s.flow_delta) && s.flow_delta !== 0)
    const inflow = withFlow.filter((s) => s.flow_delta > 0).sort((a, b) => b.flow_delta - a.flow_delta).slice(0, 6)
    const outflow = withFlow.filter((s) => s.flow_delta < 0).sort((a, b) => a.flow_delta - b.flow_delta).slice(0, 4)
    // y 轴自上而下：流入最大 → 流出最大
    return [...inflow, ...outflow.reverse()].reverse()
  }, [sectors])
  const hasSelected = selected != null && ranked.some((s) => s.name === selected)
  const theme = useTheme()
  const pal = useMemo(() => chartPalette(theme), [theme])

  const option = useMemo<EChartsOption | null>(() => {
    if (!ranked.length) return null
    return {
      // 合并更新关闭补间动画：序列数量变化时 ECharts 插值会抛异常，就地更新本身无闪烁
      animation: false,
      animationDuration: 0,
      animationDurationUpdate: 0,
      backgroundColor: "transparent",
      grid: { left: 4, right: 46, top: 4, bottom: 4, containLabel: true },
      tooltip: {
        trigger: "item",
        backgroundColor: pal.tooltipBg,
        borderColor: pal.tooltipBorder,
        textStyle: { color: pal.tooltipText, fontSize: 10 },
        formatter: (p: unknown) => {
          const s = ranked[(p as { dataIndex?: number }).dataIndex ?? 0]
          if (!s) return ""
          return `${s.name}<br/>净流入 ${fmtYi(s.flow_delta)} · 均涨 ${fmtPct(s.avg_change_pct)}<br/>${s.up_count}/${s.total_count}上涨 · 涨停${s.limit_up_count}`
        },
      },
      xAxis: {
        type: "value",
        axisLabel: { show: false },
        splitLine: { show: false },
        axisLine: { show: false },
      },
      yAxis: {
        type: "category",
        data: ranked.map((s) => s.name),
        // 让板块名标签也能触发点击事件，联动板块强弱
        triggerEvent: true,
        axisLine: { lineStyle: { color: pal.grid } },
        axisTick: { show: false },
        axisLabel: { color: pal.axisStrong, fontSize: 10 },
      },
      series: [
        {
          type: "bar" as const,
          data: ranked.map((s) => ({
            value: s.flow_delta,
            itemStyle: {
              color: s.flow_delta >= 0 ? pal.up : pal.down,
              borderRadius: 2,
              // 选中联动：未选中的板块降透明度，选中的保持高亮
              ...(hasSelected && s.name !== selected ? { opacity: 0.3 } : null),
            },
          })),
          barWidth: "58%",
          // 禁用悬停高亮态：当前 ECharts 版本 emphasis 重绘会丢柱子填充色（悬浮变透明）
          emphasis: { disabled: true },
          label: {
            show: true,
            position: "right" as const,
            fontSize: 9,
            color: pal.axis,
            formatter: (p: { value?: unknown }) => (typeof p.value === "number" ? fmtYi(p.value) : ""),
          },
        },
      ],
    }
  }, [ranked, hasSelected, selected, pal])

  // 点击柱子（series）或板块名（yAxis）→ 联动板块强弱切换；再点同一板块取消
  const handleClick = useCallback(
    (params: unknown) => {
      if (!onSelect) return
      const p = params as { componentType?: string; dataIndex?: number; value?: unknown }
      const name =
        p.componentType === "yAxis"
          ? typeof p.value === "string"
            ? p.value
            : undefined
          : ranked[p.dataIndex ?? -1]?.name
      if (name) onSelect(selected === name ? null : name)
    },
    [ranked, onSelect, selected],
  )

  const ref = useECharts(option, { click: handleClick })
  const top = ranked.length ? ranked[ranked.length - 1] : null

  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
        <div className="panel-title">板块资金净流入 · 金额排序</div>
        {top && (
          <span className="text-[9px] text-muted-foreground/80">
            居首 <span className="font-semibold text-foreground/90">{top.name}</span>{" "}
            <span className={cn("num font-semibold", pctClass(top.flow_delta))}>{fmtYi(top.flow_delta)}</span>
          </span>
        )}
      </header>
      <div className="min-h-0 flex-1">
        {ranked.length ? (
          <div ref={ref} className="h-full w-full" />
        ) : (
          <div className="flex h-full items-center justify-center px-3 text-center text-[10px] leading-snug text-muted-foreground/70">
            等待板块资金数据
          </div>
        )}
      </div>
    </section>
  )
}
