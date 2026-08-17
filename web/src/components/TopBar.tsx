import { useEffect, useState } from "react"
import { RefreshCw, Radio, Snowflake, Sun, Moon, Bell } from "lucide-react"
import { Button } from "@/components/ui/button"
import { StatusDot } from "@/components/widgets"
import { SearchBox } from "@/components/SearchBox"
import { PushSubscriptionDialog } from "@/components/PushSubscriptionDialog"
import { useTheme, toggleTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"

interface TopBarProps {
  connected: boolean
  /** 休市期间：连接断开属于正常休息，不显示红色“连接异常” */
  marketClosed?: boolean
  updatedAt: string
  dataMode: string
  frozen: boolean
  decisionStage?: string
  onRefresh: () => void
  refreshing: boolean
  onOpenDetail: (code: string) => void
  watchlistCodes?: string[]
}

const DATA_MODE_LABEL: Record<string, string> = {
  live: "实时行情",
  replay: "回放",
  local_trajectory: "本地轨迹",
  close_snapshot: "收盘快照",
}

export function TopBar({ connected, marketClosed, updatedAt, dataMode, frozen, decisionStage, onRefresh, refreshing, onOpenDetail, watchlistCodes = [] }: TopBarProps) {
  const resting = !connected && !!marketClosed
  const [clock, setClock] = useState("")
  const [pushDialogOpen, setPushDialogOpen] = useState(false)
  const theme = useTheme()
  useEffect(() => {
    const tick = () => {
      const d = new Date()
      setClock(
        `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`,
      )
    }
    tick()
    const t = window.setInterval(tick, 1000)
    return () => window.clearInterval(t)
  }, [])

  return (
    <header className="flex h-11 shrink-0 items-center gap-3 border-b border-border bg-card/60 px-3 backdrop-blur">
      <div className="flex items-center gap-2">
        <Radio className="h-4 w-4 text-primary" />
        <h1 className="text-sm font-bold tracking-wide">日内盯盘终端</h1>
        <span className="rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
          easy_tdx · 星球
        </span>
      </div>

      <div className="flex items-center gap-2 text-[11px]">
        <StatusDot ok={connected} warning={resting} />
        <span className={cn(connected ? "text-muted-foreground" : resting ? "text-gold" : "text-destructive")}>
          {connected ? "已连接" : resting ? "休息中" : "连接异常"}
        </span>
      </div>

      <div className="mx-1 h-4 w-px bg-border" />

      <div className="num text-[13px] font-semibold tracking-wider text-foreground/90">{clock}</div>
      <span className="text-[11px] text-muted-foreground">行情 {updatedAt || "--"}</span>

      <div className="flex items-center gap-1.5">
        <span className="rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
          {DATA_MODE_LABEL[dataMode] ?? dataMode ?? "--"}
        </span>
        {decisionStage && (
          <span className="rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
            {decisionStage}
          </span>
        )}
        {frozen && (
          <span className="flex items-center gap-1 rounded border border-[hsl(var(--cyan)/0.4)] bg-[hsl(var(--cyan)/0.1)] px-1.5 py-0.5 text-[10px] text-[hsl(var(--cyan))]">
            <Snowflake className="h-3 w-3" /> 数据冻结
          </span>
        )}
      </div>

      <div className="flex-1" />

      <SearchBox onOpenDetail={onOpenDetail} watchlistCodes={watchlistCodes} />

      <Button variant="outline" size="sm" className="h-7 gap-1 text-xs" onClick={onRefresh} disabled={refreshing}>
        <RefreshCw className={cn("h-3 w-3", refreshing && "animate-spin")} />
        刷新
      </Button>

      <Button
        variant="outline"
        size="sm"
        className="h-7 w-7 px-0"
        onClick={() => setPushDialogOpen(true)}
        title="飞书信号推送订阅"
      >
        <Bell className="h-3.5 w-3.5" />
      </Button>

      <Button
        variant="outline"
        size="sm"
        className="h-7 w-7 px-0"
        onClick={toggleTheme}
        title={theme === "dark" ? "切换到白天模式" : "切换到夜晚模式"}
      >
        {theme === "dark" ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
      </Button>

      <PushSubscriptionDialog open={pushDialogOpen} onOpenChange={setPushDialogOpen} watchlistCodes={watchlistCodes} />
    </header>
  )
}
