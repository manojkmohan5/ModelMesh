"""The stream still revalidates every authority boundary on every release."""

from __future__ import annotations

import pytest
from sqlalchemy import event, update

from modelmesh.contracts import NewSession
from modelmesh.live_stream import authorized_events
from modelmesh.models import Session, User
from modelmesh.stream_authority import authorize_stream_viewer


async def test_native_checks_share_one_checkout_without_caching(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    checkouts = []

    def checkout(*_):
        checkouts.append(True)

    event.listen(runtime.database.engine.sync_engine, "checkout", checkout)
    try:
        for _ in range(2):
            await authorize_stream_viewer(runtime.database, actor, session.id)
        assert len(checkouts) == 2
    finally:
        event.remove(runtime.database.engine.sync_engine, "checkout", checkout)


@pytest.mark.parametrize("revocation", ["disable", "version", "delete", "owner"])
async def test_revoked_native_authority_never_releases_ready_content(runtime, actor, revocation):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))

    async def authorize():
        await authorize_stream_viewer(runtime.database, actor, session.id)

    async def source():
        async with runtime.database.sessions.begin() as db:
            if revocation in {"disable", "version"}:
                values = {"enabled": False} if revocation == "disable" else {"auth_version": actor.auth_version + 1}
                await db.execute(update(User).where(User.id == actor.id).values(**values))
            elif revocation == "delete":
                await db.execute(update(Session).where(Session.id == session.id).values(deleted=True))
            else:
                other = User(username="other", display_name="Other", password="unused", role="member")
                db.add(other)
                await db.flush()
                await db.execute(update(Session).where(Session.id == session.id).values(owner_id=other.id))
        yield "private"

    async def disconnected():
        return False

    assert [item async for item in authorized_events(source(), authorize, disconnected)] == [
        "event: access_revoked\ndata: {}\n\n"
    ]
