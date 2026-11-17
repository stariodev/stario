"""Check every HTML page of an app for the mistakes a test of one page misses.

```python
from stario.testing import TestClient, check_page, check_pages


async def test_every_page(client: TestClient) -> None:
    assert not await check_pages(client)


async def test_todo_page_with_rows(client: TestClient) -> None:
    await client.post("/todos", data={"title": "Milk"})
    assert not await check_page(client, "/todos")
```

`check_pages` starts at `/` and at every GET route without a path parameter
that answers HTML, follows the app-relative links of each page (up to
`limit` pages), and returns one line per problem. Every page is checked for:

- a 5xx answer (with the exception behind it),
- two elements with the same `id`.

`checks=` adds checks of your own: each is an async function
`check(client, url, page) -> list[str]` that gets the parsed `HtmlPage`. The
Datastar checks are in `stario.datastar.testing`:

```python
from stario.datastar.testing import datastar_checks

assert not await check_pages(client, checks=datastar_checks())
```

A page whose 5xx comes from a service the test does not run (an outside API
that is down or unreachable: `httpx.ConnectError`, a timeout) is skipped, not
failed. When `/` itself needs such a service, `check_pages` raises
`unittest.SkipTest`, which pytest and unittest report as a skip.
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
    from stario.testing.response import TestResponse

# Exception type names (substrings) of a call to a service a test does not
# run: httpx/aiohttp connect errors and timeouts, DNS failures, a refused
# socket. A page that fails with one of them is skipped.
OUTSIDE_SERVICE_ERRORS: tuple[str, ...] = (
    "Connect",
    "Timeout",
    "gaierror",
    "RemoteProtocol",
    "NetworkError",
    "OSError",
)

type SkipErrors = Iterable[str | type[BaseException]]
type PageCheck = Callable[[TestClient, str, HtmlPage], Awaitable[list[str]]]


class PageProblems(list[str]):
    """The problems `check_pages` / `check_page` found, one line each.

    Empty (falsy) when every page checks out, so `assert not problems`
    passes; a failing assert prints one problem per line. `pages` lists the
    pages checked, `skipped` the pages skipped (with the reason).
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
    # the second one silently never updates.
    found: list[str] = []
    ids: set[str] = set()
    for _, attrs, _ in page.tags:
        for key, value in attrs:
            if key == "id":
                if value in ids:
                    found.append(f"{url}: two elements have id={value!r}")
                ids.add(value)
    return found


def _skip_reason(
    client: TestClient, r: TestResponse, skip_errors: SkipErrors
) -> str | None:
    """'TypeName: message' when a 5xx comes from an outside service, else None."""
    if r.status_code < 500:
        return None
    exc_type, message = _failure(client, r)
    for entry in skip_errors:
        if isinstance(entry, str):
            if entry and entry in exc_type:
                return f"{exc_type}: {message}"
        elif entry.__name__ == exc_type:
            return f"{exc_type}: {message}"
    return None


def _failure(client: TestClient, r: TestResponse) -> tuple[str, str]:
    """The type name and first message line of the exception behind `r`."""
    span = client.tracer.get_span(r.span_id)
    for event in span.events if span is not None else ():
        if event.name == "exception":
            message = str(event.attributes.get("exc.message", "")).splitlines()[:1]
            return str(event.attributes.get("exc.type", "")), "".join(message)
    return "", ""


async def _check(
    client: TestClient,
    url: str,
    r: TestResponse,
    result: PageProblems,
    skip_errors: SkipErrors,
    checks: Iterable[PageCheck],
) -> HtmlPage | None:
    """Check one fetched page into `result`; the parsed page when it is HTML."""
    reason = _skip_reason(client, r, skip_errors)
    if reason is not None:
        result.skipped.append(f"{url}: needs a service that is not running ({reason})")
        return None
    result.pages.append(url)
    if r.status_code >= 500:
        exc_type, message = _failure(client, r)
        cause = f"{exc_type}: {message}" if exc_type else "see the log"
        result.append(f"{url}: answers {r.status_code} ({cause})")
        return None
    if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        return None
    page = _parse(r.text)
    result += _duplicate_ids(url, page)
    for check in checks:
        result += await check(client, url, page)
    return page


def _get_routes(node: Node, out: list[Route]) -> None:
    for method, endpoint in (node.endpoints or {}).items():
        if method == "GET":
            out.append(endpoint.route)
    for child in node.exact.values():
        _get_routes(child, out)


async def _html_routes(client: TestClient) -> list[str]:
    """`href()` of each GET route without a path parameter that answers HTML."""
    routes: list[Route] = []
    _get_routes(client.app._path, routes)
    found: list[str] = []
    for route in routes:
        if "{" in route.target:
            continue  # a path parameter needs a value the checker cannot guess
        href = route.href()
        try:
            async with client.stream("GET", href, timeout=2) as r:
                html = "text/html" in r.headers.get("content-type", "")
                if r.status_code == 200 and html:
                    found.append(href)
        except TimeoutError:
            continue  # sends no headers in 2 s: a stream waiting for news
    return found


async def check_page(
    client: TestClient,
    url: str,
    *,
    skip_errors: SkipErrors = OUTSIDE_SERVICE_ERRORS,
    checks: Iterable[PageCheck] = (),
) -> PageProblems:
    """The problems of one page; empty when it checks out or is not HTML.

    Use it after a test has made rows, so the checks see them:
    `assert not await check_page(client, "/todos")`. The options are those
    of `check_pages`.
    """
    result = PageProblems()
    r = await client.get(url)
    await _check(client, url, r, result, skip_errors, tuple(checks))
    return result


async def check_pages(
    client: TestClient,
    *start: str,
    limit: int = 50,
    routes: bool = True,
    skip_errors: SkipErrors = OUTSIDE_SERVICE_ERRORS,
    checks: Iterable[PageCheck] = (),
) -> PageProblems:
    """Check every HTML page reachable from `start` (default `/`).

    `assert not await check_pages(client)` fails with one line per problem.

    - `start`: pages to begin at (`check_pages(client, "/", "/admin")`).
    - `limit`: the most pages to check (default 50).
    - `routes`: also start at each GET route without a path parameter that
      answers HTML (default `True`).
    - `skip_errors`: a page that answers 5xx because of one of these is
      skipped, not failed. A `str` matches part of the exception's type
      name, a class its exact name. The default, `OUTSIDE_SERVICE_ERRORS`,
      covers connect errors and timeouts of an outside service. Pass `()` to
      fail on every 5xx.
    - `checks`: more checks to run on each HTML page, for example
      `stario.datastar.testing.datastar_checks()`. Each is an async function
      `check(client, url, page) -> list[str]`; `page` is an `HtmlPage`.

    Raises `unittest.SkipTest` when the first start page needs a service
    that is not running.
    """
    result = PageProblems()
    checks = tuple(checks)
    queue = list(start or ("/",))
    first = await client.get(queue[0])
    reason = _skip_reason(client, first, skip_errors)
    if reason is not None:
        raise unittest.SkipTest(
            f"{queue[0]} needs a service that is not running ({reason})"
        )
    if 400 <= first.status_code < 500:
        result.append(f"{queue[0]}: answers {first.status_code}")
    if routes:
        queue += await _html_routes(client)
    seen: set[str] = set()
    fetched: dict[str, TestResponse] = {queue[0]: first}
    while queue and len(seen) < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = fetched.pop(url, None) or await client.get(url)
        page = await _check(client, url, r, result, skip_errors, checks)
        for href in page.links if page is not None else ():
            parts = urlsplit(href)
            if not parts.scheme and not parts.netloc and parts.path.startswith("/"):
                queue.append(parts.path + (f"?{parts.query}" if parts.query else ""))
    return result


__all__ = [
    "OUTSIDE_SERVICE_ERRORS",
    "HtmlPage",
    "PageCheck",
    "PageProblems",
    "check_page",
    "check_pages",
]
