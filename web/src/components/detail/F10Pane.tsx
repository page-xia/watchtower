import { useEffect, useMemo, useState } from "react"
import { getDetailF10 } from "@/lib/api"
import type { F10Category, F10Response, F10Section } from "@/types/api"
import { cn } from "@/lib/utils"

/**
 * 聚合 F10 面板：左侧分类导航 + 右侧板块内容。
 * 数据由后端按「分类 → 板块 → 字段/表格」输出，此处纯展示。
 */

function isLongText(value: unknown): boolean {
  return typeof value === "string" && value.length > 48
}

function cellText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "--"
  return String(value)
}

function looksNumeric(text: string): boolean {
  return /^[-+]?\d[\d,]*(\.\d+)?(%|亿|万|元股?|万股|亿股|次|元)?$/.test(text.replace(/\s/g, "")) || text === "--"
}

function SectionBlock({ section }: { section: F10Section }) {
  const shortFields = section.fields.filter((f) => !isLongText(f.value))
  const longFields = section.fields.filter((f) => isLongText(f.value))
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="panel-title">{section.title}</span>
        <span className="h-px flex-1 bg-border/60" />
      </div>
      {shortFields.length > 0 && (
        <div className="grid grid-cols-2 gap-1.5 xl:grid-cols-3">
          {shortFields.map((f) => (
            <div key={f.raw_key || f.label} className="rounded border border-border/60 bg-background/40 px-2 py-1">
              <div className="text-[9px] leading-tight text-muted-foreground">{f.label}</div>
              <div className={cn("mt-0.5 truncate text-[11px] font-semibold", looksNumeric(cellText(f.value)) && "num")} title={cellText(f.value)}>
                {cellText(f.value)}
              </div>
            </div>
          ))}
        </div>
      )}
      {longFields.map((f) => (
        <div key={f.raw_key || f.label} className="rounded border border-border/60 bg-background/40 px-2.5 py-2">
          <div className="text-[9px] text-muted-foreground">{f.label}</div>
          <div className="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed text-foreground/85">{cellText(f.value)}</div>
        </div>
      ))}
      {section.tables.map((t) => (
        <div key={t.title}>
          <div className="overflow-x-auto rounded border border-border/60">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-[9px] text-muted-foreground">
                  {t.columns.map((c) => (
                    <th key={c} className="whitespace-nowrap px-2 py-1.5 text-right font-normal first:text-left">
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(t.rows ?? []).map((row, i) => (
                  <tr key={i} className="border-b border-border/40 text-[10.5px] last:border-0 hover:bg-accent/40">
                    {t.columns.map((c) => {
                      const text = cellText(row[c])
                      return (
                        <td
                          key={c}
                          className={cn(
                            "max-w-[220px] px-2 py-1.5 text-right first:max-w-none first:whitespace-nowrap first:text-left first:font-medium first:text-foreground/80",
                            looksNumeric(text) ? "num whitespace-nowrap" : "text-foreground/85",
                          )}
                          title={text}
                        >
                          {text}
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
      {section.fields.length === 0 && section.tables.length === 0 && (
        <div className="py-2 text-center text-[10px] text-muted-foreground">该板块暂无数据</div>
      )}
    </div>
  )
}

function CategoryContent({ category }: { category: F10Category }) {
  if (category.error) {
    return (
      <div className="rounded border border-destructive/40 bg-destructive/10 p-3 text-[11px] text-destructive">
        该分类加载失败：{category.error}
      </div>
    )
  }
  if (!category.available || category.sections.length === 0) {
    return <div className="p-8 text-center text-xs text-muted-foreground">「{category.title}」暂无数据</div>
  }
  return (
    <div className="space-y-4 p-3">
      {category.sections.map((section) => (
        <SectionBlock key={section.key} section={section} />
      ))}
    </div>
  )
}

export function F10Pane({ code }: { code: string }) {
  const [data, setData] = useState<F10Response | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    setActiveKey(null)
    void getDetailF10(code)
      .then((payload) => {
        if (cancelled) return
        setData(payload)
        const first = payload.categories.find((c) => c.available) ?? payload.categories[0]
        setActiveKey(first?.key ?? null)
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message || "F10 数据加载失败")
      })
    return () => {
      cancelled = true
    }
  }, [code])

  const handleRefresh = () => {
    if (refreshing) return
    setRefreshing(true)
    void getDetailF10(code, true)
      .then((payload) => {
        setData(payload)
        setError(null)
      })
      .catch((exc: Error) => setError(exc.message || "F10 刷新失败"))
      .finally(() => setRefreshing(false))
  }

  const categories = useMemo(() => data?.categories ?? [], [data])
  const active = categories.find((c) => c.key === activeKey) ?? null

  if (error && !data) {
    return <div className="p-8 text-center text-xs text-destructive">F10 数据加载失败:{error}</div>
  }
  if (!data) {
    return (
      <div className="flex h-full items-center justify-center gap-2 p-8 text-xs text-muted-foreground">
        <span className="inline-block h-3 w-3 animate-spin rounded-full border border-primary border-t-transparent" />
        正在聚合 tushare + easy_tdx F10 数据…
      </div>
    )
  }
  if (!data.available) {
    return <div className="p-8 text-center text-xs text-muted-foreground">{data.note || "暂无 F10 数据"}</div>
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 头部：来源与元信息 */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2">
        <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[9px] font-semibold text-primary">tushare_pro</span>
        <span className="rounded bg-gold/15 px-1.5 py-0.5 text-[9px] font-semibold text-gold">easy_tdx</span>
        <span className="text-[10px] text-muted-foreground">
          分类 <span className="num text-foreground">{data.category_count}</span>/<span className="num">{data.expected_category_count}</span>
        </span>
        <span className="num text-[10px] text-muted-foreground">更新 {data.fetched_at}</span>
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing}
          title="强制实时拉取 tushare + easy_tdx 最新 F10 并更新缓存"
          className="ml-auto flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
        >
          <span className={cn("inline-block", refreshing && "animate-spin")}>⟳</span>
          {refreshing ? "刷新中…" : "刷新"}
        </button>
        <span className="w-full text-[9px] text-muted-foreground/70">{data.note}</span>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* 左：分类导航 */}
        <nav className="w-[128px] shrink-0 overflow-y-auto border-r border-border py-1.5">
          {categories.map((c) => {
            const activeCls = c.key === activeKey
            return (
              <button
                key={c.key}
                type="button"
                onClick={() => setActiveKey(c.key)}
                className={cn(
                  "flex w-full items-center justify-between gap-1 px-2.5 py-1.5 text-left text-[11px] transition-colors",
                  activeCls
                    ? "border-r-2 border-primary bg-accent font-semibold text-foreground"
                    : c.available
                      ? "text-muted-foreground hover:bg-muted hover:text-foreground"
                      : "text-muted-foreground/50",
                )}
              >
                <span className="truncate">{c.title}</span>
                {c.error ? (
                  <span className="text-[9px] text-destructive">!</span>
                ) : (
                  c.available && (
                    <span className={cn("num rounded px-1 text-[9px]", activeCls ? "bg-primary/20 text-primary" : "bg-muted text-muted-foreground")}>
                      {c.sections.length}
                    </span>
                  )
                )}
              </button>
            )
          })}
        </nav>

        {/* 右：板块内容 */}
        <div className="min-w-0 flex-1 overflow-y-auto">
          {active ? <CategoryContent category={active} /> : <div className="p-8 text-center text-xs text-muted-foreground">请选择左侧分类</div>}
        </div>
      </div>
    </div>
  )
}
