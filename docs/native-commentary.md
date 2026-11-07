# Native progress and final answers

ModelMesh uses Pydantic AI's `TextPart` events and Responses `phase` metadata for
commentary and final answers. The agent still uses the SDK model/tool loop,
Workflow Streams, retry budget, and conversation serialization.

The `Commentary` capability uses the public `after_model_request` hook to
reject a commentary-only closeout through `ModelRetry`. It uses
`after_output_process` to exclude commentary from the final plain-text output
when both phases appear in one response. The original model messages remain
unchanged in native history. Unphased providers retain their existing text/tool
behavior. The Temporal patch marker preserves recorded completion decisions
when replaying workflows that started before this change.

Public root commentary is saved through a native `durable_operation` into the
existing encrypted application event table. Its turn/step/part identity makes
retries idempotent. This small presentation record survives model-context
compaction without reading every historical snapshot. Older turns fall back to
the commentary still present in their latest native snapshot.

## Web

While running, a turn shows elapsed work time and its latest commentary.
Expanding the work section shows earlier updates. When the turn finishes, the
section collapses and the independently saved final answer stays visible.
Expanding a completed turn loads its saved progress on demand.

The progress endpoint enforces the same requester-only boundary as live working
activity. It excludes child-agent text and thinking parts, and redacts configured
gateway secrets. Shared final answers do not confer
access to another requester's working transcript.

## Verification

Run these in the app image as described in [Verification](verification.md):

```sh
python -m pytest native/tests/test_commentary.py native/tests/test_live_events.py native/tests/test_live_stream.py
```

and on the host:

```sh
npm test
npm run typecheck
npm run build
npm run test:ui
```

Browser fixtures are regression tests, not evidence of live model behavior.

To check the real thing, start ModelMesh and ask the agent to create and complete
two planning checklist items, with a brief progress message before the tool
calls. While it runs, the commentary should be expanded. When it finishes, the
work section should fold above the final answer. Reload and expand it to see the
saved progress. No computer is needed for this planning-only scenario.
