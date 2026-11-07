// Smart routing ("Auto") is a per-browser choice: a conversation only records the model its last
// message actually ran on, so the choice is remembered here instead. Auto is preselected until the
// user picks a fixed model: a row in a long list scrolls out of view on a short window.
const storageKey = "modelmesh:auto-model"

export function wantsAuto(): boolean {
  // Blocked storage cannot keep a fixed choice, so stay on the conversation's own model.
  try { return localStorage.getItem(storageKey) !== "0" } catch { return false }
}

export function rememberAuto(on: boolean): void {
  try { localStorage.setItem(storageKey, on ? "1" : "0") } catch { /* storage blocked: the choice lasts until reload */ }
}

/** Auto when the server offers it and the user has not picked a fixed model; otherwise the fallback. */
export function startingModel(fallback: string, auto: string | null | undefined): string {
  return auto && wantsAuto() ? auto : fallback
}

/** Model picker rows, with the Auto row first when the server offers smart routing. */
export function modelOptions(models: string[], auto: string | null | undefined, name: (model: string) => string = model => model) {
  return [...(auto ? [{ value: auto, label: "Auto (smart routing)" }] : []), ...models.map(model => ({ value: model, label: name(model) }))]
}
