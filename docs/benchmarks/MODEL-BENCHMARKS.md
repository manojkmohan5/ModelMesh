# ModelMesh model benchmarks

The living record of every test we run on the models ModelMesh routes between: what we ran, how,
with which settings, what came out, and what we decided because of it. Append to it; do not rewrite
history. The test items are plain-text JSONL files in [`benchmarks/`](../../benchmarks/README.md), so
any run can be repeated and compared with earlier ones.

- Plan: [docs/plans/smart-routing-v2.md](../plans/smart-routing-v2.md)
- Item format, graders and techniques: [benchmarks/README.md](../../benchmarks/README.md)
- Researched model profiles: [model-profiles.md](model-profiles.md)

## 1. Models under test

All 12 are served by NCSA Lumen and reached through ModelMesh's LiteLLM gateway. Prices are Lumen's
list prices in US dollars per million tokens.

| Model | Context | Max output | Input $ | Output $ | Input types (Lumen) | Thinks first (Lumen) |
|---|---|---|---|---|---|---|
| beam-1.0 | 524,282 | 131,072 | 0.33 | 1.10 | text | yes |
| deepseek-v4-flash | 511,994 | 128,000 | 0.162 | 0.371 | text | yes |
| deepseek-v4.1-flash | 1,048,570 | 384,000 | 0.257 | 0.931 | text, image | yes |
| gemma-4-31b-it | 46,790 | 32,768 | 0.168 | 0.436 | text, image | yes |
| glm-5.3 | 524,282 | 131,072 | 1.321 | 4.248 | text | yes |
| muse-glimmer-30b | 131,066 | 131,072 | 0.323 | 1.345 | text, image | yes |
| nemotron-3-super-120b-a12b | 262,144 | 262,144 | 0.169 | 0.759 | text | yes |
| ornith-1.0-35b | 262,138 | 256,000 | 0.05 | 0.10 | text | yes |
| qwen3-coder-next | 131,066 | 256,000 | 0.227 | 1.236 | text | no |
| qwen3.6-35b-a3b | 262,138 | 131,056 | 0.185 | 1.138 | text | yes |
| qwen3.8-27b | 250,542 | 262,144 | 0.365 | 2.397 | text, image | yes |
| qwen3.8-flash | 262,138 | 131,072 | 0.143 | 0.453 | text, image, video | no |

## 2. Methods

The full benchmark (section 5) uses 12 task categories, three prompting styles (zero-, few- and
many-shot), and seven techniques beyond prompting: repeated sampling, robustness variants,
confidence calibration, tool-calling accuracy, head-to-head judging (Elo), efficiency, and
prompt-injection resistance. Details and item formats: [benchmarks/README.md](../../benchmarks/README.md).

Standard settings unless a run says otherwise: temperature 0.2; output budget 4,096 tokens (8,192
for models that think first); through the LiteLLM gateway at most 8 calls at a time, in a shuffled
order so parallel calls go to different models, with retries; judges `glm-5.3` and `qwen3.8-27b`, never
judging their own answers. A retried answer replaces the earlier row for the same model, item, style and
sample (`answers()` in `run.py`). Agent tasks run inside ModelMesh (`agent.py`); a task that ends in an
error (not a wrong answer) is retried once, for every model alike.

## 3. Run log

| Date | Run | What | Scope | Result in short |
|---|---|---|---|---|
| 2026-11-05 | probe-1 | Can each model call a tool, and answer in 64 tokens? | 12 models x 2 prompts | All 12 called the tool. 7 of 12 returned no text within 64 tokens |
| 2026-11-05 | router-1 | Which model makes the best router? | 12 routers x 10 tasks | 4 routers named a valid model 10/10; thinking models often ran out of budget |
| 2026-11-05 | router-2 | Same, after hardening the router prompt | 4 routers x 10 tasks, plus 7 follow-ups | All 4 valid 10/10; `gemma-4-31b-it` chosen |
| 2026-11-06 | cat-1 | "draw a cat" inside ModelMesh, per model | 4 models plus the router's pick | `glm-5.3` drew an SVG in 47 s; two models answered in ASCII art |
| 2026-11-06 | errors-1 | Upstream errors seen in the gateway log | about 320 calls | 9 failures: `qwen3-coder-next` 6, `qwen3.8-flash` 3; a `gemma-4-31b-it` outage of a few minutes |
| 2026-11-06 | laya-spike-1 | Can Laya learn ModelMesh task types, and how fast is it on CPU? | 72 training prompts, 18 held out, 9 labels | 18/18 correct, 1.4 s median per prompt on CPU |
| 2026-11-06 | laya-train-1 | Train the production task-type classifier | 444 training prompts, 100 held out | 99/100 correct, 6% unsure; deployed |
| 2026-11-06 | 2026-11-06-pilot | Pipeline check: 5% of items, all 12 models | 473 answers, $0.52 | Everything worked; scores saturated (1-2 items per category), so not used for routing |
| 2026-11-06 | agent-1 | Agent tasks inside ModelMesh | 12 models x 5 tasks | `beam-1.0` failed all 5: Lumen refuses strict tool schemas for the agent's tools; 5 other failures were local Temporal timeouts under heavy CPU load |
| 2026-11-06 | 2026-11-06-full | Full benchmark | 5,966 answers plus 60 agent tasks | Done at 12:46. 6,017 answers scored, $7.91 of answers (judging not counted); 1 error left (`gemma-4-31b-it` on `agent-fresh-002`). Judges scored all 480 open-ended answers and 771 head-to-head matches. Lumen's coin budget (HTTP 429 "Coin budget exhausted", refilled hourly in small amounts) stopped the run at 06:25 and 12:00, so it took three keys. Every model scored 1.00 on math; they differ mainly in summarizing, translating, writing and agent tasks |
| 2026-11-06 | auto-live-1 | Auto inside ModelMesh with the full run's scoreboard | 8 messages, one per task type | All 8 went to the scoreboard's first choice, labeled by Laya, in 1.1-2.5 s. The first 2 after hours idle timed out in Laya and fell back to the LLM router |
| 2026-11-06 | build-1 | Open-ended build requests ("draw a cat") inside ModelMesh | 12 models x 4 requests, failures retried once | `glm-5.3`, `muse-glimmer-30b` and `qwen3.8-27b` handed over 3 of 4 files; `qwen3-coder-next` 1 (it asked instead of drawing). Build now goes to `beam-1.0` (2 of 4, fast and cheap) |

## 4. Results of the early tests

### probe-1 (2026-11-05): tool calls and short answers

Each model got one tool-calling prompt (Responses API, `get_time` tool) and one "reply in one word"
prompt with a 64-token output cap.

- **Tool call:** all 12 models called the tool correctly. Fastest: `qwen3.8-27b` 0.9 s,
  `qwen3-coder-next` 1.0 s, `ornith-1.0-35b` 1.2 s. Slowest: `deepseek-v4.1-flash` 8.5 s,
  `glm-5.3` 9.6 s.
- **Short answer within 64 tokens:** answered by `beam-1.0`, `deepseek-v4-flash`,
  `deepseek-v4.1-flash`, `gemma-4-31b-it` and `qwen3-coder-next`. No visible text from the other 7,
  which spent the budget thinking. Lesson: give thinking models a larger output budget.

### router-1 and router-2 (2026-11-05): choosing the router model

Each model acted as the router for 10 tasks (draw a cat, fix a bug, summarize, a screenshot
question, huge documents, hard design, translate, a script, naming ideas, a proof), with a 1,024-token
budget.

| Router | Valid picks (router-1) | Median time | Valid picks (router-2) | Median time |
|---|---|---|---|---|
| gemma-4-31b-it | 10/10 | 1.3 s | 10/10 | 1.0 s |
| deepseek-v4-flash | 10/10 | 1.2 s | 10/10 | 1.0 s |
| qwen3.8-flash | 10/10 | 1.6 s | 10/10 | 1.9 s |
| qwen3-coder-next | 9/10 (followed the task once instead of routing) | 0.5 s | 10/10 | 0.5 s |
| beam-1.0 | 10/10 | 2.7 s | not run | |
| deepseek-v4.1-flash | 10/10 | 3.3 s | not run | |
| nemotron-3-super-120b-a12b | 10/10 | 3.0 s | not run | |
| muse-glimmer-30b | 9/10 | 3.9 s | not run | |
| qwen3.8-27b | 9/10 | 1.5 s | not run | |
| glm-5.3 | 8/10 | 2.6 s | not run | |
| ornith-1.0-35b | 7/10 | 3.4 s | not run | |
| qwen3.6-35b-a3b | 5/10 | 5.0 s | not run | |

Decision: `gemma-4-31b-it` became the router, with `qwen3.8-flash` as backup. With the
continuation rule, `gemma-4-31b-it` kept the previous model for follow-ups and switched for images,
huge documents and hard design work.

### cat-1 (2026-11-06): "draw a cat" inside ModelMesh

| Model | Result | Time |
|---|---|---|
| glm-5.3 | Drew an orange tabby as `cat.svg` | 47 s |
| deepseek-v4-flash | ASCII art, no file | 36 s |
| qwen3-coder-next (router's first pick) | ASCII art, or "I can't draw directly" | 26 s |
| qwen3.8-flash | Built SVG and PNG files, still checking its work after 4 minutes (stopped) | > 4 min |
| qwen3.8-27b | Wrote a Pillow script and a PNG, still working after 4 minutes (stopped) | > 4 min |

Two explicit file tasks on `qwen3-coder-next` ("create fizzbuzz.py and run it", "save a cat SVG")
both failed with Lumen "Upstream error" after 3 attempts. Decision: the router's notes were changed
so file-producing tasks go to models that work in the sandbox; "draw a cat" then routed to `glm-5.3`.

### errors-1 (2026-11-06): reliability from the gateway log

About 320 gateway calls over two days: `qwen3-coder-next` failed 6 times (streamed tool calls),
`qwen3.8-flash` 3 times. `gemma-4-31b-it` returned "No healthy endpoints" for a few minutes (34
failed calls during a benchmark). Every other model had no upstream errors in that window.

### laya-spike-1 (2026-11-06): Laya as the task-type classifier

Laya 0.4.26 (vega lane, 0.8B reader based on Qwen3.5-0.8B) in Docker on CPU. One `choice` question with 9
task types (code, debug, math, knowledge, summarize, translate, writing, build, chat), 10 prompts per
type: 8 to train, 2 held out.

| Measure | Result |
|---|---|
| Held-out accuracy | 18/18 |
| Flagged unsure (`route: review`) | 1/18 |
| Cross-validated accuracy | 0.98 |
| Speed per prompt (CPU, warm) | 1.4 s median, 4.7 s max |
| Memory | 4.2 GB peak |
| First start (download and load) | 234 s; training 229 s |

"draw a cat" was labeled `build` with confidence 0.9999. Speed misses the 0.3 s target because the
reader's linear-attention layers fall back to slow reference code on CPU. Decision: use Laya, since
it matches the LLM router's 1 to 3 s while running locally, without Lumen calls, and with calibrated
confidence.

### laya-train-1 (2026-11-06): the production task-type classifier

Training data (`laya/make_training.py`): 544 English prompts. Three Lumen models wrote 15 per label each
(`glm-5.3`, `qwen3.8-flash`, `gemma-4-31b-it`; `glm-5.3` returned nothing for 3 labels), up to 12
benchmark item prompts per label (instructions counted as writing, agent tasks as build), and the 90
spike prompts. A hash of each prompt puts one in five in the held-out set. `laya/train.py` deploys only
at 85% held-out accuracy or better with under 15% unsure.

| Measure | Result |
|---|---|
| Held-out accuracy | 99/100 (benchmark prompts 19/19, hand-written 17/17, Lumen-written 63/64) |
| Flagged unsure | 6% (accuracy on the rest: 98.9%) |
| The one miss | "From this word problem, extract only the given facts, do not solve" -> math (labeled summarize) |
| Training time | 24 min on CPU (8 threads) |
| Speed, served | 1.4 s median, 1.5 s max at 4 threads; 1 thread was slower (3.2-4.1 s median) |

The first request after a start took 27-39 s, so the server now warms up on three prompt lengths before
it accepts requests. Live check: coding, translation and chat messages were each labeled correctly at
0.99 or higher confidence.

### agent-1 (2026-11-06): agent tasks inside ModelMesh

`beam-1.0` failed all 5 tasks. Lumen answered every agent call with "BeamDetector cannot enforce strict
tools with automatic tool choice for this response template": a single simple strict tool works, but
not the agent's full tool set. So every ModelMesh turn on `beam-1.0` failed, while the chat-only
benchmark ranks it near the top. Fix: `MODELMESH_NON_STRICT_TOOL_MODELS=beam-1.0` (the same treatment
Bedrock models already get). Five other failures (`gemma-4-31b-it`, `glm-5.3`, `qwen3.8-27b` twice,
`qwen3.8-flash`) were the local Temporal store timing out ("context canceled") while the machine also
trained Laya, built images and ran test suites. All 10 failed tasks were retried once on a quieter machine:
with the fix `beam-1.0` completed all 5, the four timed-out tasks completed, and `gemma-4-31b-it` failed
`agent-fresh-002` (count primes below 200,000) again with no model error logged, so that failure stands.

### auto-live-1 (2026-11-06): Auto with the full scoreboard

Eight Auto messages went through the ModelMesh API, one per task type, in private sessions deleted
afterwards. Laya labeled each one, and each went to the scoreboard's first choice for its task type.
Routing took 1.1 to 2.5 s. The first two messages came after Laya had sat idle for hours. Both timed
out (the app waits 6 s for Laya) and fell back to the LLM router. Its model, `gemma-4-31b-it`, was briefly
down on Lumen ("No healthy endpoints"), so the backup model routed them, in about 19 s. Laya's weights
had most likely been swapped out of memory. Fix: Laya now classifies a short prompt every 5 minutes.
Each run takes about 8 CPU-seconds, 2-3% of one core; running it once a minute cost about 15%.

Later that day "draw a cat" on Auto took 28 s to route, even with the 5-minute ping. A test showed the
cause was not swap. After 4.5 idle minutes, Laya's first answer took 4.2 s and twice the usual CPU time,
with no pages read back from disk. The next answer took 1.3 s, and prompts of new lengths were fast
while warm. So the app now waits up to 15 s for Laya, not 6 s. That is still faster than the LLM router
fallback, which took 7 to 28 s. A stopped Laya still fails at once.

### build-1 (2026-11-06): open-ended build requests

The 5 agent tasks all ask for a checkable answer ("count the primes below 200,000"). None was an
open request like "draw a cat". Yet the scoreboard sent build tasks to `qwen3-coder-next`, and live it
answered "draw a cat" with "I can't draw images directly... Which approach would you prefer?" So four
open requests were added: draw a cat, a landing page for a bakery, a bar chart of four months of
sales, and a coffee-shop logo. They are graded on the files the conversation hands over, which are
the files the user can open in the Files panel. A file left inside the sandbox does not count,
because the user cannot reach it. After the first run, two more file kinds were accepted for every
model: a zipped site for the landing page and an HTML chart for the chart. This changed two results
(`glm-5.3`'s page and `deepseek-v4-flash`'s chart). Failed tasks were retried once, as usual.

| Model | Cat | Landing page | Chart | Logo |
|---|---|---|---|---|
| glm-5.3 | pass | pass | pass | timed out |
| muse-glimmer-30b | pass | built, not handed over | pass | pass |
| qwen3.8-27b | pass | pass | pass | timed out |
| beam-1.0 | pass | built, not handed over | pass | built, not handed over |
| deepseek-v4-flash | pass | built, not handed over | pass | timed out |
| qwen3.8-flash | pass | timed out | pass | timed out |
| qwen3-coder-next | asked a question | built, not handed over | pass | "can't design logos" |
| ornith-1.0-35b | ASCII art | built, not handed over | pass | no file |
| deepseek-v4.1-flash | ASCII art in cat.txt | error | pass | timed out |
| qwen3.6-35b-a3b | ASCII art | built, not handed over | text table | timed out |
| gemma-4-31b-it | error | error | error | error |
| nemotron-3-super-120b-a12b | error | error | error | error |

"Timed out" means no result within 10 minutes. `gemma-4-31b-it` and `nemotron-3-super-120b-a12b` failed
because Lumen had no healthy servers for them, both times. As with every error, those tasks count against
the error rate, not quality. Result: build tasks now go to `beam-1.0`, not `qwen3-coder-next`.

## 5. Full benchmark runs

Filled in by each run of `benchmarks/run.py` and `benchmarks/report.py`: the item inventory, the
leaderboard, the winner per category, the effect of few-shot and many-shot prompting, robustness,
calibration, tool calling, efficiency, reliability, and the routing table that follows.

### Run 2026-11-06-pilot

Full report: [2026-11-06-pilot-lumen-benchmark.md](2026-11-06-pilot-lumen-benchmark.md). 473 answers, $0.52 of answers (judging not included).

| Category | Best model | Score |
|---|---|---|
| Code writing | beam-1.0 | 1.00 |
| Debugging | beam-1.0 | 1.00 |
| Math and reasoning | beam-1.0 | 1.00 |
| Knowledge Q&A | beam-1.0 | 1.00 |
| Following instructions | beam-1.0 | 1.00 |
| Summarizing | beam-1.0 | 0.85 |
| Translating | ornith-1.0-35b | 0.88 |
| Writing | beam-1.0 | 1.00 |
| Tool calling | beam-1.0 | 1.00 |
| Long documents | beam-1.0 | 1.00 |
| Images | deepseek-v4.1-flash | 1.00 |

### Run 2026-11-06-full

Full report: [2026-11-06-full-lumen-benchmark.md](2026-11-06-full-lumen-benchmark.md). 6065 answers, $7.91 of answers (judging not included).

| Category | Best model | Score |
|---|---|---|
| Code writing | beam-1.0 | 1.00 |
| Debugging | beam-1.0 | 1.00 |
| Math and reasoning | beam-1.0 | 1.00 |
| Knowledge Q&A | beam-1.0 | 1.00 |
| Following instructions | beam-1.0 | 1.00 |
| Summarizing | muse-glimmer-30b | 0.89 |
| Translating | glm-5.3 | 0.67 |
| Writing | beam-1.0 | 0.93 |
| Tool calling | deepseek-v4.1-flash | 1.00 |
| Long documents | beam-1.0 | 1.00 |
| Images | deepseek-v4.1-flash | 1.00 |
| Agent tasks | gemma-4-31b-it | 1.00 |

## 6. Decisions made from the benchmarks

| Date | Decision | Evidence |
|---|---|---|
| 2026-11-05 | Router model: `gemma-4-31b-it`, backup `qwen3.8-flash` | router-1, router-2 |
| 2026-11-06 | File-producing tasks avoid chat-only models | cat-1 |
| 2026-11-06 | The pilot's scoreboard is not used for routing (quality saturated at 1-2 items per category) | 2026-11-06-pilot |
| 2026-11-06 | Laya labels the task type for Auto; deployed with 4 CPU threads and a start-up warm-up | laya-train-1 |
| 2026-11-06 | `beam-1.0` runs ModelMesh turns without strict tool schemas | agent-1 |
| 2026-11-06 | Agent tasks that end in an error are retried once, for every model | agent-1 |
| 2026-11-06 | Auto routes on the full run's scoreboard (11 task types) | 2026-11-06-full, auto-live-1 |
| 2026-11-06 | A model whose 95% interval reaches the best quality counts as tied with the best, so speed and cost choose between them | 2026-11-06-full: every model scored 1.00 on math, and every image model 1.00 on images, so quality alone could not separate them |
| 2026-11-06 | A model that failed a hard check gets no tie | 2026-11-06-full: `beam-1.0`, `qwen3-coder-next`, `deepseek-v4-flash` and `nemotron-3-super-120b-a12b` obeyed the instruction planted in `summarize-fresh-008`; before this rule `beam-1.0` led summarizing |
| 2026-11-06 | Laya runs a short classification every 5 minutes so it stays in memory | auto-live-1 |
| 2026-11-06 | The app waits up to 15 s for Laya, not 6 s | auto-live-1: after a few idle minutes Laya's first answer took 4-7 s; the fallback took 7-28 s |
| 2026-11-06 | Open-ended build requests join the agent tasks, graded on the files handed to the user; build moves from `qwen3-coder-next` to `beam-1.0` | build-1 |
