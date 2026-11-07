const working = ["queued", "starting", "running", "saving", "stopping", "waiting"]

/** What to announce when a conversation moves from one status to the next. Nothing for load or unrelated changes. */
export function turnAnnouncement(previous: string | undefined, next: string | undefined): string {
  if (!previous || !next || previous === next || !working.includes(previous)) return ""
  if (next === "idle" || next === "completed") return "Reply ready"
  if (next === "waiting") return "Needs your input"
  if (next === "stopped" || next === "interrupted") return "Stopped"
  if (next === "failed") return "Failed"
  return ""
}
