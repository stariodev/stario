# pyright: reportMissingImports=false

import argparse
import asyncio

from sanic import Sanic, html, json, text

from apps.common import HELLO, Hub, datastar_event, jinja_page, sse_fragment, ticker

app = Sanic("stario_benchmark_sanic")
app.config.ACCESS_LOG = False
app.config.RESPONSE_TIMEOUT = 60
app.config.REQUEST_TIMEOUT = 60
app.config.KEEP_ALIVE_TIMEOUT = 75
hub = Hub()
render_page = jinja_page()


@app.before_server_start
async def start_ticker(app, loop):
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
    body = request.json or {}
    name = body.get("name")
    age = body.get("age")

    if not isinstance(name, str) or not name:
        return json({"error": "name must be a non-empty string"}, status=400)
    if not isinstance(age, int) or age < 0 or age > 150:
        return json(
            {"error": "age must be an integer between 0 and 150"},
            status=400,
        )

    return json({"name": name, "age": age, "valid": True})


@app.get("/page")
async def page(request):
    return html(render_page())


@app.get("/sse")
async def sse(request):
    response = await request.respond(
        content_type="text/event-stream", headers={"cache-control": "no-cache"}
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
        single_process=args.workers == 1,
        workers=args.workers,
        backlog=2048,  # Sanic's default is 100; Stario and Uvicorn use 2048
        access_log=False,
        debug=False,
    )
