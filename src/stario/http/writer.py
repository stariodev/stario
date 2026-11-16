"""Writer protocol: status, headers, and body for one response.

The Cython ``RequestHandle`` implements this (one per handler call; it raises
once the handler has returned). TestClient has its own writer.
There is no Python production writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from stario.http.headers import Headers
    from stario.json import JsonValue
    from stario.markup.types import HtmlElement


class Writer(Protocol):
    """Low-level HTTP response for one request.

    Set headers on ``headers``, then ``respond`` for a whole body or
    ``write_headers`` followed by ``write`` / ``end`` for streaming.
    """

    headers: Headers

    @property
    def status_code(self) -> int | None:
        """HTTP status after headers were sent, else ``None``."""
        ...

    @property
    def started(self) -> bool:
        """``True`` once the status line and headers have been sent."""
        ...

    @property
    def completed(self) -> bool:
        """``True`` after ``end`` / ``respond`` / ``abort`` finished the response."""
        ...

    @property
    def closing(self) -> bool:
        """Whether the connection is closing or closed."""
        ...

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        """Send a full response in one shot."""
        ...

    def abort(self) -> None:
        """Close without framing a failed started response as complete."""
        ...

    def write_headers(self, status_code: int) -> Self:
        """Send the status line and current ``headers`` (at most once).

        HEAD is handled by the server: the headers go out exactly as declared
        (``Content-Length`` keeps the GET entity size) and the body is dropped.
        """
        ...

    def write(self, data: bytes) -> Self:
        """Write one body chunk; sends default ``200`` headers if needed."""
        ...

    async def drain(self) -> None:
        """Wait until the client has taken enough of what was written.

        ``write()`` never blocks, so a loop writing large chunks to a slow
        client would buffer everything in memory. ``await w.drain()`` after
        each chunk keeps that bounded. Returns immediately while the
        connection keeps up, or once it is closed (check ``closing``).
        """
        ...

    def end(self, data: bytes | None = None) -> None:
        """Finish the response. Optional final body bytes."""
        ...

    def text(self, body: str, status: int = 200) -> None:
        """Send `body` as UTF-8 ``text/plain`` and finish the response."""
        ...

    def json(self, value: JsonValue, status: int = 200) -> None:
        """Serialize `value` to compact UTF-8 JSON and finish the response."""
        ...

    def html(self, content: bytes | str | HtmlElement, status: int = 200) -> None:
        """Send `bytes`, `str`, or markup nodes as ``text/html``."""
        ...

    def redirect(self, url: str, status: int = 307) -> None:
        """Send a redirect with an empty body and a ``Location`` header."""
        ...

    def empty(self, status: int = 204) -> None:
        """Finish with no message body (default ``204 No Content``)."""
        ...


__all__ = ["Writer"]
