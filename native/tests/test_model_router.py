"""Smart routing: the "auto" model id is swapped for a real model before admission."""
from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select, update

from modelmesh import model_router
from modelmesh.api_views import RoutingDecision
from modelmesh.contracts import NewMessage, NewSession
from modelmesh.model_router import AUTO_MODEL, NOTES, ModelRouter, decision, instructions, pick
from modelmesh.models import Event, Session, Turn, Upload
from modelmesh.settings import Settings

MODELS = ["qwen3-coder-next", "glm-5.3", "qwen3.8-flash"]


def settings(**values) -> Settings:
    values = {
        "default_model": "qwen3.8-flash", "model_catalog": ",".join(MODELS),
        "gateway_url": "http://127.0.0.1:4000/v1", "gateway_key": "sk-test", **values,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("reply, expected", [
    ("glm-5.3", "glm-5.3"),
    ("`glm-5.3`.", "glm-5.3"),  # A sentence-ending dot is not part of the id.
    ("I would pick QWEN3-CODER-NEXT for this.", "qwen3-coder-next"),  # Case-insensitive, canonical spelling.
    ("glm-5.3 or qwen3.8-flash", "glm-5.3"),  # The first mention wins.
    ("glm-5.3.1", None),  # Another model, not glm-5.3.
    ("qwen3.8-flash-pro", None),
    ("no idea", None),
    ("", None),
])
def test_pick_names_one_enabled_model(reply, expected):
    assert pick(reply, MODELS) == expected


def test_instructions_describe_each_model_and_the_previous_one():
    text = instructions([*MODELS, "mystery-1"], previous="glm-5.3")
    assert all(f"- {model}: {NOTES[model]}" in text for model in MODELS)
    assert "- mystery-1\n" in text + "\n"  # No notes: listed by id alone.
    assert "last message ran on glm-5.3. Switching models drops the earlier tool results" in text
    assert "last message ran on" not in instructions(MODELS, previous=None)
    assert "last message ran on" not in instructions(MODELS, previous="not-enabled")


@pytest.mark.parametrize("changes, enabled", [
    ({}, True),
    ({"model_catalog": ""}, False),  # One model: nothing to choose between.
    ({"gateway_key": ""}, False),  # This process cannot call the gateway.
    ({"gateway_url": ""}, False),
])
def test_routing_needs_models_and_a_gateway(changes, enabled):
    assert settings(**changes).routing_enabled is enabled


class FakeClient:
    def __init__(self, reply="", error=None, down=()):
        self.calls, self.reply, self.error, self.down = [], reply, error, set(down)
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **call):
        self.calls.append(call)
        if self.error or call["model"] in self.down:
            raise self.error or TimeoutError("unhealthy")
        return SimpleNamespace(output_text=self.reply)


def router(client, **values) -> ModelRouter:
    chosen = ModelRouter(settings(**values))
    chosen.client = client
    return chosen


async def test_router_returns_the_pick_and_tells_it_about_images():
    client = FakeClient("qwen3-coder-next")
    assert await router(client, router_model="glm-5.3").choose("draw a cat", images=1) == "qwen3-coder-next"
    [call] = client.calls
    assert call["model"] == "glm-5.3" and call["store"] is False
    assert call["input"].startswith("<task>\ndraw a cat\n</task>") and "1 attached image" in call["input"]


@pytest.mark.parametrize("client", [FakeClient(error=TimeoutError("slow")), FakeClient("I cannot decide."), FakeClient("")])
async def test_routing_failure_keeps_the_previous_model_or_the_default(client):
    assert await router(client).choose("hi", previous="glm-5.3") == "glm-5.3"
    assert await router(client).choose("hi", previous="not-enabled") == "qwen3.8-flash"
    assert await router(client).choose("hi") == "qwen3.8-flash"


async def test_the_default_model_backs_up_a_router_model_that_is_down():
    client = FakeClient("glm-5.3", down={"gemma-4-31b-it"})
    assert await router(client, router_model="gemma-4-31b-it").choose("hi") == "glm-5.3"
    assert [call["model"] for call in client.calls] == ["gemma-4-31b-it", "qwen3.8-flash"]
    both = FakeClient(error=TimeoutError("unhealthy"))
    assert await router(both, router_model="gemma-4-31b-it").choose("hi", previous="glm-5.3") == "glm-5.3"
    assert len(both.calls) == 2  # Router model, then the default; then no routing.


@pytest.fixture
def routed(runtime):
    runtime.settings.model_catalog = "test,other"
    runtime.settings.gateway_url, runtime.settings.gateway_key = "http://127.0.0.1:4000/v1", SecretStr("sk-test")
    calls = []

    async def decide(prompt, **context):
        calls.append((prompt, context))
        return decision("other", "llm", "test pick")
    runtime.database.router.decide = decide
    return calls


async def test_auto_message_runs_on_the_routed_model_and_retries_stay_idempotent(runtime, actor, routed):
    session = await runtime.database.create_session(actor, NewSession(visibility="private", model=AUTO_MODEL))
    assert session.model == "test"  # The conversation starts on the default model.
    body = NewMessage(prompt="draw a cat", request_key="auto-1", model=AUTO_MODEL)
    turn = await runtime.database.submit(actor, session.id, body)
    # A new conversation has no saved history to keep, so the router gets no "previous model" hint.
    assert turn.model == "other" and routed == [("draw a cat", {"images": 0, "previous": None})]
    async with runtime.database.sessions.begin() as db:
        assert (await db.get(Session, session.id)).model == "other"
        await db.execute(update(Session).where(Session.id == session.id).values(last_model="other"))
    await runtime.database.submit(
        actor, session.id, NewMessage(prompt="now make it blue", request_key="auto-2", model=AUTO_MODEL, mode="when_idle"),
    )
    assert routed[-1] == ("now make it blue", {"images": 0, "previous": "other"})  # Follow-ups see the last model.
    assert turn.routing["model"] == "other" and turn.routing["engine"] == "llm"
    async with runtime.database.sessions() as db:
        stored = await db.scalar(select(Event).where(Event.turn_id == turn.id, Event.kind == "model_routed"))
    assert json.loads(stored.content)["reason"] == "test pick"  # Kept for the pop-up, the answer tag and the dashboard.

    async def other(prompt, **context):
        return decision("test", "llm", "changed its mind")
    runtime.database.router.decide = other  # The router changed its mind before the client retried.
    assert (await runtime.database.submit(actor, session.id, body)).id == turn.id


async def test_only_image_attachments_make_an_auto_message_a_vision_task(runtime, actor, routed):
    session = await runtime.database.create_session(actor, NewSession(visibility="private", model=AUTO_MODEL))
    async with runtime.database.sessions.begin() as db:
        csv = Upload(id="upload-csv", owner_id=actor.id, name="sales.csv", media_type="text/csv", digest="0" * 64, size=10)
        png = Upload(id="upload-png", owner_id=actor.id, name="chart.png", media_type="image/png", digest="1" * 64, size=10)
        db.add_all([csv, png])
    for attachments, images in ((["upload-csv"], 0), (["upload-csv", "upload-png"], 1)):
        body = NewMessage(prompt="chart this", request_key=f"auto-{images}", model=AUTO_MODEL, attachments=attachments)
        await runtime.database.resolve_auto(actor, session.id, body)
        assert routed[-1][1]["images"] == images  # A CSV or a code file is not an image.


async def test_guidance_for_a_running_turn_keeps_that_turns_model(runtime, actor, routed):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    first = await runtime.database.submit(actor, session.id, NewMessage(prompt="Start.", request_key="first"))
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Turn).where(Turn.id == first.id).values(status="running"))
        await db.execute(update(Session).where(Session.id == session.id).values(active_turn=first.id))
    steering = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Also add tests.", request_key="second", model=AUTO_MODEL, mode="asap"),
    )
    assert routed == []  # No router call: the guidance joins the running turn.
    assert steering.model == "test" and steering.status == "steering" and steering.parent_turn == first.id
    assert steering.routing["engine"] == "running-turn"


async def test_auto_is_refused_when_routing_is_unavailable(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    with pytest.raises(HTTPException) as refused:
        await runtime.database.submit(actor, session.id, NewMessage(prompt="hi", request_key="k", model=AUTO_MODEL))
    assert refused.value.status_code == 422


def test_decision_carries_benchmark_evidence(monkeypatch):
    monkeypatch.setattr(model_router, "policy", lambda: {"benchmark": "run-1", "task_types": {"code": {"ranking": [
        {"model": "glm-5.3", "quality": 0.91, "latency": 9.5, "cost": 0.004},
        {"model": "qwen3.8-flash", "quality": 0.804, "latency": 2.1, "cost": 0.0005}]}}})
    made = decision("qwen3.8-flash", "laya", "why", task_type="code", confidence=0.9)
    assert (made["score"], made["rank"], made["of"], made["benchmark"]) == (0.8, 2, 2, "run-1")
    assert (made["latency"], made["cost"]) == (2.1, 0.0005)
    assert made["alternatives"] == [{"model": "glm-5.3", "score": 0.91, "latency": 9.5, "cost": 0.004}]
    RoutingDecision.model_validate(made)  # What the router stores is what the API serves.
    assert decision("qwen3.8-flash", "llm", "why")["rank"] is None  # No task type, no evidence.


async def test_decide_says_when_routing_fell_back():
    made = await router(FakeClient(error=TimeoutError("down"))).decide("hi", previous="glm-5.3")
    assert made["model"] == "glm-5.3" and made["engine"] == "fallback"
    made = await router(FakeClient("qwen3-coder-next")).decide("write a parser")
    assert made["engine"] == "llm" and made["reason"].startswith("Picked for its strengths: chat-only code help")


SCOREBOARD = {"benchmark": "run-1", "task_types": {
    "code": {"ranking": [{"model": "glm-5.3", "quality": 0.9, "combined": 0.80},
                         {"model": "qwen3.8-flash", "quality": 0.85, "combined": 0.77},
                         {"model": "qwen3-coder-next", "quality": 0.7, "combined": 0.50}]},
    "vision": {"ranking": [{"model": "not-enabled", "quality": 0.95, "combined": 0.9},
                           {"model": "qwen3.8-flash", "quality": 0.8, "combined": 0.7}]},
}}


def laya_router(monkeypatch, answer=None, error=None, reply="qwen3-coder-next"):
    monkeypatch.setattr(model_router, "policy", lambda: SCOREBOARD)
    client = FakeClient(reply)
    chosen = router(client, laya_url="http://laya:8000")

    async def classify(prompt):
        if error:
            raise error
        return answer
    chosen._laya = classify
    return chosen, client


async def test_laya_task_type_picks_the_enabled_scoreboard_leader(monkeypatch):
    chosen, client = laya_router(monkeypatch, ("code", 0.93, "auto"))
    made = await chosen.decide("write a parser")
    assert (made["model"], made["engine"], made["task_type"], made["confidence"]) == ("glm-5.3", "laya", "code", 0.93)
    assert (made["rank"], made["of"]) == (1, 3) and client.calls == []  # The LLM router is not asked.
    kept = await chosen.decide("now add tests", previous="qwen3.8-flash")  # 0.77 is within 5 points of 0.80.
    assert (kept["model"], kept["engine"]) == ("qwen3.8-flash", "follow-up") and "keeps its earlier tool results" in kept["reason"]
    switched = await chosen.decide("now add tests", previous="qwen3-coder-next")  # 0.50 is not.
    assert (switched["model"], switched["engine"]) == ("glm-5.3", "laya")


@pytest.mark.parametrize("answer, error", [(("code", 0.4, "review"), None), (None, TimeoutError("slow")), (("chat", 0.9, "auto"), None)])
async def test_unsure_unreachable_or_unscored_laya_leaves_it_to_the_llm_router(monkeypatch, answer, error):
    chosen, client = laya_router(monkeypatch, answer, error)
    made = await chosen.decide("hmm")
    assert (made["model"], made["engine"]) == ("qwen3-coder-next", "llm") and len(client.calls) == 1
    assert made["reason"].startswith("Laya was unsure") == (answer is not None and answer[2] == "review")


async def test_images_follow_the_vision_scoreboard_without_asking_laya(monkeypatch):
    chosen, client = laya_router(monkeypatch, error=AssertionError("Laya must not be asked"))
    made = await chosen.decide("what is in this picture?", images=2)
    assert (made["model"], made["engine"], made["task_type"]) == ("qwen3.8-flash", "rule", "vision")  # The leader is not enabled.
    assert made["reason"].startswith("The message has 2 image(s)") and (made["rank"], made["of"]) == (1, 1)
    made = await chosen.decide("x" * 60_001)  # No long-document scoreboard yet: the LLM router decides, not Laya.
    assert made["engine"] == "llm" and client.calls


async def test_laya_is_asked_with_the_key_and_a_clipped_prompt(monkeypatch):
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, json={"task_type": "math", "confidence": 0.8, "route": "auto"})
    monkeypatch.setattr(model_router.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(answer)))
    chosen = router(FakeClient(), laya_url="http://laya:8000/", laya_key="k-1")
    assert await chosen._laya("a" * 5000 + "solve it") == ("math", 0.8, "auto")
    [request] = seen
    assert str(request.url) == "http://laya:8000/classify" and request.headers["authorization"] == "Bearer k-1"
    text = json.loads(request.content)["text"]
    assert len(text) < 4100 and text.endswith("solve it")
