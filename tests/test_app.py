"""Tests for app-level routing and host dispatch."""

import logging

import pytest

from stario import Route
from stario.exceptions import StarioError
from stario.http.app import App
from stario.http.context import Context
from stario.http.middleware import catch_errors
from stario.http.writer import Writer
from stario.testing import TestClient
from tests.helpers import run_with_app


def test_eager_create_task_uses_direct_task_with_uvloop() -> None:
    uvloop = pytest.importorskip("uvloop")

    async def run() -> None:
        app = App()

        async def complete_immediately() -> int:
            return 42

        task = app.create_task(complete_immediately(), eager_start=True)

        assert task.done()
        assert task.result() == 42

    uvloop.run(run())


class TestHostRouting:
    def test_wildcard_host_uses_route_params(self):
        seen: list[tuple[str, str]] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append((c.match.params["subhost"], c.match.pattern))
            w.end()

        def setup(app: App) -> None:
            app.add(Route("GET //{subhost}.example.com/dashboard"), handler)

        context, _writer = run_with_app(setup, "/dashboard", host="acme.example.com")

        assert dict(context.match.params) == {"subhost": "acme"}
        assert context.match.pattern == "GET //{subhost}.example.com/dashboard"
        assert seen == [("acme", "GET //{subhost}.example.com/dashboard")]

    @pytest.mark.asyncio
    async def test_trailing_slash_redirect_preserves_query_string(self):
        async with TestClient(App()) as client:
            response = await client.get(
                "/search/", params={"q": "cats", "page": 2}, follow_redirects=False
            )

        assert response.status_code == 308
        assert response.headers.get("location") == "/search?q=cats&page=2"

    @pytest.mark.asyncio
    async def test_trailing_slash_redirect_finishes_span(self):
        app = App()

        async def search(_c: Context, w: Writer) -> None:
            w.respond(b"ok", b"text/plain", 200)

        app.add(Route("GET /search"), search)
        async with TestClient(app) as client:
            response = await client.get("/search/?q=cats", follow_redirects=False)
            assert response.status_code == 308
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.attributes.get("response.status_code") == 308
            assert span.attributes.get("request.method") == "GET"
            assert span.attributes.get("request.path") == "/search/"
            assert span.ok
            assert not client.tracer.has_open_spans()

    @pytest.mark.asyncio
    async def test_trailing_slash_on_double_slash_is_not_protocol_relative(self):
        async with TestClient(App()) as client:
            response = await client.get("//aftra.io/", follow_redirects=False)

        assert response.status_code == 308
        assert response.headers.get("location") == "/aftra.io"


class TestAppErrorSurface:
    @pytest.mark.asyncio
    async def test_unhandled_exception_writes_500(
        self, caplog: pytest.LogCaptureFixture
    ):
        async def boom(_c: Context, _w: Writer) -> None:
            raise RuntimeError("boom")

        app = App()
        app.add(Route("GET /boom"), boom)
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            async with TestClient(app) as client:
                response = await client.get("/boom")
                span = client.tracer.get_span(response.span_id)

        assert response.status_code == 500
        assert response.text == "Internal Server Error"
        # The span carries the failure — no duplicate log line.
        assert span is not None
        assert span.error == "boom"
        assert "Handler failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_unhandled_exception_is_500_on_test_client(self):
        app = App()

        async def boom(_c: Context, _w: Writer) -> None:
            raise RuntimeError("boom")

        app.add(Route("GET /boom"), boom)
        async with TestClient(app) as client:
            response = await client.get("/boom")
            assert response.status_code == 500
            assert response.text == "Internal Server Error"
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.attributes.get("response.status_code") == 500
            assert not span.ok
            assert not client.tracer.has_open_spans()

    @pytest.mark.asyncio
    async def test_handler_must_send_explicit_response(self):
        async def missing_response(_c: Context, _w: Writer) -> None:
            return None

        app = App()
        app.add(Route("GET /missing"), missing_response)
        async with TestClient(app) as client:
            response = await client.get("/missing")

        assert response.status_code == 500
        assert response.text == "Internal Server Error"

    @pytest.mark.asyncio
    async def test_write_then_raise_keeps_response_and_is_traced(
        self, caplog: pytest.LogCaptureFixture
    ):
        async def handler(_c: Context, w: Writer) -> None:
            w.respond(b"ok", b"text/plain", 200)
            raise RuntimeError("after write")

        app = App()
        app.add(Route("GET /x"), handler)
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            async with TestClient(app) as client:
                response = await client.get("/x")
                span = client.tracer.get_span(response.span_id)

        assert response.status_code == 200
        assert response.text == "ok"
        assert span is not None
        assert span.error == "after write"
        assert "Handler failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_uncaught_app_error_becomes_500_without_middleware(self):
        class AppError(Exception):
            pass

        async def handler(_c: Context, _w: Writer) -> None:
            raise AppError("nope")

        app = App()
        app.add(Route("GET /x"), handler)
        async with TestClient(app) as client:
            response = await client.get("/x")

        assert response.status_code == 500
        assert response.text == "Internal Server Error"

    def test_catch_errors_middleware_maps_app_exception(self):
        class AppError(Exception):
            pass

        async def respond(_c: Context, w: Writer, exc: BaseException) -> None:
            w.text(str(exc), 422)

        async def handler(_c: Context, _w: Writer) -> None:
            raise AppError("nope")

        def setup(app: App) -> None:
            app.use("/", catch_errors(AppError, respond=respond))
            app.add(Route("GET /x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 422
        assert writer.body == "nope"

    def test_dispatch_uses_find_handler(self):
        seen: list[tuple[str, str, str]] = []

        async def handler(_c: Context, w: Writer) -> None:
            w.respond(b"ok", b"text/plain", 200)

        def setup(app: App) -> None:
            app.add(Route("GET /x"), handler)
            original = app.find_handler

            def wrapped(host: str, path: str, method: str):
                seen.append((host, path, method))
                return original(host, path, method)

            app.find_handler = wrapped

        _context, writer = run_with_app(setup, "/x")

        assert seen == [("", "/x", "GET")]
        assert writer.status == 200
        assert writer.body == "ok"

    def test_app_requires_running_loop(self):
        with pytest.raises(StarioError, match="requires a running event loop"):
            App()


class TestRequestSpanName:
    async def test_matched_route_renames_span(self):
        async def ok(c: Context, w: Writer) -> None:
            w.empty(204)

        app = App()
        app.add(Route("GET /users/{user_id}"), ok)
        async with TestClient(app) as client:
            response = await client.get("/users/7")

        root = client.tracer.get_span(response.span_id)
        assert root is not None
        assert root.name == "GET /users/{user_id}"
        assert root.attributes["http.route"] == "/users/{user_id}"

    async def test_trailing_slash_uses_canonical_route_name(self):
        async def ok(c: Context, w: Writer) -> None:
            w.empty(204)

        app = App()
        app.add(Route("GET /search"), ok)
        async with TestClient(app) as client:
            response = await client.get("/search/", follow_redirects=False)

        assert response.status_code == 308
        root = client.tracer.get_span(response.span_id)
        assert root is not None
        assert root.name == "GET /search"

    async def test_not_found_keeps_method_name(self):
        async with TestClient(App()) as client:
            response = await client.get("/missing")

        root = client.tracer.get_span(response.span_id)
        assert root is not None
        assert root.name == "GET"
        assert "http.route" not in root.attributes
