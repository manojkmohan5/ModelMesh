"""Fresh authority for each SSE release, without redundant native checkouts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

from .models import User

if TYPE_CHECKING:
    from .database import Database


async def authorize_stream_viewer(database: Database, user: User, session_id: str) -> None:
    # A new session per check keeps native controls and ACLs fresh. Reuse its
    # connection for the checks, never its identity map across releases.
    async with database.sessions() as db:
        fresh = await db.get(User, user.id)
        if fresh is None or not fresh.enabled or fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your access changed.")
        await database.allowed_session(db, session_id, user)
