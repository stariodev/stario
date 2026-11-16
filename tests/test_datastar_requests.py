"""A Datastar request is answered with SSE events or a 204, never a redirect.

`is_datastar` tells the two kinds of request apart, `w.redirect` refuses a
Datastar request, and the TestClient raises on a reply Datastar ignores.
"""

from __future__ import annotations

import pytest

from stario import App, Context, RedirectException, Route, Writer, is_datastar
from stario.datastar import SSE, at, data
from stario.markup import html as h
from stario.testing import DatastarReplyError, TestClient, check_pages

DS = {"datastar-request": "true"}


def _app() -> App:
    async def kind(c: Context, w: Writer) -> None:
        w.text("datastar" if is_datastar(c.req) else "page")

    async def guard(c: Context, w: Writer) -> None:
        if is_datastar(c.req):
            SSE(w).navigate("/login")
        else:
            w.redirect("/login", 303)

    async def plain_redirect(_c: Context, w: Writer) -> None:
        w.redirect("/login", 303)

    async def raised_redirect(_c: Context, _w: Writer) -> None:
        raise RedirectException(303, "/login")

    async def missing(_c: Context, w: Writer) -> None:
        w.text("not found", 404)

    async def nothing(_c: Context, w: Writer) -> None:
        w.empty()

    app = App()
    app.add(Route("GET /kind"), kind)
    app.add(Route("POST /guard"), guard)
    app.add(Route("POST /redirect"), plain_redirect)
    app.add(Route("POST /raised"), raised_redirect)
    app.add(Route("POST /missing"), missing)
    app.add(Route("POST /nothing"), nothing)
    return app


async def test_is_datastar_reads_the_header() -> None:
    async with TestClient(_app()) as client:
        assert (await client.get("/kind")).text == "page"
        assert (await client.get("/kind", headers=DS)).text == "datastar"


async def test_guard_branches_on_is_datastar() -> None:
    async with TestClient(_app(), follow_redirects=False) as client:
        page = await client.post("/guard")
        action = await client.post("/guard", headers=DS)
    assert page.status_code == 303
    assert page.headers.get("location") == "/login"
    assert action.status_code == 200
    assert "window.location" in action.text


async def test_plain_request_redirects_as_before() -> None:
    async with TestClient(_app(), follow_redirects=False) as client:
        response = await client.post("/redirect")
    assert response.status_code == 303


@pytest.mark.parametrize("path", ["/redirect", "/raised"])
async def test_redirect_on_a_datastar_request_raises_with_the_fix(path: str) -> None:
    async with TestClient(_app(), follow_redirects=False) as client:
        with pytest.raises(DatastarReplyError) as info:
            await client.post(path, headers=DS)
        assert info.value.status_code == 500
        span = client.tracer.get_span(client.exchanges[-1].response.span_id)
    # The span carries the StarioError with the fix, not the bare redirect.
    assert span is not None
    assert span.error is not None
    assert "Datastar does not follow redirects" in span.error
    assert "SSE(w).navigate(url)" in span.error
    assert "is_datastar(c.req)" in span.error


async def test_client_raises_on_a_reply_datastar_ignores() -> None:
    async with TestClient(_app()) as client:
        with pytest.raises(DatastarReplyError, match="Datastar ignores a 404") as info:
            await client.post("/missing", headers=DS)
        assert info.value.status_code == 404
        assert "not found" in str(info.value)
        assert "navigate" in (info.value.help_text or "")
        # The same handler without the header is plain HTTP.
        assert (await client.post("/missing")).status_code == 404
        assert (await client.post("/nothing", headers=DS)).status_code == 204


async def test_stream_raises_too() -> None:
    async with TestClient(_app()) as client:
        with pytest.raises(DatastarReplyError):
            async with client.stream("POST", "/missing", headers=DS):
                pass


async def test_check_pages_reports_a_stream_datastar_ignores() -> None:
    live = Route("GET /live")

    async def home(_c: Context, w: Writer) -> None:
        w.html(h.Html(h.Body(h.Div({"id": "feed"}, data.init(at.get(live.href()))))))

    async def stream(_c: Context, w: Writer) -> None:
        w.text("gone", 404)

    app = App()
    app.add(Route("GET /"), home)
    app.add(live, stream)
    async with TestClient(app) as client:
        problems = await check_pages(client)
    assert any("the stream /live answers 404" in p for p in problems), problems
