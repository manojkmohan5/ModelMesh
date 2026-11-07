# Architecture

ModelMesh runs on one machine. It builds on Pydantic AI 2.55.0, Pydantic AI
Harness 0.55.0 and Temporal (Python SDK 1.34.0). The versions are pinned in
`uv.lock`. Pydantic AI and Harness supply the agent loop, tools, planning, memory,
skills and compaction. The ModelMesh code in `native/src/modelmesh` supplies
sign-in, saved conversations, Auto routing, the Docker computer, files and the
settings pages.

## Containers

`compose.yaml` defines three services. The app also starts one more container
per conversation.

```
browser ── 127.0.0.1:8787 ──> app ──> lumen-gateway (LiteLLM) ──> NCSA Lumen models
                               │  │
                               │  └──> laya (task-type classifier, no published port)
                               └──> Docker socket ──> modelmesh-ws-<id> (one computer per conversation)
```

| Container | What it runs | State |
|---|---|---|
| `app` | `modelmesh dev`: the FastAPI API, the web UI, a local Temporal server, the Temporal worker and the dispatcher, all in one process | `modelmesh-data` volume, mounted at `/data` |
| `lumen-gateway` | LiteLLM with `lumen-gateway/config.yaml`. It turns the app's Responses API calls into Lumen chat completions | none |
| `laya` | `laya/server.py`, the classifier used by Auto routing | `laya-models` volume |
| `modelmesh-ws-<id>` | A conversation's computer, built from `workspace.Dockerfile` | the container's own filesystem |

The app uses `network_mode: "service:lumen-gateway"`. It shares the gateway's
network, so it reaches the gateway at `http://127.0.0.1:4000/v1` and the
gateway publishes the app's port 8787 on `127.0.0.1` only. The app reaches Laya
at `http://laya:8000`. Model credentials (`LUMEN_API_KEY`) exist only in the
gateway. A computer never receives them.

The app container starts as root, hands `/data` to UID 10001 and drops to that
user (`docker_start.py`). It keeps the Docker socket's group so it can run
`docker`. The socket is root-equivalent on the Docker host, so ModelMesh is for
local use only.

## Request flow

1. The browser sends a message to `POST /api/sessions/{id}/messages`. The API
   checks the session cookie, the CSRF token and who may see the session.
2. If the model is `auto`, `Database.resolve_auto` picks a real model before
   anything is saved (see [Auto routing](#auto-routing)).
3. The API saves the message and a turn row in `modelmesh.db`, with a request
   key. A repeated request key does not create a second turn.
4. The dispatcher, an async loop in the app process, polls the database every
   0.5 seconds. It starts a Temporal workflow for each queued turn. The
   workflow ID is stored on the turn, so a lost acknowledgment is reconciled
   without starting a second run. The dispatcher also forwards steering
   messages, Stop requests, goal continuations and swarm work.
5. The worker, in the same process, runs the workflow and its activities. The
   agent calls the gateway for each model request, and calls the Docker
   computer for shell and file work.
6. The browser follows the turn with server-sent events from
   `GET /api/sessions/{id}/events`, and reads saved state from the API.
   A reconcile loop settles turns whose workflow ended while the app could not
   record it.

The API never runs the agent. Only Temporal runs it.

## A turn on Temporal

Each turn is one workflow, `ModelMeshPydanticTurnV1`, on the task queue
`modelmesh-v1` (`TEMPORAL_TASK_QUEUE`). Activities run on three further queues,
`modelmesh-v1-model`, `modelmesh-v1-tools` and `modelmesh-v1-control`, so a long
shell command cannot starve model requests or Stop.

1. `modelmesh_prepare_turn_v1` rechecks that the user may still run the turn,
   loads the saved conversation and images, and expands any skills the message
   selected.
2. The workflow runs the agent with Pydantic AI's public `agent.iter` loop.
   There is no second agent loop. Model requests are activities that call the
   gateway, tried up to 3 times with a 300 second limit each
   (`MODELMESH_MODEL_TIMEOUT`). Tool calls are activities too. Shell commands
   and file writes get one attempt. Read-only tools get up to 3.
3. Every tool call goes through `ToolCaller` (`tools/tool_caller.py`). It
   retries safe reads, passes cancellation and lost-workspace errors to the
   workflow, and returns other errors to the model as a failed tool call.
4. Before each model request and tool call, the app checks that the turn is
   still allowed to run. A user who was disabled or a turn that was stopped
   cannot continue.
5. If a command might have run but its result is unknown, ModelMesh saves a
   receipt. The agent is told the attempt failed, and it cannot retry the same
   effect.
6. When the agent finishes, `modelmesh_save_answer_v1` saves the answer, then
   `modelmesh_save_computer_v1` records the computer, then
   `modelmesh_settle_answer_v1` finishes. A failure to save the computer shows a
   warning and keeps the answer. A question for the user ends the run
   in a waiting state, and the answer resumes the same conversation.
7. When the workflow history reaches 8 MiB or 8,000 events, the workflow saves
   a checkpoint and continues as new, with the same turn and workflow ID.
   See [History handoff](pydantic-history-handoff.md).
8. If the workflow is cancelled or fails, activities mark the turn stopped or
   failed. For a failure, ModelMesh first saves the settled conversation so a
   later message can continue from it, unless an earlier action's outcome is
   unknown.

Stop saves the intent first. The dispatcher then cancels the workflow. A
command that already ran is not undone.

Other durable work uses the same Temporal server:

- Automations are Temporal Schedules. Each firing admits an ordinary turn
  (see [Automations](pydantic-automations.md)).
- Goals are rows in `modelmesh.db`. The dispatcher admits the next turn when
  one ends.
- Swarms and subagents are conversations of their own, each running its own
  turn workflow (see [Swarms](native-swarms.md) and
  [Subagent chats](subagent-chats.md)).

## The computer

`docker_workspace.py` creates one container per conversation the first time the
agent needs a computer. It runs `docker run -d --init --name modelmesh-ws-<16 hex>`
from the `modelmesh-workspace` image (`MODELMESH_DOCKER_IMAGE`), with working
directory `/workspace` and the labels `modelmesh.workspace=1` and
`modelmesh.key=<conversation key>`. The container name is saved with the
conversation, so later turns reuse the same container. Commands run through
`docker exec`. A stopped container (after Docker restarts, for example) is
started again with its files intact. If the container no longer exists, the turn
fails instead of starting an empty one.

The image holds Python 3.13 with uv and pytest, Node.js 22 with npm, git,
ripgrep, curl, jq and the bundled skills in `/opt/modelmesh/skills`. It has no
browser or desktop. The agent hands files to the user with `save_artifact`,
which copies a file (up to 20 MB) into the object store. ModelMesh never
removes a computer, even after its conversation is deleted.

## The data volume

`modelmesh-data` is mounted at `/data` (`DATA_DIR`). It holds:

| Path | Contents |
|---|---|
| `modelmesh.db` | The application database (SQLite, with `-wal` and `-shm` files beside it): users, conversations, turns, messages, events, goals, swarms, automations, saved skills, usage records and effect receipts. Tables start with `modelmesh_`. The app creates missing tables with SQLAlchemy `create_all` at startup. A change to an existing table's columns needs its own migration step. |
| `agent-state.db` | Harness stores in SQLite: agent memory, per-step conversation snapshots and plans |
| `temporal.db` | The local Temporal server's state: workflow histories and automation schedules |
| `objects/` | Immutable encrypted objects named by a hash: saved files, uploads, and Temporal payloads of 64 KiB or more |
| `.master-key` | A random 32-byte key, created on first start. It encrypts message text, saved conversation state, objects and Temporal payloads. Setting `ENCRYPTION_KEY` (32 or more characters) uses that key instead |

`agent-state.db` is not encrypted, so protect the volume like the data it holds.
Back up the whole volume while ModelMesh is stopped. Without `.master-key` the
encrypted data cannot be read. Passwords are stored as hashes.

An object's name is derived from its content. When an object is read, the app
recomputes the name and refuses the object if it does not match.

## Auto routing

Auto is the model id `auto`. It is offered when at least two models are
configured and the app has a gateway URL and key (`Settings.routing_enabled`).
`ModelRouter.decide` (`model_router.py`) runs these steps in order, before the
message is admitted:

1. **Rules.** A message with images uses the leader of the `vision` scoreboard.
   A message over 60,000 characters uses the leader of `long_context`.
2. **Laya.** The app posts the message (first 3,000 and last 1,000 characters) to
   `MODELMESH_LAYA_URL/classify` with `LAYA_API_KEY`, and waits up to 15
   seconds. Laya returns a task type, a confidence and a route. When the route
   is not `review`, the app looks the task type up in
   `native/src/modelmesh/routing_policy.json` and picks the top model among the
   enabled ones. If the conversation's last model scores within 0.05 of the top
   (5 points), it stays on that model, so its saved tool results still apply.
3. **LLM router.** If Laya says `review`, fails, or the type has no scoreboard,
   a model reads the message and the model notes in `model_router.py` and names
   a model. `MODELMESH_ROUTER_MODEL` sets which model (default `AGENT_MODEL`),
   and the default model backs it up.
4. **Fallback.** If every step fails, the conversation's previous model or the
   default model runs the message.

A guidance message sent while a turn is running skips routing and joins that
turn's model (`running-turn`). Routing never fails a message.

The decision (model, engine, task type, confidence, reason and benchmark
figures) is saved with the turn. The UI shows it in the routing pop-up and the
**via** tag, and **Settings → Routing** counts it.

`routing_policy.json` is written by `benchmarks/report.py`. Each task type has a
ranking of models by a combined score: quality 60%, speed 20%, cost 20%. A
conversation keeps its saved agent history only when the next turn runs on the
same model that produced it. After a switch, the new model sees the visible
transcript but not the earlier tool results.

## Code map

| File | Role |
|---|---|
| `cli.py` | `modelmesh dev` (everything in one process), `serve` (API only), `worker`, `user` |
| `api.py`, `settings_api.py`, `swarm_routes.py`, `history_routes.py` | HTTP routes. Payload types come from Pydantic through OpenAPI and the generated client in `native/web/generated` |
| `database.py`, `models.py` | Admission, sessions, turns and the SQLAlchemy models |
| `model_router.py`, `routing_policy.json` | Auto routing |
| `agent.py` | Builds the Pydantic AI agent and its capabilities |
| `workflow.py`, `activities.py`, `worker.py`, `worker_capacity.py` | The turn workflow, its activities, the dispatcher and the queues |
| `docker_workspace.py` | The Docker computer |
| `storage.py`, `security.py`, `stores.py` | Object store, encryption, Harness stores |
| `native/web`, `frontend` | The React UI and shared shadcn components. The build goes to `native/src/modelmesh/static`, which the app serves |

`modelmesh serve` and `modelmesh worker` can run the API and the worker as
separate processes against a Temporal server you provide (`TEMPORAL_ADDRESS`,
`TEMPORAL_NAMESPACE`, `TEMPORAL_TLS`, `TEMPORAL_API_KEY`). ModelMesh does not use
that mode itself. `compose.yaml` runs `modelmesh dev`.
