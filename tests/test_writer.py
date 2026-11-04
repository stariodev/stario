"""Tests for the public test client and low-level writer fallbacks."""

import asyncio
import json

import pytest

import stario.cookies as cookies
from stario import App, Route
from stario.datastar import SSE
from stario.exceptions import StarioRuntime
from stario.markup import html as h
from stario.testing import TestClient
from tests.helpers import TestWriter
from tests.helpers import (
    make_context as _make_context,
)


def _make_app() -> App:
    app = App()

    async def text_route(c, w):
        w.text("Hello, World!")

    async def html_route(c, w):
        w.html(h.H1("Hello"))

    async def json_route(c, w):
        w.json({"message": "hello"})

    async def redirect_route(c, w):
        w.redirect("/final", 302)

    async def final_route(c, w):
        w.text("redirected")

    async def redirect_cookie_route(c, w):
        cookies.set_cookie(w, "session", "fresh")
        w.redirect("/redirect-cookie/final", 302)

    async def redirect_cookie_final(c, w):
        w.text(c.req.cookies.get("session") or "missing")

    async def json_echo(c, w):
        w.json(json.loads(await c.req.body()))

    async def form_echo(c, w):
        w.json(
            {
                "body": (await c.req.body()).decode(),
                "content_type": c.req.headers.get("content-type"),
            },
        )

    async def upload_echo(c, w):
        w.text((await c.req.body()).decode("utf-8", errors="replace"))

    async def login(c, w):
        cookies.set_cookie(w, "session", "abc123", httponly=True)
        cookies.set_cookie(w, "theme", "dark")
        w.empty()

    async def me(c, w):
        w.json(
            {
                "session": c.req.cookies.get("session"),
                "theme": c.req.cookies.get("theme"),
            },
        )

    async def telemetry_route(c, w):
        c.span.event("handler.hit", {"path": c.req.path})
        w.json({"ok": True})

    async def error_route(c, w):
        w.text("teapot", 418)

    app.add(Route("GET /"), text_route)
    app.add(Route("GET /html"), html_route)
    app.add(Route("GET /json"), json_route)
    app.add(Route("GET /redirect"), redirect_route)
    app.add(Route("GET /final"), final_route)
    app.add(Route("GET /redirect-cookie"), redirect_cookie_route)
    app.add(Route("GET /redirect-cookie/final"), redirect_cookie_final)
    app.add(Route("POST /json-echo"), json_echo)
    app.add(Route("POST /form-echo"), form_echo)
    app.add(Route("POST /upload"), upload_echo)
    app.add(Route("GET /login"), login)
    app.add(Route("GET /me"), me)
    app.add(Route("GET /telemetry"), telemetry_route)
    app.add(Route("GET /error"), error_route)
    return app


@pytest.mark.asyncio
class TestClientBasics:
    async def test_sync_client_handles_text_html_and_json(self):
        async with TestClient(_make_app()) as client:
            text_response = await client.get("/")
            html_response = await client.get("/html")
            json_response = await client.get("/json")

        assert text_response.status_code == 200
        assert text_response.text == "Hello, World!"
        assert text_response.headers.get("content-type") == "text/plain; charset=utf-8"

        assert html_response.status_code == 200
        assert html_response.text == "<h1>Hello</h1>"
        assert html_response.headers.get("content-type") == "text/html; charset=utf-8"

        assert json_response.status_code == 200
        assert json_response.json() == {"message": "hello"}
        assert (
            json_response.headers.get("content-type")
            == "application/json; charset=utf-8"
        )

    async def test_client_follows_redirects_by_default(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/redirect")

        assert response.status_code == 200
        assert response.text == "redirected"
        assert response.url == "http://testserver/final"
        assert len(response.history) == 1
        assert response.history[0].status_code == 302
        assert response.history[0].headers.get("location") == "/final"

    async def test_client_can_disable_redirect_following(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/redirect", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers.get("location") == "/final"

    async def test_redirects_merge_response_cookies_into_request_cookie_overrides(self):
        async with TestClient(_make_app()) as client:
            response = await client.get(
                "/redirect-cookie", cookies={"session": "stale"}
            )

        assert response.status_code == 200
        assert response.text == "fresh"

    async def test_client_sends_query_json_form_and_file_payloads(self):
        async with TestClient(_make_app()) as client:
            json_response = await client.post("/json-echo", json={"name": "Ada"})
            form_response = await client.post(
                "/form-echo", data={"active": True, "count": 2}
            )
            upload_response = await client.post(
                "/upload",
                data={"kind": "avatar"},
                files={"file": ("avatar.txt", "hello", "text/plain")},
            )

        assert json_response.json() == {"name": "Ada"}
        assert form_response.json() == {
            "body": "active=true&count=2",
            "content_type": "application/x-www-form-urlencoded",
        }
        assert 'name="kind"' in upload_response.text
        assert 'name="file"; filename="avatar.txt"' in upload_response.text
        assert "Content-Type: text/plain" in upload_response.text
        assert "hello" in upload_response.text

    async def test_client_persists_cookies_across_requests(self):
        async with TestClient(_make_app()) as client:
            login_response = await client.get("/login")
            profile_response = await client.get("/me")

        assert login_response.status_code == 204
        assert login_response.cookies == {"session": "abc123", "theme": "dark"}
        assert profile_response.json() == {"session": "abc123", "theme": "dark"}

    async def test_client_records_request_exchange_and_telemetry(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/telemetry")

        rid = response.span_id
        root = client.tracer.get_span(rid)
        assert root is not None
        assert root.attributes["request.method"] == "GET"
        assert root.attributes["request.path"] == "/telemetry"
        assert root.attributes["response.status_code"] == 200
        ev = client.tracer.get_event(rid, "handler.hit")
        assert ev is not None
        assert ev.attributes == {"path": "/telemetry"}

        exchange = client.exchanges[-1]
        assert exchange.request.path == "/telemetry"
        assert exchange.request.url == "http://testserver/telemetry"
        assert exchange.response is response

    async def test_client_exit_drains_app_tasks(self):
        app = App()
        state = {"n": 0}

        async def handler(c, w):
            async def bg():
                await asyncio.sleep(0.02)
                state["n"] = 1

            c.app.create_task(bg())
            w.text("ok")

        app.add(Route("GET /"), handler)
        async with TestClient(app) as client:
            await client.get("/")
            assert state["n"] == 0
        assert state["n"] == 1

    async def test_client_exit_signals_app_shutdown_before_draining_tasks(self):
        app = App()
        stopped = asyncio.Event()

        async def handler(c, w):
            async def bg():
                await c.app.shutdown
                stopped.set()

            c.app.create_task(bg())
            w.text("ok")

        app.add(Route("GET /"), handler)
        async with asyncio.timeout(0.5):
            async with TestClient(app) as client:
                await client.get("/")

        assert stopped.is_set()

    async def test_redirect_loop_raises_after_max_redirects(self):
        app = App()

        async def loop_route(c, w):
            w.redirect("/loop", 302)

        app.add(Route("GET /loop"), loop_route)
        async with TestClient(app, max_redirects=3) as client:
            with pytest.raises(RuntimeError, match="Too many redirects"):
                await client.get("/loop")

    @pytest.mark.parametrize(
        ("client_timeout", "request_timeout"),
        [
            (0.01, None),
            (None, 0.01),
        ],
    )
    async def test_request_timeout_default_applies(
        self, client_timeout, request_timeout
    ):
        app = App()

        async def slow(c, w):
            await asyncio.sleep(10)
            w.text("late")

        app.add(Route("GET /slow"), slow)
        async with TestClient(app, request_timeout=client_timeout) as client:
            with pytest.raises(TimeoutError):
                await client.get("/slow", timeout=request_timeout)

    async def test_response_raise_for_status(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/error")

        with pytest.raises(RuntimeError, match="418 I'm a Teapot: teapot"):
            response.raise_for_status()

    async def test_stream_iter_bytes_chunked(self):
        app = App()

        async def handler(c, w):
            w.write_headers(200)
            w.write(b"hel")
            w.write(b"lo")
            w.end()

        app.add(Route("GET /s"), handler)
        async with (
            TestClient(app) as client,
            client.stream("GET", "/s", headers={"Accept-Encoding": "identity"}) as r,
        ):
            assert r.status_code == 200
            parts = [c async for c in r.iter_bytes()]
        assert b"".join(parts) == b"hello"

    async def test_stream_iter_events(self):
        app = App()

        async def handler(c, w):
            w.headers.set("content-type", "text/event-stream")
            w.write_headers(200)
            w.write(b"event: ping\ndata: hello\n\n")
            w.end()

        app.add(Route("GET /e"), handler)
        async with (
            TestClient(app) as client,
            client.stream("GET", "/e", headers={"Accept-Encoding": "identity"}) as r,
        ):
            evs = [e async for e in r.iter_events()]
        assert evs == [{"event": "ping", "data": "hello"}]

    async def test_client_exit_signals_context_disconnect(self):
        app = App()
        done = asyncio.Event()

        async def handler(c, w):
            async def watch():
                while not c.disconnected:
                    await asyncio.sleep(0)
                done.set()

            c.app.create_task(watch())
            w.text("ok")

        app.add(Route("GET /"), handler)
        async with TestClient(app) as client:
            r = await client.get("/")
            assert r.status_code == 200
            assert not done.is_set()
        assert done.is_set()


class TestWriterRaw:
    async def test_context_alive_exits_when_connection_closes(self):
        loop = asyncio.get_running_loop()
        disconnect = loop.create_future()
        context = _make_context(loop=loop, disconnect=disconnect)
        started = asyncio.Event()
        stopped = asyncio.Event()
        never = asyncio.Event()

        async def worker() -> None:
            async with context.alive():
                started.set()
                await never.wait()
            stopped.set()

        task = asyncio.create_task(worker())
        await started.wait()

        disconnect.set_result(None)
        await asyncio.wait_for(stopped.wait(), timeout=0.1)
        await task

    async def test_context_alive_leaves_no_callback_on_app_shutdown(self):
        """Each scope reads a new `app.shutdown` shield and must unhook it."""
        loop = asyncio.get_running_loop()
        context = _make_context(loop=loop)
        shared = context.app._future_for(loop)
        before = len(shared._callbacks or ())

        for _ in range(100):
            async with context.alive():
                await asyncio.sleep(0)

        assert len(shared._callbacks or ()) == before

    async def test_app_shutdown_link_unhooks_on_cancel_and_timeout(self):
        """`wait_for(app.shutdown, t)` at its timeout leaves nothing behind."""
        loop = asyncio.get_running_loop()
        app = App()
        shared = app._future_for(loop)

        for _ in range(100):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(app.shutdown, 0)
        link = app.shutdown
        link.cancel()
        await asyncio.sleep(0)

        assert len(shared._callbacks or ()) == 0
        assert not shared.cancelled()
        waiter = app.shutdown
        app.signal_shutdown()
        await asyncio.wait_for(waiter, 1)
        assert app.shutdown.done()

    async def test_context_closing_combines_disconnect_and_shutdown(self):
        loop = asyncio.get_running_loop()
        context = _make_context(loop=loop)

        assert not context.closing

        context.app.signal_shutdown()

        assert context.shutting_down
        assert context.closing

    async def test_context_alive_does_not_swallow_unrelated_cancellation(self):
        loop = asyncio.get_running_loop()
        context = _make_context(loop=loop)
        started = asyncio.Event()
        never = asyncio.Event()

        async def worker() -> None:
            async with context.alive():
                started.set()
                await never.wait()

        task = asyncio.create_task(worker())
        await started.wait()

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_context_alive_raced_cancellation_still_propagates(self):
        # A foreign cancel in flight when disconnect fires must propagate —
        # the watcher does not stack a second cancel on an unwinding task.
        loop = asyncio.get_running_loop()
        disconnect = loop.create_future()
        context = _make_context(loop=loop, disconnect=disconnect)
        started = asyncio.Event()
        never = asyncio.Event()

        async def worker() -> None:
            async with context.alive():
                started.set()
                await never.wait()

        task = asyncio.create_task(worker())
        await started.wait()

        task.cancel()
        await asyncio.sleep(0)
        disconnect.set_result(None)

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_context_alive_without_source_raises(self):
        loop = asyncio.get_running_loop()
        context = _make_context(loop=loop)

        with pytest.raises(RuntimeError, match=r"async with c\.alive"):
            async for _ in context.alive():
                pass

    async def test_context_alive_iterates_source_until_disconnect(self):
        loop = asyncio.get_running_loop()
        disconnect = loop.create_future()
        context = _make_context(loop=loop, disconnect=disconnect)

        async def source():
            yield "a"
            yield "b"
            await asyncio.Event().wait()

        collected: list[str] = []

        async def worker() -> None:
            async for item in context.alive(source()):
                collected.append(item)
                if len(collected) == 2:
                    disconnect.set_result(None)

        await asyncio.wait_for(worker(), timeout=0.2)
        assert collected == ["a", "b"]

    def test_end_without_data_is_204(self):
        w = TestWriter()
        w.end()
        assert w.status_code == 204
        assert w.body == b""

    def test_closing_follows_disconnect(self):
        loop = asyncio.new_event_loop()
        try:
            disconnect = loop.create_future()
            w = TestWriter(disconnect=disconnect)
            assert not w.closing
            disconnect.set_result(None)
            assert w.closing
        finally:
            loop.close()

    def test_write_after_204_raises(self):
        w = TestWriter()
        w.write_headers(204)
        with pytest.raises(RuntimeError, match="Cannot write a body"):
            w.write(b"data")

    def test_sse_rejects_non_event_stream_after_headers_started(self):
        w = TestWriter()
        w.headers.set("content-type", "text/html")
        w.write_headers(200)
        with pytest.raises(StarioRuntime, match="text/event-stream"):
            SSE(w).patch_signals({"x": 1})

    def test_write_headers_twice_raises(self):
        w = TestWriter()
        w.write_headers(200)
        with pytest.raises(RuntimeError, match="already started"):
            w.write_headers(200)

    def test_respond_after_started_raises(self):
        w = TestWriter()
        w.write_headers(200)
        with pytest.raises(RuntimeError, match="already started"):
            w.respond(b"late", b"text/plain")

    def test_write_after_end_raises(self):
        w = TestWriter()
        w.respond(b"ok", b"text/plain")
        with pytest.raises(RuntimeError, match="completed"):
            w.write(b"extra")

    def test_respond_keeps_custom_headers_and_cookies(self):
        w = TestWriter()
        cookies.set_cookie(w, "session", "abc123")
        cookies.set_cookie(w, "theme", "dark")
        w.headers.set("X-Request-ID", "in-1")
        w.respond(b"ok", b"text/plain")
        assert w.headers.get("x-request-id") == "in-1"
        assert w.headers.get("content-type") == "text/plain"
        lines = w.headers.unsafe_getlist(b"set-cookie")
        assert len(lines) == 2
        assert w.body == b"ok"
