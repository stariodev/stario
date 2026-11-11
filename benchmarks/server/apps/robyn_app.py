# pyright: reportMissingImports=false
"""Robyn target, written the way the Robyn docs show.

- Handlers return a plain dict or str. Robyn's Rust executor turns a dict into
  JSON with orjson and a str into text/plain, without building a Python
  Response. A path parameter arrives as a typed function argument.
- /validate annotates a Pydantic model, so Robyn runs model_validate_json on
  the raw body. A bad body gets an automatic 422.
- /page uses robyn.templating.JinjaTemplate. It only loads templates from a
  directory, so the shared template is written to page.html in a temp
  directory at import. HTML templates are autoescaped.
- /sse uses SSEResponse and SSEMessage with an async generator that the
  in-process Hub feeds. The ticker starts in a startup handler on the same
  event loop.
"""

import argparse
import asyncio
import os
import pathlib
import tempfile

from pydantic import BaseModel, Field
from robyn import Robyn, SSEMessage, SSEResponse
from robyn.templating import JinjaTemplate

from apps.common import HELLO, NAV, PAGE_TEMPLATE, ROWS, Hub, sse_fragment, ticker

# const=True makes Robyn cache the first response in Rust and never call the
# handler again. Every other target runs its handler per request, so the
# comparison leaves it off; BENCH_ROBYN_CONST=1 turns it back on.
CONST = os.environ.get("BENCH_ROBYN_CONST") == "1"

_templates = pathlib.Path(tempfile.mkdtemp(prefix="robyn-templates-"))
(_templates / "page.html").write_text(PAGE_TEMPLATE, encoding="utf-8")
templates = JinjaTemplate(str(_templates))
hub = Hub()

app = Robyn(__file__)


class UserInput(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0, le=150)


async def start_ticker():
    asyncio.create_task(ticker(hub.publish))


app.startup_handler(start_ticker)


@app.get("/plaintext", const=CONST)
def plaintext():
    return HELLO


@app.get("/json", const=CONST)
def json_endpoint():
    return {"message": HELLO}


@app.get("/user/:user_id")
def get_user(user_id: str):
    return {"id": user_id, "name": f"User {user_id}"}


@app.post("/validate")
def validate(body: UserInput):
    return {"name": body.name, "age": body.age, "valid": True}


@app.get("/page")
def page():
    return templates.render_template("page.html", nav=NAV, rows=ROWS)


@app.get("/sse")
async def sse():
    async def events():
        queue = hub.subscribe()
        try:
            while True:
                tick, ts = await queue.get()
                yield SSEMessage("elements " + sse_fragment(tick, ts), event="datastar-patch-elements")
        finally:
            hub.unsubscribe(queue)

    return SSEResponse(events())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args, _ = parser.parse_known_args()  # Robyn parses --processes/--workers/--log-level itself
    app.start(host=args.host, port=args.port, _check_port=False)
