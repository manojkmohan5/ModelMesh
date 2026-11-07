"""Pure report semantics and native usage/privacy tests; not provider E2E."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.usage import RunUsage
from test_native_settings import client_for, member

from modelmesh.analytics import UsageEvent, actual_cost, daily_usage, report_range, usage_totals
from modelmesh.analytics_service import inference_event, report
from modelmesh.contracts import NewMessage, NewSession
from modelmesh.inference_usage import InferenceUsage
from modelmesh.models import Session, Turn, User, new_id

START, END = "2026-10-01T00:00:00+00:00", "2026-10-04T00:00:00+00:00"


def test_proven_not_dispatched_ledger_is_not_an_unpriced_http_request():
    row = InferenceUsage(
        id="attempt", turn_id="turn", session_id="session", actor_id="actor", model="model",
        created_at=START, status="not_dispatched", coverage_error="not_dispatched",
        cost_status="missing", cost_usd=None,
    )
    event = inference_event(row)
    assert event.turn_id == "turn" and event.requests == 0 and event.cost is None
    totals = usage_totals([event]).view()
    assert totals["requests"] == totals["priced_requests"] == totals["unpriced_requests"] == 0
    assert totals["cost_usd"] is None
    for uncertain in ("interrupted", "failed", "pending"):
        row.status = uncertain
        assert inference_event(row).requests == 1


def test_utc_presets_match_legacy_calendar_controls_and_rolling_24_hours():
    clock = datetime(2026, 1, 2, 17, 30, tzinfo=UTC)
    week = report_range("7d", clock=clock)
    assert week.start.isoformat() == "2025-12-27T00:00:00+00:00"
    assert week.end.isoformat() == "2026-01-03T00:00:00+00:00"
    assert len(week.dates()) == 7
    day = report_range("24h", clock=clock)
    assert day.start == clock - timedelta(hours=24) and day.end == clock
    assert not day.contains(day.end) and day.contains(day.start)
    custom = report_range("custom", "2026-10-01T01:00:00+01:00", END)
    assert custom.start.isoformat() == START and len(custom.dates()) == 3
    for start, end in ((END, START), (START, START), ("invalid", END), ("2020-01-01", END)):
        with pytest.raises(ValueError):
            report_range("custom", start, end)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1e9999", True, None, "not money"])
def test_invalid_or_unreported_cost_never_becomes_zero(value):
    assert actual_cost(value, "actual", "response_usage") is None
    assert actual_cost("10", "missing", "response_usage") is None
    assert actual_cost("10", "actual", None) is None


def test_daily_bins_preserve_unknown_cost_and_explicit_partial_cumulative_values():
    period = report_range("custom", START, END)
    events = [
        UsageEvent("a", "t1", "s", "u", "model-a", datetime(2026, 10, 2, tzinfo=UTC), cost=Decimal("0.1")),
        UsageEvent("b", "t2", "s", "u", "model-a", datetime(2026, 10, 2, tzinfo=UTC)),
        UsageEvent("c", "t3", "s", "u", "model-b", datetime(2026, 10, 3, tzinfo=UTC), cost=Decimal(0)),
    ]
    rows = daily_usage(events, period)
    assert rows[0]["requests"] == 0 and rows[0]["cost_usd"] is None
    assert rows[0]["cumulative_cost_usd"] is None
    assert rows[1]["cost_usd"] == .1 and rows[1]["pricing_status"] == "partial"
    assert rows[2]["cost_usd"] == 0 and rows[2]["pricing_status"] == "priced"
    assert rows[2]["cumulative_cost_usd"] == .1 and rows[2]["cumulative_pricing_status"] == "partial"
    assert rows[2]["models"][0]["cost_usd"] is None


def test_missing_token_observations_are_not_reported_as_zero():
    event = UsageEvent("a", "t", "s", "u", "model", datetime(2026, 10, 2, tzinfo=UTC))
    assert usage_totals([event]).view()["total_tokens"] is None
    assert usage_totals([]).view()["total_tokens"] == 0
    known = UsageEvent("b", "t", "s", "u", "model", event.created_at, total_tokens=0)
    assert usage_totals([known]).view()["total_tokens"] == 0
    partial = usage_totals([event, known]).view()
    assert partial["total_tokens"] == 0 and partial["token_coverage"] == .5


async def ledger(runtime, actor, session, turn, *, timestamp=START, cost="0.1", model="test"):
    identity = new_id()
    async with runtime.database.sessions.begin() as db:
        row = InferenceUsage(
            id=identity, request_key=identity, session_id=session.id, turn_id=turn.id, actor_id=actor.id,
            model=model, provider_scope="synthetic", created_at=timestamp, finished_at=timestamp,
            input_tokens=20, output_tokens=10, total_tokens=30,
            cost_usd=cost, cost_status="actual" if cost is not None else "missing",
            cost_source="response_usage" if cost is not None else None, status="completed",
        )
        db.add(row)
    return row


async def test_failed_turn_without_usage_remains_unknown(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Unverified old turn", request_key="unknown-failure",
    ))
    await runtime.database.fail(turn.id, "No recorded usage.")
    value = await report(runtime, actor, None, None, None, None, None, 100, 0, "json")
    assert value["summary"]["pricing_status"] == "unpriced"
    assert value["coverage"]["unknown_request_turns"] == 1


async def test_report_uses_ledger_without_double_counting_zero_fills_and_exports_full_aggregates(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(
        title="PRIVATE_TITLE_DO_NOT_EXPORT", visibility="private",
    ))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="PRIVATE_PROMPT_DO_NOT_EXPORT", request_key="recorded",
    ))
    historic = await runtime.database.submit(actor, session.id, NewMessage(prompt="Old", request_key="historic"))
    day_two = "2026-10-02T12:00:00+00:00"
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).created_at = "2026-09-30T00:00:00+00:00"
        (await db.get(Turn, turn.id)).created_at = "2026-09-30T00:00:00+00:00"
        recorded = await db.get(Turn, turn.id)
        recorded.usage = json.dumps({"requests": 101, "input_tokens": 9999, "output_tokens": 1000})
        recorded.preparation = runtime.security.seal({"conversation": json.dumps({
            "messages": [], "usage": {"requests": 98, "input_tokens": 9939, "output_tokens": 970},
        })})
        old = await db.get(Turn, historic.id)
        old.created_at, old.usage = day_two, json.dumps({"requests": 2, "input_tokens": 5, "output_tokens": 5})
        old.preparation = runtime.security.seal({"conversation": None})
    await ledger(runtime, actor, session, turn, timestamp=day_two, cost="0.10")
    await ledger(runtime, actor, session, turn, timestamp="2026-10-03T00:00:00+00:00", cost="0", model="=UNSAFE")
    await ledger(runtime, actor, session, turn, timestamp=END, cost="500")
    params = {"start": START, "end": END, "limit": 1}
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/usage", params=params)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["total"] == 2 and len(value["rows"]) == 1
        summary = value["summary"]
        assert summary["requests"] == 4 and summary["total_tokens"] == 70
        assert summary["cost_usd"] == .1 and summary["pricing_status"] == "partial"
        assert summary["sessions_created"] == 0 and summary["sessions_active"] == 1
        assert value["coverage"]["ledger_requests"] == 2 and value["coverage"]["legacy_turns"] == 1
        assert len(value["series"]) == 3 and value["series"][0]["cost_usd"] is None
        assert value["series"][2]["cost_usd"] == 0
        assert value["by_user"][0]["requests"] == 4
        assert "PRIVATE_" not in response.text
        for export in ("turns", "users", "daily", "models"):
            exported = await client.get("/api/settings/usage", params={**params, "format": "csv", "export": export})
            assert exported.status_code == 200
            parsed = list(csv.DictReader(io.StringIO(exported.text)))
            assert len(parsed) == {"turns": 2, "users": 1, "daily": 3, "models": 2}[export]
            assert "PRIVATE_" not in exported.text
        model_csv = await client.get("/api/settings/usage", params={**params, "format": "csv", "export": "models"})
        assert "'=UNSAFE" in model_csv.text
    # The AnalyticsReport response model neither drops nor adds keys.
    assert value == json.loads(json.dumps(await report(runtime, actor, START, END, None, None, None, 1, 0, "json")))


async def test_reports_are_admin_only(runtime, actor):
    other = await member(runtime)
    with pytest.raises(HTTPException) as denied:
        await report(runtime, other, START, END, None, None, None, 100, 0, "json")
    assert denied.value.status_code == 403


async def test_analytics_filter_preserves_long_canonical_product_actor_ids(runtime, actor):
    canonical = User(
        id="google:" + "x" * 500, username="synthetic-canonical-user",
        display_name="Synthetic Canonical User", password=actor.password, role="member",
    )
    async with runtime.database.sessions.begin() as db:
        db.add(canonical)
    session = await runtime.database.create_session(canonical, NewSession())
    turn = await runtime.database.submit(
        canonical, session.id, NewMessage(prompt="Synthetic", request_key="canonical-filter"),
    )
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).created_at = START
        (await db.get(Turn, turn.id)).created_at = START
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/usage", params={
            "start": START, "end": END, "user_id": canonical.id,
        })
    assert response.status_code == 200
    assert response.json()["by_user"][0]["user_id"] == canonical.id


async def test_historical_fallback_subtracts_exact_seed_and_does_not_guess_missing_baseline(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turns = [await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Synthetic baseline test", request_key=f"baseline-{index}",
    )) for index in range(3)]
    current = [
        {"requests": 1, "input_tokens": 10, "output_tokens": 5},
        {"requests": 2, "input_tokens": 25, "output_tokens": 12},
        {"requests": 50, "input_tokens": 500, "output_tokens": 500},
    ]
    async with runtime.database.sessions.begin() as db:
        for index, turn in enumerate(turns):
            row = await db.get(Turn, turn.id)
            row.created_at = "2026-10-02T00:00:00+00:00"
            row.usage = json.dumps(current[index])
            row.preparation = runtime.security.seal({
                "prompt": "PRIVATE_BASELINE_DO_NOT_EXPORT",
                "conversation": None if index == 0 else json.dumps({"usage": current[0]}),
            }) if index < 2 else None
    value = await report(runtime, actor, START, END, None, None, None, 1, 0, "json")
    assert value["summary"]["requests"] == 2
    assert value["summary"]["input_tokens"] == 25 and value["summary"]["output_tokens"] == 12
    assert value["summary"]["total_tokens"] == 37
    assert value["summary"]["cost_usd"] is None
    assert value["coverage"]["unknown_request_turns"] == 1
    assert "PRIVATE_BASELINE_DO_NOT_EXPORT" not in json.dumps(value)


async def test_capture_gap_compares_usage_delta_to_all_attempts_and_never_invents_cost(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Synthetic", request_key="gap"))
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Turn, turn.id)
        row.created_at = "2026-10-02T00:00:00+00:00"
        row.preparation = runtime.security.seal({"conversation": json.dumps({
            "usage": {"requests": 3, "input_tokens": 30, "output_tokens": 20},
        })})
        row.usage = json.dumps({"requests": 5, "input_tokens": 70, "output_tokens": 40})
    await ledger(runtime, actor, session, turn, timestamp="2026-10-02T01:00:00+00:00", cost=".1")
    # A later attempt outside the selected window still belongs to the SDK's
    # saved cumulative total and must not produce a false capture-gap alarm.
    await ledger(runtime, actor, session, turn, timestamp=END, cost=".9")
    complete = await report(runtime, actor, START, END, None, None, None, 100, 0, "json")
    assert complete["summary"]["requests"] == 1 and complete["summary"]["cost_usd"] == .1
    assert complete["coverage"]["capture_gap_turns"] == 0
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, turn.id)).usage = json.dumps({"requests": 6, "input_tokens": 80, "output_tokens": 50})
    incomplete = await report(runtime, actor, START, END, None, None, None, 100, 0, "json")
    assert incomplete["coverage"]["capture_gap_turns"] == 1
    assert incomplete["summary"]["requests"] == 1 and incomplete["summary"]["cost_usd"] == .1
    assert incomplete["summary"]["cost_coverage"] is None
    assert incomplete["summary"]["pricing_status"] == "partial"
    assert "not proof" in incomplete["methodology"]["coverage"]
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, turn.id)).preparation = runtime.security.seal("invalid preparation")
    unknown = await report(runtime, actor, START, END, None, None, None, 100, 0, "json")
    assert unknown["coverage"]["unknown_request_turns"] == 1
    assert unknown["summary"]["pricing_status"] == "partial"


async def test_model_filter_is_root_cohort_and_steering_inputs_do_not_invent_missing_inference(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    parent = await runtime.database.submit(actor, session.id, NewMessage(prompt="Synthetic", request_key=new_id()))
    await runtime.database.claim_next()
    ctx = SimpleNamespace(deps=(await runtime.database.prepare(parent.id)).scope)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).created_at = START
        (await db.get(Turn, parent.id)).created_at = START
    await ledger(runtime, actor, session, parent, timestamp=START, cost=".1", model="test")
    await ledger(runtime, actor, session, parent, timestamp=START, cost=".9", model="child")
    child = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Synthetic correction", request_key="included-correction", mode="asap",
    ))
    await runtime.database.steering_receipt(child.id, "native-enqueue-receipt")
    usage = {"requests": 2, "input_tokens": 40, "output_tokens": 20}
    assert await runtime.database.save_result(
        ctx.deps, conversation=ConversationTypeAdapter.dump_json(Conversation(usage=RunUsage(**usage))).decode(),
        output="Synthetic result", workspace=None, usage=usage,
    )
    await runtime.database.settle(parent.id)
    async with runtime.database.sessions.begin() as db:
        included = await db.get(Turn, child.id)
        assert included.status == "included" and included.parent_turn == parent.id
        assert included.preparation is None and included.usage == "{}"
        included.created_at = "2026-10-02T00:00:00+00:00"
    for model in (None, "test"):
        result = await report(runtime, actor, START, END, model, None, None, 100, 0, "json")
        assert result["summary"]["turns"] == 2 and result["summary"]["requests"] == 2
        assert result["summary"]["cost_usd"] == 1
        assert result["summary"]["pricing_status"] == "priced" and result["summary"]["cost_coverage"] == 1
        assert result["coverage"]["unknown_request_turns"] == 0
        assert result["coverage"]["capture_gap_turns"] == 0 and result["summary"]["unpriced_turns"] == 0
        assert {row["model"]: row["cost_usd"] for row in result["by_model"]} == {"test": .1, "child": .9}
        included_row = next(row for row in result["rows"] if row["id"] == child.id)
        assert included_row["usage_role"] == "steering_input" and included_row["requests"] == 0
        root_row = next(row for row in result["rows"] if row["id"] == parent.id)
        assert root_row["cost_models"] == ["child", "test"]
    child_only = await report(runtime, actor, START, END, "child", None, None, 100, 0, "json")
    assert child_only["rows"] == [] and child_only["summary"]["requests"] == 0
