import { useState } from "react"
import type { AuctionSnapshot, DataTable, DetailExtrasResponse, ExtrasSection, MessageEvidence } from "@/types/api"
import { dateShort, fmtAmount, timeShort } from "@/lib/format"
import { cn } from "@/lib/utils"

type TabKey = "messages" | "ai" | "auction" | "capital" | "fundamentals" | "chanlun"

const TABS: { key: TabKey; label: string }[] = [
  { key: "messages", label: "星球消息" },
  { key: "ai", label: "AI分析" },
  { key: "auction", label: "集合竞价" },
  { key: "capital", label: "资金流" },
  { key: "fundamentals", label: "F10/财务" },
  { key: "chanlun", label: "缠论" },
]

function MessageCard({ msg }: { msg: MessageEvidence }) {
  const [expanded, setExpanded] = useState(false)
  const bullish = msg.direction === "1"
  return (
    <div className="rounded-md border border-border/60 bg-background/40 p-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[12px] font-semibold leading-snug">{msg.topic_title || msg.event_title}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[9px] text-muted-foreground">
            <span>{msg.owner_name}</span>
            <span>{dateShort(msg.create_time)} {timeShort(msg.create_time)}</span>
            <span className={cn("rounded px-1", bullish ? "bg-up-dim text-up" : "bg-muted text-muted-foreground")}>
              {msg.event_type} · {msg.role}
            </span>
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
        {msg.topic_content || msg.event_summary}
      </div>
      {(msg.keywords ?? []).length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {msg.keywords.slice(0, 8).map((k) => (
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
  if (stock.length + sector.length === 0) {
    return <div className="p-8 text-center text-xs text-muted-foreground">暂无星球消息关联该股/板块</div>
  }
  return (
    <div className="grid grid-cols-1 gap-2 p-3 xl:grid-cols-2">
      <div className="space-y-2">
        <div className="panel-title">直接提及个股（{stock.length}）</div>
        {stock.map((m) => <MessageCard key={m.event_id} msg={m} />)}
      </div>
      <div className="space-y-2">
        <div className="panel-title">板块相关（{sector.length}）</div>
        {sector.map((m, i) => <MessageCard key={m.event_id ?? i} msg={m} />)}
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

function GenericSection({ section, emptyText }: { section: ExtrasSection | null | undefined; emptyText: string }) {
  if (!section || !section.available) {
    return (
      <div className="p-8 text-center text-xs text-muted-foreground">
        {section?.note || emptyText}
      </div>
    )
  }
  const summary = section.summary ?? {}
  const tables = section.tables ?? []
  return (
    <div className="space-y-3 p-3">
      {Object.keys(summary).length > 0 && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(summary).map(([k, v]) => (
            <div key={k} className="rounded border border-border/60 bg-background/40 px-2 py-1">
              <div className="text-[9px] text-muted-foreground">{k}</div>
              <div className="num text-[11px] font-semibold">
                {typeof v === "number" ? fmtAmount(v) : String(v ?? "--")}
              </div>
            </div>
          ))}
        </div>
      )}
      {tables.map((t: DataTable) => (
        <div key={t.title}>
          <div className="panel-title mb-1">{t.title}</div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border text-[9px] text-muted-foreground">
                  {t.columns.map((c) => <th key={c} className="py-1 pr-2 text-right font-normal first:text-left">{c}</th>)}
                </tr>
              </thead>
              <tbody>
                {(t.rows ?? []).slice(0, 30).map((row, i) => (
                  <tr key={i} className="border-b border-border/40 text-[10px]">
                    {t.columns.map((c) => {
                      const v = row[c]
                      return (
                        <td key={c} className="num py-1 pr-2 text-right first:text-left">
                          {typeof v === "number" ? (Math.abs(v) >= 1e6 ? fmtAmount(v) : v.toFixed(2)) : String(v ?? "--")}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
      {tables.length === 0 && Object.keys(summary).length === 0 && (
        <div className="p-4 text-center text-xs text-muted-foreground">{section.note || emptyText}</div>
      )}
    </div>
  )
}

export function DetailTabs({ extras, error }: { extras: DetailExtrasResponse | null; error?: string | null }) {
  const [tab, setTab] = useState<TabKey>("messages")
  const msgCount = (extras?.message_evidence?.stock?.length ?? 0) + (extras?.message_evidence?.sector?.length ?? 0)
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
        {!extras && !error && <span className="ml-2 self-center text-[10px] text-muted-foreground">加载中…</span>}
        {error && <span className="ml-2 self-center text-[10px] text-destructive">扩展数据加载失败：{error}</span>}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {!extras ? (
          <div className="p-8 text-center text-xs text-muted-foreground">
            {error ? "扩展数据暂不可用，可关闭后重试" : "加载扩展数据…"}
          </div>
        ) : (
          <>
            {tab === "messages" && <MessagesPane extras={extras} />}
            {tab === "ai" && <AiPane extras={extras} />}
            {tab === "auction" && <AuctionPane history={extras.auction_history ?? []} />}
            {tab === "capital" && <GenericSection section={extras.capital_flow} emptyText="暂无资金流数据" />}
            {tab === "fundamentals" && <GenericSection section={extras.fundamentals} emptyText="暂无 F10/财务数据" />}
            {tab === "chanlun" && <GenericSection section={extras.chanlun} emptyText="暂无缠论数据" />}
          </>
        )}
      </div>
    </div>
  )
}
