"""Real SDK planning IDs across old and current workflow histories."""

import re
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.planning import InMemoryPlanStore

from modelmesh.agent import WorkflowPlanning


@pytest.mark.parametrize("current_history", [False, True])
async def test_planning_ids_do_not_add_a_durable_read_to_old_workflow_histories(monkeypatch, current_history):
    from modelmesh import agent as agent_module

    markers = []

    def patched(marker):
        markers.append(marker)
        return current_history

    monkeypatch.setattr(agent_module, "workflow", SimpleNamespace(in_workflow=lambda: True, patched=patched))
    store = InMemoryPlanStore()
    observed = []

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("write_plan", {"items": [
                {"content": "Keep this running task", "status": "in_progress"},
            ]})])
        observed.append(results[-1].content)
        return ModelResponse(parts=[TextPart("Done")])

    result = await Agent(FunctionModel(model), capabilities=[WorkflowPlanning(store=store)]).run("Create a plan.")
    assert result.output == "Done"
    assert markers == ["modelmesh-planning-persisted-ids-v1"]
    tasks = await store.get_items()
    assert len(tasks) == 1
    assert (tasks[0].id in observed[0]) is current_history
    assert observed[0].startswith("Plan updated.\n\n" if current_history else "Plan updated:")


async def test_write_plan_exposes_ids_that_the_next_real_sdk_call_can_update():
    store = InMemoryPlanStore()
    observed = []

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("write_plan", {"items": [
                {"content": "Inspect changes", "status": "in_progress"},
            ]})])
        observed.append(results[-1].content)
        if results[-1].tool_name == "write_plan":
            actual_id = re.search(r"\[([0-9a-f]{8})\]", results[-1].content).group(1)
            return ModelResponse(parts=[ToolCallPart("update_task_status", {
                "task_id": actual_id, "status": "completed",
            })])
        return ModelResponse(parts=[TextPart("Done")])

    result = await Agent(FunctionModel(model), capabilities=[
        WorkflowPlanning(store=store, enable_subtasks=True),
    ]).run("Inspect the change and mark the task complete.")
    assert result.output == "Done"
    tasks = await store.get_items()
    assert len(tasks) == 1 and tasks[0].status == "completed"
    assert tasks[0].id in observed[0]
    assert not any("not found" in value.lower() for value in observed)
