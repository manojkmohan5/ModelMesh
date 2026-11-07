"""Recorded preparation activity policy and compatibility with pre-change history."""

import asyncio

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from modelmesh.storage import encrypted_converter
from modelmesh.worker import make_worker
from modelmesh.worker_capacity import ActivityCapacity
from modelmesh.workflow import PREPARE_VERSION, TurnWorkflow


@pytest.mark.parametrize("old_history", [False, True])
async def test_preparation_policy_and_old_history_replay(runtime, active, monkeypatch, old_history):
    _, turn, _ = active
    original_patched = workflow.patched
    if old_history:
        # Execute the exact former scheduling branch without recording its new marker.
        def before_preparation_change(identity, **kwargs):
            return False if identity == PREPARE_VERSION else original_patched(identity, **kwargs)
        monkeypatch.setattr(workflow, "patched", before_preparation_change)

    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
            history = await handle.fetch_history()
        preparation = [
            event.activity_task_scheduled_event_attributes for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
            and event.activity_task_scheduled_event_attributes.activity_type.name == "modelmesh_prepare_turn_v1"
        ]
        assert len(preparation) == 1
        recorded = preparation[0]
        assert recorded.start_to_close_timeout.seconds == (60 if old_history else 1200)
        assert recorded.schedule_to_close_timeout.seconds == (180 if old_history else 1200)
        assert recorded.heartbeat_timeout.seconds == (0 if old_history else 30)
        assert recorded.retry_policy.maximum_attempts == 3
        monkeypatch.setattr(workflow, "patched", original_patched)
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)

