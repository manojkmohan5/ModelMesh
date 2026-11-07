import React, { useEffect, useRef, useState } from "react"
import { ArrowUpRight, Bug, Clock3, Code2, Globe, ListChecks, LockKeyhole, Search, Send } from "lucide-react"
import { Button } from "@/components/ui/button"
import { SkillComposer } from "./skill-composer"
import { GoalChip, goalCommandAction } from "./goal"
import { Select, SelectContent, SelectItem, SelectValue } from "@/components/ui/select"
import { NamedSelectTrigger } from "./named-select"
import { SpaceBackdrop } from "./space-backdrop"
import { BrandMark } from "./brand"
import "./home.css"
import { csrfToken } from "./api"
import { AttachButton, AttachmentPreviews, useAttachments, useFileDrop, FileDropOverlay } from "./attachments"
import { modelOptions, rememberAuto, startingModel } from "./model-choice"
import type { NewConversation, Session } from "./conversation-state"
import { createSession, createSessionMessage, type NewSession } from "./generated/api"

const starters = [
  { label: "Review code", detail: "Find the edge cases", prompt: "Review my changes for bugs and improvements.", Icon: Code2 },
  { label: "Fix a bug", detail: "Trace it to the source", prompt: "Help me investigate and fix this bug: ", Icon: Bug },
  { label: "Plan a feature", detail: "Make the next change clear", prompt: "Help me plan and implement this feature: ", Icon: ListChecks },
]
export function homeSubmission(prompt: string, model: string, privateSession: boolean, attachments: string[]) {
  const body = { prompt, model, attachments }, visibility: NonNullable<NewSession["visibility"]> = privateSession ? "private" : "shared"
  return { body, visibility, signature: JSON.stringify([visibility, body]) }
}

export function Home({ models, defaultModel, autoModel, initiallyPrivate = false, recentSessions, recentLoading, onSearchSessions, onCreated, onStarted }: {
  models: string[]; defaultModel: string; autoModel?: string | null; initiallyPrivate?: boolean
  recentSessions: { key: string; title: Session["title"]; href: string }[]; recentLoading: boolean; onSearchSessions: () => void
  onCreated: () => Promise<void>; onStarted: (conversation: NewConversation) => void
}) {
  const [draft, setDraft] = useState("")
  const [model, setModel] = useState(() => startingModel(defaultModel, autoModel))
  const [privateSession, setPrivateSession] = useState(initiallyPrivate)
  const isPrivate = privateSession
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [goalMode, setGoalMode] = useState(false)
  const pictures = useAttachments(setError)
  const fileDrop = useFileDrop(pictures.add, setError, busy)
  const [composerFocus, setComposerFocus] = useState(0)
  const admission = useRef<{ signature: string; session?: Session; request: string } | null>(null)
  const submitting = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    document.title = "New session · ModelMesh"
    return () => { mounted.current = false }
  }, [])
  async function send(prompt = draft) {
    if (prompt.trim() === "/goal") { setGoalMode(true); setDraft(""); setComposerFocus(value => value + 1); return }
    if (goalMode && !prompt.trim()) return
    const auth = csrfToken()
    try { await submit(goalMode ? `/goal --budget none ${prompt}` : prompt) }
    catch (reason) { if (mounted.current && csrfToken() === auth) setError(String(reason)) }
  }

  async function submit(prompt: string): Promise<void> {
    const action = goalCommandAction(prompt)
    if (action && action !== "set") throw new Error("Open an existing conversation to change its goal.")
    if (submitting.current) throw new Error("A message is already being sent.")
    if (pictures.uploading) throw new Error("Wait for the attached files to finish uploading, then retry.")
    if (pictures.hasErrors) throw new Error("Retry or remove the failed attachments before sending.")
    if (!prompt.trim() && !pictures.files.length) throw new Error("Enter a message or attach a file.")
    submitting.current = true
    setBusy(true); setError("")
    const auth = csrfToken(), attachments = pictures.readyFiles
    const requireCurrent = () => {
      if (!mounted.current || csrfToken() !== auth) throw new Error("Your sign-in changed. Retry from the current workspace.")
    }
    try {
      const submission = homeSubmission(prompt, model, isPrivate, attachments.map(file => file.id))
      const signature = JSON.stringify([auth, submission.signature])
      if (admission.current?.signature !== signature) admission.current = { signature, request: crypto.randomUUID() }
      const attempt = admission.current
      if (!attempt.session) {
        attempt.session = (await createSession({ body: { model, visibility: submission.visibility } })).data
      }
      requireCurrent()
      const { data: turn } = await createSessionMessage({ path: { session_id: attempt.session.id }, body: {
        ...submission.body, request_key: attempt.request,
      } })
      requireCurrent()
      if (!("id" in turn)) throw new Error("The request did not start a conversation.")
      onStarted({ session: attempt.session, turn, prompt, attachments })
      // Admission succeeded. A list refresh failure must not invite a second start.
      try { await onCreated() }
      catch { if (mounted.current && csrfToken() === auth) setError("Your message was sent, but the session list could not be refreshed.") }
    }
    finally { submitting.current = false; if (mounted.current) setBusy(false) }
  }

  return <section className="home file-drop-zone" {...fileDrop.handlers}>
    {fileDrop.dragging && <FileDropOverlay />}
    <SpaceBackdrop />
    <div className="home-content">
      <header className="home-header">
        {autoModel && <p className="home-eyebrow"><span className="home-eyebrow-beacon" aria-hidden="true" />Smart routing across {models.length} models</p>}
        <h1>What are we <span className="mm-gradient-text">building?</span></h1>
        <p className="home-lede">{autoModel ? "Describe a task. ModelMesh routes it to the best model and gets it done." : "Describe a task and ModelMesh gets it done."}</p>
      </header>
      <form className="home-form" onSubmit={event => { event.preventDefault(); void send() }}>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="composer home-composer">
          <AttachmentPreviews files={pictures.files} remove={busy ? undefined : pictures.remove} retry={busy ? undefined : pictures.retry} draft />
          <div className="home-editor-input"><SkillComposer focusRequest={composerFocus} value={draft} onChange={setDraft} onSend={prompt => void send(prompt)} onGoalSelect={() => { setGoalMode(true); setDraft("") }} shared={!isPrivate} disabled={busy} placeholder={goalMode ? "Describe a goal to keep pursuing…" : "Describe a change, paste an error, or ask a question…"} onPaste={pictures.paste} /></div>
          <div className="composer-actions">
            <div className="home-composer-tools">
              <AttachButton add={pictures.add} disabled={busy} compact />
              <Button type="button" variant="ghost" size="sm" className="home-privacy-toggle" data-private={isPrivate} aria-describedby="home-privacy-hint" disabled={busy}
                title={isPrivate ? "Private · Only you. Click to share with your workspace." : "Public · Shared with your workspace. Click to make private."}
                onClick={() => setPrivateSession(value => !value)}>
                {isPrivate ? <LockKeyhole /> : <Globe />}{isPrivate ? "Private" : "Public"}
              </Button>
              <span id="home-privacy-hint" hidden>{isPrivate ? "Only you can see this session. Select to share it with your workspace." : "Shared with your workspace. Select to make it private."}</span>
              {goalMode && <GoalChip disabled={busy} onRemove={() => { setGoalMode(false); setComposerFocus(value => value + 1) }} />}
            </div>
            <div className="send-actions"><Select value={model} onValueChange={value => { setModel(value); rememberAuto(value === autoModel) }}><NamedSelectTrigger label="Model"><SelectValue /></NamedSelectTrigger><SelectContent>{modelOptions(models, autoModel, value => value.replace(/^openai\//, "")).map(option => <SelectItem value={option.value} key={option.value}>{option.label}</SelectItem>)}</SelectContent></Select>
              <Button type="submit" size="icon" aria-label="Send message" disabled={busy || pictures.uploading || pictures.hasErrors || (!draft.trim() && (goalMode || !pictures.files.length))}><Send /></Button>
            </div>
          </div>
        </div>
        <div className="home-composer-hint">
          {busy && <span role="status" className="routing-status">{model === autoModel ? <><BrandMark size={14} />Picking the best model…</> : "Sending…"}</span>}
          {pictures.uploading && <span role="status">Uploading files…</span>}
          <span>Drop files or paste images · Enter to send</span>
        </div>
      </form>
      <section className="home-recent" aria-labelledby="home-recent-heading">
        <div className="home-recent-header">
          <h2 id="home-recent-heading">Recent sessions</h2>
          <Button type="button" variant="ghost" size="sm" onClick={onSearchSessions}><Search aria-hidden="true" />Search sessions</Button>
        </div>
        {recentSessions.length > 0 ? <ul>
          {recentSessions.map(row => <li key={row.key}><Button asChild variant="ghost" className="home-recent-link">
            <a href={row.href} title={row.title || "Untitled session"}><Clock3 aria-hidden="true" /><span>{row.title || "Untitled session"}</span><ArrowUpRight aria-hidden="true" /></a>
          </Button></li>)}
        </ul> : <p className="muted" role="status">{recentLoading ? "Loading recent sessions…" : "No recent sessions in this view. Search to find more."}</p>}
      </section>
      <div className="home-starters" role="group" aria-label="Start with a prompt" aria-hidden={Boolean(draft.trim())}>
        <p className="home-starters-heading">Start with a prompt</p>
        {starters.map(({ label, detail, prompt, Icon }) => <Button key={label} type="button" variant="ghost" aria-label={label} disabled={busy || Boolean(draft.trim())}
          onClick={() => {
            setDraft(prompt)
            setComposerFocus(value => value + 1)
          }}><Icon aria-hidden="true" /><span className="home-starter-label">{label}</span><span className="home-starter-detail" aria-hidden="true">{detail}</span><ArrowUpRight className="home-starter-arrow" aria-hidden="true" /></Button>)}
      </div>
    </div>
  </section>
}
