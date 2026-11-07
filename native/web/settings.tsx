import React, { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from "react"
import { ArrowLeft, ArrowRight, BookOpen, CalendarClock, ChevronRight, Gauge, LayoutGrid, Menu, Pencil, Plus, RefreshCw, Route, Search, Shield, SlidersHorizontal, FileText, Trash2, Users, Wallet, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Select, SelectContent, SelectItem, SelectValue } from "@/components/ui/select"
import { NamedSelectTrigger } from "./named-select"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ApiError, csrfToken } from "./api"
import { listSessions } from "./generated/api"
import { AnalyticsPage } from "./analytics"
import { AnalyticsMetrics } from "./analytics-shared"
import { getSettingsRouting, type RoutingCount } from "./generated/api"
import { engineLabel, taskLabel } from "./routing-notice"
import { listSettingsUsers, type SettingsAccount } from "./generated/api"
import { getMe } from "./generated/api"
import { Brand, SidebarToggle } from "./brand"
import { AppearanceToggle } from "./appearance-toggle"
import { Switch } from "@/components/ui/switch"
import { currentMotion, setMotion, subscribeMotion } from "./motion"
import type { GeneralPreferences } from "./general-preferences"
import {
  createSettingsAutomation, createSettingsSkill, createSettingsUser, deleteSettingsAutomation, deleteSettingsMemory,
  deleteSettingsSkill, deleteSettingsUser, getSettingsGeneral, getSettingsOverview, listSettingsAutomationLogs,
  listSettingsAutomations, listSettingsMemory, listSettingsSkills, updateSettingsAutomation, updateSettingsGeneral,
  updateSettingsSkill, updateSettingsUser, writeSettingsMemory,
  type SettingsAutomation, type SettingsMemoryFile, type SettingsSkill, type UserEditWritable,
} from "./generated/api"
import "./settings.css"

type SettingsUser = { id: string; name: string; role: string }
type SettingsPageProps = { user: SettingsUser; page: string; onNavigate: (page: string) => void; onBack: () => void; sidebarCollapsed: boolean; onToggleSidebar: () => void; onPreferences: (preferences: GeneralPreferences) => void }
type Capabilities = { read?: boolean; create?: boolean; update?: boolean; delete?: boolean; roles?: boolean; preferences?: boolean; review?: boolean; reason?: string; runtime_available?: boolean; shared?: boolean; run?: boolean }
type Collection = { capabilities?: Capabilities; reason?: string }
type Account = Pick<SettingsAccount, "id" | "username" | "display_name" | "role" | "enabled">
const loadUsers = async (signal: AbortSignal) => (await listSettingsUsers({ signal })).data
const loadSessions = async (signal: AbortSignal) => (await listSessions({ signal })).data
type Option = { value: string; label: string }
type Resource<T> = { data: T | undefined; loading: boolean; error: string; refresh: () => void }
type FormValues = Record<string, string | number | boolean>
type Field = { name: string; label: string; type?: "text" | "password" | "textarea" | "select" | "checkbox" | "number" | "url"; required?: boolean; hint?: string; options?: Option[]; disabled?: boolean; min?: number; max?: number; minLength?: number; maxLength?: number; pattern?: string }

const destinations = [
  { id: "general", title: "General", description: "Set your chat preferences.", group: "Preferences", Icon: SlidersHorizontal },
  { id: "skills", title: "Skills", description: "Choose reusable instructions for your chats.", group: "Workflows", Icon: FileText },
  { id: "automations", title: "Automations", description: "Schedule recurring work and review past runs.", group: "Workflows", Icon: CalendarClock },
  { id: "memory", title: "Memory", description: "Manage what ModelMesh remembers about your work.", group: "Workflows", Icon: BookOpen },
  { id: "users", title: "Users", description: "Manage workspace members, roles, and access.", group: "Administration", Icon: Users, admin: true },
  { id: "performance", title: "Performance", description: "Review recorded turn timings across the workspace.", group: "Administration", Icon: Gauge, admin: true },
  { id: "usage", title: "Spend & usage", description: "Review recorded model usage and cost coverage.", group: "Administration", Icon: Wallet, admin: true },
  { id: "routing", title: "Routing", description: "See how Auto routing chose models and how each model is holding up.", group: "Administration", Icon: Route, admin: true },
]
const groups = ["Preferences", "Workflows", "Administration"]
const scopeOptions = [{ value: "all", label: "All scopes" }, { value: "personal", label: "Personal" }, { value: "shared", label: "Shared" }, { value: "bundled", label: "Bundled" }]
const scopes = (user: SettingsUser, capabilities?: Capabilities) => scopeOptions.filter(option => option.value === "personal" || (user.role === "admin" && capabilities?.shared === true && option.value === "shared"))
const contains = (query: string, ...values: (string | undefined)[]) => values.join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
const count = (value: number | null | undefined) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "Unavailable"
const dateTime = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Not recorded"
const errorText = (error: unknown) => error instanceof ApiError && error.status === 404
  ? "This settings endpoint is not available on this server."
  : error instanceof Error ? error.message : "The request could not be completed."

const scopeInput = (value: FormValues[string]) => value === "shared" ? "shared" as const : "personal" as const

/** `path` keys the read (and its cache); `load` performs it with a generated SDK call. */
function useResource<T>(path: string | null, load: (signal: AbortSignal) => Promise<T>): Resource<T> {
  const loader = useRef(load)
  loader.current = load
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<{ path: string | null; data?: T; loading: boolean; error: string }>({ path, loading: Boolean(path), error: "" })
  useEffect(() => {
    if (!path) { setState({ path, loading: false, error: "" }); return }
    const controller = new AbortController()
    setState(previous => ({ path, data: previous.path === path ? previous.data : undefined, loading: true, error: "" }))
    void loader.current(controller.signal).then(
      data => { if (!controller.signal.aborted) setState({ path, data, loading: false, error: "" }) },
      error => { if (!controller.signal.aborted) setState({ path, loading: false, error: errorText(error) }) },
    )
    return () => controller.abort()
  }, [path, revision])
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  return { data: state.path === path ? state.data : undefined, loading: state.path !== path || state.loading, error: state.path === path ? state.error : "", refresh }
}

function useMutation() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [errorField, setErrorField] = useState("")
  const alive = useRef(true)
  const pending = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const run = async (action: () => Promise<unknown>, done: () => void) => {
    if (pending.current) return
    const requestAuth = csrfToken()
    pending.current = true; setBusy(true); setError(""); setErrorField("")
    try { await action(); if (alive.current && csrfToken() === requestAuth) done() }
    catch (error) {
      if (alive.current && csrfToken() === requestAuth) {
        setError(errorText(error)); setErrorField(error instanceof ApiError ? error.field ?? "" : "")
        if (error instanceof ApiError && error.status === 403) window.dispatchEvent(new Event("modelmesh:access-changed"))
      }
    }
    finally { pending.current = false; if (alive.current && csrfToken() === requestAuth) setBusy(false) }
  }
  return { busy, error, errorField, run }
}

function Choice({ label, value, onChange, options, disabled, id }: { label: string; value: string; onChange: (value: string) => void; options: Option[]; disabled?: boolean; id?: string }) {
  return <Select value={value} onValueChange={onChange} disabled={disabled}>
    <NamedSelectTrigger id={id} label={label} title={options.find(option => option.value === value)?.label}><SelectValue placeholder={label} /></NamedSelectTrigger>
    <SelectContent className="native-settings-select">{options.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
  </Select>
}

function LoadState<T>({ resource, children }: { resource: Resource<T>; children: (data: T) => React.ReactNode }) {
  if (resource.error) return <div className="ns-empty" role="alert"><h2>Unable to load this page</h2><p>{resource.error}</p><Button variant="outline" onClick={resource.refresh}><RefreshCw />Try again</Button></div>
  if (!resource.data) return <p className="ns-muted" role="status">Loading settings…</p>
  return <div aria-busy={resource.loading}>{children(resource.data)}</div>
}

function Toolbar({ search, setSearch, count: total, resource, filter, action }: {
  search: string; setSearch: (value: string) => void; count?: number; resource: { loading: boolean; refresh: () => void }
  filter?: React.ReactNode; action?: React.ReactNode
}) {
  return <div className="ns-toolbar">
    <div className="ns-search"><Search size={16} aria-hidden="true" /><Input type="search" aria-label="Search this list" placeholder="Search…" value={search} onChange={event => setSearch(event.target.value)} />
      {search && <Button type="button" variant="ghost" size="icon-sm" aria-label="Clear search" onClick={() => setSearch("")}><X /></Button>}</div>
    {filter}{total !== undefined && <span className="ns-count">{count(total)}</span>}
    <Button type="button" variant="outline" disabled={resource.loading} onClick={resource.refresh} aria-label="Refresh settings"><RefreshCw size={16} /><span>Refresh</span></Button>
    {action}
  </div>
}

function Empty({ filtered, noun, onClear }: { filtered: boolean; noun: string; onClear: () => void }) {
  return <div className="ns-empty"><h2>{filtered ? `No matching ${noun}` : `No ${noun} yet`}</h2>
    {filtered && <p>Try another search or clear the filters.</p>}
    {filtered && <Button variant="outline" onClick={onClear}>Clear filters</Button>}
  </div>
}

function CapabilityNote({ capabilities, reason }: Collection) {
  return (reason || capabilities?.reason) ? <p className="ns-notice" role="status">{reason || capabilities?.reason}</p>
    : !capabilities ? <p className="ns-notice">Editing is not enabled by this server.</p> : null
}

function RecordTable({ label, headings, children }: { label: string; headings: string[]; children: React.ReactNode }) {
  return <div className="ns-table" role="region" aria-label={label} tabIndex={0}><Table>
    <TableHeader><TableRow>{headings.map(heading => <TableHead key={heading} scope="col">{heading}</TableHead>)}</TableRow></TableHeader>
    <TableBody>{children}</TableBody>
  </Table></div>
}

function Editor({ title, description, fields, initial, onSave, onClose, submitLabel = "Save" }: {
  title: string; description: string; fields: Field[] | ((values: FormValues) => Field[]); initial: FormValues
  onSave: (values: FormValues) => Promise<unknown>; onClose: () => void; submitLabel?: string
}) {
  const [values, setValues] = useState(initial)
  const mutation = useMutation()
  const id = useId()
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog open onOpenChange={open => { if (!open && !mutation.busy) onClose() }}>
    <DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus() }}>
      <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription></DialogHeader>
      <form onSubmit={event => { event.preventDefault(); void mutation.run(() => onSave(values), onClose) }}>
        <div className="ns-fields">{(typeof fields === "function" ? fields(values) : fields).map((field, index) => {
          const fieldId = `${id}-${field.name}`, hintId = `${fieldId}-hint`, errorId = `${id}-error`
          // When the server names the field it rejected, mark that field and point it at the error.
          const invalid = Boolean(mutation.error && mutation.errorField === field.name)
          const describedBy = [field.hint ? hintId : "", invalid ? errorId : ""].filter(Boolean).join(" ") || undefined
          const change = (value: string | number | boolean) => setValues(previous => ({ ...previous, [field.name]: value }))
          return <div className={`ns-field ${field.type === "checkbox" ? "ns-check" : ""}`} key={field.name}>
            {field.type === "checkbox" ? <><Checkbox id={fieldId} checked={values[field.name] === true} disabled={mutation.busy || field.disabled} onCheckedChange={value => change(value === true)} /><label htmlFor={fieldId}>{field.label}</label></>
              : <><label htmlFor={fieldId}>{field.label}</label>
                {field.type === "textarea" ? <Textarea id={fieldId} rows={8} value={String(values[field.name] ?? "")} required={field.required} maxLength={field.maxLength} disabled={mutation.busy || field.disabled} aria-invalid={invalid || undefined} aria-describedby={describedBy} onChange={event => change(event.target.value)} />
                  : field.type === "select" ? <Choice id={fieldId} label={field.label} value={String(values[field.name] ?? "")} options={field.options ?? []} disabled={mutation.busy || field.disabled} onChange={change} />
                    : <Input id={fieldId} autoFocus={index === 0} type={field.type ?? "text"} value={String(values[field.name] ?? "")} required={field.required} min={field.min} max={field.max} minLength={field.minLength} maxLength={field.maxLength} pattern={field.pattern} disabled={mutation.busy || field.disabled} autoComplete={field.type === "password" ? "new-password" : "off"} title={field.pattern ? field.hint : undefined} aria-invalid={invalid || undefined} aria-describedby={describedBy} onChange={event => change(field.type === "number" && event.target.value !== "" ? Number(event.target.value) : event.target.value)} />}</>}
            {field.hint && <p id={hintId} className="ns-field-hint">{field.hint}</p>}
          </div>
        })}</div>
        {mutation.error && <p id={`${id}-error`} className="ns-error" role="alert">{mutation.error}</p>}
        <div className="ns-dialog-actions"><Button type="button" variant="outline" disabled={mutation.busy} onClick={onClose}>Cancel</Button><Button type="submit" disabled={mutation.busy}>{mutation.busy ? "Saving…" : submitLabel}</Button></div>
      </form>
    </DialogContent>
  </Dialog>
}

function Confirm({ title, description, label, onConfirm, onClose }: { title: string; description: string; label: string; onConfirm: () => Promise<unknown>; onClose: () => void }) {
  const mutation = useMutation()
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog open onOpenChange={open => { if (!open && !mutation.busy) onClose() }}><DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus() }}>
    <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription></DialogHeader>
    {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
    <div className="ns-dialog-actions"><Button autoFocus variant="outline" disabled={mutation.busy} onClick={onClose}>Cancel</Button><Button variant="destructive" disabled={mutation.busy} onClick={() => void mutation.run(onConfirm, onClose)}>{mutation.busy ? "Saving…" : label}</Button></div>
  </DialogContent></Dialog>
}

function SkillsPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/settings/skills", signal => listSettingsSkills({ signal }).then(({ data }) => data))
  const [search, setSearch] = useState(""), [scope, setScope] = useState("all")
  const [editor, setEditor] = useState<SettingsSkill | "new" | null>(null), [remove, setRemove] = useState<SettingsSkill | null>(null)
  const clear = () => { setSearch(""); setScope("all") }
  return <LoadState resource={resource}>{data => {
    const rows = data.skills.filter(row => contains(search, row.name, row.description, row.content) && (scope === "all" || row.scope === scope))
    return <><CapabilityNote {...data} />
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} filter={<Choice label="Skill scope" value={scope} onChange={setScope} options={scopeOptions} />}
        action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />New skill</Button>} />
      {rows.length ? <div className="ns-records">{rows.map(row => <article className="ns-record" key={row.id}>
        <div className="ns-record-copy"><div className="ns-record-title"><h2>{row.name}</h2><span className="ns-badge">{row.scope}</span>{!row.enabled && <span className="ns-badge">Disabled</span>}</div><p>{row.description}</p>
          <details><summary>View instructions</summary><pre>{row.content}</pre></details><small>Updated {dateTime(row.updated_at)}</small></div>
        <div className="ns-row-actions">{row.editable && data.capabilities?.update && <Button variant="outline" size="sm" onClick={() => setEditor(row)} aria-label={`Edit ${row.name}`}><Pencil />Edit</Button>}
          {row.editable && data.capabilities?.delete && <Button variant="ghost" size="icon-sm" aria-label={`Delete ${row.name}`} onClick={() => setRemove(row)}><Trash2 /></Button>}</div>
      </article>)}</div> : <Empty filtered={Boolean(search) || scope !== "all"} noun="skills" onClear={clear} />}
      {editor && <Editor title={editor === "new" ? "New skill" : "Edit skill"} description="Instructions are available to the selected scope. Bundled skills are managed by the server." fields={[
        { name: "name", label: "Name", required: true, maxLength: 64, pattern: "[a-z0-9]+(?:-[a-z0-9]+)*", hint: "Lowercase letters and numbers, separated by hyphens." }, { name: "description", label: "Description", required: true, maxLength: 1024 },
        { name: "scope", label: "Scope", type: "select", options: scopes(user, data.capabilities) }, { name: "content", label: "Instructions", type: "textarea", required: true, maxLength: 65536 },
        { name: "enabled", label: "Enable this skill", type: "checkbox" },
      ]} initial={editor === "new" ? { name: "", description: "", scope: "personal", content: "", enabled: true } : { name: editor.name, description: editor.description, scope: editor.scope, content: editor.content, enabled: editor.enabled }}
        onSave={async values => {
          const body = { name: String(values.name), description: String(values.description), content: String(values.content), scope: scopeInput(values.scope), enabled: values.enabled === true }
          if (editor === "new") await createSettingsSkill({ body })
          else await updateSettingsSkill({ path: { identity: editor.id }, body })
          resource.refresh()
        }} onClose={() => setEditor(null)} />}
      {remove && <Confirm title={`Delete ${remove.name}?`} description="This removes the saved skill. Existing conversations are kept." label="Delete skill" onConfirm={async () => { await deleteSettingsSkill({ path: { identity: remove.id } }); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function MemoryPage() {
  const [namespace, setNamespace] = useState("")
  const resource = useResource(`/settings/memory${namespace ? `?${new URLSearchParams({ namespace })}` : ""}`, signal => listSettingsMemory({ query: namespace ? { namespace } : undefined, signal }).then(({ data }) => data))
  const [options, setOptions] = useState<Option[]>([])
  const [search, setSearch] = useState("")
  const [editor, setEditor] = useState<SettingsMemoryFile | "new" | null>(null), [remove, setRemove] = useState<SettingsMemoryFile | null>(null)
  useEffect(() => { if (resource.data) setOptions(resource.data.namespaces.map(item => ({ value: item.id, label: item.label }))) }, [resource.data])
  return <>
    {options.length > 0 && <div className="ns-scope-picker"><label htmlFor="memory-namespace">Memory namespace</label><Choice id="memory-namespace" label="Memory namespace" value={namespace || resource.data?.namespace || resource.data?.default_namespace || options[0].value} onChange={value => { setNamespace(value); setEditor(null); setRemove(null) }} options={options} /></div>}
    <LoadState resource={resource}>{data => {
      const rows = data.memories.filter(row => contains(search, row.path, row.content))
      return <><CapabilityNote {...data} />{data.truncated && <p className="ns-notice">Showing the first {count(data.memories.length)} memory files in this namespace. Additional files are not included in this list.</p>}<Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
        action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />Add memory</Button>} />
        {rows.length ? <div className="ns-records">{rows.map(row => <article key={`${row.namespace}/${row.path}`} className="ns-record">
          <div className="ns-record-copy"><h2>{row.path}</h2><details><summary>View memory</summary><pre>{row.content}</pre></details>{row.truncated && <p>This preview is truncated. Editing is unavailable to avoid overwriting unseen content.</p>}</div>
          <div className="ns-row-actions">{data.capabilities?.update && !row.truncated && <Button size="sm" variant="outline" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.path}</span></Button>}
            {data.capabilities?.delete && <Button size="icon-sm" variant="ghost" aria-label={`Delete ${row.path}`} onClick={() => setRemove(row)}><Trash2 /></Button>}</div>
        </article>)}</div> : <Empty filtered={Boolean(search)} noun="memories" onClear={() => setSearch("")} />}
        {editor && <Editor title={editor === "new" ? "Add memory" : "Edit memory"} description="Changes apply to the selected namespace. A newer saved version will not be overwritten." initial={{ path: editor === "new" ? "" : editor.path, content: editor === "new" ? "" : editor.content }} fields={[
          { name: "path", label: "Path", required: true, disabled: editor !== "new", pattern: "(?!.*\\.\\.)[A-Za-z0-9_][A-Za-z0-9_.\\-]{0,199}\\.md", maxLength: 203, hint: "Use a Markdown filename such as preferences.md, without directory segments." },
          { name: "content", label: "Memory content", type: "textarea", required: true, maxLength: 65536 },
        ]} onSave={async values => { await writeSettingsMemory({ query: { namespace: editor === "new" ? data.namespace : editor.namespace, path: String(values.path) }, body: { content: String(values.content), version: editor === "new" ? null : editor.version } }); resource.refresh() }} onClose={() => setEditor(null)} />}
        {remove && <Confirm title={`Delete ${remove.path}?`} description="This removes the saved memory from this namespace. Existing conversations are kept." label="Delete memory" onConfirm={async () => { await deleteSettingsMemory({ query: { namespace: remove.namespace, path: remove.path, ...(remove.version ? { version: remove.version } : {}) } }); resource.refresh() }} onClose={() => setRemove(null)} />}
      </>
    }}</LoadState>
  </>
}

const routingRanges = [{ value: "1", label: "Last 24 hours" }, { value: "7", label: "Last 7 days" }, { value: "30", label: "Last 30 days" }]

function RoutingCounts({ title, rows, label }: { title: string; rows: RoutingCount[]; label: (name: string) => string }) {
  return <div className="analytics-table-frame" role="region" aria-label={title} tabIndex={0}>
    <Table><caption className="ns-routing-caption">{title}</caption>
      <TableHeader><TableRow><TableHead scope="col">Name</TableHead><TableHead scope="col">Messages</TableHead></TableRow></TableHeader>
      <TableBody>{rows.map(row => <TableRow key={row.name}><TableCell>{label(row.name)}</TableCell><TableCell>{count(row.count)}</TableCell></TableRow>)}</TableBody>
    </Table>
  </div>
}

function RoutingPage() {
  const [days, setDays] = useState("7")
  const resource = useResource(`/settings/routing?days=${days}`, signal => getSettingsRouting({ query: { days: Number(days) }, signal }).then(({ data }) => data))
  return <LoadState resource={resource}>{data => <div className="native-analytics ns-routing">
    <div className="ns-toolbar"><Choice label="Time range" value={days} onChange={setDays} options={routingRanges} /></div>
    <AnalyticsMetrics items={[
      ["Routed messages", count(data.decisions), "Messages sent with Auto"],
      ["Median routing time", data.median_router_ms === null ? "Not recorded" : `${count(data.median_router_ms)} ms`, "Time to choose a model"],
      ["Task classifier", data.laya ? "Laya" : "Off", data.laya ? "Unsure answers go to the LLM router" : "The LLM router decides alone"],
      ["Benchmark scoreboard", data.benchmark ?? "None yet", data.benchmark ? "Ranks the models for each task type" : "Run benchmarks/report.py to create one"],
    ]} />
    {data.models.length === 0 ? <div className="ns-empty"><h2>No turns in this period</h2><p>Send a message with Auto selected, or choose a longer time range.</p></div> : <>
      <div className="analytics-table-frame" role="region" aria-label="Models: routed messages, turns and failures" tabIndex={0}>
        <Table><caption className="ns-routing-caption">Models. Failed turns ended with an error; the rate is failed turns out of all turns.</caption>
          <TableHeader><TableRow>{["Model", "Routed by Auto", "All turns", "Failed", "Failure rate"].map(column => <TableHead scope="col" key={column}>{column}</TableHead>)}</TableRow></TableHeader>
          <TableBody>{data.models.map(row => <TableRow key={row.model}>
            <TableCell className="ns-routing-model">{row.model}</TableCell><TableCell>{count(row.routed)}</TableCell><TableCell>{count(row.turns)}</TableCell>
            <TableCell>{count(row.failed)}</TableCell><TableCell>{row.turns ? `${Math.round(row.failed / row.turns * 100)}%` : "n/a"}</TableCell>
          </TableRow>)}</TableBody>
        </Table>
      </div>
      {data.decisions > 0 && <div className="ns-routing-split">
        <RoutingCounts title="How models were chosen" rows={data.engines} label={engineLabel} />
        <RoutingCounts title="Task types" rows={data.task_types} label={taskLabel} />
      </div>}
    </>}
  </div>}</LoadState>
}

function GeneralPage({ onPreferences }: { onPreferences: (preferences: GeneralPreferences) => void }) {
  const resource = useResource("/settings/general", signal => getSettingsGeneral({ signal }).then(({ data }) => data))
  return <LoadState resource={resource}>{data => <GeneralForm initial={data} onPreferences={onPreferences} />}</LoadState>
}

/** WCAG 2.2.2: a way to stop decorative animation, kept per browser. */
function ReduceMotionRecord() {
  const reduced = useSyncExternalStore(subscribeMotion, currentMotion) === "reduced"
  return <div className="ns-record"><div className="ns-record-copy"><h2 id="reduce-motion-title">Reduce motion</h2><p id="reduce-motion-hint">Stop the animated background and transitions. This applies to this browser only.</p></div>
    <div className="ns-row-actions"><Switch checked={reduced} onCheckedChange={checked => setMotion(checked ? "reduced" : "full")} aria-labelledby="reduce-motion-title" aria-describedby="reduce-motion-hint" /></div>
  </div>
}

function GeneralForm({ initial, onPreferences }: { initial: GeneralPreferences; onPreferences: (preferences: GeneralPreferences) => void }) {
  const [preferences, setPreferences] = useState(initial)
  const mutation = useMutation()
  const save = (patch: Partial<GeneralPreferences>) => {
    let updated: GeneralPreferences
    void mutation.run(async () => { updated = (await updateSettingsGeneral({ body: patch })).data }, () => {
      setPreferences(updated); onPreferences(updated)
    })
  }
  return <>
    <div className="ns-records">
      <div className="ns-record"><div className="ns-record-copy"><h2>Message delivery</h2><p>When you send a message while ModelMesh is working.</p></div>
        <div className="ns-row-actions"><Choice label="Message delivery" value={preferences.delivery_mode} disabled={mutation.busy} onChange={value => save({ delivery_mode: value as GeneralPreferences["delivery_mode"] })}
          options={[{ value: "when_idle", label: "Queue next" }, { value: "asap", label: "Send now" }]} /></div>
      </div>
      <ReduceMotionRecord />
    </div>
    {mutation.busy && <p className="ns-muted" role="status">Saving…</p>}
    {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
  </>
}

function UsersPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/settings/users", loadUsers)
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [editor, setEditor] = useState<Account | "new" | null>(null), [remove, setRemove] = useState<Account | null>(null)
  return <LoadState resource={resource}>{data => {
    const rows = data.users.filter(row => contains(search, row.username, row.display_name) && (filter === "all" || (filter === "disabled" ? !row.enabled : row.role === filter)))
    return <><CapabilityNote {...data} /><Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
      filter={<Choice label="User role or access" value={filter} onChange={setFilter} options={[{ value: "all", label: "All members" }, { value: "admin", label: "Administrators" }, { value: "member", label: "Members" }, { value: "disabled", label: "Disabled" }]} />}
      action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />Add user</Button>} />
      {rows.length ? <RecordTable label="Workspace users" headings={["User", "Role", "Access", "Actions"]}>{rows.map(row => <TableRow key={row.id}>
        <TableCell><strong>{row.display_name || row.username}{row.id === user.id ? " (you)" : ""}</strong><small>{row.username}</small></TableCell><TableCell>{row.role === "admin" ? "Administrator" : "Member"}</TableCell><TableCell><span className={`ns-badge ${row.enabled ? "ns-success" : ""}`}>{row.enabled ? "Enabled" : "Disabled"}</span></TableCell>
        <TableCell><div className="ns-row-actions">{data.capabilities?.update && <Button size="sm" variant="outline" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.username}</span></Button>}
          {data.capabilities?.delete && row.enabled && row.id !== user.id && <Button size="sm" variant="ghost" onClick={() => setRemove(row)}>Disable<span className="visually-hidden"> {row.username}</span></Button>}</div></TableCell>
      </TableRow>)}</RecordTable> : <Empty filtered={Boolean(search) || filter !== "all"} noun="users" onClear={() => { setSearch(""); setFilter("all") }} />}
      {editor && <Editor title={editor === "new" ? "Add user" : "Edit user"} description={editor === "new" ? "Share new credentials through an approved channel." : "Saving changes revokes existing sign-ins and stops this user’s pending or running work. Saved history is retained."} initial={editor === "new" ? { username: "", display_name: "", password: "", role: "member", enabled: true } : { username: editor.username, display_name: editor.display_name, password: "", role: editor.role, enabled: editor.enabled }} fields={[
        { name: "username", label: "Username", required: true, disabled: editor !== "new", maxLength: 200, hint: "Start with a letter or number. Use only letters, numbers and . _ @ + -", pattern: "[A-Za-z0-9][A-Za-z0-9_.@+\\-]*" }, { name: "display_name", label: "Display name", required: true, maxLength: 200 },
        { name: "password", label: editor === "new" ? "Password" : "New password", type: "password", required: editor === "new", minLength: 12, maxLength: 4096, hint: editor === "new" ? "Use at least 12 characters and a unique password." : "Leave blank to keep the existing password; replacements need at least 12 characters." },
        { name: "role", label: "Role", type: "select", options: [{ value: "member", label: "Member" }, { value: "admin", label: "Administrator" }], disabled: editor !== "new" && editor.id === user.id },
        { name: "enabled", label: "Enable workspace access", type: "checkbox", disabled: editor !== "new" && editor.id === user.id },
      ]} onSave={async values => {
        const role = values.role === "admin" ? "admin" as const : "member" as const
        if (editor === "new") await createSettingsUser({ body: { username: String(values.username), display_name: String(values.display_name), password: String(values.password), role, enabled: values.enabled === true } })
        else {
          const body: UserEditWritable = {}
          if (values.display_name !== editor.display_name) body.display_name = String(values.display_name)
          if (values.role !== editor.role) body.role = role
          if (values.enabled !== editor.enabled) body.enabled = values.enabled === true
          if (values.password) body.password = String(values.password)
          if (!Object.keys(body).length) return
          await updateSettingsUser({ path: { identity: editor.id }, body })
        }
        resource.refresh()
      }} onClose={() => setEditor(null)} />}
      {remove && <Confirm title={`Disable ${remove.display_name || remove.username}?`} description="This revokes workspace access and existing sign-ins, and stops this user’s pending or running work. Saved history is retained." label="Disable user" onConfirm={async () => { await deleteSettingsUser({ path: { identity: remove.id } }); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function AutomationEditor({ user, onClose, refresh }: { user: SettingsUser; onClose: () => void; refresh: () => void }) {
  const sessions = useResource("/sessions", loadSessions)
  const models = useResource("/me", signal => getMe({ signal }).then(({ data }) => data))
  if (sessions.error || models.error || !sessions.data || !models.data) return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="native-settings-dialog"><DialogHeader><DialogTitle>New automation</DialogTitle><DialogDescription>Choose an active session and a model available to your account.</DialogDescription></DialogHeader>
    {sessions.error || models.error ? <><p className="ns-error" role="alert">{sessions.error || models.error}</p><Button variant="outline" onClick={() => { sessions.refresh(); models.refresh() }}>Try again</Button></> : <p role="status">Loading sessions and models…</p>}
  </DialogContent></Dialog>
  const eligible = sessions.data.filter(row => row.owner_id === user.id && !row.archived && !row.deleted && row.surface === "web" && row.visibility === "private")
  if (!eligible.length || !models.data.models.length) return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="native-settings-dialog"><DialogHeader><DialogTitle>New automation</DialogTitle><DialogDescription>{!eligible.length ? "Create an active web session you own before scheduling work." : "No models are available to this account. Ask an administrator to configure model access."}</DialogDescription></DialogHeader><Button variant="outline" onClick={onClose}>Close</Button></DialogContent></Dialog>
  const modelOptions = models.data.models.map(value => ({ value, label: value }))
  return <Editor title="New automation" description="Recurring work runs in the selected session using the chosen model. Review the prompt and interval before saving." submitLabel="Create automation"
    initial={{ name: "", prompt: "", session_id: eligible[0].id, model: modelOptions.some(row => row.value === models.data?.default_model) ? models.data.default_model : modelOptions[0].value, interval_minutes: 1440 }} fields={[
      { name: "name", label: "Name", required: true, maxLength: 150 }, { name: "prompt", label: "Instructions", type: "textarea", required: true, maxLength: 100000 },
      { name: "session_id", label: "Session", type: "select", options: eligible.map(row => ({ value: row.id, label: row.title })) },
      { name: "model", label: "Model", type: "select", options: modelOptions }, { name: "interval_minutes", label: "Repeat every (minutes)", type: "number", required: true, min: 5, max: 525600 },
    ]} onSave={async values => {
      await createSettingsAutomation({ body: { name: String(values.name), prompt: String(values.prompt), session_id: String(values.session_id), model: String(values.model), interval_minutes: Number(values.interval_minutes) } }); refresh()
    }} onClose={onClose} />
}

function AutomationLogs({ automation, onClose }: { automation: SettingsAutomation; onClose: () => void }) {
  const resource = useResource(`/settings/automations/${encodeURIComponent(automation.id)}/logs`, signal => listSettingsAutomationLogs({ path: { identity: automation.id }, signal }).then(({ data }) => data))
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus() }}>
    <DialogHeader><DialogTitle>{automation.name}: run history</DialogTitle><DialogDescription>Recorded admissions and their status. Open the session to review a turn’s output.</DialogDescription></DialogHeader>
    <LoadState resource={resource}>{data => data.logs.length ? <RecordTable label="Automation run history" headings={["Created", "Status", "Turn"]}>{data.logs.map(row => <TableRow key={row.id}><TableCell>{dateTime(row.created_at)}</TableCell><TableCell>{row.status}</TableCell><TableCell>{row.turn_id ? <a className="ns-link" href={`#${automation.session_id}`}>{row.turn_id}</a> : "No turn admitted"}</TableCell></TableRow>)}</RecordTable> : <p className="ns-muted">No recorded runs yet.</p>}</LoadState>
    <Button variant="outline" disabled={resource.loading} onClick={resource.refresh}><RefreshCw />Refresh history</Button>
  </DialogContent></Dialog>
}

function AutomationsPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/settings/automations", signal => listSettingsAutomations({ signal }).then(({ data }) => data))
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [create, setCreate] = useState(false), [remove, setRemove] = useState<SettingsAutomation | null>(null), [logs, setLogs] = useState<SettingsAutomation | null>(null)
  const mutation = useMutation()
  return <LoadState resource={resource}>{data => {
    const rows = data.automations.filter(row => contains(search, row.name, row.prompt) && (filter === "all" || (filter === "paused" ? row.paused : !row.paused)))
    return <><CapabilityNote {...data} /><Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
      filter={<Choice label="Automation state" value={filter} onChange={setFilter} options={[{ value: "all", label: "All automations" }, { value: "active", label: "Not paused" }, { value: "paused", label: "Paused" }]} />}
      action={data.capabilities?.create && <Button onClick={() => setCreate(true)}><Plus />New automation</Button>} />
      {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
      {rows.length ? <div className="ns-records">{rows.map(row => <article key={row.id} className="ns-record"><CalendarClock className="ns-record-icon" aria-hidden="true" /><div className="ns-record-copy"><div className="ns-record-title"><h2>{row.name}</h2><span className="ns-badge">{row.paused ? "Paused" : row.state}</span></div>
        <p>Every {count(row.interval_minutes)} minutes · {row.model}</p><details><summary>View instructions</summary><pre>{row.prompt}</pre></details><a className="ns-link" href={`#${row.session_id}`}>Open session</a></div>
        <div className="ns-row-actions"><Button variant="outline" size="sm" onClick={() => setLogs(row)}>Run history<span className="visually-hidden"> for {row.name}</span></Button>
          {data.capabilities?.update && <Button variant="outline" size="sm" disabled={mutation.busy} onClick={() => void mutation.run(() => updateSettingsAutomation({ path: { identity: row.id }, body: { paused: !row.paused } }), resource.refresh)}>{row.paused ? "Resume" : "Pause"}<span className="visually-hidden"> {row.name}</span></Button>}
          {data.capabilities?.delete && <Button variant="ghost" size="icon-sm" aria-label={`Delete ${row.name}`} disabled={mutation.busy} onClick={() => setRemove(row)}><Trash2 /></Button>}</div>
      </article>)}</div> : <Empty filtered={Boolean(search) || filter !== "all"} noun="automations" onClear={() => { setSearch(""); setFilter("all") }} />}
      {create && <AutomationEditor user={user} refresh={resource.refresh} onClose={() => setCreate(false)} />}
      {logs && <AutomationLogs automation={logs} onClose={() => setLogs(null)} />}
      {remove && <Confirm title={`Delete ${remove.name}?`} description="Future scheduled admissions stop. Work already admitted to a session and its history are kept." label="Delete automation" onConfirm={async () => { await deleteSettingsAutomation({ path: { identity: remove.id } }); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function OverviewPage({ user, navigate }: { user: SettingsUser; navigate: (page: string) => void }) {
  const resource = useResource("/settings/overview", signal => getSettingsOverview({ signal }).then(({ data }) => data))
  return <>
    {resource.error && <p className="ns-notice" role="alert">Overview counts could not be loaded. {resource.error} <Button variant="ghost" onClick={resource.refresh}>Try again</Button></p>}
    {resource.loading && <p className="ns-muted" role="status">Loading overview counts…</p>}
    <div className="ns-overview-grid">{groups.filter(group => group !== "Administration" || user.role === "admin").map(group => <section key={group} className="ns-overview-group"><div className="ns-section-heading"><h2>{group}</h2>{group === "Administration" && <span className="ns-badge">Admin</span>}</div>
      <div>{destinations.filter(item => item.group === group).map(item => <SettingsLink key={item.id} page={item.id} navigate={navigate} className="ns-overview-link"><item.Icon size={20} aria-hidden="true" /><span><strong>{item.title}</strong><span>{item.description}</span></span>
        {resource.data?.counts[item.id] !== undefined && <small>{count(resource.data.counts[item.id])}</small>}<ChevronRight size={18} aria-hidden="true" /></SettingsLink>)}</div>
    </section>)}</div>
  </>
}

function SettingsLink({ page, navigate, children, ...props }: { page: string; navigate: (page: string) => void; children: React.ReactNode } & Omit<React.ComponentProps<"a">, "href" | "onClick">) {
  return <a {...props} href={`#settings/${page}`} onClick={event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return
    event.preventDefault(); navigate(page)
  }}>{children}</a>
}

export function SettingsPage({ user, page, onNavigate, onBack, sidebarCollapsed, onToggleSidebar, onPreferences }: SettingsPageProps) {
  const current = !page || page === "settings" ? "overview" : page === "spend" ? "usage" : page
  const destination = destinations.find(item => item.id === current)
  const [navOpen, setNavOpen] = useState(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const title = current === "overview" ? "Settings" : destination?.title ?? "Page not found"
  const reportPage = user.role === "admin" && (current === "performance" || current === "usage")
  useEffect(() => {
    setNavOpen(false); heading.current?.focus(); content.current?.scrollTo({ top: 0 })
    const previous = document.title
    document.title = `${title} · ModelMesh`
    return () => { document.title = previous }
  }, [current, title])
  const navigate = (value: string) => { setNavOpen(false); onNavigate(value) }
  const navLink = (value: string, label: string, icon: React.ReactNode) => <SettingsLink page={value} navigate={navigate} aria-current={current === value ? "page" : undefined}>{icon}<span>{label}</span></SettingsLink>
  return <div className={`native-settings ${sidebarCollapsed ? "ns-sidebar-collapsed" : ""}`}>
    <a className="ns-skip" href="#native-settings-content" onClick={event => { event.preventDefault(); heading.current?.focus() }}>Skip to settings content</a>
    <aside className="ns-rail">
      <div className="ns-brand-row"><Brand onClick={event => { if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onBack() } }} /><SidebarToggle collapsed={sidebarCollapsed} onToggle={onToggleSidebar} /><Button className="ns-nav-toggle" size="icon" variant="ghost" aria-label="Toggle settings navigation" aria-expanded={navOpen} aria-controls="native-settings-navigation" onClick={() => setNavOpen(value => !value)}><Menu /></Button></div>
      <Button className="ns-back" variant="ghost" onClick={onBack}><ArrowLeft size={16} />Back to workspace</Button>
      <nav id="native-settings-navigation" className={navOpen ? "ns-navigation open" : "ns-navigation"} aria-label="Settings">
        <p className="ns-nav-title">Settings</p>{navLink("overview", "Overview", <LayoutGrid size={17} aria-hidden="true" />)}
        {groups.filter(group => group !== "Administration" || user.role === "admin").map(group => <section className="ns-nav-group" key={group}><h2>{group}</h2>{destinations.filter(item => item.group === group).map(item => <React.Fragment key={item.id}>{navLink(item.id, item.title, <item.Icon size={17} aria-hidden="true" />)}</React.Fragment>)}</section>)}
      </nav><div className="ns-account"><span>{user.name}</span><small>{user.role === "admin" ? "Administrator" : "Workspace member"}</small></div>
    </aside>
    <div className="ns-main"><div className="ns-breadcrumb"><nav className="ns-breadcrumb-trail" aria-label="Breadcrumb"><SettingsLink page="overview" navigate={navigate} aria-current={current === "overview" ? "page" : undefined}>Settings</SettingsLink>{current !== "overview" && <><ChevronRight size={14} aria-hidden="true" /><span aria-current="page">{title}</span></>}</nav><AppearanceToggle /></div>
      <div className="ns-content" id="native-settings-content" ref={content}>{!reportPage && <header className="ns-page-heading"><h1 ref={heading} tabIndex={-1}>{title}</h1>{destination?.description && current !== "general" && <p>{destination.description}</p>}</header>}
        <div key={`${user.id}:${current}`}>
          {destination?.admin && user.role !== "admin" ? <div className="ns-empty" role="alert"><Shield size={24} /><h2>Administrator access required</h2><p>This page is available to workspace administrators.</p><Button variant="outline" onClick={() => navigate("overview")}>Back to overview</Button></div>
            : current === "overview" ? <OverviewPage user={user} navigate={navigate} />
              : current === "general" ? <GeneralPage onPreferences={onPreferences} />
              : current === "skills" ? <SkillsPage user={user} />
                : current === "automations" ? <AutomationsPage user={user} />
                  : current === "memory" ? <MemoryPage />
                        : current === "users" ? <UsersPage user={user} />
                          : current === "performance" || current === "usage" ? <AnalyticsPage kind={current} headingRef={heading} />
                          : current === "routing" ? <RoutingPage />
                            : <div className="ns-empty"><p>This settings page does not exist.</p><Button variant="outline" onClick={() => navigate("overview")}>Back to overview<ArrowRight /></Button></div>}
        </div>
      </div>
    </div>
  </div>
}
