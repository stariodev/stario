"""TestClient runs the production protocol: what a test sees is wire truth."""

from __future__ import annotations

import asyncio

import pytest

from stario import App, Context, Route, Writer
from stario.exceptions import RequestBodyError
from stario.http.compression import CompressionConfig
from stario.http.config import RequestPolicy
from stario.testing import TestClient
from stario.testing.wire import ResponseReader
from stario_cython.exchange import Request


async def test_invalid_target_is_the_protocols_400() -> None:
    """No handler runs; the answer comes from the same code `serve()` uses."""
    seen: list[str] = []

    async def handler(c: Context, w: Writer) -> None:
        seen.append(c.req.path)
        w.text("ok")

    app = App()
    app.add(Route("GET /"), handler)
    async with TestClient(app) as client:
        bad_query = await client.get("/?b=%00")
        bad_path = await client.get("/%zz")
        root = await client.get("/")
    assert bad_query.status_code == 400
    assert bad_path.status_code == 400
    assert root.status_code == 200
    assert seen == ["/"]
    span = client.tracer.get_span(bad_query.span_id)
    assert span is not None
    assert span.name == "GET"
    assert span.attributes["response.status_code"] == 400


async def test_head_and_204_have_no_body() -> None:
    async def page(_c: Context, w: Writer) -> None:
        w.text("hello world")

    async def gone(_c: Context, w: Writer) -> None:
        w.empty(204)

    app = App()
    app.add(Route("GET /page"), page)
    app.add(Route("HEAD /page"), page)
    app.add(Route("DELETE /thing"), gone)
    async with TestClient(app) as client:
        head = await client.head("/page")
        deleted = await client.delete("/thing")
    assert head.status_code == 200
    assert head.headers.get("content-length") == "11"
    assert head.content == b""
    assert deleted.status_code == 204
    assert deleted.content == b""


async def test_compression_is_real_and_decoded_for_buffered_calls() -> None:
    body = "x" * 4096

    async def page(_c: Context, w: Writer) -> None:
        w.text(body)

    app = App()
    app.add(Route("GET /"), page)
    async with TestClient(app, compression=CompressionConfig(min_size=1)) as client:
        plain = await client.get("/")
        zipped = await client.get("/", headers={"accept-encoding": "gzip"})
    assert plain.headers.get("content-encoding") is None
    assert plain.text == body
    assert zipped.headers.get("content-encoding") == "gzip"
    assert zipped.text == body  # decoded for you
    assert "accept-encoding" in (zipped.headers.get("vary") or "").lower()


async def test_post_body_round_trips_through_the_parser() -> None:
    async def echo(c: Context, w: Writer) -> None:
        data = await c.req.body()
        w.headers.set("x-len", c.req.headers.get("content-length") or "")
        w.json({"n": len(data), "ct": c.req.headers.get("content-type")})

    app = App()
    app.add(Route("POST /echo"), echo)
    async with TestClient(app) as client:
        r = await client.post("/echo", json={"a": [1, 2, 3]})
    assert r.status_code == 200
    assert r.json() == {"n": 13, "ct": "application/json; charset=utf-8"}
    assert r.headers.get("x-len") == "13"


async def test_stream_exit_hangs_up_and_the_handler_sees_it() -> None:
    events: list[str] = []
    started = asyncio.Event()

    async def live(c: Context, w: Writer) -> None:
        w.headers.set("content-type", "text/event-stream")
        w.write_headers(200)
        w.write(b"data: one\n\n")
        started.set()
        await c.disconnect
        events.append("disconnected" if c.disconnected else "?")

    app = App()
    app.add(Route("GET /live"), live)
    async with TestClient(app) as client:
        async with client.stream("GET", "/live") as s:
            assert s.status_code == 200
            assert s.headers.get("transfer-encoding") == "chunked"
            first = await anext(s.iter_events())
            assert first == {"data": "one"}
            assert not s.disconnected
        assert s.disconnected
    # Leaving the block waited for the handler, which saw the hang-up.
    assert events == ["disconnected"]
    assert not client.tracer.has_open_spans()


async def test_timeout_hangs_up_and_cancels_the_handler() -> None:
    cancelled = asyncio.Event()

    async def slow(_c: Context, w: Writer) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        w.text("late")

    app = App()
    app.add(Route("GET /slow"), slow)
    async with TestClient(app) as client:
        with pytest.raises(TimeoutError, match="no headers"):
            await client.get("/slow", timeout=0.05)
        assert cancelled.is_set()
        # The client is still usable afterwards.
        assert (await client.get("/missing")).status_code == 404


async def test_exit_signals_shutdown_so_work_loops_stop() -> None:
    """A `while not app.shutting_down` job exits like under the real Server."""

    async def page(c: Context, w: Writer) -> None:
        async def job() -> None:
            while not c.app.shutting_down:
                await asyncio.sleep(0.01)

        c.app.create_task(job())
        w.text("ok")

    async def bootstrap(app: App, span):
        app.add(Route("GET /"), page)
        yield

    async with asyncio.timeout(1):
        async with TestClient(bootstrap) as client:
            assert (await client.get("/")).status_code == 200


async def test_exit_cancels_tasks_past_shutdown_timeout() -> None:
    """A job that never wakes on its own is cancelled at the deadline."""
    cancelled = asyncio.Event()

    async def page(c: Context, w: Writer) -> None:
        async def job() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        c.app.create_task(job())
        w.text("ok")

    async def bootstrap(app: App, span):
        app.add(Route("GET /"), page)
        yield

    async with asyncio.timeout(5):
        async with TestClient(bootstrap, shutdown_timeout=0.1) as client:
            assert (await client.get("/")).status_code == 200
    assert cancelled.is_set()


async def deaf_loop() -> None:
    while True:
        await asyncio.sleep(3600)


def cancelled_names(client: TestClient) -> list[str]:
    shutdown = client.tracer.find_span("server.shutdown")
    assert shutdown is not None
    events = client.tracer.get_events(
        shutdown.id, name="server.shutdown.task_cancelled"
    )
    assert shutdown.attributes["server.shutdown.cancelled_tasks"] == len(events)
    return sorted(e.attributes["task.name"] for e in events)


async def test_exit_names_cancelled_tasks_on_bootstrap_shutdown_span() -> None:
    """The drain runs inside `server.shutdown`, so each cancel is an event there."""

    async def bootstrap(app: App, span):
        app.create_task(deaf_loop())
        app.create_task(deaf_loop(), name="ticker")
        yield

    async with asyncio.timeout(5):
        async with TestClient(bootstrap, shutdown_timeout=0.05) as client:
            pass
    assert cancelled_names(client) == ["deaf_loop", "ticker"]
    shutdown = client.tracer.find_span("server.shutdown")
    assert shutdown is not None
    assert shutdown.attributes["server.shutdown.trigger"] == "expected_stop"


async def test_exit_opens_shutdown_span_for_built_app() -> None:
    app = App()
    async with asyncio.timeout(5):
        async with TestClient(app, shutdown_timeout=0.05) as client:
            app.create_task(deaf_loop())
    assert cancelled_names(client) == ["deaf_loop"]


async def test_exit_records_no_cancel_when_loops_stop_in_time() -> None:
    async def polite_loop(app: App) -> None:
        while not app.shutting_down:
            await asyncio.wait({app.shutdown}, timeout=3600)

    app = App()
    async with asyncio.timeout(1):
        async with TestClient(app) as client:
            app.create_task(polite_loop(app))
    assert cancelled_names(client) == []


async def test_buffered_call_returns_after_the_handler_finished() -> None:
    """Post-response work in the handler is done; app.create_task work is not."""
    order: list[str] = []

    async def page(c: Context, w: Writer) -> None:
        async def background() -> None:
            await asyncio.sleep(0.01)
            order.append("background")

        c.app.create_task(background())
        w.text("ok")
        await asyncio.sleep(0)
        order.append("after respond")

    app = App()
    app.add(Route("GET /"), page)
    async with TestClient(app) as client:
        await client.get("/")
        assert order == ["after respond"]
    # Exit drained the background task.
    assert order == ["after respond", "background"]


def test_response_reader_frames_chunked_and_length_bodies() -> None:
    chunked = ResponseReader(head_request=False)
    chunked.feed(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n3\r\nabc\r\n")
    chunked.feed(b"2\r\nde\r\n0\r\n\r\n")
    assert chunked.status_code == 200
    assert chunked.done
    assert b"".join(chunked._chunks) == b"abcde"

    sized = ResponseReader(head_request=False)
    sized.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\n\r\nab")
    assert not sized.done
    sized.feed(b"cd")
    assert sized.done

    head = ResponseReader(head_request=True)
    head.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 99\r\n\r\n")
    assert head.done


async def test_body_limit_is_a_default_each_read_can_raise() -> None:
    """`RequestPolicy.max_body_bytes` is the default; `max_size` overrides it."""

    async def default_limit(c: Context, w: Writer) -> None:
        w.text(str(len(await c.req.body())))

    async def upload(c: Context, w: Writer) -> None:
        w.text(str(len(await c.req.body(max_size=1000))))

    app = App()
    app.add(Route("POST /small"), default_limit)
    app.add(Route("POST /upload"), upload)
    async with TestClient(app, requests=RequestPolicy(max_body_bytes=20)) as client:
        small_ok = await client.post("/small", content=b"y" * 20)
        small_big = await client.post("/small", content=b"y" * 100)
        upload_big = await client.post("/upload", content=b"y" * 100)
        upload_huge = await client.post("/upload", content=b"y" * 1001)
    assert (small_ok.status_code, small_ok.text) == (200, "20")
    assert small_big.status_code == 413
    assert (upload_big.status_code, upload_big.text) == (200, "100")
    assert upload_huge.status_code == 413


async def test_detached_body_keeps_the_default_limit() -> None:
    """A body read after the handler returns still gets the server default."""

    held: list[Request] = []

    async def keep(c: Context, w: Writer) -> None:
        held.append(c.req)
        w.text("ok")

    app = App()
    app.add(Route("POST /"), keep)
    async with TestClient(app, requests=RequestPolicy(max_body_bytes=20)) as client:
        response = await client.post("/", content=b"y" * 100)
        await asyncio.sleep(0)
    assert response.status_code == 200
    req = held[0]
    with pytest.raises(RequestBodyError) as info:
        await req.body()
    assert info.value.status_code == 413
    assert await req.body(max_size=100) == b"y" * 100


async def test_a_body_read_with_a_larger_limit_reads_again() -> None:
    """A second read without max_size returns the body the first read accepted."""
    held: list[Request] = []

    async def upload(c: Context, w: Writer) -> None:
        first = await c.req.body(max_size=1000)
        again = await c.req.body()
        held.append(c.req)
        w.text(f"{len(first)} {len(again)}")

    app = App()
    app.add(Route("POST /"), upload)
    async with TestClient(app, requests=RequestPolicy(max_body_bytes=20)) as client:
        response = await client.post("/", content=b"y" * 100)
        await asyncio.sleep(0)
    assert (response.status_code, response.text) == (200, "100 100")
    assert await held[0].body() == b"y" * 100
    with pytest.raises(RequestBodyError):
        await held[0].body(max_size=50)
async def test_get_route_answers_head_without_a_body() -> None:
    """RFC 9110 9.3.2: a GET route answers HEAD with the same headers."""
    seen: list[tuple[str, str]] = []

    async def page(c: Context, w: Writer) -> None:
        seen.append((c.req.method, c.match.pattern))
        w.text("hello world")

    async def stream(_c: Context, w: Writer) -> None:
        w.write_headers(200)
        w.write(b"alpha")
        w.write(b"bravo")
        w.end()

    app = App()
    app.add(Route("GET /page/{id}"), page)
    app.add(Route("GET /stream"), stream)
    async with TestClient(app) as client:
        get = await client.get("/page/1")
        head = await client.head("/page/1")
        streamed = await client.head("/stream")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers.get("content-length") == get.headers.get("content-length")
    assert head.headers.get("content-type") == get.headers.get("content-type")
    assert seen == [("GET", "GET /page/{id}"), ("HEAD", "GET /page/{id}")]
    assert streamed.status_code == 200
    assert streamed.content == b""


async def test_explicit_head_route_wins_over_get() -> None:
    async def page(_c: Context, w: Writer) -> None:
        w.text("page")

    async def head_only(_c: Context, w: Writer) -> None:
        w.headers.set("x-head", "1")
        w.empty(204)

    app = App()
    app.add(Route("HEAD /page"), head_only)
    app.add(Route("GET /page"), page)
    async with TestClient(app) as client:
        head = await client.head("/page")
        get = await client.get("/page")
    assert (head.status_code, head.headers.get("x-head")) == (204, "1")
    assert (get.status_code, get.text) == (200, "page")


async def test_head_runs_the_get_route_middleware() -> None:
    calls: list[str] = []

    def track(handler):
        async def wrapped(c: Context, w: Writer) -> None:
            calls.append(c.req.method)
            await handler(c, w)

        return wrapped

    async def page(_c: Context, w: Writer) -> None:
        w.text("page")

    app = App()
    app.use("/admin", track)
    app.add(Route("GET /admin/page"), page)
    async with TestClient(app) as client:
        head = await client.head("/admin/page")
        post = await client.post("/admin/page")
    assert head.status_code == 200
    assert post.status_code == 405
    assert post.headers.get("allow") == "GET, HEAD"
    assert calls == ["HEAD", "POST"]
