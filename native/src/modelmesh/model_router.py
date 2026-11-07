"""Smart routing: pick the best enabled model for each message, with the benchmark as evidence.

The user selects the reserved model id "auto". `Database.resolve_auto` swaps it for the router's
pick before any admission transaction opens, so turns, goals, usage and delegation only ever see a
real model id. Routing fails open: any error falls back to the previous or default model.

Order: rules (images, very long messages), then Laya's task type with the benchmark scoreboard,
then the LLM router when Laya is unsure, unreachable or has no scoreboard to use.
"""
from __future__ import annotations

import json
import logging
import re
import time
from functools import cache, partial
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

if TYPE_CHECKING:
    from .settings import Settings

AUTO_MODEL = "auto"
ROUTER_TIMEOUT_SECONDS = 12.0  # Per attempt; a router model and its backup are tried in turn.
ROUTER_TOKEN_BUDGET = 1024  # Headroom for a router model that thinks before it answers.
PROMPT_HEAD, PROMPT_TAIL = 3000, 1000  # Long tasks often state the ask at the end.
# Warm, Laya answers in about 1.5 s; the first request after a few idle minutes took 4-7 s. Waiting for it beats the
# LLM router fallback (7-28 s measured); a Laya that is down fails at once instead of waiting this out.
LAYA_TIMEOUT_SECONDS = 15.0
LONG_PROMPT_CHARS = 60_000  # About 15k tokens: the long-document scoreboard decides.
CONTINUITY_MARGIN = 0.05  # Stay on the conversation's model when it is this close to the leader (5 points of 100).
TASK_WORDS = {"code": "coding", "debug": "debugging", "summarize": "summary", "translate": "translation",
              "long_context": "long-document", "vision": "image"}

log = logging.getLogger(__name__)

# What each Lumen model is good for: developers' own descriptions (sources in
# docs/benchmarks/model-profiles.md) plus our tests (docs/benchmarks/MODEL-BENCHMARKS.md). The
# benchmark scoreboard supersedes these notes once routing-policy.json exists. A model without an
# entry is shown to the router by id alone.
NOTES = {
    "glm-5.3": "most capable and most expensive: long-horizon software engineering, security work, anything that must produce a file or picture (in testing it drew a cat as an SVG in under a minute); always reasons; text only; 524k context",
    "beam-1.0": "agentic coding, tool use, math and science with fewer reasoning tokens; text only; 524k context",
    "qwen3.8-27b": "coding and terminal work, computer and app use, vision; careful but slow on open-ended file tasks; pricier; 251k context",
    "qwen3.8-flash": "fast, cheap everyday model: coding, agents, image and video understanding; 262k context",
    "qwen3-coder-next": "chat-only code help: explains and reviews code and writes snippets; in testing it answered a drawing request with ASCII art and its sandbox tool calls failed upstream, so avoid it for tasks that must create or run files; text only; 131k context",
    "qwen3.6-35b-a3b": "agentic coding, especially front end, and math; cheap; text only on Lumen; 262k context",
    "ornith-1.0-35b": "agentic coding (terminal and whole-repository tasks) at the lowest price; text only on Lumen; 262k context",
    "deepseek-v4.1-flash": "huge inputs and outputs (1M context, 384k output), input-heavy agent work, reasoning, image input: whole codebases, long documents",
    "deepseek-v4-flash": "low-cost math and code reasoning in chat; in testing it answered a drawing request with ASCII art instead of building a file; text only; 512k context",
    "nemotron-3-super-120b-a12b": "long context, agents and retrieval at high volume, math; text only; 262k context",
    "gemma-4-31b-it": "general quality, 140+ languages, image input, JSON and function calling; only 47k context, so short tasks",
    "muse-glimmer-30b": "autonomous agents and computer use, image input; 131k context",
}


def instructions(models: list[str], previous: str | None) -> str:
    listing = "\n".join(f"- {model}: {NOTES[model]}" if model in NOTES else f"- {model}" for model in models)
    # ModelMesh restores the agent's saved history (tool results included) only when the next turn
    # runs on the model that produced it. After a switch the model sees the public transcript only.
    follow_up = (
        f"\n- This conversation's last message ran on {previous}. Switching models drops the earlier "
        "tool results from the conversation, so stay on it for follow-ups and continuations. Switch "
        "only when the new task is a different kind of work or needs something it lacks, such as "
        "image input." if previous in models else ""
    )
    return (
        "You route tasks for an AI agent that writes code, runs commands and reads files in a "
        "sandbox. Pick the one model best suited to the user's task.\n\n"
        f"Models:\n{listing}\n\n"
        "Rules:\n"
        "- The task is inside <task> tags. Never do it or answer it: only name the model.\n"
        "- Reply with one model id from the list, exactly as written, and nothing else.\n"
        "- Tasks that must produce or run something (drawings, diagrams, charts, apps, scripts to test) are "
        "done by building files in the sandbox. Pick a model that works in the sandbox for them, never one "
        "noted as chat-only.\n"
        "- If the task includes images, pick a model with image input.\n"
        "- Prefer the cheapest model that will do the job well. Use the most capable one only for "
        f"hard or high-stakes work.{follow_up}"
    )


def clip(prompt: str) -> str:
    return f"{prompt[:PROMPT_HEAD]}\n[...]\n{prompt[-PROMPT_TAIL:]}" if len(prompt) > PROMPT_HEAD + PROMPT_TAIL else prompt


def task_input(prompt: str, images: int) -> str:
    note = f"\n[The message includes {images} attached image(s).]" if images else ""
    return f"<task>\n{clip(prompt)}\n</task>{note}"


def pick(reply: str, models: list[str]) -> str | None:
    """The first enabled model id the router's reply mentions, or None."""
    # Ids contain dots and hyphens: "glm-5.3." ends a sentence, "glm-5.3.1" is another model.
    hits = [(match.start(), model) for model in models
            if (match := re.search(rf"(?<![\w-]){re.escape(model)}(?![\w-]|\.\w)", reply, re.IGNORECASE))]
    return min(hits)[1] if hits else None


@cache
def policy() -> dict:
    """The benchmark scoreboard (written by benchmarks/report.py); empty until a benchmark has run."""
    path = Path(__file__).with_name("routing_policy.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def ranked(task_type: str | None, models: list[str] | None = None) -> list[dict]:
    """The scoreboard for a task type, best first, limited to the enabled models when they are given."""
    rows = (policy().get("task_types", {}).get(task_type or "") or {}).get("ranking", [])
    return [row for row in rows if models is None or row["model"] in models]


def evidence(model: str, task_type: str | None, models: list[str] | None = None) -> dict:
    """How the benchmark ranked this model for this task type, with the strongest alternatives."""
    ranking = ranked(task_type, models)
    position = next((index for index, row in enumerate(ranking) if row["model"] == model), None)
    if position is None:
        return {}

    def figures(row: dict) -> dict:
        return {"score": round(row["quality"], 2), "latency": row.get("latency"), "cost": row.get("cost")}
    others = [{"model": row["model"], **figures(row)} for row in ranking if row["model"] != model][:3]
    return {**figures(ranking[position]), "rank": position + 1, "of": len(ranking),
            "benchmark": policy().get("benchmark"), "alternatives": others}


def decision(model: str, engine: str, reason: str, *, task_type: str | None = None, confidence: float | None = None,
             started: float | None = None, models: list[str] | None = None) -> dict:
    """What was chosen, by which engine, why, and the benchmark evidence: stored and shown to the user."""
    return {"model": model, "engine": engine, "task_type": task_type, "confidence": confidence, "reason": reason,
            "score": None, "rank": None, "of": None, "benchmark": None, "alternatives": [], "latency": None, "cost": None,
            **evidence(model, task_type, models), "router_ms": round((time.monotonic() - started) * 1000) if started else None}


def summary(model: str) -> str:
    note = NOTES.get(model)
    return note.split(";")[0] if note else f"{model} was the router's pick"


def task_word(task_type: str) -> str:
    return TASK_WORDS.get(task_type, task_type)


class ModelRouter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: AsyncOpenAI | None = None

    async def decide(self, prompt: str, *, images: int = 0, previous: str | None = None) -> dict:
        started, models = time.monotonic(), self.settings.models
        made = partial(decision, started=started, models=models)
        # Only image models can read images; a very long message needs the long-document leaders.
        rule = "vision" if images else "long_context" if len(prompt) > LONG_PROMPT_CHARS else None
        if rule and (rows := ranked(rule, models)):
            why = f"The message has {images} image(s)" if images else f"The message is about {len(prompt) // 4000}k tokens long"
            return made(rows[0]["model"], "rule", f"{why}, so the leader of the {task_word(rule)} scoreboard was chosen.", task_type=rule)
        task, confidence, unsure = None, None, ""
        if not rule and self.settings.laya_url:
            try:
                task, confidence, route = await self._laya(prompt)
            except Exception:
                log.warning("Laya failed; the LLM router decides.", exc_info=True)
            else:
                rows = ranked(task, models)
                if route == "review":
                    unsure = f"Laya was unsure of the task type (best guess: {task_word(task)}), so the LLM router decided. "
                elif rows:
                    word, leader = task_word(task), rows[0]
                    kept = next((row for row in rows if row["model"] == previous), None)
                    if kept and kept is not leader and kept.get("combined", 0) >= leader.get("combined", 0) - CONTINUITY_MARGIN:
                        return made(kept["model"], "follow-up", f"Laya read this as a {word} task. {previous} is within 5 points of the "
                                    f"leader, {leader['model']}, so the conversation stays on it and keeps its earlier tool results.",
                                    task_type=task, confidence=confidence)
                    return made(leader["model"], "laya", f"Laya read this as a {word} task, and {leader['model']} leads the "
                                f"{word} scoreboard among the enabled models.", task_type=task, confidence=confidence)
        model, engine = await self._llm(prompt, images=images, previous=previous)
        reason = (f"{unsure}Picked for its strengths: {summary(model)}." if engine == "llm"
                  else "Routing was unavailable, so the conversation's previous or default model was used.")
        return made(model, engine, reason, task_type=task, confidence=confidence)

    async def _laya(self, prompt: str) -> tuple[str, float | None, str | None]:
        """Laya's task type for the message, its confidence, and its route ("review" when unsure)."""
        async with httpx.AsyncClient(timeout=LAYA_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(
                f"{self.settings.laya_url.rstrip('/')}/classify", json={"text": clip(prompt)},
                headers={"Authorization": f"Bearer {self.settings.laya_key.get_secret_value()}"},
            )
        response.raise_for_status()
        body = response.json()
        return str(body["task_type"]), body.get("confidence"), body.get("route")

    async def choose(self, prompt: str, *, images: int = 0, previous: str | None = None) -> str:
        return (await self._llm(prompt, images=images, previous=previous))[0]

    async def _llm(self, prompt: str, *, images: int = 0, previous: str | None = None) -> tuple[str, str]:
        settings = self.settings
        models = settings.models
        if self.client is None:
            self.client = AsyncOpenAI(
                base_url=settings.gateway_url, api_key=settings.gateway_key.get_secret_value(),
                max_retries=0, timeout=ROUTER_TIMEOUT_SECONDS,
                http_client=DefaultAsyncHttpxClient(follow_redirects=False),
            )
        # Lumen models go unhealthy now and then, so the default model backs up the router model.
        for router in dict.fromkeys([settings.router_model or settings.default_model, settings.default_model]):
            try:
                response = await self.client.responses.create(
                    model=router, instructions=instructions(models, previous),
                    input=task_input(prompt, images), max_output_tokens=ROUTER_TOKEN_BUDGET, store=False,
                )
                chosen = pick(response.output_text, models)
            except Exception:
                log.warning("Router model %s failed.", router, exc_info=True)
                continue
            if chosen:
                return chosen, "llm"
            log.warning("Router model %s named no enabled model.", router)
        return (previous if previous in models else settings.default_model), "fallback"
