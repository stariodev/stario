"""Cancel the app tasks that outlive a shutdown drain, and name each one.

`Server` and `TestClient` share these helpers, so both put the same
`server.shutdown.task_cancelled` events on the `server.shutdown` span.
"""

import asyncio
from collections.abc import Iterable
from typing import Any

from stario.telemetry.core import Attributes, Span


def task_name(task: asyncio.Task[Any]) -> str:
    """The task name, or the coroutine's `__qualname__` when asyncio named it `Task-N`.

    Naming at cancel time keeps `create_task` and the handler hot path untouched.
    A handler task gets the handler's own name this way.
    """
    name = task.get_name()
    if not name.startswith("Task-"):
        return name
    return getattr(task.get_coro(), "__qualname__", name)


async def cancel_pending(tasks: Iterable[asyncio.Task[Any]]) -> list[str]:
    """Cancel every unfinished task, wait for each to settle, return their names."""
    pending = [task for task in tasks if not task.done()]
    names = [task_name(task) for task in pending]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return names


def record_cancelled(
    span: Span, names: Iterable[str], attributes: Attributes | None = None
) -> None:
    """One `server.shutdown.task_cancelled` event for each name."""
    for name in names:
        span.event(
            "server.shutdown.task_cancelled", {"task.name": name, **(attributes or {})}
        )
