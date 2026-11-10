"""Second review pass: vendored llhttp, native decoders, and build flags."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from stario import App, Route
from tests.cython.http import RecordingTransport, make_protocol, response_statuses

ROOT = Path(__file__).resolve().parents[2]


def test_vendored_llhttp_is_9_4_3_or_newer() -> None:
    # 9.4.3 rejects an empty Transfer-Encoding (smuggling); 9.4.2 fixes a tab
    # after `connection: close`. Do not vendor an older release.
    header = (ROOT / "vendor/llhttp/include/llhttp.h").read_text()
    version = tuple(
        int(re.search(rf"#define LLHTTP_VERSION_{part} (\d+)", header).group(1))
        for part in ("MAJOR", "MINOR", "PATCH")
    )
    assert version >= (9, 4, 3)


@pytest.mark.parametrize(
    "head",
    [
        b"Transfer-Encoding:\r\nContent-Length: 5\r\n",
        b"Transfer-Encoding: \r\n",
        b"Content-Length: 5\r\nTransfer-Encoding:\r\n",
        b"Transfer-Encoding: chunked\r\nTransfer-Encoding:\r\n",
    ],
)
@pytest.mark.asyncio
async def test_empty_transfer_encoding_is_rejected(head: bytes) -> None:
    """llhttp 9.2.1 read an empty Transfer-Encoding as absent (or as chunked
    when another field said so), so `TE:` + `hello` became a zero-length body
    followed by a second request. A front end that rejects or frames that
    differently desynchronises from us (request smuggling)."""
    loop = asyncio.get_running_loop()
    app = App()
    ran: list[bytes] = []

    async def post(c, w) -> None:
        ran.append(await c.req.body())
        w.text("ok")

    app.add(Route("POST /"), post)
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    body = b"5\r\nhello\r\n0\r\n\r\n" if b"chunked" in head else b"hello"
    try:
        proto.data_received(b"POST / HTTP/1.1\r\nHost: t\r\n" + head + b"\r\n" + body)
        await asyncio.sleep(0)
        await app.drain_tasks()
        assert ran == []
        assert response_statuses(transport.writes) == [400]
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await app.drain_tasks()
