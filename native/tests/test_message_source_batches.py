"""Bounded provenance reads preserve authored text and fresh release semantics."""

from contextlib import contextmanager

import pytest
from pydantic_ai import ConversationTypeAdapter
from sqlalchemy import event

from modelmesh.contracts import NewMessage, NewSession
from modelmesh.message_sources import public_messages, valid_memory_sources
from modelmesh.models import Message, MessageSource, Session, Turn


@contextmanager
def statements(database):
    captured = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        captured.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        yield captured
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


async def seeded_messages(runtime, actor, count):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Seed", request_key="seed"))
    async with runtime.database.sessions.begin() as db:
        rows = [Message(
            session_id=session.id, turn_id=turn.id, author_id=actor.id, role="user",
            content=runtime.security.seal(f"Authored preference {index}."), identity=f"batch:{index}",
            created_at="2026-01-01T00:00:00+00:00",
        ) for index in range(count)]
        db.add_all(rows)
        await db.flush()
        db.add_all([MessageSource(
            message_identity=row.identity, kind="web", data=runtime.security.seal({"kind": "web"}),
        ) for row in rows])
    return session, turn, rows


@pytest.mark.parametrize("count,query_budget", [(1, 2), (40, 2), (501, 4)])
async def test_quote_validation_batches_unique_sources_and_checks_each_note(runtime, actor, count, query_budget):
    session, _, rows = await seeded_messages(runtime, actor, count)
    notes = [{"source": {
        "core": "pydantic-v1", "type": "chat", "native_message_id": row.id,
        "native_session_id": session.id, "quote": f"Authored preference {index}.",
    }} for index, row in enumerate(rows)]
    notes += [
        notes[0],  # repeated evidence must not cost another SELECT
        {"source": {**notes[0]["source"], "native_session_id": "another-session"}},
        {"source": {**notes[0]["source"], "native_message_id": 999999}},
        {"source": {**notes[0]["source"], "quote": "Context that the person did not author"}},
        {"source": {**notes[0]["source"], "quote": ""}},
        {"source": {"type": "manual"}},
    ]
    async with runtime.database.sessions() as db:
        with statements(runtime.database) as queries:
            valid = await valid_memory_sources(runtime.database, db, notes)
    assert valid == [True] * count + [True, False, False, False, False, True]
    assert len(queries) <= query_budget
    async with runtime.database.sessions.begin() as db:
        source = await db.get(MessageSource, rows[0].identity)
        source.data = runtime.security.seal({"kind": "slack", "deleted": True})
    async with runtime.database.sessions() as db:
        assert (await valid_memory_sources(runtime.database, db, notes))[0] is False


async def test_manual_and_empty_provenance_need_no_database_reads(runtime):
    async with runtime.database.sessions() as db:
        with statements(runtime.database) as queries:
            assert await valid_memory_sources(runtime.database, db, []) == []
            assert await valid_memory_sources(runtime.database, db, [{"source": {"type": "manual"}}]) == [True]
            assert await public_messages(runtime.database, db, []) == {}
    assert queries == []


async def test_mismatched_session_is_rejected_before_decrypting_source(runtime, actor):
    _, _, rows = await seeded_messages(runtime, actor, 1)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Message, rows[0].id)).content = "Must not decrypt a different session's message"
    async with runtime.database.sessions() as db:
        assert await valid_memory_sources(runtime.database, db, [{"source": {
            "core": "pydantic-v1", "type": "chat", "native_message_id": rows[0].id,
            "native_session_id": "different-session", "quote": "Authored preference",
        }}]) == [False]


async def test_prepare_reconstructs_ordered_history_with_one_provenance_read(runtime, actor):
    session, seed, _rows = await seeded_messages(runtime, actor, 40)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, seed.id)).status = "completed"
        saved = await db.get(Session, session.id)
        saved.active_turn = None
        saved.conversation = runtime.security.seal("Different actor/model context")
        saved.last_actor_id = None
    current = await runtime.database.submit(actor, session.id, NewMessage(prompt="Next", request_key="next"))
    assert (await runtime.database.claim_next()).id == current.id
    with statements(runtime.database) as queries:
        prepared = await runtime.database.prepare(current.id)
    assert sum("FROM modelmesh_message_sources" in query for query in queries) == 1
    history = ConversationTypeAdapter.validate_json(prepared.conversation).messages
    assert len(history) == 41
    for index, message in enumerate(history[1:]):
        assert message.parts[0].content == f"Earlier public message by {actor.id}:\nAuthored preference {index}."
