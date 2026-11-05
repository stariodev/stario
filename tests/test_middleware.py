"""Tests for catch_errors middleware and framework-mapped exceptions."""

import asyncio
import logging

import pytest

from stario import App, Route
from stario.exceptions import RedirectException, RequestBodyError, StarioError
from stario.http.context import Context
from stario.http.middleware import catch_errors, same_origin, security_headers
from stario.http.writer import Writer
from stario.testing import TestClient
from tests.helpers import run_with_app


class TestCatchErrors:
    def test_maps_listed_exception_when_nothing_sent(self):
        class AppError(Exception):
            pass

        async def respond(_c: Context, w: Writer, exc: BaseException) -> None:
            w.text(str(exc), 422)

        async def handler(_c: Context, _w: Writer) -> None:
            raise AppError("nope")

        def setup(app) -> None:
            app.use("/", catch_errors(AppError, respond=respond))
            app.add(Route("GET /x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 422
        assert writer.body == "nope"
        assert writer.completed

    def test_re_raises_after_response_started(self, caplog):
        class AppError(Exception):
            pass

        async def respond(_c: Context, w: Writer, _exc: BaseException) -> None:
            w.text("handled", 422)

        async def handler(_c: Context, w: Writer) -> None:
            w.respond(b"partial", b"text/plain", 200)
            raise AppError("after write")

        async def run() -> None:
            app = App()
            app.use("/", catch_errors(AppError, respond=respond))
            app.add(Route("GET /x"), handler)
            async with TestClient(app) as client:
                response = await client.get("/x")
            assert response.status_code == 200
            assert response.text == "partial"
            # Traced request: the failure lives on the span, not the logger.
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.error == "after write"

        with caplog.at_level(logging.ERROR, logger="stario.http"):
            asyncio.run(run())

        assert "Handler failed" not in caplog.text


class TestFrameworkExceptions:
    """The protocol answers RedirectException/RequestBodyError itself."""

    def test_request_body_error_maps_to_its_status(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RequestBodyError(413, "too big")

        async def run() -> None:
            app = App()
            app.add(Route("POST /upload"), handler)
            async with TestClient(app) as client:
                response = await client.post("/upload")
            assert response.status_code == 413
            assert response.text == "too big"
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.ok is True

        asyncio.run(run())

    def test_redirect_exception_maps_to_its_location(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RedirectException(301, "/new")

        async def run() -> None:
            app = App()
            app.add(Route("GET /old"), handler)
            async with TestClient(app) as client:
                response = await client.get("/old", follow_redirects=False)
            assert response.status_code == 301
            assert response.headers.get("location") == "/new"
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.ok is True

        asyncio.run(run())

    def test_redirect_exception_after_response_started_keeps_it(self, caplog):
        async def handler(_c: Context, w: Writer) -> None:
            w.text("kept")
            raise RedirectException(301, "/new")

        async def run() -> None:
            app = App()
            app.add(Route("GET /old"), handler)
            async with TestClient(app) as client:
                response = await client.get("/old", follow_redirects=False)
            assert response.status_code == 200
            assert response.text == "kept"
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.error

        with caplog.at_level(logging.ERROR, logger="stario.http"):
            asyncio.run(run())

        assert "Handler failed" not in caplog.text


class TestSameOrigin:
    """`same_origin` blocks cross-origin state-changing requests (403)."""

    def _app(self, **kwargs) -> App:
        app = App()

        async def handler(_c: Context, w: Writer) -> None:
            w.text("ok")

        app.use("/", same_origin(**kwargs))
        app.add(Route("POST /x"), handler)
        app.add(Route("GET /x"), handler)
        return app

    async def _post(self, app: App, **headers: str):
        async with TestClient(app) as client:
            return await client.post("/x", headers=headers)

    async def test_same_origin_fetch_site_allowed(self):
        response = await self._post(self._app(), **{"sec-fetch-site": "same-origin"})
        assert response.status_code == 200

    async def test_none_fetch_site_allowed(self):
        response = await self._post(self._app(), **{"sec-fetch-site": "none"})
        assert response.status_code == 200

    async def test_cross_site_fetch_site_blocked(self):
        response = await self._post(self._app(), **{"sec-fetch-site": "cross-site"})
        assert response.status_code == 403
        assert response.text == "Cross-origin request blocked"

    async def test_same_site_blocked_by_default_allowed_when_opted_in(self):
        response = await self._post(self._app(), **{"sec-fetch-site": "same-site"})
        assert response.status_code == 403
        response = await self._post(
            self._app(allow_same_site=True), **{"sec-fetch-site": "same-site"}
        )
        assert response.status_code == 200

    async def test_safe_method_skips_fetch_site_check(self):
        app = self._app()
        async with TestClient(app) as client:
            response = await client.get("/x", headers={"sec-fetch-site": "cross-site"})
        assert response.status_code == 200

    async def test_origin_matching_host_allowed(self):
        response = await self._post(
            self._app(),
            **{"origin": "https://example.com", "host": "example.com"},
        )
        assert response.status_code == 200

    async def test_origin_default_port_normalised(self):
        response = await self._post(
            self._app(),
            **{"origin": "https://example.com:443", "host": "example.com"},
        )
        assert response.status_code == 200

    async def test_origin_mismatched_host_blocked(self):
        response = await self._post(
            self._app(),
            **{"origin": "https://evil.example.com", "host": "example.com"},
        )
        assert response.status_code == 403

    async def test_origin_null_blocked(self):
        response = await self._post(
            self._app(), **{"origin": "null", "host": "example.com"}
        )
        assert response.status_code == 403

    async def test_no_headers_allowed(self):
        # Non-browser clients send neither Sec-Fetch-Site nor Origin.
        response = await self._post(self._app())
        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("origin", "status"),
        [
            ("https://pay.example.com", 200),
            ("https://PAY.example.com:443", 200),
            ("http://pay.example.com", 403),
            ("https://evil.example.com", 403),
            ("null", 403),
        ],
    )
    async def test_trusted_origins_pass_cross_site(self, origin: str, status: int):
        app = self._app(trusted_origins=["https://pay.example.com"])
        cross_site = await self._post(
            app, **{"sec-fetch-site": "cross-site", "origin": origin}
        )
        assert cross_site.status_code == status
        no_fetch_site = await self._post(
            self._app(trusted_origins=["https://pay.example.com"]),
            **{"origin": origin, "host": "example.com"},
        )
        assert no_fetch_site.status_code == status

    @pytest.mark.parametrize(
        "value", ["pay.example.com", "https://pay.example.com/return", "null", ""]
    )
    def test_trusted_origins_must_be_origins(self, value: str):
        with pytest.raises(StarioError, match="trusted_origins"):
            same_origin(trusted_origins=[value])

    @pytest.mark.parametrize(
        ("path", "status"),
        [("/hooks", 200), ("/hooks/github", 200), ("/hooksx", 403), ("/x", 403)],
    )
    async def test_exempt_paths_skip_the_check(self, path: str, status: int):
        app = App()

        async def handler(_c: Context, w: Writer) -> None:
            w.text("ok")

        app.use("/", same_origin(exempt=["/hooks/"]))
        app.add(Route(f"POST {path}"), handler)
        async with TestClient(app) as client:
            response = await client.post(path, headers={"sec-fetch-site": "cross-site"})
        assert response.status_code == status

    @pytest.mark.parametrize("path", ["/hooks%2Fx", "/hooks%2F..%2Faccount"])
    async def test_exempt_prefix_matches_the_raw_path(self, path: str):
        # The decoded path starts with /hooks/, but the router matches one
        # segment (`/{page}`) outside /hooks: the check must still run.
        app = App()

        async def handler(_c: Context, w: Writer) -> None:
            w.text("ok")

        app.use("/", same_origin(exempt=["/hooks"]))
        app.add(Route("POST /{page}"), handler)
        async with TestClient(app) as client:
            response = await client.post(path, headers={"sec-fetch-site": "cross-site"})
        assert response.status_code == 403

    def test_exempt_paths_must_be_absolute(self):
        with pytest.raises(StarioError, match="exempt"):
            same_origin(exempt=["hooks"])


class TestSecurityHeaders:
    def _app(self, **kwargs) -> App:
        app = App()

        async def handler(_c: Context, w: Writer) -> None:
            w.text("ok")

        app.use("/", security_headers(**kwargs))
        app.add(Route("GET /x"), handler)
        return app

    async def test_defaults(self):
        async with TestClient(self._app()) as client:
            response = await client.get("/x")
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert response.headers.get("x-frame-options") == "DENY"
        assert (
            response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
        )
        assert response.headers.get("strict-transport-security") is None

    async def test_hsts_and_sameorigin(self):
        app = self._app(frame="SAMEORIGIN", referrer="no-referrer", hsts=31536000)
        async with TestClient(app) as client:
            response = await client.get("/x")
        assert response.headers.get("x-frame-options") == "SAMEORIGIN"
        assert response.headers.get("referrer-policy") == "no-referrer"
        assert (
            response.headers.get("strict-transport-security")
            == "max-age=31536000; includeSubDomains"
        )

    async def test_none_disables_optional_headers(self):
        app = self._app(frame=None, referrer=None)
        async with TestClient(app) as client:
            response = await client.get("/x")
        assert response.headers.get("x-frame-options") is None
        assert response.headers.get("referrer-policy") is None
        # nosniff stays on.
        assert response.headers.get("x-content-type-options") == "nosniff"

    async def test_handler_may_override(self):
        app = App()

        async def handler(_c: Context, w: Writer) -> None:
            w.headers.set("x-frame-options", "SAMEORIGIN")
            w.text("ok")

        app.use("/", security_headers(frame="DENY"))
        app.add(Route("GET /x"), handler)
        async with TestClient(app) as client:
            response = await client.get("/x")
        assert response.headers.get("x-frame-options") == "SAMEORIGIN"
