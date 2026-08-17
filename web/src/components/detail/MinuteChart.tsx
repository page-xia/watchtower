import { useMemo } from "react"
import type { EChartsOption } from "echarts"
import { useECharts } from "@/hooks/useECharts"
import type { FormulaOverlay, MinuteChartData, OverlayMarker } from "@/types/api"
import { signalTone } from "@/lib/format"
import { chartPalette, useTheme } from "@/lib/theme"

interface MinuteChartProps {
  chart: MinuteChartData | null
  markers: OverlayMarker[]
  overlay?: FormulaOverlay | null
  /** 现价落在低吸区间（底线-1% ~ 顶线+3%）时，买点改用金色圆点突出 */
  nearLine?: boolean
  /** 短期/长期趋势线（价格口径）：只有现价贴近 ±3% 的那条才会画出来 */
  trendLines?: { short?: number | null; long?: number | null } | null
}

export function MinuteChart({ chart, markers, overlay, nearLine = false, trendLines = null }: MinuteChartProps) {
  const theme = useTheme()
  const pal = useMemo(() => chartPalette(theme), [theme])

  const option = useMemo<EChartsOption | null>(() => {
    if (!chart || !chart.times?.length) return null
    const { times, change_pcts, vwaps, volumes, prev_close, prices } = chart
    const vwapPcts = vwaps.map((v) => (prev_close > 0 ? (v / prev_close - 1) * 100 : 0))

    // 做T公式叠层只画可交易价位：阻力/支撑两条水平线。
    // MA30/强弱线数值在右侧公式卡片呈现，不上图，避免曲线过多干扰读价。
    const levelLines: Record<string, unknown>[] = []
    if (overlay?.available) {
      if (overlay.resistance_pct != null) {
        levelLines.push({
          yAxis: overlay.resistance_pct,
          lineStyle: { color: pal.down, type: "dashed", width: 1 },
          label: { show: true, formatter: `阻力 ${overlay.resistance.toFixed(2)}`, color: pal.down, fontSize: 9, position: "insideStartTop" },
        })
      }
      if (overlay.support_pct != null) {
        levelLines.push({
          yAxis: overlay.support_pct,
          lineStyle: { color: pal.up, type: "dashed", width: 1 },
          label: { show: true, formatter: `支撑 ${overlay.support.toFixed(2)}`, color: pal.up, fontSize: 9, position: "insideStartBottom" },
        })
      }
    }

    const buySellPoints = markers.map((m) => {
      const tone = signalTone(m.signal)
      // 现价落在低吸区间（底线-1% ~ 顶线+3%）时，买点用金色圆点替代红色图钉，提示低吸机会
      const goldBuy = nearLine && tone === "buy"
      return {
        name: m.signal,
        coord: [m.time, m.change_pct],
        value: m.signal,
        symbol: goldBuy ? "circle" : tone === "sell" ? "triangle" : tone === "buy" ? "pin" : "circle",
        symbolRotate: tone === "sell" && !goldBuy ? 180 : 0,
        symbolSize: goldBuy ? 11 : tone === "watch" ? 7 : 13,
        itemStyle: {
          color: goldBuy ? pal.gold : tone === "sell" ? pal.down : tone === "buy" ? pal.up : pal.gold,
          borderColor: pal.symbolBorder,
          borderWidth: 1,
        },
        label: { show: false },
        marker: m,
      }
    })
    // 短期/长期趋势线：辅助线逻辑——只有现价贴近（±3%）的线才显示，
    // 且只有显示出来的线参与 Y 轴范围，不贴近时完全不影响主分时图空间。
    // 现价高于两线 → 最多显示下方 3% 内的那条；夹在两线中间 → 贴近哪条显示哪条。
    const currentPrice = prices[prices.length - 1] ?? 0
    const currentPct = change_pcts[change_pcts.length - 1] ?? 0
    const zoneLines: Record<string, unknown>[] = []
    const visibleLinePcts: number[] = []
    if (trendLines && prev_close > 0 && currentPrice > 0) {
      for (const [label, value] of [
        ["短期", trendLines.short],
        ["长期", trendLines.long],
      ] as const) {
        if (value == null || value <= 0) continue
        if (Math.abs(currentPrice - value) / value > 0.03) continue
        const pct = (value / prev_close - 1) * 100
        visibleLinePcts.push(pct)
        zoneLines.push({
          yAxis: pct,
          lineStyle: { color: pal.gold, type: "dashed", width: 1, opacity: 0.7 },
          label: {
            show: true,
            formatter: `${label} ${value.toFixed(2)}`,
            color: pal.gold,
            fontSize: 9,
            position: pct >= currentPct ? "insideEndTop" : "insideEndBottom",
          },
        })
      }
    }
    // 两条线都贴近（现价夹在中间）时才给阴影带
    const zoneBandPcts =
      visibleLinePcts.length === 2 ? ([Math.max(...visibleLinePcts), Math.min(...visibleLinePcts)] as const) : null

    const volColors = volumes.map((_, i) => {
      if (i === 0) return pal.flatA(0.5)
      return prices[i] >= prices[i - 1] ? pal.upA(0.75) : pal.downA(0.75)
    })
    const latest = change_pcts[change_pcts.length - 1] ?? 0
    const priceColor = latest >= 0 ? pal.up : pal.down
    const allPcts = [
      ...change_pcts,
      ...vwapPcts,
      ...levelLines.map((l) => Number((l as { yAxis?: number }).yAxis ?? 0)),
      ...visibleLinePcts,
      0,
    ]
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
      axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: pal.axisPointer } },
      tooltip: {
        trigger: "axis",
        backgroundColor: pal.tooltipBg,
        borderColor: pal.tooltipBorder,
        textStyle: { color: pal.tooltipText, fontSize: 11 },
        formatter: (params) => {
          const arr = Array.isArray(params) ? params : [params]
          const idx = arr[0]?.dataIndex ?? 0
          const t = times[idx]
          const lines = [
            `<div style="font-weight:700;margin-bottom:2px">${t}</div>`,
            `<div>价格 <b>${prices[idx]?.toFixed(2) ?? "--"}</b> · 涨幅 <b style="color:${(change_pcts[idx] ?? 0) >= 0 ? pal.up : pal.down}">${(change_pcts[idx] ?? 0).toFixed(2)}%</b></div>`,
            `<div style="color:${pal.axis}">VWAP ${vwaps[idx]?.toFixed(2) ?? "--"} · 量 ${(volumes[idx] ?? 0).toFixed(0)}手 · 量比 ${(chart.amount_ratios[idx] ?? 0).toFixed(1)}</div>`,
          ]
          const hit = markers.filter((m) => m.time === t)
          for (const m of hit) {
            lines.push(
              `<div style="margin-top:3px;border-top:1px solid ${pal.grid};padding-top:3px"><b style="color:${signalTone(m.signal) === "sell" ? pal.down : pal.up}">${m.signal} · ${m.phase}</b></div>`,
              `<div style="max-width:280px;white-space:normal;color:${pal.axis}">${(m.reasons ?? []).join("；")}</div>`,
            )
            if ((m.risks ?? []).length) lines.push(`<div style="max-width:280px;white-space:normal;color:${pal.gold}">${m.risks.join("；")}</div>`)
          }
          return lines.join("")
        },
      },
      xAxis: [
        {
          type: "category",
          data: times,
          gridIndex: 0,
          axisLine: { lineStyle: { color: pal.grid } },
          axisTick: { show: false },
          axisLabel: { color: pal.axis, fontSize: 10, interval: Math.ceil(times.length / 8) },
          splitLine: { show: false },
        },
        {
          type: "category",
          data: times,
          gridIndex: 1,
          axisLine: { lineStyle: { color: pal.grid } },
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
          axisLabel: { color: pal.axis, fontSize: 10, formatter: (v: number) => `${v.toFixed(1)}%` },
          splitLine: { lineStyle: { color: pal.grid, type: "dashed" } },
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
            color: latest >= 0 ? pal.upA(0.14) : pal.downA(0.14),
          },
          // 悬停高亮态会让折线描边重绘消失（ECharts emphasis 重绘 bug），
          // 本图不需要 emphasis 样式，直接禁用；tooltip/十字线不受影响。
          emphasis: { disabled: true },
          markLine: {
            silent: true,
            symbol: "none",
            data: [{ yAxis: 0 }, ...levelLines, ...zoneLines],
            lineStyle: { color: pal.markLine, type: "dashed", width: 1 },
            label: { show: true, formatter: "昨收", color: pal.axis, fontSize: 9, position: "insideEndTop" },
          },
          // 低吸阴影带：仅当短期/长期两条线都贴近（现价夹在中间）时绘制
          markArea: zoneBandPcts
            ? {
                silent: true,
                itemStyle: { color: pal.goldA(0.07) },
                data: [[{ yAxis: zoneBandPcts[0] }, { yAxis: zoneBandPcts[1] }]],
              }
            : undefined,
          markPoint: {
            silent: false,
            data: buySellPoints,
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
          lineStyle: { color: pal.gold, width: 1, type: "dashed", opacity: 0.8 },
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
  }, [chart, markers, overlay, nearLine, trendLines, pal])

  const ref = useECharts(option)
  if (!chart) {
    return <div className="flex h-full items-center justify-center text-xs text-muted-foreground">分时数据加载中…</div>
  }
  return <div ref={ref} className="h-full w-full" />
}
