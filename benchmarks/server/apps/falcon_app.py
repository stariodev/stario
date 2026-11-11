# pyright: reportMissingImports=false
"""Falcon (ASGI) target, written the way the Falcon docs lay it out.

- JSON in and out goes through resp.media / req.get_media(), with
  falcon.media.JSONHandler switched to msgspec. The JSONHandler docs name
  msgspec, orjson and rapidjson as the faster options, and the msgspec recipe
  uses prebuilt Encoder/Decoder objects.
- /validate follows the docs' msgspec recipe: a process_resource middleware
  turns the request media into the resource's POST_SCHEMA Struct, and an
  error handler turns msgspec.ValidationError into a 422.
- /sse uses the native resp.sse emitter that yields falcon.asgi.SSEvent.
- Falcon has no templating of its own ("batteries not included"), so /page
  is plain Jinja2 written into resp.text with MEDIA_HTML.
"""

import asyncio
from typing import Annotated, Any

import falcon
import falcon.asgi
import falcon.media
import jinja2
import msgspec

from apps.common import HELLO, NAV, PAGE_TEMPLATE, ROWS, Hub, sse_fragment, ticker

HELLO_BYTES = HELLO.encode()
hub = Hub()
page_template = jinja2.Environment(autoescape=True).from_string(PAGE_TEMPLATE)


class UserInput(msgspec.Struct):
    name: Annotated[str, msgspec.Meta(min_length=1)]
    age: Annotated[int, msgspec.Meta(ge=0, le=150)]


class Plaintext:
    async def on_get(self, req, resp):
        resp.content_type = falcon.MEDIA_TEXT  # Falcon's default media type is JSON
        resp.data = HELLO_BYTES  # resp.data is faster when the body is already bytes


class JsonResource:
    async def on_get(self, req, resp):
        resp.media = {"message": HELLO}


class UserResource:
    async def on_get(self, req, resp, user_id):
        resp.media = {"id": user_id, "name": f"User {user_id}"}


class ValidateResource:
    POST_SCHEMA = UserInput

    async def on_post(self, req, resp, userinput: UserInput):
        resp.media = {"name": userinput.name, "age": userinput.age, "valid": True}


class PageResource:
    async def on_get(self, req, resp):
        resp.content_type = falcon.MEDIA_HTML
        resp.text = page_template.render(nav=NAV, rows=ROWS)


class SseResource:
    async def on_get(self, req, resp):
        async def emitter():
            queue = hub.subscribe()
            try:
                while True:
                    tick, ts = await queue.get()
                    yield falcon.asgi.SSEvent(
                        event="datastar-patch-elements",
                        text="elements " + sse_fragment(tick, ts),
                    )
            finally:
                hub.unsubscribe(queue)

        resp.set_header("Cache-Control", "no-cache")
        resp.sse = emitter()  # content type defaults to text/event-stream


class MsgspecMiddleware:
    """The docs' msgspec recipe, in the async form that falcon.asgi requires."""

    async def process_resource(self, req, resp, resource, params: dict[str, Any]):
        if schema := getattr(resource, f"{req.method}_SCHEMA", None):
            params[schema.__name__.lower()] = msgspec.convert(await req.get_media(), schema)


class Ticker:
    async def process_startup(self, scope, event):
        self.task = asyncio.create_task(ticker(hub.publish))


async def handle_validation_error(req, resp, ex, params):
    raise falcon.HTTPUnprocessableEntity(description=str(ex))


json_handler = falcon.media.JSONHandler(
    dumps=msgspec.json.Encoder().encode,
    loads=msgspec.json.Decoder().decode,
)

app = falcon.asgi.App(middleware=[Ticker(), MsgspecMiddleware()])
app.req_options.media_handlers[falcon.MEDIA_JSON] = json_handler
app.resp_options.media_handlers[falcon.MEDIA_JSON] = json_handler
app.add_error_handler(msgspec.ValidationError, handle_validation_error)
app.add_route("/plaintext", Plaintext())
app.add_route("/json", JsonResource())
app.add_route("/user/{user_id}", UserResource())
app.add_route("/validate", ValidateResource())
app.add_route("/page", PageResource())
app.add_route("/sse", SseResource())
