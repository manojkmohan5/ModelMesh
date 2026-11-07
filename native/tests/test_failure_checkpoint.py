"""A failed turn keeps its settled conversation; the Docker computer keeps its files."""
import json

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot

from modelmesh.contracts import NewMessage
from modelmesh.failure_checkpoint import WARNING, preserve_failed_workspace
from modelmesh.models import Effect, Session, Turn

REFERENCE = {"provider": "docker", "id": "modelmesh-ws-original"}


async def interrupted(runtime, active):
    session, turn, prepared = active
    async with runtime.database.sessions.begin() as db:
        current = await db.get(Session, session.id)
        current.workspace = json.dumps(REFERENCE)
        current.workspace_scope = prepared.scope.workspace_key
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=turn.id, step_index=2, state="interrupted", messages=[
            ModelRequest(parts=[UserPromptPart("Edit the files")]),
            ModelResponse(parts=[TextPart("The edits are saved; checking them next.")]),
        ],
    ))


async def test_failed_turn_keeps_its_conversation_and_computer(runtime, actor, active):
    session, turn, _ = active
    await interrupted(runtime, active)
    await preserve_failed_workspace(runtime, turn.id)
    await runtime.database.fail(turn.id, "The worker failed.")
    async with runtime.database.sessions() as db:
        current, source = await db.get(Session, session.id), await db.get(Turn, turn.id)
        assert source.status == "failed" and not source.result_accepted and source.result is None
        assert current.conversation == source.conversation and current.conversation
        assert current.updated_at == source.finished_at
        assert json.loads(current.workspace) == REFERENCE and current.save_warning == WARNING
    followup = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Inspect and finish the saved edits.", request_key="after-failure"),
    )
    assert (await runtime.database.claim_next()).id == followup.id
    assert (await runtime.database.prepare(followup.id)).workspace == REFERENCE


async def test_failure_preservation_cannot_cross_an_uncertain_write(runtime, active):
    session, turn, prepared = active
    await interrupted(runtime, active)
    async with runtime.database.sessions.begin() as db:
        db.add(Effect(id="uncertain", session_id=session.id, turn_id=turn.id, actor_id=prepared.scope.actor_id,
                      tool_name="shell", arguments=runtime.security.seal("{}"), fingerprint="same", state="pending"))
    await preserve_failed_workspace(runtime, turn.id)
    async with runtime.database.sessions() as db:
        current = await db.get(Session, session.id)
        assert current.conversation is None and current.save_warning == WARNING
        assert (await db.get(Effect, "uncertain")).state == "pending"
