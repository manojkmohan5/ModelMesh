"""Build laya/training.jsonl: labeled English prompts for ModelMesh's task-type classifier.

Three sources, merged and de-duplicated:
- prompts that three Lumen models write for each label (glm-5.3, qwen3.8-flash, gemma-4-31b-it);
- the benchmark items' prompts, labeled by category (benchmarks/items), at most 12 per label;
- the hand-written prompts from the Laya spike (laya/handwritten.jsonl).
A hash of the text puts one row in five in the test split, so reruns keep the split stable.

Run from the repository root with the Lumen gateway up: python laya/make_training.py
"""
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "benchmarks"))
from run import call, gateway_key  # noqa: E402

LABELS = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
WRITERS = ["glm-5.3", "qwen3.8-flash", "gemma-4-31b-it"]
PER_WRITER = 15
CATEGORY_LABELS = {"code": "code", "debug": "debug", "math": "math", "knowledge": "knowledge", "summarize": "summarize",
                   "translate": "translate", "writing": "writing", "instructions": "writing", "agent": "build"}
ASK = """Write {n} different messages that real people might send to an AI assistant. Every message must be this kind of task:

{label}: {definition}

Other kinds, for contrast (do not write these): {others}.

Vary them a lot: from two words to five sentences; casual and formal; a few with typos; some that paste a short snippet of code, \
data or text; many topics and professions. Include a few that look like another kind at first but clearly belong to this one.
Reply with only a JSON array of {n} strings."""


def written(writer: str, label: str, key: str) -> list[str]:
    others = "; ".join(f"{name} ({text})" for name, text in LABELS.items() if name != label)
    prompt = ASK.format(n=PER_WRITER, label=label, definition=LABELS[label], others=others)
    reply = call(writer, [{"role": "user", "content": prompt}], temperature=0.9, max_tokens=8192, tools=None, key=key)
    text = reply["text"]
    try:
        values = json.loads(text[text.index("["):text.rindex("]") + 1])
    except ValueError:
        print(f"{writer}/{label}: no JSON array ({reply['error'] or text[:80]!r})", file=sys.stderr)
        return []
    return [value.strip() for value in values if isinstance(value, str) and 2 <= len(value.strip()) <= 2000]


def main() -> None:
    key = gateway_key()
    rows = [json.loads(line) for line in (HERE / "handwritten.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    for category, label in CATEGORY_LABELS.items():
        items = [json.loads(line) for line in (HERE.parent / "benchmarks/items" / f"{category}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        rows += [{"text": item["prompt"], "label": label, "source": f"benchmark:{category}"} for item in items if not item.get("variant_of")][:12]
    jobs = [(writer, label) for label in LABELS for writer in WRITERS]
    with ThreadPoolExecutor(4) as pool:
        for (writer, label), texts in zip(jobs, pool.map(lambda job: written(*job, key), jobs)):
            rows += [{"text": text, "label": label, "source": f"lumen:{writer}"} for text in texts]
    seen, kept = set(), []
    for row in rows:
        folded = " ".join(row["text"].lower().split())
        if folded not in seen:
            seen.add(folded)
            kept.append(row | {"split": "test" if int(hashlib.sha256(folded.encode()).hexdigest(), 16) % 5 == 0 else "train"})
    with open(HERE / "training.jsonl", "w", encoding="utf-8", newline="\n") as out:
        out.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in kept)
    counts = {label: sum(row["label"] == label for row in kept) for label in LABELS}
    print(f"{len(kept)} rows ({sum(row['split'] == 'test' for row in kept)} test): {counts}")


if __name__ == "__main__":
    main()
