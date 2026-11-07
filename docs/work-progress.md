# Working updates

The web interface expands working updates while a task runs. Public parent commentary,
grouped tool activity and named subagent updates appear in the conversation.
Open an agent's details to inspect its history or its interactive child chat.
Private reasoning, tool arguments and tool results are not part of this feed.

A **Send now** correction starts a new working section. Earlier sections stay
visible above that input. Only a saved, nonempty parent answer collapses the
answered section; stopped or failed work without an answer stays expanded.
Saved activity includes history continuations and remains available after reload.
Work resumed after a login or approval has its own activity section, even without
a new user message. The current action label follows the saved running action;
catching up on old streamed events does not replay them as current work.
Everyone who can read a conversation can see its activity, including live
updates, saved tool details and the original delegated assignments in shared
conversations. Losing conversation access also removes access to its activity.

Expand an action count, such as **11 actions**, then select a row to open its
input and output in the right panel. The selected action updates while it runs;
**Refresh action** reloads its saved details. The same panel opens actions from
subagents and side chats. Credential, memory and capability-loading payloads
remain private. Long inputs and outputs show a labelled preview of the first
100,000 characters.

The feed supports both historical SDK delegates and interactive child chats.
For a child chat it reads only the original delegated assignment and generated
answer continuations. Later unrelated private follow-ups are not copied into
the parent's feed. Opening a child's full chat still requires access to that
conversation. Activity is bounded and indicates when older entries were
omitted. Visible active conversations refresh persisted activity every four
seconds alongside the existing root text stream.

If an execution fails, ModelMesh first saves the settled conversation so a later
message can continue from it. If that cannot be saved, the conversation shows a
recovery warning.

Debug exports identify omitted teammate activity so their visible token totals
are not mistaken for the complete session.
