"""TestClient runs the production protocol: what a test sees is wire truth."""

from __future__ import annotations

import asyncio

import pytest

from stario import App, Context, Route, Writer
from stario.http.compression import CompressionConfig
from stario.testing import TestClient
from stario.testing.wire import ResponseReader


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
