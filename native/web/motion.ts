export type Motion = "full" | "reduced"

const storageKey = "modelmesh-motion"
const changeEvent = "modelmesh:motion-change"
const system = "(prefers-reduced-motion: reduce)"

function savedMotion(): Motion {
  try { return localStorage.getItem(storageKey) === "reduced" ? "reduced" : "full" }
  catch { return "full" }
}

export function currentMotion(): Motion {
  return document.documentElement.dataset.motion === "reduced" ? "reduced" : "full"
}

function applyMotion(motion: Motion) {
  if (motion === "reduced") document.documentElement.dataset.motion = "reduced"
  else delete document.documentElement.dataset.motion
}

export function initializeMotion() {
  applyMotion(savedMotion())
}

export function setMotion(motion: Motion) {
  applyMotion(motion)
  try { localStorage.setItem(storageKey, motion) } catch { /* Keep the choice for this page when storage is unavailable. */ }
  window.dispatchEvent(new Event(changeEvent))
}

export function subscribeMotion(onChange: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key !== storageKey && event.key !== null) return
    try { if (event.storageArea !== localStorage) return } catch { return }
    applyMotion(event.newValue === "reduced" ? "reduced" : "full")
    onChange()
  }
  window.addEventListener(changeEvent, onChange)
  window.addEventListener("storage", onStorage)
  return () => {
    window.removeEventListener(changeEvent, onChange)
    window.removeEventListener("storage", onStorage)
  }
}

/** Like matchMedia("(prefers-reduced-motion: reduce)"), but the in-app Reduce motion setting also counts. */
export function reducedMotionQuery() {
  const media = window.matchMedia(system)
  return {
    get matches() { return media.matches || currentMotion() === "reduced" },
    addEventListener(_type: "change", listener: () => void) { media.addEventListener("change", listener); window.addEventListener(changeEvent, listener) },
    removeEventListener(_type: "change", listener: () => void) { media.removeEventListener("change", listener); window.removeEventListener(changeEvent, listener) },
  }
}
