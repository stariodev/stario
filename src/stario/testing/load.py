"""Bootstrap loading for integration tests."""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from stario.http.app import App
from stario.http.bootstrap import AppRun, Bootstrap, run_app
from stario.testing.tracer import TestTracer


@asynccontextmanager
async def aload_app(
    bootstrap: Bootstrap,
    *,
    app_factory: Callable[[], App] | None = None,
    tracer: TestTracer | None = None,
) -> AsyncGenerator[App]:
    """Load `bootstrap` like production; emits `server.startup` / `server.shutdown` spans.

    Pass `tracer` to share one `TestTracer` with `TestClient` (same instance
    as `client.tracer` when the client wires bootstrap).
    """
    async with aload_run(bootstrap, app_factory=app_factory, tracer=tracer) as run:
        yield run.app


@asynccontextmanager
async def aload_run(
    bootstrap: Bootstrap,
    *,
    app_factory: Callable[[], App] | None = None,
    tracer: TestTracer | None = None,
) -> AsyncGenerator[AppRun]:
    """`aload_app`, but yields the `AppRun` so `TestClient` can open `server.shutdown`."""
    t = tracer if tracer is not None else TestTracer()
    t.__enter__()
    try:
        async with run_app(bootstrap, t, app_factory=app_factory) as run:
            run.span.attr("test.aload_app", True)
            run.end_startup()
            yield run
    finally:
        t.__exit__(None, None, None)
