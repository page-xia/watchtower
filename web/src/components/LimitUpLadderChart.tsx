import { useCallback, useMemo } from "react"
import type { EChartsOption } from "echarts"
import { useECharts } from "@/hooks/useECharts"
import type { SectorRank } from "@/types/api"

const GRID = "hsl(220 15% 14%)"
const AXIS = "hsl(220 10% 45%)"
const UP = "hsl(354 88% 58%)"
const GOLD = "hsl(40 95% 55%)"

interface LimitUpLadderChartProps {
  sectors: SectorRank[]
  selected?: string | null
  onSelect?: (name: string | null) => void
}

/**
 * 涨停情绪梯队：按板块涨停家数排序，金色堆叠为炸板/开板承接。
 * 板块情绪个股上板是买入共振的第三脚——上板多且炸板少的板块才有持续性。
 * 点击柱子或板块名可联动左侧「板块强弱」切换选中板块，再次点击取消。
 */
export function LimitUpLadderChart({ sectors, selected = null, onSelect }: LimitUpLadderChartProps) {
  const ranked = useMemo(
    () =>
      [...(sectors ?? [])]
        .filter((s) => s.limit_up_count > 0 || s.opened_limit_count > 0)
        .sort((a, b) => b.limit_up_count - a.limit_up_count || a.opened_limit_count - b.opened_limit_count)
        .slice(0, 8)
        .reverse(),
    [sectors],
  )
  const hasSelected = selected != null && ranked.some((s) => s.name === selected)

  const option = useMemo<EChartsOption | null>(() => {
    if (!ranked.length) return null
    // 选中联动：未选中的板块降透明度，选中的保持高亮
    const dim = (name: string) => (hasSelected && name !== selected ? { opacity: 0.3 } : undefined)
    return {
      // 合并更新关闭补间动画：序列数量变化时 ECharts 插值会抛异常，就地更新本身无闪烁
      animation: false,
      animationDuration: 0,
      animationDurationUpdate: 0,
      backgroundColor: "transparent",
      grid: { left: 4, right: 34, top: 4, bottom: 4, containLabel: true },
      tooltip: {
        trigger: "item",
        backgroundColor: "hsl(222 28% 9%)",
        borderColor: "hsl(220 15% 20%)",
        textStyle: { color: "hsl(220 15% 85%)", fontSize: 10 },
        formatter: (p: unknown) => {
          const s = ranked[(p as { dataIndex?: number }).dataIndex ?? 0]
          if (!s) return ""
          const total = s.limit_up_count + s.opened_limit_count
          const rate = total > 0 ? Math.round((s.limit_up_count / total) * 100) : 0
          return `${s.name}<br/>涨停 ${s.limit_up_count} · 炸板 ${s.opened_limit_count} · 封板率 ${rate}%<br/>${s.up_count}/${s.total_count}上涨 · 龙头 ${s.leader_name || "--"}`
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
        axisLine: { lineStyle: { color: GRID } },
        axisTick: { show: false },
        axisLabel: { color: "hsl(220 15% 75%)", fontSize: 10 },
      },
      series: [
        {
          name: "涨停",
          type: "bar" as const,
          stack: "limit",
          data: ranked.map((s) => ({ value: s.limit_up_count, itemStyle: dim(s.name) })),
          itemStyle: { color: UP, borderRadius: [0, 0, 0, 0] },
          // 禁用悬停高亮态：当前 ECharts 版本 emphasis 重绘会丢柱子填充色（悬浮变透明）
          emphasis: { disabled: true },
          barWidth: "58%",
          label: {
            show: true,
            position: "insideLeft" as const,
            fontSize: 9,
            color: "#fff",
            formatter: (p: { value?: unknown }) => (typeof p.value === "number" && p.value > 0 ? String(p.value) : ""),
          },
        },
        {
          name: "炸板",
          type: "bar" as const,
          stack: "limit",
          data: ranked.map((s) => ({ value: s.opened_limit_count, itemStyle: dim(s.name) })),
          itemStyle: { color: GOLD, borderRadius: [0, 2, 2, 0] },
          emphasis: { disabled: true },
          label: {
            show: true,
            position: "right" as const,
            fontSize: 9,
            color: AXIS,
            formatter: (p: { dataIndex?: number }) => {
              const s = ranked[p.dataIndex ?? 0]
              return s ? `${s.limit_up_count}+${s.opened_limit_count}` : ""
            },
          },
        },
      ],
    }
  }, [ranked, hasSelected, selected])

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
  const totalUp = (sectors ?? []).reduce((acc, s) => acc + s.limit_up_count, 0)
  const totalOpened = (sectors ?? []).reduce((acc, s) => acc + s.opened_limit_count, 0)
  const sealRate = totalUp + totalOpened > 0 ? Math.round((totalUp / (totalUp + totalOpened)) * 100) : 0

  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
        <div className="panel-title">涨停情绪梯队</div>
        <span className="text-[9px] text-muted-foreground/80">
          全板块 涨停<span className="num font-semibold text-up">{totalUp}</span> · 炸板
          <span className="num font-semibold text-gold">{totalOpened}</span> · 封板率
          <span className="num font-semibold text-foreground/90">{sealRate}%</span>
        </span>
      </header>
      <div className="min-h-0 flex-1">
        {ranked.length ? (
          <div ref={ref} className="h-full w-full" />
        ) : (
          <div className="flex h-full items-center justify-center px-3 text-center text-[10px] leading-snug text-muted-foreground/70">
            当前无涨停/炸板板块
          </div>
        )}
      </div>
    </section>
  )
}
