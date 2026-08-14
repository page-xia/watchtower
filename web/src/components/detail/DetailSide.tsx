import type { ConfluenceSnapshot, FormulaState, RiskReward } from "@/types/api"
import { fmtPrice } from "@/lib/format"
import { ScoreMeter } from "@/components/widgets"
import { cn } from "@/lib/utils"

/** 多方/空方力度条 */
function StrengthBar({ label, value, tone }: { label: string; value: number; tone: "up" | "down" }) {
  const v = Math.max(0, Math.min(100, value))
  return (
    <div className="flex items-center gap-2">
      <span className="w-12 shrink-0 text-[10px] text-muted-foreground">{label}</span>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
        <div
          className={cn("h-full rounded-full transition-all duration-500", tone === "up" ? "bg-up/80" : "bg-down/80")}
          style={{ width: `${v}%` }}
        />
      </div>
      <span className={cn("num w-10 shrink-0 text-right text-[11px] font-bold", tone === "up" ? "text-up" : "text-down")}>
        {value.toFixed(1)}
      </span>
    </div>
  )
}

export function FormulaCard({ formula }: { formula: FormulaState | null }) {
  if (!formula) return null
  return (
    <div className="terminal-panel flex min-h-0 flex-col overflow-y-auto p-2.5">
      <div className="panel-title mb-2">做T公式状态</div>
      <div className="space-y-1.5">
        <StrengthBar label="多方力度" value={formula.duo_strength ?? 0} tone="up" />
        <StrengthBar label="空方力度" value={formula.kong_strength ?? 0} tone="down" />
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
        <div className="flex justify-between">
          <span className="text-muted-foreground">保护价</span>
          <span className="num font-semibold text-gold">{fmtPrice(formula.protection_price)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">趋势线</span>
          <span className={cn("num font-semibold", formula.near_trend_line ? "text-up" : "text-muted-foreground")}>
            {formula.near_trend_line_name} {formula.trend_distance_pct?.toFixed(1)}%
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">主力吸筹</span>
          <span className="num font-semibold">{formula.main_absorption?.toFixed(0) ?? "--"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">金色共振</span>
          <span className={cn("font-semibold", formula.gold_resonance ? "text-gold" : "text-muted-foreground")}>
            {formula.gold_resonance ? "✦ 共振中" : "无"}
          </span>
        </div>
      </div>
      {formula.trigger_note && (
        <div className="mt-1.5 border-t border-border/60 pt-1.5 text-[9px] leading-snug text-muted-foreground/70">
          {formula.trigger_note}
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
        <div className="panel-title">盘面量化共振</div>
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
        <div className="panel-title">盈亏比评估</div>
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
