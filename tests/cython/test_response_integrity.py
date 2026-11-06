"""Response framing: HEAD, Content-Length, trailers, and caller-owned buffers."""

from __future__ import annotations

import array
import asyncio
import gzip
import zlib

import pytest

from stario import App, Route
from stario.http.compression import CompressionConfig
from stario_cython.codecs import brotli_decompress, zstd_decompress
from tests.cython import h2wire as h2
from tests.cython.http import read_response, running_server


def _h2_get(stream_id: int, path: str, *, method: str = "GET", extra=None) -> bytes:
    return h2.pack_frame(
        h2.TYPE_HEADERS,
        h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
        stream_id,
        h2.encode_request(method=method, path=path, extra=extra),
    )


def _header_map(head: bytes) -> dict[bytes, bytes]:
    lines = head.split(b"\r\n")[1:]
    return {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(b":") for line in lines if line)
        if name.strip().lower() != b"date"
    }


async def _page_respond_text(_c, w) -> None:
    w.text("x" * 4096)


async def _page_respond_image(_c, w) -> None:
    w.respond(b"x" * 4096, b"image/png")


async def _page_stream_text(_c, w) -> None:
    w.headers.set("content-type", "text/plain")
    w.write_headers(200)
    w.write(b"x" * 4096)
    w.end()


async def _page_stream_image(_c, w) -> None:
    w.headers.set("content-type", "image/png")
    w.write_headers(200)
    w.write(b"x" * 4096)
    w.end()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [_page_respond_text, _page_respond_image, _page_stream_text, _page_stream_image],
)
async def test_head_headers_match_get(page) -> None:
    # RFC 9110 9.3.2: same fields as GET; Content-Length may be omitted
    # but must not differ, and no body follows.
    app = App()
    app.add(Route("GET /"), page)
    app.add(Route("HEAD /"), page)
    request = b" / HTTP/1.1\r\nHost: t\r\nAccept-Encoding: gzip\r\n\r\n"
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET" + request)
        get = _header_map(await reader.readuntil(b"\r\n\r\n"))
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"HEAD" + request + b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        async with asyncio.timeout(2):
            head = _header_map(await reader.readuntil(b"\r\n\r\n"))
            follow = await reader.readuntil(b"\r\n\r\n")
        writer.close()
    assert follow.startswith(b"HTTP/1.1 200")
    if b"content-length" in head:
        assert head[b"content-length"] == get[b"content-length"]
    framing = {b"content-length", b"transfer-encoding"}
    assert {k: v for k, v in head.items() if k not in framing} == {
        k: v for k, v in get.items() if k not in framing
    }


@pytest.mark.asyncio
async def test_h2_head_with_accept_encoding_sends_no_data() -> None:
    app = App()

    async def page(_c, w) -> None:
        w.text("x" * 4096)

    app.add(Route("GET /"), page)
    app.add(Route("HEAD /"), page)
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(
            _h2_get(1, "/", method="HEAD", extra=[(b"accept-encoding", b"gzip")])
        )
        frames, _buf = await h2.read_stream(reader, buf, 1)
        writer.close()
    assert b"\x88" in h2.stream_headers_blob(frames, 1)
    assert h2.stream_data(frames, 1) == b""
    assert not h2.has_rst(frames, 1)


@pytest.mark.asyncio
async def test_h2_respond_gzip_round_trip() -> None:
    app = App()
    body = b"h2 compressed body " * 1024

    async def page(_c, w) -> None:
        w.respond(body, b"text/plain")

    app.add(Route("GET /"), page)
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(_h2_get(1, "/", extra=[(b"accept-encoding", b"gzip")]))
        frames, _buf = await h2.read_stream(reader, buf, 1)
        writer.close()
    assert gzip.decompress(h2.stream_data(frames, 1)) == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "decompress"),
    [
        (b"gzip", gzip.decompress),
        (b"br", brotli_decompress),
        (b"zstd", zstd_decompress),
    ],
)
async def test_h2_streaming_compression_round_trip(encoding, decompress) -> None:
    app = App()

    async def stream(_c, w) -> None:
        w.headers.set("content-type", "text/event-stream")
        w.write_headers(200)
        w.write(b"data: 0\n\n")
        w.write(bytearray(b"data: 1\n\n"))
        w.end()

    app.add(Route("GET /stream"), stream)
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(_h2_get(1, "/stream", extra=[(b"accept-encoding", encoding)]))
        frames, _buf = await h2.read_stream(reader, buf, 1)
        writer.close()
    assert decompress(h2.stream_data(frames, 1)) == b"data: 0\n\ndata: 1\n\n"


@pytest.mark.asyncio
async def test_h1_streaming_gzip_large_body_round_trip() -> None:
    app = App()
    parts = [bytes(range(256)) * 256] * 4

    async def page(_c, w) -> None:
        w.headers.set("content-type", "application/octet-stream")
        w.write_headers(200)
        for part in parts:
            w.write(part)
        w.end()

    app.add(Route("GET /"), page)
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        raw = await _raw_exchange(
            port,
            b"GET / HTTP/1.1\r\nHost: t\r\nAccept-Encoding: gzip\r\n"
            b"Connection: close\r\n\r\n",
        )
    head, body = raw.split(b"\r\n\r\n", 1)
    assert b"content-encoding: gzip" in head.lower()
    assert b"transfer-encoding: chunked" in head.lower()
    assert gzip.decompress(_dechunk(body)) == b"".join(parts)


@pytest.mark.asyncio
async def test_h2_handler_keeps_running_after_end() -> None:
    app = App()
    after_end = asyncio.Event()

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.write(b"done")
        w.end()
        await asyncio.sleep(0.01)
        after_end.set()

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(_h2_get(1, "/"))
        frames, _buf = await h2.read_stream(reader, buf, 1)
        async with asyncio.timeout(2):
            await after_end.wait()
        writer.close()
    assert h2.stream_data(frames, 1) == b"done"


@pytest.mark.asyncio
async def test_chunked_trailers_are_not_merged_into_headers() -> None:
    app = App()
    seen: list[object] = []

    async def upload(c, w) -> None:
        body = await c.req.body()
        seen.append(c.req.headers.get("x-trailer"))
        w.text(body.decode())

    app.add(Route("POST /"), upload)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n0\r\nX-Trailer: injected\r\n\r\n"
        )
        response = await read_response(reader)
        writer.close()
    assert response.startswith(b"HTTP/1.1 200")
    assert response.endswith(b"abc")
    assert seen == [None]


@pytest.mark.asyncio
async def test_oversized_trailers_do_not_break_the_request() -> None:
    app = App()

    async def upload(c, w) -> None:
        w.text((await c.req.body()).decode())

    app.add(Route("POST /"), upload)
    async with running_server(app, max_header_bytes=1024) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n0\r\nX-Big: " + b"a" * 2048 + b"\r\n\r\n"
            b"GET /missing HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        first = await read_response(reader)
        second = await read_response(reader)
        writer.close()
    assert first.startswith(b"HTTP/1.1 200")
    assert first.endswith(b"abc")
    assert second.startswith(b"HTTP/1.1 404")


@pytest.mark.asyncio
@pytest.mark.parametrize("http2", [False, True])
async def test_write_past_content_length_raises(http2: bool) -> None:
    app = App()
    errors: list[str] = []

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.headers.set("content-length", "3")
        try:
            w.write(b"abcd")
        except Exception as exc:
            errors.append(type(exc).__name__)
        w.write(b"abc")
        w.end()

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        if http2:
            reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
            writer.write(_h2_get(1, "/"))
            frames, _buf = await h2.read_stream(reader, buf, 1)
            body = h2.stream_data(frames, 1)
        else:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
            body = (await read_response(reader)).split(b"\r\n\r\n", 1)[1]
        writer.close()
    assert errors == ["StarioRuntime"]
    assert body == b"abc"


@pytest.mark.asyncio
async def test_h2_write_copies_a_callers_bytearray() -> None:
    app = App()

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        chunk = bytearray(b"first")
        w.write(chunk)
        chunk[:] = b"XXXXX"
        w.write(bytearray(b"-second"))
        w.end()

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(_h2_get(1, "/"))
        frames, _buf = await h2.read_stream(reader, buf, 1)
        writer.close()
    assert h2.stream_data(frames, 1) == b"first-second"


@pytest.mark.asyncio
async def test_h1_chunked_write_snapshots_a_callers_bytearray() -> None:
    app = App()

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        chunk = bytearray(b"first")
        w.write(chunk)
        chunk[:] = b"XXXXX"
        w.write(bytearray(b"-second"))
        w.end()

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        raw = await _raw_exchange(
            port,
            b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        )
    head, body = raw.split(b"\r\n\r\n", 1)
    assert b"transfer-encoding: chunked" in head.lower()
    assert _dechunk(body) == b"first-second"


@pytest.mark.asyncio
async def test_declared_length_write_snapshots_a_caller_bytearray() -> None:
    app = App()

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.headers.set("content-length", "8")
        w.write_headers(200)
        body = bytearray(b"original")
        w.write(body)
        body[:] = b"corrupt!"
        w.end()

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        raw = await _raw_exchange(
            port,
            b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        )
    head, body = raw.split(b"\r\n\r\n", 1)
    assert b"content-length: 8" in head.lower()
    assert body == b"original"


@pytest.mark.asyncio
async def test_respond_snapshots_a_caller_bytearray() -> None:
    app = App()

    async def page(_c, w) -> None:
        body = bytearray(b"original")
        w.respond(body, b"text/plain")
        body[:] = b"corrupt!"

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        raw = await _raw_exchange(
            port,
            b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        )
    head, body = raw.split(b"\r\n\r\n", 1)
    assert b"content-length: 8" in head.lower()
    assert body == b"original"


@pytest.mark.asyncio
async def test_wide_memoryview_body_is_rejected() -> None:
    app = App()
    errors: list[str] = []

    async def page(_c, w) -> None:
        wide = memoryview(array.array("i", [1, 2]))
        try:
            w.write(wide)
        except TypeError as exc:
            errors.append(str(exc))
        w.text("ok")

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        response = await read_response(reader)
        writer.close()
    assert response.endswith(b"ok")
    assert len(errors) == 1
    assert "memoryview" in errors[0]


@pytest.mark.asyncio
async def test_h2_no_rst_stream_after_client_reset() -> None:
    app = App()
    started = asyncio.Event()
    aborted = asyncio.Event()

    async def page(_c, w) -> None:
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            w.abort()
            aborted.set()
            raise

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(_h2_get(1, "/"))
        async with asyncio.timeout(2):
            await started.wait()
        writer.write(h2.pack_frame(h2.TYPE_RST_STREAM, 0, 1, (8).to_bytes(4, "big")))
        async with asyncio.timeout(2):
            await aborted.wait()
        ping = 0x6
        writer.write(h2.pack_frame(ping, 0, 0, b"barrier!"))
        frames: list[h2.H2Frame] = []
        async with asyncio.timeout(2):
            while not any(f.type == ping and f.flags & h2.FLAG_ACK for f in frames):
                buf += await reader.read(65536)
                parsed, buf = h2.parse_frames(buf)
                frames.extend(parsed)
        writer.close()
    assert not h2.has_rst(frames, 1)


async def _raw_exchange(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    async with asyncio.timeout(2):
        data = await reader.read()
    writer.close()
    return data


def _dechunk(body: bytes) -> bytes:
    out = b""
    while True:
        size_line, body = body.split(b"\r\n", 1)
        size = int(size_line, 16)
        if size == 0:
            return out
        out += body[:size]
        body = body[size + 2 :]


@pytest.mark.asyncio
@pytest.mark.parametrize("gzip", [False, True])
async def test_http10_stream_is_close_delimited(gzip: bool) -> None:
    app = App()

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.write(b"alpha-" * 50)
        w.write(memoryview(b"beta"))
        w.end()

    app.add(Route("GET /"), page)
    extra = b"Accept-Encoding: gzip\r\n" if gzip else b""
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        raw = await _raw_exchange(
            port,
            b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n" + extra + b"\r\n",
        )
    head, body = raw.split(b"\r\n\r\n", 1)
    lower = head.lower()
    assert head.startswith(b"HTTP/1.1 200")
    assert b"transfer-encoding" not in lower
    assert b"connection: close" in lower
    if gzip:
        assert b"content-encoding: gzip" in lower
        body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
    assert body == b"alpha-" * 50 + b"beta"


@pytest.mark.asyncio
@pytest.mark.parametrize("gzip", [False, True])
async def test_chunked_write_accepts_memoryview_parts(gzip: bool) -> None:
    app = App()

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.write([b"ab", memoryview(b"cd"), bytearray(b"ef")])
        w.write(memoryview(b"gh"))
        w.end()

    app.add(Route("GET /"), page)
    extra = b"Accept-Encoding: gzip\r\n" if gzip else b""
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        raw = await _raw_exchange(
            port,
            b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n" + extra + b"\r\n",
        )
    head, body = raw.split(b"\r\n\r\n", 1)
    assert b"transfer-encoding: chunked" in head.lower()
    body = _dechunk(body)
    if gzip:
        body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
    assert body == b"abcdefgh"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "view",
    [
        memoryview(b"abcdef").cast("B", (2, 3)),
        memoryview(b"abcdef")[::2],
    ],
    ids=["2d", "strided"],
)
async def test_non_flat_memoryview_body_is_rejected(view: memoryview) -> None:
    app = App()
    errors: list[str] = []

    async def page(_c, w) -> None:
        try:
            w.write(view)
        except TypeError as exc:
            errors.append(str(exc))
        w.text("ok")

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        response = await read_response(reader)
        writer.close()
    assert response.endswith(b"ok")
    assert len(errors) == 1
