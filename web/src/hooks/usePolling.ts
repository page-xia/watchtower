import { useCallback, useEffect, useRef, useState } from "react"
import { pollingDecision, type PollingDecision } from "@/lib/marketRefresh"
import { useDocumentHidden } from "@/hooks/useDocumentHidden"

export interface PollingState<T> {
  data: T | null
  error: string | null
  loading: boolean
  lastOkAt: number | null
  refresh: () => void
}

/**
 * 通用轮询 hook：立即执行一次，之后按 intervalMs 轮询。
 * fetcher 变化（deps 变化）时重置并重新拉取。
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = [],
): PollingState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastOkAt, setLastOkAt] = useState<number | null>(null)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const dataRef = useRef<T | null>(null)
  const seqRef = useRef(0)
  const documentHidden = useDocumentHidden()
  const intervalRef = useRef(intervalMs)
  const hiddenRef = useRef(documentHidden)
  intervalRef.current = intervalMs
  hiddenRef.current = documentHidden
  const [cadence, setCadence] = useState<PollingDecision>(() =>
    pollingDecision({
      baseIntervalMs: intervalMs,
      hasData: false,
      documentHidden,
    }),
  )

  const updateCadence = useCallback((nextData: T | null) => {
    setCadence(
      pollingDecision({
        baseIntervalMs: intervalRef.current,
        source: nextData as object | null,
        hasData: nextData !== null,
        documentHidden: hiddenRef.current,
      }),
    )
  }, [])

  const run = useCallback(async (showLoading: boolean) => {
    const seq = ++seqRef.current
    if (showLoading) setLoading(true)
    try {
      const result = await fetcherRef.current()
      if (seq !== seqRef.current) return
      dataRef.current = result
      setData(result)
      setError(null)
      setLastOkAt(Date.now())
      updateCadence(result)
    } catch (e) {
      if (seq !== seqRef.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
  }, [updateCadence])

  useEffect(() => {
    updateCadence(dataRef.current)
  }, [documentHidden, intervalMs, updateCadence])

  useEffect(() => {
    // 关键：deps 变化（如切换板块）时保留上一份数据，后台静默刷新，
    // 避免整页清空闪烁；仅首次无数据时显示加载态。
    run(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, run, ...deps])

  useEffect(() => {
    if (intervalMs <= 0 || !cadence.enabled || cadence.intervalMs === null) return
    const timer = window.setInterval(() => run(false), cadence.intervalMs)
    return () => window.clearInterval(timer)
  }, [cadence.enabled, cadence.intervalMs, intervalMs, run])

  return { data, error, loading, lastOkAt, refresh: () => void run(false) }
}
