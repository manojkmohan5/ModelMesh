"""Read public commentary from the Harness's authoritative step snapshots."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from .commentary import saved_commentary
from .history_handoff import latest_turn_snapshot
from .models import Event, Turn
from .presentation import redact, saved_activity

if TYPE_CHECKING:
    from .runtime import Runtime


async def turn_progress(runtime: Runtime, turn: Turn) -> list[dict[str, str]]:
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Event).where(
            Event.turn_id == turn.id, Event.kind == "commentary",
        ).order_by(Event.id)))
    if rows:
        messages = [
            {"created_at": row.created_at, **json.loads(runtime.security.unseal(row.content))}
            for row in rows
        ]
    else:
        # Older turns predate the projection. Their native snapshot still offers
        # whatever public commentary remains in that saved conversation.
        snapshot = await latest_turn_snapshot(runtime, turn.id)
        messages = saved_commentary(snapshot.messages, turn.id) if snapshot else []
    return redact(messages, progress_secrets(runtime, turn))


def progress_secrets(runtime: Runtime, turn: Turn) -> list[str]:
    """The values every public progress projection redacts: the model gateway key."""
    return [runtime.settings.gateway_key.get_secret_value()]


async def turn_activity(runtime: Runtime, turn: Turn) -> list[dict[str, Any]]:
    """Use the live feed's credential redaction for saved activity too."""
    snapshot = await latest_turn_snapshot(runtime, turn.id)
    return redact(saved_activity(snapshot.messages, turn.id), progress_secrets(runtime, turn)) if snapshot else []
