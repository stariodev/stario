"""Read Datastar request signals."""

from typing import Any, TypedDict, cast

from stario.exceptions import RequestBodyError
from stario.http.request import Request
from stario.json import loads as json_loads


class FileSignal(TypedDict):
    """One file value nested inside a Datastar signals object.

    Datastar encodes file inputs bound with `data-bind` as signal values, not as
    `multipart/form-data`. `contents` is base64 text; `mime` may be `None`.

    ```python
    payload = await read_signals(req)
    avatar: FileSignal = payload["avatar"]
    ```
    """

    name: str
    contents: str
    mime: str | None


async def read_signals(req: Request, *, max_size: int | None = None) -> dict[str, Any]:
    """Parse the JSON signals blob Datastar sends.

    `GET`, `HEAD`, and `DELETE` use the `datastar` query parameter. Other methods
    use the request body, limited by `max_size` as in `Request.body()`. Pass a
    larger `max_size` for signals that carry a `FileSignal`.

    This is convenience parsing only. Incoming signals are untrusted; validate
    types, sizes, and nested shapes before using them as application data.
    Signal names pass through unchanged; names starting with `_` are Datastar
    local signals, so the browser does not send them by default and they are
    missing here. Nested client state arrives as nested JSON objects.

    ```python
    from stario import Route
    from stario.datastar import SSE, read_signals

    ACTION = Route("POST", "/action")

    async def action(c, w):
        sig = await read_signals(c.req)
        SSE(w).patch_signals({"n": int(sig.get("n", 0)) + 1})
    ```
    """
    if req.method.upper() in ("GET", "HEAD", "DELETE"):
        raw = req.query.get("datastar", "")
    else:
        raw = await req.body(max_size=max_size)

    if raw in ("", b""):
        return {}

    try:
        value = json_loads(raw)
    except (ValueError, RecursionError) as exc:
        # RecursionError: nesting deeper than the decoder's limit (stdlib
        # json raises it unwrapped; orjson caps depth with a ValueError).
        raise RequestBodyError(400, "Invalid Datastar signals") from exc
    if not isinstance(value, dict):
        raise RequestBodyError(400, "Invalid Datastar signals")
    return cast(dict[str, Any], value)
