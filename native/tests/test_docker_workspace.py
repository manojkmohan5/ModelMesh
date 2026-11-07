"""The local Docker computer against the SDK's workspace conformance suite.

Needs a Docker daemon and the computer image:
    docker build -t modelmesh-workspace -f workspace.Dockerfile .
"""
from __future__ import annotations

import shutil
import subprocess

import pytest
from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from modelmesh.docker_workspace import DockerBackend, DockerSandbox
from modelmesh.settings import Settings

SETTINGS = Settings(_env_file=None)


def _ready() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "image", "inspect", SETTINGS.docker_image], check=False, capture_output=True,
    ).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the modelmesh-workspace image")


class TestDockerBackend(WorkspaceBackendSuite):
    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"

    @pytest.fixture
    async def backend(self):
        backend = DockerBackend(SETTINGS)
        try:
            yield backend
        finally:
            if backend.ref is not None:
                await DockerSandbox(SETTINGS).destroy(backend.ref)

    @pytest.fixture
    def fresh_backend(self):
        return lambda: DockerBackend(SETTINGS)

    @pytest.fixture
    def attach_backend(self):
        return lambda ref: DockerBackend(SETTINGS, ref=ref)

    @pytest.fixture
    def destroy_environment(self):
        async def destroy(backend):
            await DockerSandbox(SETTINGS).destroy(backend.ref)
        return destroy

    async def test_missing_container_is_unavailable_not_recreated(self):
        ref = WorkspaceRef(provider="docker", id="modelmesh-ws-does-not-exist")
        backend = DockerBackend(SETTINGS, ref=ref)
        with pytest.raises(WorkspaceUnavailableError):
            await backend.working_dir()
        assert backend.ref == ref
