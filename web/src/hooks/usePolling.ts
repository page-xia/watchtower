import { useCallback, useEffect, useRef, useState } from "react"

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
  const seqRef = useRef(0)

  const run = useCallback(async (showLoading: boolean) => {
    const seq = ++seqRef.current
    if (showLoading) setLoading(true)
    try {
      const result = await fetcherRef.current()
      if (seq !== seqRef.current) return
      setData(result)
      setError(null)
      setLastOkAt(Date.now())
    } catch (e) {
      if (seq !== seqRef.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    // 关键：deps 变化（如切换板块）时保留上一份数据，后台静默刷新，
    // 避免整页清空闪烁；仅首次无数据时显示加载态。
    run(false)
    if (intervalMs <= 0) return
    const timer = window.setInterval(() => run(false), intervalMs)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, run, ...deps])

  return { data, error, loading, lastOkAt, refresh: () => void run(false) }
}
