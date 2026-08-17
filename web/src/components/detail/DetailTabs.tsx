import { useEffect, useMemo, useRef, useState } from "react"
import type { AuctionSnapshot, ChipDaily, ChipIntraday, DetailExtrasResponse, MessageEvidence } from "@/types/api"
import { getDetailExtras, type DetailExtrasOptions } from "@/lib/api"
import { dateShort, fmtAmount, timeShort } from "@/lib/format"
import { cn } from "@/lib/utils"
import { messageBody, messageKeywords, messageMetaLabels } from "./messagePresentation"
import { F10Pane } from "./F10Pane"
import { CapitalFlowPane, ChanlunPane, IndicatorsPane } from "./ExtrasPanes"
import { ChipDailyPane, ChipIntradayPane } from "./ChipPane"

type TabKey = "chip" | "messages" | "ai" | "auction" | "capital" | "indicators" | "fundamentals" | "chanlun"
type MessageScope = "stock" | "sector"

const TABS: { key: TabKey; label: string }[] = [
  { key: "chip", label: "筹码" },
  { key: "messages", label: "星球消息" },
  { key: "ai", label: "AI分析" },
  { key: "auction", label: "集合竞价" },
  { key: "capital", label: "资金流" },
  { key: "indicators", label: "技术指标" },
  { key: "fundamentals", label: "F10/财务" },
  { key: "chanlun", label: "缠论" },
]

function MessageCard({ msg }: { msg: MessageEvidence }) {
  const [expanded, setExpanded] = useState(false)
  const bullish = msg.direction === "1"
  const body = messageBody(msg)
  const metaLabels = messageMetaLabels(msg)
  const keywords = messageKeywords(msg)
  return (
    <div className="rounded-md border border-border/60 bg-background/40 p-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[12px] font-semibold leading-snug">{msg.topic_title || msg.event_title}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[9px] text-muted-foreground">
            <span>{msg.owner_name}</span>
            <span>{dateShort(msg.create_time)} {timeShort(msg.create_time)}</span>
            {metaLabels.length > 0 && (
              <span className={cn("rounded px-1", bullish ? "bg-up-dim text-up" : "bg-muted text-muted-foreground")}>
                {metaLabels.join(" · ")}
              </span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1 text-[9px] text-muted-foreground">
          <span>置信 <b className="num text-foreground">{Math.round((msg.confidence ?? 0) * 100)}%</b></span>
          <span>相关 <b className="num text-foreground">{Math.round((msg.relevance ?? 0) * 100)}%</b></span>
        </div>
      </div>
      <div
        className={cn("mt-1.5 cursor-pointer whitespace-pre-wrap text-[11px] leading-relaxed text-foreground/80", !expanded && "line-clamp-3")}
        onClick={() => setExpanded(!expanded)}
      >
        {body}
      </div>
      {keywords.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {keywords.slice(0, 8).map((k) => (
            <span key={k} className="rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground">{k}</span>
          ))}
        </div>
      )}
    </div>
  )
}

function MessagesPane({ extras }: { extras: DetailExtrasResponse }) {
  const stock = extras.message_evidence?.stock ?? []
  const sector = extras.message_evidence?.sector ?? []
  const [scope, setScope] = useState<MessageScope>("stock")
  const counts: Record<MessageScope, number> = { stock: stock.length, sector: sector.length }
  const activeScope: MessageScope = counts[scope] > 0 ? scope : stock.length > 0 ? "stock" : "sector"
  const activeMessages = activeScope === "stock" ? stock : sector
  const scopeTabs: { key: MessageScope; label: string }[] = [
    { key: "stock", label: "直接提及个股" },
    { key: "sector", label: "板块相关" },
  ]

  if (stock.length + sector.length === 0) {
    return <div className="p-8 text-center text-xs text-muted-foreground">暂无星球消息关联该股/板块</div>
  }
  return (
    <div className="flex min-h-0 flex-col">
      <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-2 border-b border-border bg-card/95 px-3 py-2 backdrop-blur">
        <div className="inline-flex rounded-md border border-border bg-muted/40 p-0.5">
          {scopeTabs.map((item) => {
            const count = counts[item.key]
            const active = item.key === activeScope
            return (
              <button
                key={item.key}
                type="button"
                disabled={count === 0}
                aria-pressed={active}
                onClick={() => setScope(item.key)}
                className={cn(
                  "rounded px-2.5 py-1 text-[11px] font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-45",
                  active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-background/70",
                )}
              >
                {item.label}
                <span className="num ml-1 rounded bg-primary/15 px-1 text-[9px] text-primary">{count}</span>
              </button>
            )
          })}
        </div>
        <div className="text-[10px] text-muted-foreground">
          当前 {activeScope === "stock" ? "个股" : "板块"} · 共 <span className="num text-foreground">{activeMessages.length}</span> 条
        </div>
      </div>
      <div className="space-y-2 p-3">
        {activeMessages.map((m, i) => (
          <MessageCard key={`${m.event_id || "message"}-${m.entity_type || activeScope}-${m.code || m.name || i}`} msg={m} />
        ))}
      </div>
    </div>
  )
}

function AiPane({ extras }: { extras: DetailExtrasResponse }) {
  const a = extras.analysis
  if (!a || a.status !== "ok" || !a.raw_text) {
    return <div className="p-8 text-center text-xs text-muted-foreground">暂无 AI 分析记录</div>
  }
  return (
    <div className="p-3">
      <div className="mb-2 flex items-center gap-2 text-[10px] text-muted-foreground">
        <span className="rounded bg-primary/15 px-1.5 py-0.5 text-primary">{a.model ?? a.provider}</span>
        <span>{a.generated_at}</span>
      </div>
      <div className="whitespace-pre-wrap rounded-md border border-border/60 bg-background/40 p-3 text-[12px] leading-relaxed text-foreground/90">
        {a.raw_text}
      </div>
    </div>
  )
}

function AuctionPane({ history }: { history: AuctionSnapshot[] }) {
  if (!history?.length) return <div className="p-8 text-center text-xs text-muted-foreground">暂无集合竞价数据</div>
  return (
    <div className="p-3">
      <table className="w-full">
        <thead>
          <tr className="border-b border-border text-[10px] text-muted-foreground">
            <th className="py-1 text-left font-normal">日期</th>
            <th className="text-left font-normal">时间</th>
            <th className="text-right font-normal">指示价</th>
            <th className="text-right font-normal">匹配量</th>
            <th className="text-right font-normal">金额</th>
            <th className="text-right font-normal">口径</th>
          </tr>
        </thead>
        <tbody>
          {history.slice(0, 60).map((s, i) => (
            <tr key={i} className="border-b border-border/40 text-[11px]">
              <td className="num py-1 text-muted-foreground">{s.trade_date}</td>
              <td className="num text-muted-foreground">{s.as_of}</td>
              <td className="num text-right font-semibold">{s.price?.toFixed(2)}</td>
              <td className="num text-right">{fmtAmount(s.volume)}</td>
              <td className="num text-right">{fmtAmount(s.amount)}</td>
              <td className="text-right text-[9px] text-muted-foreground">{s.data_quality}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

interface DetailTabsProps {
  /** 轻量核心 extras（仅 AI 分析），打开详情即加载；星球消息切到对应 tab 才查 CloudBase */
  coreExtras: DetailExtrasResponse | null
  error?: string | null
  code: string
  watchlistCodes: string[]
  chipDaily?: ChipDaily | null
  chipIntraday?: ChipIntraday | null
  /** 主图当前视图：分时→当日量价分布；日K→历史筹码峰 */
  chartView?: "minute" | "daily"
}

/** 懒加载 tab：切换到对应 tab 时才请求后端对应 include 分片。
 *  auction/capital/indicators/chanlun 为日频数据，每次打开详情只拉一次；
 *  messages 盘中会更新，每次激活都刷新（服务端有 60s 读缓存，成本低）。 */
type HeavyTab = "auction" | "capital" | "indicators" | "chanlun"
type LazyTab = HeavyTab | "messages"
const LAZY_TABS: ReadonlySet<TabKey> = new Set<TabKey>(["auction", "capital", "indicators", "chanlun", "messages"])

function sliceOptions(tab: LazyTab): DetailExtrasOptions {
  return {
    includeAuctionHistory: tab === "auction",
    includeCapitalFlow: tab === "capital",
    includeFundamentals: false, // 前端无消费者，F10/财务 tab 走独立 F10 接口
    includeIndicators: tab === "indicators",
    includeChanlun: tab === "chanlun",
    includeMessages: tab === "messages",
  }
}

/** 只取该 tab 对应字段，避免分片响应里的空默认字段覆盖已加载的其他分片 */
function pickSlice(tab: LazyTab, payload: DetailExtrasResponse): Partial<DetailExtrasResponse> {
  switch (tab) {
    case "auction":
      return { auction_history: payload.auction_history }
    case "capital":
      return { capital_flow: payload.capital_flow }
    case "indicators":
      return { technical_indicators: payload.technical_indicators }
    case "chanlun":
      return { chanlun: payload.chanlun }
    case "messages":
      return { message_evidence: payload.message_evidence }
  }
}

export function DetailTabs({ coreExtras, error, code, watchlistCodes, chipDaily, chipIntraday, chartView = "minute" }: DetailTabsProps) {
  const [tab, setTab] = useState<TabKey>("chip")
  // 筹码 tab 跟随主图视图，但允许在 tab 内手动切换；主图切换时重新跟随
  const [chipModeOverride, setChipModeOverride] = useState<"intraday" | "daily" | null>(null)
  useEffect(() => setChipModeOverride(null), [chartView])
  const followMode = chartView === "daily" ? ("daily" as const) : ("intraday" as const)
  const chipMode = chipModeOverride ?? followMode

  // ---- 懒加载 tab：按 tab 缓存已加载分片，换股票时清空 ----
  // 拉取状态用 ref 跟踪，effect 只响应 tab/code/重试 变化，自身 setState 不会重复触发请求
  const watchlistKey = watchlistCodes.join(",")
  const [slices, setSlices] = useState<Partial<DetailExtrasResponse>>({})
  const fetchedRef = useRef<Set<HeavyTab>>(new Set())
  const inflightRef = useRef<LazyTab | null>(null)
  const [loadingTab, setLoadingTab] = useState<LazyTab | null>(null)
  // 失败按 tab 记录：只挡住失败的那个 tab 自动重试，不影响切去其他 tab
  const [sliceError, setSliceError] = useState<{ tab: LazyTab; message: string } | null>(null)
  const [retryNonce, setRetryNonce] = useState(0)

  useEffect(() => {
    fetchedRef.current = new Set()
    inflightRef.current = null
    setSlices({})
    setLoadingTab(null)
    setSliceError(null)
    setTab("chip")
  }, [code])

  useEffect(() => {
    if (!LAZY_TABS.has(tab)) return
    const lazyTab = tab as LazyTab
    // 日频分片拉过一次就不再拉；messages 盘中会更新，每次激活都刷新
    if (lazyTab !== "messages" && fetchedRef.current.has(lazyTab as HeavyTab)) return
    if (inflightRef.current === lazyTab || sliceError?.tab === lazyTab) return
    let cancelled = false
    inflightRef.current = lazyTab
    setLoadingTab(lazyTab)
    getDetailExtras(code, watchlistCodes, sliceOptions(lazyTab))
      .then((payload) => {
        if (inflightRef.current === lazyTab) inflightRef.current = null
        if (cancelled) return
        if (lazyTab !== "messages") fetchedRef.current.add(lazyTab as HeavyTab)
        setSlices((prev) => ({ ...prev, ...pickSlice(lazyTab, payload) }))
        setLoadingTab(null)
      })
      .catch((err) => {
        if (inflightRef.current === lazyTab) inflightRef.current = null
        if (cancelled) return
        setSliceError({ tab: lazyTab, message: err instanceof Error ? err.message : String(err) })
        setLoadingTab(null)
      })
    return () => {
      cancelled = true
      // 切走时若本 tab 的请求还在飞，释放 in-flight 锁，避免挡住后续 tab 的加载
      if (inflightRef.current === lazyTab) inflightRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, code, watchlistKey, retryNonce])

  // 核心 extras + 已加载分片的合并视图（分片只含各自字段，合并安全）
  const extras = useMemo(() => {
    if (!coreExtras && !Object.keys(slices).length) return null
    return { ...(coreExtras ?? {}), ...slices } as DetailExtrasResponse
  }, [coreExtras, slices])

  // 消息数角标来自已加载的消息分片：未访问过星球消息 tab 时不显示
  const msgCount = (slices.message_evidence?.stock?.length ?? 0) + (slices.message_evidence?.sector?.length ?? 0)
  const isLazy = LAZY_TABS.has(tab)
  const corePending = tab === "ai" && !coreExtras && !error
  // messages 已有数据时后台刷新不闪 loading；日频分片未加载时显示 loading
  const messagesFirstLoad = tab === "messages" && slices.message_evidence == null
  const slicePending =
    isLazy &&
    (loadingTab === tab
      ? tab !== "messages" || messagesFirstLoad
      : tab !== "messages" && !fetchedRef.current.has(tab as HeavyTab) && sliceError?.tab !== tab)
  return (
    <div className="terminal-panel flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 gap-1 border-b border-border px-2 pt-1.5">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={cn(
              "rounded-t px-3 py-1.5 text-[11px] font-semibold transition-colors",
              tab === t.key ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-muted",
            )}
          >
            {t.label}
            {t.key === "messages" && msgCount > 0 && (
              <span className="num ml-1 rounded bg-primary/20 px-1 text-[9px] text-primary">{msgCount}</span>
            )}
          </button>
        ))}
        {(corePending || slicePending) && <span className="ml-2 self-center text-[10px] text-muted-foreground">加载中…</span>}
        {error && tab === "ai" && (
          <span className="ml-2 self-center text-[10px] text-destructive">扩展数据加载失败：{error}</span>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        {tab === "chip" ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="flex shrink-0 items-center justify-between border-b border-border/60 px-3 py-1.5">
              <div className="inline-flex rounded-md border border-border bg-muted/40 p-0.5">
                {([
                  { key: "intraday", label: "分时分布" },
                  { key: "daily", label: "历史筹码峰" },
                ] as const).map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => setChipModeOverride(item.key === followMode ? null : item.key)}
                    className={cn(
                      "rounded px-2.5 py-0.5 text-[10px] font-semibold transition-colors",
                      chipMode === item.key ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-background/70",
                    )}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              <span className="text-[9px] text-muted-foreground">
                {chipModeOverride ? "手动查看" : "跟随主图"}
              </span>
            </div>
            <div className="min-h-0 flex-1">
              {chipMode === "daily" ? (
                <ChipDailyPane chip={chipDaily} />
              ) : (
                <ChipIntradayPane chip={chipIntraday} />
              )}
            </div>
          </div>
        ) : tab === "fundamentals" ? (
          <F10Pane code={code} />
        ) : sliceError && sliceError.tab === tab ? (
          <div className="p-8 text-center text-xs text-muted-foreground">
            <div>该模块数据加载失败：{sliceError.message}</div>
            <button
              type="button"
              className="mt-2 rounded border border-border px-3 py-1 text-[11px] text-foreground hover:bg-muted"
              onClick={() => {
                setSliceError(null)
                setRetryNonce((n) => n + 1)
              }}
            >
              重试
            </button>
          </div>
        ) : !extras || corePending || slicePending ? (
          <div className="p-8 text-center text-xs text-muted-foreground">
            {error && tab === "ai" ? "扩展数据暂不可用，可关闭后重试" : "加载扩展数据…"}
          </div>
        ) : (
          <>
            {tab === "messages" && <MessagesPane extras={extras} />}
            {tab === "ai" && (coreExtras ? <AiPane extras={coreExtras} /> : null)}
            {tab === "auction" && <AuctionPane history={extras.auction_history ?? []} />}
            {tab === "capital" && <CapitalFlowPane section={extras.capital_flow} />}
            {tab === "indicators" && <IndicatorsPane section={extras.technical_indicators} />}
            {tab === "chanlun" && <ChanlunPane section={extras.chanlun} />}
          </>
        )}
      </div>
    </div>
  )
}
