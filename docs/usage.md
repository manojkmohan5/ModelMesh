# Using ModelMesh

[Set up ModelMesh](../README.md#set-up) · [Documentation](README.md)

Open <http://127.0.0.1:8787> and sign in. The first account is `admin`, with the
password you set as `WORKSPACE_PASSWORD`. More accounts are added in
**Settings → Users**.

## Start a conversation

The home page has a message box, a model picker, and a list of recent sessions.
Type a task and press Enter, or choose a starter such as **Review code**.

- **Attach** adds files. You can also drop files anywhere in the chat column or
  paste an image with Ctrl+V or Command+V.
- **Private** or **Public** sets who can see the session. Public sessions are
  shared with every account on this ModelMesh. Private sessions are visible
  only to you. New sessions start as Public.
- The send button is **Send message**.

Each conversation gets its own computer, a Docker container named
`modelmesh-ws-<id>`. The agent works in `/workspace` there. It has Python 3.13
(with uv and pytest), Node.js 22, git, ripgrep, curl and jq. It has no browser
and no desktop. Files stay in the container between turns.

### Attachments

A message can hold up to 8 files of up to 20 MB each. Images must be PNG, JPEG,
GIF or WebP and no larger than 20 megapixels. Zip a folder before attaching it.
The agent sees images directly. It copies other files into its computer when it
needs them.

## Choose a model

The **Model** picker sits next to the send button. The first row,
**Auto (smart routing)**, appears when at least two models are configured
(`MODELMESH_MODELS`) and the gateway is reachable. Auto is selected until you
pick a fixed model. The choice is remembered in this browser only.

With Auto, ModelMesh chooses a model for each message:

1. Messages with images go to the best vision model. Messages longer than
   about 60,000 characters go to the best long-document model.
2. Otherwise Laya reads the message and names a task type (code, math, writing,
   build and so on). The model at the top of the benchmark scoreboard for that
   type runs the message. The scoreboard weighs quality 60%, speed 20% and cost
   20%.
3. If Laya is unsure or cannot be reached, a language model reads the message
   and picks.
4. If routing fails, the message runs on the conversation's previous model, or
   on the default model.

Two more rules keep a conversation steady. If the conversation's last model is
within 5 points of the leader, it stays on that model, so its earlier tool
results are still available. A message sent with **Send now** while a task runs
joins that task on its current model.

### The routing pop-up

After an Auto message, a **Routed to <model>** pop-up appears for about 8
seconds. It shows the task type, how sure Laya was, and the benchmark result
behind the choice. It stays open while you hover over it, focus it, or open
**Details**. **Close routing notice** dismisses it.

**Details** explains why that model ran: how the choice was made (rule, Laya,
LLM router, follow-up, running task or fallback), the task type, the benchmark
report, and a table of the leading models with their quality, median time and
average cost. Each answer keeps a small **via <model> · <task>** tag that opens
the same details later. Screen readers hear a short announcement when a message
is routed.

Under the message box, a line reads "Auto picks the best model for each
message" and names the model the last message ran on.

## While the agent works

A working turn shows elapsed time and the agent's latest progress notes.
Expand the work section to see earlier notes and tool actions, and select an
action to read its input and output in the side panel. When the turn finishes,
the work section collapses and the answer stays visible.

- **Stop** ends the current work. It cannot undo a command that already
  ran.
- If you send a message while the agent is working, it follows your delivery
  setting (**Settings → General**). **Queue next** holds the message until the
  task ends. **Send now** hands it to the running task as soon as it can take
  it. Queued messages appear under **Queued after the current task**, where you
  can **Edit** them, use **Send now**, or cancel them.
- When the agent needs an answer, a **Your input is needed** card appears.
  Pick an option or type a reply. Required questions wait for you. Optional
  questions time out after 90 seconds by default
  (`MODELMESH_OPTIONAL_QUESTION_TIMEOUT`, 5 to 900 seconds), and the agent then
  states its assumption.

## Files and the computer

Open the **+** menu in the side panel and choose **Files**. The Files panel
("Files and sources") lists:

- **Outputs**: files the agent saved with `save_artifact`, and links it
  attached as outputs. Select one to preview it. Text and Markdown render in
  the panel, and images, video and audio use a media viewer.
  **Download original** saves the file.
- **Sources**: files you attached and links the agent attached as sources.

Only files the agent saves appear here. Other files stay on the computer.
Each saved file can be up to 20 MB. Try "draw a cat" with Auto selected. The
drawing shows up as `cat.svg`.

There is no screen or browser to open. To look inside a computer, run
`docker exec -it modelmesh-ws-<id> sh` yourself. `docker ps -a --filter name=modelmesh-ws-`
lists the computers. ModelMesh never removes one, even after you delete the
conversation. Remove it with `docker rm -f <name>` when you no longer need its
files.

### Panels

The **Workspace** button (Ctrl+. or Command+.) opens an overlay of **Subagents**
and **Outputs**. The **+** menu in the side panel adds **Side chat**, **Files**,
**Activity** and **Plan** tabs. **Plan** shows the agent's task list.
**Activity** shows recorded tool calls and has **Get debug trace**. Drag the
divider to resize the panel, or focus it and use the arrow keys. The default
split is 65% chat and 35% panel.

## Find and organize sessions

Press Ctrl+K or Command+K (**Search sessions & commands**) to search. The
sidebar groups sessions into **Pinned**, your folders, **Participated** (shared
sessions of other people that you have posted in) and **Recent** (sessions you
created). **New folder** adds a folder, and you can drag sessions into it.
**Show more sessions** loads older ones. The archive and trash buttons in the
sidebar header switch to archived and deleted sessions.

Each session's menu offers Open in side chat, Folder, Rename, Copy session
link, Mark read or unread, Pin, Archive, **Get debug trace** and **Delete
session…**. Deleting stops active work and moves the session to **Deleted**,
where **Restore session** brings it back. Deleting does not remove the
session's computer.

## Goals

A goal keeps the agent working across turns until it reports the objective
verified. Type `/goal` and choose **Goal** from the picker. A **Goal** chip
appears below the message box. Type the objective and send it. You can also
send `/goal <objective>` directly. Click the chip to leave goal mode.

| Command | Action |
| --- | --- |
| `/goal <objective>` | Start a goal. A new goal replaces the current one. |
| `/goal edit <objective>` | Change the objective. |
| `/goal pause` | Pause work and automatic continuation. |
| `/goal resume` | Resume the saved goal. |
| `/goal clear` | Stop and remove the goal. The conversation stays. |

Objectives can be up to 4,000 characters. A bar above the message box shows
the objective, status, turns and tokens, with buttons **Pause goal** or
**Resume goal**, **Clear goal** and **Expand goal**. The expanded editor has
**Goal details**, where you can set a token budget. Without a budget the goal
has no token limit. `/goal --budget 100000 <objective>` sets one from the
message box. The budget is checked between turns.

Questions and approval requests still wait for your answer. A goal becomes
blocked when its last turn failed or was stopped, or when an action's outcome
is unknown. Goals continue after ModelMesh restarts.

## Subagents

The agent can hand bounded work to subagents. Each one is a full private
conversation with its own computer. Open **Workspace → Subagents** and select
one to read its transcript, send it a message, answer its questions or stop it.
**Parent chat** returns to the conversation that started it. In the sidebar,
subagents nest under their parent. **Open in side chat** shows one in the side
panel while you keep your place in the parent. See
[Subagent chats](subagent-chats.md) for how they run.

## Swarms

A swarm is a team of 2 or more worker conversations with a coordinator, working
on one shared goal. The interface does not start swarms. Start one with
`POST /api/swarms` (see [Swarms](native-swarms.md)). Once a swarm exists, its
conversation shows a **Swarm mission** panel with **Space** and **Chat** views,
the member roster, team controls (**Pause team**, **Resume team**, **Stop team**, a work window) and
a shared team chat.

## Settings

The settings button in the sidebar opens **Settings**. Members see Overview,
General, Skills, Automations and Memory. Administrators also see Users,
Performance, Spend & usage and Routing.

- **Overview** links to each page, with counts.
- **General** has **Message delivery**: **Queue next** or **Send now**.
- **Skills** lists reusable instructions. Search them, filter by **Skill
  scope** (All scopes, Personal, Shared, Bundled), or choose **New skill**.
  A skill has a Name (lowercase letters, numbers and hyphens), a Description,
  a Scope, Instructions, and **Enable this skill**. Only administrators create
  Shared skills. Bundled skills (`coding`, `research`, `review`) come with
  ModelMesh and cannot be edited. To use a skill, type `/` in the message box,
  pick it, and keep typing. The skill shows as a name token in the message.
  Personal skills are available in your private sessions only. Shared skills
  are available in every session.
- **Automations** runs a prompt on a schedule. **New automation** asks for a
  Name, Instructions, a Session, a Model (a fixed model, not Auto) and
  **Repeat every (minutes)** (5 to 525,600). The session must be your own active private web session.
  Each run adds a turn to that session. A run missed while ModelMesh was down
  is made up only if ModelMesh is back within 5 minutes. **Run history** lists
  recorded runs. **Pause** and **Resume**
  control the schedule, and the trash button deletes it. Work already started
  is kept. Schedules run on the local Temporal server inside the app
  container, so they only run while ModelMesh is running.
- **Memory** lists the agent's memory files (Markdown notes it reads in later
  conversations). Pick a **Memory namespace**, then **Add memory** (Path such
  as `preferences.md`, and Memory content), edit or delete a file. A newer
  saved version is never overwritten.
- **Users** (administrators) lists accounts. **Add user** asks for Username,
  Display name, Password (at least 12 characters), Role (Member or
  Administrator) and **Enable workspace access**. **Disable** revokes sign-ins
  and stops that user's work. The last enabled administrator cannot be disabled.
- **Performance** (administrators) shows how long turns take to start: cards
  for **Startup**, **Queue wait**, **Request setup** and **Computer startup**
  (average, median, p95 and slowest), a latency trend, a startup breakdown, a
  computer readiness table (Cold start or Existing computer) and a turns table.
  Filter by model, user, status and date range, and use **Export CSV**.
  [Trace a slow turn](turn-timing.md) explains the measures.
- **Spend & usage** (administrators) shows requests, tokens and reported cost
  from the model gateway, in the **Overall**, **Users** and **Usage history**
  views, with the same filters and **Export CSV**. A request the gateway
  reported no cost for is counted as unpriced rather than as free.
- **Routing** (administrators) shows how Auto has been routing. Pick a time
  range (Last 24 hours, 7 days or 30 days). It shows **Routed messages**,
  **Median routing time**, the **Task classifier** (Laya or Off) and the
  **Benchmark scoreboard** in use. The Models table shows, for each model, how
  many messages Auto routed to it, all its turns, failed turns and the failure
  rate. Two more tables count how models were chosen and the task types.

## Behavior to know

- Sending the same request twice does not start two turns.
- A model request is tried up to three times. A shell command or file write is
  tried once. If the outcome of a command is unknown, the agent is told, and it
  does not repeat the command. See
  [Architecture](architecture.md#a-turn-on-temporal).
- ModelMesh saves the answer before it saves the computer's state. A failure to
  save the state shows a warning and keeps the answer.
- Long turns roll over into a new Temporal run before their history fills up.
  You will not see this. See [History handoff](pydantic-history-handoff.md).
- Saved conversations, files and Temporal payloads are encrypted with the key in
  `.master-key`. Back up the `modelmesh-data` volume. Without that key the data
  cannot be read.
