import asyncio

import pytest

from stario import App, Route
from stario.exceptions import StarioRuntime
from stario.http.compression import CompressionConfig
from stario.telemetry.noop import NoOpTracer
from stario.testing.tracer import TestTracer
from stario_cython.protocol import HttpProtocol
from tests.cython.http import connection_set, free_port, read_response
from tests.helpers import assert_status_span


class TrackingApp(App):
    """Spy on App.create_task. Protocol dispatch uses asyncio.Task directly."""

    def __init__(self) -> None:
        super().__init__()
        self.eager_starts: list[bool] = []

    def create_task(self, coro, **kwargs):
        self.eager_starts.append(kwargs.get("eager_start", False))
        return super().create_task(coro, **kwargs)


@pytest.mark.asyncio
async def test_trailing_slash_redirects_without_create_task() -> None:
    loop = asyncio.get_running_loop()
    app = TrackingApp()

    async def search(_c, w):
        w.text("hit")

    app.add(Route("GET /search"), search)
    connections = connection_set()

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        )

    port = free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /search/?q=cats&page=2 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        first = await read_response(reader)
        assert b"308" in first.split(b"\r\n", 1)[0]
        assert b"location: /search?q=cats&page=2" in first.lower()
        assert app.eager_starts == []

        writer.write(b"GET //aftra.io/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        second = await read_response(reader)
        assert b"308" in second.split(b"\r\n", 1)[0]
        assert b"location: /aftra.io" in second.lower()
        assert app.eager_starts == []
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_trailing_slash_redirect_finishes_span_without_fail() -> None:
    loop = asyncio.get_running_loop()
    app = TrackingApp()
    connections = connection_set()

    with TestTracer() as tracer:

        def factory():
            return HttpProtocol(
                loop,
                app,
                tracer,
                CompressionConfig(),
                connections,
            )

        port = free_port()
        server = await loop.create_server(factory, "127.0.0.1", port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /search/?q=cats&page=2 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            )
            await writer.drain()
            first = await read_response(reader)
            assert b"308" in first.split(b"\r\n", 1)[0]
            assert app.eager_starts == []
            assert_status_span(tracer, 308, method="GET", path="/search/")
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_not_found_and_method_not_allowed_use_handlers() -> None:
    loop = asyncio.get_running_loop()
    app = TrackingApp()
    seen: list[str] = []

    async def hello(_c, w):
        w.text("hi")

    async def custom_404(_c, w):
        seen.append("404")
        w.text("gone", 404)

    def custom_405(allowed: frozenset[str]):
        async def respond(_c, w):
            seen.append("405")
            w.headers.set("Allow", ", ".join(sorted(allowed)))
            w.text("nope", 405)

        return respond

    app.add(Route("GET /hello"), hello)
    app.not_found("/", custom_404)
    app.method_not_allowed("/", custom_405)
    connections = connection_set()

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        )

    port = free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /missing HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        missing = await read_response(reader)
        assert b"404" in missing.split(b"\r\n", 1)[0]
        assert b"gone" in missing
        assert seen == ["404"]

        writer.write(
            b"POST /hello HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n"
        )
        await writer.drain()
        denied = await read_response(reader)
        assert b"405" in denied.split(b"\r\n", 1)[0]
        assert b"nope" in denied
        assert b"allow: get" in denied.lower()
        assert seen == ["404", "405"]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_handler_exception_writes_500() -> None:
    loop = asyncio.get_running_loop()
    app = TrackingApp()

    async def boom(_c, _w):
        raise RuntimeError("boom")

    app.add(Route("GET /boom"), boom)
    connections = connection_set()

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        )

    port = free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /boom HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        response = await read_response(reader)
        assert b"500" in response.split(b"\r\n", 1)[0]
        assert b"Internal Server Error" in response
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_plaintext_and_post_and_keepalive() -> None:
    loop = asyncio.get_running_loop()
    app = TrackingApp()
    writers = []

    async def plaintext(_c, w):
        assert type(w).__name__ == "RequestHandle"
        assert _c is w
        writers.append(w._exchange_id)
        w.text("Hello, World!")

    async def echo(c, w):
        writers.append(w._exchange_id)
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.add(Route("GET /plaintext"), plaintext)
    app.add(Route("POST /echo"), echo)

    connections = connection_set()

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        )

    port = free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /plaintext HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        first = await read_response(reader)
        assert b"Hello, World!" in first
        assert all(proto.disconnect is None for proto in connections)

        writer.write(
            b"POST /echo HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\n\r\nabcde"
        )
        await writer.drain()
        second = await read_response(reader)
        assert b"abcde" in second
        # One pooled exchange serves both keep-alive requests.
        assert writers[0] == writers[1]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_small_content_length_body_is_complete_when_handler_runs() -> None:
    """Content-Length bodies <= 256KiB dispatch after llhttp finishes the message."""
    loop = asyncio.get_running_loop()
    app = App()

    async def echo(c, w):
        # Deferred dispatch: body() is already complete (no Event wait).
        payload = await c.req.body()
        assert payload == b"x" * 1024
        w.respond(payload, b"text/plain; charset=utf-8")

    app.add(Route("POST /echo"), echo)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    payload = b"x" * 1024
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 1024\r\n\r\n" + payload
        )
        await writer.drain()
        response = await read_response(reader)
        assert payload in response
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fragmented_headers_materialize_correctly() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def inspect(c, w):
        assert c.req.host == "example.com"
        assert c.req.headers.getlist("x-test") == ["one", "two"]
        w.text("ok")

    app.add(Route("GET /"), inspect)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    request = (
        b"GET / HTTP/1.1\r\n"
        b"Host: Example.COM:80\r\n"
        b"X-Test: one\r\n"
        b"x-test: two\r\n"
        b"Connection: close\r\n\r\n"
    )
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for byte in request:
            writer.write(bytes((byte,)))
            await writer.drain()
            await asyncio.sleep(0)
        assert b"ok" in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_headers_scan_arena_without_copy() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    seen = []

    async def inspect(c, w):
        headers = c.req.headers
        assert type(headers).__name__ == "RequestHeaders"
        assert headers.get("authorization") == "Bearer token"
        assert headers.get("Authorization") == "Bearer token"
        assert headers.get("x-missing") is None
        assert headers.getlist("cookie") == ["a=1", "b=2"]
        assert "X-Request-ID" in headers
        assert "authorization" in headers
        seen.append(headers.items())
        w.text("ok")

    app.add(Route("GET /"), inspect)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET / HTTP/1.1\r\n"
            b"Host: Example.COM:80\r\n"
            b"Authorization: Bearer token\r\n"
            b"Cookie: a=1\r\n"
            b"cookie: b=2\r\n"
            b"X-Request-ID: request-1\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"ok" in await read_response(reader)
        assert (b"cookie", b"a=1") in [
            (name.encode("latin-1"), value.encode("latin-1")) for name, value in seen[0]
        ]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_header_view_resets_when_exchange_is_reused() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    seen_local = []

    async def inspect(c, w):
        headers = c.req.headers
        seen_local.append(headers.get("x-local"))
        w.text("ok")

    app.add(Route("GET /"), inspect)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: first\r\nX-Local: one\r\n\r\n")
        await writer.drain()
        assert b"ok" in await read_response(reader)
        writer.write(b"GET / HTTP/1.1\r\nHost: second\r\nConnection: close\r\n\r\n")
        await writer.drain()
        assert b"ok" in await read_response(reader)
        assert seen_local == ["one", None]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_large_and_chunked_upload() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def upload(c, w):
        total = 0
        async for chunk in c.req.stream():
            total += len(chunk)
        w.respond(str(total).encode("ascii"), b"text/plain; charset=utf-8", 200)

    app.add(Route("POST /upload"), upload)

    connections = connection_set()

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        )

    port = free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = b"x" * (70 * 1024)
        writer.write(
            b"POST /upload HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
        )
        await writer.drain()
        first = await read_response(reader)
        assert b"71680" in first

        chunk = b"y" * 4096
        parts = []
        for _ in range(8):
            parts.append(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
        parts.append(b"0\r\n\r\n")
        writer.write(
            b"POST /upload HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n" + b"".join(parts)
        )
        await writer.drain()
        second = await read_response(reader)
        assert b"32768" in second
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ignored_slow_body_stays_owned_until_message_complete() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    seen = []

    async def ignore(c, w):
        seen.append(c._exchange_id)
        w.text("ignored")

    async def next_request(c, w):
        seen.append(c._exchange_id)
        w.text(c.req.path)

    app.add(Route("POST /ignore"), ignore)
    app.add(Route("GET /next"), next_request)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /ignore HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"4\r\nslow\r\n"
        )
        await writer.drain()
        assert b"ignored" in await read_response(reader)

        writer.write(
            b"0\r\n\r\n"
            b"GET /next HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"/next" in await read_response(reader)
        assert len(seen) == 2
        assert seen[0] == seen[1]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_abandoned_stream_discards_remainder_and_advances_pipeline() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def partial(c, w):
        async for _ in c.req.stream():
            break
        w.text("partial")

    async def next_request(_c, w):
        w.text("next")

    app.add(Route("POST /partial"), partial)
    app.add(Route("GET /next"), next_request)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    payload = b"x" * (2 * 1024 * 1024)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /partial HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
            + b"GET /next HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"partial" in await read_response(reader)
        assert b"next" in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_small_expect_continue_is_sent_before_body() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    started = asyncio.Event()

    async def echo(c, w):
        started.set()
        w.respond(await c.req.body(), b"text/plain")

    app.add(Route("POST /echo"), echo)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 5\r\n"
            b"Expect: 100-continue\r\n\r\n"
        )
        await writer.drain()
        async with asyncio.timeout(2):
            await started.wait()
            assert await reader.readuntil(b"\r\n\r\n") == (
                b"HTTP/1.1 100 Continue\r\n\r\n"
            )

        writer.write(b"hello")
        await writer.drain()
        assert b"hello" in await read_response(reader)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_disconnect_future_is_lazy() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def watch(c, w):
        assert c.disconnected is False
        fut = c.disconnect
        assert not fut.done()
        w.text("ok")

    app.add(Route("GET /watch"), watch)
    connections = connection_set()

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        )

    port = free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /watch HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        payload = await read_response(reader)
        assert b"ok" in payload
        assert all(proto.disconnect is not None for proto in connections)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_pipeline_waits_for_handler_and_uses_each_request_keepalive() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    events = []
    exchanges = {}

    async def first(c, w):
        exchanges["first"] = c
        assert c is w
        w.text("first")
        await asyncio.sleep(0)
        events.append(("first-done", c.req.path))

    async def second(c, w):
        exchanges["second"] = c
        assert c is w
        assert c is not exchanges["first"]
        events.append(("second-start", c.req.path))
        w.text("second")

    app.add(Route("GET /first"), first)
    app.add(Route("GET /second"), second)
    connections = connection_set()

    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n"
            b"GET /second HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"first" in await read_response(reader)
        assert b"second" in await read_response(reader)
        assert events == [("second-start", "/second"), ("first-done", "/first")]
        assert exchanges["first"].completed
        assert exchanges["second"].completed
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_handler_after_respond_stays_on_app_tasks_and_next_request_starts() -> (
    None
):
    """4.3: respond() frees the connection; the handler Task drains on app.tasks."""
    loop = asyncio.get_running_loop()
    app = App()
    release = asyncio.Event()
    first_responded = asyncio.Event()
    second_started = asyncio.Event()

    async def first(c, w):
        w.text("first")
        first_responded.set()
        await release.wait()

    async def second(c, w):
        second_started.set()
        w.text("second")

    app.add(Route("GET /first"), first)
    app.add(Route("GET /second"), second)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        assert b"first" in await read_response(reader)
        await asyncio.wait_for(first_responded.wait(), timeout=1.0)
        assert any(not task.done() for task in app.tasks)

        writer.write(b"GET /second HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        assert b"second" in await read_response(reader)

        drain = asyncio.create_task(app.drain_tasks())
        await asyncio.sleep(0)
        assert not drain.done()
        release.set()
        await asyncio.wait_for(drain, timeout=1.0)
        assert not app.tasks
        writer.close()
        await writer.wait_closed()
    finally:
        release.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_large_single_read_pipeline_is_bounded() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    release = asyncio.Event()
    handled: list[int] = []

    async def endpoint(c, w):
        index = int(c.req.query_bytes)
        handled.append(index)
        if index == 0:
            await release.wait()
        w.respond(str(index).encode("ascii"), b"text/plain")

    app.add(Route("GET /"), endpoint)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    request_count = 128
    requests = []
    for index in range(request_count):
        requests.append(
            b"GET /?"
            + str(index).encode("ascii")
            + b" HTTP/1.1\r\nHost: localhost\r\n"
            + b"\r\n"
        )
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"".join(requests))
        await writer.drain()
        await asyncio.sleep(0.05)
        release.set()
        # Request 0 plus the eight queued ones are answered in order, then
        # 429 for the next one and the connection closes.
        for index in range(9):
            response = await read_response(reader)
            assert response.startswith(b"HTTP/1.1 200")
            assert response.endswith(str(index).encode("ascii"))
        assert (await read_response(reader)).startswith(b"HTTP/1.1 429")
        assert handled == list(range(9))
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_pipelined_streaming_bodies_are_request_owned() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def upload(c, w):
        total = 0
        async for chunk in c.req.stream():
            total += len(chunk)
        w.respond(str(total).encode("ascii"), b"text/plain; charset=utf-8")

    app.add(Route("POST /upload"), upload)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    first = b"a" * (66 * 1024)
    second = b"b" * (67 * 1024)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
            + str(len(first)).encode()
            + b"\r\n\r\n"
            + first
            + b"POST /upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
            + str(len(second)).encode()
            + b"\r\n\r\n"
            + second
        )
        await writer.drain()
        assert str(len(first)).encode() in await read_response(reader)
        assert str(len(second)).encode() in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_exchange_pool_reuses_across_connections() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    exchanges = []

    async def endpoint(c, w):
        assert c is w
        exchanges.append(c._exchange_id)
        w.text("ok")

    app.add(Route("GET /"), endpoint)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            assert b"ok" in await read_response(reader)
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0)
        assert exchanges[0] == exchanges[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_handler_starts_before_content_length_body_arrives() -> None:
    """Bodies larger than 256KiB still dispatch at headers-complete."""
    loop = asyncio.get_running_loop()
    app = App()
    started = asyncio.Event()
    payload = b"x" * (257 * 1024)

    async def echo(c, w):
        started.set()
        body = await c.req.body()
        w.respond(body, b"text/plain")

    app.add(Route("POST /echo"), echo)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        writer.write(payload)
        await writer.drain()
        assert payload in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_body_wait_survives_multi_segment_upload() -> None:
    """body() must not require per-segment Event wakes — only completion."""
    loop = asyncio.get_running_loop()
    app = App()
    started = asyncio.Event()

    async def echo(c, w):
        started.set()
        body = await c.req.body()
        w.respond(body, b"text/plain")

    app.add(Route("POST /echo"), echo)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = b"abcdefghij" * 30000  # 300 KiB: above the 256 KiB deferral cap
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        # Deliver body in small segments with yields so body() is waiting.
        for i in range(0, len(payload), 512):
            writer.write(payload[i : i + 512])
            await writer.drain()
            await asyncio.sleep(0)
        assert payload in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_content_length_body_survives_many_64k_segments() -> None:
    """Known Content-Length body() must not join a tail per 64KiB feed."""
    loop = asyncio.get_running_loop()
    app = App()
    started = asyncio.Event()
    payload = bytes(range(256)) * 2048  # 512 KiB: above the 256 KiB deferral cap

    async def echo(c, w):
        started.set()
        body = await c.req.body()
        w.respond(body, b"application/octet-stream")

    app.add(Route("POST /echo"), echo)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        for i in range(0, len(payload), 4096):
            writer.write(payload[i : i + 4096])
            await writer.drain()
            await asyncio.sleep(0)
        assert payload in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_content_length_body_in_same_packet_as_headers() -> None:
    """First body bytes in the headers-complete execute still materialize correctly."""
    loop = asyncio.get_running_loop()
    app = App()
    payload = b"y" * (128 * 1024)

    async def echo(c, w):
        w.respond(await c.req.body(), b"application/octet-stream")

    app.add(Route("POST /echo"), echo)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
        )
        await writer.drain()
        assert payload in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_next_request_starts_after_respond_before_handler_returns() -> None:
    """Connection advances on response send, not when the handler coroutine ends."""
    loop = asyncio.get_running_loop()
    app = App()
    first_responded = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first(_c, w):
        order.append("first-enter")
        w.respond(b"one", b"text/plain")
        first_responded.set()
        await release_first.wait()
        order.append("first-exit")

    async def second(_c, w):
        order.append("second-enter")
        second_started.set()
        w.respond(b"two", b"text/plain")

    app.add(Route("GET /first"), first)
    app.add(Route("GET /second"), second)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n"
            b"GET /second HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"one" in await read_response(reader)
        await asyncio.wait_for(first_responded.wait(), timeout=1.0)
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        assert "second-enter" in order
        assert "first-exit" not in order
        release_first.set()
        assert b"two" in await read_response(reader)
        await asyncio.sleep(0)
        assert order == ["first-enter", "second-enter", "first-exit"]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_max_chunk_must_be_below_limit() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    errors: list[ValueError] = []

    async def upload(c, w):
        try:
            async for _ in c.req.stream(max_chunk=1024 * 1024):
                pass
        except ValueError as exc:
            errors.append(exc)
            raise
        w.respond(b"ok", b"text/plain")

    app.add(Route("POST /upload"), upload)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\n\r\nx"
        )
        await writer.drain()
        try:
            response = await read_response(reader)
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            ConnectionAbortedError,
        ):
            response = b""
        assert b"500" in response.split(b"\r\n", 1)[0]
        assert errors
        assert "stream chunk limit" in str(errors[0])
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_respects_max_chunk_batches() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    sizes: list[int] = []

    async def upload(c, w):
        async for chunk in c.req.stream(max_chunk=8 * 1024):
            sizes.append(len(chunk))
        w.respond(str(sum(sizes)).encode("ascii"), b"text/plain")

    app.add(Route("POST /upload"), upload)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    payload = b"z" * (2 * 1024 * 1024)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /upload HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
        )
        await writer.drain()
        assert b"2097152" in await read_response(reader)
        assert sizes
        assert all(size <= 8 * 1024 for size in sizes)
        assert any(size == 8 * 1024 for size in sizes[:-1] or sizes)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_medium_content_length_body_is_complete_when_handler_runs() -> None:
    """200 KiB is under the 256 KiB complete-before-dispatch cutoff."""
    loop = asyncio.get_running_loop()
    app = App()

    async def echo(c, w):
        payload = await c.req.body()
        assert payload == b"y" * (200 * 1024)
        w.respond(payload, b"text/plain; charset=utf-8")

    app.add(Route("POST /echo"), echo)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    payload = b"y" * (200 * 1024)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
        )
        await writer.drain()
        assert payload in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_default_chunk_follows_content_length() -> None:
    """Known Content-Length: default stream() yields min(length, 256 KiB)."""
    loop = asyncio.get_running_loop()
    app = App()
    sizes: list[int] = []

    async def upload(c, w):
        async for chunk in c.req.stream():
            sizes.append(len(chunk))
        w.respond(str(sum(sizes)).encode("ascii"), b"text/plain")

    app.add(Route("POST /upload"), upload)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    payload = b"z" * (300 * 1024)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /upload HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
        )
        await writer.drain()
        assert str(len(payload)).encode("ascii") in await read_response(reader)
        assert sizes == [256 * 1024, 44 * 1024]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_headers_are_read_only() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    errors: list[str] = []

    async def inspect(c, w):
        with pytest.raises(StarioRuntime, match="read-only"):
            c.req.headers.set("X-Local", "yes")
        with pytest.raises(StarioRuntime, match="read-only"):
            c.req.headers.add("X-Local", "yes")
        with pytest.raises(StarioRuntime, match="read-only"):
            c.req.headers.remove("host")
        errors.append("raised")
        w.text("ok")

    app.add(Route("GET /"), inspect)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET / HTTP/1.1\r\nHost: stario.test\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"ok" in await read_response(reader)
        assert errors == ["raised"]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_lazy_cookies_and_query_from_arena() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    seen: dict[str, object] = {}

    async def inspect(c, w):
        assert type(c.req.headers).__name__ == "RequestHeaders"
        assert c.req.cookies.get("a") == "2"
        assert c.req.cookies.get("x") == "a b"
        assert c.req.cookies.get("b") == "3"
        assert c.req.cookies.get("missing") is None
        assert dict(c.req.cookies) == {"a": "2", "b": "3", "x": "a b"}
        assert c.req.query.get("q") == "hello world"
        assert c.req.query.getlist("tag") == ["a", "b"]
        assert c.req.query.get("accent") == "é"
        assert c.req.query.get("bad") == "�"
        assert c.req.query.get("eq") == "1=2"
        assert c.req.headers.get("authorization") == "Bearer abc"
        seen["ok"] = True
        w.text("ok")

    app.add(Route("GET /search"), inspect)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /search?q=hello+world&tag=a&tag=b&accent=%C3%A9&bad=%A9&eq=1=2"
            b" HTTP/1.1\r\n"
            b"Host: stario.test\r\n"
            b"Authorization: Bearer abc\r\n"
            b'Cookie: a=1; x="a b"\r\n'
            b"Cookie: a=2; b=3\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"ok" in await read_response(reader)
        assert seen.get("ok") is True
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cookies_do_not_leak_across_keepalive() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    seen: list[dict[str, str]] = []

    async def inspect(c, w):
        seen.append(dict(c.req.cookies))
        w.text("ok")

    app.add(Route("GET /"), inspect)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: first\r\nCookie: sid=one\r\n\r\n")
        await writer.drain()
        assert b"ok" in await read_response(reader)
        writer.write(
            b"GET / HTTP/1.1\r\n"
            b"Host: second\r\n"
            b"Cookie: sid=two\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"ok" in await read_response(reader)
        assert seen == [{"sid": "one"}, {"sid": "two"}]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_host_routing_normalizes_host_header() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def hello(c, w):
        w.text(f"host:{c.req.host}")

    app.add(Route("GET //example.com/"), hello)
    connections = connection_set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET / HTTP/1.1\r\nHost: Example.COM:80\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"host:example.com" in await read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


def test_date_line_follows_the_wall_clock() -> None:
    """The real clock path (time()/gmtime_s on Windows, clock_gettime/gmtime_r
    elsewhere); every other test pins the Date header."""
    from datetime import UTC, datetime
    from email.utils import parsedate_to_datetime

    from stario_cython.protocol import Connections

    line = Connections().date_line
    assert line.startswith(b"date: ")
    assert line.endswith(b" GMT\r\n")
    when = parsedate_to_datetime(line[len(b"date: ") : -2].decode("ascii"))
    assert abs((when - datetime.now(UTC)).total_seconds()) < 5
