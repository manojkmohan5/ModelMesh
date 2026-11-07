# Trace a slow turn

Export one turn's recorded timing as an HTML timeline, CSV, and Chrome/Perfetto
trace. The exporter reads Temporal history without executing workflows, starting
a worker, decrypting payloads, or calling a model. It changes nothing.

This report describes execution timing. The app's separate **Get debug trace**
export (see [Debug traces](debug-traces.md)) describes conversation steps and tool
inputs and results. It does not replace these Temporal and ingress measurements.

For an overview of many turns, use **Settings → Performance** and its
**Export CSV** instead. This page is for taking one slow turn apart.

## Capture and inspect

From the repository root, use the project's installed Python environment
(`uv sync`) and a connection to the Temporal server that holds the turn. Get
the **turn ID** from the session's `/api/sessions/{session_id}` response or the
turns table in **Settings → Performance**. A session can contain several turns;
its URL alone is not a turn ID.

The connection uses the `TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE` environment
variables, which must both be set, and optionally `TEMPORAL_TLS` and
`TEMPORAL_API_KEY`. The script does not load `.env` files.

The Temporal server inside the `app` container (`modelmesh dev`) listens on a
port the operating system picks, on the container's loopback, and the compose
file does not publish it. So `--turn-id` works only against a Temporal server
you run yourself with `modelmesh serve` and `modelmesh worker`. For the compose
setup, use Settings → Performance.

```sh
# Replace this example UUID with the slow turn's ID.
TURN_ID=11111111-1111-4111-8111-111111111111
uv run python scripts/trace_turn.py --turn-id "$TURN_ID" \
  --output ".data/turn-traces/$TURN_ID"
```

Open the generated `index.html`. Start with **Activities and evidence**, then
select **Workflow tasks** to inspect failures/replays or **Scheduled waits** to
inspect scheduling delays. Click an operation for its timestamps, task queue,
attempt, and Temporal event IDs. Download `trace.json` for a local trace viewer.
Do not upload traces to a third-party service without authorization.

The directory also contains `history.json`, a projection with no activity inputs,
results, signals' payloads, credentials, or message text. Regenerate a report
offline with Python's standard library:

```sh
python3 scripts/trace_turn.py \
  --history ".data/turn-traces/$TURN_ID/history.json" \
  --output ".data/turn-traces/$TURN_ID-offline"
```

Successful output reports a row count and the HTML path. A connection failure
requires correcting the Temporal endpoint, namespace, TLS setting, or access.
An unknown workflow usually means the session ID was supplied instead of a turn
ID, the wrong namespace was selected, or history retention has expired.

## Add web and gateway boundaries

Temporal alone starts after admission and does not capture final browser paint.
Join evidence by **turn ID** and **inference attempt ID**. A timestamp near a
turn is not sufficient correlation.

| Evidence | What it measures | Limit |
| --- | --- | --- |
| `performance.v1.received` | HTTP middleware entry, or native admission for turns that do not come from a web request (automations, goal continuations) | Time before admission is excluded |
| `performance.v1.worker` | Entry to the successful preparation activity | Not the beginning of all dispatcher work |
| `performance.v1.dispatch` | The app's gateway client receiving the request, and the first foreground outbound request | The model's first token is not recorded here |
| `performance.v1.computer` | Computer request and readiness | Can overlap preparation; do not add twice |
| `modelmesh_inference_usage` | Each HTTP attempt and response-stream completion | Later background requests can reuse the turn ID; match the dispatch attempt |
| `modelmesh_turns.finished_at` | Backend answer completion boundary | Not browser paint or workflow cleanup completion |

Use read-only queries on `modelmesh.db` for database evidence, and select only
the metadata you need. Do not save message bodies or credentials in reports. The
exporter never queries or decrypts the database itself.

Save known boundaries in a small JSON file. For example, this **synthetic** data
adds a server receipt and an outbound interval:

```json
{
  "label": "Example web turn",
  "origin": "2026-01-01T12:00:00Z",
  "summary": ["Synthetic example; replace with recorded evidence."],
  "milestones": [
    {"name": "API received", "at": "2026-01-01T12:00:00Z", "source": "performance.v1.received"}
  ],
  "intervals": [
    {
      "name": "Outbound request through stream completion",
      "start": "2026-01-01T12:00:03Z",
      "end": "2026-01-01T12:00:05Z",
      "source": "Matched dispatch and inference ledger attempt"
    }
  ]
}
```

Pass its path as `--evidence .data/turn-evidence.json` with either capture command.
`--origin` can override t=0. Timestamps must include a timezone. Optional milestone
`uncertainty_ms` and `note` fields retain qualifications; an uncertain timestamp
must not be presented as exact. Reports contain identifiers and supplied notes,
so keep them in an untracked directory such as `.data` and review them before sharing.

Optional `targets` maps an exact operation name to `label`, `basis`, `change`,
`owner`, and `url` strings, displayed on hover and in row details. Distinguish a
measured floor from a proposed target. A local microbenchmark or avoided retry
does not establish a production end-to-end minimum.

## Interpret the timeline

Start with nonoverlapping boundaries: receipt → outbound request → stream
completion → backend answer completion → client delivery. Inspect activity spans
inside those intervals to identify where work occurred. Activities, workflow
tasks, computer readiness, and application intervals can overlap.

The exporter pairs events by IDs and then orders them by timestamp. Temporal can
record an activity start later in history even though its timestamp precedes
other events. Schedule-to-start for a retried activity can include prior attempts
and retry backoff; it is not necessarily pure queue wait. Missing starts or ends
remain unknown. A failed workflow task followed by replay is not proof of a
second model call; check the inference ledger.

Durations are wall time, including network, persistence, serialization, and
authorization. They are not CPU profiles. Millisecond display does not establish
clock synchronization or reveal suboperations that were never instrumented.
The UI's elapsed timer also differs from end-to-end delivery: it uses turn
creation and completion, while workflow/computer cleanup may continue afterward.

For finer future measurements, add correlated spans at the missing boundaries:

- Browser submit, HTTP acknowledgment, first stream receipt, and final paint.
- Request authentication, session lookup and native admission.
- Skill expansion, history loading and workflow-side preparation.
- Model transport connect/headers, first stream byte, first content token, and
  stream completion, preserving provider request IDs.
- Answer persistence and the browser's receipt of the final event.

Use monotonic clocks for durations within a process, wall timestamps for cross
service correlation, and record attempt IDs and outcome on both success and
failure. Preserve context, authorization, and required work while measuring.
Treat separate revisions or model selections as separate incidents, not
equivalent performance benchmarks.
