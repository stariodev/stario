"""
Stario Hello World.

One route, one handler, one HTML page. This file is the smallest complete
Stario app: a `Route`, a handler that writes through `Writer`, and a
`bootstrap` that registers the route.

Run with: uv run python -m main

For Datastar, SSE, and shared state, see Tiles: https://github.com/stariodev/tiles
"""

from stario import App, Context, Route, Span, Writer, h

HOME = Route("GET", "/")


def home_view():
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "UTF-8"}),
            h.Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            h.Title("Hello, Stario"),
        ),
        h.Body(
            {
                "style": "font-family: system-ui; padding: 2rem; max-width: 36rem; margin: 0 auto;"
            },
            h.H1("Hello, Stario"),
            h.P("This page is one handler and one route."),
        ),
    )


async def home(c: Context, w: Writer) -> None:
    w.html(home_view())


async def bootstrap(app: App, span: Span):
    span.attr("app.name", "hello-world")
    app.add(HOME, home)
    yield


if __name__ == "__main__":
    import asyncio

    from stario import serve

    asyncio.run(serve(bootstrap))
