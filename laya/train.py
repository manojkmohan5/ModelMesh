"""Fit ModelMesh's task-type classifier on laya/training.jsonl and check it on the held-out split.

Runs in the modelmesh-laya image, which mounts the laya-models volume at /models:

    docker compose build laya
    docker compose run --rm laya python train.py
    docker compose restart laya

The fitted classifier replaces /models/modelmesh-task-type only when at least 85% of held-out prompts are classified
correctly and fewer than 15% are flagged unsure. Otherwise the deployed one stays and the command exits 1.
"""
import json
import os
import shutil
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import laya

HERE = Path(__file__).resolve().parent
TARGET = Path(os.environ.get("LAYA_CLASSIFIER", "/models/modelmesh-task-type"))
QUESTIONS = {"task_type": {"type": "choice", "instructions": "What kind of task is the user asking an AI assistant to do?",
                           "criteria": json.loads((HERE / "labels.json").read_text(encoding="utf-8"))}}
MIN_ACCURACY, MAX_UNSURE = 0.85, 0.15


def main() -> int:
    rows = [json.loads(line) for line in (HERE / "training.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    train = [{"state": row["text"], "gold": {"task_type": row["label"]}} for row in rows if row["split"] == "train"]
    test = [row for row in rows if row["split"] == "test"]
    agent = laya.Agent(vegaml=True)
    started = time.monotonic()
    agent.fit(train, questions=QUESTIONS)
    fit_s = time.monotonic() - started
    agent.predict("warm up", QUESTIONS)
    results, latencies, misses = defaultdict(lambda: [0, 0]), [], []
    sure_right = sure = unsure = 0
    for row in test:
        started = time.monotonic()
        answer = agent.predict(row["text"], QUESTIONS)["answers"]["task_type"]
        latencies.append((time.monotonic() - started) * 1000)
        right, flagged = answer["choice"] == row["label"], answer.get("route") == "review"
        unsure += flagged
        sure += not flagged
        sure_right += right and not flagged
        group = results[row["source"].split(":")[0]]
        group[0] += right
        group[1] += 1
        if not right:
            misses.append({"text": row["text"][:80], "expected": row["label"], "got": answer["choice"],
                           "confidence": answer.get("answer_confidence"), "unsure": flagged})
    accuracy = sum(right for right, _ in results.values()) / len(test)
    report = {"laya_version": getattr(laya, "__version__", "?"), "train_rows": len(train), "test_rows": len(test),
              "fit_s": round(fit_s), "accuracy": round(accuracy, 3), "unsure_rate": round(unsure / len(test), 3),
              "accuracy_when_sure": round(sure_right / max(sure, 1), 3),
              "by_source": {source: f"{right}/{total}" for source, (right, total) in sorted(results.items())},
              "latency_ms_median": round(statistics.median(latencies)), "latency_ms_max": round(max(latencies)), "misses": misses}
    print(json.dumps(report, indent=1))
    if accuracy < MIN_ACCURACY or unsure / len(test) >= MAX_UNSURE:
        print(f"Not deployed: needs {MIN_ACCURACY:.0%} accuracy and under {MAX_UNSURE:.0%} unsure.", file=sys.stderr)
        return 1
    fresh = TARGET.with_name(TARGET.name + ".new")
    shutil.rmtree(fresh, ignore_errors=True)
    agent.save_calibration(str(fresh))
    (fresh / "questions.json").write_text(json.dumps(QUESTIONS), encoding="utf-8")
    (fresh / "training-report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    shutil.rmtree(TARGET, ignore_errors=True)
    fresh.rename(TARGET)
    print(f"Deployed to {TARGET}. Restart the laya service to load it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
