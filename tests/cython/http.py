"""Shared helpers for Cython protocol tests."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from stario.http.compression import CompressionConfig
from stario.telemetry.noop import NoOpTracer
from stario_cython.protocol import Connections, HttpProtocol

DATE = b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"

# Production sweeps with the Date header (1s). Tests use 50ms so header/idle
# cases finish in a few hundred milliseconds.
SWEEP_INTERVAL = 0.05


def connection_set(date: bytes = DATE) -> Connections:
    """A test registry: ticks every 50 ms while a connection is open; fixed Date."""
    return Connections(sweep_interval=SWEEP_INTERVAL, date=date)


class RecordingTransport(asyncio.Transport):
    """Minimal transport used by mock-protocol hardening tests."""

    __slots__ = ("_closing", "_protocol", "eof", "reading_calls", "writes")

    def __init__(self, protocol: asyncio.Protocol) -> None:
        super().__init__()
        self._protocol = protocol
        self.writes: list[bytes] = []
        self._closing = False
        self.eof = False
        self.reading_calls: list[str] = []

    def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
        assert not self._closing, "write after transport close"
        assert not self.eof, "write after write_eof"
        self.writes.append(bytes(data))

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        self.eof = True

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._protocol.connection_lost(None)

    def is_closing(self) -> bool:
        return self._closing

    def pause_reading(self) -> None:
        self.reading_calls.append("pause")

    def resume_reading(self) -> None:
        self.reading_calls.append("resume")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_protocol(
    loop: asyncio.AbstractEventLoop,
    app,
    *,
    tracer=None,
    connections: Connections | None = None,
    date: bytes = DATE,
    compression: CompressionConfig | None = None,
    max_header_bytes: int = 64 * 1024,
    max_body_bytes: int = 10 * 1024 * 1024,
    header_timeout: float = 5.0,
    keep_alive_timeout: float = 5.0,
    body_timeout: float = 30.0,
    max_pipelined_requests: int = 8,
    write_timeout: float = 30.0,
) -> HttpProtocol:
    if connections is None:
        connections = connection_set(date)
    elif date != DATE:
        connections.pin_date(date)
    return HttpProtocol(
        loop,
        app,
        NoOpTracer() if tracer is None else tracer,
        CompressionConfig() if compression is None else compression,
        connections,
        max_header_bytes=max_header_bytes,
        max_body_bytes=max_body_bytes,
        header_timeout=header_timeout,
        keep_alive_timeout=keep_alive_timeout,
        body_timeout=body_timeout,
        max_pipelined_requests=max_pipelined_requests,
        write_timeout=write_timeout,
    )


@asynccontextmanager
async def running_server(
    app,
    *,
    date: bytes = DATE,
    compression: CompressionConfig | None = None,
    max_header_bytes: int = 64 * 1024,
    max_body_bytes: int = 10 * 1024 * 1024,
    header_timeout: float = 5.0,
    keep_alive_timeout: float = 5.0,
    body_timeout: float = 30.0,
    max_pipelined_requests: int = 8,
    write_timeout: float = 30.0,
    tracer=None,
) -> AsyncIterator[int]:
    loop = asyncio.get_running_loop()
    connections = connection_set(date)
    server = await loop.create_server(
        lambda: make_protocol(
            loop,
            app,
            connections=connections,
            date=date,
            compression=compression,
            max_header_bytes=max_header_bytes,
            max_body_bytes=max_body_bytes,
            header_timeout=header_timeout,
            keep_alive_timeout=keep_alive_timeout,
            body_timeout=body_timeout,
            max_pipelined_requests=max_pipelined_requests,
            write_timeout=write_timeout,
            tracer=tracer,
        ),
        "127.0.0.1",
        free_port(),
    )
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def read_response(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(2):
        header = await reader.readuntil(b"\r\n\r\n")
        if b"content-length:" in header.lower():
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
                    body = await reader.readexactly(length) if length else b""
                    return header + body
    return header


async def read_chunk(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(2):
        size = int((await reader.readuntil(b"\r\n"))[:-2], 16)
        if size == 0:
            assert await reader.readexactly(2) == b"\r\n"
            return b""
        data = await reader.readexactly(size)
        assert await reader.readexactly(2) == b"\r\n"
        return data


def response_status(writes: list[bytes]) -> int | None:
    raw = b"".join(writes)
    if not raw.startswith(b"HTTP/"):
        return None
    line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = line.split(None, 2)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def response_statuses(writes: list[bytes]) -> list[int]:
    raw = b"".join(writes)
    statuses: list[int] = []
    for part in raw.split(b"HTTP/1.1 ")[1:]:
        statuses.append(int(part.split(b" ", 1)[0]))
    return statuses
