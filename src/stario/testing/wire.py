"""One in-memory HTTP/1.1 connection through the production protocol.

`TestClient` does not call handlers itself. Each request becomes request
bytes, fed to a real `HttpProtocol` on a `MemoryTransport`. The protocol
parses, routes, dispatches, frames, and compresses exactly as `serve()`
does; `ResponseReader` parses the bytes it writes back. What a test sees is
what a browser would get.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from stario.http.headers import Headers
from stario_cython.protocol import Connections, HttpProtocol

# Response bodies that carry no bytes however the headers read (RFC 9110 §6.4.1).
_BODYLESS_STATUSES = frozenset({204, 304})


def request_bytes(
    method: str,
    path: str,
    query: str,
    headers: Headers,
    body: bytes,
) -> bytes:
    """Serialize one HTTP/1.1 request head and body."""
    target = f"{path}?{query}" if query else path
    lines = [f"{method} {target} HTTP/1.1".encode("latin-1")]
    for name, value in headers.unsafe_items():
        lines.append(name + b": " + value)
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


class ResponseReader:
    """Incremental HTTP/1.1 response parser fed by `MemoryTransport.write`.

    `wait_headers` resolves once the status line and headers are in.
    `next_chunk` yields de-framed body bytes as the handler writes them and
    `None` at the end of the body. Framing follows the headers: a
    `Content-Length`, `Transfer-Encoding: chunked`, nothing for HEAD / 204 /
    304, else the body ends when the server closes the connection.
    """

    __slots__ = (
        "_buf",
        "_chunks",
        "_event",
        "_framing",
        "_remaining",
        "closed",
        "done",
        "error",
        "head_request",
        "headers",
        "status_code",
    )

    def __init__(self, *, head_request: bool) -> None:
        self.head_request = head_request
        self.status_code: int | None = None
        self.headers: Headers | None = None
        self.done = False
        self.closed = False
        self.error: Exception | None = None
        self._buf = bytearray()
        self._chunks: deque[bytes] = deque()
        self._event = asyncio.Event()
        # "head" until the status line is in, then "length" / "chunked" /
        # "until_close".
        self._framing = "head"
        self._remaining = 0

    # -- producer side (transport) --------------------------------------

    def feed(self, data: bytes) -> None:
        if self.done or not data:
            return
        self._buf.extend(data)
        try:
            self._parse()
        except Exception as exc:  # malformed framing is a server bug
            self.error = exc
            self._finish()
            return
        self._event.set()

    def close(self) -> None:
        """The connection went away: end a close-delimited body, or fail early."""
        self.closed = True
        if self.done:
            return
        if self._framing == "head":
            self.error = RuntimeError(
                "Connection closed before the response headers were sent."
            )
        elif self._framing != "until_close":
            self.error = RuntimeError("Connection closed in the middle of the body.")
        self._finish()

    def _finish(self) -> None:
        self.done = True
        self._event.set()

    def _parse(self) -> None:
        if self._framing == "head":
            head, sep, _ = self._buf.partition(b"\r\n\r\n")
            if not sep:
                return
            del self._buf[: len(head) + 4]
            self._parse_head(bytes(head))
        if self._framing == "length":
            self._take_length()
        elif self._framing == "chunked":
            self._take_chunks()
        elif self._framing == "until_close" and self._buf:
            self._chunks.append(bytes(self._buf))
            self._buf.clear()

    def _parse_head(self, head: bytes) -> None:
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ", 2)
        if len(parts) < 2 or not parts[0].startswith(b"HTTP/1."):
            raise RuntimeError(f"Malformed status line: {lines[0]!r}")
        status = int(parts[1])
        headers = Headers()
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers.unsafe_add(bytes(name.lower()), bytes(value.strip()))
        if 100 <= status < 200:
            # Interim response (100 Continue): the final one follows.
            self._framing = "head"
            return
        self.status_code = status
        self.headers = headers
        if self.head_request or status in _BODYLESS_STATUSES:
            self._framing = "none"
            self._finish()
            return
        te = (headers.unsafe_get(b"transfer-encoding") or b"").lower()
        length = headers.unsafe_get(b"content-length")
        if b"chunked" in te:
            self._framing = "chunked"
        elif length is not None:
            self._framing = "length"
            self._remaining = int(length)
            if self._remaining == 0:
                self._finish()
        else:
            self._framing = "until_close"

    def _take_length(self) -> None:
        if self._buf:
            take = min(len(self._buf), self._remaining)
            self._chunks.append(bytes(self._buf[:take]))
            del self._buf[:take]
            self._remaining -= take
        if self._remaining == 0:
            self._finish()

    def _take_chunks(self) -> None:
        buf = self._buf
        while True:
            nl = buf.find(b"\r\n")
            if nl < 0:
                return
            size = int(bytes(buf[:nl]).split(b";", 1)[0], 16)
            if size == 0:
                # Last chunk; trailers are not produced by Stario, expect CRLF.
                end = buf.find(b"\r\n", nl + 2)
                if end < 0:
                    return
                del buf[: end + 2]
                self._finish()
                return
            start = nl + 2
            if len(buf) < start + size + 2:
                return
            self._chunks.append(bytes(buf[start : start + size]))
            del buf[: start + size + 2]

    # -- consumer side (client) ----------------------------------------

    async def _wait(self, deadline: float | None) -> None:
        self._event.clear()
        if deadline is None:
            await self._event.wait()
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(self._event.wait(), timeout=remaining)

    async def wait_headers(self, deadline: float | None) -> tuple[int, Headers]:
        """Status and headers, once the status line and header block are in."""
        while self.headers is None or self.status_code is None:
            if self.error is not None:
                raise self.error
            if self.done:
                raise RuntimeError("Response ended before headers were sent.")
            await self._wait(deadline)
        if self.error is not None and not self._chunks:
            raise self.error
        return self.status_code, self.headers

    async def next_chunk(self, deadline: float | None) -> bytes | None:
        while True:
            if self._chunks:
                return self._chunks.popleft()
            if self.error is not None:
                raise self.error
            if self.done:
                return None
            await self._wait(deadline)

    async def body(self, deadline: float | None) -> bytes:
        parts: list[bytes] = []
        while (chunk := await self.next_chunk(deadline)) is not None:
            parts.append(chunk)
        return b"".join(parts)


class MemoryTransport(asyncio.Transport):
    """The socket side of one test connection: writes land in a `ResponseReader`."""

    __slots__ = ("_closing", "_loop", "_protocol", "reader")

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        protocol: asyncio.BaseProtocol,
        reader: ResponseReader,
    ) -> None:
        super().__init__()
        self._loop = loop
        self._protocol = protocol
        self.reader = reader
        self._closing = False

    def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
        if self._closing:
            return
        self.reader.feed(bytes(data))

    def writelines(
        self, list_of_data: Iterable[bytes | bytearray | memoryview[Any]]
    ) -> None:
        for data in list_of_data:
            self.write(data)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.reader.close()
        # Like a socket transport: connection_lost runs on the next loop turn.
        self._loop.call_soon(self._protocol.connection_lost, None)

    def abort(self) -> None:
        self.close()

    def is_closing(self) -> bool:
        return self._closing

    def get_write_buffer_size(self) -> int:
        return 0

    def pause_reading(self) -> None:
        return None

    def resume_reading(self) -> None:
        return None

    def set_write_buffer_limits(
        self, high: int | None = None, low: int | None = None
    ) -> None:
        return None


@dataclass(slots=True)
class Exchange:
    """One request on its own connection: protocol, reader, and the tasks it started."""

    transport: MemoryTransport
    reader: ResponseReader
    span_id: UUID
    # The handler Task (None when the protocol answered without a handler,
    # e.g. a 308 or 400 target). The client awaits it so a buffered call
    # returns with the handler done and its span closed, and cancels it on
    # timeout. Work the handler started with `app.create_task` is not
    # included; `TestClient.drain_tasks` covers that.
    task: asyncio.Task[Any] | None = None

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()

    def disconnect(self) -> None:
        """Hang up like a client would; `c.disconnect` resolves on the handler side."""
        self.transport.close()

    async def finish(self, deadline: float | None) -> None:
        """Wait for this request's handler task; cancel it past `deadline`."""
        task = self.task
        if task is None or task.done():
            return
        timeout = None
        if deadline is not None:
            timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        _done, still = await asyncio.wait({task}, timeout=timeout)
        if still:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError


def open_exchange(
    *,
    app: Any,
    tracer: Any,
    compression: Any,
    connections: Connections,
    policy: dict[str, Any],
    raw: bytes,
    head_request: bool,
) -> Exchange:
    """Feed one request to a fresh protocol and capture what it starts."""
    loop = asyncio.get_running_loop()
    reader = ResponseReader(head_request=head_request)
    protocol = HttpProtocol(
        loop,
        app,
        tracer,
        compression,
        connections,
        timeout_cleanup="off",
        **policy,
    )
    transport = MemoryTransport(loop, protocol, reader)
    protocol.connection_made(transport)
    tracer.last_created = None
    # data_received is synchronous: the request span and the handler task
    # exist (or have already finished) when it returns.
    protocol.data_received(raw)
    span_id = tracer.last_created
    if span_id is None:
        raise RuntimeError("The protocol did not open a request span.")
    return Exchange(
        transport=transport,
        reader=reader,
        span_id=span_id,
        task=protocol.last_task,
    )
