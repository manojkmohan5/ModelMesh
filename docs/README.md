# Documentation

[Set up and run ModelMesh](../README.md)

## Use and understand

| Guide | Purpose |
| --- | --- |
| [Using ModelMesh](usage.md) | Chat, the model picker and Auto routing, files and the computer, goals, subagents, swarms, and every Settings page |
| [Architecture](architecture.md) | The containers, how a turn runs on Temporal, the data volume, and Auto routing |

## Features

| Guide | Purpose |
| --- | --- |
| [Automations](pydantic-automations.md) | Interval schedules that send a prompt to a conversation |
| [Subagent chats](subagent-chats.md) | Subagents as full conversations, and messages between agents |
| [Swarms](native-swarms.md) | Teams of worker conversations on one goal, started through the API |
| [Working updates](work-progress.md) | The progress feed shown while a task runs |
| [Progress and final answers](native-commentary.md) | How commentary and final answers are separated |
| [Debug traces](debug-traces.md) | Exporting an ATIF trace of a session |

## Runtime and performance

| Guide | Purpose |
| --- | --- |
| [Runtime performance](pydantic-performance.md) | Activity queues, capacity settings and computer startup |
| [Trace a slow turn](turn-timing.md) | Exporting one turn's timeline |
| [History handoff](pydantic-history-handoff.md) | How long turns continue in a new Temporal execution |
| [Smaller Temporal histories](pydantic-history-efficiency.md) | Payload compression and fewer stream events |

## Development

| Guide | Purpose |
| --- | --- |
| [UI components](shadcn-ui.md) | Shared controls, theming, the frontend build and browser checks |
| [Verification](verification.md) | Tests, lint, build, and how to check a running ModelMesh |
| [Runtime sources](../native/README.md) | What is in `native/` |

## Routing evidence

| Guide | Purpose |
| --- | --- |
| [Model benchmarks](benchmarks/MODEL-BENCHMARKS.md) | Results of the benchmark that builds the Auto scoreboard |
| [Model profiles](benchmarks/model-profiles.md) | What each Lumen model is for, from its developers |
| [Benchmark README](../benchmarks/README.md) | How to run the benchmark and write a new scoreboard |

## Plans

[Smart routing v2](plans/smart-routing-v2.md) is the routing design.
[ModelMesh cleanup](plans/modelmesh-cleanup.md) logs what was removed from the
Moyai code that ModelMesh started from. ModelMesh began as a copy of Moyai, a
hosted multi-tenant product. Plans describe the state at their time of writing.
