# pyright: reportMissingImports=false
"""FastAPI target, written the way the FastAPI docs recommend.

- JSON: declare a return type and let FastAPI serialize it with Pydantic
  (Rust) straight to bytes; no response_class, no ORJSON/UJSONResponse
  (both deprecated since FastAPI 0.131).
- HTML: Starlette's Jinja2Templates + TemplateResponse.
- SSE: FastAPI's built-in EventSourceResponse + ServerSentEvent (0.135+).
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterable

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

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

hub = Hub()

# Starlette's Jinja2Templates accepts a preconfigured Environment via env=.
# select_autoescape() is what Jinja2Templates(directory=...) uses itself, and
# it turns escaping on for "page.html". auto_reload=False: the template never
# changes, so skip Jinja's per-request "is the source up to date?" check.
templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.DictLoader({"page.html": PAGE_TEMPLATE}),
        autoescape=jinja2.select_autoescape(),
        auto_reload=False,
    )
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(ticker(hub.publish))
    yield
    task.cancel()


# Stario runs with STARIO_TRACER=noop; the counterpart here is FastAPI's
# documented switch for its built-in OpenTelemetry hooks (advanced/opentelemetry.md),
# which otherwise look up the tracer/meter/logger providers on every request.
app = FastAPI(lifespan=lifespan, telemetry={"tracing": False, "metrics": False, "logs": False})
if COMPRESS:
    from fastapi.middleware.gzip import GZipMiddleware

    # Starlette's default compresslevel is 9; BENCH_GZIP_LEVEL overrides it.
    app.add_middleware(
        GZipMiddleware, compresslevel=int(os.environ.get("BENCH_GZIP_LEVEL", "9"))
    )


class Message(BaseModel):
    message: str


class User(BaseModel):
    id: str
    name: str


class UserInput(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0, le=150)


class ValidatedUser(BaseModel):
    name: str
    age: int
    valid: bool


@app.get("/plaintext", response_class=PlainTextResponse)
async def plaintext() -> PlainTextResponse:
    return PlainTextResponse(HELLO)


@app.get("/json")
async def json_endpoint() -> Message:
    return Message(message=HELLO)


@app.get("/user/{user_id}")
async def get_user(user_id: str) -> User:
    return User(id=user_id, name=f"User {user_id}")


@app.post("/validate")
async def validate(body: UserInput) -> ValidatedUser:
    return ValidatedUser(name=body.name, age=body.age, valid=True)


@app.get("/page", response_class=HTMLResponse)
async def page(request: Request):
    return templates.TemplateResponse(request, "page.html", {"nav": NAV, "rows": ROWS})


@app.get("/sse", response_class=EventSourceResponse)
async def sse() -> AsyncIterable[ServerSentEvent]:
    queue = hub.subscribe()
    try:
        while True:
            tick, ts = await queue.get()
            # raw_data: send the Datastar payload as-is, not JSON-encoded.
            yield ServerSentEvent(
                event="datastar-patch-elements",
                raw_data=f"elements {sse_fragment(tick, ts)}",
            )
    finally:
        hub.unsubscribe(queue)
