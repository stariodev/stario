"""Shared redirect encoding used by responses and Datastar SSE."""

import pytest

from stario.exceptions import StarioError
from stario.http.redirect import normalized_location
from tests.helpers import make_writer_raw

SAFE_REDIRECT_TARGETS = [
    "/dashboard",
    "/dashboard with spaces?tab=team settings#profile section",
    "//api.example.com/v1/users",
]

UNSAFE_REDIRECT_TARGETS = [
    r"/\\evil",
    "/safe\r\nx-header: injected",
    "login",
    "javascript:alert(1)",
    "ftp://example.com/file",
]

UNSAFE_REDIRECT_MATCH = "control characters|backslashes|app-relative path or absolute"


class TestNormalizedLocation:
    @pytest.mark.parametrize("target", SAFE_REDIRECT_TARGETS)
    def test_accepts_safe_targets(self, target: str) -> None:
        assert normalized_location(target)

    def test_accepts_route_host_href(self) -> None:
        from stario import Route

        target = Route("GET //{tenant}.example.com/users/{user_id}").href(
            tenant="acme", user_id="42"
        )
        assert normalized_location(target) == target

    @pytest.mark.parametrize("target", UNSAFE_REDIRECT_TARGETS)
    def test_rejects_unsafe_targets(self, target: str) -> None:
        with pytest.raises(StarioError, match=UNSAFE_REDIRECT_MATCH):
            normalized_location(target)


def test_redirect_rejects_non_3xx_status() -> None:
    w, _sink, loop = make_writer_raw()
    try:
        with pytest.raises(StarioError, match="3xx"):
            w.redirect("/ok", 200)
    finally:
        loop.close()


class TestDatastarRedirect:
    """A Datastar request gets `navigate` instead of a 3xx it would not follow."""

    @staticmethod
    def _app():
        from stario import App, Context, RedirectException, Route, Writer
        from stario.datastar import SSE

        async def save(_c: Context, w: Writer) -> None:
            w.redirect("/done?x=1 2", 303)

        async def guarded(_c: Context, _w: Writer) -> None:
            raise RedirectException(303, "/login")

        async def live(_c: Context, w: Writer) -> None:
            SSE(w).patch_signals({"n": 1})
            w.redirect("/login")

        async def done(_c: Context, w: Writer) -> None:
            w.text("done")

        app = App()
        app.add(Route("POST /save"), save)
        app.add(Route("POST /guarded"), guarded)
        app.add(Route("GET /live"), live)
        app.add(Route("GET /done"), done)
        return app

    async def test_writer_redirect_sends_navigate(self) -> None:
        from stario.testing import TestClient

        async with TestClient(self._app()) as client:
            r = await client.post("/save", headers={"datastar-request": "true"})
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("text/event-stream")
        assert r.headers.get("location") is None
        assert "event: datastar-patch-elements" in r.text
        assert 'window.location = "/done?x=1%202"' in r.text

    async def test_redirect_exception_sends_navigate(self) -> None:
        from stario.testing import TestClient

        async with TestClient(self._app()) as client:
            r = await client.post("/guarded", headers={"datastar-request": "true"})
        assert r.status_code == 200
        assert 'window.location = "/login"' in r.text

    async def test_open_stream_gets_navigate_as_last_event(self) -> None:
        from stario.testing import TestClient

        async with TestClient(self._app()) as client:
            r = await client.get("/live", headers={"datastar-request": "true"})
        assert r.status_code == 200
        assert r.text.index("datastar-patch-signals") < r.text.index("window.location")

    async def test_plain_request_still_gets_the_3xx(self) -> None:
        from stario.testing import TestClient

        async with TestClient(self._app(), follow_redirects=False) as client:
            saved = await client.post("/save")
            guarded = await client.post("/guarded")
        assert saved.status_code == 303
        assert saved.headers.get("location") == "/done?x=1%202"
        assert guarded.status_code == 303
        assert guarded.headers.get("location") == "/login"

    async def test_unsafe_target_still_raises_for_datastar(self) -> None:
        from stario import App, Context, Route, Writer
        from stario.testing import TestClient

        errors: list[Exception] = []

        async def bad(_c: Context, w: Writer) -> None:
            try:
                w.redirect("javascript:alert(1)")
            except StarioError as exc:
                errors.append(exc)
                w.text("refused", 400)

        app = App()
        app.add(Route("POST /bad"), bad)
        async with TestClient(app) as client:
            r = await client.post("/bad", headers={"datastar-request": "true"})
        assert r.status_code == 400
        assert errors
