"""stario.testing.check_pages: every HTML page of an app, checked."""

from __future__ import annotations

import unittest

import pytest

from stario import App, Context, HtmlElement, Route, Writer, h
from stario.datastar import SSE, at, data
from stario.datastar.testing import datastar_checks
from stario.markup.types import TagAttributes
from stario.testing import (
    OUTSIDE_SERVICE_ERRORS,
    HtmlPage,
    TestClient,
    check_page,
    check_pages,
)


def _page(*body: HtmlElement | TagAttributes) -> HtmlElement:
    return h.HtmlDocument(h.Body(*body))


def _good_app() -> App:
    async def home(_c: Context, w: Writer) -> None:
        w.html(
            _page(
                h.Div(data.signals({"count": 0}), h.Span(data.text("$count"))),
                h.Button(data.on("click", at.post("/inc")), "+"),
                h.A({"href": "/about"}, "About"),
                h.A({"href": "https://example.com/"}, "Out"),
                h.Div({"id": "live"}, data.init(at.get("/live")), "0"),
            )
        )

    async def about(_c: Context, w: Writer) -> None:
        w.html(_page(h.P("About")))

    async def inc(_c: Context, w: Writer) -> None:
        SSE(w).patch_signals({"count": 1})

    async def live(_c: Context, w: Writer) -> None:
        SSE(w).patch_elements(h.Div({"id": "live"}, data.init(at.get("/live")), "1"))

    async def api(_c: Context, w: Writer) -> None:
        w.json({"ok": True})

    app = App()
    app.add(Route("GET /"), home)
    app.add(Route("GET /about"), about)
    app.add(Route("POST /inc"), inc)
    app.add(Route("GET /live"), live)
    app.add(Route("GET /api"), api)
    return app


async def test_a_good_app_has_no_problems() -> None:
    async with TestClient(_good_app()) as client:
        problems = await check_pages(client, checks=datastar_checks())
    assert not problems
    assert problems.pages == ["/", "/about"]
    assert problems.skipped == []
    assert repr(problems) == "PageProblems([])"


async def test_every_problem_is_one_line() -> None:
    async def home(_c: Context, w: Writer) -> None:
        w.html(
            _page(
                h.P({"id": "x"}, "a"),
                h.P({"id": "x"}, "b"),
                h.Button(data.on("click", at.post("/missing")), "go"),
                h.Span(data.text("$nobody")),
                h.Dialog("hi"),
                h.Div(data.init(at.get("/live")), "no id"),
                h.A({"href": "/broken"}, "Broken"),
            )
        )

    async def broken(_c: Context, _w: Writer) -> None:
        raise RuntimeError("boom")

    app = App()
    app.add(Route("GET /"), home)
    app.add(Route("GET /broken"), broken)
    async with TestClient(app) as client:
        plain = await check_pages(client)
        problems = await check_pages(client, checks=datastar_checks())
    # Without checks= only the HTML checks run: no Datastar knowledge.
    assert plain == [
        "/: two elements have id='x'",
        "/broken: answers 500 (RuntimeError: boom)",
    ]
    assert problems == [
        "/: two elements have id='x'",
        "/: POST /missing has no route",
        "/: data-text reads $nobody, which no element defines",
        '/: a <dialog> has no data.preserve_attr("open")',
        "/: GET /live has no route",
        "/: <div data-init> opens a stream but has no id",
        "/broken: answers 500 (RuntimeError: boom)",
    ]
    assert "\n/broken: answers 500" in repr(problems)


async def test_stream_problems() -> None:
    async def home(_c: Context, w: Writer) -> None:
        w.html(
            _page(
                h.Div({"id": "a"}, data.init(at.get("/silent")), "a"),
                h.Div({"id": "b"}, data.init(at.get("/changes")), "b"),
                h.Div({"id": "c"}, data.init(at.get("/gone")), "c"),
                h.Div({"id": "d"}, data.init(at.get("/signals")), "d"),
            )
        )

    async def silent(c: Context, _w: Writer) -> None:
        await c.disconnect

    async def changes(_c: Context, w: Writer) -> None:
        SSE(w).patch_elements(h.Div({"id": "b"}, "lost its data-init"))

    async def signals(_c: Context, w: Writer) -> None:
        SSE(w).patch_signals({"n": 1})

    app = App()
    app.add(Route("GET /"), home)
    app.add(Route("GET /silent"), silent)
    app.add(Route("GET /changes"), changes)
    app.add(Route("GET /signals"), signals)
    async with TestClient(app) as client:
        problems = await check_pages(client, checks=datastar_checks())
        quiet = await check_pages(client, checks=datastar_checks(streams=False))
    assert problems == [
        "/: GET /gone has no route",
        "/: the stream /silent sent no event within 2 s (send the current view first)",
        '/: the stream /changes patches <div id="b" data-init="@get(\'/changes\')"> '
        "into a different tag",
        "/: the stream /gone answers 404",
    ]
    assert quiet == ["/: GET /gone has no route"]


async def test_outside_service_errors_skip_the_page() -> None:
    class ConnectError(Exception):
        pass

    async def home(_c: Context, w: Writer) -> None:
        w.html(_page(h.A({"href": "/weather"}, "Weather")))

    async def weather(_c: Context, _w: Writer) -> None:
        raise ConnectError("api.example.com refused the connection")

    app = App()
    app.add(Route("GET /"), home)
    app.add(Route("GET /weather"), weather)
    async with TestClient(app) as client:
        problems = await check_pages(client)
        strict = await check_pages(client, skip_errors=())
        by_class = await check_page(client, "/weather", skip_errors=(ConnectError,))
    assert not problems
    assert problems.skipped == [
        "/weather: needs a service that is not running "
        "(ConnectError: api.example.com refused the connection)"
    ]
    assert strict == [
        "/weather: answers 500 (ConnectError: api.example.com refused the connection)"
    ]
    assert not by_class
    assert "Connect" in OUTSIDE_SERVICE_ERRORS


async def test_home_needing_an_outside_service_is_a_skip() -> None:
    async def home(_c: Context, _w: Writer) -> None:
        raise TimeoutError("the API took too long")

    app = App()
    app.add(Route("GET /"), home)
    async with TestClient(app) as client:
        with pytest.raises(unittest.SkipTest, match="TimeoutError: the API took"):
            await check_pages(client)


async def test_missing_home_page_is_a_problem() -> None:
    async def about(_c: Context, w: Writer) -> None:
        w.html(_page(h.P("About")))

    app = App()
    app.add(Route("GET /about"), about)
    async with TestClient(app) as client:
        problems = await check_pages(client)
        only_given = await check_pages(client, "/about", routes=False)
    assert problems == ["/: answers 404"]
    assert problems.pages == ["/", "/about"]
    assert not only_given


async def test_signals_defined_by_a_script_and_limit() -> None:
    async def home(_c: Context, w: Writer) -> None:
        w.html(
            _page(
                h.Span(data.text("$_toast")),
                *[h.A({"href": f"/p/{i}"}, str(i)) for i in range(5)],
            )
        )

    async def numbered(_c: Context, w: Writer) -> None:
        w.html(_page(h.P("page")))

    app = App()
    app.add(Route("GET /"), home)
    app.add(Route("GET /p/{n}"), numbered)
    async with TestClient(app) as client:
        flagged = await check_pages(client, checks=datastar_checks())
        problems = await check_pages(
            client, checks=datastar_checks(signals=["_toast"]), limit=3
        )
    assert flagged == ["/: data-text reads $_toast, which no element defines"]
    assert not problems
    assert problems.pages == ["/", "/p/0", "/p/1"]


async def test_check_page_sees_rows_a_test_made() -> None:
    rows: list[str] = []

    async def todos(_c: Context, w: Writer) -> None:
        w.html(_page(h.Ul([h.Li({"id": "row"}, r) for r in rows])))

    app = App()
    app.add(Route("GET /todos"), todos)
    async with TestClient(app) as client:
        assert not await check_page(client, "/todos")
        rows += ["a", "b"]
        problems = await check_page(client, "/todos")
    assert problems == ["/todos: two elements have id='row'"]


async def test_a_check_of_your_own() -> None:
    async def no_images_without_alt(
        _client: TestClient, url: str, page: HtmlPage
    ) -> list[str]:
        return [
            f"{url}: an <img> has no alt"
            for tag, attrs, _ in page.tags
            if tag == "img" and "alt" not in dict(attrs)
        ]

    async def home(_c: Context, w: Writer) -> None:
        w.html(_page(h.Img({"src": "/a.png"}), h.Img({"src": "/b.png", "alt": "b"})))

    app = App()
    app.add(Route("GET /"), home)
    async with TestClient(app) as client:
        problems = await check_pages(client, checks=[no_images_without_alt])
        one = await check_page(client, "/", checks=[no_images_without_alt])
    assert problems == ["/: an <img> has no alt"]
    assert one == ["/: an <img> has no alt"]
