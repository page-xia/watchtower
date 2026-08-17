import { useCallback, useEffect, useRef, useState } from "react"
import * as echarts from "echarts"
import type { EChartsOption } from "echarts"

/**
 * 挂载一个 ECharts 实例到 div。
 * 使用 callback ref：宿主 div 首次挂载（包括从加载态切换而来）时才初始化实例，
 * option 变化时 setOption，容器尺寸变化时自动 resize。
 */
export function useECharts(
  option: EChartsOption | null,
  events?: Record<string, (params: unknown) => void>,
  /** 这些依赖变化时下一次 setOption 用 notMerge 整图重建（用于切换系列数量不同的视图，避免旧系列残留） */
  resetDeps: unknown[] = [],
) {
  const chartRef = useRef<echarts.ECharts | null>(null)
  const [node, setNode] = useState<HTMLDivElement | null>(null)
  const optionRef = useRef(option)
  optionRef.current = option
  // 事件回调走 ref：option/榜单每次推送都会生成新闭包，不能让 chart.on 持有过期数据
  const eventsRef = useRef(events)
  eventsRef.current = events
  const resetRef = useRef(false)

  const ref = useCallback((el: HTMLDivElement | null) => setNode(el), [])

  useEffect(() => {
    resetRef.current = true
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, resetDeps)

  useEffect(() => {
    if (!node) return
    const chart = echarts.init(node, undefined, { renderer: "canvas" })
    chartRef.current = chart
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(node)
    for (const evt of Object.keys(eventsRef.current ?? {})) {
      chart.on(evt, (params: unknown) => eventsRef.current?.[evt]?.(params))
    }
    if (optionRef.current) {
      chart.setOption(optionRef.current, { notMerge: true })
    }
    return () => {
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [node])

  useEffect(() => {
    if (option && chartRef.current) {
      if (resetRef.current) {
        // 视图切换：整图重建，清掉上一视图的多余系列
        chartRef.current.setOption(option, { notMerge: true })
        resetRef.current = false
      } else {
        // 合并模式增量更新：数据刷新时 ECharts 就地过渡，不整图重绘、不闪烁
        chartRef.current.setOption(option, { notMerge: false, lazyUpdate: true })
      }
    }
  }, [option])

  return ref
}
