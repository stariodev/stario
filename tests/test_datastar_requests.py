"""`is_datastar` tells a Datastar request from a page load.

The HTTP layer does not look at the `datastar-request` header: a redirect is
a redirect, and the TestClient returns every reply as the server sent it.
"""

from __future__ import annotations

import pytest

from stario import App, Context, RedirectException, Route, Writer
from stario.datastar import SSE, at, data, is_datastar
from stario.datastar.testing import datastar_checks
from stario.markup import html as h
from stario.testing import TestClient, check_pages

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
async def test_redirect_ignores_the_datastar_header(path: str) -> None:
    async with TestClient(_app(), follow_redirects=False) as client:
        response = await client.post(path, headers=DS)
    assert response.status_code == 303
    assert response.headers.get("location") == "/login"


async def test_client_returns_every_reply_as_sent() -> None:
    async with TestClient(_app()) as client:
        missing = await client.post("/missing", headers=DS)
        nothing = await client.post("/nothing", headers=DS)
        async with client.stream("POST", "/missing", headers=DS) as streamed:
            streamed_status = streamed.status_code
    assert missing.status_code == 404
    assert missing.text == "not found"
    assert nothing.status_code == 204
    assert streamed_status == 404


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
        plain = await check_pages(client)
        problems = await check_pages(client, checks=datastar_checks())
    assert not plain
    assert any("the stream /live answers 404" in p for p in problems), problems
