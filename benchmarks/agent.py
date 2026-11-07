"""Run the agent items through ModelMesh itself, one private conversation per model and task:

    python benchmarks/agent.py --run-id 2026-11-06

Each answer lands in results/<run-id>.jsonl with category "agent", so graders.py and report.py
treat it like any other item. The benchmark's conversations are deleted afterwards (moved to the
trash). Signs in as the workspace administrator using WORKSPACE_PASSWORD from modelmesh.env.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from run import MODELS, RESULTS, ROOT, load

APP = "http://127.0.0.1:8787"


class Client:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        env = (ROOT.parent / "modelmesh.env").read_text(encoding="utf-8")
        password = re.search(r"^WORKSPACE_PASSWORD=(.+)$", env, re.M).group(1).strip()
        user = (re.search(r"^MODELMESH_ADMIN_USER=(.+)$", env, re.M) or re.search(r"(admin)", "admin")).group(1).strip()
        self.csrf = self.request("POST", "/api/login", {"username": user, "password": password}).get("csrf") or self.request("GET", "/api/me")["csrf"]

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if getattr(self, "csrf", None):
            headers |= {"X-ModelMesh-CSRF": self.csrf, "X-CSRF-Token": self.csrf}
        request = urllib.request.Request(APP + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        with self.opener.open(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--models", default="all")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for one task")
    parser.add_argument("--retry-errors", action="store_true", help="re-run tasks that failed on an earlier attempt")
    parser.add_argument("--items", default="all", help="comma-separated item ids, so a retry can leave other tasks alone")
    args = parser.parse_args()
    models = list(MODELS) if args.models == "all" else args.models.split(",")
    out = RESULTS / f"{args.run_id}.jsonl"
    done = set()
    if out.exists():
        done = {(r["model"], r["item_id"]) for r in map(json.loads, out.read_text(encoding="utf-8").splitlines())
                if r["category"] == "agent" and (not r["error"] or not args.retry_errors)}
    client, lock = Client(), threading.Lock()
    jobs = [(model, item) for model in models for item in load("items", "agent")
            if (model, item["id"]) not in done and (args.items == "all" or item["id"] in args.items.split(","))]
    print(f"{len(jobs)} agent tasks to run", flush=True)

    def run(job: tuple[str, dict]) -> None:
        model, item = job
        started, session, text, status, error, files = time.monotonic(), None, "", "", "", []
        try:
            session = client.request("POST", "/api/sessions", {"model": model, "visibility": "private", "title": f"[benchmark] {item['id']}"})
            client.request("POST", f"/api/sessions/{session['id']}/messages", {
                "prompt": item["prompt"], "model": model, "request_key": str(uuid.uuid4()), "mode": "asap", "connections": [], "attachments": []})
            while time.monotonic() - started < args.timeout:
                time.sleep(5)
                view = client.request("GET", f"/api/sessions/{session['id']}?include_activity=false&include_messages=true")
                turn = (view.get("turns") or [{}])[0]
                status = turn.get("status", "")
                if status in ("completed", "failed", "stopped"):
                    error = turn.get("error") or ""
                    replies = [m["content"] for m in view.get("messages") or [] if m.get("role") == "assistant"]
                    text = replies[-1] if replies else ""
                    # The files the user would see in the conversation; open-ended build tasks are graded on these.
                    files = [{key: made.get(key) for key in ("name", "media_type", "size")}
                             for made in client.request("GET", f"/api/sessions/{session['id']}/files")]
                    break
            else:
                error = "timed out"
        except Exception as failure:  # noqa: BLE001 - recorded as an error row
            error = f"{type(failure).__name__}: {failure}"
        row = {"run": args.run_id, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "category": "agent", "item_id": item["id"],
               "style": "zero", "sample": 0, "temperature": None, "fresh": True, "variant": None, "variant_of": None, "text": text,
               "tool_calls": [], "files": files, "finish": status, "usage": {}, "reasoning_chars": 0, "latency": time.monotonic() - started, "ttft": None,
               "attempts": 1, "error": error if status != "completed" else "", "cost": 0.0, "budget": None}
        with lock:
            with out.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{model} {item['id']}: {status or error} in {row['latency']:.0f} s", flush=True)
        if session:
            try:
                client.request("DELETE", f"/api/sessions/{session['id']}")
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run, jobs))


if __name__ == "__main__":
    main()
