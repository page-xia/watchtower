import { useEffect, useState } from "react"
import { Bell, Loader2, Send } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import {
  fetchPushSubscription,
  savePushSubscription,
  sendTestPush,
} from "@/lib/pushSubscription"

interface PushSubscriptionDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  watchlistCodes: string[]
}

export function PushSubscriptionDialog({ open, onOpenChange, watchlistCodes }: PushSubscriptionDialogProps) {
  const [webhookUrl, setWebhookUrl] = useState("")
  const [enabled, setEnabled] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [status, setStatus] = useState<{ kind: "ok" | "err"; text: string } | null>(null)

  useEffect(() => {
    if (!open) return
    setStatus(null)
    setLoading(true)
    fetchPushSubscription()
      .then((sub) => {
        setWebhookUrl(sub.webhook_url)
        setEnabled(sub.enabled)
      })
      .catch(() => setStatus({ kind: "err", text: "读取订阅状态失败" }))
      .finally(() => setLoading(false))
  }, [open])

  const handleSave = () => {
    setSaving(true)
    setStatus(null)
    savePushSubscription({ webhook_url: webhookUrl.trim(), enabled, codes: watchlistCodes })
      .then(() => setStatus({ kind: "ok", text: `已保存，监听 ${watchlistCodes.length} 只自选股` }))
      .catch((error: Error) => setStatus({ kind: "err", text: error.message }))
      .finally(() => setSaving(false))
  }

  const handleTest = () => {
    setTesting(true)
    setStatus(null)
    sendTestPush(webhookUrl.trim())
      .then(() => setStatus({ kind: "ok", text: "测试卡片已推送，请查看飞书群" }))
      .catch((error: Error) => setStatus({ kind: "err", text: error.message }))
      .finally(() => setTesting(false))
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-sm">
            <Bell className="h-4 w-4 text-primary" />
            飞书信号推送
          </DialogTitle>
          <DialogDescription className="text-xs">
            自选股出现买T/卖T信号时，以卡片消息推送到飞书群机器人；同一只票 5 分钟内只推一次。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="feishu-webhook" className="text-xs text-muted-foreground">
              Webhook 地址
            </Label>
            <Input
              id="feishu-webhook"
              placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..."
              value={webhookUrl}
              onChange={(event) => setWebhookUrl(event.target.value)}
              disabled={loading}
              className="h-8 font-mono text-xs"
            />
          </div>

          <div className="flex items-center justify-between rounded border border-border bg-muted/50 px-3 py-2">
            <div className="flex flex-col">
              <span className="text-xs font-medium">启动推送</span>
              <span className="text-[10px] text-muted-foreground">
                监听当前 {watchlistCodes.length} 只自选股的买卖点信号
              </span>
            </div>
            <Switch checked={enabled} onCheckedChange={setEnabled} disabled={loading} />
          </div>

          {status && (
            <div
              className={
                status.kind === "ok"
                  ? "rounded border border-up/40 bg-up-dim px-3 py-1.5 text-[11px] text-up"
                  : "rounded border border-destructive/50 bg-destructive/15 px-3 py-1.5 text-[11px] text-destructive"
              }
            >
              {status.text}
            </div>
          )}
        </div>

        <DialogFooter className="gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1 text-xs"
            onClick={handleTest}
            disabled={testing || saving || loading || !webhookUrl.trim()}
          >
            {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
            发送测试
          </Button>
          <Button
            size="sm"
            className="h-7 gap-1 text-xs"
            onClick={handleSave}
            disabled={saving || loading || (enabled && !webhookUrl.trim())}
          >
            {saving && <Loader2 className="h-3 w-3 animate-spin" />}
            保存订阅
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
