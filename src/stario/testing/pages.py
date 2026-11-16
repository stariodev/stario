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
`limit` pages), and returns one line per problem. A page is checked for:

- a 5xx answer (with the exception behind it),
- two elements with the same `id`,
- a Datastar action (`@get('/x')`, `@post(...)`, ...) whose path and method
  have no route,
- a `$signal` read in a `data-*` attribute that no element defines,
- a `<dialog>` without `data.preserve_attr("open")` (a patch would close it),
- a `data-init` stream (`@get`) that does not answer 200, sends no event
  first, or patches the element into a different opening tag (it would stop
  reconnecting or lose its `id`).

A page whose 5xx comes from a service the test does not run (an outside API
that is down or unreachable: `httpx.ConnectError`, a timeout) is skipped, not
failed. When `/` itself needs such a service, `check_pages` raises
`unittest.SkipTest`, which pytest and unittest report as a skip.
"""

from __future__ import annotations

import json
import re
import unittest
from collections.abc import Iterable
from html import unescape
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from stario.http.route import Route
from stario.testing.client import DatastarReplyError

if TYPE_CHECKING:
    from stario.http.dispatch import Node
    from stario.testing.client import TestClient
    from stario.testing.response import TestResponse, TestStreamResponse

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

_ACTION = re.compile(r"@(get|post|put|patch|delete)\(\s*'([^']*)'")
_SIGNAL = re.compile(r"\$([A-Za-z_]\w*)")
_ASSIGN = re.compile(r"\$([A-Za-z_]\w*)\s*=(?!=)")
_KEY = re.compile(r"['\"]([A-Za-z_]\w*)['\"]\s*:")
_QUOTED = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")

type SkipErrors = Iterable[str | type[BaseException]]


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


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str]], str]] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pairs = [(k, v or "") for k, v in attrs]
        self.tags.append((tag, pairs, self.get_starttag_text() or ""))
        if tag == "a":
            self.links += [v for k, v in pairs if k == "href"]


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


def _json_keys(value: str) -> set[str]:
    try:
        decoded = json.loads(value)
    except ValueError:
        return set(_KEY.findall(value))
    return set(decoded) if isinstance(decoded, dict) else set()


def _defined_signals(page: _Page, known: Iterable[str]) -> set[str]:
    names = set(known)
    for _, attrs, _ in page.tags:
        for key, value in attrs:
            base, _, suffix = key.partition(":")
            base = base.split("__")[0]
            if base in ("data-bind", "data-ref", "data-indicator") and not suffix:
                names.add(value)
            elif (
                base in ("data-signals", "data-computed", "data-signal", "data-bind")
                and suffix
            ):
                names.add(suffix.split("__")[0].replace("-", "_"))
            elif base in ("data-signals", "data-computed"):
                names |= _json_keys(value)
            if key.startswith("data-"):
                names |= set(_ASSIGN.findall(_QUOTED.sub("", value)))
    return names


def _host(client: TestClient) -> str:
    return urlsplit(client.base_url).hostname or "localhost"


def _page_problems(
    client: TestClient, url: str, page: _Page, known: Iterable[str]
) -> list[str]:
    found: list[str] = []
    names = _defined_signals(page, known)
    ids: set[str] = set()
    host = _host(client)
    for tag, attrs, _ in page.tags:
        for key, value in attrs:
            if key == "id":
                if value in ids:
                    found.append(f"{url}: two elements have id={value!r}")
                ids.add(value)
            if not key.startswith("data-") or key.startswith("data-signals"):
                continue
            for method, target in _ACTION.findall(value):
                path = urlsplit(unescape(target)).path
                _, route, _ = client.app.find_handler(host, path, method.upper())
                if route is Route.empty():
                    found.append(f"{url}: {method.upper()} {path} has no route")
            for name in _SIGNAL.findall(_QUOTED.sub("", value)):
                if name not in names:
                    found.append(
                        f"{url}: {key} reads ${name}, which no element defines"
                    )
        if tag == "dialog" and "open" not in dict(attrs).get("data-preserve-attr", ""):
            found.append(f'{url}: a <dialog> has no data.preserve_attr("open")')
    return found


def _sent_signals(page: _Page) -> str:
    """The signals a Datastar request from `page` sends (no leading `_`)."""
    values: dict[str, object] = {}
    for _, attrs, _ in page.tags:
        for key, value in attrs:
            if key.split("__")[0] != "data-signals":
                continue
            try:
                decoded = json.loads(value)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                values |= {k: v for k, v in decoded.items() if not k.startswith("_")}
    return json.dumps(values)


async def _first_event(response: TestStreamResponse) -> dict[str, str] | None:
    async for event in response.iter_events():
        return event
    return None


async def _stream_problems(client: TestClient, url: str, page: _Page) -> list[str]:
    found: list[str] = []
    for tag, attrs, text in page.tags:
        init = dict(attrs).get("data-init", "")
        match = _ACTION.search(init)
        if not match or match[1] != "get":
            continue
        if "id" not in dict(attrs):
            found.append(f"{url}: <{tag} data-init> opens a stream but has no id")
            continue
        live = unescape(match[2])
        try:
            async with client.stream(
                "GET",
                live,
                params={"datastar": _sent_signals(page)},
                headers={"datastar-request": "true", "accept-encoding": "identity"},
                timeout=2,
            ) as response:
                if response.status_code != 200:
                    found.append(
                        f"{url}: the stream {live} answers {response.status_code}"
                    )
                    continue
                event = await _first_event(response)
        except DatastarReplyError as error:
            found.append(f"{url}: the stream {live} answers {error.status_code}")
            continue
        except TimeoutError:
            event = None
        if event is None:
            found.append(
                f"{url}: the stream {live} sent no event within 2 s "
                "(send the current view first)"
            )
            continue
        data = event.get("data", "")
        if event.get("event") != "datastar-patch-elements" or "selector " in data:
            continue  # signals, or a patch aimed elsewhere (a navigate, a toast)
        if text not in data:
            found.append(
                f"{url}: the stream {live} patches {text} into a different tag"
            )
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
    signals: Iterable[str],
    streams: bool,
) -> _Page | None:
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
    result += _page_problems(client, url, page, signals)
    if streams:
        result += await _stream_problems(client, url, page)
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
            continue
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
    signals: Iterable[str] = (),
    streams: bool = True,
) -> PageProblems:
    """The problems of one page; empty when it checks out or is not HTML.

    Use it after a test has made rows, so the checks see them:
    `assert not await check_page(client, "/todos")`. The options are those
    of `check_pages`.
    """
    result = PageProblems()
    r = await client.get(url)
    await _check(client, url, r, result, skip_errors, signals, streams)
    return result


async def check_pages(
    client: TestClient,
    *start: str,
    limit: int = 50,
    routes: bool = True,
    skip_errors: SkipErrors = OUTSIDE_SERVICE_ERRORS,
    signals: Iterable[str] = (),
    streams: bool = True,
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
    - `signals`: signal names defined outside the HTML (by a script), so
      reading them is not a problem.
    - `streams`: open each `data-init` stream and check its first event
      (default `True`).

    Raises `unittest.SkipTest` when the first start page needs a service
    that is not running.
    """
    result = PageProblems()
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
        page = await _check(client, url, r, result, skip_errors, signals, streams)
        for href in page.links if page is not None else ():
            parts = urlsplit(href)
            if not parts.scheme and not parts.netloc and parts.path.startswith("/"):
                queue.append(parts.path + (f"?{parts.query}" if parts.query else ""))
    return result


__all__ = ["OUTSIDE_SERVICE_ERRORS", "PageProblems", "check_page", "check_pages"]
