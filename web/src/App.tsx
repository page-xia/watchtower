import { useCallback, useEffect, useMemo, useState } from "react"
import { syncPushCodes } from "@/lib/pushSubscription"
import {
  applyLocalWatchlistToPayload,
  loadLocalWatchlist,
  localWatchlistPlaceholders,
  removeLocalWatchlist,
  saveLocalWatchlist,
  upsertLocalWatchlist,
  watchlistCodes,
} from "@/lib/localWatchlist"
import { useLiveChannel } from "@/hooks/useLiveChannel"
import { useTerminalStream } from "@/hooks/useTerminalStream"
import { refreshPolicyFromPayload } from "@/lib/marketRefresh"
import type { BoardItem, IndexMinutesResponse } from "@/types/api"
import { TopBar } from "@/components/TopBar"
import { MarketStrip } from "@/components/MarketStrip"
import { SectorPanel } from "@/components/SectorPanel"
import { SectorFlowChart } from "@/components/SectorFlowChart"
import { SectorNetInflowChart } from "@/components/SectorNetInflowChart"
import { LimitUpLadderChart } from "@/components/LimitUpLadderChart"
import { DarkPoolPanel } from "@/components/DarkPoolPanel"
import { StockBoardPanel } from "@/components/StockBoard"
import { RightRail } from "@/components/RightRail"
import { StockDetail } from "@/components/detail/StockDetail"

export default function App() {
  const [sector, setSector] = useState<string | null>(null)
  // 板块口径：1/2/3 = 申万行业级别；4/5/6 = 通达信概念/风格/地区
  const [boardLevel, setBoardLevel] = useState(3)
  const [sort, setSort] = useState("activity")
  const [page, setPage] = useState(1)
  // 过滤开关持久化：刷新页面后保持上次选择（与主题一样走 localStorage）
  const [nearTrend, setNearTrend] = useState(() => localStorage.getItem("board-near-trend") === "1")
  const [pinBuy, setPinBuy] = useState(() => localStorage.getItem("board-pin-buy") === "1")
  const [detailCode, setDetailCode] = useState<string | null>(null)
  const [localWatchlist, setLocalWatchlist] = useState(() => loadLocalWatchlist())
  const localWatchlistCodes = useMemo(() => watchlistCodes(localWatchlist), [localWatchlist])

  // 已开启飞书推送时，自选股变化自动同步到后端监听池（内部按签名去抖）
  useEffect(() => {
    void syncPushCodes(localWatchlistCodes)
  }, [localWatchlistCodes])

  // 全页共用一条持久 /ws/live：终端、指数分钟线、暗盘资金都在同一连接上
  // 订阅/切换；页面交互只换频道，不关闭浏览器 WebSocket。
  const stream = useTerminalStream({ sector, boardLevel, sort, page, pageSize: 40, nearTrend, pinBuy, watchlistCodes: localWatchlistCodes })
  const refreshStream = stream.refresh
  const indexMinutes = useLiveChannel<IndexMinutesResponse>("index_minutes", {})

  const data = useMemo(() => applyLocalWatchlistToPayload(stream.data, localWatchlist), [stream.data, localWatchlist])
  // 休市判定：盘后/非交易日后端会冻结并停流，此时断线属正常“休息中”而非连接异常
  const marketClosed = useMemo(() => {
    if (!data) return false
    const policy = refreshPolicyFromPayload(data)
    return policy?.traffic_mode === "static" || policy?.is_trading_window === false || policy?.should_stream === false
  }, [data])
  const boardData = data?.stock_board ?? null
  const watchlistForRail = useMemo(
    () => (data ? data.watchlist_preview : localWatchlistPlaceholders(localWatchlist)),
    [data, localWatchlist],
  )

  // 当前选中板块的领涨锚（龙头），传给榜单头部展示
  const sectorAnchor = useMemo(() => {
    if (!sector) return null
    return (data?.sectors ?? []).find((s) => s.name === sector) ?? null
  }, [data?.sectors, sector])

  const refreshAll = useCallback(() => {
    stream.refresh()
    indexMinutes.refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.refresh, indexMinutes.refresh])

  const handleSelectSector = useCallback((name: string | null) => {
    setSector(name)
    setPage(1)
  }, [])

  // 暗盘资金面板联动：点击板块桶时同时切换板块口径并选中（name=null 仅清除过滤）
  const handleDarkPoolSelectSector = useCallback((level: number, name: string | null) => {
    setBoardLevel(level)
    setSector(name)
    setPage(1)
  }, [])

  const handleBoardLevel = useCallback((lv: number) => {
    setBoardLevel(lv)
    // 切换口径后原选中项在另一套分类里不存在，直接清空避免带着旧过滤
    setSector(null)
    setPage(1)
  }, [])

  const handleSort = useCallback((s: string) => {
    setSort(s)
    setPage(1)
  }, [])

  const handleToggleNearTrend = useCallback(() => {
    setNearTrend((v) => {
      localStorage.setItem("board-near-trend", v ? "0" : "1")
      return !v
    })
    setPage(1)
  }, [])

  const handleTogglePinBuy = useCallback(() => {
    setPinBuy((v) => {
      localStorage.setItem("board-pin-buy", v ? "0" : "1")
      return !v
    })
    setPage(1)
  }, [])

  const handleToggleWatch = useCallback(
    (item: BoardItem) => {
      setLocalWatchlist((current) => {
        const next = item.watchlisted ? removeLocalWatchlist(current, item.code) : upsertLocalWatchlist(current, item)
        saveLocalWatchlist(next)
        return next
      })
      refreshStream()
    },
    [refreshStream],
  )

  const handleDetailToggleWatch = useCallback(
    (code: string, name: string, watchlisted: boolean) => {
      setLocalWatchlist((current) => {
        const next = watchlisted ? removeLocalWatchlist(current, code) : upsertLocalWatchlist(current, { code, name })
        saveLocalWatchlist(next)
        return next
      })
      refreshStream()
    },
    [refreshStream],
  )

  const handleRemoveWatch = useCallback(
    (code: string) => {
      setLocalWatchlist((current) => {
        const next = removeLocalWatchlist(current, code)
        saveLocalWatchlist(next)
        return next
      })
      refreshStream()
    },
    [refreshStream],
  )

  const fatalError = stream.error && !data ? stream.error : null

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background">
      <TopBar
        connected={stream.connected}
        marketClosed={marketClosed}
        updatedAt={data?.market?.updated_at ?? ""}
        dataMode={data?.data_mode ?? ""}
        frozen={!!data?.market?.frozen}
        decisionStage={data?.market?.decision_stage}
        onRefresh={refreshAll}
        refreshing={!data && stream.connected}
        onOpenDetail={setDetailCode}
        watchlistCodes={localWatchlistCodes}
      />

      <div className="shrink-0 px-2 pt-2">
        <MarketStrip market={data?.market ?? null} indexMinutes={indexMinutes.data} />
      </div>

      <main className="grid min-h-0 flex-1 grid-cols-1 gap-2 p-2 lg:grid-cols-[232px_minmax(720px,760px)_minmax(0,1fr)]">
        {/* 左：板块扫描（含净流入额，共振口径按金额读） */}
        <div className="min-h-0 max-lg:h-[420px]">
          <SectorPanel
            sectors={data?.sectors ?? []}
            selected={sector}
            onSelect={handleSelectSector}
            boardLevel={boardLevel}
            onBoardLevel={handleBoardLevel}
          />
        </div>

        {/* 中：活跃股榜单通高（机会队列已移至右侧图表区首位） */}
        <div className="min-h-0 max-lg:h-[720px]">
          <StockBoardPanel
            board={boardData}
            loading={!boardData && stream.connected}
            sort={sort}
            onSort={handleSort}
            page={page}
            onPage={setPage}
            nearTrend={nearTrend}
            onToggleNearTrend={handleToggleNearTrend}
            pinBuy={pinBuy}
            onTogglePinBuy={handleTogglePinBuy}
            onOpenDetail={setDetailCode}
            onToggleWatch={handleToggleWatch}
            sectorAnchor={sectorAnchor}
          />
        </div>

        {/* 右：机会队列（自选/买T卖T）占据原指数共振位，其余为板块图表与暗盘资金 */}
        <div className="grid min-h-0 grid-cols-1 grid-rows-5 gap-2 max-lg:h-[1180px] xl:grid-cols-[1.45fr_1fr] xl:grid-rows-[1fr_1fr_minmax(170px,0.62fr)]">
          <RightRail
            items={boardData?.items ?? []}
            watchlist={watchlistForRail}
            onOpenDetail={setDetailCode}
            onRemoveWatch={handleRemoveWatch}
          />
          <SectorNetInflowChart sectors={data?.sectors ?? []} selected={sector} onSelect={handleSelectSector} />
          <SectorFlowChart series={data?.sector_flow ?? []} selected={sector} onSelect={handleSelectSector} />
          <LimitUpLadderChart sectors={data?.sectors ?? []} selected={sector} onSelect={handleSelectSector} />
          {/* 暗盘资金：横跨整行，置于涨停情绪梯队下方；跟随首页板块选中联动过滤 */}
          <div className="min-h-0 xl:col-span-2">
            <DarkPoolPanel selected={sector} boardLevel={boardLevel} onSelectSector={handleDarkPoolSelectSector} />
          </div>
        </div>
      </main>

      {fatalError && (
        <div className="fixed bottom-3 left-1/2 z-40 -translate-x-1/2 rounded-md border border-destructive/50 bg-destructive/15 px-3 py-1.5 text-[11px] text-destructive">
          后端连接异常：{fatalError}（请确认 127.0.0.1:8788 盯盘后端已启动）
        </div>
      )}

      {detailCode && (
        <StockDetail
          key={detailCode}
          code={detailCode}
          onClose={() => setDetailCode(null)}
          onToggleWatch={handleDetailToggleWatch}
          watchlisted={localWatchlistCodes.includes(detailCode)}
          watchlistCodes={localWatchlistCodes}
        />
      )}
    </div>
  )
}
