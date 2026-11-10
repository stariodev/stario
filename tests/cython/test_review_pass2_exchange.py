"""Regressions from the second review pass over ``exchange.pyx`` and its includes."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from stario import App, Route
from stario_cython.exchange import ParsedCookies, Request, RequestHeaders
from tests.cython.http import RecordingTransport, make_protocol, response_status

ROOT = Path(__file__).resolve().parents[2]


def _attach(app: App):
    proto = make_protocol(asyncio.get_running_loop(), app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    return proto, transport


async def _settle(app: App) -> None:
    for _ in range(10):
        await asyncio.sleep(0)
    for task in list(app.tasks):
        if not task.done():
            task.cancel()
    await app.drain_tasks()


# --- Content-Length formatting ----------------------------------------------

_HUGE_CL_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys
    from stario import App, Route
    from tests.cython.http import RecordingTransport, make_protocol

    async def main():
        app = App()

        async def handler(_c, w):
            w.headers.set("content-length", sys.argv[1])
            w.write_headers(200)
            w.abort()

        app.add(Route("GET /"), handler)
        proto = make_protocol(asyncio.get_running_loop(), app)
        transport = RecordingTransport(proto)
        proto.connection_made(transport)
        proto.data_received(b"GET / HTTP/1.1\\r\\nHost: t\\r\\n\\r\\n")
        for _ in range(5):
            await asyncio.sleep(0)
        sys.stdout.write(b"".join(transport.writes).split(b"\\r\\n\\r\\n")[0].decode())

    asyncio.run(main())
    """
)


@pytest.mark.parametrize("value", ["1000000000000000", "9223372036854775807"])
def test_huge_declared_content_length_is_formatted_without_overflow(value: str) -> None:
    """``write_headers()`` re-formats a declared Content-Length into a 16-byte
    stack buffer: 16+ digits overflowed it (``_FORTIFY_SOURCE`` aborted the
    whole process). Run in a child so a regression cannot kill the suite."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, "-c", _HUGE_CL_SCRIPT, value],
        capture_output=True,
        env=env,
        cwd=ROOT,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-500:]
    assert f"content-length: {value}".encode() in proc.stdout


# --- ParsedCookies(lines) ---------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [bytearray(b"a=1; b=2"), memoryview(b"a=1; b=2")],
    ids=["bytearray", "memoryview"],
)
def test_parsed_cookies_get_reads_bytes_like_lines_as_bytes(line) -> None:
    """``get()`` cast every stored line to ``bytes`` unchecked: a bytearray
    line was read through ``PyBytes_AS_STRING`` (bytes from inside the
    bytearray object header, ``len(line)`` of them, past its end)."""
    cookies = ParsedCookies([line])
    assert cookies.get("a") == "1"
    assert cookies.get("b") == "2"
    assert cookies.as_dict() == {"a": "1", "b": "2"}


def test_parsed_cookies_rejects_lines_that_are_not_text_or_bytes() -> None:
    with pytest.raises(TypeError):
        ParsedCookies([12345])


# --- RequestHeaders re-initialisation ---------------------------------------


def test_request_headers_reinit_resets_hot_indexes() -> None:
    """``__init__`` kept the old Host/Cookie/Authorization indexes when it
    ran again, so they pointed past the new, shorter table
    (``Request.host`` read that row without a bounds check)."""
    first = {f"x-{i}": "v" for i in range(40)}
    first["host"] = "old.example"
    first["cookie"] = "a=1"
    headers = RequestHeaders(first)
    headers.__init__({"host": "new.example", "cookie": "b=2"})
    assert headers.get("host") == "new.example"
    assert headers.get("cookie") == "b=2"
    assert Request(headers=headers).host == "new.example"
    assert Request(headers=headers).cookies.get("b") == "2"
    headers.__init__({"x": "y"})
    assert headers.get("host") is None
    assert Request(headers=headers).host == ""
    headers.__init__()
    assert len(headers) == 0


@pytest.mark.asyncio
async def test_live_request_headers_cannot_be_reinitialised() -> None:
    app = App()
    seen: list[str] = []

    async def handler(c, w) -> None:
        with pytest.raises(TypeError):
            c.req.headers.__init__({"host": "evil"})
        seen.append(c.req.host)
        w.text("ok")

    app.add(Route("GET /"), handler)
    proto, transport = _attach(app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _settle(app)
        assert response_status(transport.writes) == 200
        assert seen == ["t"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _settle(app)


# --- HTTP/1.0 close-delimited stream ----------------------------------------


@pytest.mark.asyncio
async def test_http10_close_delimited_stream_closes_even_without_connection_header() -> None:
    """An HTTP/1.0 streamed body ends only when the connection closes. The
    close was keyed on ``connection: close`` in ``w.headers`` at completion;
    a handler that removed it kept the connection open behind a body with
    no end."""
    app = App()

    async def handler(_c, w) -> None:
        w.write_headers(200)
        w.write(b"part")
        w.headers.remove("connection")
        w.end()

    app.add(Route("GET /"), handler)
    proto, transport = _attach(app)
    try:
        proto.data_received(b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        await _settle(app)
        assert response_status(transport.writes) == 200
        assert b"".join(transport.writes).endswith(b"part")
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _settle(app)
