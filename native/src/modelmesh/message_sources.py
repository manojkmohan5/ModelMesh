"""Human text and verified transport provenance; model context stays in Turn.prompt."""

from __future__ import annotations

import json
from collections.abc import Sequence
from itertools import batched
from typing import Any

from sqlalchemy import select

from .models import Message, MessageSource


async def record_source(database, db, identity: str) -> None:
    """Every new message comes from the web app; other kinds exist only in saved history."""
    await db.flush()  # the message's unique identity is the provenance FK
    db.add(MessageSource(message_identity=identity, kind="web", mirror=True,
                         data=database.security.seal({"kind": "web"})))


async def public_message(database, db, row: Message, *, sources: dict | None = None) -> tuple[str, dict[str, Any] | None]:
    saved = sources.get(row.identity) if sources is not None else await db.get(MessageSource, row.identity)
    return _public_message(database, row, saved)


async def public_messages(
    database, db, rows: Sequence[Message],
) -> dict[int, tuple[str, dict[str, Any] | None]]:
    """Project only these admitted rows, on the caller's transaction/connection.

    Keep provenance local to this release, including absent records. Bounded
    batches also cover long reconstructed histories without exceeding bind limits.
    """
    result = {}
    for batch in batched(rows, 500):
        sources = {item.message_identity: item for item in await db.scalars(select(MessageSource).where(
            MessageSource.message_identity.in_([row.identity for row in batch]),
        ))}
        for row in batch:
            result[row.id] = _public_message(database, row, sources.get(row.identity))
    return result


def _public_message(database, row: Message, saved: MessageSource | None) -> tuple[str, dict[str, Any] | None]:
    content = database.security.unseal(row.content).decode()
    return content, json.loads(database.security.unseal(saved.data)) if saved is not None else None


async def memory_text(database, db, row: Message) -> str:
    content, source = await public_message(database, db, row)
    return _memory_text(row, content, source)


async def memory_texts(database, db, rows: Sequence[Message]) -> dict[int, str]:
    projected = await public_messages(database, db, rows)
    return {row.id: _memory_text(row, *projected[row.id]) for row in rows}


def _memory_text(row: Message, content: str, source: dict[str, Any] | None) -> str:
    return "" if source and (source.get("deleted") or source.get("edited_after_start")) else content


async def valid_memory_source(database, db, note: dict) -> bool:
    return (await valid_memory_sources(database, db, [note]))[0]


async def valid_memory_sources(database, db, notes: Sequence[dict]) -> list[bool]:
    """Validate all quotes against fresh authored text, never a cross-call cache."""
    references = {
        (source["native_message_id"], source["native_session_id"]) for note in notes
        if (source := note.get("source", {})).get("core") == "pydantic-v1"
        and source.get("type") == "chat" and isinstance(source.get("native_message_id"), int)
        and isinstance(source.get("native_session_id"), str)
        and isinstance(source.get("quote"), str) and source["quote"]
    }
    messages = {}
    for batch in batched({identity for identity, _ in references}, 500):
        messages.update({
            row.id: row for row in await db.scalars(select(Message).where(Message.id.in_(batch)))
            if (row.id, row.session_id) in references
        })
    # Preserve the single-note check's session gate before decrypting any body.
    texts = await memory_texts(database, db, list(messages.values()))
    return [_valid_memory_source(note, messages, texts) for note in notes]


def _valid_memory_source(note: dict, messages: dict[int, Message], texts: dict[int, str]) -> bool:
    source = note.get("source", {})
    if source.get("core") != "pydantic-v1" or source.get("type") != "chat":
        return True
    identity, quote = source.get("native_message_id"), source.get("quote")
    if not isinstance(identity, int) or not isinstance(quote, str) or not quote:
        return False
    message = messages.get(identity)
    return bool(message and message.session_id == source.get("native_session_id")
                and quote in texts[identity])
