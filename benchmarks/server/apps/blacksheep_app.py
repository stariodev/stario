# pyright: reportMissingImports=false

import asyncio
import os

import ujson
from blacksheep import Application, Content, Request, Response, StreamedContent
from blacksheep.server.responses import text as text_response

from apps.common import (
    COMPRESS,
    HELLO,
    Hub,
    datastar_event,
    jinja_page,
    sse_fragment,
    ticker,
)

JSON_CONTENT_TYPE = b"application/json"
HTML_CONTENT_TYPE = b"text/html; charset=utf-8"

app = Application(show_error_details=False)
hub = Hub()
render_page = jinja_page()

if COMPRESS:
    from blacksheep.server.compression import GzipMiddleware

    # BlackSheep's default compresslevel is 5; BENCH_GZIP_LEVEL overrides it.
    app.middlewares.append(
        GzipMiddleware(comp_level=int(os.environ.get("BENCH_GZIP_LEVEL", "5")))
    )


async def start_ticker(application: Application) -> None:
    application.services_ticker = asyncio.create_task(ticker(hub.publish))


app.on_start += start_ticker


def json_response(value: object, status: int = 200) -> Response:
    return Response(
        status,
        content=Content(JSON_CONTENT_TYPE, ujson.dumps(value).encode("utf-8")),
    )


@app.router.get("/plaintext")
async def plaintext():
    return text_response(HELLO)


@app.router.get("/json")
async def json_endpoint() -> Response:
    return json_response({"message": HELLO})


@app.router.get("/user/{user_id}")
async def get_user(user_id: str) -> Response:
    return json_response({"id": user_id, "name": f"User {user_id}"})


@app.router.post("/validate")
async def validate(request: Request) -> Response:
    body = ujson.loads(await request.read())
    name = body.get("name")
    age = body.get("age")

    if not isinstance(name, str) or not name:
        return json_response({"error": "name must be a non-empty string"}, 400)
    if not isinstance(age, int) or age < 0 or age > 150:
        return json_response(
            {"error": "age must be an integer between 0 and 150"},
            400,
        )

    return json_response({"name": name, "age": age, "valid": True})


@app.router.get("/page")
async def page() -> Response:
    return Response(200, content=Content(HTML_CONTENT_TYPE, render_page().encode("utf-8")))


@app.router.get("/sse")
async def sse(request: Request) -> Response:
    queue = hub.subscribe()

    async def events():
        try:
            while True:
                tick, ts = await queue.get()
                yield datastar_event(sse_fragment(tick, ts))
        finally:
            hub.unsubscribe(queue)

    response = Response(200, content=StreamedContent(b"text/event-stream", events))
    response.add_header(b"cache-control", b"no-cache")
    return response
