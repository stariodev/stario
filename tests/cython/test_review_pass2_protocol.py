"""Regressions from the second review pass of ``protocol.pyx``."""

from __future__ import annotations

import asyncio

import pytest

from stario import App, Route
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
