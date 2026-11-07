"""Score a benchmark run's answers: python benchmarks/graders.py --run-id <run-id>

Reads results/<run-id>.jsonl and writes results/<run-id>-scores.jsonl (one score per answer) and
results/<run-id>-pairs.jsonl (head-to-head judgments). Code runs only inside a throwaway Docker
container with no network. Judge calls go through the gateway and are resumable.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import tempfile
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from run import RESULTS, answers, call, gateway_key, items_for

JUDGES = ["glm-5.3", "qwen3.8-27b", "deepseek-v4-flash"]  # The first two judge; the third stands in for a judged judge.
ANSWER = re.compile(r"answer\s*[:：]\s*\**\s*\(?\$?\s*([^\n*]+)", re.I)


def final_value(text: str) -> str | None:
    found = ANSWER.findall(text or "")
    return found[-1].strip().rstrip(".") if found else None


def number(value: str | None) -> float | None:
    match = re.search(r"-?\d[\d,]*\.?\d*|-?\.\d+", value or "")
    return float(match.group().replace(",", "")) if match else None


def grade_number(item: dict, text: str) -> tuple[float, bool]:
    value = final_value(text)
    got = number(value) if value is not None else number((re.findall(r"-?\d[\d,]*\.?\d*", text or "") or [None])[-1])
    return float(got is not None and abs(got - float(item["answer"].replace(",", ""))) < 1e-6), value is not None


def grade_letter(item: dict, text: str) -> tuple[float, bool]:
    value = final_value(text)
    letter = re.search(r"\b([ABCD])\b", value or "")
    return float(bool(letter) and letter.group(1) == item["answer"]), bool(letter)


def words(text: str) -> list[str]:
    return re.findall(r"\b[\w'-]+\b", text)


def check(rule: dict, text: str) -> bool:
    kind, value, stripped = rule["type"], rule.get("value"), (text or "").strip()
    if kind in ("json", "json_keys"):
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped)
        try:
            data = json.loads(body)
        except ValueError:
            return False
        return isinstance(data, dict) and (kind == "json" or all(key in data for key in value))
    lines = [line for line in stripped.splitlines() if line.strip()]
    return {
        "max_words": lambda: len(words(stripped)) <= value,
        "min_words": lambda: len(words(stripped)) >= value,
        "bullet_count": lambda: sum(line.lstrip().startswith(("- ", "* ")) for line in lines) == value,
        "line_count": lambda: len(lines) == value,
        "paragraph_count": lambda: len([p for p in re.split(r"\n\s*\n", stripped) if p.strip()]) == value,
        "contains": lambda: str(value).lower() in stripped.lower(),
        "not_contains": lambda: str(value).lower() not in stripped.lower(),
        "starts_with": lambda: stripped.lower().startswith(str(value).lower()),
        "ends_with": lambda: stripped.lower().endswith(str(value).lower()),
        "all_lowercase": lambda: stripped == stripped.lower(),
        "all_uppercase": lambda: stripped == stripped.upper(),
        "no_commas": lambda: "," not in stripped,
        "regex": lambda: re.search(value, stripped) is not None,
    }[kind]()


def chrf(hypothesis: str, reference: str, order: int = 6, beta: float = 2.0) -> float:
    """chrF (Popović 2015): character n-gram F-score, whitespace removed, as in sacrebleu."""
    hyp, ref = re.sub(r"\s+", "", hypothesis or ""), re.sub(r"\s+", "", reference or "")
    precisions, recalls = [], []
    for n in range(1, order + 1):
        h, r = Counter(hyp[i:i + n] for i in range(len(hyp) - n + 1)), Counter(ref[i:i + n] for i in range(len(ref) - n + 1))
        if not h or not r:
            continue
        overlap = sum((h & r).values())
        precisions.append(overlap / sum(h.values()))
        recalls.append(overlap / sum(r.values()))
    if not precisions:
        return 0.0
    p, r = sum(precisions) / len(precisions), sum(recalls) / len(recalls)
    return 0.0 if p + r == 0 else (1 + beta ** 2) * p * r / (beta ** 2 * p + r)


def grade_tool(item: dict, row: dict) -> float:
    calls, expected = row.get("tool_calls") or [], item["expected"]
    if expected is None:
        return float(not calls)
    if not calls:
        return 0.0
    function = calls[0].get("function") or {}
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except ValueError:
        return 0.0
    same = lambda a, b: a.strip().lower() == b.strip().lower() if isinstance(a, str) and isinstance(b, str) else a == b  # noqa: E731
    return float(function.get("name") == expected["name"]
                 and all(same(arguments.get(key), value) for key, value in expected.get("arguments", {}).items()))


def code_block(text: str, entry: str) -> str:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text or "", re.S)
    return next((block for block in blocks if f"def {entry}" in block), blocks[0] if blocks else text or "")


RUNNER = """
import json, pathlib, subprocess, sys
for path in sorted(pathlib.Path('/w').glob('*.py')):
    if path.name == 'runner.py':
        continue
    try:
        done = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=10)
        ok = done.returncode == 0 and 'BENCH-PASS' in done.stdout
        detail = (done.stderr or '')[-300:]
    except subprocess.TimeoutExpired:
        ok, detail = False, 'timeout'
    print(json.dumps({'file': path.stem, 'pass': ok, 'detail': detail}))
"""


def run_python(cases: dict[str, tuple[str, dict]]) -> dict[str, tuple[bool, str]]:
    """Run each answer's code against its tests inside one offline, throwaway container."""
    outcome = {}
    with tempfile.TemporaryDirectory() as folder:
        for name, (code, item) in cases.items():
            program = f"{code}\n\n{item['tests']}\n\ncheck({item['entry_point']})\nprint('BENCH-PASS')\n"
            (Path(folder) / f"{name}.py").write_text(program, encoding="utf-8")
        (Path(folder) / "runner.py").write_text(RUNNER, encoding="utf-8")
        done = subprocess.run(["docker", "run", "--rm", "--network", "none", "--memory", "1g", "--pids-limit", "256", "--cpus", "2",
                               "-v", f"{folder}:/w:ro", "modelmesh-workspace", "python", "/w/runner.py"],
                              capture_output=True, text=True, timeout=60 + 12 * len(cases), env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        for line in done.stdout.splitlines():
            if line.startswith("{"):
                result = json.loads(line)
                outcome[result["file"]] = (result["pass"], result["detail"])
    return outcome


def judge_prompt(item: dict, answer: str) -> str:
    return (f"You are grading an AI assistant's answer.\n\nTask given to the assistant:\n<task>\n{item['prompt']}\n</task>\n\n"
            f"What a great answer does:\n{item['rubric']}\n\nThe assistant's answer:\n<answer>\n{answer}\n</answer>\n\n"
            "Grade strictly from 1 (useless) to 10 (excellent). Reply with one short reason, then a final line 'Score: <1-10>'.")


def pair_prompt(item: dict, first: str, second: str) -> str:
    return (f"Two AI assistants answered the same task.\n\nTask:\n<task>\n{item['prompt']}\n</task>\n\nWhat a great answer does:\n"
            f"{item['rubric']}\n\nAnswer A:\n<answer_a>\n{first}\n</answer_a>\n\nAnswer B:\n<answer_b>\n{second}\n</answer_b>\n\n"
            "Which answer is better? Ignore length unless it hurts the task. Reply with one short reason, then a final line "
            "'Winner: A', 'Winner: B' or 'Winner: tie'.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--matches", type=int, default=4, help="head-to-head opponents per model and item")
    args = parser.parse_args()
    rows = answers(args.run_id)
    items = {item["id"]: item for category in {row["category"] for row in rows} for item in items_for(category)}
    scores_path, pairs_path = RESULTS / f"{args.run_id}-scores.jsonl", RESULTS / f"{args.run_id}-pairs.jsonl"
    previous = {}
    if scores_path.exists():
        for line in scores_path.read_text(encoding="utf-8").splitlines():
            score = json.loads(line)
            previous[score["key"]] = score
    key, lock, scores, code_cases = gateway_key(), threading.Lock(), {}, {}

    def key_of(row: dict) -> str:
        return f"{row['model']}|{row['item_id']}|{row['style']}|{row['sample']}"

    for row in rows:
        item, name = items[row["item_id"]], key_of(row)
        base = {"key": name, "model": row["model"], "category": row["category"], "item_id": row["item_id"], "style": row["style"],
                "sample": row["sample"], "fresh": row.get("fresh"), "variant": row.get("variant"), "variant_of": row.get("variant_of"),
                "error": bool(row["error"]), "hit_budget": row.get("finish") == "length"}
        if row["error"]:
            scores[name] = {**base, "score": None}
            continue
        grader, text = item["grader"], row["text"]
        if grader == "python_tests":
            code_cases[f"c{len(code_cases):05d}"] = (code_block(text, item["entry_point"]), item, name, base)
            continue
        if grader == "judge":
            scores[name] = previous.get(name) if previous.get(name, {}).get("score") is not None else {**base, "score": None, "pending": True}
            continue
        extra = {}
        if grader in ("exact_number",):
            score, extra["format_ok"] = grade_number(item, text)
        elif grader == "choice_letter":
            score, extra["format_ok"] = grade_letter(item, text)
            confidence = re.findall(r"confidence\s*[:：]\s*(\d{1,3})", text or "", re.I)
            if row["style"] == "calibrated" and confidence:
                extra["confidence"] = min(100, int(confidence[-1])) / 100
        elif grader == "constraints":
            passed = [check(rule, text) for rule in item["checks"]]
            score, extra["all_pass"] = sum(passed) / len(passed), all(passed)
        elif grader == "chrf":
            score = chrf(text, item["reference"])
        elif grader == "tool_call":
            score = grade_tool(item, row)
        elif grader == "agent_check":
            score = float(any(str(answer).lower() in (text or "").lower() for answer in [item["answer"], *item.get("accept", [])]))
        elif grader == "agent_file":  # Passes when the conversation made a file of the asked-for kind, as its Files panel shows.
            score = float(any(re.search(item["file"], made.get("name") or "", re.I) and (made.get("size") or 0) >= item.get("min_bytes", 200)
                              for made in row.get("files") or []))
        elif grader == "vision_exact":
            value = (final_value(text) or "").lower().strip(" .")
            score = float(value in [str(answer).lower() for answer in [item["answer"], *item.get("accept", [])]])
        else:
            raise ValueError(f"unknown grader {grader}")
        scores[name] = {**base, "score": score, **extra}

    if code_cases:
        print(f"running {len(code_cases)} code answers in the sandbox", flush=True)
        names = list(code_cases)
        for start in range(0, len(names), 60):
            batch = {name: (code_cases[name][0], code_cases[name][1]) for name in names[start:start + 60]}
            outcome = run_python(batch)
            for name in batch:
                _, _, row_key, base = code_cases[name]
                passed, detail = outcome.get(name, (False, "no result"))
                scores[row_key] = {**base, "score": float(passed), "detail": detail[-200:]}

    pending = [(name, score) for name, score in scores.items() if score.get("pending")]
    if pending and not args.no_judge:
        by_key = {key_of(row): row for row in rows}
        print(f"judging {len(pending)} open-ended answers with 2 judges each", flush=True)

        def judge_one(entry: tuple[str, dict]) -> None:
            name, score = entry
            row, item = by_key[name], items[score["item_id"]]
            if any(not check(rule, row["text"]) for rule in item.get("checks", [])):
                result = {**score, "score": 0.0, "pending": False, "judged": [], "failed_check": True}
            else:
                judges = [judge for judge in JUDGES if judge != row["model"]][:2]
                marks, failed = [], False
                for judge in judges:
                    reply = call(judge, [{"role": "user", "content": judge_prompt(item, row["text"])}], temperature=0, max_tokens=8192, tools=None, key=key)
                    failed |= bool(reply["error"])  # e.g. budget out: leave unscored so the next run judges it again
                    found = re.findall(r"score\s*[:：]\s*(\d{1,2})", reply["text"] or "", re.I)
                    if found:
                        marks.append(min(10, int(found[-1])))
                result = {**score, "score": sum(marks) / len(marks) / 10 if marks and not failed else None, "pending": False, "judged": list(zip(judges, marks))}
            with lock:
                scores[name] = result

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(judge_one, pending))

    with scores_path.open("w", encoding="utf-8") as handle:
        for score in scores.values():
            handle.write(json.dumps(score, ensure_ascii=False) + "\n")
    print(f"wrote {len(scores)} scores to {scores_path.name}", flush=True)

    if args.no_judge:
        return
    # Head-to-head: each model meets a few random opponents per open-ended item, judged by a third model.
    done_pairs = set()
    if pairs_path.exists():
        for line in pairs_path.read_text(encoding="utf-8").splitlines():
            pair = json.loads(line)
            done_pairs.add((pair["item_id"], *sorted((pair["a"], pair["b"]))))
    texts = defaultdict(dict)
    for row in rows:
        if items[row["item_id"]]["grader"] == "judge" and row["style"] == "zero" and row["sample"] == 0 and not row["error"] and row["text"].strip():
            texts[row["item_id"]][row["model"]] = row["text"]
    rng, matches = random.Random(7), []
    for item_id, by_model in texts.items():
        models = sorted(by_model)
        for model in models:
            for opponent in rng.sample([m for m in models if m != model], min(args.matches, len(models) - 1)):
                pair = (item_id, *sorted((model, opponent)))
                if pair not in done_pairs:
                    done_pairs.add(pair)
                    matches.append(pair)
    print(f"{len(matches)} head-to-head matches to judge", flush=True)

    def judge_pair(pair: tuple[str, str, str]) -> None:
        item_id, first, second = pair
        if rng.random() < .5:
            first, second = second, first
        judge = next(judge for judge in JUDGES if judge not in (first, second))
        reply = call(judge, [{"role": "user", "content": pair_prompt(items[item_id], texts[item_id][first], texts[item_id][second])}],
                     temperature=0, max_tokens=8192, tools=None, key=key)
        if reply["error"]:
            return  # Not recorded, so the next run plays this match again.
        verdict = (re.findall(r"winner\s*[:：]\s*(a|b|tie)", reply["text"] or "", re.I) or [None])[-1]
        result = {"item_id": item_id, "category": items[item_id]["category"], "a": first, "b": second, "judge": judge,
                  "winner": {"a": first, "b": second, "tie": "tie"}.get((verdict or "").lower())}
        with lock:
            with pairs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result) + "\n")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(judge_pair, matches))
    print("head-to-head judging finished", flush=True)


if __name__ == "__main__":
    main()
