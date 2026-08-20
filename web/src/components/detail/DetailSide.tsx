import type { ConfluenceSnapshot, DailyMainFormula, FormulaState, RiskReward } from "@/types/api"
import { fmtPrice, pctClass } from "@/lib/format"
import { ScoreMeter } from "@/components/widgets"
import { cn } from "@/lib/utils"

type TrendLatest = NonNullable<DailyMainFormula["trend_latest"]>

function Metric({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("num font-semibold", className)}>{value}</span>
    </div>
  )
}

/** 万元金额简写：输入已是万元单位 */
function fmtWan(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "--"
  const abs = Math.abs(v)
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}亿`
  return `${v.toFixed(0)}万`
}

export function FormulaCard({ formula, trend }: { formula: FormulaState | null; trend?: TrendLatest | null }) {
  if (!formula) return null
  const adviceChip =
    formula.score >= 70
      ? "bg-up-dim text-up"
      : formula.score >= 50
        ? "bg-muted text-gold"
        : formula.score >= 30
          ? "bg-muted text-muted-foreground"
          : "bg-down-dim text-down"
  const trendBull = formula.trend_text.startsWith("多头")
  return (
    <div className="terminal-panel flex min-h-0 flex-col overflow-y-auto p-2.5">
      <div className="mb-1.5 flex items-center justify-between">
        <div className="panel-title">做T公式</div>
        <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-bold", adviceChip)}>
          {formula.advice || "--"}
        </span>
      </div>
      <ScoreMeter value={formula.score} className="mb-1.5" />
      {/* 核心盯盘行：趋势多空 / 资金态度 / 位置·均价（与榜单「做T分析」列同口径） */}
      <div className="mb-1.5 flex flex-wrap items-center gap-1 text-[9px] leading-tight">
        {formula.trend_text && (
          <span
            className={cn(
              "rounded px-1 py-0.5 font-bold",
              trendBull ? "bg-up-dim text-up" : "bg-down-dim text-down",
            )}
          >
            {formula.trend_text}
          </span>
        )}
        {(formula.fund_text || formula.fund_attitude) && (
          <span
            className={cn(
              "num rounded bg-muted px-1 py-0.5 font-semibold",
              pctClass(formula.fund_flow),
            )}
            title="大单分钟口径：单分钟成交额>160万按涨跌方向累计（A2/A3）"
          >
            资金 {formula.fund_text} {formula.fund_attitude}
          </span>
        )}
        {(formula.position_text || formula.vwap_relation) && (
          <span className="rounded bg-muted px-1 py-0.5 text-muted-foreground">
            {formula.position_text} {formula.vwap_relation}
          </span>
        )}
      </div>
      {/* 日线趋势值（趋势公式.md）：M5/M10/M20 + 短期（知行短期趋势线）/长期（知行多空线） */}
      {trend && (
        <div className="mb-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 rounded border border-border/60 bg-background/40 px-1.5 py-1 text-[9px]">
          {(
            [
              ["M5", trend.ma5],
              ["M10", trend.ma10],
              ["M20", trend.ma20],
              ["短期", trend.zx_trend],
              ["长期", trend.zx_duokong],
            ] as const
          ).map(([label, value]) => (
            <span key={label} className="flex items-baseline gap-0.5 whitespace-nowrap">
              <span className="text-muted-foreground">{label}</span>
              <span
                className={cn(
                  "num font-semibold",
                  value != null && formula.price > 0
                    ? value >= formula.price
                      ? "text-up"
                      : "text-down"
                    : "text-muted-foreground",
                )}
              >
                {value != null ? value.toFixed(2) : "--"}
              </span>
            </span>
          ))}
        </div>
      )}
      {/* 做T当日常量价位（现价/涨跌见详情头部，量比/换手折叠进下方量能行） */}
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
        <Metric label="阻力" value={fmtPrice(formula.resistance)} className="text-down" />
        <Metric label="支撑" value={fmtPrice(formula.support)} className="text-up" />
        <Metric label="中轴" value={fmtPrice(formula.mid)} className="text-flat" />
        <Metric label="均价" value={fmtPrice(formula.vwap)} />
        <Metric label="大单买" value={fmtWan(formula.big_buy_amount)} className="text-up" />
        <Metric label="大单卖" value={fmtWan(formula.big_sell_amount)} className="text-down" />
        <Metric label="资金流向" value={fmtWan(formula.fund_flow)} className={pctClass(formula.fund_flow)} />
        <Metric label="买卖净" value={fmtWan(formula.net_amount_wan)} className={pctClass(formula.net_amount_wan)} />
      </div>
      <div className="mt-1 text-[9px] text-muted-foreground/80">
        量能 {formula.volume_text || "--"}
        {formula.turnover_rate != null && ` · 换手 ${formula.turnover_rate.toFixed(1)}%`}
        {` · 买占比 ${formula.buy_pct.toFixed(0)}% / 卖 ${formula.sell_pct.toFixed(0)}%（内外盘口径）`}
      </div>
      {(formula.buy_signal || formula.sell_signal) && (
        <div
          className={cn(
            "mt-1.5 rounded px-1.5 py-1 text-[10px] font-bold",
            formula.buy_signal ? "bg-up-dim text-up" : "bg-down-dim text-down",
          )}
        >
          {formula.buy_signal ? "▲ 买信号：回踩支撑确认" : "▼ 卖信号：冲高阻力兑现"}
        </div>
      )}
      {formula.advice_detail && (
        <div className="mt-1 border-t border-border/60 pt-1 text-[9px] leading-snug text-muted-foreground/70">
          {formula.advice_detail}
        </div>
      )}
    </div>
  )
}

const FACTOR_LABELS: { key: keyof ConfluenceSnapshot; label: string }[] = [
  { key: "l1_transaction_flow", label: "L1成交流" },
  { key: "intraday_volume", label: "分时量能" },
  { key: "sector_attack", label: "板块进攻" },
  { key: "index_turning", label: "指数拐头" },
]

export function ConfluenceCard({ confluence }: { confluence: ConfluenceSnapshot | null }) {
  if (!confluence) return null
  return (
    <div className="terminal-panel flex min-h-0 flex-col overflow-y-auto p-2.5">
      <div className="mb-2 flex items-center justify-between">
        <div className="panel-title">量化共振</div>
        <span className={cn("num text-sm font-bold", confluence.score >= 60 ? "text-up" : confluence.score >= 40 ? "text-gold" : "text-muted-foreground")}>
          {confluence.score}
        </span>
      </div>
      <ScoreMeter value={confluence.score} className="mb-2" />
      <div className="grid grid-cols-2 gap-1">
        {FACTOR_LABELS.map(({ key, label }) => {
          const f = confluence[key]
          if (!f || typeof f !== "object" || !("label" in f)) return null
          return (
            <div key={key} className="rounded border border-border/60 bg-background/40 px-1.5 py-1">
              <div className="text-[9px] text-muted-foreground">{label}</div>
              <div className="truncate text-[10px] font-medium">{f.label || "--"}</div>
            </div>
          )
        })}
      </div>
      {(confluence.summary ?? []).length > 0 && (
        <div className="mt-1.5 space-y-0.5 border-t border-border/60 pt-1.5">
          {confluence.summary.slice(0, 3).map((s, i) => (
            <div key={i} className="text-[9px] leading-snug text-muted-foreground">· {s}</div>
          ))}
        </div>
      )}
    </div>
  )
}

export function RiskRewardCard({ rr }: { rr: RiskReward | null | undefined }) {
  if (!rr) return null
  const ok = rr.available && rr.favorable
  return (
    <div className="terminal-panel flex min-h-0 flex-col overflow-y-auto p-2.5">
      <div className="mb-2 flex items-center justify-between">
        <div className="panel-title">盈亏比</div>
        <span className={cn(
          "rounded px-1.5 py-0.5 text-[10px] font-bold",
          ok ? "bg-up-dim text-up" : "bg-muted text-muted-foreground",
        )}>
          {rr.status || "不参与"}
        </span>
      </div>
      {rr.available ? (
        <>
          <div className="flex items-baseline justify-center gap-1 py-1">
            <span className={cn("num text-2xl font-bold", ok ? "text-up" : "text-muted-foreground")}>
              {rr.reward_risk_ratio?.toFixed(1) ?? "--"}
            </span>
            <span className="text-[10px] text-muted-foreground">R（要求≥{rr.min_required_ratio?.toFixed(1)}）</span>
          </div>
          <div className="grid grid-cols-3 gap-1 text-center text-[9px]">
            <div className="rounded bg-background/60 py-1">
              <div className="text-muted-foreground">入场</div>
              <div className="num font-semibold">{fmtPrice(rr.entry_price)}</div>
            </div>
            <div className="rounded bg-background/60 py-1">
              <div className="text-muted-foreground">目标</div>
              <div className="num font-semibold text-up">{fmtPrice(rr.target_price)}</div>
            </div>
            <div className="rounded bg-background/60 py-1">
              <div className="text-muted-foreground">失效</div>
              <div className="num font-semibold text-down">{fmtPrice(rr.invalidation_price)}</div>
            </div>
          </div>
        </>
      ) : (
        <div className="py-2 text-center text-[10px] text-muted-foreground">{rr.context || "等待盘面结构"}</div>
      )}
      {(rr.risks ?? []).length > 0 && (
        <div className="mt-1.5 border-t border-border/60 pt-1.5 text-[9px] leading-snug text-gold/80">
          {rr.risks.slice(0, 2).map((r, i) => <div key={i}>⚠ {r}</div>)}
        </div>
      )}
    </div>
  )
}
