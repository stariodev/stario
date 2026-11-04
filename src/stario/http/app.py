"""
Application object: route table plus shutdown-aware tasks.

The HTTP protocol does not call this class per request. It reads the compiled
trie (`compiled()`) and constructs `asyncio.Task(handler(c, w))`, registering
incomplete tasks on `app.tasks` for shutdown drain. `create_task` puts app
background work on that same set. `TestClient` drives the protocol too, so
there is one dispatch path.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

from stario.exceptions import StarioError
from stario_cython.exchange import AppState

from .dispatch import Router


def _finish(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


class App(Router):
    """Route table plus task tracking for graceful shutdown.

    Handlers are `async def` and must write a complete response. Uncaught
    exceptions are logged; if nothing was sent, the framework writes 500.
    A response already on the wire is not rewritten. Use `catch_errors` middleware
    or write error responses in handlers. Use `create_task` for work tied to a running server
    so drain can observe it.

    `shutdown` is a per-loop Future for the calling loop. `signal_shutdown()`
    completes it on every attached loop so worker threads wake together.
    `tasks` is per-thread so drain only sees work on this loop.
    """

    def __init__(self) -> None:
        """Create an application (a `Router` with tracked tasks).

        Requires a running event loop — create inside `serve()`, bootstrap,
        or async test code. `shutdown` completes when the runner begins draining.
        """
        super().__init__()
        self._app_state = AppState()
        self._app_state.host_routing = self._host_routing
        self._app_state.router = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise StarioError(
                "App() requires a running event loop",
                help_text="Create App inside serve(), bootstrap, or async test code.",
            ) from exc

        self._shutdown_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._loop_shutdown: dict[asyncio.AbstractEventLoop, asyncio.Future[None]] = {}
        self._task_local = threading.local()
        self.attach_loop(loop)

    def attach_loop(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.Future[None]:
        """Register `loop` (default: running loop) so shutdown reaches it."""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                raise StarioError(
                    "attach_loop() requires a running event loop",
                    help_text="Call App.attach_loop() from the worker thread's loop.",
                ) from exc
        return self._future_for(loop)

    def _future_for(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
        with self._shutdown_lock:
            fut = self._loop_shutdown.get(loop)
            if fut is None:
                fut = loop.create_future()
                self._loop_shutdown[loop] = fut
        if self._shutdown_event.is_set():
            self._wake(loop, fut)
        return fut

    def _wake(self, loop: asyncio.AbstractEventLoop, fut: asyncio.Future[None]) -> None:
        """Complete `fut`, hopping to its loop when called from another thread."""
        if fut.done() or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _finish(fut)
            return
        try:
            loop.call_soon_threadsafe(_finish, fut)
        except RuntimeError:
            if not loop.is_closed():
                raise

    @property
    def shutdown(self) -> asyncio.Future[None]:
        """This loop's shutdown Future — await it; `signal_shutdown()` completes all loops.

        Each access returns a new shield. A pending shield keeps one callback
        on the loop's shared future until shutdown, so a loop that waits on
        each pass reads `app.shutdown` once, before the loop:

        ```python
        shutdown = app.shutdown
        while not app.shutting_down:
            await work()
            await asyncio.wait({shutdown}, timeout=interval)
        ```
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise StarioError(
                "app.shutdown requires a running event loop",
                help_text="Await app.shutdown from async code on the loop that serves requests.",
            ) from exc
        # Shield: a cancelled waiter must not cancel the loop's shared future.
        return asyncio.shield(self._future_for(loop))

    @property
    def shutting_down(self) -> bool:
        """`True` once the runner has started draining this app."""
        return self._shutdown_event.is_set()

    def signal_shutdown(self) -> None:
        """Complete shutdown on every attached loop."""
        self._shutdown_event.set()
        self._app_state.shutting_down = True
        with self._shutdown_lock:
            items = list(self._loop_shutdown.items())
        for loop, fut in items:
            self._wake(loop, fut)

    @property
    def tasks(self) -> set[asyncio.Task[Any]]:
        """Tasks scheduled via `create_task` on this OS thread."""
        stored: set[asyncio.Task[Any]] | None = getattr(self._task_local, "tasks", None)
        if stored is None:
            stored = set()
            self._task_local.tasks = stored
        return stored

    def create_task[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        name: str | None = None,
        eager_start: bool = False,
    ) -> asyncio.Task[T]:
        """Schedule a coroutine on the running loop and retain the task until it completes.

        The HTTP protocol schedules each request handler through this method so
        graceful shutdown can await in-flight work. App code can use the same
        API for background work; both share `tasks` until shutdown drain.

        - `coro`: Coroutine to run.
        - `loop`: Optional loop to schedule on when the caller already has it.
        - `name`: Optional task name for debuggers.
        - `eager_start`: Run immediately until the first suspension.

        The new `asyncio.Task`.

        - `StarioError`: If no event loop is running (call from async request or app code only).
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                raise StarioError(
                    "app.create_task() requires a running event loop",
                    help_text="Call app.create_task() from async code while the app is running.",
                ) from exc
        task = asyncio.Task(
            coro,
            loop=loop,
            name=name,
            eager_start=eager_start,
        )
        if not task.done():
            tasks = self.tasks
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return task

    async def drain_tasks(self) -> None:
        """Await until every task created with `create_task` has finished (including nested scheduling).

        Useful in tests to wait for background work after the HTTP response has been sent. Do not call from
        inside a coroutine that is itself tracked in `create_task` or you risk deadlock.
        """
        while True:
            pending = set(self.tasks)
            if not pending:
                return
            await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)
