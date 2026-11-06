"""Bound coordinator waits without assuming cancellation can undo native effects."""

import asyncio


def consume(task):
    if not task.cancelled():
        task.exception()


async def critical(awaitable):
    """Finish a short ownership transition even if its caller is cancelled."""
    task, cancelled = asyncio.ensure_future(awaitable), False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


async def bounded(awaitable, seconds):
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if not done:
            raise TimeoutError
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            # Let cooperative database contexts finish rollback/connection
            # return before their supervising task exits. Unresponsive native
            # cleanup still has a finite drain allowance.
            await asyncio.wait({task}, timeout=min(1, seconds))
        task.add_done_callback(consume)


async def cancel_tasks(tasks, seconds):
    tasks = set(tasks)
    for task in tasks:
        task.cancel()
        task.add_done_callback(consume)
    if not tasks:
        return True
    _, pending = await asyncio.wait(tasks, timeout=seconds)
    return not pending
