import type { TransactionFlow } from "@/types/api"
import { fmtAmount, fmtVolume } from "@/lib/format"
import { cn } from "@/lib/utils"

/** 逐笔成交流（L1 transaction tape）：买卖失衡、大单、最近成交磁带 */
export function TapePanel({ flow }: { flow: TransactionFlow | null }) {
  if (!flow || !flow.available) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 p-4 text-center">
        <div className="text-xs font-semibold text-muted-foreground">暂无逐笔成交数据</div>
        <div className="text-[10px] text-muted-foreground/70">盘中按需读取 easy_tdx L1 成交回报</div>
      </div>
    )
  }
  const total = flow.buy_amount + flow.sell_amount || 1
  const buyW = (flow.buy_amount / total) * 100
  const largeTotal = flow.large_buy_amount + flow.large_sell_amount || 1
  const largeBuyW = (flow.large_buy_amount / largeTotal) * 100
  const imb = flow.imbalance_pct ?? 0

  return (
    <div className="flex h-full min-h-0">
      {/* 左：主动买卖失衡 + 大单统计 */}
      <div className="w-[280px] shrink-0 space-y-2 overflow-y-auto border-r border-border p-3">
        {/* 主动买卖失衡 */}
        <div>
          <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
            <span>主动买入 <b className="num text-up">{fmtAmount(flow.buy_amount)}</b></span>
            <span className={cn("num font-bold", imb >= 0 ? "text-up" : "text-down")}>
              {imb >= 0 ? "+" : ""}{imb.toFixed(1)}%
            </span>
            <span>主动卖出 <b className="num text-down">{fmtAmount(flow.sell_amount)}</b></span>
          </div>
          <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-muted">
            <div className="bg-up/85" style={{ width: `${buyW}%` }} />
            <div className="bg-down/85" style={{ width: `${100 - buyW}%` }} />
          </div>
        </div>
        {/* 大单 */}
        <div>
          <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
            <span>大单买 <b className="num text-up">{flow.large_buy_count}笔</b> <span className="num">{fmtAmount(flow.large_buy_amount)}</span></span>
            <span>大单卖 <b className="num text-down">{flow.large_sell_count}笔</b> <span className="num">{fmtAmount(flow.large_sell_amount)}</span></span>
          </div>
          <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div className="bg-up/60" style={{ width: `${largeBuyW}%` }} />
            <div className="bg-down/60" style={{ width: `${100 - largeBuyW}%` }} />
          </div>
          <div className="mt-1 text-[9px] text-muted-foreground/70">
            大单阈值 {fmtAmount(flow.large_trade_threshold_amount)} · {flow.confidence}
          </div>
        </div>
        <div className="border-t border-border/60 pt-1.5 text-[9px] leading-snug text-muted-foreground/60">
          {flow.note}
        </div>
      </div>

      {/* 右：最近成交磁带 */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <table className="w-full">
          <thead className="sticky top-0 bg-card">
            <tr className="text-[9px] text-muted-foreground">
              <th className="py-1 pl-3 text-left font-normal">时间</th>
              <th className="text-right font-normal">价格</th>
              <th className="text-right font-normal">量</th>
              <th className="pr-3 text-right font-normal">方向</th>
            </tr>
          </thead>
          <tbody>
            {[...(flow.recent_trades ?? [])].reverse().map((t, i) => (
              <tr
                key={`${t.time}-${i}`}
                className={cn("border-b border-border/40 text-[11px]", t.large && "bg-[hsl(var(--gold)/0.07)]")}
              >
                <td className="num py-1 pl-3 text-muted-foreground">{t.time}</td>
                <td className={cn("num text-right font-semibold", t.side === "buy" ? "text-up" : t.side === "sell" ? "text-down" : "text-flat")}>
                  {t.price.toFixed(2)}
                </td>
                <td className="num text-right text-foreground/80">{fmtVolume(t.volume)}</td>
                <td className="pr-3 text-right">
                  <span className={cn(
                    "rounded px-1 text-[10px] font-semibold",
                    t.side === "buy" ? "bg-up-dim text-up" : t.side === "sell" ? "bg-down-dim text-down" : "bg-muted text-muted-foreground",
                  )}>
                    {t.large ? `大${t.side_label}` : t.side_label}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
