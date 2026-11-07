import { useEffect, useRef, useState } from "react"
import { X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { BrandMark } from "./brand"
import type { RoutingDecision } from "./generated/api"

// Task types are the benchmark scoreboard's (TASK_TYPES in benchmarks/report.py).
const TASKS: Record<string, string> = { code: "coding", debug: "debugging", summarize: "summary", translate: "translation", long_context: "long document", vision: "image" }
const ENGINES: Record<RoutingDecision["engine"], string> = {
  rule: "Rule: images or a very long message, then the benchmark scoreboard",
  laya: "Laya task classifier, then the benchmark scoreboard",
  llm: "LLM router",
  "follow-up": "Follow-up rule: kept the conversation's model",
  "running-turn": "Joined the task already running",
  fallback: "Fallback: routing was unavailable",
}
const SHOWN_MS = 8000

/** Plain names for the dashboard's engine and task-type counts. */
export function engineLabel(name: string): string {
  const labels: Partial<Record<string, string>> = ENGINES
  return labels[name] ?? name
}
export const taskLabel = (name: string) => name === "none" ? "Not classified" : TASKS[name] ?? name.replace(/_/g, " ")

const task = (decision: RoutingDecision) => decision.task_type ? TASKS[decision.task_type] ?? decision.task_type.replace(/_/g, " ") : null
const sure = (decision: RoutingDecision) => decision.confidence == null ? "" : ` (${Math.round(decision.confidence * 100)}% sure)`
const seconds = (value: number | null) => value == null ? "n/a" : `${value < 10 ? value.toFixed(1) : Math.round(value)} s`
const dollars = (value: number | null) => value == null ? "n/a" : `$${value < 0.01 ? value.toFixed(4) : value.toFixed(3)}`

/** The benchmark evidence as one sentence, or null when the decision has none. */
function proof(decision: RoutingDecision): string | null {
  if (decision.rank == null || decision.score == null) return null
  return `Benchmark ${decision.benchmark}: quality ${decision.score.toFixed(2)} for ${task(decision)}, ranked #${decision.rank} of ${decision.of}.`
}

/** What screen readers hear when a message is routed. */
export function routingAnnouncement(decision: RoutingDecision): string {
  const kind = task(decision)
  return [`Routed to ${decision.model}${kind ? ` for a ${kind} task${sure(decision)}` : ""}.`, decision.reason, proof(decision)].filter(Boolean).join(" ")
}

function RoutingDetails({ decision }: { decision: RoutingDecision }) {
  const kind = task(decision)
  // Alternatives keep the scoreboard's order; put the chosen model back at its own rank.
  const rows = [...decision.alternatives]
  rows.splice(Math.min((decision.rank ?? 1) - 1, rows.length), 0, decision)
  return <div className="routing-details">
    <p className="routing-details-title">Why {decision.model}</p>
    <p>{decision.reason}</p>
    <dl>
      <dt>Decided by</dt><dd>{ENGINES[decision.engine]}{decision.router_ms != null && ` in ${decision.router_ms} ms`}</dd>
      {kind && <><dt>Task type</dt><dd>{kind}{sure(decision)}</dd></>}
      {decision.benchmark && <><dt>Report</dt><dd><code>docs/benchmarks/{decision.benchmark}-lumen-benchmark.md</code></dd></>}
    </dl>
    {decision.rank != null && <Table>
      <TableCaption>Ranked for {kind} tasks by quality, speed and cost. Time is the median per answer; cost is the average per answer.</TableCaption>
      <TableHeader><TableRow><TableHead>Model</TableHead><TableHead>Quality</TableHead><TableHead>Time</TableHead><TableHead>Cost</TableHead></TableRow></TableHeader>
      <TableBody>{rows.map(row => <TableRow key={row.model} data-chosen={row === decision || undefined}>
        <TableCell>{row.model}{row === decision && " (chosen)"}</TableCell>
        <TableCell>{row.score?.toFixed(2) ?? "n/a"}</TableCell><TableCell>{seconds(row.latency)}</TableCell><TableCell>{dollars(row.cost)}</TableCell>
      </TableRow>)}</TableBody>
    </Table>}
  </div>
}

/** The answer's "via model · task" tag; it reopens the routing details later. */
export function RoutedTag({ decision }: { decision: RoutingDecision }) {
  const kind = task(decision)
  return <Popover>
    <PopoverTrigger asChild><Button type="button" variant="outline" size="xs" className="routed-tag">via {decision.model}{kind && ` · ${kind}`}</Button></PopoverTrigger>
    <PopoverContent className="routing-popover" align="start" collisionPadding={12} aria-label="Routing details"><RoutingDetails decision={decision} /></PopoverContent>
  </Popover>
}

/** The short pop-up after a routed message. It never takes focus and pauses while hovered, focused or open. */
export function RoutingNotice({ decision, onClose }: { decision: RoutingDecision; onClose: () => void }) {
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [details, setDetails] = useState(false)
  const remaining = useRef(SHOWN_MS), close = useRef(onClose)
  useEffect(() => { close.current = onClose })
  const paused = hovered || focused || details
  useEffect(() => {
    if (paused) return
    const started = Date.now(), timer = setTimeout(() => close.current(), remaining.current)
    return () => { clearTimeout(timer); remaining.current -= Date.now() - started }
  }, [paused])
  const kind = task(decision)
  return <section className="routing-notice" aria-label="Model routing" onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
    onFocus={() => setFocused(true)} onBlur={event => setFocused(event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget))}>
    <BrandMark size={16} />
    <div className="routing-notice-text">
      <p><strong>Routed to {decision.model}</strong>{kind && <> · {kind} task{sure(decision)}</>}</p>
      <p className="routing-notice-why">{proof(decision) ?? decision.reason}</p>
    </div>
    <Popover open={details} onOpenChange={setDetails}>
      <PopoverTrigger asChild><Button type="button" variant="ghost" size="xs">Details</Button></PopoverTrigger>
      <PopoverContent className="routing-popover" align="end" collisionPadding={12} aria-label="Routing details"><RoutingDetails decision={decision} /></PopoverContent>
    </Popover>
    <Button type="button" variant="ghost" size="icon-xs" aria-label="Close routing notice" onClick={onClose}><X /></Button>
  </section>
}
