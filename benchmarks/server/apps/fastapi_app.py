# pyright: reportMissingImports=false

import asyncio
import contextlib
import os

import ujson
from fastapi import FastAPI
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from apps.common import (
    COMPRESS,
    HELLO,
    Hub,
    datastar_event,
    jinja_page,
    sse_fragment,
    ticker,
)

JSON_MEDIA_TYPE = "application/json"
hub = Hub()
render_page = jinja_page()


@contextlib.asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(ticker(hub.publish))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
if COMPRESS:
    from starlette.middleware.gzip import GZipMiddleware

    # Starlette's default compresslevel is 9; BENCH_GZIP_LEVEL overrides it.
    app.add_middleware(
        GZipMiddleware, compresslevel=int(os.environ.get("BENCH_GZIP_LEVEL", "9"))
    )


class UserInput(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0, le=150)


def json_response(value: object, status_code: int = 200) -> Response:
    return Response(
        ujson.dumps(value).encode("utf-8"),
        status_code=status_code,
        media_type=JSON_MEDIA_TYPE,
    )


@app.get("/plaintext")
async def plaintext() -> PlainTextResponse:
    return PlainTextResponse(HELLO)


@app.get("/json")
async def json_endpoint() -> Response:
    return json_response({"message": HELLO})


@app.get("/user/{user_id}")
async def get_user(user_id: str) -> Response:
    return json_response({"id": user_id, "name": f"User {user_id}"})


@app.post("/validate")
async def validate(body: UserInput) -> Response:
    return json_response({"name": body.name, "age": body.age, "valid": True})


@app.get("/page")
async def page() -> HTMLResponse:
    return HTMLResponse(render_page())


@app.get("/sse")
async def sse() -> StreamingResponse:
    queue = hub.subscribe()

    async def events():
        try:
            while True:
                tick, ts = await queue.get()
                yield datastar_event(sse_fragment(tick, ts))
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"cache-control": "no-cache"}
    )
