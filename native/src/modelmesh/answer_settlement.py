"""Accept answers and settle their turns without losing durable evidence.

Temporal retains the existing activity identities and ordering. This module owns
result acceptance and answer, failure and Stop settlement.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter
from sqlalchemy import select, update

from .contracts import NO_GRANTS_KEY, PreparedTurn, RunScope
from .failed_action_attempts import settle_failed_action_attempts
from .failure_diagnostics import FAILURE_KIND, safe_failure_metadata
from .models import Event, Message, Session, Turn, now

if TYPE_CHECKING:
    from .database import Database

SETTLEMENT_WARNING = (
    "Your response is saved, but this turn did not finish cleanly. Files on the computer are unchanged."
)


class AnswerSettlement:
    def __init__(self, database: Database):
        self.database = database

    async def save_result(
        self, scope: RunScope, *, conversation: str, output: str,
        workspace: dict[str, Any] | None, usage: dict[str, Any],
        deferred: bool = False,
    ) -> bool:
        result_identity = {
            "conversation": conversation, "output": output, "workspace": workspace,
            "usage": usage, "deferred": deferred,
        }
        if deferred and scope.background_delegation:
            result_identity["background_agents"] = scope.background_agents
        fingerprint = hashlib.sha256(json.dumps(result_identity, sort_keys=True).encode()).hexdigest()
        async with self.database.sessions.begin() as db:
            turn = await db.get(Turn, scope.turn_id)
            session = await db.get(Session, scope.session_id)
            if turn is None or session is None:
                raise ValueError("Unknown turn.")
            if turn.result_fingerprint is not None:
                if turn.result_fingerprint != fingerprint:
                    raise ValueError("The completed result for this turn is immutable.")
                return bool(turn.result_accepted)
            # This is the first write in the transaction. Stop fences the same
            # session row first, so whichever transaction wins decides publication.
            accepted = await db.scalar(update(Session).where(
                Session.id == scope.session_id,
                Session.active_turn == turn.id,
                Session.status == "running",
            ).values(
                conversation=self.database.security.seal(conversation),
                workspace=json.dumps(workspace) if workspace else session.workspace,
                workspace_scope=scope.workspace_key if workspace else session.workspace_scope,
                last_actor_id=scope.actor_id, last_model=scope.model,
                last_grants=NO_GRANTS_KEY,
                revision=Session.revision + 1, updated_at=now(),
                status="waiting" if deferred else "saving",
            ).returning(Session.id))
            # Preserve the factual result, even when Stop won the publication race.
            turn.result = self.database.security.seal(output)
            turn.conversation = self.database.security.seal(conversation)
            turn.result_fingerprint = fingerprint
            turn.usage = json.dumps(usage)
            turn.finished_at = now()
            if accepted is None:
                turn.result_accepted = False
                return False
            turn.result_accepted = True
            turn.status = "waiting" if deferred else "saving"
            completed_agents: set[str] = set()
            if scope.agent_messages:
                from .agent_messages import acknowledge_mail, receipts
                ids, completed_agents = receipts(ConversationTypeAdapter.validate_json(conversation).messages)
                # Persist consumption with the snapshot, including final-drain
                # messages whose SDK delivery event has not reached its hook.
                await acknowledge_mail(db, scope.session_id, scope.turn_id, ids)
            if deferred:
                from .questions import record_optional_deadline
                record_optional_deadline(self.database, db, turn)
                if scope.background_delegation and turn.preparation:
                    prepared = TypeAdapter(PreparedTurn).validate_json(self.database.security.unseal(turn.preparation))
                    prepared.scope.background_delegation = True
                    prepared.scope.background_agents = [
                        child for child in scope.background_agents if child not in completed_agents
                    ]
                    turn.preparation = self.database.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
            await db.execute(update(Turn).where(
                Turn.parent_turn == turn.id, Turn.status == "accepted"
            ).values(status="included", finished_at=now()))
            if not deferred:
                existing = await db.scalar(select(Message.id).where(Message.identity == f"answer:{turn.id}"))
                if existing is None:
                    db.add(Message(
                        session_id=session.id, turn_id=turn.id, role="assistant",
                        content=self.database.security.seal(output), identity=f"answer:{turn.id}",
                    ))
                await db.flush()
                await settle_failed_action_attempts(db, session.id, accepted_turn_id=turn.id)
            return True

    async def settle(self, turn_id: str, *, warning: str | None = None) -> None:
        async with self.database._admission_session(None) as db:
            session_id = await db.scalar(select(Turn.session_id).where(Turn.id == turn_id))
            if session_id is None:
                raise ValueError("Unknown turn.")
            # Read the mutable settlement intent only after taking Stop's lock.
            # Otherwise a stale "saving" object can resurrect a stopped approval.
            await db.execute(update(Session).where(
                Session.id == session_id,
            ).values(revision=Session.revision))
            turn = await db.get(Turn, turn_id)
            if turn is None:
                raise ValueError("Unknown turn.")
            session = await db.get(Session, turn.session_id)
            if session is None or session.active_turn != turn.id or turn.status != "saving":
                return
            self._settle_answer(turn, session, warning=warning)

    def _settle_answer(self, turn: Turn, session: Session | None, *, warning: str | None = None) -> None:
        """Later cleanup cannot downgrade a published answer to failure or Stop."""
        turn.status = "completed"
        if session is not None and session.active_turn == turn.id:
            stopped = session.deleted or session.status == "stopped"
            session.save_warning = warning
            # The accepted answer stays completed, but its bookkeeping cannot
            # clear a later Stop or deletion.
            session.status = "stopped" if stopped else "idle"
            session.active_turn = None

    async def fail(
        self, turn_id: str, message: str, *, only_unsettled: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        async with self.database._admission_session(None) as db:
            # Resolve ownership without caching a mutable Turn. Stop, deletion
            # and answer publication fence the session first; take that same
            # lock before reading the outcome this failure is allowed to settle.
            session_id = await db.scalar(select(Turn.session_id).where(Turn.id == turn_id))
            if session_id is None:
                return
            locked = await db.scalar(update(Session).where(
                Session.id == session_id,
            ).values(revision=Session.revision).returning(Session.id))
            if locked is None:
                return
            turn = await db.get(Turn, turn_id)
            if turn is None:
                return
            if turn.status in ("completed", "stopped"):
                return
            if only_unsettled and turn.status not in ("starting", "running", "saving"):
                return
            session = await db.get(Session, session_id)
            if turn.status == "saving" and turn.result_accepted:
                self._settle_answer(
                    turn, session,
                    warning="The answer was saved, but finishing the turn was interrupted.",
                )
                return
            turn.status = "failed"
            turn.error = message
            turn.finished_at = now()
            details = safe_failure_metadata(diagnostics)
            identity = f"failure-diagnostics:{turn.id}"
            if details and not await db.scalar(select(Event.id).where(Event.identity == identity)):
                db.add(Event(
                    session_id=session_id, turn_id=turn.id, kind=FAILURE_KIND, identity=identity,
                    content=self.database.security.seal(details),
                ))
            await db.flush()
            await settle_failed_action_attempts(db, session_id)
            if session and session.active_turn == turn.id:
                session.active_turn = None
                session.status = "failed"
                session.updated_at = turn.finished_at or now()

    async def stopped(self, turn_id: str, conversation: str | None = None) -> None:
        async with self.database._admission_session(None) as db:
            session_id = await db.scalar(select(Turn.session_id).where(Turn.id == turn_id))
            if session_id is None:
                return
            await db.execute(update(Session).where(
                Session.id == session_id,
            ).values(revision=Session.revision))
            turn = await db.get(Turn, turn_id)
            if turn is None:
                return
            session = await db.get(Session, turn.session_id)
            if session is None or session.active_turn != turn.id:
                return
            if turn.status == "saving" and turn.result_accepted:
                self._settle_answer(
                    turn, session,
                    warning="The answer was saved before Stop interrupted the end of the turn.",
                )
                return
            if conversation:
                session.conversation = self.database.security.seal(conversation)
                session.last_actor_id = turn.actor_id
                session.last_model = turn.model
                if turn.preparation:
                    session.last_grants = NO_GRANTS_KEY
            turn.status = "stopped"
            turn.finished_at = now()
            session.active_turn = None
            session.status = "stopped"
            session.updated_at = turn.finished_at or now()
