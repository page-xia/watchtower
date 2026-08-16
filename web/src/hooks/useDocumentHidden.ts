import { useEffect, useState } from "react"

type HiddenListener = (hidden: boolean) => void

const listeners = new Set<HiddenListener>()
let subscribed = false

function currentHidden(): boolean {
  return typeof document !== "undefined" ? document.hidden : false
}

function emitHidden() {
  const hidden = currentHidden()
  for (const listener of listeners) listener(hidden)
}

function subscribeHidden(listener: HiddenListener): () => void {
  listeners.add(listener)
  if (!subscribed && typeof document !== "undefined") {
    document.addEventListener("visibilitychange", emitHidden)
    subscribed = true
  }
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0 && subscribed && typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", emitHidden)
      subscribed = false
    }
  }
}

export function useDocumentHidden(): boolean {
  const [hidden, setHidden] = useState(currentHidden)

  useEffect(() => subscribeHidden(setHidden), [])

  return hidden
}
