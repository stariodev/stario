"""Check every HTML page of an app for the mistakes a test of one page misses.

```python
from stario.testing import TestClient, check_page, check_pages


async def test_every_page(client: TestClient) -> None:
    assert not await check_pages(client)


async def test_todo_page_with_rows(client: TestClient) -> None:
    await client.post("/todos", data={"title": "Milk"})
    assert not await check_page(client, "/todos")
```

`check_pages` starts at `/` and at every GET route without a path parameter,
follows the app-relative links and redirects of each HTML page, and returns
one line per problem:

- a 5xx answer, with the exception behind it;
- a 4xx answer of a start page or of a link (a broken link);
- two elements with the same `id`;
- more pages than `limit`: the pages after the limit are not checked.

A 4xx of a route that no page links to is no problem: a page behind a sign-in
answers 401 or 303 to a client that has not signed in. Sign the client in
first to check those pages.

`checks=` adds checks of your own: each is an async function
`check(client, url, page) -> list[str]` that gets the parsed `HtmlPage`.
"""

from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable, Iterable
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from stario.http.route import Route

if TYPE_CHECKING:
    from stario.http.dispatch import Node
    from stario.testing.client import TestClient
    from stario.testing.response import TestStreamResponse

type SkipErrors = Iterable[type[BaseException]]
type PageCheck = Callable[[TestClient, str, HtmlPage], Awaitable[list[str]]]

_IDENTITY = {"accept-encoding": "identity"}


class PageProblems(list[str]):
    """The problems `check_pages` / `check_page` found, one line each.

    Empty (falsy) when every page checks out, so `assert not problems`
    passes; a failing assert prints one problem per line. `pages` lists the
    URLs checked, `skipped` the URLs skipped, with the reason.
    """

    def __init__(self) -> None:
        super().__init__()
        self.pages: list[str] = []
        self.skipped: list[str] = []

    def __repr__(self) -> str:
        if not self:
            return "PageProblems([])"
        return "\n" + "\n".join(self)


class HtmlPage(HTMLParser):
    """One page as `check_pages` parsed it, for a check passed in `checks=`.

    - `tags`: each start tag as `(name, [(key, value), ...], opening_tag)`,
      in document order. A value is decoded (`&amp;` is `&`); a bare
      attribute has the value `""`.
    - `links`: the `href` of each `<a>`.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str]], str]] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pairs = [(k, v or "") for k, v in attrs]
        self.tags.append((tag, pairs, self.get_starttag_text() or ""))
        if tag == "a":
            self.links += [v for k, v in pairs if k == "href"]


def _parse(html: str) -> HtmlPage:
    page = HtmlPage()
    page.feed(html)
    page.close()
    return page


def _duplicate_ids(url: str, page: HtmlPage) -> list[str]:
    # getElementById, labels and morphing find the first element with an id;
    # the second one never updates.
    found: list[str] = []
    ids: set[str] = set()
    for _, attrs, _ in page.tags:
        for key, value in attrs:
            if key == "id":
                if value in ids:
                    found.append(f"{url}: two elements have id={value!r}")
                ids.add(value)
    return found


def _failure(client: TestClient, r: TestStreamResponse) -> tuple[str, str]:
    """The type name and first message line of the exception behind `r`."""
    span = client.tracer.get_span(r.span_id)
    for event in span.events if span is not None else ():
        if event.name == "exception":
            message = str(event.attributes.get("exc.message", "")).splitlines()[:1]
            return str(event.attributes.get("exc.type", "")), "".join(message)
    return "", ""


def _app_url(href: str) -> str | None:
    """`href` as an app path (with its query), or None when it leaves the app."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _get_routes(node: Node, out: list[Route]) -> None:
    for method, endpoint in (node.endpoints or {}).items():
        if method == "GET":
            out.append(endpoint.route)
    for child in node.exact.values():
        _get_routes(child, out)


def _route_urls(client: TestClient) -> list[str]:
    """`href()` of each GET route without a path parameter."""
    routes: list[Route] = []
    _get_routes(client.app._path, routes)
    # A path parameter needs a value the checker cannot guess: start at a
    # page that links to such routes, or check one with check_page().
    return [route.href() for route in routes if "{" not in route.target]


class _Checker:
    def __init__(
        self,
        client: TestClient,
        skip_errors: SkipErrors,
        checks: Iterable[PageCheck],
        timeout: float,
    ) -> None:
        self.client = client
        self.skip_errors = tuple(skip_errors)
        self.checks = tuple(checks)
        self.timeout = timeout
        self.result = PageProblems()

    def _skip_reason(self, exc_type: str) -> bool:
        return any(entry.__name__ == exc_type for entry in self.skip_errors)

    async def check(self, url: str, linked_from: str | None) -> list[str] | None:
        """Check one URL into `result`. Returns the app URLs it leads to.

        `linked_from` is the page that links to `url`, "" for a start page,
        and None for a route that no page links to. Returns None when the
        URL needs a service in `skip_errors`.
        """
        client, result = self.client, self.result
        where = f" (linked from {linked_from})" if linked_from else ""
        try:
            async with client.stream(
                "GET", url, headers=_IDENTITY, timeout=self.timeout
            ) as r:
                if r.status_code >= 500:
                    exc_type, message = _failure(client, r)
                    if exc_type and self._skip_reason(exc_type):
                        result.skipped.append(
                            f"{url}: needs a service that is not running "
                            f"({exc_type}: {message})"
                        )
                        return None
                    result.pages.append(url)
                    cause = f"{exc_type}: {message}" if exc_type else "see the log"
                    result.append(f"{url}: answers {r.status_code} ({cause}){where}")
                    return []
                result.pages.append(url)
                if r.status_code >= 400:
                    if linked_from is not None:
                        result.append(f"{url}: answers {r.status_code}{where}")
                    return []
                if 300 <= r.status_code < 400:
                    target = _app_url(r.headers.get("location") or "")
                    return [target] if target else []
                content_type = r.headers.get("content-type") or ""
                if r.status_code != 200 or "text/html" not in content_type:
                    return []  # JSON, a file, a stream: not a page
                html = (await r.body()).decode("utf-8", "replace")
        except TimeoutError:
            result.skipped.append(
                f"{url}: sent no page within {self.timeout:g} s (a stream that "
                "waits for news?). Pass timeout= to wait longer."
            )
            return []
        page = _parse(html)
        result += _duplicate_ids(url, page)
        for check in self.checks:
            result += await check(client, url, page)
        return [u for href in page.links if (u := _app_url(href))]


async def check_page(
    client: TestClient,
    url: str,
    *,
    skip_errors: SkipErrors = (),
    checks: Iterable[PageCheck] = (),
    timeout: float = 2.0,
) -> PageProblems:
    """The problems of one page; empty when it checks out or is not HTML.

    Use it after a test has made rows, so the checks see them:
    `assert not await check_page(client, "/todos")`. A 4xx answer is a
    problem. The options are those of `check_pages`.
    """
    checker = _Checker(client, skip_errors, checks, timeout)
    await checker.check(url, "")
    return checker.result


async def check_pages(
    client: TestClient,
    *start: str,
    limit: int = 500,
    routes: bool = True,
    skip_errors: SkipErrors = (),
    checks: Iterable[PageCheck] = (),
    timeout: float = 2.0,
) -> PageProblems:
    """Check every page reachable from `start` (default `/`).

    `assert not await check_pages(client)` fails with one line per problem.

    - `start`: pages to begin at (`check_pages(client, "/", "/admin")`).
    - `limit`: the most URLs to check (default 500). When more are left,
      that is a problem: raise `limit=`, or start at fewer pages.
    - `routes`: also check each GET route without a path parameter
      (default `True`).
    - `skip_errors`: exception classes of a service the test does not run,
      for example `(httpx.ConnectError,)`. A page that answers 5xx because
      of one is skipped, not failed. The default `()` fails on every 5xx.
    - `checks`: more checks to run on each HTML page. Each is an async
      function `check(client, url, page) -> list[str]`; `page` is an
      `HtmlPage`.
    - `timeout`: seconds to wait for one URL (default 2). A URL that sends
      nothing in that time is a stream waiting for news: it is skipped.

    Raises `unittest.SkipTest` when the first start page needs a service
    in `skip_errors`.
    """
    if limit < 1:
        raise ValueError("check_pages(limit=) must be at least 1")
    checker = _Checker(client, skip_errors, checks, timeout)
    queue: list[tuple[str, str | None]] = [(url, "") for url in start or ("/",)]
    # Routes come after the pages that links reach, so a route that a page
    # links to is checked as a link: its 4xx is a broken link.
    tail = [(url, None) for url in _route_urls(client)] if routes else []
    seen: set[str] = set()
    while queue or tail:
        if not queue:
            queue.append(tail.pop(0))
        url, linked_from = queue.pop(0)
        if url in seen:
            continue
        if len(seen) == limit:
            left = {u for u, _ in queue + tail if u not in seen} | {url}
            checker.result.append(
                f"checked {limit} URLs and stopped; {len(left)} more are not "
                f"checked, such as {min(left)}. Pass limit= to check them all."
            )
            break
        seen.add(url)
        found = await checker.check(url, linked_from)
        if found is None and not seen - {url}:
            first = checker.result.skipped[-1]
            raise unittest.SkipTest(first)
        queue += [(u, url) for u in found or ()]
    return checker.result


__all__ = [
    "HtmlPage",
    "PageCheck",
    "PageProblems",
    "check_page",
    "check_pages",
]
