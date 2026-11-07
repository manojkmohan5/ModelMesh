# Runtime performance

For a slow request, [export its recorded turn timeline](turn-timing.md) to compare
admission, setup, inference and persistence. **Settings → Performance** shows the
same timings across all turns. The exporter keeps overlapping work and marks
missing evidence rather than inventing durations.

## Activity queues

The `app` container runs the API, the dispatcher and the Temporal worker in one
process. Inside that process, the worker polls four task queues:

| Queue | Runs |
|---|---|
| `modelmesh-v1` | Workflow tasks, and activities from older histories |
| `modelmesh-v1-model` | Model requests to the gateway |
| `modelmesh-v1-tools` | Tool calls, including shell commands in the computer |
| `modelmesh-v1-control` | Preparing a turn, checking authority, saving results, Stop |

`TEMPORAL_TASK_QUEUE` changes the base name. The split keeps a long computer
command from using the capacity that another model request, an authority check,
a result save or a Stop needs. Workflows that started before the split keep
their original queue. They are recorded with a Temporal patch marker.

## Computer startup

A turn starts without a computer. It loads the packaged skills from a catalog
read before the agent is built, and it waits to create or reach the
conversation's Docker container until a tool needs it, so a text-only turn does
not start one. The Coder capability's repository context is loaded before the
first computer action runs, so a parallel batch cannot write before the agent
has read it. A conversation's first computer action starts its container
(`docker run`). Later turns reuse the container, starting it again if Docker
stopped it.

## Capacity settings

| Setting | Default | Effect |
|---|---|---|
| `TEMPORAL_WORKER_ACTIVITIES` | 120 | Activity slots in each polled queue |
| `TEMPORAL_WORKFLOW_CACHE_SIZE` | 200 | Workflows the worker keeps in memory. 0 turns the cache off |
| `TEMPORAL_DISPATCH_BATCH_SIZE` | 200 | Most turns the dispatcher reserves in one pass |
| `TEMPORAL_DISPATCH_CONCURRENCY` | 10 | Workflow-start requests in flight at once |
| `MODELMESH_WORKER_SHUTDOWN_SECONDS` | 0 | How long a stopping worker waits for running activities (0 to 240) |
| `MODELMESH_MODEL_TIMEOUT` | 300 | Seconds allowed for one model request (10 to 900) |

Set them in `modelmesh.env`. The dispatcher polls the database every 0.5 seconds. It skips the
wait while a backlog remains.

## Checks

These tests in `native/tests` cover capacity, queue isolation and startup:
`test_dispatch_capacity.py`, `test_temporal_capacity.py`,
`test_bundled_skills_startup.py`, `test_computer_startup.py`,
`test_worker_pool_shutdown.py` and `test_live_stream.py`. They check that a
saturated tool queue does not block model or control activities, and that
histories replay. They are supporting tests. They do not measure real model
latency.
