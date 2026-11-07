# Verification

Run commands from the repository root. The [README](../README.md#develop) has the
short version. This page adds what each check covers and where it stops.

## Python tests

The tests need the app's dependencies. The simplest way is to run them in the
app image with this checkout mounted, so you need no local Python:

```sh
docker run --rm --memory=2g -v "$PWD:/work" -w /work -e PYTHONPATH=/work/native/src:/work \
  --entrypoint sh modelmesh -c "uv pip install --python /app/.venv/bin/python -q pytest pytest-asyncio \
  && /app/.venv/bin/python -m pytest native/tests tests -q"
```

On Windows Git Bash, prefix the command with `MSYS_NO_PATHCONV=1` and use
`$(pwd -W)` in place of `$PWD`. Run one container at a time: the suites are
heavy, and Docker Desktop can run out of memory.

With a local environment (`uv sync --frozen`), `uv run pytest native/tests tests`
runs the same suites.

- `native/tests` uses the real Pydantic AI SDK, Harness and a local Temporal
  server with controlled models. It covers turns, replay, Stop, delegation,
  swarms, goals, settings, routing, storage and encryption, Docker computer
  helpers, and API contracts. Temporal's test server downloads on first use,
  and a full run takes several minutes. Do not edit runtime code during a run.
- `tests/test_docker_start.py` covers the container's start script and health
  check. `tests/test_trace_turn.py` covers `scripts/trace_turn.py`.
- `native/tests/test_package_build.py` builds the package and needs npm, which
  the app image does not have. It fails there. Run it on a host with Node.js.
- Tests that use the model gateway, Laya or a real Docker computer use test
  doubles. They do not prove behavior against real Lumen models. To check the
  real thing, start ModelMesh and send a message (see below).

## Lint, types and build

```sh
uv run ruff check native/src native/tests tests benchmarks laya
uv run --directory native pyright   # configured in native/pyproject.toml
npm ci
npm run typecheck
npm test                  # unit tests in tests/test_native_*.cjs
npm run build             # writes native/src/modelmesh/static
```

The generated API client in `native/web/generated` must match the API. After
changing a route or payload, run `npm run api:generate`, and use
`npm run api:check` to confirm nothing differs. See
`.agents/skills/e2e-typesafety/SKILL.md`.

`native/src/modelmesh/static` is a build output and is ignored by Git. The
Dockerfile builds it.

## Browser tests

```sh
npx playwright install chromium
npm run test:ui
```

`tests/browser/native_*.cjs` serve the real React application against
synthetic API responses and drive it with Chromium. They cover keyboard use,
panels, goals, swarms, attachments, appearance and Settings. They show that
the UI works with the API shapes, not that a live model or computer works.

For UI changes, also open the affected pages in a browser, in light and dark
themes at 1440, 768 and 320 pixels wide. See
`.agents/skills/modelmesh-ui/SKILL.md` and [UI components](shadcn-ui.md).

## Check a running ModelMesh

After `docker compose up -d` (see the README), confirm:

1. `docker compose ps` shows `app`, `lumen-gateway` and `laya` running, and
   `docker compose logs app` shows no errors.
2. You can sign in at <http://127.0.0.1:8787>.
3. A message with **Auto (smart routing)** selected shows the **Routed to**
   pop-up. "draw a cat" goes to the build leader and saves `cat.svg` in the
   Files panel.
4. A message with a fixed model runs on that model.
5. After `docker compose stop` and `docker compose start`, earlier conversations
   and files still open.

Routing and the benchmark scoreboard have their own checks in
[benchmarks/README.md](../benchmarks/README.md).

## Limits

Local tests do not certify every model, every Lumen outage, or Docker behavior
on every host. If an effect's outcome is unknown after a crash, ModelMesh
keeps the receipt and does not repeat the action. This is conservative handling
of uncertainty. It is not exactly-once execution.
