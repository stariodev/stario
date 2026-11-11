# pyright: reportMissingImports=false

"""The benchmark app, written the way Stario's README and examples do it.

- JSON: ``w.json()`` with the process-wide codec set to orjson, as the
  README's "JSON codec" section shows for an app that wants a faster
  library than the standard one; request bodies go through the same codec.
  orjson ships no free-threaded wheel, so the 3.14t target uses msgspec,
  the README's other example.
- HTML: ``h.*`` builders, ``h.HtmlDocument`` for a full page; ``/page-baked``
  serves the same bytes through ``@baked``.
- SSE: Datastar ``SSE(w).patch_elements`` fed by an in-process ``Relay``.
"""

import asyncio

import stario.json as stario_json
from apps.common import HELLO, NAV, ROWS, sse_fragment, ticker
from stario import App, Relay, Route, Span, h
from stario.datastar import SSE
from stario.markup import baked

try:
    import orjson

    stario_json.set_codec(orjson.dumps, orjson.loads)
except ImportError:  # free-threaded build: no orjson wheel
    import msgspec

    stario_json.set_codec(msgspec.json.encode, msgspec.json.decode)
TICK = "bench.tick"


def page():
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "utf-8"}),
            h.Meta({"name": "viewport", "content": "width=device-width, initial-scale=1"}),
            h.Title("Products · Admin"),
            h.Link({"rel": "stylesheet", "href": "/static/app.css"}),
            h.Script({"type": "module", "src": "/static/datastar.js"}),
        ),
        h.Body(
            {"class": "min-h-screen bg-base"},
            h.Nav(
                {"class": "navbar"},
                *[h.A({"href": href, "class": "nav-link"}, label) for href, label in NAV],
            ),
            h.Main(
                {"class": "container mx-auto p-6"},
                h.H1({"class": "title"}, "Products"),
                h.Table(
                    {"class": "table"},
                    h.Thead(
                        h.Tr(
                            h.Th("SKU"), h.Th("Name"), h.Th("Description"),
                            h.Th("Price"), h.Th("Status"), h.Th(),
                        )
                    ),
                    h.Tbody(
                        *[
                            h.Tr(
                                {"id": f"row-{r['id']}", "class": f"row row-{r['status']}"},
                                h.Td({"class": "mono"}, r["sku"]),
                                h.Td(r["name"]),
                                h.Td({"class": "muted"}, r["desc"]),
                                h.Td({"class": "num"}, "$", r["price"]),
                                h.Td(
                                    h.Span({"class": f"badge badge-{r['status']}"}, r["status"])
                                ),
                                h.Td(
                                    h.A(
                                        {"href": f"/products/{r['id']}", "class": "btn btn-sm"},
                                        "Edit",
                                    )
                                ),
                            )
                            for r in ROWS
                        ]
                    ),
                ),
            ),
            h.Footer({"class": "footer"}, h.P("50 products")),
        ),
    )


# The same page with @baked: the layout and the row are compiled once, and
# each call only splices the per-request values (Stario's equivalent of a
# precompiled template). Byte-identical to page(); see bench.py's checks.


@baked
def _row(rid, cls, sku, name, desc, price, badge, status, href):
    return h.Tr(
        {"id": rid, "class": cls},
        h.Td({"class": "mono"}, sku),
        h.Td(name),
        h.Td({"class": "muted"}, desc),
        h.Td({"class": "num"}, "$", price),
        h.Td(h.Span({"class": badge}, status)),
        h.Td(h.A({"href": href, "class": "btn btn-sm"}, "Edit")),
    )


@baked
def _layout(nav, rows):
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "utf-8"}),
            h.Meta({"name": "viewport", "content": "width=device-width, initial-scale=1"}),
            h.Title("Products · Admin"),
            h.Link({"rel": "stylesheet", "href": "/static/app.css"}),
            h.Script({"type": "module", "src": "/static/datastar.js"}),
        ),
        h.Body(
            {"class": "min-h-screen bg-base"},
            h.Nav({"class": "navbar"}, nav),
            h.Main(
                {"class": "container mx-auto p-6"},
                h.H1({"class": "title"}, "Products"),
                h.Table(
                    {"class": "table"},
                    h.Thead(
                        h.Tr(
                            h.Th("SKU"), h.Th("Name"), h.Th("Description"),
                            h.Th("Price"), h.Th("Status"), h.Th(),
                        )
                    ),
                    h.Tbody(rows),
                ),
            ),
            h.Footer({"class": "footer"}, h.P("50 products")),
        ),
    )


def baked_page():
    return _layout(
        [h.A({"href": href, "class": "nav-link"}, label) for href, label in NAV],
        [
            _row(
                f"row-{r['id']}", f"row row-{r['status']}", r["sku"], r["name"], r["desc"],
                r["price"], f"badge badge-{r['status']}", r["status"], f"/products/{r['id']}",
            )
            for r in ROWS
        ],
    )


async def plaintext(c, w):
    w.text(HELLO)


async def json_endpoint(c, w):
    w.json({"message": HELLO})


async def get_user(c, w):
    user_id = c.match.params["user_id"]
    w.json({"id": user_id, "name": f"User {user_id}"})


async def validate(c, w):
    body = stario_json.loads(await c.req.body())
    name = body.get("name")
    age = body.get("age")

    if not isinstance(name, str) or not name:
        w.json({"error": "name must be a non-empty string"}, 400)
        return
    if not isinstance(age, int) or age < 0 or age > 150:
        w.json({"error": "age must be an integer between 0 and 150"}, 400)
        return

    w.json({"name": name, "age": age, "valid": True})


async def html_page(c, w):
    w.html(page())


async def html_page_baked(c, w):
    w.html(baked_page())


async def bootstrap(app: App, span: Span) -> None:
    relay: Relay[tuple[int, int]] = Relay()

    async def sse(c, w):
        async with relay.subscribe(TICK) as events:
            stream = SSE(w)
            async for _subject, (tick, ts) in c.alive(events):
                stream.patch_elements(sse_fragment(tick, ts))

    app.add(Route("GET /plaintext"), plaintext)
    app.add(Route("GET /json"), json_endpoint)
    app.add(Route("GET /user/{user_id}"), get_user)
    app.add(Route("POST /validate"), validate)
    app.add(Route("GET /page"), html_page)
    app.add(Route("GET /page-baked"), html_page_baked)
    app.add(Route("GET /sse"), sse)
    task = asyncio.create_task(ticker(lambda item: relay.publish(TICK, item)))
    yield
    task.cancel()
