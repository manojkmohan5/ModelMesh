"""Async application persistence; no model/tool loop or competing run recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import and_, delete, event, func, or_, select, update
from sqlalchemy import insert as sql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import defer

from .answer_settlement import AnswerSettlement
from .contracts import (
    NO_GRANTS_KEY,
    ExecutionContext,
    GuidanceChanged,
    InputImage,
    NewMessage,
    NewSession,
    PreparedTurn,
    RunScope,
    SteeringInput,
)
from .delegation_authority import DelegatedAuthority, session_context
from .delegation_models import DelegatedSession, DelegatedTurn
from .failed_action_attempts import settle_failed_action_attempts
from .goal_models import Goal
from .goals import Goals
from .goals import dispatchable as goal_dispatchable
from .images import MEDIA_TYPES
from .model_router import AUTO_MODEL, ModelRouter, decision
from .models import (
    Base,
    Event,
    LoginAttempt,
    Message,
    Session,
    SessionFolder,
    SessionPreference,
    Turn,
    TurnImage,
    Upload,
    User,
    new_id,
    now,
)
from .security import Security, password_hash
from .settings import Settings
from .swarm_models import SwarmMember
from .swarm_service import SwarmService, dispatchable

# Read projections and authorization do not need the agent's private checkpoint.
# Fail explicitly if a caller accidentally tries to use an omitted field.
SESSION_METADATA = (defer(Session.conversation, raiseload=True),)


class Database:
    def __init__(self, settings: Settings, security: Security):
        self.settings = settings
        self.security = security
        self.performance_tasks: set[asyncio.Task[None]] = set()
        from .performance import DispatchRecorder
        self.dispatch_timings = DispatchRecorder(self)
        self.engine = create_async_engine(settings.sqlalchemy_url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.answers = AnswerSettlement(self)
        self.swarms = SwarmService(self)
        self.goals = Goals(self)
        self.router = ModelRouter(settings)

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            # WAL lets writers commit while a reader is open. In the default rollback journal one
            # leaked read (a cancelled live stream) blocked every write with "database is locked".
            # The mode is stored in the file, so this only changes a new or older database. When two
            # processes open a new database at once, one may lose the switch; the other makes it.
            with suppress(Exception):  # The driver wraps "database is locked" in its own type.
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    async def start(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            # Creates missing tables only; a change to an existing table's columns needs a migration step.
            await conn.run_sync(Base.metadata.create_all)
            # Adopt folder names saved by the original native API. This is
            # additive and runs under the same write lock.
            existing = select(SessionFolder.id).where(
                SessionFolder.user_id == SessionPreference.user_id,
                SessionFolder.name == SessionPreference.folder,
            ).exists()
            legacy = (await conn.execute(select(
                SessionPreference.user_id, SessionPreference.folder,
            ).where(SessionPreference.folder != "", ~existing).distinct())).all()
            for user_id, name in legacy:
                await conn.execute(sql_insert(SessionFolder).values(id=new_id(), user_id=user_id, name=name))
        password = self.settings.workspace_password.get_secret_value()
        if password:
            async with self.sessions.begin() as db:
                existing = await db.scalar(select(User).where(User.username == self.settings.administrator))
                if existing is None:
                    encoded = await asyncio.to_thread(password_hash, password)
                    await db.execute(sqlite_insert(User).values(
                        username=self.settings.administrator,
                        display_name="Administrator",
                        password=encoded,
                        role="admin",
                    ).on_conflict_do_nothing(index_elements=["username"]))

    async def close(self) -> None:
        if self.performance_tasks:
            await asyncio.gather(*self.performance_tasks, return_exceptions=True)
        await self.engine.dispose()

    async def user(self, user_id: str) -> User:
        async with self.sessions() as db:
            user = await db.get(User, user_id)
            if user is None or not user.enabled:
                raise HTTPException(401, "Sign in to continue.")
            return user

    async def limit_login(self, network: str, username: str) -> None:
        window = int(time.time()) // 60
        limited = False
        async with self.sessions.begin() as db:
            await db.execute(delete(LoginAttempt).where(LoginAttempt.window < window - 1))
            for label, limit in (
                (f"login:{network}:{username.casefold()}", 10), ("all-logins", 200),
            ):
                key = f"{window}:{self.security.object_id(label.encode())}"
                value = await db.scalar(sqlite_insert(LoginAttempt).values(
                    id=key, window=window, count=1,
                ).on_conflict_do_update(
                    index_elements=["id"], set_={"count": LoginAttempt.count + 1},
                    where=LoginAttempt.count < limit,
                ).returning(LoginAttempt.count))
                limited |= value is None
        if limited:
            raise HTTPException(429, "Too many sign-in attempts. Wait one minute.")

    async def allowed_session(
        self, db: AsyncSession, session_id: str, actor: User, *, metadata_only: bool = False,
    ) -> Session:
        """Authorize a fresh reader or input, including its current source audience."""
        session = await self.execution_session(db, session_id, actor, metadata_only=metadata_only)
        await self.check_source_audience(db, session, actor, metadata_only=metadata_only)
        member = await db.get(SwarmMember, session_id)
        if member is not None and member.mission_id != session_id:
            root = await db.get(Session, member.mission_id, options=SESSION_METADATA if metadata_only else ())
            assert root is not None  # Checked by execution_session.
            await self.check_source_audience(db, root, actor, metadata_only=metadata_only)
        return session

    async def execution_session(
        self, db: AsyncSession, session_id: str, actor: User, *, metadata_only: bool = False,
    ) -> Session:
        """Local session checks for work whose saved turn authority is revalidated by the caller."""
        session = await db.get(Session, session_id, options=SESSION_METADATA if metadata_only else ())
        if session is None or session.deleted:
            raise HTTPException(404, "Conversation not found.")
        if session.visibility == "private" and session.owner_id != actor.id:
            link = await db.get(DelegatedSession, session.id)
            if link is None or not await self.same_person(db, actor, session.owner_id):
                raise HTTPException(404, "Conversation not found.")
        member = await db.get(SwarmMember, session_id)
        if member is not None and member.mission_id != session_id:
            root = await db.get(Session, member.mission_id, options=SESSION_METADATA if metadata_only else ())
            if root is None or root.deleted or (root.visibility == "private" and root.owner_id != actor.id):
                raise HTTPException(404, "Conversation not found.")
        return session

    async def check_source_audience(
        self, db: AsyncSession, session: Session, actor: User, *, metadata_only: bool = False,
    ) -> None:
        delegated = await db.get(DelegatedSession, session.id)
        parent_id = delegated.parent_session_id if delegated else None
        if parent_id and parent_id != session.id:
            parent = await db.get(Session, parent_id, options=SESSION_METADATA if metadata_only else ())
            if parent is None:
                raise HTTPException(404, "Conversation not found.")
            # Inherit provider audience, not the parent's navigation state. An
            # archived/deleted web parent must not hide its owner's private child.
            await self.check_source_audience(db, parent, actor, metadata_only=metadata_only)

    @asynccontextmanager
    async def _admission_session(self, db: AsyncSession | None) -> AsyncIterator[AsyncSession]:
        if db is not None:
            if not db.in_transaction() or db.get_bind().engine is not self.engine.sync_engine:
                raise ValueError("Admission requires a transaction on this native database.")
            yield db  # The caller owns the entire receipt/admission transaction.
        else:
            async with self.sessions.begin() as transaction:
                yield transaction

    async def create_session(
        self, actor: User, body: NewSession, *, db: AsyncSession | None = None,
    ) -> Session:
        model = body.model or self.settings.default_model
        if model == AUTO_MODEL:  # Routed per message; the conversation itself starts on the default.
            model = self.settings.default_model
        if model not in self.settings.models:
            raise HTTPException(422, "Choose an enabled model.")
        async with self._admission_session(db) as transaction:
            session = Session(
                id=new_id(), owner_id=actor.id, title=body.title,
                visibility=body.visibility, model=model,
            )
            transaction.add(session)
        return session

    async def list_sessions(self, actor: User, *, archived: bool = False, deleted: bool = False) -> list[Session]:
        async with self.sessions() as db:
            query = select(Session).where(
                Session.deleted.is_(deleted),
                or_(Session.owner_id == actor.id, Session.visibility == "shared"),
            ).order_by(Session.updated_at.desc()).limit(100)
            if not deleted:
                query = query.where(
                    Session.archived.is_(archived),
                    ~select(SwarmMember.session_id).where(
                        SwarmMember.session_id == Session.id, SwarmMember.position > 0,
                    ).exists(),
                )
            visible = []
            for session in await db.scalars(query):
                try:
                    if deleted:
                        if not await self.same_person(db, actor, session.owner_id):
                            continue
                        await self.check_source_audience(db, session, actor)
                    else:
                        await self.allowed_session(db, session.id, actor)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                else:
                    visible.append(session)
            return visible

    async def session(self, actor: User, session_id: str, *, metadata_only: bool = False) -> Session:
        async with self.sessions() as db:
            return await self.allowed_session(db, session_id, actor, metadata_only=metadata_only)

    async def messages(self, actor: User, session_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        from .message_sources import public_messages

        async with self.sessions() as db:
            await self.allowed_session(db, session_id, actor, metadata_only=True)
            rows = list((await db.scalars(select(Message).where(
                Message.session_id == session_id, Message.id > after
            ).order_by(Message.id).limit(200))).all())
            pictures: dict[str, list[dict]] = {}
            if rows:
                for link, upload in (await db.execute(select(TurnImage, Upload).join(
                    Upload, Upload.id == TurnImage.upload_id
                ).where(TurnImage.turn_id.in_([row.turn_id for row in rows])).order_by(TurnImage.position))).all():
                    pictures.setdefault(link.turn_id, []).append({
                        "id": upload.id, "name": upload.name, "media_type": upload.media_type, "size": upload.size,
                    })
            authors = {user.id: user for user in await db.scalars(select(User).where(
                User.id.in_({row.author_id for row in rows if row.author_id}),
            ))}
            identities = {key: await self.same_person(db, actor, key) for key in authors}
            projected = await public_messages(self, db, rows)
            originals = {item.id: item for item in await db.scalars(select(Upload).where(
                Upload.session_id == session_id,
            ))}
            goal = await db.get(Goal, session_id)
            result = []
            for row in rows:
                content, source = projected[row.id]
                files = pictures.get(row.turn_id, [])
                if source and source.get("deleted"):
                    files = []
                if source and source.get("files"):
                    files = [{"id": item.id, "name": item.name, "media_type": item.media_type, "size": item.size}
                             for file in source["files"] if (item := originals.get(file.get("upload_id")))]
                author = authors.get(row.author_id or "")
                result.append({
                    "id": row.id, "turn_id": row.turn_id, "role": row.role,
                    "author_id": row.author_id, "author_name": author.display_name if author else None,
                    "is_self": identities.get(row.author_id or "", False), "content": content,
                    "sent_as_goal": row.role == "user" and bool(
                        (source or {}).get("sent_as_goal") or (goal and row.turn_id == goal.source_turn_id)
                    ),
                    "created_at": row.created_at,
                    **({"source": "delegation"} if row.identity.startswith("delegation:") else {}),
                    "attachments": files if row.role == "user" or row.identity.startswith("delegation:") else [],
                })
        return result

    async def same_person(self, db: AsyncSession, actor: User, author_id: str | None) -> bool:
        """Presentation/capability relationship, never a replacement execution grant."""
        return actor.id == author_id

    async def resolve_auto(self, actor: User, session_id: str, body: NewMessage) -> tuple[NewMessage, dict | None]:
        """Swap the reserved "auto" model for the router's pick, before an admission transaction opens.

        Callers fingerprint the request first, so a retry with the same request key stays the same
        request even if the router would now pick differently.
        """
        if body.model != AUTO_MODEL:
            return body, None
        if not self.settings.routing_enabled:
            raise HTTPException(422, "Smart routing is not available. Choose a model.")
        async with self.sessions() as db:
            session = await self.allowed_session(db, session_id, actor, metadata_only=True)
            active = await db.get(Turn, session.active_turn) if session.active_turn else None
            # Only images make a message a vision task; a CSV or a code file does not.
            images = (await db.scalar(select(func.count()).select_from(Upload).where(
                Upload.id.in_(body.attachments), Upload.owner_id == actor.id,
                Upload.media_type.in_(MEDIA_TYPES.values()),
            )) or 0) if body.attachments else 0
        if body.mode == "asap" and active and active.status == "running" and active.actor_id == actor.id:
            # Guidance only joins the running turn on that turn's own model.
            routing = decision(active.model, "running-turn", "This message joins the task already running on this model.")
        else:
            # last_model is the model whose saved history the next turn can reuse; None in a new conversation.
            routing = await self.router.decide(body.prompt, images=images, previous=session.last_model)
        return body.model_copy(update={"model": routing["model"]}), routing

    async def submit(
        self, actor: User, session_id: str, body: NewMessage, *, db: AsyncSession | None = None,
        swarm_generated: bool = False,
        delegated_authority: DelegatedAuthority | None = None,
        received_at: str | None = None,
    ) -> Turn:
        admitted_at = now()
        # Preserve transport receipts admitted before clarification replies
        # acquired an explicit target.
        fingerprint = hashlib.sha256(body.model_dump_json(
            exclude={"reply_to"} if body.reply_to is None else None,
        ).encode()).hexdigest()
        if db is not None:
            # Do not recover a conflicting receipt in an independent transaction.
            # The caller must roll back/retry its complete occurrence admission.
            return await self._submit(
                actor, session_id, body, fingerprint, db=db, swarm_generated=swarm_generated,
                delegated_authority=delegated_authority,
                received_at=received_at, admitted_at=admitted_at,
            )
        if swarm_generated or delegated_authority is not None:
            raise ValueError("Generated input requires its admission transaction.")
        body, routing = await self.resolve_auto(actor, session_id, body)
        try:
            return await self._submit(
                actor, session_id, body, fingerprint, received_at=received_at, admitted_at=admitted_at, routing=routing,
            )
        except IntegrityError:
            # Two requests can race the first lookup. The database's unique identity decides.
            async with self.sessions() as lookup:
                await self.allowed_session(lookup, session_id, actor)
                existing = await lookup.scalar(select(Turn).where(
                    Turn.session_id == session_id, Turn.actor_id == actor.id,
                    Turn.request_key == body.request_key,
                ))
                if existing and existing.request_fingerprint == fingerprint:
                    return existing
            raise HTTPException(409, "A conflicting request was submitted.") from None

    async def _submit(
        self, actor: User, session_id: str, body: NewMessage, fingerprint: str,
        *, db: AsyncSession | None = None, received_at: str | None = None,
        swarm_generated: bool = False, admitted_at: str | None = None,
        delegated_authority: DelegatedAuthority | None = None,
        routing: dict | None = None,
    ) -> Turn:
        async with self._admission_session(db) as transaction:
            # Account changes and admission must serialize across API/worker
            # processes. An authorization check before this transaction can
            # become stale while a scheduled or interactive request waits.
            authorized = await transaction.scalar(update(User).where(
                User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
            ).values(auth_version=User.auth_version).returning(User.id))
            if authorized is None:
                raise HTTPException(401, "Your access changed. Sign in again.")
            mission = await self.swarms.admit(transaction, actor, session_id, body, generated=swarm_generated)
            session = await (self.execution_session if swarm_generated or delegated_authority is not None
                             else self.allowed_session)(transaction, session_id, actor)
            locked = await transaction.scalar(update(Session).where(
                Session.id == session_id, Session.deleted.is_(False),
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise HTTPException(404, "Conversation not found.")
            await transaction.refresh(session)
            await settle_failed_action_attempts(transaction, session_id)
            existing = await transaction.scalar(select(Turn).where(
                Turn.session_id == session_id, Turn.actor_id == actor.id,
                Turn.request_key == body.request_key,
            ))
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(409, "This request identity already belongs to another message.")
                return existing
            model = body.model or session.model
            if model not in self.settings.models:
                raise HTTPException(422, "Choose an enabled model.")
            proposed = RunScope(session.id, "", actor.id, model, session.visibility, session.surface)
            if session.workspace and session.workspace_scope != proposed.workspace_key:
                raise HTTPException(
                    409, "This conversation's computer belongs to another requester. Start a new conversation."
                )
            active = await transaction.get(Turn, session.active_turn) if session.active_turn else None
            if body.reply_to is not None and (
                active is None or active.id != body.reply_to or active.status != "waiting"
            ):
                raise HTTPException(409, "That question is no longer waiting. Review the current conversation.")
            if active and active.status == "waiting" and (body.reply_to or body.mode == "asap"):
                from .questions import can_reply_with_text, pending_questions, text_results
                pending = pending_questions(self, active)
                eligible = (
                    not swarm_generated and active.actor_id == actor.id
                    and active.model == model and not body.attachments
                    and can_reply_with_text(pending)
                )
                if body.reply_to and not eligible:
                    raise HTTPException(409, "Answer in the question form, on the conversation's current model.")
                if eligible and pending is not None:
                    return await self._resume(
                        actor, session_id, active.id, body.request_key,
                        text_results(pending, body.prompt), fingerprint,
                        received_at=received_at, admitted_at=admitted_at, transaction=transaction, reply=body,
                    )
            turn_id = new_id()
            turn = Turn(
                id=turn_id, session_id=session_id, actor_id=actor.id,
                request_key=body.request_key, request_fingerprint=fingerprint,
                prompt=self.security.seal(body.prompt),
                model=model, mode=body.mode, workflow_id=f"modelmesh-turn-{turn_id}",
            )
            if body.mode == "asap" and session.active_turn:
                active = await transaction.get(Turn, session.active_turn)
                if (
                    active and active.status == "running" and active.actor_id == actor.id
                    and active.model == model
                ):
                    turn.parent_turn = active.id
                    turn.status = "steering"
            transaction.add(turn)
            await transaction.flush()
            if routing is not None:  # Shown in the pop-up and the answer's tag; kept for the routing dashboard.
                transaction.add(Event(session_id=session_id, turn_id=turn.id, kind="model_routed",
                                      content=json.dumps(routing), identity=f"route:{turn.id}"))
                cast(Any, turn).routing = routing
            if turn.status == "steering":
                await self._record_guidance(transaction, turn, session.revision)
            from .performance import admission
            admission(
                transaction, turn, received_at or admitted_at or turn.created_at,
                source="http" if received_at else "admission", surface=session.surface,
            )
            await self.swarms.bind(transaction, mission, turn, generated=swarm_generated)
            await self.goals.bind(transaction, turn)
            for position, image_id in enumerate(body.attachments):
                # Reserve the upload's destination atomically before publishing
                # it into a conversation, including concurrent first attachment.
                image = await transaction.scalar(update(Upload).where(
                    Upload.id == image_id, Upload.owner_id == actor.id,
                    or_(Upload.session_id.is_(None), Upload.session_id == session_id),
                ).values(session_id=session_id).returning(Upload.id))
                if image is None:
                    raise HTTPException(404, "A file is unavailable in this conversation.")
                transaction.add(TurnImage(turn_id=turn.id, upload_id=image_id, position=position))
            transaction.add(Message(
                session_id=session_id, turn_id=turn_id, author_id=None if swarm_generated else actor.id,
                role="system" if swarm_generated else "user", content=self.security.seal(body.prompt),
                identity=f"{'swarm' if swarm_generated else 'user'}:{turn_id}",
            ))
            from .message_sources import record_source
            await record_source(self, transaction, f"{'swarm' if swarm_generated else 'user'}:{turn_id}")
            session.updated_at = now()
            if session.title == "New conversation":
                from .skill_mentions import display_text
                authored = display_text(body.prompt).strip()
                session.title = authored.splitlines()[0][:100] if authored else "Attachment conversation"
            session.model = model
        return turn

    async def execution_context(self, db: AsyncSession, session: Session) -> ExecutionContext:
        return await session_context(self, db, session)

    async def check_scope_context(self, db: AsyncSession, scope: RunScope, session: Session) -> None:
        """Recheck the persisted execution policy, never private-child presentation."""
        context = await self.execution_context(db, session)
        if scope.execution_context is not None and scope.execution_context != context:
            raise PermissionError("This execution's original policy context changed.")
        if context != ExecutionContext(session.visibility, session.surface):
            # Older activity payloads lack this field. Recover their immutable
            # context before capability catalogs release content.
            scope.execution_context = context

    async def claim_next(self) -> Turn | None:
        claimed = await self.claim_batch(1)
        return claimed[0] if claimed else None

    async def claim_batch(self, limit: int) -> list[Turn]:
        """Reserve at most one turn per session; RPCs happen after this commit."""
        if limit < 1:
            raise ValueError("Dispatch batch size must be positive.")
        claimed_turns: list[Turn] = []
        claimed_sessions: set[str] = set()
        async with self.sessions.begin() as db:
            while len(claimed_turns) < limit:
                before = len(claimed_turns)
                candidates = (await db.scalars(select(Turn).join(
                    Session, Session.id == Turn.session_id
                ).where(
                    Turn.status == "queued",
                    or_(Session.active_turn.is_(None), Session.active_turn == Turn.id),
                    Session.deleted.is_(False),
                    dispatchable(),
                    goal_dispatchable(),
                ).order_by(Turn.created_at, Turn.id).limit(limit - len(claimed_turns)))).all()
                if not candidates:
                    break
                for turn in candidates:
                    if turn.session_id in claimed_sessions:
                        continue
                    claimed = await db.scalar(update(Session).where(
                        Session.id == turn.session_id,
                        or_(Session.active_turn.is_(None), Session.active_turn == turn.id),
                        Session.deleted.is_(False),
                    ).values(revision=Session.revision + 1).returning(Session.id))
                    if claimed is None:
                        continue
                    admitted = await db.scalar(update(Turn).where(
                        Turn.id == turn.id, Turn.status == "queued", goal_dispatchable(),
                    ).values(status="starting").returning(Turn.id))
                    if admitted is not None:
                        await db.execute(update(Session).where(Session.id == turn.session_id).values(
                            active_turn=turn.id, status="queued", updated_at=now(),
                        ))
                        turn.status = "starting"
                        claimed_turns.append(turn)
                        claimed_sessions.add(turn.session_id)
                if len(claimed_turns) == before:
                    break
        return claimed_turns

    async def steer_queued(self, actor: User, turn_id: str) -> Turn:
        """Promote saved input into the SDK's existing durable enqueue path."""
        async with self._admission_session(None) as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.actor_id != actor.id:
                raise HTTPException(404, "Message not found.")
            await db.execute(update(Session).where(
                Session.id == turn.session_id,
            ).values(revision=Session.revision + 1))
            session = await self.allowed_session(db, turn.session_id, actor)
            await self.swarms.authorize(db, turn)
            await db.refresh(turn)
            if turn.status in {"steering", "signal_sent", "accepted"}:
                return turn
            if turn.status != "queued":
                raise HTTPException(409, "This message has already started.")
            turn.mode = "asap"
            active = await db.get(Turn, session.active_turn) if session.active_turn else None
            if active:
                if active.status != "running" or active.actor_id != actor.id or active.model != turn.model:
                    raise HTTPException(409, "This message needs its own run. It will start after the active task.")
                turn.parent_turn = active.id
                turn.status = "steering"
                await self._record_guidance(db, turn, session.revision)
            return turn

    async def _record_guidance(self, db: AsyncSession, turn: Turn, revision: int) -> None:
        # Event IDs order admissions while holding the session fence, including
        # "Send now" for an older queued message. Message creation time cannot
        # order those corrections. A repeated promotion can have a new receipt.
        db.add(Event(
            session_id=turn.session_id, turn_id=turn.id, kind="steering_admitted",
            identity=f"guidance:{turn.id}:{revision}",
            content=self.security.seal({"parent_turn": turn.parent_turn}),
        ))
        await self.goals.invalidate_for_guidance(db, turn)

    async def dispatch_boundary(self) -> tuple[str, str] | None:
        async with self.sessions() as db:
            row = (await db.execute(select(Turn.created_at, Turn.id).where(
                Turn.status.in_(["queued", "starting"]),
            ).order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1))).first()
            return (row[0], row[1]) if row is not None else None

    async def starting_turns(
        self, *, limit: int = 100, after: tuple[str, str] | None = None,
        through: tuple[str, str] | None = None,
    ) -> list[Turn]:
        async with self.sessions() as db:
            query = select(Turn).where(Turn.status == "starting")
            if after is not None:
                stamp, identity = after
                query = query.where(or_(
                    Turn.created_at > stamp, and_(Turn.created_at == stamp, Turn.id > identity),
                ))
            if through is not None:
                stamp, identity = through
                query = query.where(or_(
                    Turn.created_at < stamp, and_(Turn.created_at == stamp, Turn.id <= identity),
                ))
            return list((await db.scalars(query.order_by(Turn.created_at, Turn.id).limit(limit))).all())

    async def unsettled_turns(self) -> list[Turn]:
        async with self.sessions() as db:
            return list((await db.scalars(select(Turn).where(
                Turn.status.in_(["starting", "running", "saving"])
            ).order_by(Turn.created_at).limit(100))).all())

    async def prepare(self, turn_id: str, *, picked_up_at: str | None = None) -> PreparedTurn:
        picked_up_at = picked_up_at or now()
        async with self.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None:
                raise ValueError("Unknown turn.")
            actor = await db.get(User, turn.actor_id)
            if actor is None or not actor.enabled:
                raise PermissionError("The requester no longer has access.")
            session = await self.execution_session(db, turn.session_id, actor)
            member = await self.swarms.authorize(db, turn)
            delegation = await db.get(DelegatedSession, session.id)
            generated_delegation = await db.get(DelegatedTurn, turn.id) if delegation else None
            locked = await db.scalar(update(Session).where(
                Session.id == turn.session_id, Session.active_turn == turn.id,
                Session.deleted.is_(False), Session.status.in_(["queued", "running"]),
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise PermissionError("This turn is no longer authorized to execute.")
            await db.refresh(turn)
            await db.refresh(session)
            if session.active_turn != turn.id or turn.status not in ("starting", "running"):
                raise PermissionError("This turn is no longer authorized to execute.")
            context = await self.execution_context(db, session)
            scope = RunScope(
                session.id, turn.id, actor.id, turn.model, session.visibility, session.surface,
                swarm_id=member.mission_id if member else None,
                swarm_name=member.name if member else None,
                delegation_depth=delegation.depth if delegation else 0,
                delegation_generated=generated_delegation is not None,
                execution_context=context if delegation else None,
            )
            goal = await self.goals.authorize(db, turn)
            if goal is not None:
                scope.goal_generation = goal.generation
            if session.workspace and session.workspace_scope != scope.workspace_key:
                raise PermissionError(
                    "This conversation's computer belongs to another requester. Start a new conversation."
                )
            if turn.preparation:
                prepared = TypeAdapter(PreparedTurn).validate_json(self.security.unseal(turn.preparation))
                if delegation:
                    prepared.scope.execution_context = context
                return prepared
            if turn.deferred_results:
                from .delegation import resumed_agents
                scope.background_agents = list(await resumed_agents(self, db, scope))
                scope.background_delegation = bool(scope.background_agents)
            conversation = None
            if (
                session.last_actor_id == actor.id and session.last_model == turn.model
                and session.last_grants == NO_GRANTS_KEY
                and session.conversation
            ):
                conversation = self.security.unseal(session.conversation).decode()
            elif session.conversation:
                from .message_sources import public_messages

                public = list(await db.scalars(select(Message).where(
                    Message.session_id == session.id, Message.turn_id != turn.id,
                    Message.created_at < turn.created_at,
                ).order_by(Message.id)))
                projected = await public_messages(self, db, public)
                history = []
                for message in public:
                    content, _ = projected[message.id]
                    if message.role == "assistant":
                        history.append(ModelResponse(parts=[TextPart(content=content)]))
                    else:
                        history.append(ModelRequest(parts=[UserPromptPart(
                            content=f"Earlier public message by {message.author_id}:\n{content}"
                        )], metadata={"modelmesh_public_turn": message.turn_id}))
                conversation = ConversationTypeAdapter.dump_json(
                    Conversation(messages=history, conversation_id=scope.conversation_id)
                ).decode()
            turn.status = "running"
            turn.accepted_at = turn.accepted_at or now()
            session.status = "running"
            prepared = PreparedTurn(
                scope=scope,
                prompt=self.security.unseal(turn.prompt).decode(),
                conversation=conversation,
                workspace=json.loads(session.workspace) if session.workspace else None,
                deferred_results=json.loads(self.security.unseal(turn.deferred_results))
                if turn.deferred_results else None,
            )
            turn.preparation = self.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
            from .performance import evidence
            db.add(Event(**evidence(session.id, turn.id, "worker", picked_up_at)))
            return prepared

    async def authorize_scope(
        self, scope: RunScope, *, input_id: str | None = None, db: AsyncSession | None = None,
    ) -> None:
        """Recheck live authority."""
        if db is None:
            async with self.sessions() as transaction:
                await self.authorize_scope(scope, input_id=input_id, db=transaction)
        else:
            actor = await db.get(User, scope.actor_id)
            turn = await db.get(Turn, scope.turn_id)
            if actor is None or not actor.enabled or turn is None:
                raise PermissionError("This execution no longer has an authorized requester.")
            await self.swarms.authorize(db, turn, scope=scope)
            await self.goals.authorize(db, turn)
            session = await self.execution_session(db, scope.session_id, actor)
            if (
                session.active_turn != turn.id or session.status != "running" or turn.status != "running"
                or turn.actor_id != actor.id or turn.session_id != session.id
            ):
                raise PermissionError("This execution has been stopped or superseded.")
            await self.check_scope_context(db, scope, session)
            if input_id is not None:
                pending = await self.latest_guidance(db, turn.id)
                if pending and pending != input_id:
                    raise GuidanceChanged("New guidance arrived before this action was dispatched.")

    @staticmethod
    def guidance_order():
        return select(func.max(Event.id)).where(
            Event.turn_id == Turn.id, Event.kind == "steering_admitted",
        ).scalar_subquery()

    async def latest_guidance(self, db: AsyncSession, parent_turn_id: str) -> str | None:
        """One admission-order rule for every effect and memory mutation fence."""
        return await db.scalar(select(Turn.id).where(
            Turn.parent_turn == parent_turn_id,
            Turn.status.in_(["steering", "signal_sent", "accepted"]),
        ).order_by(
            self.guidance_order().desc().nulls_last(), Turn.created_at.desc(), Turn.id.desc(),
        ).limit(1))

    async def save_workspace_reference(self, scope: RunScope, reference: dict[str, Any]) -> None:
        await self.authorize_scope(scope)
        async with self.sessions.begin() as db:
            await db.execute(update(Session).where(
                Session.id == scope.session_id, Session.active_turn == scope.turn_id,
                Session.status == "running",
            ).values(workspace=json.dumps(reference), workspace_scope=scope.workspace_key))

    async def save_result(
        self, scope: RunScope, *, conversation: str, output: str,
        workspace: dict[str, Any] | None, usage: dict[str, Any],
        deferred: bool = False,
    ) -> bool:
        return await self.answers.save_result(
            scope, conversation=conversation, output=output, workspace=workspace,
            usage=usage, deferred=deferred,
        )

    async def input_images(self, turn_id: str, *, session_id: str | None = None) -> list[InputImage]:
        async with self.sessions() as db:
            query = select(Upload).join(TurnImage, TurnImage.upload_id == Upload.id).where(
                TurnImage.turn_id == turn_id
            ).order_by(TurnImage.position)
            if session_id is not None:
                query = query.where(Upload.session_id == session_id)
            return [
                InputImage(row.digest, row.media_type, row.name, upload_id=row.id, size=row.size)
                for row in (await db.scalars(query)).all()
            ]

    async def settle(self, turn_id: str, *, warning: str | None = None) -> None:
        await self.answers.settle(turn_id, warning=warning)

    async def fail(
        self, turn_id: str, message: str, *, only_unsettled: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        await self.answers.fail(turn_id, message, only_unsettled=only_unsettled, diagnostics=diagnostics)

    async def stop(self, actor: User, session_id: str) -> str | None:
        if await self.swarms.stop_session(actor, session_id):
            return None  # Durable native Stop dispatch reconciles every member.
        async with self._admission_session(None) as db:
            session = await self.allowed_session(db, session_id, actor)
            if not await self.same_person(db, actor, session.owner_id) and actor.role != "admin":
                raise HTTPException(403, "Only the conversation owner or an administrator can stop it.")
            workflow_id = None
            if session.active_turn:
                turn = await db.get(Turn, session.active_turn)
                if turn:
                    workflow_id = turn.workflow_id
            await db.execute(update(Session).where(
                Session.id == session_id
            ).values(status="stopped"))
            goal = await db.get(Goal, session_id, populate_existing=True)
            if goal is not None and goal.status == "active":
                self.goals.change(goal, "paused", "Paused by Stop.")
            await db.execute(update(Turn).where(
                Turn.session_id == session_id,
                Turn.status.in_(["queued", "starting", "running", "waiting", "steering",
                                 "signal_sent", "accepted"]),
            ).values(status="stopped", finished_at=now()))
            # Keep ownership until execution/cleanup acknowledges Stop; don't admit competing work.
            return workflow_id

    async def delete_session(self, actor: User, session_id: str) -> None:
        """Hide a session and fence work atomically; the existing Stop dispatcher cancels execution."""
        async with self._admission_session(None) as db:
            member = await self.swarms.member(db, session_id)
            # Keep the mission-before-session lock order and commit the deletion
            # together with its team-wide attention fence.
            mission = await self.swarms.lock(db, member.mission_id, actor) if member else None
            owned = await db.get(Session, session_id)
            if owned is None or not await self.same_person(db, actor, owned.owner_id):
                raise HTTPException(404, "Conversation not found.")
            await self.check_source_audience(db, owned, actor)
            locked = await db.scalar(update(Session).where(
                Session.id == session_id, Session.owner_id == owned.owner_id,
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise HTTPException(404, "Conversation not found.")
            session = await db.get(Session, session_id)
            assert session is not None
            if session.deleted:
                return
            if mission is not None:
                await self.swarms.stop_in(
                    db, mission, "attention", "A team conversation was deleted. Restore it before resuming.",
                )
            session.deleted = True
            goal = await db.get(Goal, session_id, populate_existing=True)
            if goal is not None and goal.status == "active":
                self.goals.change(goal, "paused", "The conversation was deleted.")
            session.updated_at = now()
            # An answer already being saved retains its confirmed outcome.
            if session.status != "saving":
                session.status = "stopped"
            await db.execute(update(Turn).where(
                Turn.session_id == session_id,
                Turn.status.in_(["queued", "starting", "running", "waiting", "steering", "signal_sent", "accepted"]),
            ).values(status="stopped", finished_at=now()))

    async def restore_session(self, actor: User, session_id: str) -> None:
        async with self.sessions.begin() as db:
            owned = await db.get(Session, session_id)
            if owned is None or not await self.same_person(db, actor, owned.owner_id):
                raise HTTPException(404, "Conversation not found.")
            await self.check_source_audience(db, owned, actor)
            locked = await db.scalar(update(Session).where(
                Session.id == session_id, Session.owner_id == owned.owner_id,
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise HTTPException(404, "Conversation not found.")
            session = await db.get(Session, session_id)
            assert session is not None
            if not session.deleted:
                return
            if session.active_turn:
                raise HTTPException(409, "Wait for the task to finish stopping, then restore.")
            session.deleted = False
            session.archived = False
            session.updated_at = now()

    async def stopped(self, turn_id: str, conversation: str | None = None) -> None:
        await self.answers.stopped(turn_id, conversation)

    async def pending_steering(self) -> list[tuple[Turn, Turn]]:
        async with self.sessions() as db:
            cutoff = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
            children = await db.scalars(select(Turn).where(or_(
                Turn.status == "steering",
                and_(Turn.status == "signal_sent", or_(
                    Turn.accepted_at.is_(None), Turn.accepted_at < cutoff,
                )),
            )).order_by(
                self.guidance_order().asc().nulls_first(), Turn.created_at, Turn.id,
            ).limit(100))
            result = []
            for child in children:
                parent = await db.get(Turn, child.parent_turn)
                if parent:
                    result.append((child, parent))
            return result

    async def pending_stops(self) -> list[Turn]:
        async with self.sessions() as db:
            return list((await db.scalars(select(Turn).join(
                Session, Session.active_turn == Turn.id
            ).where(Turn.status == "stopped").limit(100))).all())

    async def signal_attempted(self, turn_id: str) -> None:
        async with self.sessions.begin() as db:
            await self._fence_steering(db, turn_id)
            await db.execute(update(Turn).where(
                Turn.id == turn_id, Turn.status.in_(["steering", "signal_sent"])
            ).values(status="signal_sent", accepted_at=now()))

    async def steering_uncertain(self, turn_id: str) -> None:
        async with self.sessions.begin() as db:
            await self._fence_steering(db, turn_id)
            await db.execute(update(Turn).where(
                Turn.id == turn_id, Turn.status == "signal_sent"
            ).values(
                status="unconfirmed",
                error="Delivery of this correction was not confirmed before the task ended. "
                      "Inspect its saved activity before submitting the work again.",
            ))

    async def prepare_steering(self, parent_id: str, turn_id: str) -> SteeringInput:
        async with self.sessions() as db:
            turn = await db.get(Turn, turn_id)
            parent = await db.get(Turn, parent_id)
            if (
                turn is None or parent is None or turn.parent_turn != parent.id
                or turn.actor_id != parent.actor_id or turn.model != parent.model
                or turn.status not in ("steering", "signal_sent", "accepted")
            ):
                raise PermissionError("The correction is no longer eligible for this run.")
            return SteeringInput(turn.id, self.security.unseal(turn.prompt).decode(), "asap")

    async def steering_receipt(self, turn_id: str, enqueue_id: str | None) -> None:
        async with self._admission_session(None) as db:
            turn = await self._fence_steering(db, turn_id)
            if turn is None or turn.status not in ("steering", "signal_sent", "accepted"):
                return
            if enqueue_id is None:
                turn.status = "queued"
                turn.parent_turn = None
                turn.accepted_at = None
            else:
                turn.status = "accepted"
                turn.enqueue_id = enqueue_id
                turn.accepted_at = now()

    async def _fence_steering(self, db: AsyncSession, turn_id: str) -> Turn | None:
        """Session before Turn, matching admission, promotion, Stop and settlement."""
        turn = await db.get(Turn, turn_id)
        if turn is not None:
            await db.execute(update(Session).where(Session.id == turn.session_id).values(
                revision=Session.revision + 1,
            ))
            await db.refresh(turn)
        return turn

    async def resume(
        self, actor: User, session_id: str, previous_id: str,
        request_key: str, results: dict[str, Any], *, received_at: str | None = None,
    ) -> Turn:
        admitted_at = now()
        fingerprint = hashlib.sha256(json.dumps(
            {"previous": previous_id, "results": results}, sort_keys=True
        ).encode()).hexdigest()
        try:
            return await self._resume(
                actor, session_id, previous_id, request_key, results, fingerprint,
                received_at=received_at, admitted_at=admitted_at,
            )
        except (IntegrityError, HTTPException) as error:
            if isinstance(error, HTTPException) and error.status_code != 409:
                raise
            async with self.sessions() as db:
                await self.allowed_session(db, session_id, actor)
                existing = await db.scalar(select(Turn).where(
                    Turn.session_id == session_id, Turn.actor_id == actor.id, Turn.request_key == request_key
                ))
                if existing and existing.request_fingerprint == fingerprint:
                    return existing
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(409, "This answer conflicts with an already admitted request.") from error

    async def _resume(
        self, actor: User, session_id: str, previous_id: str,
        request_key: str, results: dict[str, Any], fingerprint: str,
        *, received_at: str | None = None, admitted_at: str | None = None,
        transaction: AsyncSession | None = None, reply: NewMessage | None = None,
    ) -> Turn:
        async with self._admission_session(transaction) as db:
            locked_actor = await db.scalar(update(User).where(
                User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
            ).values(auth_version=User.auth_version).returning(User.id))
            if locked_actor is None:
                raise HTTPException(401, "Your access changed. Sign in again.")
            session = await self.allowed_session(db, session_id, actor)
            existing = await db.scalar(select(Turn).where(
                Turn.session_id == session_id, Turn.actor_id == actor.id, Turn.request_key == request_key
            ))
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(409, "This request identity belongs to a different answer.")
                return existing
            previous = await db.get(Turn, previous_id)
            if (
                previous is None or previous.actor_id != actor.id or previous.session_id != session.id
                or previous.status != "waiting" or session.active_turn != previous.id
                or previous.preparation is None
            ):
                raise HTTPException(409, "The pending request changed.")
            mission = await self.swarms.admit(db, actor, session_id, NewMessage(
                prompt="Answer the existing pending request.", request_key=request_key, model=previous.model,
            ))
            try:
                member = await self.swarms.authorize(db, previous)
            except PermissionError as error:
                raise HTTPException(409, str(error)) from error
            original = TypeAdapter(PreparedTurn).validate_json(self.security.unseal(previous.preparation))
            await self.check_scope_context(db, original.scope, session)
            turn_id = new_id()
            turn = Turn(
                id=turn_id, session_id=session.id, actor_id=actor.id,
                request_key=request_key, request_fingerprint=fingerprint,
                prompt=self.security.seal(""), model=previous.model,
                workflow_id=f"modelmesh-turn-{turn_id}", deferred_results=self.security.seal(results),
            )
            claimed = await db.scalar(update(Session).where(
                Session.id == session.id, Session.active_turn == previous.id, Session.status == "waiting",
            ).values(active_turn=turn_id, status="queued", updated_at=now()).returning(Session.id))
            if claimed is None:
                raise HTTPException(409, "Another response already resumed this request.")
            db.add(turn)
            await db.flush()
            from .performance import admission
            admission(
                db, turn, received_at or admitted_at or turn.created_at,
                source="http" if received_at else "admission", surface=session.surface,
            )
            if reply is not None:
                db.add(Message(
                    session_id=session.id, turn_id=turn.id, author_id=actor.id,
                    role="user", content=self.security.seal(reply.prompt), identity=f"user:{turn.id}",
                ))
                from .message_sources import record_source
                await record_source(self, db, f"user:{turn.id}")
            if member is not None:
                await self.swarms.bind(db, mission, turn, generated=True)
            await self.goals.bind(db, turn, resumed_from=previous.id)
            delegated = await db.get(DelegatedTurn, previous.id)
            if delegated is not None:
                db.add(DelegatedTurn(turn_id=turn.id, session_id=delegated.session_id))
            from .agent_messages import resume_mail
            completed_agents = await resume_mail(db, self, previous, turn)
            if original.scope.background_delegation:
                from .delegation import bind_background_resume
                original.scope.background_agents = [
                    child for child in original.scope.background_agents if child not in completed_agents
                ]
                await bind_background_resume(self, db, previous, turn, original.scope)
            previous.status = "resumed"
            return turn
