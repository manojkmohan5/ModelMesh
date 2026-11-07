"""ModelMesh's authenticated API around native conversations and workflows."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, TypeAdapter
from pydantic_ai import ConversationTypeAdapter, DeferredToolResults
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai_harness.ask_user import AskUserRequest, AskUserResponse, ask_user_result
from sqlalchemy import func, select, update
from sqlalchemy.orm import load_only
from temporalio.service import RPCError

from .api_views import (
    AuthConfig,
    EffectResolveResult,
    GoalCommandResult,
    GoalStartedResult,
    HealthStatus,
    InvocableSkillPage,
    LiveEvent,
    LoginResult,
    LogoutResult,
    Me,
    MeUser,
    PendingEffect,
    PlanStep,
    QueuedTurnCancelResult,
    QueuedTurnUpdateResult,
    SavedActivity,
    SessionDeleteResult,
    SessionDetail,
    SessionFile,
    SessionListItem,
    SessionMessage,
    SessionRestoreResult,
    SessionStopResult,
    SessionSummary,
    SessionUpdateResult,
    TurnFailureDetails,
    TurnView,
    TurnWorkProgress,
    WorkActionDetail,
)
from .contracts import ApprovalReply, EffectResolution, NewMessage, NewSession, QueuedEdit, SessionEdit
from .debug_trace import debug_trace
from .failed_action_attempts import failed_action_attempt
from .failure_diagnostics import failure_details
from .file_preview import routes as file_preview_routes
from .general_preferences import GeneralPreferences, general_preferences
from .history_handoff import is_turn_run
from .images import routes as image_routes
from .live_events import PUBLIC_TOPIC
from .live_stream import authorized_events
from .model_router import AUTO_MODEL
from .models import (
    Artifact,
    Effect,
    Event,
    Message,
    Session,
    SessionPreference,
    SessionRead,
    Turn,
    User,
    now,
)
from .presentation import public_event, redact
from .public_path import PublicPathMiddleware, app_path, base_path, cookie_name, origin
from .runtime import Runtime
from .security import check_password
from .settings import Settings
from .settings_api import routes as settings_routes
from .sidebar import folders_for, lock_sidebar
from .sidebar import routes as sidebar_routes
from .worker import dispatcher, reconcile_workflows
from .workspace import routes as workspace_routes

COOKIE = "modelmesh_session"
STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


class Login(BaseModel):
    username: str = Field(default="", max_length=200)
    password: SecretStr = Field(max_length=4096)


SAVED_ACTIVITY = TypeAdapter(list[SavedActivity])
SESSION_MESSAGES = TypeAdapter(list[SessionMessage])


class EventStream(StreamingResponse):
    media_type = "text/event-stream"


def answer_actor(user: User, turn: Turn) -> User:
    """Only the original requester answers a task's questions and approvals."""
    if user.id != turn.actor_id:
        raise HTTPException(403, "Only the original requester may answer this request.")
    return user


def session_view(session: Session) -> dict[str, Any]:
    return {
        "id": session.id, "title": session.title, "owner_id": session.owner_id,
        "visibility": session.visibility, "surface": session.surface,
        "model": session.model, "status": session.status,
        "active_turn": session.active_turn, "archived": session.archived, "deleted": session.deleted,
        "workspace": json.loads(session.workspace) if session.workspace else None,
        "save_warning": session.save_warning, "updated_at": session.updated_at,
    }


def turn_view(turn: Turn) -> dict[str, Any]:
    return {
        "id": turn.id, "session_id": turn.session_id, "actor_id": turn.actor_id,
        "model": turn.model, "status": turn.status, "mode": turn.mode,
        "parent_turn": turn.parent_turn, "enqueue_id": turn.enqueue_id,
        "accepted_at": turn.accepted_at, "usage": json.loads(turn.usage),
        "error": turn.error, "created_at": turn.created_at, "finished_at": turn.finished_at,
        "routing": getattr(turn, "routing", None),
    }


def create_app(runtime: Runtime | None = None, *, run_dispatcher: bool = True) -> FastAPI:
    owned = runtime is None
    runtime = runtime or Runtime(Settings())
    session_cookie = cookie_name(COOKIE, runtime.settings.public_url)
    work_progress = runtime.work_progress

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if owned:
            await runtime.start()
        tasks = [
            asyncio.create_task(dispatcher(runtime)),
            asyncio.create_task(reconcile_workflows(runtime)),
        ] if run_dispatcher else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            work_progress.clear()
            if owned:
                await runtime.close()

    app = FastAPI(title="ModelMesh", lifespan=lifespan)
    app.state.runtime = runtime

    @app.exception_handler(RequestValidationError)
    async def safe_validation(_request: Request, error: RequestValidationError):
        # Credentials and uploaded content must not be reflected into error
        # responses consumed by clients, proxies, or diagnostic tooling.
        return JSONResponse(status_code=422, content={"detail": [
            {key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()
        ]})

    async def actor(request: Request) -> User:
        token = request.cookies.get(session_cookie, "")
        identity = runtime.security.read_session(token)
        if not identity:
            raise HTTPException(401, "Sign in to continue.")
        user = await runtime.database.user(identity["user"])
        if user.auth_version != identity["version"]:
            raise HTTPException(401, "Your session expired.")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("x-modelmesh-csrf", "")
            if not hmac.compare_digest(csrf, identity["csrf"]):
                raise HTTPException(403, "Refresh the page before making changes.")
        return user

    app.include_router(image_routes(runtime, actor))
    app.include_router(sidebar_routes(runtime, actor))
    app.include_router(workspace_routes(runtime, actor))
    app.include_router(file_preview_routes(runtime, actor))
    from .history_routes import routes as history_routes
    app.include_router(history_routes(runtime, actor))
    app.include_router(settings_routes(runtime, actor))
    from .swarm_routes import routes as swarm_routes
    app.include_router(swarm_routes(runtime, actor))

    @app.get("/api/skills", operation_id="listInvocableSkills")
    async def invocable_skills(
        shared: bool = False, offset: int = Query(0, ge=0, le=10000),
        limit: int = Query(100, ge=1, le=100), user: User = Depends(actor), session_id: str | None = None,
    ) -> InvocableSkillPage:
        if session_id is not None:
            async with runtime.database.sessions() as db:
                session = await runtime.database.allowed_session(db, session_id, user)
                context = await runtime.database.execution_context(db, session)
                if context.surface != "web":
                    return InvocableSkillPage(skills=[], has_more=False, next_offset=None)
                shared = context.visibility != "private"
        return InvocableSkillPage.model_validate(
            await runtime.skill_invocations.listing(user, shared=shared, offset=offset, limit=limit)
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if not hasattr(request.state, "modelmesh_received_at"):
            request.state.modelmesh_received_at = now()
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            request_origin = request.headers.get("origin")
            expected = origin(runtime.settings.public_url or str(request.base_url))
            if request_origin and request_origin.rstrip("/") != expected:
                return Response("Origin is not allowed.", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://api.fontshare.com; "
            "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "font-src 'self' https://cdn.fontshare.com; "
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        )
        # Only content-addressed build assets are public/cacheable. HTML,
        # authenticated API data, unversioned files and errors remain no-store.
        immutable = response.status_code in {200, 304} and re.fullmatch(
            r"/assets/[^/]+-[A-Za-z0-9_-]{8,}\.(?:js|css|woff2?|png|svg|jpe?g|webp|ico)", request.url.path,
        )
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-store"
        return response

    @app.get("/health", operation_id="getHealth")
    async def health() -> HealthStatus:
        async with runtime.database.sessions() as db:
            await db.execute(select(1))
        return HealthStatus(status="ok", engine="pydantic-ai", durability="temporal")

    @app.post("/api/login", operation_id="loginWithPassword")
    async def login(body: Login, request: Request, response: Response) -> LoginResult:
        await runtime.database.limit_login(
            request.client.host if request.client else "unknown", body.username
        )
        async with runtime.database.sessions() as db:
            user = await db.scalar(select(User).where(User.username == body.username))
        valid = (
            user is not None and user.enabled
            and await asyncio.to_thread(check_password, body.password.get_secret_value(), user.password)
        )
        if not valid or user is None:
            raise HTTPException(401, "Username or password is incorrect.")
        token, csrf = runtime.security.issue_session(user.id, user.auth_version)
        response.set_cookie(
            session_cookie, token, httponly=True, samesite="strict",
            secure=runtime.settings.secure_cookies, max_age=7 * 86400,
            path=base_path(runtime.settings.public_url) or "/",
        )
        return LoginResult(csrf=csrf)

    @app.post("/api/logout", operation_id="logout")
    async def logout(response: Response, user: User = Depends(actor)) -> LogoutResult:
        response.delete_cookie(session_cookie, path=base_path(runtime.settings.public_url) or "/")
        return LogoutResult(signed_out=True)

    @app.get("/api/auth/config", operation_id="getAuthConfig")
    async def auth_config() -> AuthConfig:
        return AuthConfig(password_enabled=True)

    @app.get("/api/me", operation_id="getMe")
    async def me(request: Request, user: User = Depends(actor)) -> Me:
        identity = runtime.security.read_session(request.cookies[session_cookie])
        if identity is None:
            raise HTTPException(401, "Your session expired.")
        return Me(
            user=MeUser(id=user.id, name=user.display_name, role=user.role),
            csrf=identity["csrf"], models=runtime.settings.models,
            default_model=runtime.settings.default_model,
            auto_model=AUTO_MODEL if runtime.settings.routing_enabled else None,
            preferences=GeneralPreferences.model_validate(general_preferences(user.preferences)),
        )

    @app.get("/api/sessions", operation_id="listSessions")
    async def sessions(
        archived: bool = False, deleted: bool = False, user: User = Depends(actor),
    ) -> list[SessionListItem]:
        rows = await runtime.database.list_sessions(user, archived=archived, deleted=deleted)
        async with runtime.database.sessions() as db:
            ids = [row.id for row in rows]
            preferences = {
                item.session_id: item for item in await db.scalars(
                    select(SessionPreference).where(SessionPreference.user_id == user.id, SessionPreference.session_id.in_(ids))
                )
            }
            participated = set(await db.scalars(select(Turn.session_id).where(
                Turn.actor_id == user.id, Turn.session_id.in_(ids),
            ).distinct()))
            latest = dict((await db.execute(select(Message.session_id, func.max(Message.id)).where(
                Message.session_id.in_(ids),
            ).group_by(Message.session_id))).all())
            reads = {item.session_id: item for item in await db.scalars(select(SessionRead).where(
                SessionRead.user_id == user.id, SessionRead.session_id.in_(ids),
            ))}
            owners = {row.id: await runtime.database.same_person(db, user, row.owner_id) for row in rows}
        return [SessionListItem.model_validate({
            **session_view(row), "can_manage": owners[row.id],
            "pinned": preferences[row.id].pinned if row.id in preferences else False,
            "folder": preferences[row.id].folder if row.id in preferences else "",
            "participated": row.owner_id != user.id and row.id in participated,
            "unread": bool(reads[row.id].unread or latest.get(row.id, 0) > reads[row.id].message_id)
            if row.id in reads else False,
        }) for row in rows]

    @app.post("/api/sessions", operation_id="createSession")
    async def create_session(body: NewSession, user: User = Depends(actor)) -> SessionSummary:
        return SessionSummary.model_validate(session_view(await runtime.database.create_session(user, body)))

    @app.get("/api/sessions/{session_id}", operation_id="getSession")
    async def session(
        session_id: str, include_activity: bool = True, include_messages: bool = False,
        user: User = Depends(actor),
    ) -> SessionDetail:
        async with runtime.database.sessions() as db:
            row = await runtime.database.allowed_session(db, session_id, user, metadata_only=True)
            # Only a waiting conversation needs its private checkpoint to render
            # pending questions. Ordinary reads must not transfer megabytes of it.
            if row.status == "waiting":
                # Keep the state and active turn aligned with this checkpoint if
                # execution advanced while the source audience was checked.
                await db.refresh(row, ["status", "active_turn", "conversation"])
        turn_columns = load_only(
            Turn.id, Turn.session_id, Turn.actor_id, Turn.model, Turn.status, Turn.mode,
            Turn.parent_turn, Turn.enqueue_id, Turn.accepted_at, Turn.usage,
            Turn.error, Turn.created_at, Turn.finished_at,
        )
        async with runtime.database.sessions() as db:
            turns = list((await db.scalars(select(Turn).where(
                Turn.session_id == session_id
            ).options(turn_columns).order_by(Turn.created_at.desc()).limit(100))).all())
            if row.active_turn and not any(turn.id == row.active_turn for turn in turns):
                active_turn = await db.get(Turn, row.active_turn, options=[turn_columns])
                if active_turn is not None:
                    turns.append(active_turn)
            guidance = dict((await db.execute(select(Event.turn_id, Event.created_at).where(
                Event.turn_id.in_([turn.id for turn in turns]), Event.kind == "steering_admitted",
            ).order_by(Event.id))).all())
            for turn_id, content in (await db.execute(select(Event.turn_id, Event.content).where(
                Event.turn_id.in_([turn.id for turn in turns]), Event.kind == "model_routed",
            ))).all():
                routed = next(turn for turn in turns if turn.id == turn_id)
                cast(Any, routed).routing = json.loads(content)
            # Resolve each relationship once for this projection/transaction.
            owners = {owner: await runtime.database.same_person(db, user, owner)
                      for owner in {row.owner_id, *(turn.actor_id for turn in turns)}}
            result: dict[str, Any] = {
                "session": {**session_view(row), "can_manage": owners[row.owner_id]}, "turns": [
                    {**turn_view(turn), "guidance_at": guidance.get(turn.id),
                     "is_self": owners[turn.actor_id]} for turn in turns
                ],
            }
            if include_messages:
                from .models import MessageSource
                version = (await db.execute(select(
                    func.coalesce(func.max(Message.id), 0), func.count(MessageSource.message_identity),
                    func.coalesce(func.sum(MessageSource.revision), 0),
                ).outerjoin(MessageSource, MessageSource.message_identity == Message.identity).where(
                    Message.session_id == session_id,
                ))).one()
                authors = (await db.execute(select(User.id, User.display_name, User.auth_version).join(
                    Message, Message.author_id == User.id,
                ).where(Message.session_id == session_id).distinct().order_by(User.id))).all()
                identities = [(author.id, author.display_name, author.auth_version,
                               await runtime.database.same_person(db, user, author.id)) for author in authors]
                identity_version = hashlib.sha256(json.dumps(identities).encode()).hexdigest()
                result["transcript_version"] = ":".join(str(part) for part in (*version, identity_version))
            swarm_member = await runtime.database.swarms.member(db, session_id)
            from .goal_models import Goal
            goal = await db.get(Goal, session_id)
            result["goal"] = runtime.database.goals.view(goal)
            if goal is not None and result["goal"] is not None:
                result["goal"]["elapsed_seconds"] = await runtime.database.goals.elapsed_seconds(db, goal)
            if swarm_member is not None:
                result["session"]["swarm_id"] = swarm_member.mission_id
            from .delegation_models import DelegatedSession
            delegation = await db.get(DelegatedSession, session_id)
            if delegation is not None:
                try:
                    await runtime.database.allowed_session(db, delegation.parent_session_id, user, metadata_only=True)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                else:
                    result["session"]["parent_session_id"] = delegation.parent_session_id
            preference = await db.get(SessionPreference, (user.id, session_id))
            result["session"]["pinned"] = bool(preference and preference.pinned)
            result["session"]["folder"] = preference.folder if preference else ""
            read = await db.get(SessionRead, (user.id, session_id))
            result["session"]["unread"] = bool(read and read.unread)
            active_turn = next((turn for turn in turns if turn.id == row.active_turn), None)
            unsettled = list((await db.scalars(select(Effect).where(
                Effect.session_id == session_id, Effect.state == "pending",
                ~failed_action_attempt(),
            ))).all())
            result["has_unsettled_effects"] = bool(unsettled)
            result["effects"] = [{
                "id": effect.id, "turn_id": effect.turn_id, "tool_name": effect.tool_name,
                "arguments": json.loads(runtime.security.unseal(effect.arguments)),
            } for effect in unsettled if await runtime.database.same_person(db, user, effect.actor_id)]
        if include_activity and turns and turns[0].status != "running":
            from .progress import turn_activity
            async with runtime.database.sessions() as db:
                latest = await db.get(Turn, turns[0].id, options=[load_only(Turn.id, Turn.preparation)])
            if latest is not None:
                result["activity"] = await turn_activity(runtime, latest)
            fresh = await runtime.database.user(user.id)
            if fresh.auth_version != user.auth_version:
                raise HTTPException(401, "Your session expired.")
            await runtime.database.session(user, session_id)
        if row.status == "waiting" and row.conversation and active_turn is not None:
            result["can_answer"] = user.id == active_turn.actor_id
            if result["can_answer"]:
                conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(row.conversation))
                result["pending"] = TypeAdapter(Any).dump_python(conversation.deferred_tool_requests, mode="json")
                from .questions import can_reply_with_text
                result["can_reply_with_text"] = can_reply_with_text(conversation.deferred_tool_requests)
                async with runtime.database.sessions() as db:
                    deadline = await db.scalar(select(Event).where(
                        Event.identity == f"optional-question:{active_turn.id}",
                    ))
                if deadline is not None:
                    result["question_deadline"] = json.loads(runtime.security.unseal(deadline.content))["deadline"]
        if include_messages:
            result["messages"] = await runtime.database.messages(user, session_id)
        return SessionDetail.model_validate(result)

    @app.get("/api/sessions/{session_id}/progress/{turn_id}", operation_id="getTurnProgress")
    async def progress(
        session_id: str, turn_id: str, include_activity: bool = False, user: User = Depends(actor),
    ) -> TurnWorkProgress:
        from .progress import turn_progress

        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
        result = (
            await work_progress.read(turn) if include_activity
            else {"messages": await turn_progress(runtime, turn)}
        )
        fresh = await runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        await runtime.database.session(user, session_id)
        return TurnWorkProgress.model_validate(result)

    @app.get("/api/sessions/{session_id}/progress/{turn_id}/actions/{action_id}", operation_id="getTurnAction")
    async def action_detail(
        session_id: str, turn_id: str, action_id: str, agent_id: str | None = None, user: User = Depends(actor),
    ) -> WorkActionDetail:
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
        result, source_session = await work_progress.action(turn, action_id, agent_id)
        fresh = await runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        await runtime.database.session(user, session_id)
        if source_session != session_id:
            # action() resolves only generated turns in this parent's assignment,
            # not later private follow-ups in the delegated conversation.
            async with runtime.database.sessions() as db:
                source = await db.get(Session, source_session)
                if source is None or source.deleted:
                    raise HTTPException(404, "Action not found.")
        return WorkActionDetail.model_validate(result)

    @app.get("/api/sessions/{session_id}/turns/{turn_id}/failure", operation_id="getTurnFailureDetails")
    async def turn_failure_details(
        session_id: str, turn_id: str, response: Response, user: User = Depends(actor),
    ) -> TurnFailureDetails:
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id or not turn.error:
                raise HTTPException(404, "Failure not found.")
            if not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(403, "Failure details are visible only to the requester.")
        details = await failure_details(runtime, turn)
        fresh = await runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        await runtime.database.session(user, session_id)
        response.headers["Cache-Control"] = "no-store"
        return TurnFailureDetails.model_validate(details)

    @app.get("/api/sessions/{session_id}/activity", operation_id="getSessionActivity")
    async def session_activity(session_id: str, user: User = Depends(actor)) -> list[SavedActivity]:
        from .progress import turn_activity

        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.scalar(select(Turn).where(
                Turn.session_id == session_id,
            ).options(load_only(Turn.id, Turn.status, Turn.preparation)).order_by(Turn.created_at.desc()).limit(1))
        result = await turn_activity(runtime, turn) if turn is not None and turn.status != "running" else []
        fresh = await runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        await runtime.database.session(user, session_id)
        return SAVED_ACTIVITY.validate_python(result)

    @app.get("/api/sessions/{session_id}/debug-trace")
    async def session_debug_trace(session_id: str, user: User = Depends(actor)):
        return JSONResponse(
            content=await debug_trace(runtime, user, session_id),
            headers={
                "Content-Disposition": f'attachment; filename="modelmesh-trace-{session_id}.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.patch("/api/sessions/{session_id}", operation_id="updateSession")
    async def edit_session(session_id: str, body: SessionEdit, user: User = Depends(actor)) -> SessionUpdateResult:
        async with runtime.database.sessions.begin() as db:
            if body.archived is True:
                # Keep the same source/user/session lock order as goal admission.
                # Archive and goal suspension commit together before dispatch can claim work.
                await runtime.database.goals.lock(db, user, session_id)
            await lock_sidebar(db, user.id)
            row = await runtime.database.allowed_session(db, session_id, user)
            if body.title is not None or body.archived is not None:
                if not await runtime.database.same_person(db, user, row.owner_id):
                    raise HTTPException(403, "Only the owner can change this conversation.")
                if body.title is not None:
                    row.title = body.title
                if body.archived is not None:
                    row.archived = body.archived
                    if body.archived:
                        from .goal_models import Goal
                        goal = await db.get(Goal, session_id)
                        if goal is not None and goal.status == "active":
                            await runtime.database.goals.pause_continuation(
                                db, goal, "The conversation was archived. Unarchive it before resuming the goal.",
                            )
                row.updated_at = now()
            if body.pinned is not None or body.folder is not None:
                pref = await db.get(SessionPreference, (user.id, session_id))
                if pref is None:
                    pref = SessionPreference(user_id=user.id, session_id=session_id)
                    db.add(pref)
                if body.pinned is not None:
                    pref.pinned = body.pinned
                if body.folder is not None:
                    if body.folder and not any(item.name == body.folder for item in await folders_for(db, user.id)):
                        raise HTTPException(404, "Folder not found.")
                    pref.folder = body.folder
            if body.unread is not None:
                read = await db.get(SessionRead, (user.id, session_id))
                if read is None:
                    read = SessionRead(user_id=user.id, session_id=session_id, message_id=0)
                    db.add(read)
                read.unread = body.unread
                if not body.unread:
                    read.message_id = await db.scalar(select(func.max(Message.id)).where(Message.session_id == session_id)) or 0
        return SessionUpdateResult(updated=True)

    @app.delete("/api/sessions/{session_id}", operation_id="deleteSession")
    async def delete_session(session_id: str, user: User = Depends(actor)) -> SessionDeleteResult:
        await runtime.database.delete_session(user, session_id)
        return SessionDeleteResult(deleted=True)

    @app.post("/api/sessions/{session_id}/restore", operation_id="restoreSession")
    async def restore_session(session_id: str, user: User = Depends(actor)) -> SessionRestoreResult:
        await runtime.database.restore_session(user, session_id)
        return SessionRestoreResult(restored=True)

    @app.get("/api/sessions/{session_id}/messages", operation_id="listSessionMessages")
    async def messages(session_id: str, after: int = 0, user: User = Depends(actor)) -> list[SessionMessage]:
        return SESSION_MESSAGES.validate_python(await runtime.database.messages(user, session_id, after=after))

    async def cancel_stopped_workflow(workflow_id: str) -> None:
        # Stop intent has committed. The dispatcher keeps ownership until
        # cancellation/terminal state is acknowledged, including lost RPCs.
        try:
            async with asyncio.timeout(2):
                client = await runtime.connect()
                await client.get_workflow_handle(workflow_id).cancel()
        except (RPCError, TimeoutError, RuntimeError, OSError):
            logging.getLogger(__name__).warning("Stop cancellation remains pending for dispatcher reconciliation.")

    @app.post("/api/sessions/{session_id}/messages", status_code=202, operation_id="createSessionMessage")
    async def send(
        session_id: str, body: NewMessage, request: Request, user: User = Depends(actor),
    ) -> GoalStartedResult | TurnView | GoalCommandResult:
        from .goals import is_goal_command
        from .skill_invocations import has_invocations
        if is_goal_command(body.prompt):
            result = await runtime.database.goals.command(user, session_id, body)
            async with runtime.database.sessions() as db:
                stopped_workflow = await db.scalar(select(Turn.workflow_id).join(
                    Session, Session.active_turn == Turn.id,
                ).where(Session.id == session_id, Turn.status == "stopped"))
            if stopped_workflow:
                await cancel_stopped_workflow(stopped_workflow)
            return (GoalStartedResult if "id" in result else GoalCommandResult).model_validate(result)
        if body.reply_to is not None:
            async with runtime.database.sessions() as db:
                await runtime.database.allowed_session(db, session_id, user)
                pending = await db.get(Turn, body.reply_to)
                if pending is None or pending.session_id != session_id:
                    raise HTTPException(409, "That question is no longer available.")
            user = answer_actor(user, pending)
        elif has_invocations(body.prompt):
            async with runtime.database.sessions() as db:
                session = await runtime.database.allowed_session(db, session_id, user)
            body = body.model_copy(update={"prompt": await runtime.skill_invocations.normalize(body.prompt, user, session)})
        return TurnView.model_validate(turn_view(await runtime.database.submit(
            user, session_id, body, received_at=request.state.modelmesh_received_at,
        )))

    @app.patch("/api/turns/{turn_id}", operation_id="updateQueuedTurn")
    async def edit_queued(turn_id: str, body: QueuedEdit, user: User = Depends(actor)) -> QueuedTurnUpdateResult:
        async with runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(404, "Message not found.")
            session = await runtime.database.allowed_session(db, turn.session_id, user)
            await runtime.database.swarms.editable_input(db, turn)
            prompt = await runtime.skill_invocations.normalize(body.prompt, user, session)
            from .models import MessageSource
            message = await db.scalar(select(Message).where(Message.identity == f"user:{turn.id}"))
            source = await db.get(MessageSource, message.identity) if message else None
            execution_prompt = prompt
            if message is not None:
                from .message_sources import public_message
                original, provenance = await public_message(runtime.database, db, message)
                if source is None:
                    source = MessageSource(message_identity=message.identity, kind=(provenance or {}).get("kind", "web"),
                                           revision=0, data=runtime.security.seal(provenance or {"kind": "web"}))
                    db.add(source)
                frozen = runtime.security.unseal(turn.prompt).decode()
                if not frozen.startswith(original):
                    raise HTTPException(409, "The original input is unavailable. Send a correction instead.")
                execution_prompt += frozen[len(original):]
                provenance = json.loads(runtime.security.unseal(source.data))
                provenance["edited_in"] = "web"
                source.data = runtime.security.seal(provenance)
                source.revision += 1
                source.mirror = True
            changed = await db.scalar(update(Turn).where(
                Turn.id == turn.id, Turn.status == "queued"
            ).values(prompt=runtime.security.seal(execution_prompt)).returning(Turn.id))
            if changed is None:
                raise HTTPException(409, "The message was already admitted. Send a correction instead.")
            await db.execute(update(Message).where(
                Message.turn_id == turn.id, Message.role == "user"
            ).values(content=runtime.security.seal(prompt)))
        return QueuedTurnUpdateResult(updated=True)

    @app.post("/api/turns/{turn_id}/steer", status_code=202, operation_id="steerQueuedTurn")
    async def steer_queued(turn_id: str, user: User = Depends(actor)) -> TurnView:
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(404, "Message not found.")
        original = answer_actor(user, turn)
        return TurnView.model_validate(turn_view(await runtime.database.steer_queued(original, turn_id)))

    @app.delete("/api/turns/{turn_id}", operation_id="cancelQueuedTurn")
    async def cancel_queued(turn_id: str, user: User = Depends(actor)) -> QueuedTurnCancelResult:
        async with runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(404, "Message not found.")
            await runtime.database.allowed_session(db, turn.session_id, user)
            await runtime.database.swarms.editable_input(db, turn)
            changed = await db.scalar(update(Turn).where(
                Turn.id == turn.id, Turn.status == "queued"
            ).values(status="stopped", finished_at=now()).returning(Turn.id))
            if changed is None:
                raise HTTPException(409, "The message was already admitted. Stop the active turn instead.")
        return QueuedTurnCancelResult(cancelled=True)

    @app.post("/api/sessions/{session_id}/stop", status_code=202, operation_id="stopSession")
    async def stop(session_id: str, user: User = Depends(actor)) -> SessionStopResult:
        workflow_id = await runtime.database.stop(user, session_id)
        if workflow_id:
            await cancel_stopped_workflow(workflow_id)
        return SessionStopResult(status="stopping")

    @app.post("/api/sessions/{session_id}/answers", status_code=202, operation_id="createSessionAnswer")
    async def answer(
        session_id: str, body: ApprovalReply, request: Request, user: User = Depends(actor),
    ) -> TurnView:
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            turn = await db.get(Turn, body.turn_id)
            if not turn or turn.session_id != session_id or not turn.conversation:
                raise HTTPException(409, "This conversation is not waiting for an answer.")
            conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(turn.conversation))
            pending = conversation.deferred_tool_requests
            if pending is None:
                raise HTTPException(409, "There are no pending requests.")
        resumed_actor = answer_actor(user, turn)
        calls = {}
        questions = {call.tool_call_id: call for call in pending.calls}
        for identity, value in body.answers.items():
            call = questions.get(identity)
            if call is None or call.tool_name != "ask_user_question":
                raise HTTPException(422, "Unknown question.")
            try:
                calls[identity] = ask_user_result(
                    AskUserRequest.from_tool_call(call), TypeAdapter(AskUserResponse).validate_python(value)
                )
            except ValueError as error:
                raise HTTPException(422, "Provide a valid response to each question.") from error
        try:
            results = pending.build_results(approvals=body.approvals, calls=calls)
            if pending.remaining(results) is not None:
                raise ValueError("Deferred requests are incomplete.")
        except (ValueError, KeyError) as error:
            raise HTTPException(422, "Complete every current question and approval.") from error
        resumed = await runtime.database.resume(
            resumed_actor, session_id, turn.id, body.request_key,
            TypeAdapter(DeferredToolResults).dump_python(results, mode="json"),
            received_at=request.state.modelmesh_received_at,
        )
        return TurnView.model_validate(turn_view(resumed))

    @app.get(
        "/api/sessions/{session_id}/events", operation_id="streamSessionEvents", response_class=EventStream,
        responses={200: {"model": LiveEvent, "description": "Server-sent events; each data frame is one LiveEvent."}},
    )
    async def events(
        request: Request, session_id: str, turn_id: str, offset: int = 0,
        user: User = Depends(actor),
    ):
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
        client = await runtime.connect()
        try:
            cursor = max(offset, int(request.headers.get("last-event-id", "-1")) + 1, 0)
        except ValueError:
            raise HTTPException(422, "Invalid event cursor.") from None
        secret_values = [runtime.settings.gateway_key.get_secret_value()]

        async def stream():
            yield "event: connected\ndata: {}\n\n"
            try:
                handle = client.get_workflow_handle(turn.workflow_id)
                next_offset = cursor
                finished = False
                while not finished and not await request.is_disconnected():
                    previous_offset = next_offset
                    async with runtime.durability.stream_agent_events(
                        client, handle, from_offset=next_offset, topic=PUBLIC_TOPIC,
                    ) as native:
                        async for event in native:
                            next_offset = native.offset + 1
                            if isinstance(event, AgentRunResultEvent):
                                finished = is_turn_run(event.result.run_id, turn.id)
                            item = public_event(event)
                            if item is not None:
                                yield f"id: {native.offset}\ndata: " + json.dumps(
                                    redact(item, secret_values), ensure_ascii=False, default=str
                                ) + "\n\n"
                    # Workflow Streams can end its iterator normally when its
                    # poll is cancelled. Preserve our reader's cancellation:
                    # reopening here would keep the old API alive during drain.
                    reader = asyncio.current_task()
                    if reader is not None and reader.cancelling():
                        raise asyncio.CancelledError
                    if next_offset == previous_offset:
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("The live stream ended; the client can refresh saved state.", exc_info=True)
                # A completed workflow no longer serves Workflow Streams. Saved app state wins.
                yield 'event: refresh\ndata: {}\n\n'
            yield 'event: finished\ndata: {}\n\n'

        async def authorize_viewer():
            from .stream_authority import authorize_stream_viewer
            await authorize_stream_viewer(runtime.database, user, session_id)

        return StreamingResponse(
            authorized_events(
                stream(), authorize_viewer, request.is_disconnected,
                reconnect=getattr(request.state, "modelmesh_reconnect", None),
            ),
            media_type="text/event-stream",
        )

    @app.get("/api/sessions/{session_id}/files", operation_id="listSessionFiles")
    async def files(session_id: str, user: User = Depends(actor)) -> list[SessionFile]:
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            rows = await db.scalars(select(Artifact).where(Artifact.session_id == session_id))
            return [SessionFile(
                id=row.id, name=row.name, media_type=row.media_type,
                size=row.size, created_at=row.created_at,
            ) for row in rows]

    @app.get("/api/sessions/{session_id}/plan", operation_id="getSessionPlan")
    async def plan(session_id: str, user: User = Depends(actor)) -> list[PlanStep]:
        await runtime.database.session(user, session_id)
        return [
            PlanStep.model_validate(item.model_dump(mode="json"))
            for item in await runtime.stores.plan(session_id).get_items()
        ]

    @app.get("/api/sessions/{session_id}/effects", operation_id="listSessionEffects")
    async def effects(session_id: str, user: User = Depends(actor)) -> list[PendingEffect]:
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            rows = await db.scalars(select(Effect).where(
                Effect.session_id == session_id,
                Effect.state == "pending",
                ~failed_action_attempt(),
            ).order_by(Effect.created_at))
            return [PendingEffect(
                id=row.id, turn_id=row.turn_id, tool_name=row.tool_name,
                arguments=json.loads(runtime.security.unseal(row.arguments)),
                created_at=row.created_at,
            ) for row in rows if await runtime.database.same_person(db, user, row.actor_id)]

    @app.post("/api/effects/{effect_id}/resolve", operation_id="resolveEffect")
    async def resolve_effect(
        effect_id: str, body: EffectResolution, user: User = Depends(actor),
    ) -> EffectResolveResult:
        async with runtime.database.sessions.begin() as db:
            row = await db.get(Effect, effect_id)
            if row is None or not await runtime.database.same_person(db, user, row.actor_id):
                raise HTTPException(404, "Action not found.")
            session = await runtime.database.allowed_session(db, row.session_id, user)
            if session.active_turn:
                raise HTTPException(409, "Stop or finish the active task before reconciling its actions.")
            if row.state != "pending":
                raise HTTPException(409, "This action already has a recorded outcome.")
            row.state = f"reviewed_{body.outcome}"
            row.resolution = runtime.security.seal({
                "reviewer": user.id, "outcome": body.outcome, "evidence": body.evidence,
            })
            row.settled_at = now()
            db.add(Message(
                session_id=row.session_id, turn_id=row.turn_id, author_id=user.id,
                role="user", identity=f"effect-resolution:{row.id}",
                content=runtime.security.seal(
                    f"Action reconciliation by the requester: {row.tool_name} was "
                    f"{body.outcome.replace('_', ' ')}. Evidence: {body.evidence}"
                ),
            ))
            # Rebuild from the public conversation so the next run sees this evidence.
            session.last_grants = None
        return EffectResolveResult(recorded=True)

    @app.get("/api/files/{artifact_id}")
    async def download(artifact_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            row = await db.get(Artifact, artifact_id)
            if row is None:
                raise HTTPException(404, "File not found.")
            await runtime.database.allowed_session(db, row.session_id, user)
        content = await runtime.objects.get(row.digest)
        current = await runtime.database.user(user.id)
        if current.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        await runtime.database.session(user, row.session_id)
        return Response(
            content, media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(Path(row.name).name)}"},
        )

    if STATIC.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        index = STATIC / "index.html"
        if index.is_file():
            html = index.read_text().replace("<!--modelmesh-base-->",
                f'<base href="{app_path(runtime.settings.public_url)}">')
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        raise HTTPException(503, "Build the web application with npm run build in native/web.")

    app.add_middleware(PublicPathMiddleware, public_url=runtime.settings.public_url)
    return app
