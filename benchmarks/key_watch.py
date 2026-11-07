"""Swap in the backup Lumen key once the current key's credits run out, then resume the benchmark runs.

    python benchmarks/key_watch.py watch --run 2026-11-06-full --agent-run 2026-11-06-agent
    python benchmarks/key_watch.py swap      # for grading: swaps only if the current key is confirmed exhausted

`watch` reads the runs' results files for errors that look like exhausted credits. A suspected exhaustion
is confirmed with two direct calls to Lumen a minute apart (a passing rate limit does not count), and the
backup key must answer a test call. Then it stops the running benchmark processes, moves the old key to
.env.lumen.exhausted, puts the backup key (.env.lumen.next) into .env.lumen, recreates the gateway and the
app (which shares the gateway's network), and resumes both runs with --retry-errors. It exits when the
runs finish. No key is ever printed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "benchmarks" / "results"
LUMEN = "https://lumen.ncsa.illinois.edu/v1/chat/completions"
CREDIT_WORDS = re.compile(r"budget|credit|quota|insufficient|balance|billing|payment|spend limit|out of funds", re.I)


def key_in(path: Path) -> str:
    match = re.search(r"^LUMEN_API_KEY=(.+)$", path.read_text(encoding="utf-8"), re.M)
    return match.group(1).strip().strip('"').strip("'") if match else ""


def looks_exhausted(status: int, text: str) -> bool:
    return status in (401, 402, 403) or (status >= 400 and bool(CREDIT_WORDS.search(text)))


def probe(key: str) -> tuple[int, str]:
    """One tiny request straight to Lumen (not through the gateway)."""
    body = json.dumps({"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}).encode()
    request = urllib.request.Request(LUMEN, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, ""
    except urllib.error.HTTPError as failure:
        return failure.code, failure.read()[:400].decode("utf-8", "replace")
    except Exception as failure:  # noqa: BLE001 - a network error is not exhaustion
        return 0, f"{type(failure).__name__}: {failure}"


def confirmed_exhausted() -> bool:
    for attempt in range(2):
        status, text = probe(key_in(ROOT / ".env.lumen"))
        if not looks_exhausted(status, text):
            print(f"current key still answers (HTTP {status}); no swap", flush=True)
            return False
        print(f"probe {attempt + 1}: current key refused (HTTP {status}: {CREDIT_WORDS.findall(text)[:3]})", flush=True)
        if attempt == 0:
            time.sleep(60)
    return True


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.yaml"), *args], check=True, cwd=ROOT)


def swap() -> bool:
    backup = ROOT / ".env.lumen.next"
    if not backup.exists() or not key_in(backup):
        print("no backup key in .env.lumen.next; nothing swapped", flush=True)
        return False
    status, _ = probe(key_in(backup))
    if status != 200:
        print(f"backup key failed its test call (HTTP {status}); nothing swapped", flush=True)
        return False
    env = ROOT / ".env.lumen"
    text = env.read_text(encoding="utf-8")
    old = re.search(r"^LUMEN_API_KEY=.*$", text, re.M).group(0)
    with (ROOT / ".env.lumen.exhausted").open("a", encoding="utf-8") as spent:  # Append: earlier keys refill too.
        spent.write(old + "\n")
    env.write_text(text.replace(old, f"LUMEN_API_KEY={key_in(backup)}", 1), encoding="utf-8")
    backup.unlink()  # In use now; .env.lumen holds it.
    compose("up", "-d", "--no-deps", "--force-recreate", "lumen-gateway")
    compose("up", "-d", "--no-deps", "--force-recreate", "app")  # Shares the gateway's network namespace.
    time.sleep(20)
    print("swapped: the backup key is now in .env.lumen; gateway and app recreated", flush=True)
    return True


def running(pattern: str) -> list[int]:
    """Process ids whose command line matches (Windows)."""
    script = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '{pattern}' -and $_.CommandLine -notlike '*key_watch*' }}"
              " | ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout
    return [int(line) for line in out.split() if line.isdigit()]


def stop(pattern: str) -> None:
    for pid in running(pattern):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def watch(run_id: str, agent_run: str | None) -> int:
    patterns = {f"*run.py*--run-id {run_id}*": [sys.executable, str(ROOT / "benchmarks/run.py"), "--run-id", run_id, "--max-cost", "60", "--retry-errors"]}
    if agent_run:
        patterns[f"*agent.py*--run-id {agent_run}*"] = [sys.executable, str(ROOT / "benchmarks/agent.py"), "--run-id", agent_run, "--retry-errors"]
    files = [RESULTS / f"{run_id}.jsonl"] + ([RESULTS / f"{agent_run}.jsonl"] if agent_run else [])
    offsets = {path: path.stat().st_size if path.exists() else 0 for path in files}
    print(f"watching {', '.join(path.name for path in files)}", flush=True)
    while True:
        suspect = False
        for path in files:
            if not path.exists():
                continue
            with path.open("rb") as handle:
                handle.seek(offsets[path])
                chunk = handle.read()
            complete = chunk[:chunk.rfind(b"\n") + 1]
            offsets[path] += len(complete)
            for line in complete.decode("utf-8", "replace").splitlines():
                error = json.loads(line).get("error") or ""
                status = int(match.group(1)) if (match := re.match(r"HTTP (\d{3})", error)) else 500
                suspect |= bool(error) and looks_exhausted(status, error)
        if suspect and confirmed_exhausted():
            for pattern in patterns:
                stop(pattern)
            if not swap():
                return 1
            resumed = [subprocess.Popen(command, cwd=ROOT) for command in patterns.values()]
            print("resumed the runs with --retry-errors", flush=True)
            return max(process.wait() for process in resumed)
        if not any(running(pattern) for pattern in patterns):
            print("the runs finished without the key running out", flush=True)
            return 0
        time.sleep(30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["watch", "swap"])
    parser.add_argument("--run", help="results file to watch, by run id")
    parser.add_argument("--agent-run", help="the agent tasks' run id, resumed too")
    args = parser.parse_args()
    if args.action == "swap":
        return 0 if confirmed_exhausted() and swap() else 1
    return watch(args.run, args.agent_run)


if __name__ == "__main__":
    sys.exit(main())
