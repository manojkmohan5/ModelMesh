"""Native persistence/authorization adapter for reusable analytics arithmetic."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Response
from sqlalchemy import func, select

from . import performance_report
from .analytics import (
    ReportRange,
    UsageEvent,
    actual_cost,
    csv_export,
    daily_usage,
    integer,
    model_totals,
    percentile,
    report_range,
    usage_totals,
    utc,
)
from .inference_usage import InferenceUsage
from .models import Session, Turn, User

MAX_IDENTITIES = 50_000
MAX_TURNS = 10_000
MAX_INFERENCES = 50_000
MAX_BASELINE_BYTES = 8 * 1024 * 1024
USAGE_COUNTERS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "tool_calls")
TURN_COLUMNS = (
    Turn.id, Turn.actor_id, Turn.session_id, Turn.model, Turn.status, Turn.usage,
    Turn.created_at, Turn.accepted_at, Turn.finished_at,
    Turn.parent_turn, Turn.preparation.is_not(None).label("has_preparation"), Turn.workflow_id,
)


def usage_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def record_time(value: str) -> datetime:
    parsed = utc(value)
    if parsed is None:
        raise HTTPException(503, "An analytics record has an invalid timestamp.")
    return parsed


async def usage_deltas(runtime: Any, db: Any, records: list[Any]) -> dict[str, dict[str, int | None]]:
    """Subtract the exact admitted seed; never sum cumulative SDK RunUsage.

    Preparation contains private content, so read a bounded amount only inside
    this adapter and return counters alone. Missing/oversize/invalid seeds stay
    unknown, rather than borrowing another turn's possibly different context.
    """
    lengths = (await db.execute(select(Turn.id, func.length(Turn.preparation)).where(
        Turn.id.in_([row.id for row in records]),
    ))).all()
    selected, remaining = [], MAX_BASELINE_BYTES
    for identity, size in sorted(lengths, key=lambda item: item[1] or 0):
        if size is not None and 0 < size <= min(2 * 1024 * 1024, remaining):
            selected.append(identity)
            remaining -= size
    baselines: dict[str, dict[str, Any] | None] = {}
    for identity, encrypted in (await db.execute(select(Turn.id, Turn.preparation).where(Turn.id.in_(selected)))).all():
        try:
            prepared = json.loads(runtime.security.unseal(encrypted))
            if not isinstance(prepared, dict) or "conversation" not in prepared:
                continue
            if prepared["conversation"] is None:
                baselines[identity] = dict.fromkeys(USAGE_COUNTERS, 0)
            elif isinstance(prepared["conversation"], str):
                conversation = json.loads(prepared["conversation"])
                seed = conversation.get("usage") if isinstance(conversation, dict) else None
                if isinstance(seed, dict):
                    baselines[identity] = seed
        except (ValueError, TypeError, InvalidToken, UnicodeError):
            continue
    result = {}
    for row in records:
        current, seed = usage_json(row.usage), baselines.get(row.id)
        deltas = {}
        for key in USAGE_COUNTERS:
            value = integer(current.get(key))
            previous = integer(seed.get(key)) if seed is not None else None
            deltas[key] = value - previous if value is not None and previous is not None and value >= previous else None
        result[row.id] = deltas
    return result


def legacy_event(row: Any, usage: dict[str, int | None]) -> UsageEvent:
    inputs, outputs = usage["input_tokens"], usage["output_tokens"]
    return UsageEvent(
        f"legacy:{row.id}", row.id, row.session_id, row.actor_id, row.model, record_time(row.created_at),
        requests=usage["requests"] or 0, input_tokens=inputs, output_tokens=outputs,
        total_tokens=inputs + outputs if inputs is not None and outputs is not None else None,
        cache_read_tokens=usage["cache_read_tokens"],
        cache_write_tokens=usage["cache_write_tokens"], legacy=True,
        request_count_known=usage["requests"] is not None,
    )


async def report_users(runtime: Any) -> list[dict[str, Any]]:
    """Report labels for every account; never an authentication resolver."""
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(User).order_by(User.username).limit(MAX_IDENTITIES + 1)))
    if len(rows) > MAX_IDENTITIES:
        raise HTTPException(422, "Too many report identities.")
    return [{key: getattr(row, key) for key in (
        "id", "username", "display_name", "role", "enabled", "auth_version",
    )} for row in rows]


def inference_event(row: InferenceUsage) -> UsageEvent:
    return UsageEvent(
        row.id, row.turn_id, row.session_id, row.actor_id, row.model, record_time(row.created_at),
        requests=0 if row.status == "not_dispatched" else 1,
        input_tokens=integer(row.input_tokens), output_tokens=integer(row.output_tokens),
        total_tokens=integer(row.total_tokens), cache_read_tokens=integer(row.cache_read_tokens),
        cache_write_tokens=integer(row.cache_write_tokens),
        cost=actual_cost(row.cost_usd, row.cost_status, row.cost_source),
    )


def elapsed(start: str | None, end: str | None) -> float | None:
    lower, upper = utc(start), utc(end)
    return (upper - lower).total_seconds() * 1000 if lower and upper and upper >= lower else None


def turn_view(
    row: Any, events: list[UsageEvent], delta: dict[str, int | None], unknown: set[str], gaps: set[str],
    *, steering: bool = False,
) -> dict[str, Any]:
    totals = usage_totals(events, unknown_turns=unknown & {row.id}, capture_gaps=gaps & {row.id})
    return {
        "id": row.id, "user_id": row.actor_id, "model": row.model, "status": row.status,
        "cost_models": sorted({event.model for event in events}),
        "usage_role": "steering_input" if steering else "inference_turn",
        "created_at": row.created_at, "accepted_at": row.accepted_at, "finished_at": row.finished_at,
        **totals.view(),
        "tool_calls": delta["tool_calls"] or 0,
        "queue_ms": elapsed(row.created_at, row.accepted_at),
        "latency_ms": elapsed(row.accepted_at, row.finished_at),
        "total_ms": elapsed(row.created_at, row.finished_at),
    }


def turn_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [row["latency_ms"] for row in rows if row["latency_ms"] is not None]
    return {
        "turns": len(rows), "completed": sum(row["status"] == "completed" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "latency_samples": len(durations),
        "mean_latency_ms": sum(durations) / len(durations) if durations else None,
        "p50_latency_ms": percentile(durations, .5), "p95_latency_ms": percentile(durations, .95),
        "priced_turns": sum(row["pricing_status"] == "priced" for row in rows if row["usage_role"] != "steering_input"),
        "unpriced_turns": sum(row["pricing_status"] in {"partial", "unpriced"} for row in rows
                             if row["usage_role"] != "steering_input"),
    }


def filtered(query: Any, model: str | None, status: str | None, actor_ids: set[str] | None, *, table: Any = Turn):
    if model:
        query = query.where(table.model == model)
    if actor_ids is not None:
        query = query.where(table.actor_id.in_(actor_ids))
    if status:
        query = query.where(Turn.status == status)
    return query


async def report(
    runtime: Any, user: User, start: str | None, end: str | None, model: str | None,
    status: str | None, user_id: str | None, limit: int, offset: int, format: str,
    *, preset: str = "7d", export: str = "turns",
) -> Any:
    if user.role != "admin" or not user.enabled:
        raise HTTPException(403, "Administrator access is required.")
    try:
        period = report_range(preset, start, end)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    lower, upper = period.start.isoformat(), period.end.isoformat()
    users = await report_users(runtime)
    actor_ids = {user_id} if user_id else None
    async with runtime.database.sessions() as db:
        turns = list((await db.execute(filtered(select(*TURN_COLUMNS).where(
            Turn.created_at >= lower, Turn.created_at < upper,
        ), model, status, actor_ids).limit(MAX_TURNS + 1))).all())
        ledger_query = select(InferenceUsage).join(
            Turn, Turn.id == InferenceUsage.turn_id, isouter=True,
        ).where(InferenceUsage.created_at >= lower, InferenceUsage.created_at < upper)
        if model:
            ledger_query = ledger_query.where(Turn.model == model)
        ledger = list(await db.scalars(filtered(
            ledger_query, None, status, actor_ids, table=InferenceUsage,
        ).limit(MAX_INFERENCES + 1)))
        if len(turns) > MAX_TURNS or len(ledger) > MAX_INFERENCES:
            raise HTTPException(422, "Too many matching records. Narrow the date range or filters.")
        ids = {row.id for row in turns}
        missing_ids = {row.turn_id for row in ledger} - ids
        # Native admission IDs are bounded by ledger records; do not load prompt,
        # result, conversation or session title columns.
        if missing_ids:
            turns.extend((await db.execute(select(*TURN_COLUMNS).where(Turn.id.in_(missing_ids)))).all())
        if len(turns) > MAX_TURNS:
            raise HTTPException(422, "More than 10,000 active turns match. Narrow the filters.")
        timing = await performance_report.observations(db, turns, period)
        ledger_counts = dict((await db.execute(select(InferenceUsage.turn_id, func.count()).where(
            InferenceUsage.turn_id.in_([row.id for row in turns]),
        ).group_by(InferenceUsage.turn_id))).all())
        steering = {row.id for row in turns if row.parent_turn and not row.has_preparation and row.id not in ledger_counts}
        inference_turns = [row for row in turns if row.id not in steering]
        deltas = await usage_deltas(runtime, db, inference_turns)
        unknown = {row.id for row in inference_turns if deltas[row.id]["requests"] is None}
        gaps = {
            row.id for row in inference_turns if ledger_counts.get(row.id, 0)
            and deltas[row.id]["requests"] is not None
            and deltas[row.id]["requests"] > ledger_counts[row.id]
        }
        session_query = select(Session.id, Session.owner_id, Session.created_at).where(
            Session.created_at >= lower, Session.created_at < upper,
        )
        if actor_ids is not None:
            session_query = session_query.where(Session.owner_id.in_(actor_ids))
        if model:
            session_query = session_query.where(Session.model == model)
        if status:
            session_query = session_query.where(select(Turn.id).where(
                Turn.session_id == Session.id, Turn.status == status,
            ).exists())
        created_sessions = list((await db.execute(session_query.limit(MAX_TURNS + 1))).all())
        if len(created_sessions) > MAX_TURNS:
            raise HTTPException(422, "Too many sessions match. Narrow the filters.")
    events = [inference_event(row) for row in ledger]
    events.extend(legacy_event(row, deltas[row.id]) for row in inference_turns if row.id not in ledger_counts)
    per_turn: dict[str, list[UsageEvent]] = defaultdict(list)
    for event in events:
        per_turn[event.turn_id].append(event)
    turns.sort(key=lambda row: (row.created_at, row.id), reverse=True)
    rows = [turn_view(
        row, per_turn[row.id], deltas.get(row.id, dict.fromkeys(USAGE_COUNTERS, 0)), unknown, gaps,
        steering=row.id in steering,
    ) for row in turns]
    for row in rows:
        row.update(timing.get(row["id"], {
            **performance_report.timing_view({}),
            **{f"{name}_timing_status": "outside_range" for name in performance_report.CARD_FIELDS},
        }))
    active_sessions = {row.session_id for row in turns} | {row.session_id for row in ledger}
    totals = usage_totals(events, unknown_turns=unknown, capture_gaps=gaps)
    summary = {
        **turn_metrics(rows), **totals.view(), "sessions": len(active_sessions),
        "sessions_active": len(active_sessions), "sessions_created": len(created_sessions),
    }
    by_user = []
    contributors = (
        {row.actor_id for row in turns} | {item.user_id for item in events}
        | {row.owner_id for row in created_sessions}
    )
    for identity in sorted(contributors):
        user_events = [item for item in events if item.user_id == identity]
        user_turns = [row for row in turns if row.actor_id == identity]
        user_ids = {row.id for row in user_turns}
        active = {row.session_id for row in user_turns} | {item.session_id for item in user_events}
        by_user.append({
            "user_id": identity, **turn_metrics([row for row in rows if row["user_id"] == identity]),
            **usage_totals(user_events, unknown_turns=unknown & user_ids, capture_gaps=gaps & user_ids).view(),
            "sessions": len(active), "sessions_active": len(active),
            "sessions_created": sum(row.owner_id == identity for row in created_sessions),
        })
    by_user.sort(key=lambda row: (-row["turns"], row["user_id"]))
    series = daily_usage(events, period)
    cumulative_incomplete = False
    for day in series:
        day_start = utc(day["date"])
        assert day_start is not None
        day_period = ReportRange(max(day_start, period.start), min(day_start + timedelta(days=1), period.end), "custom")
        day_events = [item for item in events if day_period.contains(item.created_at)]
        created_turns = [row for row in turns if day_period.contains(utc(row.created_at))]
        day_turn_ids = {item.turn_id for item in day_events} | {row.id for row in created_turns}
        day_active = {item.session_id for item in day_events} | {row.session_id for row in created_turns}
        possible_ids = {
            row.id for row in turns if record_time(row.created_at) < day_period.end
            and (not row.finished_at or record_time(row.finished_at) >= day_period.start)
        }
        day_unknown, day_gaps = unknown & possible_ids, gaps & possible_ids
        cumulative_incomplete |= bool(day_unknown or day_gaps)
        if cumulative_incomplete:
            day["cumulative_pricing_status"] = "partial" if day["cumulative_cost_usd"] is not None else "unpriced"
        day.update({
            **usage_totals(day_events, unknown_turns=day_unknown, capture_gaps=day_gaps).view(),
            **turn_metrics([row for row in rows if row["id"] in day_turn_ids]),
            "sessions": len(day_active), "sessions_active": len(day_active),
            "sessions_created": sum(day_period.contains(utc(row.created_at)) for row in created_sessions),
        })
        for entry in day["models"]:
            model_ids = {row.id for row in turns if row.model == entry["model"]} | {
                item.turn_id for item in events if item.model == entry["model"]
            }
            entry.update(usage_totals(
                (item for item in day_events if item.model == entry["model"]),
                unknown_turns=day_unknown & model_ids, capture_gaps=day_gaps & model_ids,
            ).view())
    by_model = model_totals(events)
    for entry in by_model:
        model_ids = {row.id for row in turns if row.model == entry["model"]} | {
            item.turn_id for item in events if item.model == entry["model"]
        }
        entry.update(usage_totals(
            (item for item in events if item.model == entry["model"]),
            unknown_turns=unknown & model_ids, capture_gaps=gaps & model_ids,
        ).view())
    if format == "csv":
        exported = rows if export == "turns" else by_user if export == "users" else series if export == "daily" else by_model
        if export == "users":
            user_labels = {row["id"]: row for row in users}
            exported = [{
                **row, "display_name": user_labels.get(row["user_id"], {}).get("display_name"),
                "username": user_labels.get(row["user_id"], {}).get("username"),
            } for row in exported]
        flattened = [{
            **row, **({"cost_models": json.dumps(row["cost_models"])} if "cost_models" in row else {}),
        } for row in exported]
        columns = list(dict.fromkeys(
            key for row in flattened for key in row if key != "models"
        )) or (["id"] if export == "turns" else ["user_id"] if export == "users" else ["date"] if export == "daily" else ["model"])
        # The exact decimal subtotal is exported alongside its numeric chart value.
        response: Any = Response(
            csv_export(flattened, columns), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="modelmesh-{export}.csv"'},
        )
    else:
        response = {
            "rows": rows[offset:offset + limit], "summary": summary, "series": series,
            "by_user": by_user, "by_model": by_model,
            "models": sorted({row.model for row in turns}),
            "users": users, "total": len(rows), "limit": limit, "offset": offset,
            "start": lower, "end": upper, "preset": period.preset, "timezone": "UTC", "currency": "USD",
            "performance": performance_report.summarize(timing, turns, period),
            "coverage": {
                "legacy_turns": len(totals.legacy_turns), "ledger_requests": len(ledger),
                "unknown_request_turns": len(totals.unknown_request_turns),
                "capture_gap_turns": len(gaps),
                "priced_requests": totals.priced_requests, "unpriced_requests": totals.unpriced_requests,
            },
            "methodology": {
                **performance_report.METHODOLOGY,
                "users": "User totals and filters use each recorded account ID.",
                "sessions": "Created uses Session.created_at; active is distinct sessions with a newly admitted turn or recorded inference in range. Daily active counts are distinct per UTC date.",
                "cost": "Actual reported USD subtotal only; partial coverage excludes missing amounts, never estimates them. Inference attempts use their recorded timestamp. Historical usage subtracts the exact admitted conversation's cumulative seed and stays unknown if that seed is unavailable. Only turns with no attempt ledger use this unpriced fallback.",
                "requests": "New ledger requests count observed HTTP attempts, including retries. Historical fallback uses the admitted-seed usage delta. Attached steering inputs remain in input/turn counts but their inference belongs to the parent turn; they do not add missing-cost records.",
                "coverage": "Coverage concerns observed records. A verified SDK request delta above ALL attempt rows demonstrates a capture gap; retries can mask gaps, so absence of that signal is not proof of complete costs. Baseline reads are bounded and missing baselines remain unknown.",
                "model": "The model filter selects root turns and their session cohort, including child-model attempts. Ledger breakdowns use the actual requested alias. Historical fallback is attributed only to its recorded root model and marked legacy; historical per-request aliases are unavailable.",
            },
            "capabilities": {"reported_costs": True},
            "limits": {"max_turns": MAX_TURNS, "max_inferences": MAX_INFERENCES,
                       "max_baseline_bytes_per_cohort": MAX_BASELINE_BYTES},
            "cost_note": "Only reported costs are summed. Missing and historical prices remain unpriced; partial subtotals exclude them.",
            "latency_note": "Latency is accepted-to-finished wall time, including tools and waits; it is not model TTFT.",
        }
    fresh = await runtime.database.user(user.id)
    if fresh.role != "admin" or fresh.auth_version != user.auth_version:
        raise HTTPException(401, "Your access changed. Sign in again.")
    return response
