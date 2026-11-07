"""Turn a scored benchmark run into the report and the routing scoreboard:

    python benchmarks/report.py --run-id 2026-11-06

Writes docs/benchmarks/<run-id>-lumen-benchmark.md, benchmarks/routing-policy.json, and a dated
section in docs/benchmarks/MODEL-BENCHMARKS.md (replaced if the same run is reported again).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import defaultdict

from run import MODELS, RESULTS, ROOT, STYLES, answers

DOCS = ROOT.parent / "docs" / "benchmarks"
WEIGHTS = {"quality": 0.6, "speed": 0.2, "cost": 0.2}
# What Laya decides (task type) and which benchmark categories prove a model is good at it.
TASK_TYPES = {
    "code": ["code", "tools"], "debug": ["debug"], "math": ["math"], "knowledge": ["knowledge"],
    "summarize": ["summarize"], "translate": ["translate"], "writing": ["writing"],
    "build": ["agent", "code", "tools"], "chat": ["knowledge", "writing"],
    "vision": ["vision"], "long_context": ["long_context"],
}
CATEGORY_NAMES = {
    "code": "Code writing", "debug": "Debugging", "math": "Math and reasoning", "knowledge": "Knowledge Q&A",
    "instructions": "Following instructions", "summarize": "Summarizing", "translate": "Translating", "writing": "Writing",
    "long_context": "Long documents", "vision": "Images", "agent": "Agent tasks", "tools": "Tool calling",
}


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def interval(*groups: list[float], rounds: int = 1000) -> tuple[float, float] | None:
    """95% bootstrap interval of the mean of the groups' means (one group: of its plain mean)."""
    if not groups or any(len(group) < 2 for group in groups):
        return None
    rng = random.Random(11)
    means = sorted(mean([mean([rng.choice(group) for _ in group]) for group in groups]) for _ in range(rounds))
    return means[int(rounds * .025)], means[int(rounds * .975)]


def fmt(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def bradley_terry(pairs: list[dict]) -> dict[str, float]:
    """Head-to-head ratings on the Elo scale (mean 1000) from win/tie records."""
    models = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
    wins, games = defaultdict(float), defaultdict(float)
    for pair in pairs:
        if pair["winner"] is None:
            continue
        a, b = pair["a"], pair["b"]
        games[(a, b)] += 1
        games[(b, a)] += 1
        if pair["winner"] == "tie":
            wins[a] += .5
            wins[b] += .5
        else:
            wins[pair["winner"]] += 1
    strength = {model: 1.0 for model in models}
    for _ in range(200):
        for model in models:
            denominator = sum(games[(model, other)] / (strength[model] + strength[other]) for other in models if other != model)
            strength[model] = max(wins[model], .1) / denominator if denominator else strength[model]
        scale = math.exp(statistics.mean(math.log(s) for s in strength.values()))
        strength = {model: s / scale for model, s in strength.items()}
    return {model: 1000 + 400 * math.log10(s) for model, s in strength.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    rows = answers(args.run_id)
    scores = {s["key"]: s for s in map(json.loads, (RESULTS / f"{args.run_id}-scores.jsonl").read_text(encoding="utf-8").splitlines())}
    pairs_path = RESULTS / f"{args.run_id}-pairs.jsonl"
    pairs = [json.loads(line) for line in pairs_path.read_text(encoding="utf-8").splitlines()] if pairs_path.exists() else []
    for row in rows:
        row["score"] = scores.get(f"{row['model']}|{row['item_id']}|{row['style']}|{row['sample']}", {})
    models = sorted({row["model"] for row in rows})
    categories = [c for c in [*STYLES, "agent"] if any(row["category"] == c for row in rows)]

    def cell(model: str, category: str, style: str = "zero", originals: bool = True) -> list[float]:
        return [row["score"]["score"] for row in rows if row["model"] == model and row["category"] == category and row["style"] == style
                and row["sample"] == 0 and row["score"].get("score") is not None and (not originals or not row.get("variant_of"))]

    quality = {(m, c): mean(cell(m, c)) for m in models for c in categories}
    calls = defaultdict(list)
    for row in rows:
        calls[row["model"]].append(row)

    def speed(model: str, cats: list[str] | None = None) -> float | None:
        values = [row["latency"] for row in calls[model] if row["latency"] and (cats is None or row["category"] in cats)]
        return statistics.median(values) if values else None

    def cost_per_call(model: str, cats: list[str] | None = None) -> float | None:
        values = [row["cost"] for row in calls[model] if not row["error"] and (cats is None or row["category"] in cats)]
        return mean(values)

    # A document too long for a model's window is not the model being unreliable.
    counted = {m: [r for r in calls[m] if not (r["category"] == "long_context" and r["error"].startswith("HTTP 400"))] for m in models}
    error_rate = {m: sum(bool(r["error"]) for r in counted[m]) / max(len(counted[m]), 1) for m in models}
    budget_rate = {m: sum(r.get("finish") == "length" for r in calls[m]) / len(calls[m]) for m in models}
    lines = [f"# Lumen model benchmark: {args.run_id}", "",
             f"{len(rows)} answers from {len(models)} models across {len(categories)} categories. Generated by "
             "`benchmarks/report.py`; methods in [benchmarks/README.md](../../benchmarks/README.md); the running record is "
             "[MODEL-BENCHMARKS.md](MODEL-BENCHMARKS.md). Scores run from 0 to 1. Unless a table says otherwise they use "
             "zero-shot prompts on the original items, which is how people write in ModelMesh.", ""]

    overall = {m: mean([q for (mm, c), q in quality.items() if mm == m and q is not None]) for m in models}
    lines += ["## Overall", "", "| Rank | Model | Mean score | Median time | Cost per call | Errors | Hit output budget |", "|---|---|---|---|---|---|---|"]
    for rank, model in enumerate(sorted(models, key=lambda m: -(overall[m] or 0)), 1):
        lines.append(f"| {rank} | {model} | {fmt(overall[model])} | {fmt(speed(model), 1)} s | ${fmt(cost_per_call(model), 5)} | "
                     f"{error_rate[model]:.0%} | {budget_rate[model]:.0%} |")

    lines += ["", "## Winner per category", "", "| Category | Best model | Score (95% interval) | Runner-up | Items |", "|---|---|---|---|---|"]
    for category in categories:
        ranked = sorted((m for m in models if quality[(m, category)] is not None), key=lambda m: -quality[(m, category)])
        if not ranked:
            continue
        best, values = ranked[0], cell(ranked[0], category)
        ci = interval(values)
        runner = f"{ranked[1]} ({fmt(quality[(ranked[1], category)])})" if len(ranked) > 1 else "–"
        lines.append(f"| {CATEGORY_NAMES[category]} | {best} | {fmt(quality[(best, category)])} "
                     f"({fmt(ci[0]) if ci else '–'} to {fmt(ci[1]) if ci else '–'}) | {runner} | {len(values)} |")

    lines += ["", "## Every model in every category", "", "| Model | " + " | ".join(CATEGORY_NAMES[c] for c in categories) + " |",
              "|---|" + "---|" * len(categories)]
    for model in models:
        lines.append(f"| {model} | " + " | ".join(fmt(quality[(model, c)]) for c in categories) + " |")

    lines += ["", "## Prompting styles: change from zero-shot", "",
              "Positive numbers mean examples helped. Many-shot runs only where the prompt fits the model's context window.", "",
              "| Model | Category | Zero | Few | Many | Calibrated |", "|---|---|---|---|---|---|"]
    for model in models:
        for category in categories:
            base = mean(cell(model, category))
            if base is None or len(STYLES.get(category, {})) < 2:
                continue
            delta = lambda style: fmt(mean(cell(model, category, style)) - base, 2) if mean(cell(model, category, style)) is not None else "–"  # noqa: E731
            lines.append(f"| {model} | {CATEGORY_NAMES[category]} | {fmt(base)} | {delta('few')} | {delta('many')} | {delta('calibrated')} |")

    lines += ["", "## Beyond prompting", "", "### Consistency and best-of-3 (code and math, temperature 0.7)", "",
              "| Model | Category | Same outcome as the first answer | Best of 3 | Single answer |", "|---|---|---|---|---|"]
    for model in models:
        for category in ("code", "math"):
            groups = defaultdict(dict)
            for row in calls[model]:
                if row["category"] == category and row["style"] == "zero" and row["score"].get("score") is not None:
                    groups[row["item_id"]][row["sample"]] = row["score"]["score"]
            repeated = {item: s for item, s in groups.items() if len(s) > 1 and 0 in s}
            if repeated:
                same = mean([mean([float(v == s[0]) for k, v in s.items() if k]) for s in repeated.values()])
                best = mean([max(v for k, v in s.items() if k) for s in repeated.values()])
                single = mean([s[0] for s in repeated.values()])
                lines.append(f"| {model} | {CATEGORY_NAMES[category]} | {fmt(same)} | {fmt(best)} | {fmt(single)} |")

    lines += ["", "### Robustness: score on changed wording minus score on the original", "",
              "| Model | Paraphrase | Distractor | Typos |", "|---|---|---|---|"]
    by_id = {(row["model"], row["item_id"], row["style"], row["sample"]): row for row in rows}
    for model in models:
        drops = defaultdict(list)
        for row in calls[model]:
            if row.get("variant_of") and row["style"] == "zero" and row["sample"] == 0 and row["score"].get("score") is not None:
                original = by_id.get((model, row["variant_of"], "zero", 0))
                if original and original["score"].get("score") is not None:
                    drops[row["variant"]].append(row["score"]["score"] - original["score"]["score"])
        if drops:
            lines.append(f"| {model} | " + " | ".join(fmt(mean(drops.get(v, []))) for v in ("paraphrase", "distractor", "typos")) + " |")

    lines += ["", "### Confidence calibration (knowledge, calibrated style)", "",
              "Expected calibration error: 0 means stated confidence matches accuracy; higher is worse.", "",
              "| Model | Accuracy | Mean confidence | Calibration error | Answers |", "|---|---|---|---|---|"]
    for model in models:
        pairs_cc = [(row["score"]["confidence"], row["score"]["score"]) for row in calls[model]
                    if row["style"] == "calibrated" and row["score"].get("confidence") is not None and row["score"].get("score") is not None]
        if pairs_cc:
            bins = defaultdict(list)
            for confidence, correct in pairs_cc:
                bins[min(int(confidence * 10), 9)].append((confidence, correct))
            ece = sum(len(b) / len(pairs_cc) * abs(mean([c for _, c in b]) - mean([p for p, _ in b])) for b in bins.values())
            lines.append(f"| {model} | {fmt(mean([c for _, c in pairs_cc]))} | {fmt(mean([p for p, _ in pairs_cc]))} | {fmt(ece)} | {len(pairs_cc)} |")

    if pairs:
        lines += ["", "### Head-to-head ratings (Elo scale, summaries and writing)", "",
                  "Judges compared two models' answers side by side; a third model judged, never one of the two.", "",
                  "| Model | Summarizing | Writing | Matches |", "|---|---|---|---|"]
        ratings = {c: bradley_terry([p for p in pairs if p["category"] == c]) for c in ("summarize", "writing")}
        for model in sorted(models, key=lambda m: -(ratings["writing"].get(m, 0) + ratings["summarize"].get(m, 0))):
            played = sum(model in (p["a"], p["b"]) for p in pairs)
            lines.append(f"| {model} | {fmt(ratings['summarize'].get(model), 0)} | {fmt(ratings['writing'].get(model), 0)} | {played} |")

    lines += ["", "### Efficiency", "", "| Model | Output tokens per correct answer | Output tokens per second | Cost per correct answer | Median time to first token |",
              "|---|---|---|---|---|"]
    for model in models:
        graded = [r for r in calls[model] if r["score"].get("score") is not None]
        correct = sum(r["score"]["score"] for r in graded)
        tokens = sum((r["usage"] or {}).get("completion_tokens", 0) for r in graded)
        rates = [(r["usage"] or {}).get("completion_tokens", 0) / (r["latency"] - r["ttft"]) for r in graded
                 if r["latency"] and r["ttft"] and r["latency"] - r["ttft"] > .2]
        ttft = [r["ttft"] for r in graded if r["ttft"]]
        lines.append(f"| {model} | {fmt(tokens / correct if correct else None, 0)} | {fmt(statistics.median(rates) if rates else None, 0)} | "
                     f"${fmt(sum(r['cost'] for r in graded) / correct if correct else None, 5)} | {fmt(statistics.median(ttft) if ttft else None, 1)} s |")

    lines += ["", "### Fresh items versus public test items", "",
              "Public test sets may be in the models' training data; a big gap suggests memorization.", "",
              "| Model | Public items | Fresh items |", "|---|---|---|"]
    for model in models:
        public = [r["score"]["score"] for r in calls[model] if r["style"] == "zero" and r["sample"] == 0 and r["fresh"] is False and r["score"].get("score") is not None]
        fresh = [r["score"]["score"] for r in calls[model] if r["style"] == "zero" and r["sample"] == 0 and r["fresh"] is True and r["score"].get("score") is not None]
        lines.append(f"| {model} | {fmt(mean(public))} | {fmt(mean(fresh))} |")

    # The routing scoreboard: per task type, models ranked by quality, speed and cost.
    policy = {"benchmark": args.run_id, "weights": WEIGHTS, "task_types": {}}
    lines += ["", "## Routing scoreboard", "", f"Combined score = {WEIGHTS['quality']:.0%} quality + {WEIGHTS['speed']:.0%} speed + "
              f"{WEIGHTS['cost']:.0%} cost, each scaled 0 to 1 across the models that can do the task, then reduced by the error rate. "
              "A model whose 95% interval reaches the best quality counts as tied with it, so speed and cost decide between them, "
              "unless it failed a hard check (such as obeying an instruction planted in the text).", "",
              "| Task type | First choice | Second | Third |", "|---|---|---|---|"]
    for task, cats in TASK_TYPES.items():
        candidates = []
        for model in models:
            values = [quality[(model, c)] for c in cats if (model, c) in quality and quality[(model, c)] is not None]
            if not values or (task == "vision" and not MODELS[model]["vision"]):
                continue
            candidates.append({"model": model, "quality": mean(values), "latency": speed(model, cats), "cost": cost_per_call(model, cats),
                               "items": sum(len(cell(model, c)) for c in cats)})
        if not candidates:
            continue
        # The plan's tie rule: a model whose 95% interval reaches the best quality is tied with it, so speed and cost
        # decide between them. With 20-50 items per cell, smaller gaps are noise (19/20 vs 20/20 is not a real difference).
        best = max(candidate["quality"] for candidate in candidates)
        for candidate in candidates:
            bounds = interval(*[cell(candidate["model"], c) for c in cats if cell(candidate["model"], c)])
            # A failed hard check (such as obeying an instruction planted in the text) is not noise, so it gets no tie.
            failed = any(row["score"].get("failed_check") for row in calls[candidate["model"]] if row["category"] in cats
                         and row["style"] == "zero" and row["sample"] == 0 and not row.get("variant_of"))
            candidate["tied"] = bool(bounds) and bounds[1] >= best and not failed
            candidate["ranked_quality"] = best if candidate["tied"] else candidate["quality"]

        def scaled(key: str, invert: bool) -> dict[str, float]:
            values = {c["model"]: c[key] for c in candidates if c[key] is not None}
            low, high = min(values.values()), max(values.values())
            return {m: (1.0 if high == low else ((high - v) if invert else (v - low)) / (high - low)) for m, v in values.items()}

        q, s, c = scaled("ranked_quality", False), scaled("latency", True), scaled("cost", True)
        for candidate in candidates:
            m = candidate["model"]
            candidate["combined"] = round((WEIGHTS["quality"] * q.get(m, 0) + WEIGHTS["speed"] * s.get(m, 0) + WEIGHTS["cost"] * c.get(m, 0))
                                          * (1 - error_rate[m]), 4)
        ranked = sorted(candidates, key=lambda c: -c["combined"])
        policy["task_types"][task] = {"categories": cats, "ranking": [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in c.items()} for c in ranked]}
        lines.append(f"| {task} | " + " | ".join(f"{r['model']} (q {fmt(r['quality'])})" for r in ranked[:3]) + " |")

    DOCS.mkdir(parents=True, exist_ok=True)
    report_path = DOCS / f"{args.run_id}-lumen-benchmark.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for target in (ROOT / "routing-policy.json", ROOT.parent / "native/src/modelmesh/routing_policy.json"):  # The router reads the second.
        target.write_text(json.dumps(policy, indent=1) + "\n", encoding="utf-8")

    record = DOCS / "MODEL-BENCHMARKS.md"
    text = record.read_text(encoding="utf-8")
    marker = f"### Run {args.run_id}"
    summary = [marker, "", f"Full report: [{report_path.name}]({report_path.name}). {len(rows)} answers, "
               f"${sum(r['cost'] for r in rows):.2f} of answers (judging not included).", "",
               "| Category | Best model | Score |", "|---|---|---|"]
    for category in categories:
        ranked = sorted((m for m in models if quality[(m, category)] is not None), key=lambda m: -quality[(m, category)])
        if ranked:
            summary.append(f"| {CATEGORY_NAMES[category]} | {ranked[0]} | {fmt(quality[(ranked[0], category)])} |")
    block = "\n".join(summary) + "\n"
    if marker in text:
        start = text.index(marker)
        end = text.find("\n### Run ", start + 1)
        end = text.find("\n## 6.", start) if end == -1 else end
        text = text[:start] + block + text[end:]
    else:
        text = text.replace("\n## 6. Decisions made from the benchmarks", "\n" + block + "\n## 6. Decisions made from the benchmarks")
    record.write_text(text, encoding="utf-8")
    print(f"wrote {report_path} and routing-policy.json; updated MODEL-BENCHMARKS.md")


if __name__ == "__main__":
    main()
