"""Trusted process resources. Workflows call only the registered native agent."""

from __future__ import annotations

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from pydantic_ai.models import Model
from temporalio.client import Client

from .agent import inference_models, make_agent
from .contracts import RunScope
from .database import Database
from .security import Security
from .settings import Settings
from .storage import ObjectStore, encrypted_converter
from .stores import NativeStores


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.security = Security(settings.master_key)
        self.objects = ObjectStore(settings, self.security)
        self.database = Database(settings, self.security)
        self.stores = NativeStores(settings)
        self.client: Client | None = None
        from .skill_invocations import SkillInvocations
        from .work_progress import WorkProgress
        self.skill_invocations = SkillInvocations(self)
        self.work_progress = WorkProgress(self)
        self.agent: Agent[RunScope, str | DeferredToolRequests]
        self.durability: TemporalDurability[RunScope]
        self.models: dict[str, Model] = {}
        self._models_owned = False

    async def start(
        self, *, models: dict[str, Model] | None = None,
        workspace: AbstractCapability[RunScope] | None = None,
        skills_directory: str = "/opt/modelmesh/skills",
    ) -> None:
        try:
            await self.database.start()
            await self.stores.start()
            self.models = models or inference_models(self.settings)
            self._models_owned = not models
            self.agent, self.durability = make_agent(
                self.settings, self.database, self.stores, self.objects, models=self.models, workspace=workspace,
                skills_directory=skills_directory,
            )
        except BaseException:
            await self.close()
            raise

    async def connect(self) -> Client:
        if self.client is None:
            self.client = await Client.connect(
                self.settings.temporal_address,
                namespace=self.settings.temporal_namespace,
                tls=self.settings.temporal_tls,
                api_key=self.settings.temporal_api_key.get_secret_value() or None,
                plugins=[PydanticAIPlugin()],
                data_converter=encrypted_converter(self.security, self.objects),
            )
        return self.client

    async def close(self) -> None:
        self.work_progress.clear()
        try:
            if self._models_owned:
                from pydantic_ai.models.openai import OpenAIResponsesModel
                # Registered models share one public provider client. Close
                # it once, after worker/request drain, before DB resources.
                clients = {
                    id(model.client): model.client for model in self.models.values()
                    if isinstance(model, OpenAIResponsesModel)
                }
                self._models_owned = False
                for client in clients.values():
                    await client.close()
        finally:
            try:
                await self.stores.close()
            finally:
                await self.database.close()


_runtime: Runtime | None = None


def configure(runtime: Runtime) -> None:
    global _runtime
    _runtime = runtime


def current() -> Runtime:
    if _runtime is None:
        raise RuntimeError("ModelMesh runtime is not initialized.")
    return _runtime
