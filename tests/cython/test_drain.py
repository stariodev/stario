"""w.drain(): write-side backpressure for streaming handlers."""

from __future__ import annotations

import asyncio
import struct

import pytest

from stario import App, Route
from tests.cython import h2wire as h2
from tests.cython.http import (
    RecordingTransport,
    connection_set,
    make_protocol,
    running_server,
)


@pytest.mark.asyncio
async def test_drain_waits_while_the_transport_is_paused() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    events: list[str] = []
    go = asyncio.Event()

    async def stream(_c, w) -> None:
        await go.wait()
        w.write_headers(200)
        w.write(b"chunk")
        events.append("wrote")
        await w.drain()
        events.append("drained")
        w.end()

    app.add(Route("GET /"), stream)
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        proto.pause_writing()
        go.set()
        await asyncio.sleep(0.01)
        assert events == ["wrote"]
        proto.resume_writing()
        await app.drain_tasks()
        assert events == ["wrote", "drained"]
    finally:
        if not transport.is_closing():
            transport.close()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_drain_returns_when_the_client_disconnects() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    events: list[bool] = []
    go = asyncio.Event()

    async def stream(c, w) -> None:
        await go.wait()
        w.write_headers(200)
        w.write(b"chunk")
        await w.drain()
        events.append(c.disconnected)

    app.add(Route("GET /"), stream)
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
    proto.pause_writing()
    go.set()
    await asyncio.sleep(0.01)
    assert events == []
    transport.close()
    await app.drain_tasks()
    assert events == [True]


@pytest.mark.asyncio
async def test_drain_bounds_memory_for_a_slow_client() -> None:
    app = App()
    peak: list[int] = []
    chunk = b"x" * (1024 * 1024)
    count = 32

    async def big(c, w) -> None:
        w.headers.set("content-length", str(len(chunk) * count))
        w.write_headers(200)
        transport = next(iter(servers)).transport
        for _ in range(count):
            w.write(chunk)
            await w.drain()
            if transport is not None:
                peak.append(transport.get_write_buffer_size())
        w.end()

    app.add(Route("GET /big"), big)
    servers = connection_set()
    loop = asyncio.get_running_loop()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=servers), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=2**16)
        writer.write(b"GET /big HTTP/1.1\r\nHost: t\r\n\r\n")
        await asyncio.sleep(0.3)  # client not reading: the server must wait
        assert len(peak) < count
        received = 0
        async with asyncio.timeout(10):
            await reader.readuntil(b"\r\n\r\n")
            while received < len(chunk) * count:
                received += len(await reader.read(1 << 20))
        writer.close()
        await app.drain_tasks()
    finally:
        server.close()
        await server.wait_closed()
    assert received == len(chunk) * count
    assert max(peak) <= 4 * len(chunk)


@pytest.mark.asyncio
async def test_h2_drain_waits_for_the_stream_window() -> None:
    app = App()
    drained = asyncio.Event()
    payload = b"y" * (1024 * 1024)

    async def big(_c, w) -> None:
        w.write_headers(200)
        w.write(payload)
        await w.drain()
        drained.set()
        w.end()

    app.add(Route("GET /big"), big)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(
            h2.pack_frame(
                h2.TYPE_HEADERS,
                h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
                1,
                h2.encode_request(path="/big"),
            )
        )
        await asyncio.sleep(0.1)
        # Only the default 64 KiB window was granted: most of 1 MiB is queued.
        assert not drained.is_set()
        grant = struct.pack(">I", 2 * len(payload))
        writer.write(
            h2.pack_frame(h2.TYPE_WINDOW_UPDATE, 0, 0, grant)
            + h2.pack_frame(h2.TYPE_WINDOW_UPDATE, 0, 1, grant)
        )
        # Read while waiting: drain needs the bytes to leave the transport,
        # and how much the kernel holds for a client that is not reading
        # depends on the platform and the write pattern (Windows accepts one
        # whole send while its buffer has room, then blocks the next).
        reading = asyncio.create_task(h2.read_stream(reader, buf, 1, timeout=5))
        try:
            async with asyncio.timeout(2):
                await drained.wait()
            frames, buf = await reading
        finally:
            reading.cancel()
        writer.close()
    assert h2.stream_data(frames, 1) == payload


@pytest.mark.asyncio
async def test_drain_after_the_handler_finished_is_a_no_op() -> None:
    app = App()
    kept = []

    async def quick(_c, w) -> None:
        kept.append(w)
        w.text("ok")

    app.add(Route("GET /"), quick)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await reader.readuntil(b"ok")
        writer.close()
    await kept[0].drain()
