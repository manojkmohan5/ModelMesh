"""Response models for the routes defined in api.py.

These are the source of the generated frontend types (native/web/generated/api).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import MISSING, BaseModel, ConfigDict, Field, JsonValue, RootModel, computed_field
from pydantic_ai.messages import ToolCallPart
from pydantic_ai_harness.ask_user import AskUserRequest

from .general_preferences import GeneralPreferences


def _missing(value: object) -> bool:
    return value is MISSING


def absent() -> Any:
    """A response key that some rows omit: optional in OpenAPI, omitted (not null) on the wire."""
    return Field(default=MISSING, exclude_if=_missing)


class MeUser(BaseModel):
    id: str
    name: str
    role: str


class Me(BaseModel):
    user: MeUser
    csrf: str
    models: list[str]
    default_model: str
    # The model id that asks the router to pick a model per message; null when smart routing is unavailable.
    auto_model: str | None
    preferences: GeneralPreferences


class ApiView(BaseModel):
    """Response payload: fields with defaults are still always present in output."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class HealthStatus(ApiView):
    status: Literal["ok"]
    engine: str
    durability: str


class AuthConfig(ApiView):
    password_enabled: bool


class LoginResult(ApiView):
    csrf: str


class LogoutResult(ApiView):
    signed_out: bool


class InvocableSkill(ApiView):
    id: str
    name: str
    description: str
    scope: str
    reference: str
    icon: str


class InvocableSkillPage(ApiView):
    skills: list[InvocableSkill]
    has_more: bool
    next_offset: int | None


class SessionSummary(ApiView):
    id: str
    title: str
    owner_id: str
    visibility: str
    surface: str
    model: str
    status: str
    active_turn: str | None
    archived: bool
    deleted: bool
    # A pydantic-ai WorkspaceRef; opaque to the web app beyond presence.
    workspace: dict[str, JsonValue] | None
    save_warning: str | None
    updated_at: str


class SessionListItem(SessionSummary):
    can_manage: bool
    pinned: bool
    folder: str
    participated: bool
    unread: bool


class SessionDetailSession(SessionSummary):
    can_manage: bool
    pinned: bool
    folder: str
    unread: bool
    swarm_id: str | None = None
    parent_session_id: str | None = None


class RoutingAlternative(ApiView):
    model: str
    score: float | None
    # Benchmark medians for the task type; absent from decisions stored before they were recorded.
    latency: float | None = None  # Seconds per answer.
    cost: float | None = None  # Dollars per answer.


class RoutingDecision(ApiView):
    """Why Auto routing chose this turn's model, shown in the pop-up and the answer's tag."""

    model: str
    engine: Literal["rule", "laya", "llm", "follow-up", "running-turn", "fallback"]
    task_type: str | None
    confidence: float | None
    reason: str
    score: float | None
    rank: int | None
    of: int | None
    benchmark: str | None
    alternatives: list[RoutingAlternative]
    router_ms: int | None
    latency: float | None = None
    cost: float | None = None


class TurnView(ApiView):
    id: str
    session_id: str
    actor_id: str
    model: str
    status: str
    mode: str
    parent_turn: str | None
    enqueue_id: str | None
    accepted_at: str | None
    # pydantic-ai RunUsage counters.
    usage: dict[str, JsonValue]
    error: str | None
    created_at: str
    finished_at: str | None
    # Set when Auto routing chose the model; null for a model the user picked.
    routing: RoutingDecision | None = None


class GoalView(ApiView):
    elapsed_seconds: float
    objective: str
    status: Literal["active", "paused", "complete", "blocked", "budget_limited"]
    owner_id: str
    generation: int
    tokens_used: int
    token_budget: int | None
    turns: int
    blocked_count: int
    reason: str
    created_at: str


class GoalCommandResult(ApiView):
    goal: GoalView | None
    help: str | None = None


class GoalStartedResult(TurnView):
    goal: GoalView


class SessionTurn(TurnView):
    guidance_at: str | None
    is_self: bool


class MessageAttachment(ApiView):
    id: str
    name: str
    media_type: str
    size: int


class SessionMessage(ApiView):
    sent_as_goal: bool = False
    id: int
    turn_id: str
    role: str
    author_id: str | None
    author_name: str | None
    is_self: bool
    content: str
    created_at: str
    attachments: list[MessageAttachment]
    source: Literal["delegation"] | None = None


class SessionEffect(ApiView):
    id: str
    turn_id: str
    tool_name: str
    arguments: JsonValue


class PendingEffect(SessionEffect):
    created_at: str


class PendingToolCall(ApiView):
    """A pydantic-ai ToolCallPart awaiting an answer or approval."""

    tool_name: str
    args: str | dict[str, JsonValue] | None
    tool_call_id: str
    tool_kind: str | None = None
    id: str | None = None
    provider_name: str | None = None
    provider_details: dict[str, JsonValue] | None = None
    part_kind: str


class PendingRequests(ApiView):
    calls: list[PendingToolCall]
    approvals: list[PendingToolCall]
    # Per tool_call_id metadata; its shape depends on the tool (e.g. an optional question).
    metadata: dict[str, dict[str, JsonValue]]

    @computed_field
    @property
    def questions(self) -> list[AskUserRequest]:
        """Use the same validated questions for presentation and answer checking."""
        return [
            AskUserRequest.from_tool_call(ToolCallPart(
                call.tool_name, call.args, tool_call_id=call.tool_call_id,
            ))
            for call in self.calls if call.tool_name == "ask_user_question"
        ]


class ToolCallActivity(ApiView):
    type: Literal["tool_call"]
    id: str
    name: str
    arguments: str | dict[str, JsonValue] | None


class ToolResultActivity(ApiView):
    type: Literal["tool_result"]
    id: str
    name: str
    content: str | None
    failed: bool


SavedActivity = Annotated[ToolCallActivity | ToolResultActivity, Field(discriminator="type")]


class ResponseStartEvent(ApiView):
    type: Literal["response_start"]
    run_id: str
    step: int


class TextEvent(ApiView):
    type: Literal["text_start", "text_delta", "text_end"]
    index: int
    text: str
    phase: Literal["commentary", "final_answer"] | None


class AgentFinishedEvent(ApiView):
    type: Literal["agent_finished"]
    run_id: str


class CapabilityUpdateEvent(ApiView):
    type: Literal["capability"]
    name: str
    # asdict() of a public pydantic-ai capability update; shapes vary by capability.
    data: dict[str, Any]


class LiveEvent(RootModel[Annotated[
    ResponseStartEvent | TextEvent | ToolCallActivity | ToolResultActivity | AgentFinishedEvent | CapabilityUpdateEvent,
    Field(discriminator="type"),
]]):
    """One `data:` frame of GET /api/sessions/{id}/events (see presentation.public_event)."""


class SessionDetail(ApiView):
    goal: GoalView | None = None
    session: SessionDetailSession
    turns: list[SessionTurn]
    has_unsettled_effects: bool
    effects: list[SessionEffect]
    transcript_version: str | None = None
    activity: list[SavedActivity] | None = None
    can_answer: bool | None = None
    pending: PendingRequests | None = None
    can_reply_with_text: bool | None = None
    question_deadline: str | None = None
    messages: list[SessionMessage] | None = None


class WorkCommentary(ApiView):
    id: str
    text: str
    phase: Literal["commentary"]
    created_at: str


class WorkProgressMessage(ApiView):
    id: str
    text: str
    phase: Literal["commentary", "final_answer"]
    created_at: str


class WorkToolProgress(ApiView):
    id: str
    name: str
    status: Literal["running", "completed", "failed", "interrupted"]
    created_at: str
    finished_at: str | None


class WorkAgentProgress(ApiView):
    id: str
    session_id: str | None
    parent_id: str | None
    parent_name: str | None
    name: str
    avatar: int
    depth: int
    status: str
    created_at: str
    updated_at: str
    messages: list[WorkProgressMessage]
    tools: list[WorkToolProgress]
    messages_limited: bool
    tools_limited: bool


class TurnWorkProgress(ApiView):
    messages: list[WorkCommentary]
    # Present only with include_activity=true.
    tools: list[WorkToolProgress] | None = None
    subagents: list[WorkAgentProgress] | None = None
    tools_limited: bool | None = None
    subagents_limited: bool | None = None


class WorkActionDetail(WorkToolProgress):
    input: str | None
    output: str | None
    restricted: bool
    input_truncated: bool
    output_truncated: bool


class FailureCauseView(ApiView):
    type: str
    application_type: str | MISSING = absent()
    activity_type: str | MISSING = absent()
    activity_id: str | MISSING = absent()


class FailureRuntimeView(ApiView):
    exception_chain: list[FailureCauseView]


class FailureActionView(ApiView):
    id: str
    tool: str
    timestamp: str | None
    status: Literal["completed", "failed", "interrupted"]


class FailureToolErrorView(FailureActionView):
    error: str | None
    restricted: bool
    truncated: bool


class FailureUsageView(ApiView):
    requests: int | float | MISSING = absent()
    input_tokens: int | float | MISSING = absent()
    output_tokens: int | float | MISSING = absent()


class TurnFailureDetails(ApiView):
    session_id: str
    turn_id: str
    workflow_id: str
    # Uses the same runtime state vocabulary as TurnView.
    status: str
    model: str
    summary: str
    created_at: str
    finished_at: str | None
    runtime: FailureRuntimeView | None
    notes: list[str]
    last_action: FailureActionView | None
    recent_failures: list[FailureToolErrorView]
    failures_limited: bool
    usage: FailureUsageView


class SessionUpdateResult(ApiView):
    updated: bool


class SessionDeleteResult(ApiView):
    deleted: bool


class SessionRestoreResult(ApiView):
    restored: bool


class QueuedTurnUpdateResult(ApiView):
    updated: bool


class QueuedTurnCancelResult(ApiView):
    cancelled: bool


class SessionStopResult(ApiView):
    status: Literal["stopping"]


class EffectResolveResult(ApiView):
    recorded: bool


class SessionFile(ApiView):
    id: str
    name: str
    media_type: str
    size: int
    created_at: str


class PlanStep(ApiView):
    id: str
    content: str
    status: Literal["pending", "in_progress", "completed", "cancelled", "blocked"]
    active_form: str
    parent_id: str | None
    depends_on: list[str]
