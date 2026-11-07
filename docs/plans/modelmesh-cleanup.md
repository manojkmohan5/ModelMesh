# ModelMesh cleanup: remove unused Moyai code and rename to ModelMesh

Started 2026-11-06. This is the living progress log: each step's status, what changed, and how it
was checked. It is updated as the work goes.

## Goal

ModelMesh began as a copy of Moyai, a hosted AI-agent product. ModelMesh is a local chat app and AI
gateway: NCSA Lumen models behind a LiteLLM gateway, with Auto routing (Laya plus the benchmark
scoreboard). The goal is to remove every Moyai feature ModelMesh does not use, then rename what is
left from Moyai to ModelMesh, so the app is ModelMesh throughout.

What ModelMesh uses, and so keeps:
- The app in standalone mode on SQLite: chat, Auto routing, goals, automations, swarms, subagents,
  files, settings, and the routing dashboard.
- The local Docker sandbox (`SANDBOX_PROVIDER=docker`).
- The LiteLLM gateway (`lumen-gateway/`) and Laya (`laya/`).
- The web UI (`native/web`) and its shadcn components (`frontend/`).
- The benchmark (`benchmarks/`, `docs/benchmarks/`).

## Ground rules

- **No git commits** during the cleanup. On 2026-11-07, after it was done, the owner asked for the
  result to be pushed to their repository (see the log, 11:00). The backup is a file
  archive of every tracked and untracked file, taken before step 1. Env files are not in it.
  - `C:\Users\MRWHIT~1\AppData\Local\Temp\claude\e476bb2ed2fc8242\cdbe0e46-ab80-43c4-bf8d-c703b489e78b\scratchpad\modelmesh-before-cleanup.tgz`
  - That is a temporary folder, so copy the archive somewhere safe if you want to keep it.
- API keys stay in the git-ignored env files only (`.env.lumen`, `.env.lumen.exhausted`,
  `.env.laya`, `moyai.env`). They are never printed or copied.
- The sandboxes of the owner's own conversations stay: the Porsche advert page, "Hey", "hello.py",
  and six "draw a cat" tests. Removing a sandbox deletes that conversation's files.
- Each stage is checked before the next starts: lint, type check, UI build, test suites, and the
  rebuilt app in the browser.

## Steps

| # | Step | Status | Notes |
|---|---|---|---|
| 1a | Hosted leftovers: Helm charts, AWS deploy, evals, CI, release and Render scripts, hosted docs | Done | See the log, 13:15 |
| 1b | Stage 2: Slack, GitHub (and the PR panel), Linear, Notion, `agentchat`, the Rust build stage | Done | Tests compared with the backup; see the log, 16:30 |
| 2 | Rebuild the app; ship the routing fix; live-check "draw a cat" on Auto; check the UI | Done | Done together with step 3; see the log, 17:50 |
| 3 | Hosted product mode: `product_*` code, Google and Okta sign-in, billing, drain and fleet, installer, Kubernetes, the old `app/` folder, their UI | Done | `product_routes.py` became `history_routes.py`; see the log, 17:50 |
| 4 | Lambda and Modal sandboxes; remote desktop and browser stack | Done | Docker is the only sandbox; see the log, 19:06 |
| 5 | Dead tables and models, Postgres support, schema-migration machinery | Done | SQLite only; 66 tables became 31; see the log, 20:25 |
| 5b | Secrets settings page (nothing reads saved secrets), the dead inbound-event code, and the S3 storage backend | Done | Found in step 5; no secrets are saved; see the log, 01:35 |
| 6 | Rename Moyai to ModelMesh: package, command, settings, headers, storage keys, database tables, containers, images, env file, compose project | Done | Data copied into a new volume; the old one is the backup; see the log, 01:50 |
| 7 | Final verification, audit, new README | Done | Audit fixes, docs, README; 885 Python and 276 browser tests pass; see the log, 10:36 |

### Step 6 risks found while planning

Some "moyai" strings are stored inside existing data, so renaming them in code breaks that data.
Each needs its own handling:

- `security.py` signs sign-in cookies with the salt `moyai-session-v1`. Renaming it signs everyone
  out once. That is acceptable.
- `security.py` derives object IDs with `moyai-object-identity-v1`. Renaming it changes the IDs of
  new content only. Check every `object_id` caller before renaming.
- `storage.py` marks encrypted Temporal payloads `moyai/encrypted-*-v1`. Temporal history already
  stored with those marks becomes unreadable after a rename. Either clear Temporal's state while
  no turn runs, or keep reading the old marks. Checked at 16:45: the database has 0 automations,
  0 goals and 0 swarms, so Temporal holds no schedules or long-running work, only finished turns.
  Starting Temporal fresh is safe; the old `temporal.db` stays in the backup volume.
- Docker labels cannot change on an existing container. The app finds a sandbox by the container
  name saved with its conversation, and uses the labels only for manual cleanup. The owner's 9
  sandboxes can be renamed (`docker rename`) with their saved names updated. Recreating them from
  a `docker commit` copy also updates the labels.

### Found while planning steps 5 to 7

- Step 5: the models define 66 tables; the live database has 52. Dead after steps 2 to 4: the
  `product_*` model files, `billing_storage_models.py`, `pr_publications.py`, and in `models.py` the
  Slack tables, `Connection` and `ProductTurnAuthority`. `NewMessage.connections` is a dead request
  field. Postgres lives in `postgres.py`, `stores.py`, `snapshot_store.py`, `harness_schema.py`,
  `database.py` and four settings (`postgres_url`, `database_schema`, `native_schema_mode`,
  `database_pool_size`).
- Step 6: the old Moyai brand assets are unused. Nothing imports `frontend/moyai-sculpture.ts`
  (a 3D Moyai statue) or `frontend/moyai-original-surface.ts`, and nothing outside them uses
  `native/web/public/moyai.glb` (6.3 MB), `native/web/assets/moyai-logo.png` or
  `moyai-meteor-poster.webp`. The server still serves the statue at `/static/models/moyai.glb`.
  Remove them all, the route, and the `three` and `@types/three` packages, which only those two
  modules use.
- Step 6: a git-ignored `.env` at the repository root mentions Moyai. Check its variable names
  (never its values) to see whether anything still reads it. The host `.venv` needs `uv sync`
  after the package rename so the `modelmesh` command exists.

## How each stage is checked

Commands as of step 6 (before it, the image was `moyai-local` and the compose file
`compose.lumen.yaml`, with service `moyai`):

- Python tests (the app image, with the source mounted over the installed package, one container
  at a time):
  `MSYS_NO_PATHCONV=1 docker run --rm --memory=2g -v "$(pwd -W):/work" -w /work -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work/native/src:/work --entrypoint sh modelmesh -c "uv pip install --python /app/.venv/bin/python -q pytest pytest-asyncio; /app/.venv/bin/python -m pytest native/tests tests -q -p no:cacheprovider"`
- Lint: `uv run ruff check --no-cache native/src native/tests tests benchmarks laya --select F,E9`
- UI: `npm run typecheck`, `npm run build`, `npm test`
- Rebuild and restart, only when no turn is running: `docker compose build app` then
  `docker compose up -d --no-deps app`
- Live: "draw a cat" through Auto, plus one message per task type, then a browser pass over Home,
  chat, Settings and the routing pop-up.

## Log

- **13:15 Step 1a done.**
  - Deleted: `charts/`, `deploy/`, `evals/`, `.github/`, `GLOSSARY.md`, the old `compose.yaml`,
    `render_start.py`, `build_workspace_image.py`, and 9 release, Helm, Lambda and Postgres scripts in
    `scripts/` (`trace_turn` stays).
  - Deleted 7 tests for those scripts, and 27 hosted-product docs.
  - Edited: the Render branches are gone from `docker_start.py`, `docker_healthcheck.py` and their
    test, and from the Dockerfile's `COPY` line.
  - Kept: the core runtime docs (architecture, automations, swarms, subagents, history, debug traces),
    because their code stays; step 6 renames them.
- **13:20 Stage 2 started**, run by a helper agent.
- **14:30 Routing fix ready, waiting for the rebuild in step 2.**
  - "draw a cat" on Auto had gone to `qwen3-coder-next`, which answered "I can't draw images
    directly". Four open-ended build tasks joined the benchmark (run build-1 in
    `docs/benchmarks/MODEL-BENCHMARKS.md`). Build now goes to `beam-1.0`, which drew the cat.
  - The app now waits up to 15 s for Laya, not 6 s.
- **14:30 Benchmark leftovers cleaned.** Removed the sandboxes of 46 benchmark conversations already
  in the trash.
- **14:42 Docker Desktop restarted** (not caused by this work). All containers came back. The owner's
  9 sandboxes stopped and restart when their conversations are next used. A `litellm_db` container
  from a separate `litellm` project also started; it is not part of ModelMesh and was left alone.
- **15:20 Stage 2 edits done.** Deleted 107 files, edited about 110.
  - Removed:
    - Slack: all Slack modules, the Personal Slack settings page, Slack message formatting, and
      `failure_report.py`.
    - GitHub: the GitHub app, branch policy, the pull-request panel and PR analytics.
    - Connections: the Connections settings and connection pickers. Every connection kind was
      GitHub, Linear, Notion or Slack, so the whole feature went.
  - Dependencies: dropped `agentchat` and `slack-sdk`, and the harness extras `github`, `linear`,
    `notion` and `slack`. `uv.lock` was refreshed, and `uv lock --check` passes.
  - Dockerfile: the Rust build stage and `build-essential` are gone, because every locked package
    ships a Linux wheel.
  - Kept for now:
    - All database models and tables, which step 5 removes.
    - The internal grant fields that Temporal payloads carry. They are now always empty.
  - Checked:
    - Type check and UI build pass. UI unit tests: 167 passed, 0 failed.
    - The app imports, and the OpenAPI dump and client regeneration work.
    - Lint: the same 17 F811 (name defined twice) errors as the pre-cleanup backup, so they are not
      new; step 7 fixes them.
  - Not yet checked: the full Python test suites.
- **14:41 and 14:52 Docker Desktop crashed** while stage 2's test containers ran. The host had
  0.35 GB of RAM free, mostly because a process from another project used 6.1 GB. That process
  ended. Docker Desktop was restarted at 15:20. From now on, test containers run one at a time with
  a 2 GB memory cap.
- **16:30 Stage 2 tests compared with the backup.** Both trees ran in the app image, one container
  at a time, capped at 2 GB.
  - `tests/`: the same single failure in both (`test_online_migrations.py`, an old-schema check that
    step 5 removes). 430 passed in the backup.
  - `native/tests`: the backup has 102 failures of its own. Most are release-package checks that
    need a release image. Stage 2 adds 72 failures, and none is a fault in code ModelMesh keeps:
    - Tests of files deleted in step 1a: `render_start`, `build_workspace_image`, the `deploy/`
      installer (`test_docker_helper`, `test_packaged_startup`, `test_deployment_*`,
      `test_modal_runtime`).
    - Product-mode tests that still import removed Slack helpers or fixtures
      (`test_product_history`, `test_product_identity`, `test_product_automation_*`,
      `test_stream_product_authority`, `test_delegation_postgres`). Step 3 or 5 removes them.
    - Three tests that still expect Slack: `test_delegation_authority` (2) and
      `test_bundled_skills_startup` (the coding skill no longer has GitHub publishing). These are
      fixed with step 3.
  - The removed `prefetch_session_audiences` only ever ran Slack audience lookups in product mode,
    so removing it changes nothing in standalone mode.
- **16:30 Step 2 merged into step 3.** The host had under 1 GB of RAM free, so the app is rebuilt
  once, after step 3 is checked, instead of twice.
- **17:50 Steps 2 and 3 done.**
  - Step 3, by a helper agent:
    - Removed 58 backend modules (all `product_*` services, billing, fleet, worker adoption,
      deployment, installer, Kubernetes, maintenance), the `app/` folder, 71 test files for them, and
      the product, sign-in-provider and billing parts of the UI. About 61,000 lines in all.
    - Renamed `product_routes.py` to `history_routes.py`; it now lists native conversations only.
    - Moved the two helpers the kept code still used: the public-URL helpers to `public_path.py`,
      and the inference-usage drain into `inference_usage.py`.
    - The app starts with `moyai dev` by default. The Dockerfile no longer copies `app/`.
    - The regenerated API client lost 45 product paths and 116 schemas.
    - Kept for step 5: the database models and the schema-version modules.
  - Fixed after review:
    - The three tests that still expected Slack or GitHub publishing (`test_delegation_authority`,
      `test_bundled_skills_startup`).
    - Removed the `pr-delivery` and `support-check` skills. Both open GitHub pull requests on a
      connected repository, which ModelMesh no longer has.
    - Removed pull-request guidance from the agent instructions (`native/instructions.md`).
  - Checked:
    - Lint: 0 errors. The 17 old F811 errors were all in deleted `app/` tests.
    - Type check passes; UI unit tests 134 passed.
    - The changed tests: 52 passed.
    - The helper ran the Python suites: 16 failures, 10 of them its own, which it fixed. Of the
      other 6, three were the Slack and GitHub tests fixed above, two are `test_modal_runtime`
      (step 4), and one is `test_package_build`, which also fails in the backup because the
      image has no npm.
  - Rebuilt the image (64 s, 1.29 GB, was 1.34 GB) and recreated the app. It is healthy, and the
    deployed `routing_policy.json` matches the repository's build-1 version.
  - Live checks:
    - "draw a cat" on Auto went to Laya, then `build` (confidence 0.999997), then `beam-1.0`. The
      turn finished in 62 s and saved `cat.svg`.
    - Six more Auto messages each went to the scoreboard leader for their task: chat, code, math,
      writing, summary, build. Laya answered in 1.1 to 1.4 s, and the saved routing matched.
    - The browser check covered Home and all 10 Settings pages at 1440, 768 and 320 px, in both
      the dark and light themes. It found no page errors, console errors or failed API calls, and
      no visible "Moyai", "Slack", "GitHub", "Modal" or "Lambda".
    - Test conversations went to the trash and their sandboxes were removed.
  - Left for later steps:
    - The agent instructions still describe a Modal desktop and browser. The Docker sandbox has
      neither, so "draw a cat" offered to "open it in the browser". Step 4 rewrites this.
    - PyJWT and the empty `product` extra are unused (step 4 tidies the dependencies).
    - The browser test `tests/browser/native_product_shell.cjs` still assumes product mode, and
      `docs/usage.md` and `docs/architecture.md` still describe it (step 7).
- **18:40 Step 6 prepared and tested on copies** while the step 4 helper works.
  - A rename script (in the session scratchpad, `rename_to_modelmesh.py`) renames paths and file
    contents. It skips env files, lock files (rebuilt with uv and npm instead), the LICENSE, build
    output, benchmark result data and this log. On a copy of the step 3 tree it edited 268 files
    and moved 7 paths. Lint passed, the package imported, and 115 sampled tests ran: 76 passed,
    39 skipped.
  - A scan of the stored data, decrypting sealed values in memory and printing counts only, found
    which "moyai" strings are saved inside the data and so must be handled before renaming:
    - Must stay byte-for-byte: `moyai-object-identity-v1` in `security.py`. Each saved file's
      ID is derived from it, and `storage.get` rejects a file whose ID no longer matches, so
      renaming it would make every saved file and upload unreadable.
    - Migrated: the message metadata key `moyai_public_turn`, which the app reads back from saved
      history (114 history snapshots and 3 sealed values), and the sandbox pointers of 120
      conversations (`moyai-ws-*`).
    - Left as history: the word "Moyai" inside past conversations, old Temporal names in old
      activity records, and old `moyai-turn-*` workflow IDs. The agent framework looks history up
      by run ID, never by agent name, so the old agent name `moyai` in saved runs is harmless.
  - A one-time data migration (`migrate_data.py`) copies the data into a fresh ModelMesh volume:
    the master key, the object store, `agent-state.db`, and every row of every table into the new
    `modelmesh_*` tables built from the app's own models. Temporal starts fresh. A test on a copy
    of the live data copied every table with matching row counts (180 conversations, 194 turns,
    314 messages), left behind 11 dead tables (all empty), and passed SQLite's integrity and
    foreign-key checks. The old `moyai-data` volume stays untouched as the backup.
  - The owner's 9 sandboxes get renamed with `docker rename`, which keeps everything in them.
    Two have packages installed outside `/workspace` (for example Pillow), so copying only
    `/workspace` into new containers would lose them. Their old Docker labels stay; nothing in
    the app reads those labels.
- **19:06 Step 4 done**, by a helper agent. Docker is now the only sandbox. About 9,700 lines removed.
  - Removed:
    - The Lambda and Modal sandboxes, sandbox checkpoints, and the loop that kept a remote
      computer alive.
    - The remote desktop and browser stack: the desktop stream, the browser and desktop tools,
      browser sign-in, and the guest helpers that ran inside Modal (`desktop.py`,
      `browser_guest.py`, `browser_driver.py`, `ocu_guest.py`, `lambda_guest/`).
    - The `desktop-apps` skill.
    - In the UI: the Computer tab, the "Open computer" menu item and the browser sign-in card.
    - The routes `/api/sessions/{id}/computer`, `/api/sessions/{id}/browser` and the desktop
      stream WebSocket.
    - 20 test files that tested only the removed code, and 2 UI tests.
    - Settings `sandbox_provider`, the Modal and Lambda settings, and the computer lifetime
      settings. A leftover `SANDBOX_PROVIDER` in an env file is ignored.
    - Dependencies: `modal`, `playwright`, `websockets`, `PyJWT` (still installed, because `mcp`
      needs it), the empty `product` extra, `pytest-split`, and the npm package `@novnc/novnc`.
      `uv lock` dropped 20 packages.
  - A Docker turn now offers the agent 26 tools instead of 32. The six removed browser and
    desktop tools refused to run on Docker anyway.
  - The agent instructions now describe the Docker computer as it is: one container per
    conversation, files kept in `/workspace`, Python 3.13 with uv and pytest, Node.js 22, git,
    ripgrep, curl and jq, and no browser, desktop or display. The agent hands files over with
    `save_artifact` instead of offering to open a browser.
  - Fixed after review: the instructions and two tool descriptions sent the user to a
    "Workspace panel", but the UI calls that panel Files. They now say "Files panel".
  - Checked:
    - Lint clean. Type check and UI build pass; UI unit tests 126 passed.
    - Python suites: 924 passed, 129 skipped, 1 failed (`test_package_build`, the known failure:
      the image has no npm).
    - The OpenAPI dump lost 2 paths and 8 schemas, and matches a fresh dump.
    - The helper's smoke test from source showed Home, a conversation and Settings at 1440 and
      320 px with no errors.
    - The git index is unchanged.
  - The turn workflow no longer schedules the keepalive and dropped three version markers, so a
    turn running during the upgrade would fail. Before the restart: no turn was running and
    Temporal had no open workflow.
  - Rebuilt the image (1.07 GB, was 1.29 GB) and recreated the app; healthy after 18 s.
  - Live checks:
    - "draw a cat" on Auto went to Laya, then `build` (confidence 0.999997), then `beam-1.0`.
      It finished in 25 s (62 s before this step) and saved `cat.svg`. The reply pointed to the
      Files panel and no longer offered to open a browser.
    - Six more Auto messages (chat, code, math, writing, summary, translation) each went to the
      scoreboard leader for their task. Laya answered in 1.3 to 3.4 s, and the saved routing
      matched.
    - The browser check covered Home and all 10 Settings pages at 1440, 768 and 320 px in both
      themes. It found no page errors, console errors or failed API calls, and no "Moyai" in the
      markup.
    - Test conversations went to the trash and their sandbox was removed. The 14 conversations
      still listed are the owner's, all from before the cleanup.
  - Left for later:
    - `.env.example` still lists the Modal and computer-lifetime settings (step 7).
    - `README.md`, `docs/usage.md`, `docs/architecture.md`, `docs/verification.md` and
      `native/VERIFICATION.md` still describe Modal (step 7).
    - The database tables of the removed desktop (`computer_desktops`, `computer_leases`,
      `computer_operations`) and the `Session.snapshot` column (step 5).
- **19:10 Step 5 started**, run by a helper agent.
- **19:22 Accessibility scan (early part of step 7)**, run while step 5 works.
  - Method: axe-core 4.10.2 with the WCAG 2.0 and 2.1 A and AA rules, on Home, a real conversation
    (a "draw a cat" test, trashed afterwards with its sandbox) and all 10 Settings pages, in both
    themes at 1440 and 320 px. Plus a keyboard pass that tabs through Home and the conversation
    and records each stop.
  - Found and fixed:
    - The sidebar status dot and the private-conversation icon were `<span>`s with an
      `aria-label` and no role, which screen readers may ignore. The dot repeated what the
      conversation link already says ("Unread draw a cat"), so it is now hidden from screen
      readers. The icon now has `role="img"` and reads "Private conversation" or "Shared
      conversation".
    - The message box (`role="textbox"`) carried `aria-expanded`, which ARIA does not allow on a
      textbox. Removed; the skill list still links to it through `aria-controls` and
      `aria-activedescendant`.
  - Checked and fine: the conversation status in the sidebar is shown as text ("Failed", "Needs
    input", "Running") as well as by the dot's colour, and unread rows show a dot that read rows
    do not, so colour is never the only signal. The keyboard pass flagged the sidebar links in
    the light theme as having no focus outline, but their ring is drawn on the row's `::after`;
    the check now reads that too and is re-run after the next rebuild.
  - The app's Content Security Policy (`script-src 'self'`) blocked the injected scanner, as it
    should; the scan's own browser bypasses it.
  - A slip outside the repository, undone: installing the scanner from the session's temporary
    folder, which had no `package.json`, put it into the npm project in the owner's home folder
    (`C:\Users\mrwhitewolf`). Its `axe-core` entries were removed by hand, so that project lists
    only `docx` again; nothing else there changed.
- **20:25 Step 5 done**, by a helper agent. ModelMesh now runs on SQLite only and creates its
  tables with `create_all` at startup.
  - Removed:
    - Postgres support: `postgres.py`, `snapshot_store.py`, `harness_schema.py`,
      `dispatch_wakeups.py`, and the Postgres branches in `database.py`, `stores.py` and four
      other modules.
    - The schema-version machinery: `product_schema.py` and `native_online_schema.py`.
    - 35 dead tables: the 11 `product_*_models.py` files, billing, pull-request records, the
      Slack tables, `Connection`, the three remote-desktop tables and the delivery tables.
    - Two dead nullable columns: `sessions.snapshot` and `users.slack_user_id`.
    - The connected-account plumbing (grants), which was always empty after step 2, including
      the `connections` field of message, swarm, goal and automation payloads.
    - The settings `database_pool_size`, `database_schema` and `native_schema_mode`.
    - 10 test files for removed code.
    - Dependencies `asyncpg`, `psycopg`, the harness `code-mode` extra and the root
      `postgres-migration` extra. `uv lock` dropped 12 packages.
  - Kept, with reasons:
    - Six unused `connections` and `grants_key` columns. The existing database requires a value
      for them (NOT NULL without a default), so the models still fill them in.
    - `MOYAI_DATABASE_URL`, as an optional SQLite path.
  - Saved identities do not change. Conversation, memory and computer keys used to include a
    hash of the (always empty) account list; they now use that hash as a constant. A new test
    pins the old values.
  - AGENTS.md now says tables are created by `create_all` at startup and that a change to an
    existing table's columns needs its own migration step.
  - Checked:
    - Lint clean. Type check and UI build pass; UI unit tests 126 passed.
    - Python suites: 892 passed, 39 skipped, 1 failed (`test_package_build`, known). Compared
      with step 4, 122 tests are gone: Postgres tests that were always skipped here, and tests
      of the removed schema and account code.
    - Schema: fresh databases built from the step 4 and step 5 models differ only by the 35
      removed tables, their indexes and the two removed nullable columns.
    - The API lost the `connections` field from 7 schemas; a fresh dump matches.
    - The git index is unchanged.
  - Rebuilt the image (925 MB, was 1.07 GB) and recreated the app; healthy after 28 s.
  - Live checks:
    - Upgrade check: a test conversation made on the step 4 app wrote a marker file in its
      computer. After the upgrade, the same conversation read the marker back from the same
      container; no new container was created.
    - "draw a cat" on Auto went to `beam-1.0` (Laya, `build`), finished in 16 s and saved
      `cat.svg`.
    - Six more Auto messages each went to the scoreboard leader for their task; Laya answered
      in 1.1 to 1.5 s.
    - The browser check (Home and all 10 Settings pages, 3 widths, both themes) found no
      errors and no "Moyai" in the markup.
    - The accessibility scan found no WCAG 2.1 A or AA violations, so the three fixes from
      19:22 are live. The keyboard pass found a visible focus style on every Tab stop; the
      sidebar links draw theirs on `::after`, as expected.
  - Found for step 5b: Settings → Secrets saves secrets, but nothing in ModelMesh reads them
    since the tool that used them was removed. A page that stores credentials nothing uses is
    misleading, so it goes, with the inbound-event code that only the removed Slack and GitHub
    entry points used. No secrets are saved, so no data is lost.
- **20:20 Step 5b started**, by a helper agent. It stopped at about 21:00, when the account
  reached its 5-hour usage limit, with the backend part done. It was resumed at 00:30 from where
  it stopped.
- **21:00 Step 6 rehearsed** on a copy of the step 5 tree:
  - First the dead brand assets went (the 3D statue model and its two modules, the old logo and
    poster).
  - Then the rename script edited 199 files and moved 2 folders (`native/src/moyai` and the
    `moyai-ui` skill).
  - Then a hand-fix script applied 11 exact edits:
    - The package names: the app is now `modelmesh`, and the development workspace
      `modelmesh-dev`.
    - The Temporal task queue `modelmesh-v1`.
    - The Dockerfile's unused labels and build-revision lines, one of which pointed to the
      original project's GitHub repository.
    - The compose file, now `compose.yaml` with project `modelmesh`, service `app` and image
      `modelmesh`.
    - The key-swap script's service name.
    - A comment beside the protected object-identity label.
    - The sandbox module's cleanup hint, which would have deleted every computer, including
      ones in use.
  - On the result:
    - The only "moyai" left is the protected label.
    - Lint is clean, and the package imports as `modelmesh`.
    - 86 sampled tests passed.
- **00:20 The PC restarted** (not caused by this work), so Docker Desktop and every container
  stopped. Docker Desktop was started again at 00:35. The app, gateway and Laya came back on
  their own and are healthy. The owner's 9 sandboxes stay stopped until their conversations are
  next used, as before.
- **00:40 Baseline for the data move**, read-only: 14 open conversations and 100 listed in the
  trash. All 5 saved files in the open conversations open; a file only opens when its stored ID
  still matches its bytes.
- **01:35 Step 5b done.**
  - By the helper agent:
    - Removed the Secrets settings page, its four API routes, its schemas and the
      `SettingSecret` model, and the Integrations menu group, which held only Secrets.
    - Removed the inbound-event code (`inbound_events.py` and the event parts of
      `NewMessage`). Every message is now recorded as a web message, which is what every kept
      entry point already sent.
    - The API went from 61 to 59 paths and from 151 to 146 schemas.
  - By me:
    - Removed the S3 storage backend: the boto3 client and its branches in `storage.py`, and 7
      settings (`MOYAI_ARTIFACT_*`, `OBJECT_STORAGE_*`). It existed only for the hosted product;
      ModelMesh keeps files in its data volume. The lock dropped boto3, botocore, s3transfer,
      jmespath, python-dateutil and six (130 packages remain).
    - Removed `native/.test_durations`, which nothing has read since pytest-split went.
  - Checked:
    - Lint clean. Type check, build and UI unit tests pass (126 passed).
    - Python suites: 883 passed, 39 skipped, 1 failed (`test_package_build`, known). The 9
      fewer than step 5 are exactly the removed tests.
    - After the S3 change, 210 tests of storage, previews, settings, start-up and Temporal
      payloads passed with boto3 uninstalled.
    - Schema: the only difference is the `setting_secrets` table and its two indexes.
    - The git index is unchanged.
  - Rebuilt the image (876 MB, was 925 MB) and recreated the app with no turn running.
    - Healthy after 30 s.
    - 14 open conversations, and all 5 saved files open.
    - "draw a cat" on Auto went to `beam-1.0` and saved `cat.svg`.
  - Snapshot before the rename:
    `C:\Users\MRWHIT~1\AppData\Local\Temp\claude\e476bb2ed2fc8242\cdbe0e46-ab80-43c4-bf8d-c703b489e78b\scratchpad\stage5b`
    (443 files, no env files).
- **01:50 Step 6 done: the app is ModelMesh throughout.**
  - Source:
    - Removed the dead brand assets and the route that served the statue, and uninstalled
      `three` and `@types/three`. Two tests listed the statue and were updated.
    - The rename script edited 198 files and moved `native/src/moyai` to
      `native/src/modelmesh` and the `moyai-ui` skill to `modelmesh-ui`.
    - The hand-fix script applied the 11 edits rehearsed at 21:00.
    - `compose.lumen.yaml` became `compose.yaml`, with every reference updated.
    - Regenerated `uv.lock` (packages `modelmesh` and `modelmesh-dev`), `package-lock.json`
      (`modelmesh-ui`), the OpenAPI dump and client, and the UI build.
    - Synced the host `.venv`, so the `modelmesh` command works there too.
    - The only "moyai" left in the tree is the protected object-identity label, beside a
      comment that explains why. Outside the tree, the LICENSE keeps its copyright line, and
      this log and the benchmark results keep their history.
  - Env files, names only: `moyai.env` became `modelmesh.env`, and every `MOYAI_` variable is
    now `MODELMESH_`, there and in the root `.env`. No value was read or changed.
    `.env.example` is now a ModelMesh template covering all three env files.
  - Images: `modelmesh` (840 MB) and `modelmesh-workspace`.
  - Switch-over:
    1. Confirmed that no turn was running and no Temporal workflow was open.
    2. Stopped the old app.
    3. Ran the one-time copy into the new `modelmesh-data` volume. Every kept table matched
       its row count: 200 conversations, 215 turns, 1 user. The 24 tables left behind were all
       empty. It renamed 128 sandbox pointers, rewrote the `moyai_public_turn` key in 114
       history entries and 3 sealed values, and passed the integrity and foreign-key checks.
    4. Renamed the 10 sandbox containers (the owner's 9 and one test) from `moyai-ws-*` to
       `modelmesh-ws-*`.
    5. Removed the old `moyai-*` app, gateway and Laya containers and the `moyai_default`
       network, then started the `modelmesh` project: `modelmesh-app-1`,
       `modelmesh-lumen-gateway-1` and `modelmesh-laya-1`.
    - On its first start, with an empty data volume, the local Temporal server took more than
      5 s to create its database. The app exited once and Docker restarted it; it has been
      healthy since.
    - The old `moyai-data` volume is untouched and stays as the backup.
  - Live checks:
    - Sign-in works; everyone is signed out once, as planned.
    - 15 open conversations: the 14 from the baseline and the test below.
    - All 5 saved files open, so the stored file IDs still match.
    - All 10 conversations with a computer point to an existing `modelmesh-ws-*` container.
    - Upgrade check: a test conversation created on the old app wrote a marker file. On the new
      app, the same conversation read the marker back from its renamed container, and no new
      container was created.
    - "draw a cat" on Auto went to `beam-1.0`, finished in 21 s and saved `cat.svg`. Six more
      Auto messages each went to the scoreboard leader for their task; Laya answered in 1.0 to
      1.4 s.
    - All test conversations went to the trash and their sandboxes were removed.
    - The browser check (Home and the 9 Settings pages, 3 widths, both themes) found no errors
      and no "Moyai" in the markup. The accessibility scan found no WCAG 2.1 A or AA
      violations, and a visible focus style on every Tab stop.
- **01:40 Step 7 started.**
  - Running in parallel: the full Python suites on the renamed tree; three read-only reviewers
    (correctness and regressions, security, accessibility); a docs agent rewriting the docs.
  - Found and fixed: the copied database had lost SQLite's WAL mode. On 2026-11-05, WAL was
    switched on by hand in the old database file to stop "database is locked" errors, which a
    cancelled live stream caused by holding a read open. The data move built a fresh file in the
    default mode. WAL is now set in code for every connection (`configure_sqlite` in
    `database.py`), with a test, so a new data volume can no longer lose it. The live database
    switches when the app restarts on the next image.
  - New README: what ModelMesh is, how its containers fit together, setup in four steps, how
    Auto routing decides, how to operate and develop it, and the repository layout.
    - Screenshots of Home in both themes come from a temporary, empty instance, so none of the
      owner's conversations appear. The old Moyai screenshots were deleted.
  - The memory notes for future sessions were updated to the new names and commands.
  - Accessibility review of the UI code (WCAG 2.1 AA, for what an automated scan cannot see).
    It found 12 issues, all present in the UI before this cleanup. A helper agent is fixing
    them:
    - Multi-select questions could only be sent with Enter, not by touch or mouse.
    - A hidden file input was a Tab stop with no visible focus.
    - Status changes were announced unreliably, and a finished reply not at all.
    - Two controls' accessible names did not contain their visible text.
    - Error messages did not name the field.
    - Four popovers were unnamed dialogs.
    - Chart series were told apart by colour alone.
    - Performance card explanations existed only as tooltips.
    - The main app had no skip link.
    - Background motion could be paused only through the operating system setting.
    - The composer border contrast was 1.5:1.
    - Smaller items: heading levels, the breadcrumb element, `<pre>` regions, sidebar link names.
    - Checked and fine: keyboard alternatives for drag-and-drop, the command palette, panel
      tabs, menus, dialog focus handling, chart table equivalents, page titles and form labels.
    - Fixed, all 12:
      - Multi-select questions have a visible "Send answer" button. Single-select questions
        say that choosing an option sends the answer.
      - The hidden file input is no longer a Tab stop.
      - Two persistent screen-reader status regions announce "Reply ready", "Needs your
        input", "Stopped" or "Failed" when a turn changes, plus toasts, sent answers and
        code-copy results. The per-tool "Working…" text is no longer announced at every step.
      - The privacy toggle's name is its visible text, and the model pickers' names include
        the chosen model.
      - Validation errors name the field ("Display name: …"); the field is marked invalid.
        The username has a format hint, and date errors say From or Through.
      - The four popovers have names.
      - Chart legends show each line's dash pattern. Partial-coverage days are marked in
        the bar text and in a Coverage column.
      - Performance card explanations are visible text.
      - A "Skip to conversation" link, and a named sidebar.
      - A "Reduce motion" switch in Settings → General, stored per browser.
      - The composer border now meets 3:1 contrast.
      - Reply headings start at level 3, code blocks are named regions, the breadcrumb is a
        navigation landmark, and sidebar links read "title, status".
    - Checked:
      - Type check and UI build pass; UI unit tests 130 passed, including new accessibility
        tests.
      - axe found no violations on Home, conversations and Settings in both themes, with every
        popover and a question form open.
      - The browser tests broken by these intended changes were updated.
  - Browser test suite (`npm run test:ui`), by a helper agent and me:
    - `tests/browser/native_product_shell.cjs` still tested the hosted product: product sign-in,
      the product Users editor, the legacy history sidebar and Connections. It became
      `native_app_shell.cjs`. The tests of removed features were deleted, and the tests of kept
      UI were repaired (for example, dropping assertions about the removed `connections` field
      and `resolve=true` query). The README screenshot capture in it went too.
    - One more stale test opened the GitHub pull-request dialog in the Workspace panel. It
      now uses the panel's "Add output link" dialog, which checks the same keyboard behaviour.
    - Result: 276 of 276 browser tests pass.
    - The helper renamed the file with `git mv`, which staged the rename in the git index
      against the no-index-changes rule. I unstaged it (`git restore --staged` on those two
      paths only), so the index again holds only the two staged deletions that were there before
      this work. The working files did not change.
  - The PC slept from about 04:20 to 09:00, which froze Docker and left one Python test run
    waiting forever. Run alone, the file it stopped in passes (59 of 59). The final run uses
    a 5-minute limit per test.
  - The first full run after the audit fixes found 7 tests that my fixes had broken. All are
    fixed:
    - A test of two processes creating a new database at once: one could lose the switch to
      WAL. That loss is now tolerated, because the other process makes the switch.
    - A test that expected `/workspace/AGENTS.md` to be loaded as instructions. It now checks
      that the file's text never reaches the model.
    - A test that pinned the old Content Security Policy.
    - Swarm tests that use 21, 37 and 61 workers. The bound is 100: swarms can only be started
      through the API, and the bound only stops absurd sizes.
    - A new test checks that only image uploads make an Auto message a vision task.
    - The affected files pass again: 101 tests, plus 34 swarm and API-contract tests.
- **09:40 Final build and live checks.**
  - Rebuilt the image (840 MB) and recreated the app and the gateway with no turn running.
    The app was healthy after 30 s. Laya kept running.
  - The live database switched to WAL on that start.
  - The app no longer receives the Modal settings, and the gateway runs the pinned build.
  - A new conversation's computer started with every limit: 2 GB of memory and no extra swap,
    2 CPUs, 512 processes, out-of-memory priority 500, no raw sockets, no privilege gain.
    The agent's shell worked under them, and the follow-up turn reused the same computer.
  - 14 open conversations; all 5 saved files open.
  - "draw a cat" on Auto went to `beam-1.0` and saved `cat.svg`. The six other task types
    each went to their scoreboard leader; Laya answered in 1.2 to 1.4 s.
  - Only the owner's 9 computers remain.
  - Docker clean-up:
    - Removed the image `moyai-local`.
    - The image of the owner's 9 older computers is now tagged `modelmesh-workspace:legacy`,
      and its `moyai-workspace` name is gone.
    - Images left: `modelmesh`, `modelmesh-workspace`, `modelmesh-laya`.
    - Volumes left: `modelmesh-data`, `laya-models`, and `moyai-data` (the backup of the data
      as it was before the rename).
  - The final browser check (Home and the 9 Settings pages, 3 widths, both themes) found no
    errors and no "Moyai" in the markup. The accessibility scan found no WCAG 2.1 A or AA
    violations, and a visible focus style on every Tab stop.
- **10:36 Step 7 done; the cleanup is complete.**
  - Final full Python suites on the final code: 885 passed, 39 skipped, 1 failed. The one
    failure is `test_package_build`, which needs npm in the test image and also fails in the
    pre-cleanup backup.
  - UI: type check and build pass; 130 unit tests and 276 browser tests pass. Lint is clean.
  - The git index holds only the two staged deletions that were there before this work.
    Nothing was committed, and nothing on the GitHub remote was touched.
  - Left for the owner to decide:
    - Delete the `moyai-data` volume once ModelMesh has run well for a while.
    - Revoke the unused Modal token (now in `.env.modal-unused`) if nothing else uses it.
    - The root `.env` is only for running outside Docker. It still has the old Modal settings
      and a few old values that mention Moyai.
    - Make new conversations private by default before adding a second account.
    - The repository folder is still named `Moyai`. Renaming it is safe for the app, but
      Docker Desktop shows the project by its `name: modelmesh` either way.
    - The backup archive of the pre-cleanup tree is in a temporary folder (path at the top of
      this log). Copy it somewhere safe to keep it.
  - Docs, by a helper agent:
    - Rewrote `docs/usage.md`, `docs/architecture.md`, `docs/pydantic-automations.md`,
      `docs/pydantic-performance.md`, `docs/verification.md`, `native/README.md` and the index
      `docs/README.md` for ModelMesh as it is now.
    - Removed Slack, product, Postgres, Modal, broker and migration material from eight more
      docs.
    - Deleted `native/VERIFICATION.md`, the original product's handoff log about Modal, Slack,
      GitHub and desktop control.
    - Every link resolves. No doc or README names a removed feature. "Moyai" appears only
      where a doc says ModelMesh started from it.
  - Security review (read-only, by a helper agent).
    - Found sound:
      - Sign-in: scrypt hashes, constant-time compare, a 12-character minimum, and signed
        HttpOnly SameSite=Strict cookies, which a password or role change revokes.
      - CSRF on every change. Authorization on every route, with no way to reach another
        account's private data.
      - File serving: HMAC IDs checked on read; anything not a verified image downloads as an
        attachment.
      - The UI: no raw HTML from the model, and a strict Content Security Policy.
      - No path from model output to the Docker socket. No keys in code or logs.
    - Fixed now:
      - Computers had no resource limits. A runaway job could take down Laya, the gateway or
        the app; that happened twice on 2026-11-06. New computers now start with 2 GB of
        memory, 2 CPUs, 512 processes, no raw sockets and no privilege gain, and the VM kills
        their processes first under memory pressure. The owner's 9 existing computers were
        given the same memory, CPU and process limits with `docker update`, without a restart.
      - One turn can now start at most 6 subagents, each with its own computer. A swarm can
        have at most 20 workers.
      - A file the agent was tricked into writing (`/workspace/AGENTS.md` or `CLAUDE.md`) was
        loaded as a system instruction into every later turn. It no longer is. The agent still
        reads those files as ordinary files when a task needs them.
      - The unused Modal credentials and `SANDBOX_PROVIDER` were still passed into the app. They
        moved from `modelmesh.env` to a separate git-ignored file, `.env.modal-unused`; nothing
        was deleted. The owner can revoke the Modal token if nothing else uses it.
      - The LiteLLM gateway image is pinned by digest to the build in use. The gateway holds
        the Lumen key and sees every prompt, so it now changes only on purpose.
      - The README no longer says all saved data is encrypted: `agent-state.db` is not, and the
        key sits in the same volume. It states the computers' limits and their internet and
        host reach, and gives optional hardening against DNS rebinding.
    - Documented, not changed:
      - New conversations default to shared. That matters only once there is a second
        account, and the default is the owner's product choice.
      - Computers can reach the internet, PC services through `host.docker.internal`, and each
        other. A dedicated network with traffic between computers off is the next step.
      - Memory can carry an injected instruction into later conversations.
      - Sessions last 7 days after sign-out unless the password changes.
      - The UI font loads from api.fontshare.com.
      - The Temporal dev server binary downloads at start.
  - Correctness review (read-only, by a helper agent). It compared the backup with the current
    tree after mapping the names, and found no high-severity defect.
    - Confirmed:
      - No kept code calls a removed module, route, setting or field.
      - Data saved before the cleanup still loads.
      - Every activity the workflows call is registered.
      - The UI and the API match.
      - Conversation, memory and computer keys are unchanged.
    - Fixed:
      - Auto treated every attachment as an image, so a CSV or a code file sent the message to
        the vision model. Only PNG, JPEG, GIF and WebP uploads count now. This predates the
        cleanup.
      - The agent instructions described scheduling tools ModelMesh does not have. They now
        send the user to Settings → Automations.
      - Error messages that named "account context" (from the removed Connections) now name the
        requester or the model.
      - The Performance page text described a removed "broker". It now says "request setup".
      - `.env.example` listed `MODELMESH_MODEL_CALL_LIMIT`, which applies only when replaying
        history from before an old version. It is gone from the template.
      - The Content Security Policy no longer allows WebSocket connections. Only the removed
        desktop stream used them.
      - `tsconfig.json` no longer lists the deleted statue module.
    - Checked and not needed: the migration rewrote only one renamed metadata key. A read-only
      scan of every text column, sealed values included, found no stored copy of the other two
      renamed markers (skill chips and optional questions).
    - Left as harmless: dead automation tool names in `effects.py`, a condition in
      `inference_usage.py` that is always true, and the old `X-CSRF-Token` header the UI also
      sends.
- **11:00 Published to the owner's GitHub repository.** The owner asked for this codebase to be
  pushed to https://github.com/manojkmohan5/ModelMesh as branch `feature/ModelMesh-mockup`, with
  a CI workflow and no errors.
  - That repository's `main` holds a different project (a Django app) with no shared history, so
    the branch is a single standalone commit. It leaves `main` untouched and carries none of
    the original Moyai history. The local `origin` remote (the original Moyai repository) was not
    touched; the owner's repository is the remote `modelmesh`.
  - Before the commit:
    - Every env file is git-ignored.
    - No file in the commit contains any of the 7 secret values from the env files (checked by
      value, not printed).
    - The largest file is 6.5 MB.
  - Made the code pass the stricter CI checks:
    - Ruff with the project's full rules: 8 import-order fixes after the rename, a bare
      expression, and `contextlib.suppress` for the WAL switch.
    - pyright now reports 0 errors. Two came from my audit fixes (`None` counts), and two
      predated the cleanup.
    - `test_package_build` passes when npm is present, which it is in CI.
  - CI (`.github/workflows/ci.yml`) runs three jobs on every push:
    - lint and types;
    - the Python tests;
    - the web UI: API client check, type check, unit tests, build and browser tests.
  - First CI run on GitHub (Linux):
    - Lint and types passed.
    - The Python tests passed, including `test_package_build`, which has npm there.
    - The browser tests passed 275 of 276. At 320 px, the routing Details table was a few
      pixels wider than its box on Linux. Its cells could not wrap (the table component
      defaults to `nowrap`), and Linux text renders slightly wider. The cells may wrap now, so
      the table fits on any platform.
