"""Fence possibly completed external writes without replacing the SDK's tool ledger."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.capabilities.abstract import WrapToolExecuteHandler
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from .contracts import RunScope
from .database import Database
from .effect_gate import EFFECT_GATE_VERSION, workflow_effect_lock
from .models import Effect, now

logger = logging.getLogger(__name__)

COMPUTER_EFFECTS = {"shell", "run_command", "start_command"}
SKILL_EFFECTS = {"skill_create", "skill_update", "skill_delete"}
AUTOMATION_EFFECTS = {
    "automation_create", "automation_update", "automation_migrate",
    "automation_enable", "automation_pause", "automation_retire",
    "automation_grant", "automation_self_update", "automation_claim_item",
    "automation_webhook_setup",
}
SCOPED_AUTOMATION_EFFECTS = {
    "automation_grant", "automation_self_update", "automation_claim_item", "automation_webhook_setup",
}
SHELL_RECOVERY_VERSION = "modelmesh-shell-readiness-v1"
SHELL_READINESS_SUPPORTED = "modelmesh_shell_readiness_supported"


class PendingEffectError(PermissionError):
    """A new call was refused before dispatch; the earlier receipt stays pending."""


def requires_receipt(tool: ToolDefinition) -> bool:
    if tool.name in SKILL_EFFECTS:
        return not workflow.in_workflow() or workflow.patched("modelmesh-skill-receipts-v1")
    if tool.name in SCOPED_AUTOMATION_EFFECTS:
        return not workflow.in_workflow() or workflow.patched("modelmesh-scoped-automation-receipts-v1")
    return tool.name in COMPUTER_EFFECTS | AUTOMATION_EFFECTS


def effect_id(ctx: RunContext[RunScope], call: ToolCallPart) -> str:
    return hashlib.sha256(f"{ctx.deps.turn_id}:{ctx.run_id}:{call.tool_call_id}".encode()).hexdigest()


async def confirm_effect(database: Database, identity: str) -> None:
    """Persist received evidence without making another provider call."""
    async with database.sessions.begin() as db:
        await db.execute(update(Effect).where(
            # A late authoritative receipt can still confirm the original
            # attempt after its turn failed; this never dispatches it again.
            Effect.id == identity, Effect.state.in_(("pending", "failed")),
        ).values(state="confirmed", settled_at=now()))


class EffectReceipts(AbstractCapability[RunScope]):
    id = "modelmesh_effects"

    def __init__(self, database: Database):
        self.database = database

    @durable_operation("reserve")
    async def reserve(
        self, scope: RunScope, identity: str, name: str, args: dict[str, Any],
        shell_readiness: bool = False,
    ) -> None:
        await self.database.authorize_scope(scope)
        serialized = json.dumps(args, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(f"{name}:{serialized}".encode()).hexdigest()
        try:
            async with self.database.sessions.begin() as db:
                previous = await db.get(Effect, identity)
                if previous:
                    if previous.fingerprint != fingerprint:
                        raise ValueError("An effect identity cannot change its operation.")
                    return
                if await db.scalar(select(Effect.id).where(
                    Effect.session_id == scope.session_id, Effect.state == "pending"
                )):
                    raise PendingEffectError("An earlier action needs reconciliation before another write.")
                db.add(Effect(
                    id=identity, session_id=scope.session_id, turn_id=scope.turn_id,
                    actor_id=scope.actor_id, tool_name=name,
                    arguments=self.database.security.seal(serialized), fingerprint=fingerprint,
                    resolution=(
                        self.database.security.seal({"shell_phase": "reserved"})
                        if name == "shell" and shell_readiness else None
                    ),
                ))
        except IntegrityError as error:
            raise PendingEffectError("Another action is awaiting a confirmed outcome.") from error

    @durable_operation("confirm")
    async def confirm(self, identity: str) -> None:
        # A received result is factual evidence even if Stop/revocation arrived meanwhile.
        await confirm_effect(self.database, identity)

    async def prepare_tools(
        self, ctx: RunContext[RunScope], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return [
            replace(
                tool, sequential=True,
                metadata={**(tool.metadata or {}), SHELL_RECOVERY_VERSION: True},
            ) if tool.name == "shell" and (tool.metadata or {}).get(SHELL_READINESS_SUPPORTED) and (
                not workflow.in_workflow() or workflow.patched(SHELL_RECOVERY_VERSION)
            ) else replace(tool, sequential=True) if requires_receipt(tool) else tool
            for tool in tool_defs
        ]

    async def wrap_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any], handler: WrapToolExecuteHandler,
    ) -> Any:
        if (
            not workflow.in_workflow()
            or not requires_receipt(tool_def)
            or not workflow.patched(EFFECT_GATE_VERSION)
        ):
            return await handler(args)
        # The SDK handler includes reservation, dispatch and confirmation. Only
        # guarded effects wait here: models, reads and delegate_task stay parallel.
        # Cancelling a waiter creates no receipt. Cancelling an in-flight effect
        # leaves its existing uncertainty fence intact, never authorizing replay.
        async with workflow_effect_lock(ctx.deps.session_id):
            return await handler(args)

    async def before_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any],
    ) -> dict[str, Any]:
        if requires_receipt(tool_def):
            # A deferred native tool has not dispatched yet. ApprovalRequired is
            # enforced by the SDK before this hook once the tool is marked below.
            identity = effect_id(ctx, call)
            try:
                if tool_def.name == "shell" and (tool_def.metadata or {}).get(SHELL_RECOVERY_VERSION):
                    await self.reserve(ctx.deps, identity, call.tool_name, args, shell_readiness=True)
                else:
                    await self.reserve(ctx.deps, identity, call.tool_name, args)
            except Exception as error:
                cause = error.cause if isinstance(error, ActivityError) else error
                if isinstance(cause, PendingEffectError) or (
                    isinstance(cause, ApplicationError) and cause.type == "PendingEffectError"
                ):
                    raise ToolFailed(
                        "This action did not run. An earlier action has an unconfirmed outcome. "
                        "Use available read tools to inspect it, then report the failure and end this turn. "
                        "Do not repeat the earlier action. The user can send another message to continue."
                    ) from error
                raise
        return args

    async def after_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any], result: Any,
    ) -> Any:
        if requires_receipt(tool_def):
            await self.confirm(effect_id(ctx, call))
        return result

    async def on_tool_execute_error(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any], error: Exception,
    ) -> Any:
        cause = error.cause if isinstance(error, ActivityError) else None
        if (
            requires_receipt(tool_def) and isinstance(cause, ApplicationError)
            and cause.type in {"ModelMeshGuidanceChanged", "ModelMeshPermissionDenied", "ModelMeshMissingExecutionScope"}
        ):
            # Our inbound interceptor refused before calling the tool body. This
            # outcome is known, so it must not leave a false uncertainty barrier.
            await self.confirm(effect_id(ctx, call))
        raise error
