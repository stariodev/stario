"""Regressions from the November 2026 security review of the HTTP core."""

from __future__ import annotations

import asyncio
import gzip

import pytest

from stario import App, Route
from stario.exceptions import StarioError
from stario.http.compression import CompressionConfig
from tests.cython import h2wire as h2
from tests.cython.http import (
    RecordingTransport,
    make_protocol,
    response_status,
    response_statuses,
)

ENHANCE_YOUR_CALM = 0xB


async def _ok(_c, w) -> None:
    w.text("ok")


def _attach(app: App, **kwargs):
    proto = make_protocol(asyncio.get_running_loop(), app, **kwargs)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    return proto, transport


def _attach_h2(app: App, **kwargs):
    proto, transport = _attach(app, **kwargs)
    proto.data_received(h2.H2_PREFACE + h2.pack_frame(h2.TYPE_SETTINGS, 0, 0))
    proto.data_received(h2.pack_frame(h2.TYPE_SETTINGS, h2.FLAG_ACK, 0))
    return proto, transport


def _frames(transport: RecordingTransport, start: int = 0) -> list[h2.H2Frame]:
    return h2.parse_frames(b"".join(transport.writes[start:]))[0]


async def _finish(app: App, transport: RecordingTransport) -> None:
    for task in list(app.tasks):
        if not task.done():
            task.cancel()
    if not transport.is_closing():
        transport.close()
    await app.drain_tasks()


# --- HTTP/2 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_h2_server_side_resets_of_running_handlers_are_rate_limited() -> None:
    """MadeYouReset (CVE-2025-8671): the client never sends RST_STREAM. A
    DATA frame that contradicts content-length makes nghttp2 reset a stream
    whose handler already started; those resets draw from the same budget as
    client resets, and the connection ends when it is spent."""
    app = App()
    started = 0
    gate = asyncio.Event()

    async def slow(_c, w) -> None:
        nonlocal started
        started += 1
        await gate.wait()
        w.text("ok")

    app.add(Route("POST /"), slow)
    proto, transport = _attach_h2(app)
    first = len(transport.writes)
    try:
        streams = 600
        for batch in range(0, streams, 50):
            if transport.is_closing():
                break
            wire = b"".join(
                h2.pack_frame(
                    h2.TYPE_HEADERS,
                    h2.FLAG_END_HEADERS,
                    sid,
                    h2.encode_request(
                        method="POST", extra=[(b"content-length", b"300000")]
                    ),
                )
                + h2.pack_frame(h2.TYPE_DATA, h2.FLAG_END_STREAM, sid, b"x")
                for sid in range(2 * batch + 1, 2 * (batch + 50) + 1, 2)
            )
            proto.data_received(wire)
            await asyncio.sleep(0)
        frames = _frames(transport, first)
        goaways = [f for f in frames if f.type == h2.TYPE_GOAWAY]
        # Stario's budget answers ENHANCE_YOUR_CALM; a distro nghttp2 with
        # its own backported limit may get there first with another code.
        # Either way the connection ends and the handler count is bounded.
        assert goaways, "the connection was never limited"
        assert transport.is_closing()
        # The burst (100) plus the batch in flight when it ran out.
        assert started <= 150
    finally:
        gate.set()
        await _finish(app, transport)


@pytest.mark.asyncio
async def test_h2_a_few_cancelled_handlers_keep_the_connection() -> None:
    app = App()
    gate = asyncio.Event()

    async def slow(_c, w) -> None:
        await gate.wait()
        w.text("ok")

    app.add(Route("GET /slow"), slow)
    app.add(Route("GET /"), _ok)
    proto, transport = _attach_h2(app)
    first = len(transport.writes)
    try:
        for sid in range(1, 41, 2):
            proto.data_received(
                h2.pack_frame(
                    h2.TYPE_HEADERS,
                    h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
                    sid,
                    h2.encode_request(path="/slow"),
                )
                + h2.pack_frame(
                    h2.TYPE_RST_STREAM, 0, sid, h2.NGHTTP2_CANCEL.to_bytes(4, "big")
                )
            )
            await asyncio.sleep(0)
        proto.data_received(
            h2.pack_frame(
                h2.TYPE_HEADERS,
                h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
                101,
                h2.encode_request(),
            )
        )
        await asyncio.sleep(0)
        frames = _frames(transport, first)
        assert not h2.has_goaway(frames)
        assert h2.stream_data(frames, 101) == b"ok"
    finally:
        gate.set()
        await _finish(app, transport)


def _header_block(stream_id: int, block: bytes) -> bytes:
    chunks = [block[i : i + 16384] for i in range(0, len(block), 16384)]
    last = len(chunks) - 1
    out = h2.pack_frame(
        h2.TYPE_HEADERS,
        h2.FLAG_END_STREAM | (h2.FLAG_END_HEADERS if last == 0 else 0),
        stream_id,
        chunks[0],
    )
    for index, chunk in enumerate(chunks[1:], 1):
        out += h2.pack_frame(
            h2.TYPE_CONTINUATION,
            h2.FLAG_END_HEADERS if index == last else 0,
            stream_id,
            chunk,
        )
    return out


@pytest.mark.asyncio
async def test_h2_hpack_reference_flood_stops_at_twice_the_header_limit() -> None:
    """One byte of HPACK can name a 4 KiB table entry, and nghttp2 validates
    every field of a block the server already gave up on. The block is cut
    off (RST_STREAM) once it has decoded to twice the header limit, so the
    callback count stays small; the connection survives."""
    app = App()
    app.add(Route("GET /"), _ok)
    proto, transport = _attach_h2(app, max_header_bytes=16 * 1024)
    first = len(transport.writes)
    try:
        big = b"\x40" + h2.hpack_string(b"x-big") + h2.hpack_string(b"v" * 4050)
        block = h2.encode_request() + big + h2.indexed(62) * 60_000
        proto.data_received(_header_block(1, block))
        frames = _frames(transport, first)
        assert h2.rst_code(frames, 1) == ENHANCE_YOUR_CALM
        assert not transport.is_closing()
        # A block just over the limit still gets its 431.
        mark = len(transport.writes)
        proto.data_received(
            _header_block(
                3,
                h2.encode_request(extra=[(b"x-a", b"a" * 9000), (b"x-b", b"b" * 9000)]),
            )
        )
        await asyncio.sleep(0)
        blob = h2.stream_headers_blob(_frames(transport, mark), 3)
        assert b"431" in blob
    finally:
        await _finish(app, transport)


async def _h2_response_headers(handler) -> bytes:
    app = App()
    app.add(Route("GET /"), handler)
    proto, transport = _attach_h2(app)
    first = len(transport.writes)
    try:
        proto.data_received(
            h2.pack_frame(
                h2.TYPE_HEADERS,
                h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
                1,
                h2.encode_request(),
            )
        )
        await asyncio.sleep(0)
        return h2.stream_headers_blob(_frames(transport, first), 1)
    finally:
        await _finish(app, transport)


@pytest.mark.asyncio
async def test_h2_proxy_connection_response_header_is_dropped() -> None:
    """RFC 9113 8.2.2: connection-specific fields make a response malformed."""

    async def with_proxy(_c, w) -> None:
        w.headers.set("proxy-connection", "keep-alive")
        w.headers.set("x-kept", "1")
        w.text("ok")

    async def without(_c, w) -> None:
        w.headers.set("x-kept", "1")
        w.text("ok")

    async def bare(_c, w) -> None:
        w.text("ok")

    kept = await _h2_response_headers(without)
    assert await _h2_response_headers(with_proxy) == kept
    assert await _h2_response_headers(bare) != kept


# --- HTTP/1 -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_final_response_before_100_continue_closes(streamed: bool) -> None:
    """A front end may skip the body after a final response and reuse the
    connection; reading on would take the next request as this body."""
    app = App()
    seen: list[str] = []

    async def deny(c, w) -> None:
        seen.append(c.req.path)
        if streamed:
            w.write_headers(401).end(b"denied")
        else:
            w.respond(b"denied", b"text/plain", 401)

    async def page(c, w) -> None:
        seen.append(c.req.path)
        w.text("page")

    app.add(Route("POST /upload"), deny)
    app.add(Route("GET /victim"), page)
    app.add(Route("GET /admin"), page)
    proto, transport = _attach(app)
    try:
        proto.data_received(
            b"POST /upload HTTP/1.1\r\nHost: a\r\nExpect: 100-continue\r\n"
            b"Content-Length: 25\r\n\r\n"
        )
        await asyncio.sleep(0)
        wire = b"".join(transport.writes)
        assert response_status(transport.writes) == 401
        assert b"100 Continue" not in wire
        assert b"connection: close" in wire.lower()
        if not transport.is_closing():
            proto.data_received(
                b"GET /victim HTTP/1.1\r\nX: GET /admin HTTP/1.1\r\nHost: a\r\n\r\n"
            )
        await asyncio.sleep(0)
        assert seen == ["/upload"]
        assert response_statuses(transport.writes) == [401]
    finally:
        await _finish(app, transport)


@pytest.mark.asyncio
async def test_read_body_after_100_continue_keeps_the_connection() -> None:
    app = App()

    async def echo(c, w) -> None:
        w.respond(await c.req.body(), b"text/plain", 200)

    app.add(Route("POST /"), echo)
    proto, transport = _attach(app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: a\r\nExpect: 100-continue\r\n"
            b"Content-Length: 2\r\n\r\n"
        )
        await asyncio.sleep(0)
        assert b"100 Continue" in b"".join(transport.writes)
        proto.data_received(b"hi")
        await app.drain_tasks()
        wire = b"".join(transport.writes)
        assert wire.endswith(b"hi")
        assert b"connection: close" not in wire.lower()
        assert not transport.is_closing()
    finally:
        await _finish(app, transport)


@pytest.mark.asyncio
async def test_http_1_0_expectation_is_ignored() -> None:
    app = App()

    async def echo(c, w) -> None:
        w.respond(await c.req.body(), b"text/plain", 200)

    app.add(Route("POST /"), echo)
    proto, transport = _attach(app)
    try:
        proto.data_received(
            b"POST / HTTP/1.0\r\nExpect: 100-continue\r\nContent-Length: 2\r\n\r\n"
        )
        await asyncio.sleep(0)
        assert b"100 Continue" not in b"".join(transport.writes)
        proto.data_received(b"hi")
        await app.drain_tasks()
        assert response_statuses(transport.writes) == [200]
    finally:
        await _finish(app, transport)


# --- response writer --------------------------------------------------------


class _LongBytes(bytes):
    def __len__(self) -> int:
        return 300

    def __bytes__(self) -> bytes:
        return b"0\r\n\r\nHTTP/1.1 200 OK\r\n\r\nEVIL"


async def _one_response(handler, request: bytes, **kwargs) -> bytes:
    app = App()
    app.add(Route("GET /"), handler)
    proto, transport = _attach(app, **kwargs)
    try:
        proto.data_received(request)
        await app.drain_tasks()
        return b"".join(transport.writes)
    finally:
        await _finish(app, transport)


_GET = b"GET / HTTP/1.1\r\nHost: a\r\n\r\n"


@pytest.mark.asyncio
async def test_bytes_subclass_length_comes_from_its_storage() -> None:
    """`__len__` / `__bytes__` on a subclass must not size a C read or
    Content-Length: the writer would send heap bytes past the object."""

    async def respond(_c, w) -> None:
        w.headers.set("x-a", "1")
        w.respond(_LongBytes(b"hello"), b"text/plain", 200)

    async def stream(_c, w) -> None:
        w.write_headers(200).write(_LongBytes(b"abc")).end()

    async def stream_gzip(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.write_headers(200).write(_LongBytes(b"secret-free")).end()

    wire = await _one_response(respond, _GET)
    assert wire.endswith(b"content-length: 5\r\n\r\nhello")
    wire = await _one_response(stream, _GET)
    assert wire.endswith(b"\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
    assert b"EVIL" not in wire
    wire = await _one_response(
        stream_gzip,
        b"GET / HTTP/1.1\r\nHost: a\r\nAccept-Encoding: gzip\r\n\r\n",
        compression=CompressionConfig(min_size=0),
    )
    head, _, body = wire.partition(b"\r\n\r\n")
    if b"content-encoding: gzip" in head:
        raw = bytearray()
        while body:
            size, _, body = body.partition(b"\r\n")
            n = int(size, 16)
            raw += body[:n]
            body = body[n + 2 :]
        assert gzip.decompress(bytes(raw)) == b"secret-free"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["5_0", "+50", " 50", "0x32", "-1", ""])
async def test_write_headers_rejects_non_digit_content_length(value: str) -> None:
    raised: list[BaseException] = []

    async def handler(_c, w) -> None:
        w.headers.unsafe_set(b"content-length", value.encode())
        try:
            w.write_headers(200)
        except StarioError as exc:
            raised.append(exc)
        w.respond(b"", b"text/plain", 500)

    await _one_response(handler, _GET)
    assert raised


@pytest.mark.asyncio
async def test_write_headers_sends_one_canonical_content_length() -> None:
    async def handler(_c, w) -> None:
        w.headers.add("content-length", "005")
        w.headers.add("content-length", "5")
        w.write_headers(200).end(b"hello")

    wire = await _one_response(handler, _GET)
    assert wire.lower().count(b"content-length") == 1
    assert b"content-length: 5\r\n" in wire


@pytest.mark.asyncio
async def test_write_headers_rejects_conflicting_content_lengths() -> None:
    raised: list[BaseException] = []

    async def handler(_c, w) -> None:
        w.headers.add("content-length", "5")
        w.headers.add("content-length", "100")
        try:
            w.write_headers(200)
        except StarioError as exc:
            raised.append(exc)
        w.respond(b"", b"text/plain", 500)

    await _one_response(handler, _GET)
    assert raised
    assert "Conflicting" in str(raised[0])


@pytest.mark.asyncio
async def test_h2_cut_off_header_blocks_spend_the_reset_budget() -> None:
    """A flood spread over streams: each block is cut off at the limit, but
    the client never sends RST_STREAM and no handler starts. Those blocks
    draw from the same budget, so the connection ends."""
    app = App()
    app.add(Route("GET /"), _ok)
    proto, transport = _attach_h2(app, max_header_bytes=4 * 1024)
    first = len(transport.writes)
    try:
        big = b"\x40" + h2.hpack_string(b"x-big") + h2.hpack_string(b"v" * 4000)
        proto.data_received(_header_block(1, h2.encode_request() + big))
        block = h2.encode_request() + h2.indexed(62) * 8
        for sid in range(3, 603, 2):
            if transport.is_closing():
                break
            proto.data_received(_header_block(sid, block))
        frames = _frames(transport, first)
        goaways = [f for f in frames if f.type == h2.TYPE_GOAWAY]
        assert goaways, "the connection was never limited"
        assert int.from_bytes(goaways[-1].payload[4:8], "big") == ENHANCE_YOUR_CALM
        resets = [f for f in frames if f.type == h2.TYPE_RST_STREAM]
        assert len(resets) <= 110
    finally:
        await _finish(app, transport)
