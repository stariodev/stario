# pyright: reportMissingImports=false

import asyncio

import falcon.asgi
import ujson

from apps.common import HELLO, Hub, datastar_event, jinja_page, sse_fragment, ticker

JSON_MEDIA_TYPE = falcon.MEDIA_JSON
hub = Hub()
render_page = jinja_page()


def json_body(resp, value, status=falcon.HTTP_200):
    resp.status = status
    resp.content_type = JSON_MEDIA_TYPE
    resp.data = ujson.dumps(value).encode("utf-8")


class Plaintext:
    async def on_get(self, req, resp):
        resp.text = HELLO


class JsonResource:
    async def on_get(self, req, resp):
        json_body(resp, {"message": HELLO})


class UserResource:
    async def on_get(self, req, resp, user_id):
        json_body(resp, {"id": user_id, "name": f"User {user_id}"})


class ValidateResource:
    async def on_post(self, req, resp):
        body = ujson.loads(await req.stream.read())
        name = body.get("name")
        age = body.get("age")

        if not isinstance(name, str) or not name:
            json_body(resp, {"error": "name must be a non-empty string"}, falcon.HTTP_400)
            return
        if not isinstance(age, int) or age < 0 or age > 150:
            json_body(
                resp,
                {"error": "age must be an integer between 0 and 150"},
                falcon.HTTP_400,
            )
            return

        json_body(resp, {"name": name, "age": age, "valid": True})


class PageResource:
    async def on_get(self, req, resp):
        resp.content_type = falcon.MEDIA_HTML
        resp.text = render_page()


class SseResource:
    async def on_get(self, req, resp):
        queue = hub.subscribe()

        async def events():
            try:
                while True:
                    tick, ts = await queue.get()
                    yield datastar_event(sse_fragment(tick, ts))
            finally:
                hub.unsubscribe(queue)

        resp.content_type = "text/event-stream"
        resp.set_header("cache-control", "no-cache")
        resp.stream = events()


class Ticker:
    async def process_startup(self, scope, event):
        self.task = asyncio.create_task(ticker(hub.publish))


app = falcon.asgi.App(middleware=[Ticker()])
app.add_route("/plaintext", Plaintext())
app.add_route("/json", JsonResource())
app.add_route("/user/{user_id}", UserResource())
app.add_route("/validate", ValidateResource())
app.add_route("/page", PageResource())
app.add_route("/sse", SseResource())
