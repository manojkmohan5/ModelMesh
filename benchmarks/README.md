# ModelMesh model benchmark

Measures how well each Lumen model handles the kinds of tasks people bring to ModelMesh, and
produces the routing scoreboard (`routing-policy.json`) that ModelMesh routes with.
Plan: [docs/plans/smart-routing-v2.md](../docs/plans/smart-routing-v2.md).

## Layout

```
benchmarks/
  items/<category>.jsonl      test items (what we score)
  examples/<category>.jsonl   solved examples, used only to build few-shot and many-shot prompts
  generators/                 code that builds long-document and image items
  run.py                      runs the models, writes results/<run-id>.jsonl (resumable)
  graders.py                  scores answers
  report.py                   writes the report and routing-policy.json
  results/                    raw results, one line per answer
```

## Categories

| id | What it tests | Grader | Test items | Prompting styles |
|---|---|---|---|---|
| `code` | Write a function that passes hidden tests | `python_tests` | 20 | zero, few (3), many (25) |
| `debug` | Fix a function, given the bug report and failing test output | `python_tests` | 15 | zero, few (3) |
| `math` | Word problems with one numeric answer | `exact_number` | 20 | zero, few (3), many (30) |
| `knowledge` | Multiple-choice questions across subjects | `choice_letter` | 20 | zero, few (5), many (40) |
| `instructions` | Follow precise format and length constraints | `constraints` | 20 | zero, few (3) |
| `summarize` | Summarize a passage for a stated reader | `judge` | 10 | zero, few (2) |
| `translate` | English to French, Spanish, German, Hindi or Japanese | `chrf` | 20 | zero, few (5), many (40) |
| `writing` | Emails, product copy, short stories | `judge` | 10 | zero, few (2) |
| `long_context` | Find facts hidden in long documents | `exact_number` | generated | zero |
| `vision` | Questions about generated charts and images | `vision_exact` | generated | zero |
| `agent` | Multi-step tasks inside ModelMesh's computer: 5 with an answer to check, 4 open-ended build requests ("draw a cat") graded on the file the conversation made | `agent_check`, `agent_file` | 9 | zero |
| `tools` | Pick the right tool and arguments, or no tool | `tool_call` | 15 | zero |

## Techniques beyond prompting

| Technique | Applies to | How |
|---|---|---|
| Repeated sampling | `code`, `math` (first 10 items) | 3 extra samples at temperature 0.7: consistency rate and best-of-3 success |
| Robustness | `math`, `knowledge` | Items with `variant_of` set (paraphrase, distractor or typos) are scored like any item; the report compares each variant with its original |
| Calibration | `knowledge` | The `calibrated` style also asks for `Confidence: <0-100>`; the report computes expected calibration error |
| Head-to-head | `summarize`, `writing` | Judges compare two models' answers to the same item; ratings use the Elo method |
| Efficiency | all | Tokens per correct answer, output tokens per second, cost per correct answer |
| Injection resistance | `summarize` items with `"injection": true` | The passage hides an instruction; obeying it fails the item's `not_contains` check |

About a third of the items in every category are new ones written for this benchmark
(`"fresh": true`), because public test sets may be in the models' training data. The report shows
fresh and public items separately.

## Item format

One JSON object per line. Fields used by every item:

| Field | Meaning |
|---|---|
| `id` | Unique id, such as `math-gsm8k-0007` or `debug-fresh-003` |
| `category` | One of the ids above |
| `source` | Where it came from: `gsm8k-test`, `humaneval`, `mmlu-test`, `flores-devtest`, `custom`, ... |
| `license` | License of the source: `MIT`, `CC-BY-4.0`, `CC-BY-SA-4.0`, `Apache-2.0`, or `original` |
| `fresh` | `true` if written for this benchmark, `false` if taken from a public set |
| `prompt` | The task text, with no examples and no answer-format instruction (the harness adds those) |
| `grader` | How the answer is scored (below) |

Grader-specific fields:

| Grader | Fields | How it scores |
|---|---|---|
| `exact_number` | `answer`: the number as a string | 1 if the reply's final `Answer: <number>` equals it, else 0 |
| `choice_letter` | `answer`: `A`, `B`, `C` or `D` | 1 if the reply's final `Answer: <letter>` matches, else 0 |
| `python_tests` | `entry_point`: function name; `tests`: Python source defining `check(candidate)` with asserts | Runs `check(<entry_point>)` on the reply's code in a sandbox (no network, 10 s limit): 1 or 0 |
| `constraints` | `checks`: list of checks (below) | Share of checks passed; also reports all-pass |
| `chrf` | `reference`: reference translation; `target_language` | chrF score from 0 to 1 against the reference |
| `judge` | `rubric`: what a great answer contains; optional `checks` (constraint checks, for example `not_contains` for injection items) | Two judge models score 1 to 10 against the rubric, averaged and scaled to 0 to 1; a failed check scores 0 |
| `tool_call` | `tools`: OpenAI function-tool schemas; `expected`: `{"name": ..., "arguments": {...}}` (listed arguments must match; strings case-insensitive), or `"expected": null` when no tool should be called | 1 if the first tool call matches (or there is none when none is expected), else 0 |

Robustness variants are ordinary items with two extra fields: `variant_of` (the id of the original
item) and `variant` (`paraphrase`, `distractor` or `typos`).

Constraint checks: `{"type": "json"}` (the whole reply is a JSON object),
`{"type": "json_keys", "value": ["a", "b"]}`, `{"type": "max_words", "value": 50}`,
`{"type": "min_words", "value": 20}`, `{"type": "bullet_count", "value": 3}` (lines starting
with `- ` or `* `), `{"type": "line_count", "value": 4}`, `{"type": "paragraph_count", "value": 2}`,
`{"type": "contains", "value": "text"}`, `{"type": "not_contains", "value": "text"}`
(both case-insensitive), `{"type": "starts_with", "value": "text"}`,
`{"type": "ends_with", "value": "text"}`, `{"type": "all_lowercase"}`, `{"type": "all_uppercase"}`,
`{"type": "no_commas"}`, `{"type": "regex", "value": "pattern"}` (must match somewhere).

For `debug` items, `prompt` contains the buggy function, the bug report and the failing test
output; the model must return the complete fixed function, which is graded with `tests`.

## Example format

`examples/<category>.jsonl` holds solved examples: `{"id": ..., "source": ..., "license": ...,
"prompt": ..., "answer": ...}`, where `answer` is an ideal reply in the same format the harness
asks for (for example a reasoning paragraph ending in `Answer: 42`, or one Python code block).
Examples never repeat a test item.

## Prompting styles

- **zero**: the item's prompt plus the answer-format instruction.
- **few** and **many**: the given number of solved examples as earlier user and assistant turns,
  then the item. Many-shot runs only where the prompt fits the model's context window.

Answer-format instructions added by the harness: `exact_number` and `choice_letter` replies end with
a line `Answer: <value>`; `python_tests` replies contain one Python code block with the complete
function; `chrf` replies contain only the translation.

## Fairness rules

Temperature 0.2. Output budget 4,096 tokens, or 8,192 for models that think first; hitting the
budget is recorded. Calls go through the LiteLLM gateway, the same path ModelMesh uses, at most 8 at
a time in a shuffled order, so concurrent calls go to different models; with retries. A model never judges its own answer. Lumen errors count against reliability,
not against quality.
