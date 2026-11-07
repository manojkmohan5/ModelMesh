"""Durable batching and polling regressions."""

from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from temporalio.service import RPCError, RPCStatusCode

from modelmesh.contracts import NewMessage, NewSession
from modelmesh.worker import DispatchState, dispatch_once, dispatcher


async def submissions(database, actor, count):
    turns = []
    for index in range(count):
        session = await database.create_session(actor, NewSession())
        turns.append(await database.submit(
            actor, session.id, NewMessage(prompt=f"Task {index}", request_key=str(uuid4())),
        ))
    return turns


async def test_burst_drains_in_bounded_parallel_batches_without_waiting_for_worker_start(runtime, actor):
    runtime.settings.temporal_dispatch_batch_size = 7
    runtime.settings.temporal_dispatch_concurrency = 3
    turns = await submissions(runtime.database, actor, 37)
    active = peak = 0
    started = []

    async def start(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(kwargs["id"])
        try:
            await asyncio.sleep(0.01)
        finally:
            active -= 1

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=start))
    state = DispatchState()
    for _ in range(10):
        await dispatch_once(runtime, state)
        if not state.backlog:
            break
    assert not state.backlog
    assert peak == 3
    assert len(started) == len(turns)
    assert set(started) == {turn.workflow_id for turn in turns}
    # No execution worker consumed these rows. Paging must not repeatedly start
    # only the oldest batch while newly claimed turns remain undispatched.
    assert len(await runtime.database.starting_turns(limit=100)) == 37


async def test_dispatch_loop_does_not_poll_sleep_between_full_batches(runtime, actor, monkeypatch):
    from modelmesh import worker

    monkeypatch.setattr(worker, "DISPATCH_POLL_SECONDS", 30)
    runtime.settings.temporal_dispatch_batch_size = 4
    turns = await submissions(runtime.database, actor, 13)
    started = set()
    complete = asyncio.Event()

    async def start(*args, **kwargs):
        started.add(kwargs["id"])
        if len(started) == len(turns):
            complete.set()

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=start))
    task = asyncio.create_task(dispatcher(runtime))
    try:
        await asyncio.wait_for(complete.wait(), 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(started) == 13


async def test_batch_reserves_only_one_turn_per_session_and_fills_other_sessions(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    queued = [await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Serial", request_key=str(uuid4())),
    ) for _ in range(12)]
    others = await submissions(runtime.database, actor, 5)
    claimed = await runtime.database.claim_batch(6)
    assert len(claimed) == 6
    assert {turn.session_id for turn in claimed} == {session.id, *(turn.session_id for turn in others)}
    assert claimed[0].id == queued[0].id
    assert await runtime.database.claim_batch(6) == []


async def test_continuous_arrivals_cannot_starve_failed_starts_or_scan_tail(runtime, actor):
    runtime.settings.temporal_dispatch_batch_size = 2
    runtime.settings.temporal_dispatch_concurrency = 2
    initial = await submissions(runtime.database, actor, 6)
    attempts = Counter()

    async def start(*args, **kwargs):
        identity = kwargs["id"]
        attempts[identity] += 1
        if identity == initial[0].workflow_id and attempts[identity] == 1:
            raise RPCError("Temporary outage.", RPCStatusCode.UNAVAILABLE, b"")

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=start))
    state = DispatchState()
    arrivals = []
    retried_at = None
    for iteration in range(20):
        arrivals.extend(await submissions(runtime.database, actor, 2))
        await dispatch_once(runtime, state)
        if attempts[initial[0].workflow_id] >= 2 and retried_at is None:
            retried_at = iteration
    assert retried_at is not None and retried_at <= 5
    assert all(attempts[turn.workflow_id] > 0 for turn in initial)
    assert all(attempts[turn.workflow_id] > 0 for turn in arrivals[:6])


async def test_idle_dispatcher_picks_up_new_work_by_polling(runtime, actor):
    completed = asyncio.Event()
    client = SimpleNamespace(start_workflow=AsyncMock(side_effect=lambda *a, **k: completed.set()))
    runtime.connect = AsyncMock(return_value=client)
    task = asyncio.create_task(dispatcher(runtime))
    try:
        await asyncio.sleep(0.1)  # Let the dispatcher go idle before the work arrives.
        await submissions(runtime.database, actor, 1)
        # Nothing wakes the dispatcher early; its next poll finds the saved turn.
        await asyncio.wait_for(completed.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    client.start_workflow.assert_awaited_once()
