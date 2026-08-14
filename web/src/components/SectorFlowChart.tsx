import { useCallback, useMemo } from "react"
import type { EChartsOption } from "echarts"
import { useECharts } from "@/hooks/useECharts"
import { pctClass, fmtPct } from "@/lib/format"
import { cn } from "@/lib/utils"

export interface SectorFlowSeriesData {
  name: string
  heat_score: number
  final_value: number
  change_pct: number
  leader_name?: string | null
  points: { time: string; value: number }[]
  flow_basis?: string
}

/**
 * 按板块名稳定配色：榜单排序/成员每次推送都会变，不能再按下标取色，
 * 否则同一个板块会频繁换颜色。用名字的哈希乘黄金角散布到 360° 色相环，
 * 同一板块在任何排名、任何时刻颜色都一致，且板块数不受调色板长度限制。
 */
function sectorColor(name: string): string {
  let hash = 0
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0
  }
  const hue = Math.round((hash * 137.508) % 360)
  return `hsl(${hue} 82% 58%)`
}

const GRID = "hsl(220 15% 14%)"
const AXIS = "hsl(220 10% 45%)"

/** 板块资金动能走势：顶层板块的分钟级动能曲线。点击曲线/末端标签/底部图例联动「板块强弱」 */
export function SectorFlowChart({
  series,
  selected = null,
  onSelect,
}: {
  series: SectorFlowSeriesData[]
  selected?: string | null
  onSelect?: (name: string | null) => void
}) {
  const usable = (series ?? []).filter((s) => (s.points ?? []).length >= 2)
  const hasSelected = selected != null && usable.some((s) => s.name === selected)

  // 后端推送的是「每分钟净流入」差分值（零轴附近噪声大，十条线堆在一起看不出强弱）。
  // 图上重积分为累计动能曲线：强者持续上行、弱者下行，分化一目了然。
  const cumulative = useMemo(
    () =>
      usable.map((s) => {
        let acc = 0
        const values = s.points.map((p) => {
          acc += Number(p.value) || 0
          return Math.round(acc * 100) / 100
        })
        return { ...s, cumValues: values, cumFinal: values[values.length - 1] ?? 0 }
      }),
    [usable],
  )

  const option = useMemo<EChartsOption | null>(() => {
    if (!cumulative.length) return null
    const times = cumulative[0].points.map((p) => p.time)
    return {
      // 合并更新关闭补间动画：序列数量变化时 ECharts 插值会抛异常，就地更新本身无闪烁
      animation: false,
      animationDuration: 0,
      animationDurationUpdate: 0,
      backgroundColor: "transparent",
      // 全局色表与折线/图例同源（按板块名稳定取色）：悬浮提示的圆点色默认取系列色，
      // 不显式给出时 ECharts 会用内置默认色，导致悬浮颜色与线色错位
      color: cumulative.map((s) => sectorColor(s.name)),
      grid: { left: 34, right: 86, top: 8, bottom: 18 },
      tooltip: {
        trigger: "axis",
        confine: true,
        backgroundColor: "hsl(222 28% 9%)",
        borderColor: "hsl(220 15% 20%)",
        textStyle: { color: "hsl(220 15% 85%)", fontSize: 10 },
      },
      xAxis: {
        type: "category",
        data: times,
        axisLine: { lineStyle: { color: GRID } },
        axisTick: { show: false },
        axisLabel: { color: AXIS, fontSize: 9, interval: Math.ceil(times.length / 5) },
        splitLine: { show: false },
      },
      yAxis: {
        scale: true,
        axisLabel: { color: AXIS, fontSize: 9 },
        splitLine: { lineStyle: { color: GRID, type: "dashed" } },
      },
      series: cumulative.map((s, i) => ({
        name: s.name,
        type: "line" as const,
        data: s.cumValues,
        showSymbol: false,
        smooth: 0.25,
        // 选中联动：选中板块加粗，其余降透明度
        lineStyle: {
          color: sectorColor(s.name),
          width: hasSelected && s.name === selected ? 2.4 : 1.2,
          opacity: hasSelected && s.name !== selected ? 0.25 : 1,
        },
        // 线右端直接标注板块名，重叠时沿 Y 轴错开
        endLabel: {
          show: true,
          formatter: s.name,
          color: sectorColor(s.name),
          fontSize: 9,
          distance: 6,
        },
        labelLayout: { moveOverlap: "shiftY" as const },
        // 零轴基准线：累计曲线以零轴分强弱
        markLine:
          i === 0
            ? {
                silent: true,
                symbol: "none",
                label: { show: false },
                lineStyle: { color: "hsl(220 10% 30%)", type: "dashed", width: 1 },
                data: [{ yAxis: 0 }],
              }
            : undefined,
        // 禁用悬停高亮态：当前 ECharts 版本 emphasis 重绘会丢折线描边
        emphasis: { disabled: true },
      })),
    }
  }, [cumulative, hasSelected, selected])

  // 点击曲线 → 联动板块强弱切换；再点同一板块取消
  const handleClick = useCallback(
    (params: unknown) => {
      if (!onSelect) return
      const name = (params as { seriesName?: string }).seriesName
      if (name) onSelect(selected === name ? null : name)
    },
    [onSelect, selected],
  )

  const ref = useECharts(option, { click: handleClick })

  return (
    <section className="terminal-panel flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
        <div className="panel-title">板块资金动能</div>
        <span className="text-[9px] text-muted-foreground/70">
          {usable[0]?.flow_basis || "分钟资金动能"} · 累计
        </span>
      </header>
      <div className="min-h-0 flex-1">
        {usable.length ? (
          <div ref={ref} className="h-full w-full" />
        ) : (
          <div className="flex h-full items-center justify-center px-3 text-center text-[10px] leading-snug text-muted-foreground/70">
            盘中采集板块轨迹后展示分钟动能曲线
          </div>
        )}
      </div>
      {usable.length > 0 && (
        <div className="flex shrink-0 flex-wrap gap-x-2 gap-y-0.5 border-t border-border px-2 py-1">
          {usable.slice(0, 8).map((s) => (
            <button
              key={s.name}
              type="button"
              onClick={() => onSelect?.(selected === s.name ? null : s.name)}
              className={cn(
                "flex items-center gap-1 text-[9px] text-muted-foreground transition-opacity hover:text-foreground",
                hasSelected && s.name !== selected && "opacity-40",
              )}
            >
              <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: sectorColor(s.name) }} />
              {s.name}
              <span className={cn("num font-semibold", pctClass(s.change_pct))}>{fmtPct(s.change_pct, 1)}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}
