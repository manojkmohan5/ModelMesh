"""Account-revocation/admission races on native SQLite."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from test_native_settings import automation, client_for, member

from modelmesh import settings_api
from modelmesh.contracts import NewMessage
from modelmesh.models import Message, Turn, User
from modelmesh.settings_models import SettingAutomationRun
from modelmesh.settings_runtime import admit_automation


async def administrator(runtime):
    async with runtime.database.sessions() as db:
        return await db.scalar(select(User).where(User.username == runtime.settings.administrator))


@pytest.mark.parametrize("revocation", ["demote", "disable"])
async def test_stale_automation_owner_is_denied_before_any_native_turn_is_written(
    runtime, monkeypatch, revocation,
):
    """Regress the actual stale-owner lookup gap, including durable retry behavior."""
    admin = await administrator(runtime)
    owner = await member(runtime, role="admin")
    schedule = await automation(runtime, owner)
    looked_up, release = asyncio.Event(), asyncio.Event()
    original_user = runtime.database.user

    async def read_then_wait(identity):
        user = await original_user(identity)
        if identity == owner.id and not looked_up.is_set():
            looked_up.set()
            await release.wait()
        return user

    monkeypatch.setattr(runtime.database, "user", read_then_wait)
    admission = asyncio.create_task(admit_automation(runtime, schedule.id, "stale-owner"))
    async with client_for(runtime, admin) as client:
        try:
            await asyncio.wait_for(looked_up.wait(), 5)
            response = await client.patch(
                f"/api/settings/users/{owner.id}",
                json={"role": "member"} if revocation == "demote" else {"enabled": False},
            )
            assert response.status_code == 200, response.text
            assert response.json()["auth_version"] == owner.auth_version + 1
            release.set()
            assert await asyncio.wait_for(admission, 5) == {"status": "denied"}
        finally:
            release.set()
            await asyncio.gather(admission, return_exceptions=True)

        # Retry the same Temporal occurrence: denial must not enqueue work.
        assert await admit_automation(runtime, schedule.id, "stale-owner") == {"status": "denied"}
        async with runtime.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(Turn)) == 0
            assert await db.scalar(select(func.count()).select_from(Message)) == 0
            receipt = await db.scalar(select(SettingAutomationRun).where(
                SettingAutomationRun.automation_id == schedule.id,
            ))
            assert receipt.status == "denied" and receipt.turn_id is None
        assert await runtime.database.claim_next() is None

        # The fence must still permit newly authorized, explicit work.
        if revocation == "disable":
            assert (await client.patch(
                f"/api/settings/users/{owner.id}", json={"enabled": True},
            )).status_code == 200
        fresh = await original_user(owner.id)
        following = await runtime.database.submit(fresh, schedule.session_id, NewMessage(
            prompt="New explicitly authorized task.", request_key="fresh-authority",
        ))
        assert (await runtime.database.claim_next()).id == following.id
        assert (await runtime.database.prepare(following.id)).scope.actor_id == owner.id


async def test_revocation_waits_for_admission_commit_then_stops_the_admitted_turn(runtime, monkeypatch):
    """The opposite ordering must serialize too, rather than deadlock or leave queued work."""
    admin = await administrator(runtime)
    owner = await member(runtime, role="admin")
    schedule = await automation(runtime, owner)
    admitting, release, revoking = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_bind = runtime.database.swarms.bind
    original_change = settings_api.change_user

    async def bind_then_wait(*args, **kwargs):
        # Called inside the admission transaction, after the turn is written.
        await original_bind(*args, **kwargs)
        admitting.set()
        await release.wait()

    async def observed_change(*args, **kwargs):
        revoking.set()
        return await original_change(*args, **kwargs)

    monkeypatch.setattr(runtime.database.swarms, "bind", bind_then_wait)
    monkeypatch.setattr(settings_api, "change_user", observed_change)
    admission = asyncio.create_task(admit_automation(runtime, schedule.id, "admission-first"))
    revocation = None
    async with client_for(runtime, admin) as client:
        try:
            await asyncio.wait_for(admitting.wait(), 5)
            revocation = asyncio.create_task(client.patch(
                f"/api/settings/users/{owner.id}", json={"role": "member"},
            ))
            await asyncio.wait_for(revoking.wait(), 5)
            done, _ = await asyncio.wait({revocation}, timeout=0.1)
            assert not done, "Revocation must wait for the in-flight native admission transaction."
            release.set()
            result, response = await asyncio.wait_for(asyncio.gather(admission, revocation), 10)
            assert result["status"] == "admitted"
            assert response.status_code == 200, response.text
        finally:
            release.set()
            await asyncio.gather(
                admission, *([revocation] if revocation else []), return_exceptions=True,
            )

    async with runtime.database.sessions() as db:
        turn = await db.get(Turn, result["turn_id"])
        assert turn.status == "stopped"
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.turn_id == turn.id,
        )) == 1
        receipt = await db.scalar(select(SettingAutomationRun).where(
            SettingAutomationRun.automation_id == schedule.id,
        ))
        assert receipt.status == "admitted" and receipt.turn_id == turn.id
    assert await runtime.database.claim_next() is None
