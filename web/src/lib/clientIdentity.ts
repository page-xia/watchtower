/** Browser-scoped anonymous principal, ready to migrate to formal login later. */
export const CLIENT_ID_KEY = "watchtower.client-id.v1"

export interface ClientStorageWindow {
  localStorage: Pick<Storage, "getItem" | "setItem">
}

const CLIENT_ID_PATTERN = /^[A-Za-z0-9_-]{8,64}$/

function newClientId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
}

export function clientIdFromStorage(target: ClientStorageWindow): string {
  const existing = target.localStorage.getItem(CLIENT_ID_KEY)?.trim()
  if (existing && CLIENT_ID_PATTERN.test(existing)) return existing
  const id = newClientId()
  target.localStorage.setItem(CLIENT_ID_KEY, id)
  return id
}

/** Returns one stable identifier per browser profile whenever storage is available. */
export function getClientId(): string {
  if (runtimeFallback) return runtimeFallback
  if (typeof window !== "undefined") {
    try {
      return clientIdFromStorage(window)
    } catch {
      // Browsers can deny storage in private/embedded contexts.  Keep the
      // request valid, though such a profile cannot persist anonymous state.
    }
  }
  runtimeFallback = newClientId()
  return runtimeFallback
}

let runtimeFallback: string | null = null
