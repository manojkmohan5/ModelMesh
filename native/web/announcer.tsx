import { useSyncExternalStore } from "react"

type Channel = "toast" | "conversation"
const text: Record<Channel, string> = { toast: "", conversation: "" }
const listeners = new Set<() => void>()
const emit = () => listeners.forEach(listener => listener())
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }

/** Sets the text of a live region that is already mounted and empty, so screen readers announce it. */
export function announce(channel: Channel, message: string) {
  text[channel] = ""; emit()
  // Clearing first lets the same message be announced twice in a row.
  window.setTimeout(() => { text[channel] = message; emit() }, 60)
}

/** Two persistent, initially empty status regions: one for toasts, one for the conversation. */
export function LiveRegions() {
  const toast = useSyncExternalStore(subscribe, () => text.toast)
  const conversation = useSyncExternalStore(subscribe, () => text.conversation)
  return <>
    <div className="visually-hidden" role="status" data-live-region="toast">{toast}</div>
    <div className="visually-hidden" role="status" data-live-region="conversation">{conversation}</div>
  </>
}
