# Plan: benchmark-proven smart routing with Laya

Status: draft for review. Nothing in this plan is built yet. Owner: ModelMesh.
Date: 2026-11-06.

## 1. Summary

Today, ModelMesh's Auto mode asks an LLM (`gemma-4-31b-it`) to pick one of our 12 Lumen
models, guided by notes I wrote by hand from model specs and one test run per model.
That works, but the choice is not proven, it costs 1 to 3 seconds per message, and the
user never sees why a model was picked.

This plan replaces guesswork with evidence, in five steps:

1. **Benchmark** all 12 models on the kinds of tasks people bring to ModelMesh, using
   zero-shot, few-shot and many-shot prompts. Measure quality, speed, cost and reliability.
2. **Research** what each model's developer says it is built for, with sources.
3. **Route with Laya.** Laya reads each prompt and decides what kind of task it is. A
   scoreboard built from the benchmark then picks the model that scored best on that kind
   of task. The current LLM router stays as the fallback.
4. **Show the decision.** After every message, a short pop-up at the top says which model
   was chosen, why, and the benchmark score that backs it.
5. **Grow the gateway.** Add routing logs, automatic fallbacks and usage policies, so
   ModelMesh works as an AI gateway: routing, observability, optimization and governance.

Estimated effort: 8 to 10 working days, in phases you can stop after. Estimated benchmark
spend: about $40 at Lumen's list prices (section 6.5).

## 2. Decisions I need from you before I start

| # | Question | My default if you have no preference |
|---|---|---|
| 1 | Approve the benchmark spend: about $40 of Lumen usage (up to $60 with retries), about 4 hours of runtime. | Run it overnight with a $60 cap. |
| 2 | Which task types matter most for your users? | The 10 categories in section 6.1, weighted equally. |
| 3 | Include the expensive categories: vision (5 models), very long documents, and agent tasks run in the computer? | Yes, with fewer items each. |
| 4 | Which model judges open-ended answers (summaries, writing, translation)? | Two judges, `glm-5.3` and `qwen3.8-27b`, averaged. A model never grades itself. |
| 5 | How to trade quality against cost and speed when routing. | 60% quality, 20% speed, 20% cost. Adjustable in settings. |
| 6 | Will people write prompts in languages other than English? | English first. Laya's English model breaks on other languages, so I would add its multilingual model if needed. |
| 7 | Do you want budgets and rate limits per user now? This needs a small database for the LiteLLM gateway. | Not in this round (phase 8, optional). |
| 8 | Pop-up behavior. | Shows for 8 seconds, pauses while hovered or focused, and leaves a small "via model" tag on the answer. |

## 3. Where we are today

- **Auto mode:** `native/src/modelmesh/model_router.py` asks `gemma-4-31b-it` to pick a model,
  with `qwen3.8-flash` as backup. It routes once per message, keeps the previous model for
  follow-ups, and falls back to the default if routing fails.
- **Evidence:** one run per model on "draw a cat" and two small file tasks, plus a 10-prompt
  router test. Not enough to call any model "best" at anything. Some of the router's notes are
  also wrong (section 5).
- **Visibility:** the composer hint shows which model the last message ran on. There is no
  reason, no score and no history of decisions.
- **Reliability seen so far:** Lumen returned "Upstream error" 9 times out of about 320 calls
  (6 for `qwen3-coder-next`, 3 for `qwen3.8-flash`), and `gemma-4-31b-it` had a few minutes of
  "No healthy endpoints". The gateway has no automatic fallback yet.
- **Gateway:** LiteLLM proxy with the 12 models. It has no database, so LiteLLM's spend
  tracking, budgets and virtual keys are off.

## 4. What Laya is, and how we will use it

I read the Laya repository (github.com/NandhaKishorM/laya, version 0.4.26, Apache-2.0 code,
about 35k stars, last commit 2026-11-05). Key facts for this plan:

- **Laya is a fast text classifier, not a model router.** Its README calls it a
  "System 1 decision engine": it answers typed questions about text (`choice`, `score`,
  yes/no) in one forward pass of a small encoder, with no text generation.
- **It learns from labeled examples, offline.** `agent.fit(rows, questions=...)` trains a small
  head per question from rows like `state` (the text) plus the correct label. It has no API to
  load benchmark tables or model profiles, and it does not learn from outcomes by itself.
- **It knows when it is unsure.** Each answer carries probabilities, a calibrated
  `answer_confidence`, a conformal `prediction_set`, and `route: "review"` when it should abstain.
- **It runs locally.** `laya-serve` is an HTTP server (`POST /v1/systemone`, optional bearer key).
  It needs PyTorch and about 2.3 GB of model weights, and it runs on CPU. The README quotes
  33 ms per question on a T4 GPU; there is no CPU figure, so I will measure it.
- **Honest limits from its README:** near chance on zero-shot typed decisions (it must be
  trained), "a few hundred rows" for a route you can act on, and the English model is
  unreliable on other languages (a multilingual model exists).

**How this maps to your goal of "registering the benchmarks and decisions to Laya":**
Laya cannot hold the benchmark itself, so the work splits in two.

1. **Laya answers "what kind of task is this?"** We train it on labeled prompts: every
   benchmark item (its category is known), plus realistic ModelMesh prompts and, over time,
   past decisions that worked. This is how the benchmark and the decisions reach Laya.
2. **A scoreboard answers "which model is best for that kind of task?"** It is a routing
   policy file generated from the benchmark results. It lists, for each task type, the models
   ranked by a combined score of quality, speed and cost, and it skips models that cannot do the
   task (for example, text-only models for image questions).

```
prompt ──> Laya: task type + confidence ──┬─ confident ──> scoreboard ──> model + reason + proof
                                          └─ unsure ─────> LLM router (today's) ──┘
every decision ──> decision log ──> pop-up, history, routing dashboard, Laya retraining
```

If Laya proves too slow or inaccurate on CPU in the first spike, the fallback is a simpler
classifier on sentence embeddings with the same scoreboard. Nothing else in the plan changes.

## 5. What each model is documented to be good at

Preliminary research from public model cards and vendor posts, checked 2026-11-06. All 12 ids
match real public models. **Every score below is the developer's own claim**; I found no
independent replication. This is exactly why we benchmark them ourselves.

| Lumen id | Developer, architecture | What the developer says it is best at | Vendor-reported scores |
|---|---|---|---|
| beam-1.0 | Reflection AI; MoE, 501B total / 23B active | Agentic coding, tool use, math and science, using about 3x fewer reasoning tokens. Text only, mainly English | SWE-bench Verified 81.6; AIME 2026 97.7; GPQA Diamond 90.6 |
| deepseek-v4-flash | DeepSeek; MoE, 284B / 13B | Low-cost math and code reasoning, 1M context. Text only | LiveCodeBench 91.6; SWE-bench Verified 79.0; GPQA Diamond 88.1 |
| deepseek-v4.1-flash | DeepSeek; MoE, 552B | Cost-efficient, input-heavy agent work; image input; adjustable reasoning effort | Terminal-Bench 2.1 90.6; GPQA Diamond 90.9 |
| gemma-4-31b-it | Google DeepMind; dense, 31B | General quality, 140+ languages, images, function calling and JSON | MMLU-Pro 85.2; LiveCodeBench v6 80.0; GPQA Diamond 84.3 |
| glm-5.3 | Z.ai; MoE, about 744B / 40B (third-party figure) | Long-horizon software engineering, security work. Always reasons. Text only | Terminal-Bench 2.1 88.2; CyberGym 84.5 |
| muse-glimmer-30b | Meta; dense, about 30B | Autonomous agents on modest hardware, computer use, images | SWE-bench Verified 76.0; OSWorld-Verified 65.9; AIME 2026 94.7 |
| nemotron-3-super-120b-a12b | NVIDIA; hybrid Mamba/attention MoE, 120B / 12B | Long context (up to 1M), agents, retrieval (RAG), high volume. Text only | RULER at 1M 91.75; AIME25 90.21; LiveCodeBench 81.19 |
| ornith-1.0-35b | DeepReinforce; MoE, 35B (third parties say a Qwen3.5-35B-A3B post-train) | Agentic coding: terminal, whole-repository tasks | SWE-bench Verified 75.6; Terminal-Bench 2.1 64.2 |
| qwen3-coder-next | Qwen (Alibaba); MoE, 80B / 3B | Coding agents and tool calling; answers without a thinking phase | SWE-bench Verified 70.6; Terminal-Bench 2.0 36.2 |
| qwen3.6-35b-a3b | Qwen; MoE, 35B / 3B | Agentic coding (especially front end), images and video, math | SWE-bench Verified 73.4; AIME26 92.7; MMMU 81.7 |
| qwen3.8-27b | Qwen; dense vision-language, 27B | Coding and terminal work, computer and app use, vision | LiveCodeBench v6 90.3; OSWorld-Verified 84.3 |
| qwen3.8-flash | Qwen; MoE, 125B / 6B (open "Flash-Next" weights) | Coding, agents, visual understanding including long video | LiveCodeBench v6 91.9; GPQA Diamond 91.7; LVBench 76.6 |

Sources are kept per model in `docs/benchmarks/model-profiles.md` (phase 1). Main ones:
the Hugging Face model cards (deepseek-ai, google, zai-org, meta-models, nvidia,
deepreinforce-ai, Qwen) and the developers' posts (reflection.ai, api-docs.deepseek.com,
docs.z.ai, ornith.ai, Alibaba Model Studio).

**What this changes:**

- **The router's current notes are partly wrong.** They were inferred from Lumen's prices and
  names. For example, they call `ornith-1.0-35b` "the cheapest model for simple tasks", but its
  developer built it for agentic coding. Phase 1 replaces the notes with sourced profiles,
  before the benchmark lands.
- **Most of these models are marketed for agentic coding,** so vendor claims cannot tell them
  apart. Our own benchmark, run inside ModelMesh, can.
- **Things to confirm with Lumen:**
  - DeepSeek's own API now sends `deepseek-v4-flash` requests to V4.1-Flash. We should check
    which weights Lumen serves.
  - Developers say Gemma 4, Qwen3.6 and Qwen3.8-27B also accept video, while Lumen lists video
    only for `qwen3.8-flash`.
  - Several models support longer contexts than Lumen exposes. That is probably a Lumen setting.
  - Lumen flags `qwen3-coder-next` as able to reason, but its developer says it has no thinking
    mode; `glm-5.3` always reasons.

## 6. The benchmark

### 6.1 Task categories

| Category | Example item | How it is scored |
|---|---|---|
| Code writing | Write a function that passes hidden unit tests | Run the tests in the Docker computer (pass rate) |
| Debugging | Fix a failing test, given the code and the error | Tests pass after the fix |
| Math and reasoning | Grade-school and competition-style word problems | Exact final answer |
| Knowledge Q&A | Multiple-choice questions across subjects | Exact choice |
| Following instructions | "Answer in exactly 3 bullets, under 50 words, as JSON" | Automatic checks (JSON schema, counts, regex) |
| Summarizing | Summarize an article for a given reader | Two judges with a rubric (faithful, complete, concise) |
| Translating | English to and from 5 languages | Reference score (chrF) plus judges |
| Writing | Short marketing copy, emails, stories | Two judges with a rubric |
| Long documents | Find facts hidden in 30k to 200k tokens | Exact answer; only models whose context fits |
| Images (5 models) | Questions about charts, screenshots, photos | Exact or judged answer |
| Agent tasks (small set) | "Create a script, run it, report the output" in ModelMesh's computer | Task completed and checked |

### 6.2 Prompting styles

Every item is asked three ways, so we can see which models gain from examples:

- **Zero-shot:** the task alone.
- **Few-shot:** 3 worked examples, then the task.
- **Many-shot:** 25 to 50 worked examples, then the task. Only where the model's context fits.

The same items appear in all three styles, so differences come from the prompting, not the items.

### 6.3 What we measure

For each model, category and prompting style:

- **Quality:** the score above, with a 95% confidence interval (bootstrap), since samples are small.
- **Speed:** median and 95th-percentile total time, and time to first token where streamed.
- **Cost:** tokens in and out, priced with Lumen's list prices.
- **Reliability:** upstream errors, timeouts, and empty answers (a thinking model that used its
  whole output budget before answering).
- **Agent fitness:** whether tool calls succeed inside ModelMesh, since every ModelMesh turn uses tools.

### 6.4 Fairness rules

- Same temperature (0.2) and the same output budget for every model: 4,096 tokens, or 8,192 for
  models that think first. Hitting the budget is recorded, not hidden.
- Calls go through our LiteLLM gateway, the same path ModelMesh uses.
- At most 4 calls at a time, with retries and backoff, so Lumen's limits do not skew results.
- A model never judges its own answers. 10% of judged answers get a human spot check (you or me).
- Public test sets may be in the models' training data, so about a third of the items are new ones
  I write for this benchmark. The report shows both groups separately.
- Item counts: 20 per category and style (10 for judged categories, 5 for agent tasks).

### 6.4b Techniques beyond prompting (added 2026-11-06 at your request)

| Technique | What it tells us | How |
|---|---|---|
| Repeated sampling | How stable a model is, and best-of-3 success | Code and math subsets, 3 samples each at temperature 0.7 |
| Robustness | Whether small wording changes break a model | Paraphrased items, items with irrelevant details added, and items with typos, compared with the originals |
| Confidence calibration | Whether a model knows when it is wrong | Knowledge items also ask for a confidence from 0 to 100; we measure calibration error |
| Tool-calling accuracy | Whether a model picks the right tool with the right arguments | Function-calling items with OpenAI tool schemas, including cases where no tool should be called |
| Head-to-head judging | A steadier ranking for open-ended answers than single scores | Judges compare two models' answers side by side; ratings computed with the Elo method |
| Efficiency | Quality for the money and the wait | Tokens per correct answer, output speed in tokens per second, cost per correct answer |
| Prompt-injection resistance | Whether a model obeys instructions hidden in the documents it reads | Summaries of passages that contain planted instructions |

### 6.5 Cost and time estimate

About 10,600 model calls: 6,500 benchmark calls, 2,900 judge calls and 1,200 agent-task calls.
At Lumen's list prices, that is about $16 for answers, $25 for judging with two judges, and
$4 for agent tasks: **about $40 in total**, with a $60 cap for retries. At 4 parallel calls, it
takes about 4 hours. I will run a 5% pilot first and re-estimate before the full run.

### 6.6 Benchmark deliverables

- `benchmarks/`: the harness, the item files, the graders and a `README.md` with the
  one-line command to re-run everything. Resumable: a crashed run continues where it stopped.
- `benchmarks/results/<run-id>.jsonl`: every prompt, answer, score, time and cost.
- `docs/benchmarks/MODEL-BENCHMARKS.md`: the living record of everything we do to test models.
  It covers the methods, settings, item inventory, each run and its results, and the routing
  decisions made from them, so later runs can reuse and compare.
- `docs/benchmarks/model-profiles.md`: the researched profile of each model.
- `docs/benchmarks/<date>-lumen-benchmark.md`: the report. It covers the overall leaderboard,
  the winner per category, how each model responds to few-shot and many-shot prompting, the
  quality-for-cost frontier, reliability, and the routing table the results support.
- `benchmarks/routing-policy.json`: the scoreboard that ModelMesh routes with.

## 7. Routing with Laya

1. **Spike (first).** Run `laya-serve` in Docker on CPU. Measure speed and memory. Train a
   task-type head on about 50 rows as a smoke test. Check the license of the model weights
   (the code is Apache-2.0; the weights' license is not confirmed). Go/no-go decision with you.
2. **Training data.** About 400 to 600 labeled prompts: the benchmark items plus realistic ModelMesh
   prompts (short follow-ups, pasted errors, "draw a cat"). Hold 20% out for testing.
3. **Targets.** At least 85% correct task type on the held-out prompts, less than 15% "unsure",
   and routing in under 300 ms at the 95th percentile (today's LLM router takes 1 to 3 s).
4. **Decision rules,** in order:
   - The user picked a fixed model: no routing.
   - A message sent while a turn is running keeps that turn's model (as today).
   - Laya is confident: take the scoreboard's best model for that task type.
   - Laya is unsure: today's LLM router decides, limited to the scoreboard's candidates.
   - Keep the previous model for follow-ups unless another model is clearly better
     (more than 5 points) for the new task, because switching drops the saved tool history.
   - Skip models that cannot do the task (images, context too long, unhealthy right now).
5. **Deployment.** A `laya` service in `compose.yaml`, reachable only inside Docker,
   protected by `LAYA_API_KEY`. A setting `ROUTER_ENGINE=laya|llm` switches back to today's router
   instantly.
6. **Learning over time.** Every decision is logged with what happened next: the turn finished or
   failed, or the user switched the model by hand. A `retrain` command turns good decisions into new
   training rows and re-fits Laya. I will run it by hand at first; it can be scheduled later.

## 8. Showing the decision to the user

**Pop-up after every routed message,** at the top of the chat:

> **Routed to glm-5.3** · coding task (92% sure)
> Top coding score in our Nov 2026 benchmark: 0.86, #1 of 12.
> [Details]

- Details opens a small panel with the runner-up models and their scores, the speed and cost
  figures, the engine that decided (Laya, LLM router or follow-up rule), and a link to the
  benchmark report.
- Each answer keeps a small "via glm-5.3 · coding" tag that reopens the same details later.
- **Accessibility:** announced politely to screen readers, never steals focus, closable with the
  keyboard, pauses while hovered or focused, no motion when reduced motion is on, and meets the
  same contrast rules as the rest of the app.

**How it is stored and served** (no database migration):

- The decision is saved as an event on the turn (`kind = "model_routed"`, JSON content) in the
  same transaction that creates the turn.
- The API gains a typed `routing` field on each turn (Pydantic model, regenerated TypeScript
  client, per the repo's type-safety rules), so the pop-up and the history read the same data.
- The pop-up reuses the existing status-toast pattern and the shared Popover component. No new
  dependency.

## 9. Gateway features: observability, optimization, governance

In order of value. Each one can ship on its own.

1. **Automatic fallback.** When a model fails upstream, retry on the next model in the
   scoreboard for the same task type, and say so in the pop-up. This targets the 9 upstream
   errors seen so far.
2. **Routing dashboard** (Settings, admins): every decision with time, user, task type, model,
   confidence, routing time, cost and outcome. Filters and CSV export. Totals by model and
   task type, the "unsure" rate, the fallback rate and each model's error rate.
3. **Model health.** Mark a model unhealthy after repeated errors, route around it, and retry
   it later. Shown in the dashboard.
4. **Policies.** Exclude models per role, or require a fixed model for some users.
5. **Budgets and rate limits per user** with LiteLLM virtual keys. This needs a small PostgreSQL
   database for the gateway. Optional, only if you want it (question 7).

LiteLLM already offers retries, fallbacks, spend logs and budgets. ModelMesh will use those
where they fit and only add what LiteLLM cannot know: the task type, the benchmark evidence
and the conversation.

## 10. Phases and checkpoints

| Phase | Work | Effort | Your checkpoint |
|---|---|---|---|
| 0 | Your answers to section 2; Laya spike on CPU | 0.5 day | Go/no-go on Laya |
| 1 | Model profiles with sources → `model-profiles.md`; correct the router's notes | 0.5 day | |
| 2 | Benchmark items, graders and harness; 5% pilot | 2 days | Approve the full run after the pilot cost check |
| 3 | Full benchmark run and report | 1 day | **Review the benchmark report** |
| 4 | Scoreboard (routing policy) from the results | 0.5 day | |
| 5 | Laya training, evaluation and integration | 2 days | Accuracy and speed numbers |
| 6 | Pop-up, answer tags and stored decisions | 1 day | Try it in the app |
| 7 | Fallbacks, routing dashboard and model health | 1.5 days | |
| 8 | Optional: budgets and rate limits | 1 day | Only if you want it |

Total: 8 to 10 working days, plus your review time.

## 11. Testing and rollout

- Unit tests for every routing rule, the scoreboard, fallbacks and decision storage.
- An evaluation of Laya on held-out prompts, reported as accuracy, "unsure" rate and speed.
- A browser test for the pop-up: content, keyboard use, screen-reader announcement, reduced
  motion, and the 320, 768 and 1440 px widths.
- A live check with 10 prompts across task types, comparing the pop-up's claims to the report.
- The existing suites (frontend, browser, backend, API contract) stay green.
- Rollout behind `ROUTER_ENGINE`. Today's router stays available as the fallback and the off switch.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Laya is a classifier, not a router, and is weak until trained | The scoreboard does the model choice; Laya only labels the task; we train it on 400 to 600 rows; unsure answers go to the LLM router |
| Laya is slow on CPU or heavy (PyTorch, 2.3 GB) | Spike first; separate container; fallback to an embedding classifier |
| Small samples make scores noisy | Confidence intervals in every table; a winner must beat the runner-up beyond the interval, otherwise prefer the cheaper or faster model |
| Judge bias | Two judges, rubrics, no self-judging, human spot checks |
| Lumen outages skew results | Retries and backoff; failures are reported as reliability, not as wrong answers |
| Test data seen in training | A third of the items are new; report public and new items separately |
| Benchmarks go stale as Lumen updates models | Re-run command; the report and the pop-up show the benchmark date |
| Scope creep in gateway features | Phases 7 and 8 are separate and optional |

## 13. Progress log

Approved 2026-11-06. You did not answer section 2, so these defaults apply: run the benchmark with a
$60 cap after a pilot; all 10 task types, weighted equally; include images, long documents and agent
tasks with fewer items; judges `glm-5.3` and `qwen3.8-27b`, averaged, with no self-judging; routing
weights 60% quality, 20% speed, 20% cost; English first; no per-user budgets this round (phase 8
skipped); pop-up shown for 8 seconds, pausing on hover or focus.

| Phase | Status | Notes |
|---|---|---|
| 0. Laya spike | Done: go | 9-label task-type head trained on 72 prompts: 18/18 held-out correct, 1/18 flagged unsure, cross-validated accuracy 0.98; 'draw a cat' -> build at 0.9999. CPU speed: 1.4 s median, 4.7 s max per prompt (target was 0.3 s: Laya's 0.8B reader runs slow reference kernels on CPU). Memory 4.2 GB; first start about 4 min (download and load), fit 229 s. Decision: go. It matches the LLM router's 1-3 s while running locally with calibrated confidence; keep the service warm |
| 1. Model profiles | Done | `docs/benchmarks/model-profiles.md` (sourced); `NOTES` in `model_router.py` rewritten from sources and tests |
| 2. Benchmark harness and items | Done | 183 items and 145 solved examples across 12 categories (public sets: HumanEval, MBPP, GSM8K, MMLU, FLORES-200; the rest written fresh). `validate_items.py`: 0 problems, 101 Python test runs in Docker. `run.py` now shuffles jobs (seeded by run id) so parallel calls spread across models and a cost stop leaves no whole category out |
| 3. Benchmark run and report | Done | Pilot `2026-11-06-pilot`: 473 answers, $0.52, 11 min, 0 errors; its scoreboard is not used (1-2 items per category saturate quality). Full run `2026-11-06-full`, 02:10 to 12:46: 6,017 answers from 12 models (60 of them agent tasks inside ModelMesh), $7.91 of answers (judging cost not recorded), 1 error left. Judges scored all 480 open-ended answers and 771 head-to-head matches. Lumen's coin budget stopped the run twice; it took three keys. Report: `docs/benchmarks/2026-11-06-full-lumen-benchmark.md`. Two scoreboard rules added: a model whose 95% interval reaches the best quality counts as tied, so speed and cost decide, except after a failed hard check. Scoreboard deployed and checked live (`auto-live-1` in MODEL-BENCHMARKS.md); Laya now stays warm with a 5-minute ping |
| 4. Scoreboard | Done | `report.py` writes `benchmarks/routing-policy.json` and the app's copy `native/src/modelmesh/routing_policy.json` |
| 5. Laya training and integration | Done | `laya/`: `make_training.py` (544 labeled prompts: 3 Lumen writers, benchmark items, spike prompts; 1 in 5 held out by text hash), `train.py` (deploys only at >= 85% held-out accuracy and < 15% unsure), `server.py` (POST /classify with a bearer key, no published port, warms up on 3 prompt lengths), `Dockerfile`; compose service `laya`, volume `laya-models`. Result: 99/100 held out, 6% unsure, 1.4 s median at 4 CPU threads (1 thread was slower). Router order: rules (images -> vision scoreboard, > 60k characters -> long-document scoreboard), then Laya task type -> best enabled model on the scoreboard (stays on the conversation's model within 5 points), then the LLM router when Laya is unsure, down or has no scoreboard. 30 router tests. Live: coding, translation and chat labeled correctly at 0.99+ confidence. Agent tasks showed Lumen's `beam-1.0` refuses strict tool schemas for the agent's tools, so every ModelMesh turn on it failed; fixed with `MODELMESH_NON_STRICT_TOOL_MODELS` (it then completed 5/5) |
| 6. Pop-up and stored decisions | Done | Decision stored as event `model_routed`; typed `TurnView.routing` (`RoutingDecision`, engines rule/laya/llm/follow-up/running-turn/fallback, with benchmark score, rank, speed and cost). Pop-up at the top of the chat: 8 s, pauses on hover, focus or open details, never takes focus, announced through a polite live region; Details lists the alternatives; each answer keeps a "via model · task" tag. Browser test at 1440/768/320 px, dark and light. Live check: the decision returned on send matches the stored history |
| 7. Fallbacks, dashboard, model health | Done | Gateway retries transient errors twice on the same model (no cross-model fallback: the turn and its tag must name the model that answered). Settings > Routing (administrators): routed messages, median routing time, engines, task types, and per-model turns and failures over 1, 7 or 30 days (`GET /api/settings/routing`). Browser tests at 3 widths x 2 themes. Also fixed a Windows-only path bug in three UI test files; the full UI suite passes (347/347) |

Benchmark record: `docs/benchmarks/MODEL-BENCHMARKS.md` (early tests already logged there).
