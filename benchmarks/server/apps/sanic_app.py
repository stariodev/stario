# pyright: reportMissingImports=false
"""Sanic target: core Sanic with no sanic-ext, written the way the Sanic guide shows.

- JSON goes through sanic.json(). orjson is set once, globally, through
  Sanic(dumps=..., loads=...), as guide/basics/app.md shows. Without it Sanic
  uses ujson, which it installs on CPython.
- /validate reads request.json and checks the fields by hand. Core Sanic has
  no validator; sanic-ext's @validate would also turn on CORS, injection,
  OpenAPI and automatic HEAD/OPTIONS for every route (see the report).
- /page is plain Jinja2 with sync render(). Core Sanic has no templating.
- /sse uses response streaming: request.respond() and then response.send().
"""

import argparse
import asyncio

import jinja2
import orjson
from sanic import Sanic, html, json, text

from apps.common import HELLO, NAV, PAGE_TEMPLATE, ROWS, Hub, datastar_event, sse_fragment, ticker

app = Sanic("stario_benchmark_sanic", dumps=orjson.dumps, loads=orjson.loads)
app.config.KEEP_ALIVE_TIMEOUT = 75
hub = Hub()
page_template = jinja2.Environment(autoescape=True).from_string(PAGE_TEMPLATE)


@app.before_server_start
async def start_ticker(app):
    app.ctx.ticker = asyncio.create_task(ticker(hub.publish))


@app.get("/plaintext")
async def plaintext(request):
    return text(HELLO)


@app.get("/json")
async def json_endpoint(request):
    return json({"message": HELLO})


@app.get("/user/<user_id>")
async def get_user(request, user_id: str):
    return json({"id": user_id, "name": f"User {user_id}"})


@app.post("/validate")
async def validate(request):
    body = request.json  # malformed JSON -> BadRequest (400) from Sanic
    if not isinstance(body, dict):
        return json({"error": "body must be a JSON object"}, status=400)
    name, age = body.get("name"), body.get("age")
    if not isinstance(name, str) or not name:
        return json({"error": "name must be a non-empty string"}, status=400)
    if type(age) is not int or not 0 <= age <= 150:
        return json({"error": "age must be an integer between 0 and 150"}, status=400)
    return json({"name": name, "age": age, "valid": True})


@app.get("/page")
async def page(request):
    return html(page_template.render(nav=NAV, rows=ROWS))


@app.get("/sse")
async def sse(request):
    response = await request.respond(
        content_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )
    queue = hub.subscribe()
    try:
        while True:
            tick, ts = await queue.get()
            await response.send(datastar_event(sse_fragment(tick, ts)))
    finally:
        hub.unsubscribe(queue)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    app.run(
        host=args.host,
        port=args.port,
        single_process=args.workers == 1,  # no worker manager process
        workers=args.workers,
        backlog=2048,  # Sanic's default is 100; Stario and Uvicorn use 2048
        access_log=False,
        debug=False,
        motd=False,
    )
