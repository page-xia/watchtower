/** 飞书 webhook 信号推送订阅：状态保存在后端。 */
import { getClientId } from "@/lib/clientIdentity"

export interface PushSubscription {
  client_id: string
  webhook_url: string
  enabled: boolean
  codes: string[]
  updated_at: string
}

function clientHeaders(extra: HeadersInit = {}): Headers {
  const headers = new Headers(extra)
  headers.set("X-Client-ID", getClientId())
  return headers
}

async function doFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, { ...init, headers: clientHeaders(init?.headers) })
  if (!resp.ok) {
    const text = await resp.text().catch(() => "")
    let detail = `${resp.status} ${resp.statusText}`
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      if (typeof parsed.detail === "string") detail = parsed.detail
    } catch {
      if (text) detail = `${detail} · ${text.slice(0, 120)}`
    }
    throw new Error(detail)
  }
  return (await resp.json()) as T
}

export function fetchPushSubscription(): Promise<PushSubscription> {
  return doFetch<PushSubscription>("/api/push/subscription")
}

export function savePushSubscription(input: { webhook_url: string; enabled: boolean; codes: string[] }): Promise<PushSubscription> {
  return doFetch<PushSubscription>(`/api/push/subscription`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  })
}

export function sendTestPush(webhookUrl: string): Promise<{ ok: boolean; detail: string }> {
  return doFetch(`/api/push/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ webhook_url: webhookUrl }),
  })
}

// 自选股变化时自动同步监听池：只在订阅已开启时回写，避免未注册用户产生脏数据。
let lastSyncedSignature = ""

export async function syncPushCodes(codes: string[]): Promise<void> {
  const signature = codes.slice().sort().join(",")
  if (signature === lastSyncedSignature) return
  try {
    const current = await fetchPushSubscription()
    if (!current.enabled || !current.webhook_url) {
      lastSyncedSignature = signature
      return
    }
    await savePushSubscription({ webhook_url: current.webhook_url, enabled: current.enabled, codes })
    lastSyncedSignature = signature
  } catch (error) {
    console.warn("同步推送监听池失败", error)
  }
}
