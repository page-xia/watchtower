import { useEffect, useMemo, useRef } from "react"
import type { EChartsOption } from "echarts"
import { useECharts } from "@/hooks/useECharts"
import type { DailyBar, DailyDetailResponse } from "@/types/api"
import { chartPalette, useTheme } from "@/lib/theme"

export type DailySubView = "resonance" | "trend" | "trendline"

interface DailyKChartProps {
  payload: DailyDetailResponse | null
  subView: DailySubView
  /** 向前拖动到数据边缘时触发：加载更早的历史K线 */
  onNeedMoreHistory?: () => void
  loadingMore?: boolean
}

const fmtDate = (d: string) => (d.length === 8 ? `${d.slice(4, 6)}-${d.slice(6, 8)}` : d)
const fmtDateFull = (d: string) => (d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : d)

interface BarMark {
  lianban: Map<number, number>
  limitUp: Set<number>
  broken: Set<number>
  shortBuy: Set<number>
  whiteExit: Set<number>
  crash: Set<number>
  baotuan: Set<number>
  gaowei: Set<number>
}

/** 主图 K 线变色优先级：连板>首板>炸板>离场>短买>超跌>持股/观望>普通阴阳 */
function candleStyle(
  bar: DailyBar,
  state: string,
  mark: BarMark,
  index: number,
  pal: ReturnType<typeof chartPalette>,
): { color: string; borderColor: string } {
  const upDay = bar.close >= bar.open
  if (mark.lianban.has(index)) return { color: pal.magA(0.9), borderColor: pal.mag }
  if (mark.limitUp.has(index)) return { color: pal.goldA(0.9), borderColor: pal.gold }
  if (mark.broken.has(index)) return { color: pal.downA(0.85), borderColor: pal.down }
  if (mark.whiteExit.has(index)) return { color: pal.magA(0.75), borderColor: pal.mag }
  if (mark.shortBuy.has(index)) return { color: pal.goldA(0.75), borderColor: pal.gold }
  if (mark.crash.has(index)) return { color: pal.flatA(0.6), borderColor: pal.flat }
  if (state === "hold") return { color: pal.upA(0.85), borderColor: pal.up }
  if (state === "watch") return { color: pal.flatA(0.45), borderColor: pal.flat }
  return upDay
    ? { color: pal.upA(0.35), borderColor: pal.up }
    : { color: pal.downA(0.8), borderColor: pal.down }
}

/** 趋势线 tab 主图 K 线变色（主图公式.md）：偏差>0 红 / <0 绿；拐点黄进蓝出 */
function trendCandleStyle(
  bar: DailyBar,
  state: string,
  pal: ReturnType<typeof chartPalette>,
): { color: string; borderColor: string } {
  const upDay = bar.close >= bar.open
  if (state === "turn_up") return { color: pal.goldA(0.9), borderColor: pal.gold }
  if (state === "turn_down") return { color: pal.blueA(0.9), borderColor: pal.blue }
  if (state === "up") return { color: pal.upA(0.85), borderColor: pal.up }
  if (state === "down") return { color: pal.downA(0.85), borderColor: pal.down }
  return upDay
    ? { color: pal.upA(0.35), borderColor: pal.up }
    : { color: pal.downA(0.8), borderColor: pal.down }
}

export function DailyKChart({ payload, subView, onNeedMoreHistory, loadingMore }: DailyKChartProps) {
  const theme = useTheme()
  const pal = useMemo(() => chartPalette(theme), [theme])
  // 用户拖动/缩放的视口百分比（null=未交互，用默认末 60 根）
  const zoomRef = useRef<{ start: number; end: number } | null>(null)
  // 上一次渲染的 bar 数：识别"向前加载更多"造成的前插，平移视口保持锚定
  const prevCountRef = useRef(0)
  const needMoreFiredRef = useRef(false)

  // 一次"向前加载"完成后解除触发锁，允许再次拖到边缘时继续翻页
  useEffect(() => {
    if (!loadingMore) needMoreFiredRef.current = false
  }, [loadingMore])

  const option = useMemo<EChartsOption | null>(() => {
    if (!payload || !payload.bars?.length) return null
    const bars = payload.bars
    const n = bars.length
    const dates = bars.map((b) => fmtDate(b.date))
    const main = payload.formulas?.main ?? { available: false }
    const subR = payload.formulas?.sub_resonance ?? { available: false }
    const subT = payload.formulas?.sub_trend ?? { available: false }
    // 趋势线 tab：主图/副图整套切换为 主图公式.md + 副图公式.md
    const mainT = payload.formulas?.main_trend ?? { available: false }
    const subB = payload.formulas?.sub_brick ?? { available: false }
    const isTrendTab = subView === "trendline" && mainT.available === true

    const mark: BarMark = {
      lianban: new Map((main.markers?.lianban ?? []).map((m) => [m.index, m.count])),
      limitUp: new Set([...(main.markers?.limit_up ?? []), ...(main.markers?.limit_up20 ?? [])]),
      broken: new Set(main.markers?.broken ?? []),
      shortBuy: new Set(main.markers?.short_buy ?? []),
      whiteExit: new Set(main.markers?.white_exit ?? []),
      crash: new Set(main.markers?.crash ?? []),
      baotuan: new Set(main.markers?.baotuan ?? []),
      gaowei: new Set(main.markers?.gaowei ?? []),
    }
    const states = main.candle_state ?? bars.map(() => "normal")

    const candleData = bars.map((b, i) => ({
      value: [b.open, b.close, b.low, b.high],
      itemStyle: isTrendTab
        ? trendCandleStyle(b, mainT.candle_state?.[i] ?? "normal", pal)
        : candleStyle(b, states[i] ?? "normal", mark, i, pal),
    }))

    // 主图标记点：文本徽章用 symbol 透明 + label 呈现
    const textPoints: Record<string, unknown>[] = []
    for (const i of mark.baotuan) {
      textPoints.push({
        coord: [dates[i], bars[i].low],
        value: "★抱团炒妖",
        symbol: "none",
        label: { show: true, formatter: "★抱团炒妖", color: pal.gold, fontSize: 9, fontWeight: 700, position: "bottom", distance: 2 },
      })
    }
    for (const i of mark.gaowei) {
      textPoints.push({
        coord: [dates[i], bars[i].high],
        value: "高危",
        symbol: "none",
        label: { show: true, formatter: "高危", color: pal.mag, fontSize: 9, fontWeight: 700, position: "top", distance: 2 },
      })
    }
    for (const i of mark.broken) {
      textPoints.push({
        coord: [dates[i], bars[i].high],
        value: "炸",
        symbol: "none",
        label: { show: true, formatter: "炸", color: pal.down, fontSize: 9, fontWeight: 700, position: "top", distance: 2 },
      })
    }
    for (const [i, count] of mark.lianban) {
      if (count < 3) continue
      textPoints.push({
        coord: [dates[i], bars[i].high],
        value: `${count}板`,
        symbol: "none",
        label: { show: true, formatter: `${count}板`, color: pal.textStrong, fontSize: 9, fontWeight: 700, position: "top", distance: mark.broken.has(i) || mark.gaowei.has(i) ? 12 : 2 },
      })
    }
    for (const i of mark.shortBuy) {
      textPoints.push({
        coord: [dates[i], bars[i].low],
        value: "短买",
        symbol: "triangle",
        symbolSize: 7,
        itemStyle: { color: pal.gold, borderColor: pal.symbolBorder, borderWidth: 0.5 },
        label: { show: false },
      })
    }
    for (const i of mark.whiteExit) {
      textPoints.push({
        coord: [dates[i], bars[i].high],
        value: "离场",
        symbol: "triangle",
        symbolRotate: 180,
        symbolSize: 7,
        itemStyle: { color: pal.mag, borderColor: pal.symbolBorder, borderWidth: 0.5 },
        label: { show: false },
      })
    }

    // 趋势线 tab 主图标记（主图公式.md）：ATR 通道突破——高位止盈蓝标（高点上方）、
    // 低位抄底黄标（低点下方），各带 5 根水平划线（DRAWSL 斜率 0 向右 5 根）。
    const trendPoints: Record<string, unknown>[] = []
    const trendMarkLines: unknown[] = []
    if (isTrendTab) {
      for (const m of mainT.markers?.atr_high ?? []) {
        const endIdx = Math.min(m.index + 5, n - 1)
        trendPoints.push({
          coord: [dates[m.index], m.price],
          symbol: "triangle",
          symbolRotate: 180,
          symbolSize: 8,
          itemStyle: { color: pal.blue, borderColor: pal.symbolBorder, borderWidth: 0.5 },
          label: { show: true, formatter: m.price.toFixed(2), color: pal.blue, fontSize: 9, fontWeight: 700, position: "top", distance: 3 },
        })
        trendMarkLines.push([{ coord: [dates[m.index], m.price], lineStyle: { color: pal.blue, width: 1.2 } }, { coord: [dates[endIdx], m.price] }])
      }
      for (const m of mainT.markers?.atr_low ?? []) {
        const endIdx = Math.min(m.index + 5, n - 1)
        trendPoints.push({
          coord: [dates[m.index], m.price],
          symbol: "triangle",
          symbolSize: 8,
          itemStyle: { color: pal.gold, borderColor: pal.symbolBorder, borderWidth: 0.5 },
          label: { show: true, formatter: m.price.toFixed(2), color: pal.gold, fontSize: 9, fontWeight: 700, position: "bottom", distance: 3 },
        })
        trendMarkLines.push([{ coord: [dates[m.index], m.price], lineStyle: { color: pal.gold, width: 1.2 } }, { coord: [dates[endIdx], m.price] }])
      }
    }

    // SWL/SWS 红青带：透明底 + 正/负差值两条堆叠面积
    const swl = main.swl ?? []
    const sws = main.sws ?? []
    const bandBase = bars.map((_, i) => {
      const a = swl[i]
      const b = sws[i]
      return a != null && b != null ? Math.min(a, b) : null
    })
    const bandUp = bars.map((_, i) => {
      const a = swl[i]
      const b = sws[i]
      return a != null && b != null && a > b ? a - b : 0
    })
    const bandDown = bars.map((_, i) => {
      const a = swl[i]
      const b = sws[i]
      return a != null && b != null && a < b ? b - a : 0
    })

    const mainMarkLines: Record<string, unknown>[] = []
    if (main.strong_support != null) {
      mainMarkLines.push({
        yAxis: main.strong_support,
        lineStyle: { color: pal.markLine, type: "dashed", width: 1 },
        label: { show: true, formatter: `---强支撑 ${main.strong_support.toFixed(2)}`, color: pal.axis, fontSize: 9, position: "insideEndTop" },
      })
    }

    const volColors = bars.map((b) => (b.close >= b.open ? pal.upA(0.7) : pal.downA(0.7)))

    // ---- 副图系列 ----
    const subSeries: Record<string, unknown>[] = []
    const subMarkLines: Record<string, unknown>[] = []
    let subName = ""
    if (subView === "resonance" && subR.available) {
      subName = "AI主力双共振F"
      const z7 = subR.z7 ?? []
      const z8 = subR.z8 ?? []
      const bandBaseZ = bars.map((_, i) => {
        const a = z7[i]
        const b = z8[i]
        return a != null && b != null ? Math.min(a, b) : null
      })
      const bandUpZ = bars.map((_, i) => {
        const a = z7[i]
        const b = z8[i]
        return a != null && b != null && a > b ? a - b : 0
      })
      const bandDownZ = bars.map((_, i) => {
        const a = z7[i]
        const b = z8[i]
        return a != null && b != null && a < b ? b - a : 0
      })
      subSeries.push(
        { name: "大单带底", type: "line", data: bandBaseZ, stack: "zb", showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true, emphasis: { disabled: true } },
        { name: "大单净流入", type: "line", data: bandUpZ, stack: "zb", showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { color: pal.upA(0.22) }, silent: true, emphasis: { disabled: true } },
        { name: "大单净流出", type: "line", data: bandDownZ, stack: "zb", showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { color: pal.downA(0.22) }, silent: true, emphasis: { disabled: true } },
        { name: "大单买力", type: "line", data: z7, showSymbol: false, lineStyle: { color: pal.gold, width: 1 }, emphasis: { disabled: true } },
        { name: "大单卖力", type: "line", data: z8, showSymbol: false, lineStyle: { color: pal.down, width: 1 }, emphasis: { disabled: true } },
        { name: "主力动能WWW", type: "line", data: subR.www ?? [], showSymbol: false, lineStyle: { color: pal.up, width: 1 }, areaStyle: { color: pal.upA(0.3) }, emphasis: { disabled: true } },
        { name: "趋势波动线", type: "line", data: subR.tdxlfxj ?? [], showSymbol: false, lineStyle: { color: pal.flat, width: 1 }, emphasis: { disabled: true } },
      )
      // 底部三层金条 + 信号柱（透明底座堆叠定位）
      const stripBar = (flags: number[] | undefined, base: number, height: number, color: string, name: string) => {
        if (!flags?.length) return
        subSeries.push(
          { name: `${name}底座`, type: "bar", data: flags.map((f) => (f ? base : null)), stack: `strip-${name}`, barWidth: "95%", itemStyle: { color: "transparent" }, silent: true, emphasis: { disabled: true }, tooltip: { show: false } },
          { name, type: "bar", data: flags.map((f) => (f ? height : null)), stack: `strip-${name}`, barWidth: "95%", itemStyle: { color }, silent: true, emphasis: { disabled: true }, tooltip: { show: false } },
        )
      }
      stripBar(subR.strip_weak, 0, 8, pal.downA(0.85), "弱势蓄势")
      stripBar(subR.strip_mid, 15, 10, pal.goldA(0.9), "多头区")
      stripBar(subR.strip_top, 30, 10, pal.goldA(0.9), "强攻区")
      const sigBar = (idxList: number[] | undefined, base: number, height: number, color: string, name: string) => {
        if (!idxList?.length) return
        const flags = new Set(idxList)
        subSeries.push(
          { name: `${name}底座`, type: "bar", data: bars.map((_, i) => (flags.has(i) ? base : null)), stack: `sig-${name}`, barWidth: "70%", itemStyle: { color: "transparent" }, silent: true, emphasis: { disabled: true }, tooltip: { show: false } },
          { name, type: "bar", data: bars.map((_, i) => (flags.has(i) ? height : null)), stack: `sig-${name}`, barWidth: "70%", itemStyle: { color }, silent: true, emphasis: { disabled: true } },
        )
      }
      sigBar(subR.markers?.reversal, 50, 45, pal.magA(0.95), "★★反转拐点")
      sigBar(subR.markers?.start, 30, 10, pal.blueA(0.9), "控盘启动")
      subMarkLines.push(
        { yAxis: -85, lineStyle: { color: pal.cyan, type: "dotted", width: 1 }, label: { show: false } },
        { yAxis: 95, lineStyle: { color: pal.down, type: "dotted", width: 1 }, label: { show: false } },
        { yAxis: 0, lineStyle: { color: pal.zeroLine, width: 1 }, label: { show: false } },
      )
      // 双共振红灯（钻石标记在 0 轴）
      const redLights = (subR.markers?.red_light ?? []).slice(-60)
      if (redLights.length) {
        subSeries.push({
          name: "双共振红灯",
          type: "scatter",
          data: redLights.map((i) => ({ coord: [dates[i], 0], value: 0 })),
          symbol: "diamond",
          symbolSize: 8,
          itemStyle: { color: pal.gold, borderColor: pal.symbolBorder, borderWidth: 0.5 },
        })
      }
    } else if (subView === "trend" && subT.available) {
      subName = "AI主力动向F"
      const accBar = (data: (number | null)[] | undefined, color: string, name: string, width = "62%") => {
        subSeries.push({ name, type: "bar", data: data ?? [], barWidth: width, itemStyle: { color }, silent: true, emphasis: { disabled: true } })
      }
      accBar(subT.rich_accum, pal.flatA(0.5), "发财吸筹")
      accBar(subT.main_accum, pal.upA(0.9), "主力吸筹")
      accBar(subT.trend_accum, "hsl(187 85% 53% / 0.8)", "趋势吸筹")
      const pinBar = (idxList: number[] | undefined, color: string, name: string) => {
        if (!idxList?.length) return
        const flags = new Set(idxList)
        const rich = subT.rich_accum ?? []
        subSeries.push({
          name,
          type: "bar",
          data: bars.map((_, i) => (flags.has(i) ? rich[i] ?? 1 : null)),
          barWidth: 2,
          itemStyle: { color },
          silent: true,
          emphasis: { disabled: true },
        })
      }
      pinBar(subT.markers?.yellow_pin, pal.gold, "黄柱试盘")
      pinBar(subT.markers?.pink_pin, pal.mag, "粉柱试盘")
      const sigBar2 = (idxList: number[] | undefined, height: number, color: string, name: string) => {
        if (!idxList?.length) return
        const flags = new Set(idxList)
        subSeries.push({
          name,
          type: "bar",
          data: bars.map((_, i) => (flags.has(i) ? height : null)),
          barWidth: "70%",
          itemStyle: { color },
          silent: true,
          emphasis: { disabled: true },
        })
      }
      sigBar2(subT.markers?.niu, 100, theme === "dark" ? "hsl(220 15% 92%)" : "hsl(215 30% 30%)", "牛股")
      sigBar2(subT.markers?.shao, 60, pal.magA(0.9), "前哨")
      sigBar2(subT.markers?.jigou_chu, 40, pal.downA(0.9), "机构出货")
      sigBar2(subT.markers?.zhuli_chu, 30, pal.blueA(0.85), "主力出货")
      subSeries.push(
        { name: "趋势线", type: "line", data: subT.trend_line ?? [], showSymbol: false, lineStyle: { color: pal.blue, width: 1.4 }, emphasis: { disabled: true } },
        { name: "冲顶区", type: "line", data: (subT.chongding ?? []).map((v) => (v == null ? null : Math.min(v + 20, 120))), showSymbol: false, lineStyle: { color: pal.gold, width: 1, type: "dashed", opacity: 0.7 }, emphasis: { disabled: true } },
      )
      const trendVals = subT.trend_line ?? []
      const iconPoints = (idxList: number[] | undefined, color: string, rotate = 0) =>
        (idxList ?? []).slice(-40).map((i) => ({
          coord: [dates[i], (trendVals[i] ?? 0) + 6],
          symbol: "triangle",
          symbolRotate: rotate,
          symbolSize: 8,
          itemStyle: { color, borderColor: pal.symbolBorder, borderWidth: 0.5 },
          label: { show: false },
        }))
      subSeries.push({
        name: "资金拐点",
        type: "scatter",
        data: iconPoints(subT.markers?.red_hat, pal.up, 180),
        emphasis: { disabled: true },
      })
      subSeries.push({
        name: "趋势突破",
        type: "scatter",
        data: iconPoints(subT.markers?.red_triangle, pal.up, 0),
        emphasis: { disabled: true },
      })
      subMarkLines.push({ yAxis: 0, lineStyle: { color: pal.zeroLine, width: 1 }, label: { show: false } })
    } else {
      // 趋势线 tab 副图（副图公式.md）：砖型图（candlestick 画砖：open=前值 close=今值，
      // 涨红跌绿平白）+ 短买黄块（砖底下方）/ 离场青块（砖顶上方）
      subName = "砖型图 · 短买/离场"
      const brick = subB.brick ?? []
      const buySet = new Set(subB.markers?.short_buy ?? [])
      const exitSet = new Set(subB.markers?.exit ?? [])
      const brickData: Record<string, unknown>[] = []
      const buyBlocks: Record<string, unknown>[] = []
      const exitBlocks: Record<string, unknown>[] = []
      bars.forEach((_, i) => {
        const cur = brick[i] ?? 0
        const prev = i > 0 ? (brick[i - 1] ?? 0) : 0
        const lo = Math.min(prev, cur)
        const hi = Math.max(prev, cur)
        const flat = cur === prev && cur > 0
        // 砖值为 0（未出砖）时通达信不画任何柱，对齐原样跳过
        brickData.push(
          cur === 0 && prev === 0
            ? { value: null }
            : {
                value: [prev, cur, lo, hi],
                ...(flat ? { itemStyle: { color: pal.textStrong, color0: pal.textStrong, borderColor: pal.textStrong, borderColor0: pal.textStrong } } : {}),
              },
        )
        // 短买：黄色块贴砖底下方（砖底-8 ~ 砖底-3，厚 5 个坐标位）
        buyBlocks.push(
          buySet.has(i)
            ? { value: [lo - 8, lo - 3, lo - 8, lo - 3], itemStyle: { color: pal.goldA(0.95), color0: pal.goldA(0.95), borderColor: "transparent", borderColor0: "transparent" } }
            : { value: null },
        )
        // 离场：青色块贴砖顶上方（砖顶+3 ~ 砖顶+8）
        exitBlocks.push(
          exitSet.has(i)
            ? { value: [hi + 3, hi + 8, hi + 3, hi + 8], itemStyle: { color: pal.cyan, color0: pal.cyan, borderColor: "transparent", borderColor0: "transparent" } }
            : { value: null },
        )
      })
      subSeries.push(
        {
          name: "砖型图",
          type: "candlestick",
          data: brickData,
          barWidth: "70%",
          itemStyle: { color: pal.upA(0.9), color0: pal.downA(0.9), borderColor: pal.up, borderColor0: pal.down, borderWidth: 0.5 },
          silent: true,
          emphasis: { disabled: true },
        },
        { name: "短买", type: "candlestick", data: buyBlocks, barWidth: "70%", silent: true, emphasis: { disabled: true } },
        { name: "离场", type: "candlestick", data: exitBlocks, barWidth: "70%", silent: true, emphasis: { disabled: true } },
      )
      subMarkLines.push({ yAxis: 0, lineStyle: { color: pal.zeroLine, width: 1 }, label: { show: false } })
    }

    // 三个副图 tab 都保留最底格成交量
    const grids = [
      { left: 52, right: 60, top: 8, height: "50%" },
      { left: 52, right: 60, top: "58.5%", height: "24%" },
      { left: 52, right: 60, top: "86.5%", height: "10.5%" },
    ]
    const volGridIndex = 2

    const volSeries = [
      {
        name: "成交量",
        type: "bar",
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: bars.map((b, i) => ({ value: b.vol, itemStyle: { color: volColors[i] } })),
        barWidth: "70%",
        emphasis: { disabled: true },
      },
    ]

    const xAxes = grids.map((_, gi) => ({
      type: "category" as const,
      data: dates,
      gridIndex: gi,
      axisLine: { lineStyle: { color: pal.grid } },
      axisTick: { show: false },
      axisLabel: gi === grids.length - 1
        ? { color: pal.axis, fontSize: 9, interval: Math.ceil(n / 10) }
        : { show: false },
      splitLine: { show: false },
    }))
    const yAxes = grids.map((_, gi) => ({
      scale: true,
      gridIndex: gi,
      axisLabel: gi === volGridIndex
        ? { show: false }
        : { color: pal.axis, fontSize: 9 },
      splitLine: gi === 0 ? { lineStyle: { color: pal.grid, type: "dashed" as const } } : { show: false },
    }))

    const mainSeries: Record<string, unknown>[] = isTrendTab
      ? [
          {
            name: "日K",
            type: "candlestick",
            xAxisIndex: 0,
            yAxisIndex: 0,
            data: candleData,
            barWidth: "70%",
            barMaxWidth: 26,
            markPoint: { silent: true, data: trendPoints, symbolKeepAspect: true },
            markLine: { silent: true, symbol: "none", data: trendMarkLines },
            emphasis: { disabled: true },
            z: 3,
          },
          { name: "知行短期趋势线", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: mainT.zx_trend ?? [], showSymbol: false, lineStyle: { color: pal.flat, width: 1.2 }, emphasis: { disabled: true }, z: 2 },
          { name: "知行多空线", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: mainT.zx_duokong ?? [], showSymbol: false, lineStyle: { color: pal.gold, width: 2 }, emphasis: { disabled: true }, z: 2 },
        ]
      : [
      {
        name: "日K",
        type: "candlestick",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: candleData,
        barWidth: "70%",
        barMaxWidth: 26,
        markPoint: { silent: true, data: textPoints, symbolKeepAspect: true },
        markLine: { silent: true, symbol: "none", data: mainMarkLines },
        emphasis: { disabled: true },
        z: 3,
      },
      { name: "带底", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: bandBase, stack: "swl-band", showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true, emphasis: { disabled: true } },
      { name: "红带(多头)", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: bandUp, stack: "swl-band", showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { color: pal.upA(0.13) }, silent: true, emphasis: { disabled: true } },
      { name: "青带(空头)", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: bandDown, stack: "swl-band", showSymbol: false, lineStyle: { opacity: 0 }, areaStyle: { color: "hsl(187 85% 53% / 0.13)" }, silent: true, emphasis: { disabled: true } },
      { name: "SWL", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: swl, showSymbol: false, lineStyle: { color: pal.up, width: 1.2 }, emphasis: { disabled: true }, z: 2 },
      { name: "SWS", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: sws, showSymbol: false, lineStyle: { color: pal.textMuted, width: 1, type: "dotted" }, emphasis: { disabled: true }, z: 2 },
      { name: "LJX·EMA20", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: main.ljx ?? [], showSymbol: false, lineStyle: { color: pal.flat, width: 1, type: "dashed", opacity: 0.8 }, emphasis: { disabled: true }, z: 2 },
      { name: "市场成本线", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: main.cost_line ?? [], showSymbol: false, lineStyle: { color: pal.gold, width: 1, type: "dotted" }, emphasis: { disabled: true }, z: 2 },
    ]
    const subSeriesPatched = subSeries.map((s) => ({ xAxisIndex: 1, yAxisIndex: 1, ...s }))

    // ---- 视口（dataZoom）状态 ----
    // 1) 用户拖动/缩放过：沿用具象百分比；
    // 2) 向前加载了更多历史（前插 added 根）：按绝对索引平移视口，视觉锚定同一批K线；
    // 3) 首次：默认末 60 根（蜡烛更大更清晰；滚轮/拖动可继续缩放平移）。
    let dzStart: number
    let dzEnd: number
    const prevCount = prevCountRef.current
    if (zoomRef.current && prevCount > 0 && n > prevCount) {
      const added = n - prevCount
      const anchorStart = (zoomRef.current.start / 100) * (prevCount - 1) + added
      const anchorEnd = (zoomRef.current.end / 100) * (prevCount - 1) + added
      dzStart = (anchorStart / (n - 1)) * 100
      dzEnd = Math.min(100, (anchorEnd / (n - 1)) * 100)
      zoomRef.current = { start: dzStart, end: dzEnd }
    } else if (zoomRef.current) {
      dzStart = zoomRef.current.start
      dzEnd = zoomRef.current.end
    } else {
      dzStart = Math.max(0, 100 - (60 / n) * 100)
      dzEnd = 100
    }
    prevCountRef.current = n

    return {
      animation: false,
      backgroundColor: "transparent",
      grid: grids,
      axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: pal.axisPointer } },
      dataZoom: [
        {
          type: "inside",
          xAxisIndex: grids.map((_, gi) => gi),
          start: dzStart,
          end: dzEnd,
          zoomOnMouseWheel: true,
          moveOnMouseMove: true,
        },
      ],
      tooltip: {
        trigger: "axis",
        backgroundColor: pal.tooltipBg,
        borderColor: pal.tooltipBorder,
        textStyle: { color: pal.tooltipText, fontSize: 11 },
        formatter: (params) => {
          const arr = Array.isArray(params) ? params : [params]
          const idx = arr[0]?.dataIndex ?? 0
          const b = bars[idx]
          if (!b) return ""
          const prev = idx > 0 ? bars[idx - 1].close : payload.prev_close
          const pct = prev > 0 ? ((b.close / prev - 1) * 100) : 0
          const pctColor = pct >= 0 ? pal.up : pal.down
          const lines = [
            `<div style="font-weight:700;margin-bottom:2px">${fmtDateFull(b.date)}</div>`,
            `<div>开 <b>${b.open.toFixed(2)}</b> 高 <b style="color:${pal.up}">${b.high.toFixed(2)}</b> 低 <b style="color:${pal.down}">${b.low.toFixed(2)}</b> 收 <b>${b.close.toFixed(2)}</b> <span style="color:${pctColor}">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</span></div>`,
            `<div style="color:${pal.axis}">量 ${(b.vol / 10000).toFixed(0)}万股 · 额 ${(b.amount / 1e8).toFixed(2)}亿</div>`,
          ]
          const tag: string[] = []
          if (mark.lianban.has(idx)) tag.push(`${mark.lianban.get(idx)}连板`)
          if (mark.limitUp.has(idx)) tag.push("涨停")
          if (mark.broken.has(idx)) tag.push("炸板")
          if (mark.shortBuy.has(idx)) tag.push("短买")
          if (mark.whiteExit.has(idx)) tag.push("离场")
          if (mark.baotuan.has(idx)) tag.push("★抱团炒妖")
          if (mark.gaowei.has(idx)) tag.push("高危")
          if (states[idx] === "hold") tag.push("红色持股")
          if (states[idx] === "watch") tag.push("青色观望")
          if (tag.length) lines.push(`<div style="color:${pal.gold}">${tag.join(" · ")}</div>`)
          return lines.join("")
        },
      },
      xAxis: xAxes,
      yAxis: yAxes,
      series: [...mainSeries, ...subSeriesPatched, ...volSeries],
      // 副图顶部的名称标签走 graphic，避免占用图例空间
      graphic: [
        {
          type: "text",
          left: 56,
          top: "55.5%",
          style: { text: subName, fill: pal.axis, fontSize: 10, fontWeight: 600 },
          silent: true,
        },
      ],
    }
  }, [payload, subView, pal, theme])

  const ref = useECharts(
    option,
    {
      datazoom: (params: unknown) => {
        const p = params as { batch?: { start?: number; end?: number }[]; start?: number; end?: number }
        const b = Array.isArray(p?.batch) ? p.batch[0] : p
        if (b?.start == null || b?.end == null) return
        zoomRef.current = { start: b.start, end: b.end }
        // 拖到最左边缘时向前翻页加载更早历史（触发一次，等加载完成重置）
        if (b.start < 6 && !loadingMore && !needMoreFiredRef.current) {
          needMoreFiredRef.current = true
          onNeedMoreHistory?.()
        } else if (b.start >= 6) {
          needMoreFiredRef.current = false
        }
      },
    },
    [subView],
  )
  if (!payload || !payload.bars?.length) {
    return <div className="flex h-full items-center justify-center text-xs text-muted-foreground">日K数据加载中…</div>
  }
  return <div ref={ref} className="h-full w-full" />
}
