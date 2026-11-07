"""Session history for the sidebar, search and session headers (/api/history).

Mount ``routes(runtime, actor)`` once; ``actor`` must enforce the native session/CSRF policy.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import MISSING, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from .api_views import ApiView, absent
from .database import SESSION_METADATA
from .delegation_models import DelegatedSession
from .models import Message, Session, SessionPreference, SessionRead, Turn, User
from .swarm_models import SwarmMember

if TYPE_CHECKING:
    from .runtime import Runtime

Source = Literal["native"]
NavigationScope = Literal["all", "mine"]
MAX_PAGE = 100
MAX_OFFSET = 10_000
MAX_MESSAGE_CHARS = 32_768
SEARCH_ROWS = 1000
SEARCH_BYTES = 4 * 1024 * 1024
CIPHER_CHARS = 180_000


# Response models are the source of the generated frontend types. They mirror
# the dictionaries built below; FastAPI validates and filters each response once.

class HistoryCapabilities(ApiView):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True, validate_by_name=True,
                              serialize_by_alias=True)
    read: bool
    continue_: bool = Field(alias="continue")
    rename: bool
    move: bool
    pin: bool


class HistoryMatch(ApiView):
    message_id: int | None
    snippet: str


class HistorySession(ApiView):
    """A native session row, as listed by the history routes."""
    id: str
    title: str
    owner_id: str
    visibility: Literal["private", "shared"]
    surface: str
    model: str
    status: str
    active_turn: str | None
    workspace: None
    save_warning: None
    created_at: str
    updated_at: str
    archived: bool
    deleted: bool
    pinned: bool
    folder: str
    participated: bool
    created_by_me: bool
    parent_session_id: str | None
    url: str
    source_format: str
    execution_engine: str
    read_only: bool
    capabilities: HistoryCapabilities
    unread: bool | None
    # Search hits.
    match: HistoryMatch | None | MISSING = absent()


class HistoryMessage(ApiView):
    """A public native message."""
    id: int
    role: str
    content: str
    created_at: str
    author_id: str | None
    content_truncated: bool | MISSING = absent()
    content_unavailable: bool | MISSING = absent()
    turn_id: str
    author_name: str | None | MISSING = absent()
    is_self: bool | MISSING = absent()


class HistorySearchLimits(ApiView):
    native_messages: int
    native_ciphertext_bytes: int


class SourcedHistorySession(HistorySession):
    """A /api/history row: stored IDs are kept; ``key`` is the source-qualified UI identity."""
    source: Source
    key: str
    can_manage: bool | MISSING = absent()


class HistorySessionList(ApiView):
    sessions: list[SourcedHistorySession]
    ancestors: list[SourcedHistorySession]
    has_more: bool
    next_cursor: str | None
    notice: str | None
    search_limits: HistorySearchLimits | None


class HistorySessionDetail(ApiView):
    session: SourcedHistorySession


class HistoryMessagePage(ApiView):
    messages: list[HistoryMessage]
    has_more: bool
    next_offset: int | None


def bounded(limit: int, offset: int = 0) -> None:
    if not 1 <= limit <= MAX_PAGE or not 0 <= offset <= MAX_OFFSET:
        raise HTTPException(422, "Use a page size of 1–100 and an offset of 0–10000.")


def sourced(row: dict[str, Any]) -> dict[str, Any]:
    """Do not rewrite stored IDs. UI identity is the separate source-qualified key."""
    return row | {"source": "native", "key": f"native:{row['id']}", "url": "/#" + quote(row["id"], safe="")}


def newest_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (str(row.get("updated_at", "")), row["id"]), reverse=True)


@dataclass(frozen=True)
class Reader:
    user: User


class HistoryReadService:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def current(self, reader: Reader) -> None:
        current = await self.runtime.database.user(reader.user.id)
        if current.auth_version != reader.user.auth_version:
            raise HTTPException(401, "Your session expired.")

    @staticmethod
    def private_candidate(reader: Reader, session=Session):
        """SQL candidates only; native_recheck validates the live audience."""
        return session.owner_id == reader.user.id

    def visible(self, reader: Reader, *, deleted: bool = False):
        access = or_(self.private_candidate(reader), Session.visibility == "shared")
        clauses = [Session.deleted.is_(deleted), access]
        if not deleted:
            root = aliased(Session)
            membership = select(SwarmMember.session_id).where(SwarmMember.session_id == Session.id)
            parent_access = or_(
                self.private_candidate(reader, root), root.visibility == "shared",
            )
            visible_parent = select(SwarmMember.session_id).join(
                root, root.id == SwarmMember.mission_id,
            ).where(SwarmMember.session_id == Session.id, root.deleted.is_(False), parent_access)
            clauses.append(~membership.exists() | visible_parent.exists())
        return and_(*clauses)

    def native_query(self, reader: Reader):
        preference = and_(SessionPreference.session_id == Session.id, SessionPreference.user_id == reader.user.id)
        read = and_(SessionRead.session_id == Session.id, SessionRead.user_id == reader.user.id)
        participated = select(Turn.id).where(
            Turn.session_id == Session.id, Turn.actor_id == reader.user.id,
        ).exists()
        unread = select(Message.id).where(
            Message.session_id == Session.id, Message.id > SessionRead.message_id,
        ).exists()
        parent = aliased(Session)
        parent_access = or_(
            parent.visibility == "shared",
            self.private_candidate(reader, parent),
        )
        return select(
            Session.id, Session.title, Session.owner_id, Session.visibility, Session.surface, Session.model,
            Session.status, Session.active_turn, Session.archived, Session.deleted, Session.created_at,
            Session.updated_at, SessionPreference.pinned, SessionPreference.folder,
            SessionRead.unread.label("marked_unread"), unread.label("new_messages"),
            or_(Session.owner_id == reader.user.id, self.private_candidate(reader)).label("created_by_me"),
            (and_(Session.owner_id != reader.user.id, participated)).label("participated"),
            parent.id.label("parent_session_id"),
        ).outerjoin(SessionPreference, preference).outerjoin(SessionRead, read).outerjoin(
            DelegatedSession, DelegatedSession.session_id == Session.id,
        ).outerjoin(parent, and_(
            parent.id == DelegatedSession.parent_session_id, parent.deleted.is_(False), parent_access,
        ))

    @staticmethod
    def native_view(row: Any) -> dict[str, Any]:
        view = dict(row)
        marked = view.pop("marked_unread")
        new_messages = view.pop("new_messages")
        view.setdefault("parent_session_id", None)
        view.update({
            "source_format": "modelmesh-native-v1", "execution_engine": "pydantic-v1",
            "read_only": False, "pinned": bool(view["pinned"]), "folder": view["folder"] or "",
            "unread": bool(marked or new_messages), "workspace": None, "save_warning": None,
            "capabilities": {
                "read": not view["deleted"], "continue": not view["deleted"],
                "rename": not view["deleted"], "move": not view["deleted"], "pin": not view["deleted"],
            },
        })
        return sourced(view)

    async def native_recheck(self, reader: Reader, ids: list[str], *, deleted: bool = False) -> set[str]:
        await self.current(reader)
        async with self.runtime.database.sessions() as db:
            member = aliased(SwarmMember)
            rows = (await db.execute(select(
                Session.id, Session.surface, DelegatedSession.session_id.label("delegated"),
                member.session_id.label("swarm"),
            ).outerjoin(
                DelegatedSession, DelegatedSession.session_id == Session.id,
            ).outerjoin(
                member, member.session_id == Session.id,
            ).where(self.visible(reader, deleted=deleted), Session.id.in_(ids)))).mappings().all()
            valid = {row["id"] for row in rows}
            for row in rows:
                # Ordinary web sessions have no inherited audience. The fresh
                # query already checks their owner, visibility and deletion
                # state. Keep both revalidation fences, without four additional
                # lookups per row at each fence.
                if not deleted and row["surface"] == "web" and row["delegated"] is None and row["swarm"] is None:
                    continue
                session_id = row["id"]
                try:
                    if deleted:
                        session = await db.get(Session, session_id, options=SESSION_METADATA)
                        if session is None or not await self.runtime.database.same_person(db, reader.user, session.owner_id):
                            raise HTTPException(404, "Conversation not found.")
                        await self.runtime.database.check_source_audience(db, session, reader.user, metadata_only=True)
                    else:
                        await self.runtime.database.allowed_session(db, session_id, reader.user, metadata_only=True)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                    valid.discard(session_id)
        return valid

    async def native_list(
        self, reader: Reader, *, limit: int, offset: int, archived: bool = False, deleted: bool = False,
        scope: NavigationScope = "all",
    ) -> dict[str, Any]:
        async with self.runtime.database.sessions() as db:
            query = self.native_query(reader).where(self.visible(reader, deleted=deleted))
            if not deleted:
                query = query.where(
                    Session.archived.is_(archived),
                    ~select(SwarmMember.session_id).where(
                        SwarmMember.session_id == Session.id, SwarmMember.position > 0,
                    ).exists(),
                )
                if scope == "mine":
                    columns = query.selected_columns
                    query = query.where(or_(
                        columns.created_by_me, columns.participated, columns.pinned.is_(True),
                        func.coalesce(columns.folder, "") != "",
                    ))
            rows = list((await db.execute(query.order_by(Session.updated_at.desc(), Session.id.desc())
                                          .offset(offset).limit(limit + 1))).mappings())
        valid = await self.native_recheck(reader, [r["id"] for r in rows], deleted=deleted)
        return {"sessions": [self.native_view(r) for r in rows[:limit] if r["id"] in valid],
                "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None}

    async def native_search(self, reader: Reader, query: str, *, limit: int, offset: int) -> dict[str, Any]:
        """Ciphertext is searched only after ACL filtering and within explicit budgets."""
        candidates: dict[str, dict[str, Any]] = {}
        matches: dict[str, dict[str, Any]] = {}
        scanned, consumed, limited = 0, 0, False
        async with self.runtime.database.sessions() as db:
            planned = (await db.execute(select(Message.id, Message.session_id).join(
                Session, Session.id == Message.session_id,
            ).where(
                self.visible(reader), Message.role.in_(("user", "assistant")),
            ).order_by(Message.id.desc()).limit(SEARCH_ROWS + 1))).all()
        async with self.runtime.database.sessions() as db:
            title_rows = (await db.execute(self.native_query(reader).where(
                self.visible(reader), func.lower(Session.title).contains(query.lower(), autoescape=True),
            ).order_by(Session.updated_at.desc(), Session.id.desc()).limit(offset + limit + 1))).mappings()
            candidates.update({r["id"]: self.native_view(r) for r in title_rows})
            # Never select encrypted native working snapshots or another user's private messages.
            stream = await db.stream(select(
                Message.id, Message.session_id, Message.turn_id, Message.role, Message.author_id, Message.identity,
                func.substr(Message.content, 1, CIPHER_CHARS + 1).label("content"),
            ).join(Session, Session.id == Message.session_id).where(
                self.visible(reader), Message.role.in_(("user", "assistant")),
                Message.id.in_([row.id for row in planned]),
            ).order_by(Message.id.desc()).limit(SEARCH_ROWS + 1).execution_options(yield_per=16))
            try:
                async for row in stream.mappings():
                    scanned += 1
                    consumed += len(row["content"])
                    if scanned > SEARCH_ROWS or consumed > SEARCH_BYTES:
                        limited = True
                        break
                    if len(row["content"]) > CIPHER_CHARS:
                        limited = True
                        continue
                    try:
                        try:
                            await self.runtime.database.allowed_session(db, row["session_id"], reader.user)
                        except HTTPException as error:
                            if error.status_code != 404:
                                raise
                            continue
                        from .message_sources import public_message
                        content, _ = await public_message(self.runtime.database, db, Message(**dict(row)))
                    except (InvalidToken, UnicodeError, ValueError):
                        limited = True
                        continue
                    if query.casefold() in content.casefold():
                        matches.setdefault(row["session_id"], {
                            "message_id": row["id"], "snippet": content[:320],
                        })
            finally:
                await stream.close()
            if matches:
                rows = (await db.execute(self.native_query(reader).where(
                    self.visible(reader), Session.id.in_(matches),
                ))).mappings()
                candidates.update({r["id"]: self.native_view(r) | {"match": matches[r["id"]]} for r in rows})
        ordered = newest_first(list(candidates.values()))
        page = ordered[offset:offset + limit]
        valid = await self.native_recheck(reader, [r["id"] for r in page])
        return {
            "sessions": [r for r in page if r["id"] in valid], "has_more": len(ordered) > offset + limit,
            "next_offset": offset + limit if len(ordered) > offset + limit else None,
            "notice": "Message search is limited to 1,000 messages / 4 MiB per query. "
                      "Narrow the search for older matches." if limited else None,
        }

    async def native_ancestors(
        self, reader: Reader, rows: list[dict[str, Any]], *, archived: bool, scope: NavigationScope,
    ) -> list[dict[str, Any]]:
        """Sidebar context is separate from paginated hits and reuses their ACLs."""
        seen = {row["id"] for row in rows}
        pending = {
            row["parent_session_id"] for row in rows
            if row.get("parent_session_id") and not row["pinned"] and not row["folder"]
        } - seen
        ancestors: list[dict[str, Any]] = []
        # Native delegation admits children and grandchildren, never an unbounded tree.
        for _ in range(2):
            if not pending:
                break
            async with self.runtime.database.sessions() as db:
                query = self.native_query(reader).where(
                    self.visible(reader), Session.id.in_(pending), Session.archived.is_(archived),
                )
                if scope == "mine":
                    columns = query.selected_columns
                    query = query.where(or_(
                        columns.created_by_me, columns.participated, columns.pinned.is_(True),
                        func.coalesce(columns.folder, "") != "",
                    ))
                found = [self.native_view(row) for row in (await db.execute(query)).mappings()]
            seen.update(pending)
            ancestors.extend(found)
            pending = {
                row["parent_session_id"] for row in found
                if row.get("parent_session_id") and not row["pinned"] and not row["folder"]
            } - seen
        valid = await self.native_recheck(reader, [row["id"] for row in ancestors]) if ancestors else set()
        return [row for row in ancestors if row["id"] in valid]

    @staticmethod
    def cursor_scope(reader: Reader, query: str, archived: bool, deleted: bool, scope: NavigationScope = "all") -> str:
        return hashlib.sha256(json.dumps([reader.user.id, query, archived, deleted, scope]).encode()).hexdigest()

    @staticmethod
    def offset(cursor: str | None, scope: str) -> int:
        if cursor is None:
            return 0
        try:
            if len(cursor) > 512:
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(data, dict) or data.get("scope") != scope or data.get("v") != 1:
                raise ValueError
            position = data["native"]
            if type(position) is not int or not 0 <= position <= MAX_OFFSET:
                raise ValueError
            return position
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "Invalid history cursor. Restart the search.") from None

    async def listing(
        self, reader: Reader, *, query: str = "", limit: int = 50,
        cursor: str | None = None, archived: bool = False, deleted: bool = False,
        scope: NavigationScope = "all",
    ) -> dict[str, Any]:
        bounded(limit)
        query = query.strip()
        if len(query) > 200:
            raise HTTPException(422, "Search text must contain at most 200 characters.")
        await self.current(reader)
        cursor_scope = self.cursor_scope(reader, query, archived, deleted, scope)
        offset = self.offset(cursor, cursor_scope)
        notices = []
        try:
            async with asyncio.timeout(10):
                page = await self.native_search(reader, query, limit=limit, offset=offset) if query else \
                    await self.native_list(reader, limit=limit, offset=offset,
                                           archived=archived, deleted=deleted, scope=scope)
        except (HTTPException, SQLAlchemyError, TimeoutError) as exc:
            if isinstance(exc, HTTPException) and exc.status_code < 500:
                raise
            raise HTTPException(503, "Session history is temporarily unavailable. Refresh to retry.") from None
        if page.get("notice"):
            notices.append(page["notice"])
        # Current authority fences the page too.
        await self.current(reader)
        valid = await self.native_recheck(reader, [r["id"] for r in page["sessions"]], deleted=deleted)
        chosen = [row for row in page["sessions"] if row["id"] in valid][:limit]
        has_more = page["has_more"]
        offset += len(chosen)
        if not chosen and page["has_more"]:
            offset = page["next_offset"]
        if offset > MAX_OFFSET:
            has_more = False
            notices.append("History page limit reached. Narrow the search to find more sessions.")
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            {"v": 1, "scope": cursor_scope, "native": offset}, separators=(",", ":"),
        ).encode()).decode().rstrip("=") if has_more else None
        ancestors: list[dict[str, Any]] = []
        if not query and not deleted and any(row.get("parent_session_id") for row in chosen):
            try:
                async with asyncio.timeout(3):
                    ancestors = await self.native_ancestors(reader, chosen, archived=archived, scope=scope)
            except (SQLAlchemyError, TimeoutError):
                notices.append("Some parent conversations are temporarily unavailable. Refresh to retry.")
            valid = await self.native_recheck(reader, [row["id"] for row in chosen])
            chosen = [row for row in chosen if row["id"] in valid]
        rows = [*chosen, *ancestors]
        if rows:
            async with self.runtime.database.sessions() as db:
                owners = {owner: await self.runtime.database.same_person(db, reader.user, owner)
                          for owner in {row["owner_id"] for row in rows}}
                for row in rows:
                    row["can_manage"] = owners[row["owner_id"]]
                    row["capabilities"]["rename"] = row["can_manage"] and not row["deleted"]
        return {"sessions": chosen, "ancestors": ancestors, "has_more": has_more, "next_cursor": next_cursor,
                "notice": " ".join(notices) or None,
                "search_limits": {
                    "native_messages": SEARCH_ROWS, "native_ciphertext_bytes": SEARCH_BYTES,
                } if query else None}

    async def detail(self, reader: Reader, run_id: str) -> dict[str, Any]:
        await self.current(reader)
        async with self.runtime.database.sessions() as db:
            row = (await db.execute(self.native_query(reader).where(
                self.visible(reader), Session.id == run_id,
            ))).mappings().first()
        if row is None or run_id not in await self.native_recheck(reader, [run_id]):
            raise HTTPException(404, "Conversation not found.")
        result = {"session": self.native_view(row)}
        async with self.runtime.database.sessions() as db:
            result["session"]["can_manage"] = await self.runtime.database.same_person(db, reader.user, row["owner_id"])
            result["session"]["capabilities"]["rename"] = result["session"]["can_manage"]
        await self.current(reader)
        return result

    async def messages(self, reader: Reader, run_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        bounded(limit, offset)
        await self.detail(reader, run_id)
        async with self.runtime.database.sessions() as db:
            rows = list(await db.scalars(select(Message).where(
                Message.session_id == run_id, Message.role.in_(("user", "assistant")),
            ).order_by(Message.id).offset(offset).limit(limit + 1)))
            from .message_sources import public_message
            messages = []
            for row in rows[:limit]:
                item = {key: getattr(row, key) for key in ("id", "turn_id", "role", "author_id", "created_at")}
                try:
                    if len(row.content) > CIPHER_CHARS:
                        raise ValueError("Message exceeds display budget")
                    decoded, _ = await public_message(self.runtime.database, db, row)
                    author = await db.get(User, row.author_id) if row.author_id else None
                    item.update(content=decoded[:MAX_MESSAGE_CHARS], content_truncated=len(decoded) > MAX_MESSAGE_CHARS,
                                author_name=author.display_name if author else None,
                                is_self=await self.runtime.database.same_person(db, reader.user, row.author_id))
                except (InvalidToken, ValueError, UnicodeError):
                    item.update(content="", content_unavailable=True)
                messages.append(item)
            result = {"messages": messages, "has_more": len(rows) > limit,
                      "next_offset": offset + limit if len(rows) > limit else None}
        await self.detail(reader, run_id)
        return result


def routes(runtime: Runtime, actor) -> APIRouter:
    router = APIRouter()
    service = HistoryReadService(runtime)

    async def reader(response: Response, user: User = Depends(actor)) -> Reader:
        response.headers["Cache-Control"] = "no-store"
        current = Reader(user)
        await service.current(current)
        return current

    @router.get("/api/history/sessions", operation_id="listHistorySessions", response_model=HistorySessionList)
    async def sessions(
        limit: int = Query(50, ge=1, le=MAX_PAGE),
        cursor: str | None = Query(None, max_length=512), archived: bool = False, deleted: bool = False,
        scope: NavigationScope = "all",
        current: Reader = Depends(reader),
    ):
        return await service.listing(current, limit=limit, cursor=cursor, archived=archived, deleted=deleted, scope=scope)

    @router.get("/api/history/search", operation_id="searchHistory", response_model=HistorySessionList)
    async def search(
        q: str = Query("", max_length=200), limit: int = Query(20, ge=1, le=MAX_PAGE),
        cursor: str | None = Query(None, max_length=512), current: Reader = Depends(reader),
    ):
        return await service.listing(current, query=q, limit=limit, cursor=cursor)

    @router.get("/api/history/sessions/{run_id}", operation_id="getHistorySession", response_model=HistorySessionDetail)
    async def detail(run_id: str, current: Reader = Depends(reader)):
        return await service.detail(current, run_id)

    @router.get(
        "/api/history/sessions/{run_id}/messages", operation_id="listHistoryMessages",
        response_model=HistoryMessagePage,
    )
    async def messages(
        run_id: str, limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET), current: Reader = Depends(reader),
    ):
        return await service.messages(current, run_id, limit=limit, offset=offset)

    return router
