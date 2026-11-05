"""Cython protocol edge cases, including header/idle timeouts and pipeline cap."""

from __future__ import annotations

import asyncio
import gc
import logging
import random

import pytest

from stario import App, Relay, Route
from stario.datastar import SSE, read_signals
from stario.testing.tracer import TestTracer
from stario_cython.exchange import Request
from stario_cython.protocol import Connections, HttpProtocol
from tests.cython.http import (
    RecordingTransport,
    connection_set,
    free_port,
    make_protocol,
    response_status,
    response_statuses,
)
from tests.helpers import assert_status_span

# Production sweeps with the Date tick (1s). Tests use a 50ms registry (http.py).
# Timeouts here must exceed two periods; waits must exceed timeout + one period.
_TIMEOUT = 0.15
_WAIT = 0.40
_TRICKLE_PAUSE = 0.08


class HoldingTransport(RecordingTransport):
    """Keep write buffers as-is so a reused bytearray would corrupt later copies."""

    def write(self, data: bytes | bytearray | memoryview) -> None:
        assert not self._closing, "write after transport close"
        self.writes.append(data)  # pyrefly: ignore[bad-argument-type]


def _attach(app: App | None = None, **kwargs):
    loop = asyncio.get_running_loop()
    if app is None:
        app = App()
    proto = make_protocol(loop, app, **kwargs)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    return proto, app, transport


async def _drain(app: App) -> None:
    await asyncio.sleep(0)
    await app.drain_tasks()


@pytest.mark.asyncio
async def test_chunked_writes_stay_stable_if_transport_holds_buffers() -> None:
    """uvloop may keep a memoryview of the last write; each flush must be bytes."""
    loop = asyncio.get_running_loop()
    app = App()

    async def stream(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.write_headers(200)
        w.write(b"alpha")
        w.write(b"bravo")
        w.write(b"charlie")
        w.end()

    app.add(Route("GET /"), stream)
    proto = make_protocol(loop, app)
    transport = HoldingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        raw = b"".join(bytes(chunk) for chunk in transport.writes)
        assert b"5\r\nalpha\r\n" in raw
        assert b"5\r\nbravo\r\n" in raw
        assert b"7\r\ncharlie\r\n" in raw
        assert raw.count(b"alpha") == 1
        assert raw.count(b"bravo") == 1
        assert raw.count(b"charlie") == 1
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_garbage_bytes_yield_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"\x00\xff\xfe not http \r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_upgrade_request_yields_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(
            b"GET / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n"
            b"\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_write_then_raise_logs_and_keeps_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NoOp + eager complete must still log; the 200 already on the wire stays."""

    async def handler(_c, w) -> None:
        w.text("ok")
        raise RuntimeError("after write")

    app = App()
    app.add(Route("GET /x"), handler)
    proto, app, transport = _attach(app)
    try:
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            proto.data_received(b"GET /x HTTP/1.1\r\nHost: t\r\n\r\n")
            await _drain(app)
        raw = b"".join(transport.writes)
        assert response_status(transport.writes) == 200
        assert b"ok" in raw
        assert b"500" not in raw
        assert "Handler failed" in caplog.text
        assert "after write" in caplog.text
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_invalid_incoming_header_names_return_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nBad Name: v\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"GET / HTTP/1.1\r\nHost: t\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: gzip\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n",
    ],
    ids=["duplicate_content_length", "unsupported_te", "cl_plus_te"],
)
async def test_invalid_content_length_or_transfer_encoding_returns_400(
    payload: bytes,
) -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(payload)
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_split_headers_then_body() -> None:
    app = App()
    bodies: list[bytes] = []

    async def echo(c, w) -> None:
        bodies.append(await c.req.body())
        w.text("ok")

    app.add(Route("POST /"), echo)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 5\r\n")
        proto.data_received(b"\r\nhello")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert bodies == [b"hello"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_invalid_incoming_header_values_return_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nX-Test: ok\x00bad\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_header_total_over_limit_returns_431() -> None:
    limit = 512
    app = App()
    hits = 0

    async def handler(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("should not run")

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(app=app, max_header_bytes=limit)
    try:
        pad = b"x" * (limit + 32)
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\nX-Pad: " + pad + b"\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 431
        assert hits == 0
        assert not transport.is_closing()
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_statuses(transport.writes) == [431, 200]
        assert hits == 1
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_keep_alive_431_does_not_keep_header_timer() -> None:
    app = App()

    async def handler(_c, w) -> None:
        w.text("ok")

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(
        app=app, max_header_bytes=512, header_timeout=_TIMEOUT, keep_alive_timeout=5.0
    )
    try:
        pad = b"x" * 544
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\nX-Pad: " + pad + b"\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 431
        await asyncio.sleep(_WAIT)
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_body_over_limit_returns_413() -> None:
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        await c.req.body()
        w.text("ok")

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("ok")

    app.add(Route("POST /"), read_body)
    app.add(Route("GET /"), hello)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n" + (b"y" * 100)
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
        assert not transport.is_closing()
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_statuses(transport.writes) == [413, 200]
        assert hits == 1
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_body_over_default_with_larger_max_size_is_read() -> None:
    """max_body_bytes is only the default: a read may accept more."""
    app = App()
    seen: list[int] = []

    async def read_body(c, w) -> None:
        seen.append(len(await c.req.body(max_size=200)))
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n" + (b"y" * 100)
        )
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert seen == [100]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_large_body_over_default_with_larger_max_size_is_read() -> None:
    """A body past SMALL_BODY streams in after dispatch, above the default."""
    app = App()
    seen: list[int] = []
    size = 300 * 1024

    async def read_body(c, w) -> None:
        seen.append(len(await c.req.body(max_size=size)))
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: "
            + str(size).encode()
            + b"\r\n\r\n"
        )
        await asyncio.sleep(0)
        sent = 0
        while sent < size:
            step = min(64 * 1024, size - sent)
            proto.data_received(b"y" * step)
            sent += step
            await asyncio.sleep(0)
        await _drain(app)
        assert seen == [size]
        assert response_status(transport.writes) == 200
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_chunked_body_over_max_size_returns_413() -> None:
    app = App()

    async def read_body(c, w) -> None:
        await c.req.body(max_size=50)
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=1000)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"40\r\n" + (b"y" * 64) + b"\r\n0\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_stream_uses_default_and_max_size() -> None:
    app = App()
    results: list[object] = []

    async def stream_body(c, w) -> None:
        limit = int(c.req.query.get("limit", "0")) or None
        total = 0
        try:
            async for chunk in c.req.stream(max_size=limit):
                total += len(chunk)
        except Exception as exc:
            results.append(type(exc).__name__)
            raise
        results.append(total)
        w.text("ok")

    app.add(Route("POST /"), stream_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n" + (b"y" * 100)
        )
        await _drain(app)
        proto.data_received(
            b"POST /?limit=100 HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n"
            + (b"y" * 100)
        )
        await _drain(app)
        assert response_statuses(transport.writes) == [413, 200]
        assert results == ["RequestBodyError", 100]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_declared_body_over_limit_fails_before_continue() -> None:
    """A read checks Content-Length first: 413 and no 100 Continue."""
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        nonlocal hits
        hits += 1
        await c.req.body()
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nExpect: 100-continue\r\n"
            b"Content-Length: 100\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
        assert b"100 Continue" not in b"".join(transport.writes)
        assert hits == 1
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_continue_is_sent_when_max_size_accepts_the_body() -> None:
    app = App()
    seen: list[int] = []

    async def read_body(c, w) -> None:
        seen.append(len(await c.req.body(max_size=100)))
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nExpect: 100-continue\r\n"
            b"Content-Length: 100\r\n\r\n"
        )
        await asyncio.sleep(0)
        assert b"100 Continue" in b"".join(transport.writes)
        proto.data_received(b"y" * 100)
        await _drain(app)
        assert seen == [100]
        assert response_statuses(transport.writes)[-1] == 200
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_huge_declared_body_over_limit_closes() -> None:
    """A 413 on a body past the 256 KiB drain cap closes (read-DoS)."""
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        nonlocal hits
        hits += 1
        await c.req.body()
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 262145\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
        assert hits == 1
        proto.data_received(b"y" * 100)
        await _drain(app)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_unread_large_body_drains_up_to_default_then_closes() -> None:
    app = App()

    async def ignore_body(_c, w) -> None:
        w.text("ok")

    app.add(Route("POST /"), ignore_body)
    proto, app, transport = _attach(app=app, max_body_bytes=1000)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 262145\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 200
        proto.data_received(b"y" * 500)
        await _drain(app)
        assert not transport.is_closing()
        proto.data_received(b"y" * 600)
        await _drain(app)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_abandoned_large_read_drains_only_up_to_default() -> None:
    """A handler that stops a large-limit stream drains the rest only up to the default."""
    app = App()

    async def first_chunk(c, w) -> None:
        async for _chunk in c.req.stream(max_chunk=64, max_size=2**31 - 1):
            break
        w.text("ok")

    app.add(Route("POST /"), first_chunk)
    proto, app, transport = _attach(app=app, max_body_bytes=1000)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 1000000\r\n\r\n"
            + (b"y" * 100)
        )
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert not transport.is_closing()
        proto.data_received(b"y" * 2000)
        await _drain(app)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_content_length_above_read_cap_fails_before_handler() -> None:
    """No read can accept more than 2^31-1 bytes: 413 at headers, close."""
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        nonlocal hits
        hits += 1
        await c.req.body(max_size=2**40)
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 2147483648\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
        assert hits == 0
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_queued_upload_over_default_is_read_after_the_queue() -> None:
    """A pipelined upload waits paused behind a slow response, then reads past the default."""
    app = App()
    release = asyncio.Event()
    seen: list[int] = []
    size = 1024 * 1024

    async def slow(_c, w) -> None:
        await release.wait()
        w.text("slow")

    async def read_body(c, w) -> None:
        seen.append(len(await c.req.body(max_size=size)))
        w.text("ok")

    app.add(Route("GET /slow"), slow)
    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: "
            + str(size).encode()
            + b"\r\n\r\n"
        )
        await asyncio.sleep(0)
        sent = 0
        while sent <= 300 * 1024:
            proto.data_received(b"y" * (64 * 1024))
            sent += 64 * 1024
        assert "pause" in transport.reading_calls
        assert seen == []
        release.set()
        for _ in range(3):
            await asyncio.sleep(0)
        while sent < size:
            step = min(64 * 1024, size - sent)
            proto.data_received(b"y" * step)
            sent += step
            await asyncio.sleep(0)
        await _drain(app)
        assert seen == [size]
        assert response_statuses(transport.writes) == [200, 200]
    finally:
        release.set()
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_keep_alive_413_does_not_keep_header_timer() -> None:
    app = App()

    async def read_body(c, w) -> None:
        await c.req.body()
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(
        app=app, max_body_bytes=20, header_timeout=_TIMEOUT, keep_alive_timeout=5.0
    )
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n" + (b"y" * 100)
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
        await asyncio.sleep(_WAIT)
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_keep_alive_413_idle_times_out() -> None:
    app = App()

    async def read_body(c, w) -> None:
        await c.req.body()
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(
        app=app, max_body_bytes=20, header_timeout=5.0, keep_alive_timeout=_TIMEOUT
    )
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n" + (b"y" * 100)
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
        assert not transport.is_closing()
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_keep_alive_413_deferred_body_then_second_get() -> None:
    """A small body over the default dispatches once complete; 413 keeps the socket."""
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        await c.req.body()
        w.text("ok")

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("ok")

    app.add(Route("POST /"), read_body)
    app.add(Route("GET /"), hello)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n"
        )
        await _drain(app)
        assert transport.writes == []
        proto.data_received(b"y" * 100)
        await _drain(app)
        assert response_status(transport.writes) == 413
        assert not transport.is_closing()
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_statuses(transport.writes) == [413, 200]
        assert hits == 1
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_declared_body_over_limit_with_connection_close() -> None:
    app = App()

    async def read_body(c, w) -> None:
        await c.req.body()
        w.text("ok")

    app.add(Route("POST /"), read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n"
            b"Content-Length: 100\r\n\r\n" + (b"y" * 100)
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_random_inputs_do_not_raise() -> None:
    rng = random.Random(42)
    for _ in range(15):
        proto, app, transport = _attach()
        try:
            proto.data_received(rng.randbytes(rng.randint(0, 512)))
            await _drain(app)
            status = response_status(transport.writes)
            assert status in (None, 400, 404, 413, 431, 414, 505)
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
async def test_percent_encoded_path_reaches_handler() -> None:
    app = App()
    seen: list[str] = []

    async def handler(c, w) -> None:
        seen.append(c.req.path)
        w.text("ok")

    app.add(Route("GET /hello world"), handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /hello%20world HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert seen == ["/hello world"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_percent_encoded_letter_matches_static_route() -> None:
    """`%70` must decode before lookup so /%70laintext hits /plaintext."""
    app = App()
    seen: list[str] = []

    async def plaintext(c, w) -> None:
        seen.append(c.req.path)
        w.text("plain")

    app.add(Route("GET /plaintext"), plaintext)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /%70laintext HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert seen == ["/plaintext"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_req_identity_stable_within_handler() -> None:
    app = App()
    ids: list[int] = []

    async def handler(c, w) -> None:
        first = c.req
        second = c.req
        ids.append(id(first))
        ids.append(id(second))
        assert first is second
        assert first.path == "/stable"
        assert first.method == "GET"
        w.text("ok")

    app.add(Route("GET /stable"), handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /stable HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert ids[0] == ids[1]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_percent_encoded_slash_does_not_change_route_structure() -> None:
    app = App()
    seen: list[str] = []

    async def wildcard(c, w) -> None:
        seen.append(c.match.params["name"])
        w.text("wildcard")

    async def nested(_c, w) -> None:
        seen.append("nested")
        w.text("nested")

    app.add(Route("GET /files/{name}"), wildcard)
    app.add(Route("GET /files/a/b"), nested)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /files/a%2Fb HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        # One segment, fully decoded: the encoded slash is data, not a separator.
        assert seen == ["a/b"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"GET /caf% HTTP/1.1\r\nHost: t\r\n\r\n",
        b"GET /%C3 HTTP/1.1\r\nHost: t\r\n\r\n",
        b"GET /caf\xc3 HTTP/1.1\r\nHost: t\r\n\r\n",
    ],
    ids=["truncated_percent", "invalid_utf8_percent", "invalid_utf8_path"],
)
async def test_invalid_path_encoding_returns_400(payload: bytes) -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(payload)
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_chunked_request_body_reaches_handler() -> None:
    app = App()
    bodies: list[bytes] = []

    async def echo(c, w) -> None:
        bodies.append(await c.req.body())
        w.text("ok")

    app.add(Route("POST /"), echo)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"5\r\nhello\r\n"
            b"6\r\n world\r\n"
            b"0\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert bodies == [b"hello world"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_expect_100_continue_sends_interim_response() -> None:
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.text(body.decode())

    app.add(Route("POST /"), echo)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 5\r\n"
            b"Expect: 100-continue\r\n"
            b"\r\n"
        )
        await asyncio.sleep(0)
        raw = b"".join(transport.writes)
        assert raw.startswith(b"HTTP/1.1 100 Continue\r\n\r\n")

        proto.data_received(b"hello")
        await _drain(app)
        assert response_statuses(transport.writes) == [100, 200]
        assert b"".join(transport.writes).endswith(b"hello")
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_close_header_closes_socket_after_response() -> None:
    app = App()

    async def handler(_c, w) -> None:
        w.text("bye")

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_keep_alive_uses_a_new_request_object() -> None:
    app = App()
    ids: list[int] = []
    held: list[object] = []

    async def echo(c, w) -> None:
        held.append(c.req)
        ids.append(id(c.req))
        w.text("ok")

    app.add(Route("GET /"), echo)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert len(ids) == 2
        assert ids[0] != ids[1]
        assert held[0] is not held[1]
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_keep_alive_serves_second_request_on_same_connection() -> None:
    app = App()
    hits: list[str] = []

    async def a(_c, w) -> None:
        hits.append("a")
        w.text("a")

    async def b(_c, w) -> None:
        hits.append("b")
        w.text("b")

    app.add(Route("GET /a"), a)
    app.add(Route("GET /b"), b)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /a HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert hits == ["a"]

        proto.data_received(b"GET /b HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert hits == ["a", "b"]
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_pipelined_requests_are_served_in_order() -> None:
    app = App()
    order: list[str] = []

    async def slow(_c, w) -> None:
        order.append("slow.start")
        await asyncio.sleep(0.01)
        order.append("slow.end")
        w.text("slow")

    async def fast(_c, w) -> None:
        order.append("fast")
        w.text("fast")

    app.add(Route("GET /slow"), slow)
    app.add(Route("GET /fast"), fast)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /fast HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        await _drain(app)
        await app.drain_tasks()
        await _drain(app)
        assert order == ["slow.start", "slow.end", "fast"]
        assert response_statuses(transport.writes) == [200, 200]
        raw = b"".join(transport.writes)
        assert raw.index(b"slow") < raw.index(b"fast")
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_lost_signals_disconnect_without_cancelling_handler() -> None:
    app = App()
    started = asyncio.Event()
    hang = asyncio.Event()
    finished = asyncio.Event()

    async def handler(c, _w) -> None:
        started.set()
        async with c.alive():
            await hang.wait()
        finished.set()

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await started.wait()
        transport.close()
        await asyncio.sleep(0)
        await _drain(app)
        assert finished.is_set()
    finally:
        hang.set()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_lost_lets_sse_handler_run_post_alive_cleanup() -> None:
    app = App()
    relay = Relay[str]()
    cleanup_events: list[str] = []

    async def subscribe(c, w) -> None:
        async with relay.subscribe("*") as live:
            SSE(w).open()
            c.span.event("connected", {})
            async for _subject, _ in c.alive(live):
                pass
        cleanup_events.append("disconnected")
        c.span.event("disconnected", {})

    app.add(Route("GET /subscribe"), subscribe)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /subscribe HTTP/1.1\r\nHost: t\r\n\r\n")
        await asyncio.sleep(0)
        transport.close()
        await _drain(app)
        assert cleanup_events == ["disconnected"]
    finally:
        await _drain(app)


@pytest.mark.asyncio
async def test_close_if_idle_closes_keep_alive_socket() -> None:
    app = App()

    async def handler(_c, w) -> None:
        w.text("ok")

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert not transport.is_closing()
        assert proto.close_if_idle() is True
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_close_if_idle_skips_in_flight_handler() -> None:
    app = App()
    started = asyncio.Event()
    hang = asyncio.Event()

    async def handler(_c, _w) -> None:
        started.set()
        await hang.wait()

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await started.wait()
        assert proto.close_if_idle() is False
        assert not transport.is_closing()
    finally:
        hang.set()
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_with_no_data_times_out_headers() -> None:
    _proto, app, transport = _attach(header_timeout=_TIMEOUT)
    try:
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_partial_headers_time_out() -> None:
    proto, app, transport = _attach(header_timeout=_TIMEOUT)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\n")
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_trickle_headers_do_not_reset_header_timeout() -> None:
    proto, app, transport = _attach(header_timeout=_TIMEOUT)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\n")
        await asyncio.sleep(_TRICKLE_PAUSE)
        proto.data_received(b"Host: t\r\n")
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_stalled_deferred_small_body_times_out() -> None:
    """Small Content-Length bodies dispatch at message-complete. Incomplete
    bodies must not hang: the body deadline answers 408 and closes."""
    app = App()
    hits = 0

    async def handler(c, w) -> None:
        nonlocal hits
        hits += 1
        await c.req.body()
        w.text("ok")

    app.add(Route("POST /"), handler)
    proto, app, transport = _attach(app=app, header_timeout=5.0, body_timeout=_TIMEOUT)
    try:
        proto.data_received(b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 8\r\n\r\n")
        await asyncio.sleep(_WAIT)
        assert response_status(transport.writes) == 408
        assert transport.is_closing()
        assert hits == 0
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_complete_request_does_not_keep_header_timer() -> None:
    app = App()

    async def handler(_c, w) -> None:
        w.text("ok")

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(
        app=app, header_timeout=_TIMEOUT, keep_alive_timeout=5.0
    )
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        await asyncio.sleep(_WAIT)
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_idle_keep_alive_times_out() -> None:
    app = App()

    async def handler(_c, w) -> None:
        w.text("ok")

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(
        app=app, header_timeout=5.0, keep_alive_timeout=_TIMEOUT
    )
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert not transport.is_closing()
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_in_flight_handler_is_not_header_timed_out() -> None:
    app = App()
    started = asyncio.Event()
    hang = asyncio.Event()

    async def handler(_c, w) -> None:
        started.set()
        await hang.wait()
        w.text("ok")

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(app=app, header_timeout=_TIMEOUT)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await started.wait()
        await asyncio.sleep(_WAIT)
        assert not transport.is_closing()
    finally:
        hang.set()
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_http10_transfer_encoding_closes_after_response() -> None:
    app = App()

    async def handler(c, w) -> None:
        w.text((await c.req.body()).decode())

    app.add(Route("POST /"), handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(
            b"POST / HTTP/1.0\r\nConnection: keep-alive\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n2\r\nhi\r\n0\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_pipelined_trickled_headers_time_out_after_response() -> None:
    """An earlier response finishing must not swap the header deadline for idle."""
    app = App()
    release = asyncio.Event()

    async def handler(_c, w) -> None:
        await release.wait()
        w.text("ok")

    app.add(Route("GET /"), handler)
    proto, app, transport = _attach(
        app=app, header_timeout=_TIMEOUT, keep_alive_timeout=5.0
    )
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\nGET / HTTP/1.1\r\n")
        await asyncio.sleep(_TIMEOUT / 3)
        release.set()
        await _drain(app)
        assert response_status(transport.writes) == 200
        for _ in range(8):
            if transport.is_closing():
                break
            proto.data_received(b"X")
            await asyncio.sleep(_TRICKLE_PAUSE)
        assert transport.is_closing()
    finally:
        release.set()
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_stalled_chunked_body_aborts_without_hanging() -> None:
    """Chunked / large bodies dispatch at headers-complete. A stalled
    ``body()`` wait must abort via the shared connection sweeper, not hang.
    The protocol answers the raised ``RequestBodyError`` with 408.
    """
    app = App()

    async def handler(c, w) -> None:
        await c.req.body()
        w.text("ok")

    app.add(Route("POST /"), handler)
    proto, app, transport = _attach(app=app, body_timeout=_TIMEOUT, header_timeout=5.0)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello"
        )
        await asyncio.sleep(_WAIT)
        await _drain(app)
        assert response_status(transport.writes) == 408
        # RFC 9110 15.5.9: a 408 carries the close option; the stalled
        # upload is not waited for.
        assert b"connection: close" in b"".join(transport.writes).lower()
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connections_run_one_sweep_task_until_the_last_close() -> None:
    """A registry without a Server sweeps itself; two connections share the task."""
    connections = connection_set()
    _p1, app1, t1 = _attach(connections=connections, header_timeout=5.0)
    _p2, app2, t2 = _attach(connections=connections, header_timeout=5.0)
    try:
        assert len(connections) == 2
        assert connections.sweeping
        t1.close()
        assert connections.sweeping
        t2.close()
        assert not connections.sweeping
        assert len(connections) == 0
    finally:
        for t in (t1, t2):
            if not t.is_closing():
                t.close()
        await _drain(app1)
        await _drain(app2)


@pytest.mark.asyncio
async def test_server_driven_connections_start_no_sweep_task() -> None:
    """The Server's Date tick calls sweep(); the registry must not add a task."""
    connections = Connections(server_driven=True)
    _proto, app, transport = _attach(connections=connections, header_timeout=_TIMEOUT)
    try:
        assert not connections.sweeping
        # The Server would call this once a second; one call past the deadline closes.
        loop = asyncio.get_running_loop()
        connections.sweep(loop.time())  # arms the deadline
        connections.sweep(loop.time() + _TIMEOUT + 1.0)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_pipeline_cap_rejects_ninth_queued_request() -> None:
    app = App()
    release = asyncio.Event()
    handled: list[int] = []

    async def endpoint(c, w) -> None:
        index = int(c.req.query_bytes)
        handled.append(index)
        if index == 0:
            await release.wait()
        w.text(str(index))

    app.add(Route("GET /"), endpoint)
    proto, app, transport = _attach(app=app, max_pipelined_requests=8)
    try:
        chunks = [
            b"GET /?" + str(i).encode("ascii") + b" HTTP/1.1\r\nHost: t\r\n\r\n"
            for i in range(10)
        ]
        proto.data_received(b"".join(chunks))
        await asyncio.sleep(0)
        # Nothing may be written ahead of request 0's response.
        assert transport.writes == []
        release.set()
        await _drain(app)
        # The nine queued requests are answered in order, then the tenth
        # gets 429 and the connection closes.
        assert response_statuses(transport.writes) == [200] * 9 + [429]
        assert handled == list(range(9))
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


def test_request_host_is_lowercased_without_port() -> None:
    req = Request(method="GET", path="/x", headers={"host": "Example.COM:80"})
    assert req.host == "example.com"


@pytest.mark.asyncio
async def test_protocol_413_finishes_span_without_fail() -> None:
    with TestTracer() as tracer:
        proto, app, transport = _attach(tracer=tracer)
        try:
            proto.data_received(
                b"POST /upload HTTP/1.1\r\nHost: t\r\n"
                b"Content-Length: 2147483648\r\n\r\n"
            )
            await _drain(app)
            assert response_status(transport.writes) == 413
            assert_status_span(tracer, 413, method="POST", path="/upload")
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
async def test_protocol_400_finishes_span_without_fail() -> None:
    with TestTracer() as tracer:
        proto, app, transport = _attach(tracer=tracer)
        try:
            proto.data_received(b"\x00\xff\xfe not http \r\n\r\n")
            await _drain(app)
            assert response_status(transport.writes) == 400
            assert_status_span(tracer, 400)
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"{bad json",
        b"[1,2]",
        b"[" * 100_000 + b"]" * 100_000,  # decoder depth cap -> RecursionError
    ],
    ids=["bad-json", "non-object", "deep-nesting"],
)
async def test_bad_datastar_signals_answer_400_without_span_fail(body) -> None:
    """Malformed signals are client errors: 400, and the span stays ok."""
    app = App()

    async def handler(c, w) -> None:
        await read_signals(c.req)
        w.text("ok")

    app.add(Route("POST /"), handler)
    with TestTracer() as tracer:
        proto, app, transport = _attach(app=app, tracer=tracer)
        try:
            proto.data_received(
                b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await _drain(app)
            assert response_status(transport.writes) == 400
            assert b"Invalid Datastar signals" in b"".join(transport.writes)
            assert_status_span(tracer, 400, method="POST", path="/")
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
async def test_trailing_slash_308_finishes_span_without_fail() -> None:
    with TestTracer() as tracer:
        proto, app, transport = _attach(tracer=tracer)
        try:
            proto.data_received(b"GET /search/?q=cats HTTP/1.1\r\nHost: t\r\n\r\n")
            await _drain(app)
            assert response_status(transport.writes) == 308
            assert_status_span(tracer, 308, method="GET", path="/search/")
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
async def test_head_response_omits_body_and_keeps_pipeline_in_sync() -> None:
    app = App()

    async def hello(_c, w) -> None:
        w.text("hello")

    app.add(Route("GET /"), hello)
    app.add(Route("HEAD /"), hello)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(
            b"HEAD / HTTP/1.1\r\nHost: t\r\n\r\nGET / HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        await _drain(app)
        raw = b"".join(transport.writes)
        parts = raw.split(b"\r\n\r\n")
        assert response_statuses(transport.writes) == [200, 200]
        assert b"content-length: 5" in parts[0]
        assert parts[1].startswith(b"HTTP/1.1")
        assert parts[-1] == b"hello"
        assert b"helloHTTP/1.1" not in raw
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_close_error_does_not_splice_status_into_started_response() -> None:
    app = App()
    started = asyncio.get_running_loop().create_future()

    async def hang(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.write_headers(200)
        w.write(b"partial")
        started.set_result(None)
        await asyncio.Event().wait()

    app.add(Route("GET /"), hang)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(
            b"GET / HTTP/1.1\r\nHost: t\r\n\r\nGET / HTTP/2.0\r\nHost: t\r\n\r\n"
        )
        await asyncio.wait_for(started, timeout=1)
        raw = b"".join(transport.writes)
        assert response_status(transport.writes) == 200
        assert b"HTTP/1.1 400" not in raw
        assert b"partial" in raw
        # The bad second request waits behind the in-flight stream; it does
        # not cut that response off.
        assert not transport.is_closing()
        for task in list(app.tasks):
            task.cancel()
        await _drain(app)
        raw = b"".join(transport.writes)
        assert b"HTTP/1.1 400" not in raw
        assert transport.is_closing()
    finally:
        for task in list(app.tasks):
            if not task.done():
                task.cancel()
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_unread_body_trickle_does_not_reset_idle_timeout() -> None:
    app = App()

    async def ignore_body(_c, w) -> None:
        w.text("ok")

    app.add(Route("POST /"), ignore_body)
    proto, app, transport = _attach(
        app=app, header_timeout=5.0, keep_alive_timeout=_TIMEOUT
    )
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 10\r\n"
            b"Expect: 100-continue\r\n"
            b"\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert not transport.is_closing()
        proto.data_received(b"x")
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_http11_missing_host_returns_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"GET / HTTP/1.1\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 400
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_duplicate_host_returns_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(
            b"GET / HTTP/1.1\r\nHost: a.example\r\nHost: b.example\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 400
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"GET / HTTP/0.9\r\nHost: t\r\n\r\n",
        b"GET / HTTP/2.0\r\nHost: t\r\n\r\n",
    ],
    ids=["http09", "http20"],
)
async def test_non_http11_version_on_h1_path_returns_400(payload: bytes) -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(payload)
        await _drain(app)
        assert response_status(transport.writes) == 400
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_respond_before_body_does_not_emit_100_continue() -> None:
    app = App()

    async def early(c, w) -> None:
        w.text("early")
        await c.req.body()

    app.add(Route("POST /"), early)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 5\r\n"
            b"Expect: 100-continue\r\n"
            b"\r\n"
        )
        await asyncio.sleep(0)
        assert b"100 Continue" not in b"".join(transport.writes)
        proto.data_received(b"hello")
        await _drain(app)
        assert response_statuses(transport.writes) == [200]
        assert b"early" in b"".join(transport.writes)
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_oversize_headers_with_upgrade_are_single_400() -> None:
    limit = 512
    proto, app, transport = _attach(max_header_bytes=limit)
    try:
        pad = b"x" * (limit + 32)
        proto.data_received(
            b"GET / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n"
            b"X-Pad: " + pad + b"\r\n\r\n"
        )
        await _drain(app)
        assert response_statuses(transport.writes) == [400]
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_host_with_question_mark_returns_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: evil?.example.com\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 400
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_trailing_slash_backslash_is_not_protocol_relative() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"GET /\\evil.test/ HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        raw = b"".join(transport.writes).lower()
        assert response_status(transport.writes) == 308
        # Browsers read a raw ``\`` as ``/``; the Location sends it encoded.
        assert b"location: /%5cevil.test\r\n" in raw
        assert b"location: /\\evil.test" not in raw
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_mixed_case_transfer_encoding_is_rejected_by_respond() -> None:
    app = App()

    async def bad(_c, w) -> None:
        w.headers.unsafe_set(b"Transfer-Encoding", b"chunked")
        w.text("ok")

    app.add(Route("GET /"), bad)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        raw = b"".join(transport.writes).lower()
        assert response_status(transport.writes) == 500
        assert b"transfer-encoding: chunked" not in raw
        assert b"ok" not in b"".join(transport.writes)
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_respond_rejects_crlf_in_content_type() -> None:
    app = App()

    async def bad(_c, w) -> None:
        w.respond(b"x", b"text/plain\r\nX-Injected: 1")

    app.add(Route("GET /"), bad)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        raw = b"".join(transport.writes)
        assert b"X-Injected" not in raw
        assert response_status(transport.writes) == 500
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


class _ExplodingTracer:
    def create(self, name: str):
        raise RuntimeError("tracer is broken")


@pytest.mark.asyncio
async def test_protocol_span_failure_is_logged_with_traceback(caplog) -> None:
    proto, app, transport = _attach(tracer=_ExplodingTracer())
    try:
        with caplog.at_level("ERROR", logger="stario.http"):
            proto.data_received(b"GARBAGE\r\n\r\n")
            await _drain(app)
        assert response_status(transport.writes) == 400
        records = [r for r in caplog.records if r.name == "stario.http"]
        assert records
        assert records[0].exc_info is not None
        assert "tracer is broken" in str(records[0].exc_info[1])
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_handler_task_goes_through_a_loop_task_factory() -> None:
    """A loop with a task factory sees the handler task; the default path skips it."""
    loop = asyncio.get_running_loop()
    seen: list[str] = []

    def factory(loop, coro, **kwargs):
        seen.append(coro.__name__)
        return asyncio.Task(coro, loop=loop, **kwargs)

    app = App()

    async def endpoint(_c, w) -> None:
        await asyncio.sleep(0)
        w.text("ok")

    app.add(Route("GET /"), endpoint)
    previous = loop.get_task_factory()
    loop.set_task_factory(factory)
    try:
        _proto, _app, transport = _attach(app)
        try:
            _proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
            await _drain(app)
            assert response_status(transport.writes) == 200
            assert "endpoint" in seen
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)
    finally:
        loop.set_task_factory(previous)


@pytest.mark.asyncio
async def test_sse_after_the_client_closes_is_not_a_missing_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() sets is_closing before connection_lost. That is not a 500."""
    app = App()
    started = asyncio.Event()
    release = asyncio.Event()

    async def flip(_c, w) -> None:
        started.set()
        await release.wait()
        SSE(w).execute_script("void 0")

    app.add(Route("POST /ls/x/flip-item"), flip)
    proto, _, transport = _attach(app)
    try:
        proto.data_received(
            b"POST /ls/x/flip-item HTTP/1.1\r\nHost: t\r\nContent-Length: 0\r\n\r\n"
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        # The asyncio window: the socket is closing, connection_lost has not run.
        transport._closing = True
        release.set()
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            await _drain(app)
        raw = b"".join(bytes(chunk) for chunk in transport.writes)
        assert b"Internal Server Error" not in raw
        assert "without sending a response" not in caplog.text
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_deferred_small_body_progress_outlives_header_timeout() -> None:
    """A dribbled small body dispatches at message-complete: while bytes keep
    arriving the body deadline, not the header deadline, applies."""
    app = App()
    got: list[int] = []

    async def handler(c, w) -> None:
        body = await c.req.body()
        got.append(len(body))
        w.text("ok")

    app.add(Route("POST /"), handler)
    proto, app, transport = _attach(app=app, header_timeout=_TIMEOUT, body_timeout=5.0)
    try:
        proto.data_received(b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 64\r\n\r\n")
        # Past the header deadline the connection must still accept body bytes.
        for chunk in (b"d" * 24, b"d" * 24, b"d" * 16):
            await asyncio.sleep(_WAIT)
            assert not transport.is_closing()
            proto.data_received(chunk)
        await _drain(app)
        assert got == [64]
        assert response_status(transport.writes) == 200
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_respond_snapshots_mutable_body_with_custom_headers() -> None:
    """With user headers set, respond() must not hand the caller's buffer to
    the transport by reference: a later mutation stays off the wire."""
    app = App()

    async def mutable(_c, w) -> None:
        w.headers.set("x-a", "1")
        buf = bytearray(b"A" * 64)
        w.respond(buf, b"text/plain")
        buf[:] = b"B" * 64

    app.add(Route("GET /"), mutable)
    proto = make_protocol(asyncio.get_running_loop(), app)
    transport = HoldingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        raw = b"".join(bytes(chunk) for chunk in transport.writes)
        assert raw.split(b"\r\n\r\n", 1)[1] == b"A" * 64
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_respond_snapshots_mutable_body_parts_with_custom_headers() -> None:
    """Same snapshot rule for a list/tuple body in the non-fast path."""
    app = App()

    async def mutable(_c, w) -> None:
        w.headers.set("x-a", "1")
        part = bytearray(b"A" * 64)
        w.respond([part, b"tail"], b"text/plain")
        part[:] = b"B" * 64

    app.add(Route("GET /"), mutable)
    proto = make_protocol(asyncio.get_running_loop(), app)
    transport = HoldingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        raw = b"".join(bytes(chunk) for chunk in transport.writes)
        assert raw.split(b"\r\n\r\n", 1)[1] == b"A" * 64 + b"tail"
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_retained_handle_does_not_pin_protocol() -> None:
    """A stored ``c`` keeps req/match/span but must not pin the protocol."""
    retained = []
    app = App()

    async def handler(c, w) -> None:
        retained.append(c)
        w.text("ok")

    app.add(Route("GET /"), handler)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        for _ in range(20):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
        gc.collect()
        await asyncio.sleep(0.1)
        gc.collect()
        live = [obj for obj in gc.get_objects() if isinstance(obj, HttpProtocol)]
        assert live == []
        assert len(retained) == 20
        assert all(h.disconnected for h in retained)
    finally:
        server.close()
        await server.wait_closed()
        retained.clear()
        gc.collect()


@pytest.mark.asyncio
async def test_max_connections_rejects_over_cap_with_503() -> None:
    """At the cap a cleartext socket gets a minimal 503, is never registered,
    and a slot frees up as soon as an accepted connection closes."""
    connections = Connections(sweep_interval=0.05, max_connections=2)
    loop = asyncio.get_running_loop()
    app = App()

    async def ok(_c, w) -> None:
        w.text("ok")

    app.add(Route("GET /"), ok)
    transports: list[RecordingTransport] = []
    for _ in range(3):
        proto = make_protocol(loop, app, connections=connections)
        transport = RecordingTransport(proto)
        transports.append(transport)
        proto.connection_made(transport)
    try:
        assert len(connections) == 2
        assert connections.rejected == 1
        raw = b"".join(transports[2].writes)
        assert raw.startswith(b"HTTP/1.1 503 Service Unavailable")
        assert b"connection: close" in raw.lower()
        assert b"retry-after: 1" in raw.lower()
        assert transports[2].is_closing()

        # Closing an accepted connection frees a slot for the next one.
        transports[0].close()
        proto = make_protocol(loop, app, connections=connections)
        transport = RecordingTransport(proto)
        transports.append(transport)
        proto.connection_made(transport)
        assert len(connections) == 2
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
    finally:
        for transport in transports:
            if not transport.is_closing():
                transport.close()
        await _drain(app)
