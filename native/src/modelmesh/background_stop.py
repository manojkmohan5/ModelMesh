"""Cooperative shutdown at an operation boundary, retaining in-flight ownership."""

from __future__ import annotations

import asyncio


async def pause(seconds: float, stop: asyncio.Event | None = None, *, wake: asyncio.Event | None = None) -> None:
    if wake is not None:
        waiting = [asyncio.create_task(event.wait()) for event in (wake, stop) if event is not None]
        try:
            await asyncio.wait(waiting, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)
        return
    if stop is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass

