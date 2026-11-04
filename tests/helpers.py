"""Shared test helpers for the stario test suite.

Plain importable helpers (not fixtures) so test modules can compose them
freely: `from tests.helpers import DummyWriter, make_context, ...`.

`TestWriter` and `TestContext` are unit-test doubles for calling a handler or
a response helper directly. App-level tests go through `stario.testing.
TestClient`, which runs the real protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Self, cast
from urllib.parse import urlencode

from stario.exceptions import StarioError
from stario.http.app import App
from stario.http.context import (
    EMPTY_MATCH,
    Context,
    Handler,
    Match,
    _Alive,
)
from stario.http.headers import Headers
from stario.http.request import Request
from stario.http.writer import Writer
from stario.telemetry.core import Span
from stario.telemetry.noop import NoOpTracer
from stario.testing.tracer import TestTracer

type AppSetup = Callable[[App], None]


def _w_text(w: Any, body: str, status: int = 200) -> None:
    w.respond(body.encode("utf-8"), b"text/plain; charset=utf-8", status)


def _w_json(w: Any, value: Any, status: int = 200) -> None:
    from stario.json import dumps_bytes

    try:
        payload = dumps_bytes(value)
    except (TypeError, ValueError) as exc:
        raise StarioError(
            "JSON response value is not serializable",
            context={"value": value},
            help_text="Pass dict, list, str, int, float, bool, or None values only.",
        ) from exc
    w.respond(payload, b"application/json; charset=utf-8", status)


def _w_html(w: Any, content: Any, status: int = 200) -> None:
    if isinstance(content, bytes):
        body = content
    elif isinstance(content, str):
        body = content.encode("utf-8")
    else:
        from stario.markup import render

        body = render(content).encode("utf-8")
    w.respond(body, b"text/html; charset=utf-8", status)


def _w_redirect(w: Any, url: str, status: int = 307) -> None:
    from stario.http.redirect import normalized_location

    if not 300 <= status < 400:
        raise StarioError(
            "Redirect status must be a 3xx status code",
            context={"status": status},
            help_text="Use w.text/w.json/w.html for response bodies.",
        )
    w.headers.set("location", normalized_location(url))
    w.respond(b"", b"text/plain; charset=utf-8", status)


def _w_empty(w: Any, status: int = 204) -> None:
    w.respond(b"", b"text/plain; charset=utf-8", status)


async def noop_handler(_c: Context, _w: Writer) -> None:
    return None


def track(calls: list[str], label: str):
    def deco(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            calls.append(label)
            await handler(c, w)

        return wrapped

    return deco


def app_for_loop(_loop: asyncio.AbstractEventLoop) -> App:
    return App()


class DummyWriter:
    """Minimal Writer stand-in for dispatch-level router/app tests.

    Tracks the response surface the router/app touches: status, body,
    headers, started/ended/completed flags.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.body: str | None = None
        self.headers = Headers()
        self.started = False
        self.ended = False
        self.gone = False
        self._status_code: int | None = None
        self._completed = False

    @property
    def status_code(self) -> int | None:
        return self._status_code

    @property
    def completed(self) -> bool:
        return self._completed

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        self.body = body.decode("utf-8")
        self.status = status
        self.started = True
        self._status_code = status
        self.headers.set("content-type", content_type.decode("latin-1"))
        self.ended = True
        self._completed = True

    def write_headers(self, status: int):
        self.status = status
        self.started = True
        self._status_code = status
        return self

    def write(self, data: bytes):
        if data:
            self.body = data.decode("utf-8")
        return self

    async def drain(self) -> None:
        return None

    def end(self, data: bytes | None = None) -> None:
        if data is not None:
            self.body = data.decode("utf-8")
        self.ended = True
        self._completed = True
        return None

    def abort(self) -> None:
        self.ended = False
        self._completed = True

    @property
    def closing(self) -> bool:
        return self.gone or self._completed

    def text(self, body: str, status: int = 200) -> None:
        _w_text(self, body, status)

    def json(self, value: Any, status: int = 200) -> None:
        _w_json(self, value, status)

    def html(self, content: Any, status: int = 200) -> None:
        _w_html(self, content, status)

    def redirect(self, url: str, status: int = 307) -> None:
        _w_redirect(self, url, status)

    def empty(self, status: int = 204) -> None:
        _w_empty(self, status)


def make_request(
    *,
    method: str = "GET",
    path: str = "/",
    host: str = "",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: dict[str, object] | None = None,
    query_bytes: bytes | None = None,
) -> Request:
    hdrs = Headers()
    if headers:
        for name, value in headers.items():
            hdrs.set(name, value)
    if host:
        hdrs.set("host", host)

    if query_bytes is None:
        query_bytes = urlencode(query or {}, doseq=True).encode("ascii")

    return Request(
        method=method,
        path=path,
        query_bytes=query_bytes,
        headers=hdrs,
        body=body,
    )


def make_context(
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    loop: asyncio.AbstractEventLoop,
    disconnect: asyncio.Future[Any] | None = None,
) -> TestContext:
    if disconnect is None:
        disconnect = loop.create_future()
    if app is None:
        app = App()
    tracer = NoOpTracer()
    return TestContext(
        app=app,
        req=make_request(method=method, path=path, host=host, query=query),
        span=tracer.create("request"),
        _disconnect=disconnect,
        state={},
    )


async def invoke_app(
    app: App,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[TestContext, DummyWriter]:
    """Route through the app's table and call the handler directly (no HTTP)."""
    loop = asyncio.get_running_loop()
    ctx = make_context(path, method, host, app=app, query=query, loop=loop)
    w = writer or DummyWriter()
    handler, _route, ctx.match = app.find_handler(
        host if app.host_routing else "", ctx.req.raw_path.decode("latin-1"), method
    )
    await handler(ctx, cast(Writer, w))
    return ctx, w


def run_with_app(
    setup: AppSetup | App,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    query: dict[str, object] | None = None,
) -> tuple[TestContext, DummyWriter]:
    async def _run() -> tuple[TestContext, DummyWriter]:
        loop = asyncio.get_running_loop()
        if isinstance(setup, App):
            app = setup
        else:
            app = app_for_loop(loop)
            setup(app)
        return await invoke_app(app, path, method, host, query=query)

    return asyncio.run(_run())


async def invoke_handler(
    handler: Any,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[TestContext, DummyWriter]:
    loop = asyncio.get_running_loop()
    ctx = make_context(path, method, host, app=app, query=query, loop=loop)
    w = writer or DummyWriter()
    await handler(ctx, cast(Writer, w))
    return ctx, w


def run_handler(
    handler: Any,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[TestContext, DummyWriter]:
    return asyncio.run(
        invoke_handler(
            handler,
            path,
            method,
            host,
            app=app,
            query=query,
            writer=writer,
        )
    )


def run_async(awaitable: Awaitable[None]) -> None:
    asyncio.run(cast(Coroutine[Any, Any, None], awaitable))


class TestWriter:
    """Writer double: collects one response's status, headers, and body bytes."""

    __test__ = False

    def __init__(self, disconnect: asyncio.Future[None] | None = None) -> None:
        self.headers = Headers()
        self.buf = bytearray()
        self._status_code: int | None = None
        self._completed = False
        self._disconnect = disconnect

    @property
    def status_code(self) -> int | None:
        return self._status_code

    @property
    def started(self) -> bool:
        return self._status_code is not None

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def closing(self) -> bool:
        return self._completed or (
            self._disconnect is not None and self._disconnect.done()
        )

    @property
    def body(self) -> bytes:
        return bytes(self.buf)

    def respond(
        self, body: bytes, content_type: bytes | str, status: int = 200
    ) -> None:
        if self._completed:
            return
        if self._status_code is not None:
            raise RuntimeError("Response already started (headers sent).")
        ctype = (
            content_type.decode("latin-1")
            if isinstance(content_type, bytes)
            else content_type
        )
        self.headers.set("content-type", ctype)
        payload = body if body else b""
        if 100 <= status < 200 or status in {204, 304}:
            payload = b""
        self.headers.set("content-length", str(len(payload)))
        self._status_code = status
        self.buf.extend(payload)
        self._completed = True

    def write_headers(self, status_code: int) -> Self:
        if self._status_code is not None:
            raise RuntimeError("Response already started (headers sent).")
        self._status_code = status_code
        return self

    def write(self, data: bytes) -> Self:
        if self._completed:
            raise RuntimeError("Cannot write after response is completed.")
        if self._status_code is None:
            self.write_headers(200)
        if self._status_code in {204, 304} or 100 <= (self._status_code or 0) < 200:
            raise RuntimeError(
                f"Cannot write a body for HTTP {self._status_code} responses."
            )
        self.buf.extend(data)
        return self

    async def drain(self) -> None:
        return None

    def end(self, data: bytes | None = None) -> None:
        if self._completed:
            return
        if self._status_code is None:
            self.write_headers(200 if data else 204)
        if data:
            self.write(data)
        self._completed = True

    def abort(self) -> None:
        if self._completed:
            return
        self._completed = True
        self.headers.set("connection", "close")

    def text(self, body: str, status: int = 200) -> None:
        _w_text(self, body, status)

    def json(self, value: Any, status: int = 200) -> None:
        _w_json(self, value, status)

    def html(self, content: Any, status: int = 200) -> None:
        _w_html(self, content, status)

    def redirect(self, url: str, status: int = 307) -> None:
        _w_redirect(self, url, status)

    def empty(self, status: int = 204) -> None:
        _w_empty(self, status)


@dataclass(slots=True)
class TestContext:
    """Context double for calling a handler directly."""

    __test__ = False

    app: App
    req: Request
    span: Span
    _disconnect: asyncio.Future[Any] = field(repr=False)
    state: dict[str, Any] = field(default_factory=dict[str, Any])
    match: Match = field(default=EMPTY_MATCH)

    @property
    def disconnect(self) -> asyncio.Future[Any]:
        return self._disconnect

    @property
    def disconnected(self) -> bool:
        return self._disconnect.done()

    @property
    def shutting_down(self) -> bool:
        return self.app.shutting_down

    @property
    def closing(self) -> bool:
        return self.disconnected or self.shutting_down

    def alive(self, source: Any = None) -> _Alive[Any]:
        return _Alive(cast(Context, self), source)


def make_writer() -> TestWriter:
    """A writer double that collects status, headers, and body."""
    return TestWriter()


class _ClosedLoop:
    def close(self) -> None:
        return None


def make_writer_raw() -> tuple[TestWriter, bytearray, _ClosedLoop]:
    """TestWriter plus its body buffer (not HTTP/1.1 wire bytes)."""
    writer = TestWriter()
    return writer, writer.buf, _ClosedLoop()


def split_response(raw: bytes) -> tuple[bytes, bytes]:
    """Split raw HTTP/1.1 wire bytes into (head, body)."""
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body


def assert_status_span(
    tracer: TestTracer,
    status: int,
    *,
    method: str | None = None,
    path: str | None = None,
) -> None:
    """Assert a finished request span recorded `status` and was not failed."""
    matches = [
        span
        for span in tracer.finished_spans()
        if span.attributes.get("response.status_code") == status
    ]
    assert matches, f"no finished span with status {status}: {tracer.finished_spans()}"
    span = matches[0]
    assert span.ok
    if method is not None:
        assert span.attributes.get("request.method") == method
    if path is not None:
        assert span.attributes.get("request.path") == path
    assert not tracer.has_open_spans()
