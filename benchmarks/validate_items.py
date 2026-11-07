"""Check the benchmark item and example files against benchmarks/README.md.

    python -I benchmarks/validate_items.py

Static checks: required fields, graders, licenses, unique ids, constraint checks, answer
formats, robustness variants, tool schemas, and that no example repeats a test item.
Then every record with Python tests runs inside Docker with no network (image
modelmesh-workspace): item solutions and code-example answers must pass `check`, while a code
item's prompt alone (a stub that returns None) and a debug item's buggy function must fail.
Exits with status 1 if any problem is found.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Mirrors the Categories table in README.md: grader, number of test items (None when
# generated), and the most solved examples any prompting style uses.
CATEGORIES = {
    "code": ("python_tests", 20, 25),
    "debug": ("python_tests", 15, 3),
    "math": ("exact_number", 20, 30),
    "knowledge": ("choice_letter", 20, 40),
    "instructions": ("constraints", 20, 3),
    "summarize": ("judge", 10, 2),
    "translate": ("chrf", 20, 40),
    "writing": ("judge", 10, 2),
    "long_context": ("needle", None, 0),
    "vision": ("vision_exact", None, 0),
    "agent": ("agent_check", 9, 0),
    "tools": ("tool_call", 15, 3),
}
# Open-ended build tasks ("draw a cat") are graded on the file the conversation made, not on the reply.
ALSO = {"agent": {"agent_file"}}
# README marks these as generated: their files come from code, which owns the grader choice.
GENERATED = {"long_context", "vision"}
LICENSES = {"MIT", "CC-BY-4.0", "CC-BY-SA-4.0", "Apache-2.0", "original"}
ID = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
VARIANTS = {"paraphrase", "distractor", "typos"}
LANGUAGES = {"French", "Spanish", "German", "Hindi", "Japanese"}
NO_VALUE_CHECKS = {"json", "all_lowercase", "all_uppercase", "no_commas"}
INT_CHECKS = {"max_words", "min_words", "bullet_count", "line_count", "paragraph_count"}
TEXT_CHECKS = {"contains", "not_contains", "starts_with", "ends_with", "regex"}
JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
NUMBER = re.compile(r"-?\d+(\.\d+)?")
CODE_BLOCK = re.compile(r"```python\n(.*?)\n```", re.S)
DOCKER = ["docker", "run", "--rm", "--network", "none", "--memory", "512m", "--pids-limit", "128"]
IMAGE = "modelmesh-workspace"
TIMEOUT_S = 10  # README: python_tests run with a 10 s limit

problems, notes = [], []


def bad(where, message):
    problems.append(f"{where}: {message}")


def text_field(rec, field):
    value = rec.get(field)
    return value if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------- constraint checks (README)
def json_object(text):
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def check_passes(check, reply):
    """Strict reading of each README check, so a reply that passes here passes any fair grader."""
    kind, value = check["type"], check.get("value")
    text = reply.strip()
    lines = [line for line in text.splitlines() if line.strip()]
    # Words counted two common ways: whitespace-separated, and runs of word characters.
    word_counts = (len(text.split()), len(re.findall(r"\b[\w'-]+\b", text)))
    if kind == "json":
        return json_object(text) is not None
    if kind == "json_keys":
        obj = json_object(text)
        return obj is not None and set(obj) == set(value)
    if kind == "max_words":
        return max(word_counts) <= value
    if kind == "min_words":
        return min(word_counts) >= value
    if kind == "bullet_count":
        return sum(line.lstrip().startswith(("- ", "* ")) for line in lines) == value
    if kind == "line_count":
        return len(lines) == value
    if kind == "paragraph_count":
        return len([p for p in re.split(r"\n\s*\n", text) if p.strip()]) == value
    if kind == "contains":
        return value.lower() in text.lower()
    if kind == "not_contains":
        return value.lower() not in text.lower()
    if kind == "starts_with":
        return text.startswith(value)
    if kind == "ends_with":
        return text.endswith(value)
    if kind == "all_lowercase":
        return text == text.lower()
    if kind == "all_uppercase":
        return text == text.upper()
    if kind == "no_commas":
        return "," not in text
    return re.search(value, text) is not None  # regex


def valid_checks(where, checks):
    if not isinstance(checks, list) or not checks:
        bad(where, "checks must be a non-empty list")
        return False
    ok = True
    for check in checks:
        kind = check.get("type") if isinstance(check, dict) else None
        value = check.get("value") if isinstance(check, dict) else None
        if kind in NO_VALUE_CHECKS:
            fine = "value" not in check
        elif kind in INT_CHECKS:
            fine = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        elif kind in TEXT_CHECKS:
            fine = isinstance(value, str) and value != ""
            if fine and kind == "regex":
                try:
                    re.compile(value)
                except re.error:
                    fine = False
        elif kind == "json_keys":
            fine = isinstance(value, list) and value and all(isinstance(k, str) for k in value)
        else:
            fine = False
        if not fine:
            bad(where, f"invalid check {check!r}")
            ok = False
    return ok


# ---------------------------------------------------------------- per-grader checks
def options_ok(prompt):
    return re.findall(r"^([A-D])\) \S", prompt, re.M) == ["A", "B", "C", "D"]


def type_ok(schema, value):
    expected = JSON_TYPES.get(schema.get("type"))
    if expected is None:
        return True
    if isinstance(value, bool) and schema.get("type") in ("integer", "number"):
        return False
    return isinstance(value, expected)


def check_tools(where, rec):
    tools = rec.get("tools")
    if not isinstance(tools, list) or not 2 <= len(tools) <= 4:
        bad(where, "tools must list 2 to 4 function tools")
        tools = tools if isinstance(tools, list) else []
    params_by_name = {}
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or tool.get("type") != "function":
            bad(where, "each tool must be {'type': 'function', 'function': {...}}")
            continue
        name, params = fn.get("name"), fn.get("parameters")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            bad(where, f"invalid tool name {name!r}")
            continue
        if not text_field(fn, "description"):
            bad(where, f"tool {name} needs a description")
        if not (isinstance(params, dict) and params.get("type") == "object"
                and isinstance(params.get("properties"), dict)):
            bad(where, f"tool {name} parameters must be a JSON schema object with properties")
            continue
        if not set(params.get("required", [])) <= set(params["properties"]):
            bad(where, f"tool {name} requires a parameter it does not define")
        if name in params_by_name:
            bad(where, f"duplicate tool {name}")
        params_by_name[name] = params["properties"]
    if "expected" not in rec:
        bad(where, "expected is required (null when no tool should be called)")
        return
    expected = rec["expected"]
    if expected is None:
        return
    if not (isinstance(expected, dict) and set(expected) == {"name", "arguments"}
            and isinstance(expected["arguments"], dict)):
        bad(where, "expected must be {'name': ..., 'arguments': {...}} or null")
        return
    props = params_by_name.get(expected["name"])
    if props is None:
        bad(where, f"expected tool {expected['name']!r} is not one of the tools")
        return
    for arg, value in expected["arguments"].items():
        schema = props.get(arg)
        if schema is None:
            bad(where, f"expected argument {arg!r} is not a parameter of {expected['name']}")
        elif not type_ok(schema, value) or ("enum" in schema and value not in schema["enum"]):
            bad(where, f"expected argument {arg}={value!r} does not fit its schema")


def check_item(where, rec, category, runs):
    for field, kind in (("id", str), ("category", str), ("source", str), ("license", str),
                        ("fresh", bool), ("prompt", str), ("grader", str)):
        if not isinstance(rec.get(field), kind) or (kind is str and not rec[field].strip()):
            bad(where, f"missing or invalid {field!r}")
            return
    grader = CATEGORIES[category][0]
    where = f"{where} {rec['id']}"
    if rec["category"] != category:
        bad(where, f"category {rec['category']!r} does not match the file")
    if rec["grader"] != grader and rec["grader"] not in ALSO.get(category, ()):
        if category in GENERATED:
            notes.append(f"{where}: generated item uses grader {rec['grader']!r}; README.md lists {grader!r}")
        else:
            bad(where, f"grader must be {grader!r}")
    if rec["license"] not in LICENSES:
        bad(where, f"unknown license {rec['license']!r}")
    if rec["fresh"] != (rec["license"] == "original"):
        bad(where, "fresh must be true exactly when the license is 'original'")
    if not ID.fullmatch(rec["id"]):
        bad(where, "id must be lowercase letters, digits and hyphens, such as math-gsm8k-0007")
    grader = rec["grader"] if category in GENERATED or rec["grader"] in ALSO.get(category, ()) else grader
    prompt = rec["prompt"]

    if grader == "exact_number":
        if not (isinstance(rec.get("answer"), str) and NUMBER.fullmatch(rec["answer"])):
            bad(where, "answer must be a number written as a string")
        if "Answer:" in prompt:
            bad(where, "prompt must not contain the answer-format instruction")
    elif grader == "choice_letter":
        if rec.get("answer") not in ("A", "B", "C", "D"):
            bad(where, "answer must be A, B, C or D")
        if not options_ok(prompt):
            bad(where, "prompt must list the options as lines 'A) ' to 'D) '")
        if "Answer:" in prompt:
            bad(where, "prompt must not contain the answer-format instruction")
    elif grader == "python_tests":
        entry, tests, solution = (text_field(rec, f) for f in ("entry_point", "tests", "solution"))
        if not (entry and tests and solution):
            bad(where, "python_tests items need entry_point, tests and solution")
            return
        if "def check(candidate)" not in tests:
            bad(where, "tests must define check(candidate)")
        runs.append((where, program(solution, tests, entry), True))
        if category == "debug":
            buggy = CODE_BLOCK.search(prompt)
            if not (buggy and f"def {entry}(" in buggy.group(1)):
                bad(where, "prompt must show the buggy function in a ```python block")
            else:
                runs.append((f"{where} (buggy code)", program(buggy.group(1), tests, entry), False))
            if "Bug report:" not in prompt or "Failing test output:" not in prompt:
                bad(where, "prompt must contain the bug report and the failing test output")
        else:
            if f"def {entry}(" not in prompt:
                bad(where, f"prompt must contain def {entry}(")
            runs.append((f"{where} (prompt stub)", program(prompt, tests, entry), False))
    elif grader == "constraints":
        checks = rec.get("checks")
        if valid_checks(where, checks):
            if not 2 <= len(checks) <= 4:
                bad(where, "instructions items use 2 to 4 checks")
            solution = text_field(rec, "solution")
            if solution is None:
                bad(where, "needs a solution: a reference reply that shows the checks can all pass")
            else:
                for check in checks:
                    if not check_passes(check, solution):
                        bad(where, f"solution fails {check}, so the item may not be satisfiable")
    elif grader == "chrf":
        if not text_field(rec, "reference"):
            bad(where, "reference translation is required")
        if rec.get("target_language") not in LANGUAGES:
            bad(where, f"target_language must be one of {sorted(LANGUAGES)}")
        elif rec["target_language"] not in prompt:
            bad(where, "prompt must name the target language")
    elif grader == "judge":
        if not text_field(rec, "rubric"):
            bad(where, "rubric is required")
        if "checks" in rec:
            valid_checks(where, rec["checks"])
        if category == "summarize":
            words = len(prompt.split("\n\n", 1)[-1].split())
            if not 250 <= words <= 500:
                bad(where, f"passage has {words} words; it must have 250 to 500")
        if rec.get("injection") is True:
            hidden = [c.get("value") for c in rec.get("checks", []) if c.get("type") == "not_contains"]
            if not hidden or not all(isinstance(h, str) and h in prompt for h in hidden):
                bad(where, "injection items need a not_contains check for the word the passage asks for")
        elif "injection" in rec:
            bad(where, "injection must be true when present")
    elif grader == "tool_call":
        check_tools(where, rec)
    elif grader == "agent_file":
        try:
            re.compile(rec.get("file") or "")
        except re.error:
            bad(where, "file must be a regular expression for the file name")
        if not text_field(rec, "file"):
            bad(where, "agent_file items need a file pattern, such as \\.(svg|png)$")


def check_variants(path, records, category):
    by_id = {rec.get("id"): rec for _, rec in records}
    for where, rec in records:
        if "variant_of" not in rec and "variant" not in rec:
            continue
        where = f"{where} {rec.get('id')}"
        if category not in ("math", "knowledge"):
            bad(where, "robustness variants are only used for math and knowledge")
        if rec.get("variant") not in VARIANTS:
            bad(where, f"variant must be one of {sorted(VARIANTS)}")
        orig = by_id.get(rec.get("variant_of"))
        if orig is None or "variant_of" in orig:
            bad(where, "variant_of must name an original item in the same file")
        elif orig.get("answer") != rec.get("answer"):
            bad(where, "a variant must keep the original's answer")
        elif orig.get("prompt") == rec.get("prompt"):
            bad(where, "a variant must change the prompt")


# ---------------------------------------------------------------- examples
def check_example(where, rec, category, runs):
    for field in ("id", "source", "license", "prompt", "answer"):
        if not text_field(rec, field):
            bad(where, f"missing or invalid {field!r}")
            return
    where = f"{where} {rec['id']}"
    if rec["license"] not in LICENSES:
        bad(where, f"unknown license {rec['license']!r}")
    if "fresh" in rec and rec["fresh"] is not (rec["license"] == "original"):
        bad(where, "fresh must be true exactly when the license is 'original'")
    if not ID.fullmatch(rec["id"]):
        bad(where, "id must be lowercase letters, digits and hyphens")
    answer = rec["answer"]
    last = answer.strip().splitlines()[-1]
    grader = CATEGORIES[category][0]
    if grader == "python_tests":
        block = CODE_BLOCK.fullmatch(answer.strip())
        if not block or answer.count("```") != 2:
            bad(where, "answer must be exactly one ```python code block")
        elif "tests" in rec:  # optional: lets us prove the example answer is correct
            entry = text_field(rec, "entry_point")
            runs.append((where, program(block.group(1), rec["tests"], entry), True))
            buggy = CODE_BLOCK.search(rec["prompt"]) if category == "debug" else None
            if buggy:
                runs.append((f"{where} (buggy code)", program(buggy.group(1), rec["tests"], entry), False))
    elif grader == "exact_number":
        if not (re.fullmatch(r"Answer: -?\d+(\.\d+)?", last) and len(answer.strip().splitlines()) > 1):
            bad(where, "answer must be a worked solution ending with the line 'Answer: <number>'")
    elif grader == "choice_letter":
        if not re.fullmatch(r"Answer: [ABCD]", last):
            bad(where, "answer must end with the line 'Answer: <letter>'")
        if not options_ok(rec["prompt"]):
            bad(where, "prompt must list the options as lines 'A) ' to 'D) '")
    elif grader == "chrf":
        if rec.get("target_language") not in LANGUAGES:
            bad(where, f"target_language must be one of {sorted(LANGUAGES)}")
        if "```" in answer:
            bad(where, "answer must contain only the translation")
    elif grader == "constraints":
        if valid_checks(where, rec.get("checks")):
            for check in rec["checks"]:
                if not check_passes(check, answer):
                    bad(where, f"answer fails its own check {check}")


def word_set(text):
    return set(re.findall(r"\w+", text.lower()))


def check_overlap(category, items, examples):
    """Examples must never repeat or paraphrase a test item: flag heavy word overlap."""
    item_words = [(rec.get("id"), rec.get("prompt", ""), word_set(rec.get("prompt", ""))) for _, rec in items]
    for where, ex in examples:
        prompt = ex.get("prompt", "")
        words = word_set(prompt)
        for item_id, item_prompt, other in item_words:
            overlap = len(words & other) / max(1, len(words | other))
            if prompt == item_prompt or overlap >= 0.6:
                bad(f"{where} {ex.get('id')}", f"looks like test item {item_id} (word overlap {overlap:.2f})")


# ---------------------------------------------------------------- Docker test runs
def program(code, tests, entry_point):
    return f"{code}\n\n{tests}\n\ncheck({entry_point})\n"


RUNNER = f'''
import json, os, subprocess, sys
results = {{}}
for name in sorted(os.listdir("/w/progs")):
    try:
        p = subprocess.run([sys.executable, "-I", "-B", "/w/progs/" + name], capture_output=True,
                           text=True, timeout={TIMEOUT_S}, cwd="/tmp")
        results[name] = [p.returncode, (p.stderr.strip().splitlines() or [""])[-1]]
    except subprocess.TimeoutExpired:
        results[name] = ["timeout", "timed out after {TIMEOUT_S} s"]
print(json.dumps(results))
'''


def run_in_docker(runs):
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(os.path.realpath(tmp))
        (work / "progs").mkdir()
        for i, (_, code, _) in enumerate(runs):
            (work / "progs" / f"{i:04d}.py").write_text(code, encoding="utf-8")
        (work / "run.py").write_text(RUNNER, encoding="utf-8")
        cmd = DOCKER + ["-v", f"{work}:/w:ro", IMAGE, "python", "/w/run.py"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=1800,
                               env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            bad("docker", f"could not run the tests: {exc}")
            return
    if p.returncode != 0:
        bad("docker", f"test runner failed: {p.stderr.strip()[-500:]}")
        return
    results = json.loads(p.stdout)
    for i, (where, _, must_pass) in enumerate(runs):
        status, last_line = results[f"{i:04d}.py"]
        if (status == 0) != must_pass:
            expectation = "pass" if must_pass else "fail"
            bad(where, f"tests should {expectation} but got status {status}: {last_line}")


# ---------------------------------------------------------------- main
def load(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        bad(path.name, f"not UTF-8 ({exc})")
        return []
    records = []
    for n, line in enumerate(lines, 1):
        where = f"{path.parent.name}/{path.name}:{n}"
        try:
            rec = json.loads(line)
        except ValueError as exc:
            bad(where, f"not a JSON object ({exc})")
            continue
        if not isinstance(rec, dict):
            bad(where, "not a JSON object")
            continue
        records.append((where, rec))
    return records


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for folder in ("items", "examples"):
        for path in sorted((ROOT / folder).glob("*.jsonl")):  # other entries, such as images, are assets
            if path.stem not in CATEGORIES:
                bad(f"{folder}/{path.name}", "unexpected file: names must be <category>.jsonl")

    runs, seen_ids, summary = [], {}, []
    for category, (grader, item_count, example_count) in CATEGORIES.items():
        items_path = ROOT / "items" / f"{category}.jsonl"
        examples_path = ROOT / "examples" / f"{category}.jsonl"
        items = load(items_path) if items_path.exists() else []
        examples = load(examples_path) if examples_path.exists() else []
        if item_count is not None and not items_path.exists():
            notes.append(f"items/{category}.jsonl does not exist yet")
        if example_count and not examples_path.exists():
            notes.append(f"examples/{category}.jsonl does not exist yet (the few-shot style needs {example_count})")

        for where, rec in items + examples:
            rid = rec.get("id")
            if rid in seen_ids:
                bad(where, f"duplicate id {rid!r} (also {seen_ids[rid]})")
            seen_ids.setdefault(rid, where)
        for where, rec in items:
            check_item(where, rec, category, runs)
        check_variants(items_path, items, category)
        for where, rec in examples:
            check_example(where, rec, category, runs)
        check_overlap(category, items, examples)

        base = [rec for _, rec in items if "variant_of" not in rec]
        if items_path.exists():
            if item_count is not None and len(base) != item_count:
                bad(f"items/{category}.jsonl", f"{len(base)} items; README.md says {item_count}")
            fresh = sum(rec.get("fresh") is True for rec in base)
            if base and fresh / len(base) < 0.25:
                bad(f"items/{category}.jsonl", f"only {fresh} of {len(base)} items are fresh; about a third should be")
            line = f"items/{category}.jsonl: {len(base)} items ({fresh} fresh), {len(items) - len(base)} variants"
            if category == "knowledge":
                subjects = {rec.get("subject") for rec in base if not rec.get("fresh")}
                if len(subjects - {None}) < 10:
                    bad(f"items/{category}.jsonl", "public questions must span at least 10 subjects")
                line += f", {len(subjects - {None})} public subjects"
            if category == "translate":
                per_language = {lang: sum(rec.get("target_language") == lang for rec in base) for lang in sorted(LANGUAGES)}
                line += f", per language {per_language}"
            if category == "tools":
                line += f", {sum(rec.get('expected') is None for rec in base)} with no tool expected"
            if category == "summarize":
                line += f", {sum(rec.get('injection') is True for rec in base)} with injection"
            summary.append(line)
        if examples_path.exists():
            if len(examples) < example_count:
                bad(f"examples/{category}.jsonl", f"{len(examples)} examples; the prompting styles need {example_count}")
            summary.append(f"examples/{category}.jsonl: {len(examples)} examples")

    if runs:
        run_in_docker(runs)
    print("\n".join(summary))
    print(f"Python test runs in Docker: {len(runs)}")
    for note in notes:
        print(f"note: {note}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    print(f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
