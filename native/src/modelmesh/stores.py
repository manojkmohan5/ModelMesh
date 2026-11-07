"""Use the Harness's SQLite stores, with application-authenticated conversation scopes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic_ai_harness.memory import MemoryStore, SqliteMemoryStore
from pydantic_ai_harness.planning import PlanStore, SqlitePlanStore
from pydantic_ai_harness.step_persistence import SqliteStepStore

from .settings import Settings


class NativeStores:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.memory: MemoryStore
        self.steps: SqliteStepStore
        self.plans: dict[str, PlanStore] = {}
        self.database: Path = settings.data_dir / "agent-state.db"

    async def start(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.memory = await asyncio.to_thread(SqliteMemoryStore, database=self.database)
        self.steps = await asyncio.to_thread(
            SqliteStepStore, database=self.database, max_snapshots_per_run=None
        )
        # SDK constructors are lazy. Establish WAL and the shared schema
        # before concurrent agent activities race their first connection.
        # These public reads create no user runs, files or plan items.
        await self.steps.get_run(run_id="__schema_probe__")
        await self.memory.read("__schema_probe__", max_chars=1)
        await self.plan("__schema_probe__").get_items()

    def plan(self, session_id: str) -> PlanStore:
        # Constructors only select a namespace; reads/writes are native durable operations.
        if session_id not in self.plans:
            self.plans[session_id] = SqlitePlanStore(str(self.database), session=session_id)
        return self.plans[session_id]

    async def close(self) -> None:
        for store in (getattr(self, "steps", None), getattr(self, "memory", None)):
            close = getattr(store, "close", None)
            if callable(close):
                await asyncio.to_thread(close)
