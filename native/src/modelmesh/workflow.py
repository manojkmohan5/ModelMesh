"""One durable execution owner. Pydantic AI owns every agent step."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic import TypeAdapter
    from pydantic_ai import (
        Conversation,
        ConversationTypeAdapter,
        DeferredToolRequests,
        DeferredToolResults,
        ModelRequestNode,
        RunCancelled,
    )
    from pydantic_ai._agent_graph import build_run_context
    from pydantic_ai.agent import AbstractAgent
    from pydantic_ai.durable_exec.temporal import AgentEventStream, PydanticAIWorkflow
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.run import AgentRun
    from pydantic_ai.usage import UsageLimits
    from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError

    from .agent_messages import (
        DELIVERY_VERSION,
        AgentMail,
        MailObsolete,
        MailRead,
        MailReady,
        model_message,
        read_agent_mail_v2,
        receipts,
    )
    from .contracts import CompletedTurn, PreparedTurn, RunScope, SteeringInput
    from .deployment_handoff import VERSION as DEPLOYMENT_HANDOFF_VERSION
    from .deployment_handoff import DeploymentHandoff
    from .failure_diagnostics import FAILURE_VERSION, capture_failure
    from .history_handoff import VERSION as HISTORY_VERSION
    from .history_handoff import AgentCheckpoint, CheckpointSave, HistoryHandoff, HistoryLimits
    from .images import prompt_content
    from .input_identity import identified_prompt
    from .live_events import public_event_sink
    from .request_limits import unlimited_model_requests
    from .runtime import current


CONTROL = {
    "start_to_close_timeout": timedelta(seconds=60),
    "schedule_to_close_timeout": timedelta(minutes=3),
    "retry_policy": RetryPolicy(maximum_attempts=3),
}
PREPARE_VERSION = "modelmesh-turn-preparation-timeout-v1"
PREPARE = {
    **CONTROL,
    "start_to_close_timeout": timedelta(minutes=20),
    "schedule_to_close_timeout": timedelta(minutes=20),
    "heartbeat_timeout": timedelta(seconds=30),
}


@workflow.defn(name="ModelMeshPydanticTurnV1")
class TurnWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__: Sequence[AbstractAgent[Any, Any]] = ()

    @workflow.init
    def __init__(self, turn_id: str, handoff: HistoryHandoff | None = None):
        self.turn_id = turn_id
        self.events = AgentEventStream(
            prior_state=handoff.stream_state if handoff else None,
            drain_timeout=timedelta(seconds=1),
        )
        self.agent_run: AgentRun[RunScope, str | DeferredToolRequests] | None = None
        self.scope: RunScope | None = None
        self.pending_agent_mail: list[int] = list(handoff.pending_agent_mail) if handoff else []
        self.enqueued_agent_mail: set[int] = set()
        self.mail_handlers: set[int] = set()
        self.mail_closing = False
        self.finished = False
        self.steered: set[str] = set(handoff.steered) if handoff else set()
        self.pending_steering: list[str] = list(handoff.pending_steering) if handoff else []
        self.handing_off = False
        self.deployment_handoff = handoff.pending_deployment_handoff if handoff else None
        self.previous_deployment_handoff = handoff.deployment_handoff if handoff else None

    @workflow.signal
    def checkpoint_handoff(self, request: DeploymentHandoff) -> None:
        if (
            request != self.previous_deployment_handoff
            and request.applies_to(workflow.info().get_current_deployment_version())
        ):
            self.deployment_handoff = request

    @workflow.query
    def deployment_checkpoint(self) -> dict[str, Any]:
        version = workflow.info().get_current_deployment_version()
        return {
            "deployment_name": version.deployment_name if version else None,
            "build_id": version.build_id if version else None,
            "request_id": self.deployment_handoff.request_id if self.deployment_handoff else None,
            "previous_request_id": (
                self.previous_deployment_handoff.request_id if self.previous_deployment_handoff else None
            ),
            "saving": self.handing_off,
        }

    @workflow.signal
    async def agent_message(self, message_id: int) -> None:
        if message_id in self.enqueued_agent_mail or message_id in self.mail_handlers:
            return
        recoverable = workflow.patched(DELIVERY_VERSION)
        if message_id not in self.pending_agent_mail:
            self.pending_agent_mail.append(message_id)
        if self.handing_off:
            return
        self.mail_handlers.add(message_id)
        try:
            # Signal order is durable. Preserve it while loading encrypted
            # payloads so a quick completion read cannot overtake progress.
            await workflow.wait_condition(lambda: (
                self.finished or self.handing_off or (recoverable and self.mail_closing)
                or (self.agent_run is not None and self.pending_agent_mail[0] == message_id)
            ))
            if (
                self.finished or self.handing_off or (recoverable and self.mail_closing)
                or self.agent_run is None or self.scope is None
            ):
                return
            mail = await self.load_agent_mail(message_id, recoverable=recoverable)
            if self.handing_off or (recoverable and (self.finished or self.mail_closing)):
                return
            if mail is not None:
                try:
                    self.agent_run.enqueue(model_message(mail), priority="asap")
                except UserError:
                    return  # Preserve durable mail without starting another turn.
                self.enqueued_agent_mail.add(message_id)
            self.pending_agent_mail.remove(message_id)
        finally:
            self.mail_handlers.discard(message_id)

    async def load_agent_mail(self, message_id: int, *, recoverable: bool) -> AgentMail | None:
        """Resolve the queue head without surrendering ownership on a failed read."""
        while True:
            if recoverable and (self.finished or self.handing_off or self.mail_closing):
                return None
            try:
                if not recoverable:
                    # Recorded executions keep their original activity and
                    # empty-read semantics. New signals use the typed protocol.
                    legacy = await workflow.execute_activity(
                        "modelmesh_read_agent_mail_v1", args=[self.scope, message_id], result_type=MailRead, **CONTROL,
                    )
                    return legacy.mail
                result = await workflow.execute_activity(
                    read_agent_mail_v2, args=[self.scope, message_id], **CONTROL,
                )
                if isinstance(result, MailReady):
                    return result.mail
                if isinstance(result, MailObsolete):
                    return None
                # Unavailable is a live authorization refusal, not a receipt.
            except ActivityError:
                workflow.logger.warning("Agent mail read failed; retaining delivery intent.")
            if self.finished or self.handing_off or (recoverable and self.mail_closing):
                return None
            if recoverable:
                # Retry through Temporal, without waking the model or delaying
                # shutdown/history handoff behind a non-interruptible timer.
                with suppress(TimeoutError):
                    await workflow.wait_condition(
                        lambda: self.finished or self.handing_off or self.mail_closing, timeout=5,
                    )
            else:
                await workflow.sleep(5)

    @asynccontextmanager
    async def agent_mail_lifetime(self) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # AgentEventStream.close waits for every signal handler, including
            # mail. Close delivery before that outer context exits, even when
            # agent startup or cancellation raises before self.finished is set.
            # Legacy histories ignore this state through DELIVERY_VERSION.
            self.mail_closing = True

    @workflow.signal
    async def steer(self, turn_id: str) -> None:
        if turn_id in self.steered:
            return
        if turn_id not in self.pending_steering:
            self.pending_steering.append(turn_id)
        if self.handing_off:
            return
        self.steered.add(turn_id)
        try:
            value = await workflow.execute_activity(
                "modelmesh_prepare_steering_v1",
                args=[self.turn_id, turn_id], result_type=SteeringInput, **CONTROL,
            )
        except ActivityError:
            self.steered.discard(turn_id)
            if not self.handing_off:
                self.pending_steering.remove(turn_id)
            workflow.logger.warning("Steering preparation did not settle; the saved input remains pending.")
            return
        await workflow.wait_condition(lambda: self.agent_run is not None or self.finished)
        if self.handing_off:
            self.steered.discard(turn_id)
            return
        enqueue_id = None
        if not self.finished and self.agent_run is not None:
            try:
                content = prompt_content(value.prompt, value.images) or ""
                if workflow.patched("modelmesh-steering-part-identity-v1"):
                    content = identified_prompt(content, value.turn_id, self.turn_id)
                enqueue_id = self.agent_run.enqueue(
                    ModelRequest(
                        parts=[UserPromptPart(content=content)],
                        metadata={
                            "modelmesh_input_id": value.turn_id, "modelmesh_parent_turn_id": self.turn_id,
                        },
                    ),
                    priority=value.mode,
                )
            except UserError:
                pass  # Native final drain won the race; preserve it as a queued follow-up.
        await workflow.execute_activity(
            "modelmesh_steering_receipt_v1", args=[turn_id, enqueue_id], **CONTROL
        )
        self.pending_steering.remove(turn_id)

    @workflow.run
    async def run(self, turn_id: str, handoff: HistoryHandoff | None = None) -> dict:
        prepared: PreparedTurn | None = None
        try:
            history_handoff = workflow.patched(HISTORY_VERSION)
            deployment_handoff = workflow.patched(DEPLOYMENT_HANDOFF_VERSION)
            if self.deployment_handoff and not self.deployment_handoff.applies_to(
                workflow.info().get_current_deployment_version(),
            ):
                self.deployment_handoff = None
            resumed: AgentCheckpoint | None = None
            if handoff is not None:
                resumed = await workflow.execute_activity(
                    "modelmesh_load_history_checkpoint_v1", args=[turn_id, handoff.checkpoint],
                    result_type=AgentCheckpoint, **CONTROL,
                )
                assert resumed is not None
                prepared = resumed.prepared
            else:
                prepared = await workflow.execute_activity(
                    "modelmesh_prepare_turn_v1", turn_id, result_type=PreparedTurn,
                    **(PREPARE if workflow.patched(PREPARE_VERSION) else CONTROL),
                )
            if prepared is None:
                raise ValueError("Turn preparation returned no authorized input.")
            limits = await workflow.execute_activity(
                "modelmesh_history_limits_v1", result_type=HistoryLimits, **CONTROL,
            ) if history_handoff else None
            runtime = current()
            conversation = (
                ConversationTypeAdapter.validate_json(resumed.conversation) if resumed else
                ConversationTypeAdapter.validate_json(prepared.conversation)
                if prepared.conversation else Conversation(conversation_id=prepared.scope.conversation_id)
            )
            workspace_value = resumed.workspace if resumed else prepared.workspace
            workspace = (
                TypeAdapter(WorkspaceRef).validate_python(workspace_value)
                if workspace_value else None
            )
            deferred_results = (
                TypeAdapter(DeferredToolResults).validate_python(prepared.deferred_results)
                if prepared.deferred_results and resumed is None else None
            )
            initial_requests = conversation.usage.requests
            checkpoint_ref: str | None = None
            checkpoint_release: DeploymentHandoff | None = None
            async with (
                self.events,
                self.agent_mail_lifetime(),
                public_event_sink(self.events.stream),
                runtime.agent.iter(
                    None if resumed else prompt_content(prepared.prompt, prepared.images),
                    conversation=conversation,
                    run_id=prepared.scope.root_run_id,
                    model=runtime.models[prepared.scope.model],
                    deps=prepared.scope,
                    deferred_tool_results=deferred_results,
                    workspace=workspace,
                    usage_limits=UsageLimits(
                        request_limit=None if unlimited_model_requests() else runtime.settings.model_request_limit,
                    ),
                ) as agent_run,
            ):
                self.agent_run = agent_run
                self.scope = prepared.scope
                if resumed is not None:
                    # These are the pinned SDK's exported graph/context records.
                    # Carry budgets and step IDs instead of granting fresh retries.
                    agent_run.ctx.state.run_step = resumed.run_step
                    agent_run.ctx.state.output_retries_used = resumed.output_retries_used
                    agent_run.ctx.deps.tool_manager.ctx = replace(
                        build_run_context(agent_run.ctx), run_step=-1, retries=resumed.tool_retries,
                    )
                for steering_id in list(self.pending_steering):
                    await self.steer(steering_id)
                for message_id in list(self.pending_agent_mail):
                    await self.agent_message(message_id)
                # This is the public SDK iterator, not another model/tool dispatch loop.
                async for node in agent_run:
                    # This is an SDK graph boundary, not another agent loop.
                    # A running delegation batch settles before this returns.
                    # Completed answers/deferrals still finish their bookkeeping.
                    if (
                        limits is not None and isinstance(node, ModelRequestNode)
                        and agent_run.usage.requests > initial_requests
                        and not agent_run.pending_messages
                        and (self.history_full(limits) or (deployment_handoff and self.deployment_handoff is not None))
                    ):
                        # No model request or tool batch is active here. Freeze
                        # steering before the first await; later signals carry
                        # into the next execution without an enqueue receipt.
                        self.handing_off = True
                        # Poll handlers do not mutate the agent. Keep them live
                        # until persistence succeeds; detaching is irreversible.
                        await workflow.wait_condition(
                            lambda: not self.steered.intersection(self.pending_steering) and not self.mail_handlers,
                        )
                        checkpoint = self.checkpoint(prepared, agent_run, node)
                        if deployment_handoff:
                            checkpoint.deployment_handoff = self.deployment_handoff
                            checkpoint.allow_history_only = self.history_full(limits)
                        try:
                            saved = await workflow.execute_activity(
                                "modelmesh_save_history_checkpoint_v1",
                                checkpoint,
                                result_type=CheckpointSave, **CONTROL,
                            )
                        except ActivityError as error:
                            if (
                                isinstance(error.cause, ApplicationError)
                                and error.cause.type == "ModelMeshHistoryAuthorizationDenied"
                            ):
                                raise
                            workflow.logger.warning(
                                "History checkpoint could not be saved; continuing the live agent.",
                            )
                            saved = CheckpointSave()
                        if saved.reference is not None:
                            checkpoint_ref = saved.reference
                            # Recorded v1 saves lack deployment_allowed. Their
                            # successful reference already passed the v1 gate.
                            checkpoint_release = (
                                checkpoint.deployment_handoff if saved.deployment_allowed is not False else None
                            )
                            self.events.stream.detach_pollers()
                            await workflow.wait_condition(workflow.all_handlers_finished)
                            break
                        # An uncertain receipt or storage outage must not turn
                        # a still-live read/finalization path into a failed turn.
                        self.handing_off = False
                        for steering_id in list(self.pending_steering):
                            await self.steer(steering_id)
                        for message_id in list(self.pending_agent_mail):
                            await self.agent_message(message_id)
                result = agent_run.result
                if result is None and checkpoint_ref is None:
                    raise RuntimeError("The SDK ended without a run result.")
            if checkpoint_ref is not None:
                self.events.stream.detach_pollers()
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(
                    args=[
                        turn_id, HistoryHandoff(
                            checkpoint=checkpoint_ref, stream_state=self.events.stream.get_state(),
                            steered=sorted(self.steered), pending_steering=list(self.pending_steering),
                            pending_agent_mail=list(self.pending_agent_mail),
                            deployment_handoff=checkpoint_release or self.previous_deployment_handoff,
                            # A signal can arrive while the snapshot is being
                            # saved. Carry the latest unconsumed intent after
                            # handlers settle; do not grant it an unchecked
                            # version change on that already-saved snapshot.
                            pending_deployment_handoff=(
                                self.deployment_handoff
                                if self.deployment_handoff != checkpoint_release else None
                            ),
                        ),
                    ],
                    # Ordinary history rollover retains its pinned version.
                    # Only an explicit request may select the deployment's
                    # reviewed current/ramping destination at this boundary.
                    initial_versioning_behavior=(
                        workflow.ContinueAsNewVersioningBehavior.USE_RAMPING_VERSION
                        if checkpoint_release is not None else None
                    ),
                )
            assert result is not None
            self.finished = True
            await workflow.wait_condition(lambda: workflow.all_handlers_finished())
            waiting = isinstance(result.output, DeferredToolRequests)
            reference = (
                TypeAdapter(WorkspaceRef).dump_python(result.workspace.ref, mode="json")
                if result.workspace.ref else None
            )
            accepted = await workflow.execute_activity(
                "modelmesh_save_answer_v1",
                CompletedTurn(
                    scope=prepared.scope,
                    conversation=ConversationTypeAdapter.dump_json(result.conversation).decode(),
                    output="" if waiting else str(result.output),
                    workspace=reference,
                    usage=asdict(result.usage),
                    deferred=waiting,
                ),
                result_type=bool,
                **CONTROL,
            )
            if not accepted:
                await workflow.execute_activity(
                    "modelmesh_stopped_turn_v1",
                    args=[turn_id, ConversationTypeAdapter.dump_json(result.conversation).decode()],
                    **CONTROL,
                )
                return {"turn_id": turn_id, "status": "stopped"}
            if not waiting:
                try:
                    await workflow.execute_activity(
                        "modelmesh_save_computer_v1", args=[turn_id, reference],
                        start_to_close_timeout=timedelta(minutes=3),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                except ActivityError:
                    # Worker loss/timeouts bypass the activity's own error handler.
                    # The answer is already committed; only its settlement failed.
                    await workflow.execute_activity(
                        "modelmesh_settle_answer_v1", turn_id, **CONTROL,
                    )
            return {"turn_id": turn_id, "status": "waiting" if waiting else "completed"}
        except RunCancelled as error:
            self.finished = True
            conversation = ConversationTypeAdapter.dump_json(Conversation(
                messages=error.all_messages(), usage=error.usage,
                conversation_id=prepared.scope.conversation_id if prepared else turn_id,
            )).decode()
            await workflow.execute_activity(
                "modelmesh_stopped_turn_v1", args=[turn_id, conversation], **CONTROL
            )
            return {"turn_id": turn_id, "status": "stopped"}
        except asyncio.CancelledError as error:
            self.finished = True
            cancelled = RunCancelled.from_cancellation(error)
            conversation = None
            if cancelled is not None and prepared is not None:
                conversation = ConversationTypeAdapter.dump_json(Conversation(
                    messages=cancelled.all_messages(), usage=cancelled.usage,
                    conversation_id=prepared.scope.conversation_id,
                )).decode()
            await asyncio.shield(workflow.execute_activity(
                "modelmesh_stopped_turn_v1", args=[turn_id, conversation], **CONTROL
            ))
            # Temporal remains the cancellation and recovery authority.
            raise
        except Exception as error:
            self.finished = True
            message = (
                "The conversation's computer is no longer available. "
                "Your saved answers and downloads are preserved."
            ) if isinstance(error, WorkspaceUnavailableError) else (
                "The agent could not complete this turn. "
                "Inspect the saved activity before repeating a possibly completed action."
            )
            if prepared is not None and workflow.patched("modelmesh-failure-workspace-checkpoint-v1"):
                # Save the failed turn's conversation before failure settlement clears
                # active ownership. A save failure must never prevent the error settling.
                with suppress(ActivityError):
                    await workflow.execute_activity(
                        "modelmesh_preserve_failed_workspace_v1", turn_id,
                        start_to_close_timeout=timedelta(minutes=3),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
            await workflow.execute_activity(
                "modelmesh_fail_turn_v1",
                args=[turn_id, message, capture_failure(error)] if workflow.patched(FAILURE_VERSION)
                else [turn_id, message],
                **CONTROL,
            )
            if isinstance(error, ApplicationError):
                raise
            raise ApplicationError(
                "The agent turn failed; inspect its recorded activity before retrying.",
                type=type(error).__name__, non_retryable=True,
            ) from error

    @staticmethod
    def history_full(limits: HistoryLimits) -> bool:
        info = workflow.info()
        return (
            info.is_continue_as_new_suggested()
            or info.get_current_history_size() >= limits.max_bytes
            or info.get_current_history_length() >= limits.max_events
        )

    @staticmethod
    def checkpoint(
        prepared: PreparedTurn, run: AgentRun[RunScope, str | DeferredToolRequests],
        node: ModelRequestNode[RunScope, str | DeferredToolRequests],
    ) -> AgentCheckpoint:
        # The next request holds the normalized results of the settled batch;
        # it has not been appended to all_messages() yet.
        messages = [*run.all_messages(), node.request]
        if prepared.scope.agent_messages:
            # The SDK can emit the final-drain event just before this graph
            # boundary. Its message is already in this snapshot even if the
            # event consumer has not yet acknowledged it.
            _, completed = receipts(messages)
            prepared = replace(prepared, scope=replace(
                prepared.scope, background_agents=[
                    child for child in prepared.scope.background_agents if child not in completed
                ],
            ))
        manager = run.ctx.deps.tool_manager
        retries = {
            name: count for name, count in (manager.ctx.retries if manager.ctx else {}).items()
            if name not in manager.succeeded_tools
        }
        for name in manager.failed_tools:
            retries[name] = (manager.ctx.retries.get(name, 0) if manager.ctx else 0) + 1
        workspace = run.ctx.deps.workspace
        reference = workspace.ref if workspace is not None else None
        return AgentCheckpoint(
            prepared=replace(prepared, prompt="", images=[], conversation=None, deferred_results=None),
            conversation=ConversationTypeAdapter.dump_json(Conversation(
                messages=messages, usage=run.usage, conversation_id=run.conversation_id,
            )).decode(),
            workspace=(
                TypeAdapter(WorkspaceRef).dump_python(reference, mode="json") if reference else None
            ),
            run_step=run.ctx.state.run_step,
            output_retries_used=run.ctx.state.output_retries_used,
            tool_retries=retries,
        )

