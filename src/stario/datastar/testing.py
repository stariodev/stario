"""Datastar checks for `stario.testing.check_pages` and `check_page`.

```python
from stario.datastar.testing import datastar_checks
from stario.testing import check_pages


async def test_every_page(client: TestClient) -> None:
    assert not await check_pages(client, checks=datastar_checks())
```

`datastar_checks()` checks each HTML page for:

- a Datastar action (`@get('/x')`, `@post(...)`, ...) whose path and method
  have no route,
- a `$signal` read in a `data-*` attribute that no element defines,
- a `<dialog>` without `data.preserve_attr("open")` (a patch would close it),
- a `data-init` stream (`@get`) that has no `id`, does not answer 200, sends
  no event first, or patches the element into a different opening tag (it
  would stop reconnecting or lose its `id`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from stario.http.route import Route

if TYPE_CHECKING:
    from stario.testing.client import TestClient
    from stario.testing.pages import HtmlPage, PageCheck
    from stario.testing.response import TestStreamResponse

_ACTION = re.compile(r"@(get|post|put|patch|delete)\(\s*'([^']*)'")
_SIGNAL = re.compile(r"\$([A-Za-z_]\w*)")
_ASSIGN = re.compile(r"\$([A-Za-z_]\w*)\s*=(?!=)")
_KEY = re.compile(r"['\"]([A-Za-z_]\w*)['\"]\s*:")
_QUOTED = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


def datastar_checks(
    *, signals: Iterable[str] = (), streams: bool = True
) -> tuple[PageCheck, ...]:
    """The Datastar checks, for `check_pages(client, checks=...)`.

    - `signals`: signal names defined outside the HTML (by a script), so
      reading them is not a problem.
    - `streams`: open each `data-init` stream and check its first event
      (default `True`).
    """
    known = frozenset(signals)

    async def attributes(client: TestClient, url: str, page: HtmlPage) -> list[str]:
        return _attribute_problems(client, url, page, known)

    if not streams:
        return (attributes,)
    return (attributes, _stream_problems)


def _json_keys(value: str) -> set[str]:
    try:
        decoded = json.loads(value)
    except ValueError:
        return set(_KEY.findall(value))
    return set(decoded) if isinstance(decoded, dict) else set()


def _defined_signals(page: HtmlPage, known: Iterable[str]) -> set[str]:
    # Every way Datastar defines a signal: data-bind, data-ref and
    # data-indicator by value or key, data-signals and data-computed by key
    # or JSON, and an assignment `$x = ...` in any expression.
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


def _attribute_problems(
    client: TestClient, url: str, page: HtmlPage, known: Iterable[str]
) -> list[str]:
    found: list[str] = []
    names = _defined_signals(page, known)
    host = _host(client)
    for tag, attrs, _ in page.tags:
        for key, value in attrs:
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


def _sent_signals(page: HtmlPage) -> str:
    """The signals a Datastar request from `page` sends (no leading `_`)."""
    # Send what the browser would: a stream that reads its signals answers
    # the same way in the check as in the page.
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


async def _stream_problems(client: TestClient, url: str, page: HtmlPage) -> list[str]:
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


__all__ = ["datastar_checks"]
