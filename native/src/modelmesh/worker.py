"""Native Temporal worker and idempotent application-to-workflow dispatch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import (
    Interceptor,
    Worker,
    WorkerDeploymentConfig,
)

from .activities import ACTIVITIES
from .agent_messages import MailDispatch, dispatch_mail
from .execution import ExecutionAuthority
from .models import Turn
from .runtime import Runtime, configure
from .settings_runtime import SETTINGS_ACTIVITIES, SETTINGS_WORKFLOWS, settings_worker_identity
from .swarm_runtime import SWARM_ACTIVITIES, SwarmWorkflow, dispatch_missions
from .worker_capacity import ActivityCapacity, WorkerPools
from .workflow import TurnWorkflow

logger = logging.getLogger(__name__)
# Saved turns are the queue; the idle dispatcher checks them this often.
DISPATCH_POLL_SECONDS = 0.5


def make_worker(runtime: Runtime) -> WorkerPools:
    if runtime.client is None:
        raise RuntimeError("Connect to Temporal before creating the worker.")
    configure(runtime)
    TurnWorkflow.__pydantic_ai_agents__ = [runtime.agent]
    interceptors: list[Interceptor] = [ExecutionAuthority(runtime.database), ActivityCapacity()]
    agent_plugins = [AgentPlugin(runtime.agent)]
    identity = settings_worker_identity()
    activities = [*ACTIVITIES, *SETTINGS_ACTIVITIES, *SWARM_ACTIVITIES]
    activity_pools = ("control", "model", "tools")
    # WorkerPools starts all poller shutdowns concurrently, so each active
    # operation gets the shared process budget without multiplying that budget.
    shutdown_timeout = timedelta(seconds=runtime.settings.worker_shutdown_seconds)
    deployment = (
        WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=runtime.settings.worker_deployment,
                build_id=runtime.settings.worker_build_id,
            ),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        )
        if runtime.settings.worker_deployment else None
    )
    legacy = Worker(
        runtime.client,
        identity=identity,
        task_queue=runtime.settings.temporal_queue,
        workflows=[TurnWorkflow, SwarmWorkflow, *SETTINGS_WORKFLOWS],
        activities=activities,
        interceptors=interceptors,
        max_concurrent_activities=runtime.settings.temporal_worker_activities,
        max_cached_workflows=runtime.settings.temporal_workflow_cache_size,
        graceful_shutdown_timeout=shutdown_timeout,
        deployment_config=deployment,
    )
    pools = tuple(Worker(
        runtime.client, identity=identity,
        task_queue=f"{runtime.settings.temporal_queue}-{pool}",
        activities=activities, plugins=agent_plugins, interceptors=interceptors,
        max_concurrent_activities=runtime.settings.temporal_worker_activities,
        max_cached_workflows=runtime.settings.temporal_workflow_cache_size,
        graceful_shutdown_timeout=shutdown_timeout,
        deployment_config=deployment,
    ) for pool in activity_pools)
    return WorkerPools((legacy, *pools))


@dataclass
class DispatchState:
    starting_after: tuple[str, str] | None = None
    starting_through: tuple[str, str] | None = None
    backlog: bool = False
    mail: MailDispatch = field(default_factory=MailDispatch)


async def dispatch_once(runtime: Runtime, state: DispatchState | None = None) -> int:
    state = state if state is not None else DispatchState()
    state.backlog = False
    client = await runtime.connect()
    batch_size = runtime.settings.temporal_dispatch_batch_size
    try:
        async with asyncio.timeout(2):
            await runtime.database.goals.reconcile()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Goal continuation remains pending.", exc_info=True)
    if state.starting_through is None:
        # Freeze each scan round before claiming more work. Later arrivals
        # cannot extend it indefinitely and starve retries near the beginning.
        state.starting_through = await runtime.database.dispatch_boundary()
    claimed = await runtime.database.claim_batch(batch_size)
    starting = await runtime.database.starting_turns(
        limit=batch_size, after=state.starting_after, through=state.starting_through,
    )
    slots = asyncio.Semaphore(runtime.settings.temporal_dispatch_concurrency)

    async def start(turn: Turn) -> bool:
        async with slots:
            try:
                async with asyncio.timeout(10):
                    await client.start_workflow(
                        TurnWorkflow.run, turn.id, id=turn.workflow_id,
                        task_queue=runtime.settings.temporal_queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
            except WorkflowAlreadyStartedError:
                # The deterministic ID also reconciles a lost acknowledgment.
                pass
            except (RPCError, TimeoutError):
                logger.warning("Temporal did not acknowledge dispatch; the saved request will be reconciled.")
                return False
            return True

    results = await asyncio.gather(*(start(turn) for turn in starting))
    dispatched = sum(results)
    full_page = len(starting) == batch_size
    later = (
        bool(await runtime.database.starting_turns(limit=1, after=state.starting_through))
        if not full_page and state.starting_through is not None else False
    )
    selected = {turn.id for turn in starting}
    newly_claimed_outside_page = any(turn.id not in selected for turn in claimed)
    if full_page:
        state.starting_after = starting[-1].created_at, starting[-1].id
    else:
        state.starting_after = state.starting_through = None
    state.backlog = all(results) and (
        len(claimed) == batch_size or full_page or later or newly_claimed_outside_page
    )
    for turn, parent in await runtime.database.pending_steering():
        if parent.status not in {"starting", "running"}:
            if turn.status == "steering":
                await runtime.database.steering_receipt(turn.id, None)
            else:
                await runtime.database.steering_uncertain(turn.id)
            continue
        # Persist intent before the RPC. If its acknowledgment is lost and the
        # parent closes, absence of an enqueue receipt is not permission to rerun.
        await runtime.database.signal_attempted(turn.id)
        try:
            await client.get_workflow_handle(parent.workflow_id).signal(TurnWorkflow.steer, turn.id)
        except RPCError:
            continue
    for turn in await runtime.database.pending_stops():
        handle = client.get_workflow_handle(turn.workflow_id)
        try:
            description = await handle.describe()
            if description.status == WorkflowExecutionStatus.RUNNING:
                await handle.cancel()
            else:
                await runtime.database.stopped(turn.id)
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                # A Stop admitted before workflow creation never needs to start the run.
                await runtime.database.stopped(turn.id)
    try:
        async with asyncio.timeout(3):
            await dispatch_mail(runtime, state.mail)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Agent message delivery remains pending.", exc_info=True)
    try:
        async with asyncio.timeout(3):
            await dispatch_missions(runtime)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Swarm coordination dispatch remains pending.", exc_info=True)
    return dispatched


async def dispatcher(runtime: Runtime, *, stop: asyncio.Event | None = None) -> None:
    state = DispatchState()
    question_cursor = 0
    while stop is None or not stop.is_set():
        try:
            from .questions import expire_optional_questions
            question_cursor = await expire_optional_questions(runtime.database, after=question_cursor)
            await dispatch_once(runtime, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            state.backlog = False
            logger.exception("Dispatch is temporarily unavailable; saved inputs remain queued.")
        # With a backlog, only yield to HTTP/owner monitoring, without a polling delay.
        await asyncio.sleep(0 if state.backlog else DISPATCH_POLL_SECONDS)


async def reconcile_workflows(runtime: Runtime, *, stop: asyncio.Event | None = None) -> None:
    """Reflect terminal Temporal outcomes after an app write/ack failed.

    This never resumes a snapshot or starts an agent; Temporal stays the sole
    execution authority. It only settles stale application status records.
    """
    from .background_stop import pause

    while stop is None or not stop.is_set():
        try:
            client = await runtime.connect()
            for turn in await runtime.database.unsettled_turns():
                try:
                    description = await client.get_workflow_handle(turn.workflow_id).describe()
                except RPCError:
                    continue
                if description.status == WorkflowExecutionStatus.CANCELED:
                    await runtime.database.stopped(turn.id)
                elif description.status in {
                    WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.TIMED_OUT,
                    WorkflowExecutionStatus.TERMINATED, WorkflowExecutionStatus.COMPLETED,
                }:
                    await runtime.database.fail(
                        turn.id,
                        "Execution ended before the application could confirm settlement. "
                        "Inspect the saved activity before repeating an action.",
                        only_unsettled=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Execution status reconciliation is temporarily unavailable.")
        await pause(5, stop)

