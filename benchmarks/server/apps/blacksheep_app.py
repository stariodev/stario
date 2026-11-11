# pyright: reportMissingImports=false
"""BlackSheep target, written the way the BlackSheep docs describe.

- JSON: orjson plugged in through ``json_settings.use`` (settings.md, "Example:
  using orjson"), responses built with the docs' bytes-direct ``Content`` helper
  that the same section recommends for best performance with orjson.
- POST body: ``FromJSON[PydanticModel]`` binder; a failed validation becomes a
  400 Bad Request with the Pydantic error JSON.
- /page: ``view()`` with a ``JinjaRenderer`` set in ``html_settings``; the
  template comes from a ``DictLoader`` and the ``.jinja`` name turns on autoescape.
- /sse: an async generator that yields ``TextServerSentEvent`` (blacksheep.server.sse).
"""

import asyncio
import os
from collections.abc import AsyncIterable
from typing import Any

import orjson
from jinja2 import DictLoader
from pydantic import BaseModel, Field

from blacksheep import Application, Content, FromJSON, Response, text
from blacksheep.server.rendering.jinja2 import JinjaRenderer
from blacksheep.server.responses import view
from blacksheep.server.sse import TextServerSentEvent
from blacksheep.settings.html import html_settings
from blacksheep.settings.json import json_settings

from apps.common import (
    COMPRESS,
    HELLO,
    NAV,
    PAGE_TEMPLATE,
    ROWS,
    Hub,
    sse_fragment,
    ticker,
)

# orjson for every JSON operation the framework does (request.json(), binders,
# auto-serialised return values). orjson.dumps returns bytes, so decode to str here.
json_settings.use(loads=orjson.loads, dumps=lambda obj: orjson.dumps(obj).decode())

# A ".jinja" template name makes JinjaRenderer's select_autoescape turn autoescaping on.
# Sync rendering (enable_async is False by default), no auto_reload (it is off unless APP_JINJA_DEBUG is set).
html_settings.use(JinjaRenderer(loader=DictLoader({"page.jinja": PAGE_TEMPLATE})))
PAGE_MODEL = {"nav": NAV, "rows": ROWS}

app = Application()  # show_error_details defaults to False (APP_SHOW_ERROR_DETAILS)
hub = Hub()

if COMPRESS:
    from blacksheep.server.compression import GzipMiddleware

    app.middlewares.append(
        GzipMiddleware(comp_level=int(os.environ.get("BENCH_GZIP_LEVEL", "5")))
    )


@app.on_start
async def start_ticker(application: Application) -> None:
    application.ticker_task = asyncio.create_task(ticker(hub.publish))


def json(data: Any, status: int = 200) -> Response:
    """The bytes-direct JSON response from the orjson section of the BlackSheep settings docs."""
    return Response(status, None, Content(b"application/json", orjson.dumps(data)))


class UserInput(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0, le=150)


# Handlers are annotated "-> Response" so BlackSheep skips the per-call return-type check.
@app.router.get("/plaintext")
async def plaintext() -> Response:
    return text(HELLO)


@app.router.get("/json")
async def json_endpoint() -> Response:
    return json({"message": HELLO})


@app.router.get("/user/{user_id}")
async def get_user(user_id: str) -> Response:
    return json({"id": user_id, "name": f"User {user_id}"})


@app.router.post("/validate")
async def validate(body: FromJSON[UserInput]) -> Response:
    data = body.value
    return json({"name": data.name, "age": data.age, "valid": True})


@app.router.get("/page")
async def page() -> Response:
    return view("page", PAGE_MODEL)


@app.router.get("/sse")
async def sse() -> AsyncIterable[TextServerSentEvent]:
    queue = hub.subscribe()
    try:
        while True:
            tick, ts = await queue.get()
            yield TextServerSentEvent(
                "elements " + sse_fragment(tick, ts), event="datastar-patch-elements"
            )
    finally:
        hub.unsubscribe(queue)
