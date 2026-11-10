"""Regressions from the second review pass of ``protocol.pyx``."""

from __future__ import annotations

import asyncio
import gc
import tracemalloc

import pytest

from stario import App, Route
from tests.cython import h2wire as h2
from tests.cython.http import (
    RecordingTransport,
    make_protocol,
    response_statuses,
)


async def _ok(_c, w) -> None:
    w.text("ok")


def _attach(app: App, **kwargs):
    proto = make_protocol(asyncio.get_running_loop(), app, **kwargs)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    return proto, transport


async def _finish(app: App, transport: RecordingTransport) -> None:
    for task in list(app.tasks):
        if not task.done():
            task.cancel()
    if not transport.is_closing():
        transport.close()
    await app.drain_tasks()


# --- HTTP/1: one request, one response --------------------------------------


@pytest.mark.asyncio
async def test_malformed_body_after_complete_response_adds_no_second_status() -> None:
    """A handler answers before reading a chunked body, the rest of which is
    malformed. The connection closes; it must not also write a 400 after the
    200, which a client (or a front end that reuses the connection) would
    read as the response to its next request."""
    app = App()
    app.add(Route("POST /"), _ok)
    proto, transport = _attach(app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n"
        )
        await asyncio.sleep(0)
        assert response_statuses(transport.writes) == [200]
        assert not transport.is_closing()

        proto.data_received(b"zz\r\n")
        await asyncio.sleep(0)
        assert response_statuses(transport.writes) == [200]
        assert transport.is_closing()
    finally:
        await _finish(app, transport)


@pytest.mark.asyncio
async def test_malformed_body_while_response_streams_adds_no_second_status() -> None:
    app = App()
    gate = asyncio.Event()

    async def streaming(_c, w) -> None:
        w.write(b"first")
        await gate.wait()
        w.end()

    app.add(Route("POST /"), streaming)
    proto, transport = _attach(app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n"
        )
        await asyncio.sleep(0)
        assert response_statuses(transport.writes) == [200]
        proto.data_received(b"zz\r\n")
        await asyncio.sleep(0)
        assert response_statuses(transport.writes) == [200]
        assert transport.is_closing()
    finally:
        gate.set()
        await _finish(app, transport)


@pytest.mark.asyncio
async def test_malformed_body_before_any_response_still_gets_400() -> None:
    app = App()
    gate = asyncio.Event()

    async def waits(_c, w) -> None:
        await gate.wait()
        w.text("late")

    app.add(Route("POST /"), waits)
    proto, transport = _attach(app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\nzz\r\n"
        )
        await asyncio.sleep(0)
        assert response_statuses(transport.writes) == [400]
        assert transport.is_closing()
    finally:
        gate.set()
        await _finish(app, transport)


# --- HTTP/2: per-connection memory -------------------------------------------


def _attach_h2(app: App, **kwargs):
    proto, transport = _attach(app, **kwargs)
    proto.data_received(h2.H2_PREFACE + h2.pack_frame(h2.TYPE_SETTINGS, 0, 0))
    proto.data_received(h2.pack_frame(h2.TYPE_SETTINGS, h2.FLAG_ACK, 0))
    return proto, transport


def _h2_gets(first: int, n: int) -> bytes:
    return b"".join(
        h2.pack_frame(
            h2.TYPE_HEADERS,
            h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
            sid,
            h2.encode_request(),
        )
        for sid in range(first, first + 2 * n, 2)
    )


@pytest.mark.asyncio
async def test_h2_outbound_scratch_is_not_kept_after_a_burst() -> None:
    """One read that answers many streams with small responses grows the
    outbound scratch to the size of the whole pass (~0.9 MiB for 200
    streams). An idle keep-alive connection must not keep holding it."""
    app = App()
    body = "x" * 4000

    async def page(_c, w) -> None:
        w.text(body)

    app.add(Route("GET /"), page)
    window = h2.pack_frame(h2.TYPE_WINDOW_UPDATE, 0, 0, (1 << 30).to_bytes(4, "big"))
    # Warm this thread's exchange pool on another connection, so only the
    # measured connection's own buffers count.
    warm, warm_transport = _attach_h2(app)
    warm.data_received(window + _h2_gets(1, 200))
    await asyncio.sleep(0)
    await _finish(app, warm_transport)

    proto, transport = _attach_h2(app)
    try:
        proto.data_received(window)
        transport.writes.clear()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            proto.data_received(_h2_gets(1, 200))
            await asyncio.sleep(0)
            assert sum(len(w) for w in transport.writes) > 200 * 4000
            transport.writes.clear()
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0] - before
        finally:
            tracemalloc.stop()
        assert not transport.is_closing()
        assert retained < 256 * 1024, retained
    finally:
        await _finish(app, transport)
