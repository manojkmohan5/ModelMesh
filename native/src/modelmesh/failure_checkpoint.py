"""Preserve a failed turn's settled conversation before the turn releases its session.

This is not an execution retry. A new user turn must still pass ordinary
admission, current authority, and unresolved-effect checks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai_harness.step_persistence import is_provider_valid
from sqlalchemy import select, update

from .contracts import NO_GRANTS_KEY, PreparedTurn, RunScope
from .history_handoff import latest_turn_snapshot
from .models import Effect, Session, Turn, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database
    from .runtime import Runtime

logger = logging.getLogger(__name__)
WARNING = "Work stopped before this turn finished. Files on the computer are kept."


async def locked_scope(database: Database, db: AsyncSession, scope: RunScope, *, status: str) -> tuple[Session, Turn]:
    """Fence current authority and the turn's phase in source → actor → session lock order.

    Check the phase in the locking write, never against an earlier unlocked read.
    """
    turn = await db.get(Turn, scope.turn_id)
    if turn is None or turn.actor_id != scope.actor_id or turn.session_id != scope.session_id:
        raise PermissionError("The workspace task changed.")
    actor_id = await db.scalar(update(User).where(
        User.id == scope.actor_id, User.enabled.is_(True),
    ).values(auth_version=User.auth_version).returning(User.id))
    if actor_id is None:
        raise PermissionError("The workspace requester no longer has access.")
    locked = await db.scalar(update(Session).where(
        Session.id == scope.session_id, Session.active_turn == scope.turn_id,
        Session.status == status, Session.deleted.is_(False),
    ).values(revision=Session.revision).returning(Session.id))
    if locked is None:
        raise PermissionError("The workspace task stopped or was superseded.")
    session = await db.get(Session, scope.session_id, populate_existing=True)
    await db.refresh(turn)
    assert session is not None
    if (
        turn.status != session.status or session.visibility != scope.visibility
        or session.surface != scope.surface
        or (session.visibility == "private" and session.owner_id != scope.actor_id)
    ):
        raise PermissionError("The workspace authority changed.")
    await database.check_scope_context(db, scope, session)
    return session, turn


async def preserve_failed_workspace(runtime: Runtime, turn_id: str) -> None:
    database = runtime.database
    async with database.sessions() as db:
        turn = await db.get(Turn, turn_id)
        if turn is None or not turn.preparation or turn.result_accepted:
            return
        prepared = TypeAdapter(PreparedTurn).validate_json(database.security.unseal(turn.preparation))
    scope = prepared.scope
    try:
        snapshot = await latest_turn_snapshot(runtime, turn_id, include_interrupted=True)
        if snapshot is None or not is_provider_valid(snapshot.messages):
            snapshot = await latest_turn_snapshot(runtime, turn_id)
        if snapshot is None or not is_provider_valid(snapshot.messages):
            raise ValueError("No settled conversation is available.")
        conversation = ConversationTypeAdapter.dump_json(Conversation(
            messages=snapshot.messages, conversation_id=scope.conversation_id,
        ))
        async with database.sessions.begin() as db:
            session, turn = await locked_scope(database, db, scope, status="running")
            # A pending external write must retain its original review barrier.
            if await db.scalar(select(Effect.id).where(
                Effect.session_id == session.id, Effect.state == "pending",
            ).limit(1)):
                raise PermissionError("An earlier action still has an uncertain outcome.")
            session.conversation = turn.conversation = database.security.seal(conversation)
            session.last_actor_id, session.last_model = scope.actor_id, scope.model
            session.last_grants = NO_GRANTS_KEY
            session.revision += 1
            session.save_warning = WARNING
    except Exception:
        logger.warning("Could not preserve the failed turn's conversation.", exc_info=True)
        async with database.sessions.begin() as db:
            session = await db.get(Session, scope.session_id)
            if session and session.active_turn == turn_id and session.status == "running":
                session.save_warning = WARNING
