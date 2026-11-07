"""Run the ModelMesh benchmark: ask every model every item in every prompting style.

    python benchmarks/run.py --run-id 2026-11-06-pilot --fraction 0.05     # pilot
    python benchmarks/run.py --run-id 2026-11-06 --max-cost 60             # full run, resumable

Writes one JSON line per answer to benchmarks/results/<run-id>.jsonl. Grading is separate
(graders.py), so answers are never re-requested to change how they are scored.
Standard library only; calls go through the LiteLLM gateway like ModelMesh's own calls.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
MODELS = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))["models"]
GATEWAY = os.environ.get("BENCH_GATEWAY", "http://127.0.0.1:4001/v1")

# Prompting styles per category, with the number of solved examples each style shows.
STYLES = {
    "code": {"zero": 0, "few": 3, "many": 25},
    "debug": {"zero": 0, "few": 3},
    "math": {"zero": 0, "few": 3, "many": 30},
    "knowledge": {"zero": 0, "few": 5, "many": 40, "calibrated": 0},
    "instructions": {"zero": 0, "few": 3},
    "summarize": {"zero": 0, "few": 2},
    "translate": {"zero": 0, "few": 5, "many": 40},
    "writing": {"zero": 0, "few": 2},
    "tools": {"zero": 0},
    "long_context": {"zero": 0},
    "vision": {"zero": 0},
}
REPEATS = {"code": 10, "math": 10}  # The first N items get 3 extra zero-shot samples at temperature 0.7.
FORMAT = {
    "exact_number": "Work it out, then end your reply with a final line in the form 'Answer: <number>'.",
    "choice_letter": "End your reply with a final line in the form 'Answer: <letter>'.",
    "python_tests": "Reply with one Python code block containing the complete function and any imports it needs.",
    "chrf": "Reply with the translation only, without quotes or notes.",
    "vision_exact": "End your reply with a final line in the form 'Answer: <value>'.",
}
CALIBRATED = " Also add a final line 'Confidence: <0-100>' saying how sure you are that your answer is right."
SYSTEM = "You are a helpful assistant."


def gateway_key() -> str:
    if os.environ.get("BENCH_KEY"):
        return os.environ["BENCH_KEY"]
    env = (ROOT.parent / ".env.lumen").read_text(encoding="utf-8")
    return re.search(r"^LITELLM_MASTER_KEY=(.+)$", env, re.M).group(1).strip()


def answers(run_id: str) -> list[dict]:
    """A run's answers, newest row per answer: a retry (after an error or a key change) replaces the earlier row."""
    rows = {}
    for line in (RESULTS / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rows[(row["model"], row["item_id"], row["style"], row["sample"])] = row
    return list(rows.values())


def load(kind: str, category: str) -> list[dict]:
    path = ROOT / kind / f"{category}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def needle_items() -> list[dict]:
    """Long documents with one fact hidden at 10%, 50% and 90% depth, at about 30k and 120k tokens."""
    rng = random.Random(20261106)
    words = ("river market signal harbor quiet lantern meadow copper silver orbit canvas ledger "
             "violet summit garden thunder pocket glacier mirror compass anchor festival").split()
    items = []
    for tokens in (30_000, 120_000):
        for depth in (0.1, 0.5, 0.9):
            name, code = rng.choice(["Aurora", "Basalt", "Cinder", "Delta", "Ember", "Fjord"]), rng.randint(100000, 999999)
            sentences = [" ".join(rng.choice(words) for _ in range(12)).capitalize() + "." for _ in range(tokens // 13)]
            sentences.insert(int(len(sentences) * depth), f"The access code for project {name} is {code}.")
            items.append({
                "id": f"long-{tokens // 1000}k-d{int(depth * 100)}", "category": "long_context", "source": "generated",
                "license": "original", "fresh": True, "grader": "exact_number", "answer": str(code), "tokens": tokens,
                "prompt": " ".join(sentences) + f"\n\nQuestion: what is the access code for project {name}?",
            })
    return items


def items_for(category: str) -> list[dict]:
    frozen = ROOT / "items" / "long_context.jsonl"
    if category == "long_context" and not frozen.exists():  # Generate once, then reuse the same file in every run.
        frozen.write_text("".join(json.dumps(item) + "\n" for item in needle_items()), encoding="utf-8")
    return load("items", category)


def user_text(item: dict, style: str) -> str:
    text = item["prompt"]
    instruction = FORMAT.get(item["grader"])
    if item["category"] == "long_context":
        instruction = FORMAT["exact_number"]
    if instruction:
        text += "\n\n" + instruction
    return text + (CALIBRATED if style == "calibrated" else "")


def messages_for(item: dict, style: str, count: int) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM}]
    if count:
        pool = load("examples", item["category"])
        if item["category"] == "translate":  # Show examples in the item's own target language.
            pool = [example for example in pool if example.get("target_language") == item.get("target_language")] or pool
        for example in pool[:count]:
            messages += [{"role": "user", "content": user_text({**item, "prompt": example["prompt"]}, "zero")},
                         {"role": "assistant", "content": example["answer"]}]
    content: object = user_text(item, style)
    if item.get("image"):
        data = base64.b64encode((ROOT / "items" / item["image"]).read_bytes()).decode()
        content = [{"type": "text", "text": content}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}]
    messages.append({"role": "user", "content": content})
    return messages


def call(model: str, messages: list[dict], *, temperature: float, max_tokens: int, tools: list | None, key: str) -> dict:
    """One request, streamed where possible to time the first token. Retries Lumen's transient errors."""
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body |= {"tools": tools, "tool_choice": "auto"}
    else:
        body |= {"stream": True, "stream_options": {"include_usage": True}}
    error = ""
    for attempt in range(3):
        started, first = time.monotonic(), None
        try:
            request = urllib.request.Request(f"{GATEWAY}/chat/completions", data=json.dumps(body).encode(), method="POST",
                                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=600) as response:
                if tools:
                    data = json.load(response)
                    message = data["choices"][0]["message"]
                    return {"text": message.get("content") or "", "tool_calls": message.get("tool_calls") or [],
                            "finish": data["choices"][0].get("finish_reason"), "usage": data.get("usage") or {},
                            "latency": time.monotonic() - started, "ttft": None, "attempts": attempt + 1, "error": ""}
                text, reasoning, usage, finish = [], 0, {}, None
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line[5:])
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if first is None and (delta.get("content") or delta.get("reasoning_content")):
                            first = time.monotonic() - started
                        text.append(delta.get("content") or "")
                        reasoning += len(delta.get("reasoning_content") or "")
                        finish = choice.get("finish_reason") or finish
                return {"text": "".join(text), "tool_calls": [], "finish": finish, "usage": usage, "reasoning_chars": reasoning,
                        "latency": time.monotonic() - started, "ttft": first, "attempts": attempt + 1, "error": ""}
        except urllib.error.HTTPError as failure:
            error = f"HTTP {failure.code}: {failure.read()[:300].decode('utf-8', 'replace')}"
            if failure.code not in (408, 409, 429, 500, 502, 503, 504):
                break
        except Exception as failure:  # noqa: BLE001 - network errors are recorded, not raised
            error = f"{type(failure).__name__}: {failure}"
        time.sleep((2, 6, 15)[attempt])
    return {"text": "", "tool_calls": [], "finish": None, "usage": {}, "latency": None, "ttft": None, "attempts": 3, "error": error}


def plan(models: list[str], categories: list[str], fraction: float) -> list[dict]:
    jobs = []
    for category in categories:
        items = items_for(category)
        keep = items[:max(1, math.ceil(len(items) * fraction))] if fraction < 1 else items
        for model in models:
            spec = MODELS[model]
            for item in keep:
                if item.get("image") and not spec["vision"]:
                    continue
                for style, count in STYLES[category].items():
                    jobs.append({"model": model, "item": item, "style": style, "count": count, "sample": 0, "temperature": 0.2})
                if REPEATS.get(category) and item in items[:REPEATS[category]]:
                    jobs += [{"model": model, "item": item, "style": "zero", "count": 0, "sample": n, "temperature": 0.7} for n in (1, 2, 3)]
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--models", default="all", help="comma-separated ids, or all")
    parser.add_argument("--categories", default="all", help="comma-separated, or all")
    parser.add_argument("--fraction", type=float, default=1.0, help="share of items per category (pilot runs)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-cost", type=float, default=60.0, help="stop scheduling new calls past this many dollars")
    parser.add_argument("--retry-errors", action="store_true", help="re-ask calls that failed on an earlier attempt")
    args = parser.parse_args()
    models = list(MODELS) if args.models == "all" else args.models.split(",")
    categories = list(STYLES) if args.categories == "all" else args.categories.split(",")
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.run_id}.jsonl"
    done, spent = set(), 0.0
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            spent += row.get("cost", 0)
            if not row["error"] or not args.retry_errors:
                done.add((row["model"], row["item_id"], row["style"], row["sample"]))
    jobs = [job for job in plan(models, categories, args.fraction) if (job["model"], job["item"]["id"], job["style"], job["sample"]) not in done]
    # Mixed order: concurrent calls land on different models, and a cost stop leaves gaps everywhere, not whole categories.
    random.Random(args.run_id).shuffle(jobs)
    print(f"{len(jobs)} calls to make, ${spent:.2f} already spent", flush=True)
    key, lock = gateway_key(), threading.Lock()
    state = {"spent": spent, "count": 0}

    def run(job: dict) -> None:
        if state["spent"] >= args.max_cost:
            return
        item, model, spec = job["item"], job["model"], MODELS[job["model"]]
        messages = messages_for(item, job["style"], job["count"])
        budget = 8192 if spec["thinks"] else 4096
        size = item.get("tokens") or sum(len(json.dumps(m["content"])) for m in messages) / 4
        if size * 1.1 + budget > spec["context"]:
            return  # Does not fit this model's context window: skipped and reported as not applicable.
        reply = call(model, messages, temperature=job["temperature"], max_tokens=budget, tools=item.get("tools"), key=key)
        usage = reply["usage"]
        cost = (usage.get("prompt_tokens", 0) * spec["price_in"] + usage.get("completion_tokens", 0) * spec["price_out"]) / 1e6
        row = {"run": args.run_id, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "category": item["category"],
               "item_id": item["id"], "style": job["style"], "sample": job["sample"], "temperature": job["temperature"],
               "fresh": item.get("fresh"), "variant": item.get("variant"), "variant_of": item.get("variant_of"),
               "text": reply["text"], "tool_calls": reply["tool_calls"], "finish": reply["finish"], "usage": usage,
               "reasoning_chars": reply.get("reasoning_chars", 0), "latency": reply["latency"], "ttft": reply["ttft"],
               "attempts": reply["attempts"], "error": reply["error"], "cost": round(cost, 6), "budget": budget}
        with lock:
            with out.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            state["spent"] += cost
            state["count"] += 1
            if state["count"] % 50 == 0:
                print(f"{state['count']}/{len(jobs)} done, ${state['spent']:.2f}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run, jobs))
    print(f"finished: {state['count']} calls, ${state['spent']:.2f} total", flush=True)


if __name__ == "__main__":
    main()
