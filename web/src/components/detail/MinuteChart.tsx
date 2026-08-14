import { useMemo } from "react"
import type { EChartsOption } from "echarts"
import { useECharts } from "@/hooks/useECharts"
import type { MinuteChartData, OverlayMarker } from "@/types/api"
import { signalTone } from "@/lib/format"

const UP = "hsl(354 88% 58%)"
const DOWN = "hsl(152 76% 42%)"
const GOLD = "hsl(40 95% 55%)"
const GRID = "hsl(220 15% 14%)"
const AXIS = "hsl(220 10% 45%)"
const TEXT = "hsl(220 15% 85%)"

interface MinuteChartProps {
  chart: MinuteChartData | null
  markers: OverlayMarker[]
  openingMarkers: OverlayMarker[]
}

export function MinuteChart({ chart, markers, openingMarkers }: MinuteChartProps) {
  const option = useMemo<EChartsOption | null>(() => {
    if (!chart || !chart.times?.length) return null
    const { times, change_pcts, vwaps, volumes, prev_close, prices } = chart
    const vwapPcts = vwaps.map((v) => (prev_close > 0 ? (v / prev_close - 1) * 100 : 0))

    const buySellPoints = markers.map((m) => {
      const tone = signalTone(m.signal)
      return {
        name: m.signal,
        coord: [m.time, m.change_pct],
        value: m.signal,
        symbol: tone === "sell" ? "triangle" : tone === "buy" ? "pin" : "circle",
        symbolRotate: tone === "sell" ? 180 : 0,
        symbolSize: tone === "watch" ? 7 : 13,
        itemStyle: { color: tone === "sell" ? DOWN : tone === "buy" ? UP : GOLD, borderColor: "#0b0e14", borderWidth: 1 },
        label: { show: false },
        marker: m,
      }
    })
    const openingPoints = openingMarkers.map((m) => {
      const tone = signalTone(m.signal)
      return {
        name: m.phase || m.signal,
        coord: [m.time, m.change_pct],
        value: m.phase || m.signal,
        symbol: "diamond",
        symbolSize: 15,
        itemStyle: { color: tone === "sell" ? "#0d5c36" : "#8f1023", borderColor: tone === "sell" ? DOWN : UP, borderWidth: 1.5 },
        label: { show: false },
        marker: m,
      }
    })

    const volColors = volumes.map((_, i) => {
      if (i === 0) return "rgba(232,234,240,0.5)"
      return prices[i] >= prices[i - 1] ? "hsl(354 88% 58% / 0.75)" : "hsl(152 76% 42% / 0.75)"
    })
    const latest = change_pcts[change_pcts.length - 1] ?? 0
    const priceColor = latest >= 0 ? UP : DOWN
    const allPcts = [...change_pcts, ...vwapPcts, 0]
    const yMin = Math.min(...allPcts)
    const yMax = Math.max(...allPcts)
    const pad = Math.max((yMax - yMin) * 0.12, 0.3)

    return {
      // 首帧不播入场动画；轮询合并更新关闭补间动画——
      // 序列/标记数量变化时 ECharts 的 1D 数组插值会抛异常导致图表渲染崩掉，
      // 合并模式就地更新本身无闪烁，不需要补间。
      animation: false,
      animationDuration: 0,
      animationDurationUpdate: 0,
      backgroundColor: "transparent",
      grid: [
        { left: 46, right: 12, top: 10, height: "62%" },
        { left: 46, right: 12, top: "74%", height: "20%" },
      ],
      axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: "hsl(220 15% 30%)" } },
      tooltip: {
        trigger: "axis",
        backgroundColor: "hsl(222 28% 9%)",
        borderColor: "hsl(220 15% 20%)",
        textStyle: { color: TEXT, fontSize: 11 },
        formatter: (params) => {
          const arr = Array.isArray(params) ? params : [params]
          const idx = arr[0]?.dataIndex ?? 0
          const t = times[idx]
          const lines = [
            `<div style="font-weight:700;margin-bottom:2px">${t}</div>`,
            `<div>价格 <b>${prices[idx]?.toFixed(2) ?? "--"}</b> · 涨幅 <b style="color:${(change_pcts[idx] ?? 0) >= 0 ? UP : DOWN}">${(change_pcts[idx] ?? 0).toFixed(2)}%</b></div>`,
            `<div style="color:${AXIS}">VWAP ${vwaps[idx]?.toFixed(2) ?? "--"} · 量 ${(volumes[idx] ?? 0).toFixed(0)}手 · 量比 ${(chart.amount_ratios[idx] ?? 0).toFixed(1)}</div>`,
          ]
          const hit = [...markers, ...openingMarkers].filter((m) => m.time === t)
          for (const m of hit) {
            lines.push(
              `<div style="margin-top:3px;border-top:1px solid ${GRID};padding-top:3px"><b style="color:${signalTone(m.signal) === "sell" ? DOWN : UP}">${m.signal} · ${m.phase}</b></div>`,
              `<div style="max-width:280px;white-space:normal;color:${AXIS}">${(m.reasons ?? []).join("；")}</div>`,
            )
            if ((m.risks ?? []).length) lines.push(`<div style="max-width:280px;white-space:normal;color:${GOLD}">${m.risks.join("；")}</div>`)
          }
          return lines.join("")
        },
      },
      xAxis: [
        {
          type: "category",
          data: times,
          gridIndex: 0,
          axisLine: { lineStyle: { color: GRID } },
          axisTick: { show: false },
          axisLabel: { color: AXIS, fontSize: 10, interval: Math.ceil(times.length / 8) },
          splitLine: { show: false },
        },
        {
          type: "category",
          data: times,
          gridIndex: 1,
          axisLine: { lineStyle: { color: GRID } },
          axisTick: { show: false },
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          gridIndex: 0,
          min: (v: { min: number }) => Math.floor((Math.min(v.min, yMin - pad)) * 10) / 10,
          max: (v: { max: number }) => Math.ceil((Math.max(v.max, yMax + pad)) * 10) / 10,
          axisLabel: { color: AXIS, fontSize: 10, formatter: (v: number) => `${v.toFixed(1)}%` },
          splitLine: { lineStyle: { color: GRID, type: "dashed" } },
        },
        {
          scale: true,
          gridIndex: 1,
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: "价格",
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: change_pcts,
          showSymbol: false,
          lineStyle: { color: priceColor, width: 1.4 },
          // 不用渐变填充：悬停时 ECharts 重建渐变会拿到 undefined 色值，
          // addColorStop 抛异常导致整条价格线消失。纯色 alpha 填充无此问题。
          areaStyle: {
            color: latest >= 0 ? "rgba(232, 72, 85, 0.14)" : "rgba(35, 190, 120, 0.14)",
          },
          // 悬停高亮态会让折线描边重绘消失（ECharts emphasis 重绘 bug），
          // 本图不需要 emphasis 样式，直接禁用；tooltip/十字线不受影响。
          emphasis: { disabled: true },
          markLine: {
            silent: true,
            symbol: "none",
            data: [{ yAxis: 0 }],
            lineStyle: { color: "hsl(220 10% 35%)", type: "dashed", width: 1 },
            label: { show: true, formatter: "昨收", color: AXIS, fontSize: 9, position: "insideEndTop" },
          },
          markPoint: {
            silent: false,
            data: [...buySellPoints, ...openingPoints],
            tooltip: { show: false },
          },
          z: 3,
        },
        {
          name: "VWAP",
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: vwapPcts,
          showSymbol: false,
          lineStyle: { color: GOLD, width: 1, type: "dashed", opacity: 0.8 },
          emphasis: { disabled: true },
          z: 2,
        },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes.map((v, i) => ({ value: v, itemStyle: { color: volColors[i] } })),
          barWidth: "62%",
          emphasis: { disabled: true },
        },
      ],
    }
  }, [chart, markers, openingMarkers])

  const ref = useECharts(option)
  if (!chart) {
    return <div className="flex h-full items-center justify-center text-xs text-muted-foreground">分时数据加载中…</div>
  }
  return <div ref={ref} className="h-full w-full" />
}
