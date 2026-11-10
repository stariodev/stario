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


def _h2_post(sid: int, length: int, path: str = "/") -> bytes:
    return h2.pack_frame(
        h2.TYPE_HEADERS,
        h2.FLAG_END_HEADERS,
        sid,
        h2.encode_request(
            method="POST",
            path=path,
            extra=[(b"content-length", str(length).encode())],
        ),
    )


def _h2_data(sid: int, payload: bytes, end: bool = False) -> bytes:
    out = b""
    for i in range(0, len(payload), 16384):
        last = end and i + 16384 >= len(payload)
        out += h2.pack_frame(
            h2.TYPE_DATA, h2.FLAG_END_STREAM if last else 0, sid, payload[i : i + 16384]
        )
    return out


@pytest.mark.asyncio
async def test_h2_small_bodies_waiting_for_dispatch_are_bounded_per_connection() -> (
    None
):
    """Bodies with a Content-Length up to 256 KiB are read before their
    handler starts, with the windows open. 200 such streams that stop just
    short of their length held 50 MiB on one connection, on any path (no
    route needed). Past 1 MiB of them, a stream dispatches instead: here the
    404 handler answers and its body is dropped."""
    app = App()
    app.add(Route("GET /"), _ok)
    proto, transport = _attach_h2(app)
    try:
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            chunk = b"x" * (240 * 1024)
            for i in range(200):
                sid = 2 * i + 1
                proto.data_received(_h2_post(sid, 256 * 1024))
                proto.data_received(_h2_data(sid, chunk))
                await asyncio.sleep(0)
            gc.collect()
            held = tracemalloc.get_traced_memory()[0] - before
        finally:
            tracemalloc.stop()
        assert not transport.is_closing()
        assert held < 8 * 1024 * 1024, held
    finally:
        await _finish(app, transport)


@pytest.mark.asyncio
async def test_h2_interleaved_small_uploads_past_the_budget_read_whole() -> None:
    app = App()
    sizes: dict[int, int] = {}
    early = 0

    async def upload(c, w) -> None:
        nonlocal early
        body = await c.req.body()
        sid = int(c.req.query.get("sid"))
        sizes[sid] = len(body)
        assert body == bytes([sid % 251]) * len(body)
        w.text("ok")

    async def probe(c, w) -> None:
        nonlocal early
        early += 1
        await c.req.body()
        w.text("ok")

    app.add(Route("POST /up"), upload)
    app.add(Route("POST /probe"), probe)
    proto, transport = _attach_h2(app)
    try:
        streams = list(range(1, 33, 2))
        length = 200 * 1024
        for sid in streams:
            proto.data_received(_h2_post(sid, length, f"/up?sid={sid}"))
        # Interleave 16 KiB frames across all streams: 3.2 MB in flight.
        for off in range(0, length, 16384):
            part = b"".join(
                _h2_data(
                    sid,
                    bytes([sid % 251]) * min(16384, length - off),
                    end=off + 16384 >= length,
                )
                for sid in streams
            )
            proto.data_received(part)
            await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)
        assert sizes == {sid: length for sid in streams}
        assert not transport.is_closing()

        # Every finished stream gave its bytes back: a new small body is
        # read before its handler starts again.
        proto.data_received(_h2_post(101, 1000, "/probe") + _h2_data(101, b"y" * 500))
        await asyncio.sleep(0)
        assert early == 0
        proto.data_received(_h2_data(101, b"y" * 500, end=True))
        await asyncio.sleep(0)
        assert early == 1
    finally:
        await _finish(app, transport)


@pytest.mark.asyncio
async def test_data_received_accepts_a_memoryview() -> None:
    """Input that is neither bytes nor bytearray was dropped without a word
    (the parse step raised inside a noexcept function)."""
    app = App()
    app.add(Route("GET /"), _ok)
    proto, transport = _attach(app)
    try:
        proto.data_received(memoryview(b"GET / HTTP/1.1\r\nHost: a\r\n\r\n"))
        await asyncio.sleep(0)
        assert response_statuses(transport.writes) == [200]
    finally:
        await _finish(app, transport)
