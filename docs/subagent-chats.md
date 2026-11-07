# Ordinary subagent conversations

Ask a normal chat to delegate three self-contained tasks. New `delegate_task`
calls create three persistent native sessions. Open **Workspace → Subagents**
and select a child to use the same transcript, composer, queued input,
**Send now**, questions, files, activity and Stop controls as any other session.
**Parent chat** returns to its direct parent.

The left sidebar nests subagents under their parent, with an expandable group.
Selecting a name opens the full conversation. **Open in side chat**, available
beside the child and in its menu or Workspace row, opens that same session in
the right panel while preserving the main conversation and its draft. The
side chat has its own composer, queue, **Send now** and Stop controls.
Reopening the same child selects its existing tab and retains the side-chat draft.

An authorized parent outside the current history page is included as sidebar
context without consuming pagination slots. Missing or unavailable parents do
not hide accessible children. Explicit child pins and folder placements retain
their own location; otherwise children follow their parent's sidebar group.
Search continues to return matching conversations independently.

Children begin with the delegated task, copies of input attachment references,
and a frozen snapshot of the parent's model-visible conversation. Images copied
into the assignment are sent once; inherited occurrences point to that assignment
instead of repeating the bytes. Other historical image evidence remains available.
Each child has its own computer and filesystem; share saved outputs explicitly. Children
are private to the requester because their assignments may contain private
working context.

People who can read the parent conversation can inspect the original assignment's
activity in its progress feed. Later private follow-ups stay in the child's chat.

For independent work, the parent can use `spawn_agent(task)`. It returns a
`session_id` as soon as the child is admitted, so the parent can continue its
own model and tool steps while the child runs. This is an acceptance receipt,
not a completed result. The synchronous `delegate_task` tool still waits.

The parent can guide a running child with
`send_message(target=session_id, message="...")`. A generated child can push an
intermediate finding or blocker with `send_message(target="parent", message="...")`
and continue working. These are explicit messages: ordinary commentary is not
automatically forwarded. The receipt means queued, not consumed or answered.
Messages do not start a new turn on an idle agent or revive a stopped one.
The maximum is eight unread explicit messages per recipient assignment.

Completion reports are delivered automatically. Pydantic AI's public enqueue
API adds incoming messages to the recipient's next model step; Temporal signals
carry durable notification IDs. The parent can continue its own work throughout.
`wait_agent(agent_ids=[], timeout_seconds=60)` waits for incoming messages,
including parent guidance. Waiting for outstanding selected children also wakes
on incoming messages, which appear in the next model step. These waits use
Temporal conditions rather than model-driven report polling. A timeout leaves
children running. Each report identifies completed, failed, stopped, deleted,
or human-input work; a non-completed status is not a successful result.
Automatic reports are bounded previews; `wait_agent` can reread the saved
original report. Pending handles travel with the parent's durable execution
and history checkpoints. If the parent asks a human question, its authenticated
answer continuation retains access to those original reports. Ordinary later
follow-ups do not acquire that access. Pending messages also follow authenticated
answer continuations in either direction, preserving the original assignment.
Messages are reference data, not new human authorization.
Before finalizing an answer, a parent with outstanding
spawned children waits for their reports or human steering, then gets another
model step to incorporate the evidence. Stop still affects only the selected
conversation; it does not automatically cancel independently admitted children.

The parent receives the original delegated turn's report, including steering
accepted into that turn. At settlement this report is saved and later child
conversations cannot rewrite it. Queued follow-ups run independently and are not
exported back into the parent, which may have a different visibility. A child
needing a human answer, stopped by the user, failed, or deleted produces that
status rather than a successful result. A later child answer/follow-up does not
automatically restart a completed parent. Stop affects the selected conversation;
other independently admitted sessions keep their own controls.
Optional clarification preferences keep the parent waiting for the child's
answer or automatic fallback; they do not freeze a human-input handoff. Typed
answers and timeout continuations retain generated provenance and are reserved
ahead of older queued follow-ups.

Delegation is bounded to the root, its children and their grandchildren. Swarm
members and chats at the depth limit cannot create children, and a stale or
direct call to the delegate tools is refused before a child is admitted or a
computer is created. Each child uses ordinary turn admission and durable
Temporal execution. Generated work cannot grant human authority. A human
follow-up to a child uses normal admission and can steer it and reuse its
computer. Use a new ordinary conversation for different context.

Old SDK-run delegates remain inspectable in their original panels. They are not
converted into new conversations, and recorded workflows keep their original
delegation path through Temporal version markers. Swarm members cannot use
these tools. Depth-limited children can send messages and wait for guidance but
cannot create further children.

The encrypted event outbox separates Temporal's signal acknowledgment from
Pydantic consuming a message. Dispatch retries reuse the same message ID, and
the receiving workflow deduplicates it. Checkpoints preserve queued messages and
acknowledge messages already included in their saved conversation before history
rollover. Answer snapshots also acknowledge included messages in their save
transaction. Human-answer continuations reconcile older paused snapshots before
requeuing unread mail and retain only children whose completion is still pending.
Mail reads distinguish a ready payload, an obsolete message and an unavailable
authorization check. Only a successful read can establish that a message is
obsolete. An unavailable read retains its place at the head of the mailbox and
retries through Temporal, so completion cannot overtake blocked progress. Stop,
agent exit and history handoff release delivery handlers while preserving unread
outbox state. A delivery version marker retains the original activity and result
semantics when replaying recorded histories.
The dispatcher reconciles settled children and retries notifications. This is
infrastructure coordination, not parent model polling. No separate agent loop is
introduced.

## Regression coverage

These tests in `native/tests` cover delegation. They use the real Pydantic AI SDK
and a local Temporal server with controlled models:

- `test_background_delegation.py`: parent and child overlap, waits, automatic
  report delivery, checkpoint rollover and replay, idempotent starts, stopped
  children and private follow-up isolation.
- `test_agent_messages.py` and `test_agent_mail_delivery.py`: messages between
  parent and child, duplicate signals, worker replacement, backpressure and
  replay.
- `test_delegated_sessions.py`, `test_delegation_policy.py`,
  `test_delegation_authority.py`, `test_delegation_retries.py` and
  `test_delegation_temporal.py`: child sessions, depth limits, authority,
  receipt recovery and replay.
- `test_subagent_navigation.py`: the sidebar and side-chat navigation.
