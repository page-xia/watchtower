/** 飞书 webhook 信号推送订阅：client_id 本地持久化，订阅状态保存在后端。 */

const CLIENT_ID_KEY = "watchtower.client-id.v1"

export interface PushSubscription {
  client_id: string
  webhook_url: string
  enabled: boolean
  codes: string[]
  updated_at: string
}

export function getClientId(): string {
  try {
    const existing = window.localStorage.getItem(CLIENT_ID_KEY)
    if (existing && /^[A-Za-z0-9_-]{8,64}$/.test(existing)) return existing
    const id =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `client-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
    window.localStorage.setItem(CLIENT_ID_KEY, id)
    return id
  } catch {
    return `client-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  }
}

async function doFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init)
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
  return doFetch<PushSubscription>(`/api/push/subscription?client_id=${encodeURIComponent(getClientId())}`)
}

export function savePushSubscription(input: { webhook_url: string; enabled: boolean; codes: string[] }): Promise<PushSubscription> {
  return doFetch<PushSubscription>(`/api/push/subscription`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: getClientId(), ...input }),
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
