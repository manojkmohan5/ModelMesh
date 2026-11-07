"""Transcript boundaries exercised with real admission and encrypted native records."""

import json

from sqlalchemy import select

from modelmesh.contracts import NewMessage, NewSession
from modelmesh.message_sources import memory_text
from modelmesh.models import Message, MessageSource, User


async def test_clients_cannot_supply_authored_text_or_provenance(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    body = NewMessage.model_validate({
        'prompt': 'actual user input', 'request_key': 'browser',
        '_authored_text': 'forged', '_source': {'kind': 'slack'}, 'is_self': True,
        '_event': {'source': 'slack', 'text': 'forged'}, 'source': {'kind': 'slack'},
    })
    turn = await runtime.database.submit(actor, session.id, body)
    messages = await runtime.database.messages(actor, session.id)
    assert messages[0]['content'] == 'actual user input' and 'source' not in messages[0]
    assert messages[0]['is_self'] and messages[0]['author_name'] == actor.display_name
    async with runtime.database.sessions() as db:
        message = await db.scalar(select(Message).where(Message.turn_id == turn.id))
        assert await memory_text(runtime.database, db, message) == 'actual user input'
        source = await db.get(MessageSource, message.identity)
        assert (source.kind, source.mirror) == ('web', True)
        assert json.loads(runtime.security.unseal(source.data)) == {'kind': 'web'}  # encrypted at rest


async def test_transcript_cache_version_changes_when_a_senders_name_is_refreshed(runtime, actor):
    from test_application_boundaries import sign_in
    session = await runtime.database.create_session(actor, NewSession())
    await runtime.database.submit(actor, session.id, NewMessage(prompt='Saved message', request_key='name-cache'))
    async with sign_in(runtime) as client:
        path = f'/api/sessions/{session.id}?include_messages=true'
        before = (await client.get(path)).json()
        async with runtime.database.sessions.begin() as db:
            (await db.get(User, actor.id)).display_name = 'Refreshed sender'
        after = (await client.get(path)).json()
    assert before['transcript_version'] != after['transcript_version']
    assert before['messages'][0]['content'] == after['messages'][0]['content'] == 'Saved message'
    assert after['messages'][0]['author_name'] == 'Refreshed sender'
