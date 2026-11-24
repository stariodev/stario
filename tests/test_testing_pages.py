"""stario.testing.check_pages: every HTML page of an app, checked."""

from __future__ import annotations

import unittest

import pytest

from stario import App, Context, HtmlElement, Route, Writer, h
from stario.markup.types import TagAttributes
from stario.testing import HtmlPage, TestClient, check_page, check_pages


def _page(*body: HtmlElement | TagAttributes) -> HtmlElement:
    return h.HtmlDocument(h.Body(*body))


def _app(**pages: object) -> App:
    """An app with one GET route per keyword: `about=...` is `GET /about`."""
    app = App()
    for name, handler in pages.items():
        path = "/" if name == "home" else "/" + name.replace("_", "/")
        app.add(Route(f"GET {path}"), handler)  # type: ignore[arg-type]
    return app


async def test_a_good_app_has_no_problems() -> None:
    async def home(_c: Context, w: Writer) -> None:
        w.html(
            _page(
                h.A({"href": "/about"}, "About"),
                h.A({"href": "https://example.com/"}, "Out"),
                h.A({"href": "/old"}, "Old"),
            )
        )

    async def about(_c: Context, w: Writer) -> None:
        w.html(_page(h.P("About")))

    async def old(_c: Context, w: Writer) -> None:
        w.redirect("/about")

    async def api(_c: Context, w: Writer) -> None:
        w.json({"ok": True})

    async with TestClient(_app(home=home, about=about, old=old, api=api)) as client:
        problems = await check_pages(client)
    assert not problems
    assert problems.pages == ["/", "/about", "/old", "/api"]
    assert problems.skipped == []
    assert repr(problems) == "PageProblems([])"


async def test_every_problem_is_one_line() -> None:
    async def home(_c: Context, w: Writer) -> None:
        w.html(
            _page(
                h.P({"id": "x"}, "a"),
                h.P({"id": "x"}, "b"),
                h.A({"href": "/broken"}, "Broken"),
                h.A({"href": "/typo"}, "Typo"),
            )
        )

    async def broken(_c: Context, _w: Writer) -> None:
        raise RuntimeError("boom")

    async with TestClient(_app(home=home, broken=broken)) as client:
        problems = await check_pages(client)
    assert problems == [
        "/: two elements have id='x'",
        "/broken: answers 500 (RuntimeError: boom) (linked from /)",
        "/typo: answers 404 (linked from /)",
    ]
    assert "\n/broken: answers 500" in repr(problems)


async def test_a_crashing_route_that_no_page_links_is_a_problem() -> None:
    async def home(_c: Context, w: Writer) -> None:
        w.html(_page(h.P("home")))

    async def orphan(_c: Context, _w: Writer) -> None:
        raise ValueError("no link reaches me")

    async def db(_c: Context, _w: Writer) -> None:
        raise TimeoutError("db lock wait timed out")

    async def account(_c: Context, w: Writer) -> None:
        w.text("sign in first", status=401)

    app = _app(home=home, orphan=orphan, db=db, account=account)
    async with TestClient(app) as client:
        problems = await check_pages(client)
    # The app's own TimeoutError is a failure: nothing is skipped by default.
    assert problems == [
        "/orphan: answers 500 (ValueError: no link reaches me)",
        "/db: answers 500 (TimeoutError: db lock wait timed out)",
    ]
    # A 401 of a route no page links to is a page behind a sign-in.
    assert "/account" in problems.pages


async def test_skip_errors_names_a_service_the_test_does_not_run() -> None:
    class ConnectError(Exception):
        pass

    async def home(_c: Context, w: Writer) -> None:
        w.html(_page(h.A({"href": "/weather"}, "Weather")))

    async def weather(_c: Context, _w: Writer) -> None:
        raise ConnectError("api.example.com refused the connection")

    async with TestClient(_app(home=home, weather=weather)) as client:
        strict = await check_pages(client)
        problems = await check_pages(client, skip_errors=(ConnectError,))
        one = await check_page(client, "/weather", skip_errors=(ConnectError,))
    assert strict == [
        "/weather: answers 500 (ConnectError: api.example.com refused the "
        "connection) (linked from /)"
    ]
    assert not problems
    assert problems.skipped == [
        "/weather: needs a service that is not running "
        "(ConnectError: api.example.com refused the connection)"
    ]
    assert not one


async def test_home_needing_a_skipped_service_is_a_skip() -> None:
    async def home(_c: Context, _w: Writer) -> None:
        raise TimeoutError("the API took too long")

    async with TestClient(_app(home=home)) as client:
        with pytest.raises(unittest.SkipTest, match="TimeoutError: the API took"):
            await check_pages(client, skip_errors=(TimeoutError,))


async def test_missing_home_page_is_a_problem() -> None:
    async def about(_c: Context, w: Writer) -> None:
        w.html(_page(h.P("About")))

    async with TestClient(_app(about=about)) as client:
        problems = await check_pages(client)
        only_given = await check_pages(client, "/about", routes=False)
    assert problems == ["/: answers 404"]
    assert problems.pages == ["/", "/about"]
    assert not only_given


async def test_more_pages_than_the_limit_is_a_problem() -> None:
    async def home(_c: Context, w: Writer) -> None:
        w.html(_page(*[h.A({"href": f"/p/{i}"}, str(i)) for i in range(5)]))

    async def numbered(_c: Context, w: Writer) -> None:
        w.html(_page(h.P("page")))

    app = App()
    app.add(Route("GET /"), home)
    app.add(Route("GET /p/{n}"), numbered)
    async with TestClient(app) as client:
        cut = await check_pages(client, limit=3)
        whole = await check_pages(client, limit=6)
        with pytest.raises(ValueError, match="limit"):
            await check_pages(client, limit=0)
    assert cut == [
        "checked 3 URLs and stopped; 3 more are not checked, such as /p/2. "
        "Pass limit= to check them all."
    ]
    assert cut.pages == ["/", "/p/0", "/p/1"]
    assert not whole


async def test_a_stream_route_is_not_waited_on() -> None:
    async def home(_c: Context, w: Writer) -> None:
        w.html(_page(h.P("home")))

    async def live(c: Context, _w: Writer) -> None:
        await c.disconnect  # sends nothing until news comes

    async with TestClient(_app(home=home, live=live)) as client:
        problems = await check_pages(client, timeout=0.2)
    assert not problems
    assert problems.skipped == [
        "/live: sent no page within 0.2 s (a stream that waits for news?). "
        "Pass timeout= to wait longer."
    ]


async def test_check_page_sees_rows_a_test_made_and_reports_4xx() -> None:
    rows: list[str] = []

    async def todos(_c: Context, w: Writer) -> None:
        w.html(_page(h.Ul([h.Li({"id": "row"}, r) for r in rows])))

    async with TestClient(_app(todos=todos)) as client:
        assert not await check_page(client, "/todos")
        rows += ["a", "b"]
        problems = await check_page(client, "/todos")
        typo = await check_page(client, "/todo")
    assert problems == ["/todos: two elements have id='row'"]
    assert typo == ["/todo: answers 404"]


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

    async with TestClient(_app(home=home)) as client:
        problems = await check_pages(client, checks=[no_images_without_alt])
        one = await check_page(client, "/", checks=[no_images_without_alt])
    assert problems == ["/: an <img> has no alt"]
    assert one == ["/: an <img> has no alt"]
