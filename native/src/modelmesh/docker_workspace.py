"""Pydantic workspace capability for the agent's computer: a local Docker container.

For development on one machine: it needs the docker CLI and the host's Docker socket in the
worker, and isolation is only that of a container. Each conversation's workspace is one
long-lived container named modelmesh-ws-<id> and labelled modelmesh.workspace=1. ModelMesh
never removes them: delete one with `docker rm -f <name>` once its files are no longer needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping
from uuid import uuid4

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import (
    CommandResult,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from pydantic_ai.workspaces.protocol import validate_timeout

from .contracts import RunScope
from .settings import Settings

WORKDIR = "/workspace"
# One runaway job (a big build, a leak, a fork bomb) must not starve Laya, the gateway or the app,
# which share Docker's VM: cap memory, CPU and processes, and make the VM's out-of-memory killer pick
# a computer's processes first. No raw sockets, and no privilege gain through setuid binaries.
# ponytail: fixed limits for every computer; make them settings if a task ever needs more.
LIMITS = ("--memory", "2g", "--memory-swap", "2g", "--cpus", "2", "--pids-limit", "512", "--oom-score-adj", "500",
          "--cap-drop", "NET_RAW", "--security-opt", "no-new-privileges")
OUTPUT_LIMIT = 32 * 1024 * 1024
# Runs inside the container. Output goes to spool files, so a detached child that keeps the
# output streams open cannot hold up a finished command; `timeout` signals the whole process
# group and ($$ is timeout's pid after exec) STOP_SCRIPT can do the same on cancellation.
RUN_SCRIPT = (
    'echo $$ > /tmp/.mm-$1.pid; id=$1; limit=$2; shift 2; '
    'exec timeout -k 2 "$limit" "$@" </dev/null >/tmp/.mm-$id.out 2>/tmp/.mm-$id.err'
)
READ_SCRIPT = 'head -c "$2" /tmp/.mm-$1.out; head -c "$2" /tmp/.mm-$1.err >&2; rm -f /tmp/.mm-$1.*'
STOP_SCRIPT = 'kill -TERM "$(cat /tmp/.mm-$1.pid)"'


async def _docker(*args: str) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await process.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    assert process.returncode is not None
    return process.returncode, out, err


def _failure(name: str, err: bytes) -> Exception:
    text = err.decode("utf-8", "replace").strip()
    if "no such" in text.lower():  # "No such object" (older CLIs) or "no such object" (v29).
        return WorkspaceUnavailableError(f"Docker computer {name} no longer exists")
    return RuntimeError(f"Docker failed for {name}: {text}")  # Transient: let durable engines retry.


async def _require_running(name: str) -> None:
    code, out, err = await _docker("inspect", "-f", "{{.State.Running}}", name)
    if code:
        raise _failure(name, err)
    if out.strip() != b"true":
        raise WorkspaceUnavailableError(f"Docker computer {name} is not running")


class DockerSandbox(AbstractCapability[RunScope]):
    def __init__(self, settings: Settings):
        self.settings = settings

    def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None) -> WorkspaceBackend | None:
        if ref is not None and ref.provider != "docker":
            return None
        return DockerBackend(self.settings, ref=ref, key=ctx.deps.workspace_key)

    async def destroy(self, ref: WorkspaceRef) -> None:
        if ref.provider != "docker":
            raise ValueError("Expected a Docker workspace")
        await _docker("rm", "-f", ref.id)  # Already gone is fine.


class DockerBackend:
    """Commands supply the SDK's built-in filesystem fallback inside the same container."""

    def __init__(self, settings: Settings, *, ref: WorkspaceRef | None = None, key: str = ""):
        if ref is not None and ref.provider != "docker":
            raise ValueError("Expected a Docker workspace")
        self.settings, self.key = settings, key
        self._ref = ref
        self._ready = False
        self.lock = asyncio.Lock()

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    async def _create(self) -> None:
        image = self.settings.docker_image
        if (await _docker("image", "inspect", image))[0]:
            raise RuntimeError(
                f"Docker computer image {image!r} not found. "
                f"Build it from the repository root: docker build -t {image} -f workspace.Dockerfile ."
            )
        name = "modelmesh-ws-" + uuid4().hex[:16]
        code, _, err = await _docker(
            "run", "-d", "--init", "--name", name, "-w", WORKDIR, *LIMITS,
            "--label", "modelmesh.workspace=1", "--label", f"modelmesh.key={self.key}",
            image, "tail", "-f", "/dev/null",
        )
        if code:
            raise RuntimeError(f"Could not start the Docker computer: {err.decode('utf-8', 'replace').strip()}")
        self._ref = WorkspaceRef(provider="docker", id=name)


    async def _get(self) -> str:
        async with self.lock:
            if not self._ready:
                if self._ref is None:
                    task = asyncio.create_task(self._create())
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # Finish recording the ref even when cancellation races the create.
                        # Application lifecycle code owns the container; a run never removes it.
                        with contextlib.suppress(Exception):
                            await task
                        raise
                else:
                    code, out, err = await _docker("inspect", "-f", "{{.State.Running}}", self._ref.id)
                    if code:
                        raise _failure(self._ref.id, err)
                    if out.strip() != b"true":  # e.g. Docker restarted; the filesystem survives.
                        code, _, err = await _docker("start", self._ref.id)
                        if code:
                            raise _failure(self._ref.id, err)
                self._ready = True
            assert self._ref is not None
            return self._ref.id

    async def working_dir(self) -> str:
        await self._get()
        return WORKDIR

    async def run(
        self, command: WorkspaceCommand, *, shell: bool = False,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
    ) -> CommandResult:
        validate_timeout(timeout)
        if isinstance(command, str):
            if not shell:
                raise TypeError("String commands require shell=True")
            argv = ["/bin/sh", "-c", command]
        else:
            if shell:
                raise TypeError("Argument sequences require shell=False")
            argv = list(command)
            if not argv or any(not isinstance(arg, str) for arg in argv):
                raise ValueError("Command must be a nonempty string sequence")
        name = await self._get()
        job = uuid4().hex
        flags = [flag for key, value in (env or {}).items() for flag in ("-e", f"{key}={value}")]
        started = time.monotonic()
        try:
            code, _, err = await _docker(
                "exec", "-w", WORKDIR, *flags, name, "sh", "-c", RUN_SCRIPT, "sh", job, str(timeout or 0), *argv,
            )
        except BaseException:
            # `docker exec` cannot be cancelled from the client, so signal the command itself.
            # ponytail: only the foreground tree is stopped; a detached child keeps running.
            with contextlib.suppress(Exception):
                await asyncio.shield(_docker("exec", name, "sh", "-c", STOP_SCRIPT, "sh", job))
            raise
        elapsed = time.monotonic() - started
        if err.strip():  # The command's own stderr is spooled, so this is docker or sh failing.
            await _require_running(name)
            raise RuntimeError(f"Docker exec failed for {name}: {err.decode('utf-8', 'replace').strip()}")
        code2, out, err2 = await _docker("exec", name, "sh", "-c", READ_SCRIPT, "sh", job, str(OUTPUT_LIMIT + 1))
        if code2:
            await _require_running(name)
            raise RuntimeError(f"Docker could not read command output for {name}: {err2.decode('utf-8', 'replace').strip()}")
        text = {"stdout": out.decode("utf-8", "replace"), "stderr": err2.decode("utf-8", "replace")}
        if len(out) > OUTPUT_LIMIT or len(err2) > OUTPUT_LIMIT:
            raise WorkspaceOutputLimitError("Command output exceeds 32 MiB", limit=OUTPUT_LIMIT, **text)
        if timeout is not None and code in (124, 137) and elapsed >= timeout:  # timeout(1): 124, or 137 after -k.
            raise WorkspaceTimeoutError("Docker command timed out", **text)
        return CommandResult(exit_code=code, **text)
