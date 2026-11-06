import asyncio
import gzip

import pytest

import stario.cookies as cookies
from stario import App, Route
from stario.exceptions import StarioError, StarioRuntime
from stario.http.compression import CompressionConfig
from stario.http.headers import Headers as PublicHeaders
from stario_cython.codecs import (
    BrotliDecoder,
    ZstdDecoder,
    brotli_decompress,
    zstd_decompress,
)
from stario_cython.exchange import Headers
from tests.cython.http import read_chunk, read_response, running_server


def test_headers_public_api():
    assert PublicHeaders is Headers
    headers = Headers()
    headers.set("Host", "example.com")
    headers.add("Accept", "text/html")
    headers.add("Accept", "text/plain")
    assert headers.get("host") == "example.com"
    assert headers.getlist("accept") == ["text/html", "text/plain"]
    assert "HOST" in headers
    assert len(headers) == 2
    headers.remove("accept")
    assert headers.get("accept") is None


def test_headers_pair_list_keeps_order_and_repeats() -> None:
    headers = Headers()
    headers.set("X-A", "1")
    headers.add("Set-Cookie", "a=1")
    headers.add("Set-Cookie", "b=2")
    headers.set("X-B", "2")
    headers.set("X-A", "replaced")
    assert headers.items() == [
        ("set-cookie", "a=1"),
        ("set-cookie", "b=2"),
        ("x-b", "2"),
        ("x-a", "replaced"),
    ]
    assert headers.getlist("set-cookie") == ["a=1", "b=2"]
    assert headers.unsafe_items() == [
        (b"set-cookie", b"a=1"),
        (b"set-cookie", b"b=2"),
        (b"x-b", b"2"),
        (b"x-a", b"replaced"),
    ]
    assert len(headers) == 3
    headers.remove("set-cookie")
    assert headers.get("set-cookie") is None
    assert headers.items() == [("x-b", "2"), ("x-a", "replaced")]


def test_headers_unsafe_bytes():
    headers = Headers()
    headers.unsafe_add(b"user-agent", b"wrk/4")
    assert headers.unsafe_get(b"user-agent") == b"wrk/4"


@pytest.mark.parametrize(
    ("encoding", "compression"),
    [
        (
            b"br",
            CompressionConfig(
                min_size=0,
                brotli_level=4,
                brotli_window_log=18,
                zstd_level=-1,
                gzip_level=-1,
            ),
        ),
        (
            b"zstd",
            CompressionConfig(
                min_size=0,
                brotli_level=-1,
                zstd_level=3,
                gzip_level=-1,
            ),
        ),
        (
            b"gzip",
            CompressionConfig(
                min_size=0,
                brotli_level=-1,
                zstd_level=-1,
                gzip_level=6,
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_exchange_respond_native_compression_round_trip(
    encoding: bytes,
    compression: CompressionConfig,
) -> None:
    app = App()
    body = b"native compression response " * 64

    async def compressed(_c, w):
        w.respond(body, b"text/plain; charset=utf-8")

    app.add(Route("GET /"), compressed)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=compression,
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: " + encoding + b"\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header, compressed_body = payload.split(b"\r\n\r\n", 1)
            assert b"content-encoding: " + encoding + b"\r\n" in header + b"\r\n"
            if encoding == b"br":
                decoded = brotli_decompress(compressed_body)
            elif encoding == b"zstd":
                decoded = zstd_decompress(compressed_body)
            else:
                decoded = gzip.decompress(compressed_body)
            assert decoded == body
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_one_shot_compression_writes_generated_headers_without_dict_roundtrip() -> (
    None
):
    app = App()
    state = {}
    body = b"direct generated response headers " * 32

    async def compressed(_c, w):
        w.headers.set("vary", "origin")
        w.headers.add("vary", "accept-language")
        w.headers.set("x-custom", "present")
        w.respond(body, b"text/plain; charset=utf-8")
        state["content-encoding"] = w.headers.get("content-encoding")
        state["content-type"] = w.headers.get("content-type")
        state["content-length"] = w.headers.get("content-length")
        state["vary"] = w.headers.get("vary")

    app.add(Route("GET /"), compressed)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=-1,
            zstd_level=-1,
            gzip_level=6,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: gzip\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header, compressed_body = payload.split(b"\r\n\r\n", 1)
            assert b"x-custom: present\r\n" in header + b"\r\n"
            assert b"content-encoding: gzip\r\n" in header + b"\r\n"
            assert b"vary: origin\r\n" in header + b"\r\n"
            assert b"vary: accept-language\r\n" in header + b"\r\n"
            assert b"vary: accept-encoding\r\n" in header + b"\r\n"
            assert b"content-type: text/plain; charset=utf-8\r\n" in header + b"\r\n"
            assert gzip.decompress(compressed_body) == body
            assert state == {
                "content-encoding": None,
                "content-type": None,
                "content-length": None,
                "vary": "origin",
            }
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize(
    ("accept_encoding", "expected"),
    [
        (b"gzip;q=1, br;q=0.5", b"gzip"),
        (b"gzip;q=0.5, br;q=0.5", b"br"),
        (b"BR;Q=1", b"br"),
        (b"gzip, zstd", b"zstd"),
        (b"zstd;q=0.5, gzip;q=0.5", b"zstd"),
        (b"zstd, br", b"zstd"),
        (b"ZSTD;Q=1", b"zstd"),
        (b"*;q=0.8, identity;q=1", None),
    ],
)
@pytest.mark.asyncio
async def test_native_compression_qvalue_negotiation(
    accept_encoding: bytes,
    expected: bytes | None,
) -> None:
    app = App()
    body = b"compression negotiation " * 32

    async def compressed(_c, w):
        w.respond(body, b"text/plain")

    app.add(Route("GET /"), compressed)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=4,
            zstd_level=3,
            gzip_level=6,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: "
                + accept_encoding
                + b"\r\nConnection: keep-alive, CLOSE\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header = payload.split(b"\r\n\r\n", 1)[0] + b"\r\n"
            if expected is None:
                assert b"content-encoding:" not in header
            else:
                assert b"content-encoding: " + expected + b"\r\n" in header
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_compression_preference_overrides_tie_break() -> None:
    app = App()
    body = b"compression preference " * 32

    async def compressed(_c, w):
        w.respond(body, b"text/plain")

    app.add(Route("GET /"), compressed)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(min_size=0, preference=("br",)),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: zstd, br\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header = payload.split(b"\r\n\r\n", 1)[0] + b"\r\n"
            assert b"content-encoding: br\r\n" in header
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_compression_disabled_still_serves_with_accept_encoding() -> None:
    app = App()
    body = b"plain body " * 64

    async def hello(_c, w):
        w.respond(body, b"text/plain")

    app.add(Route("GET /"), hello)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=-1,
            zstd_level=-1,
            gzip_level=-1,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: gzip, br\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header, resp_body = payload.split(b"\r\n\r\n", 1)
            assert b"content-encoding:" not in header.lower()
            assert resp_body == body
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize(
    ("content_type", "compressed"),
    [
        (b" Text/Plain ; charset=utf-8", True),
        (b"IMAGE/PNG; charset=binary", False),
        (b"application/zip", False),
        (b"; charset=utf-8", False),
    ],
)
@pytest.mark.asyncio
async def test_native_content_type_compression_check(
    content_type: bytes,
    compressed: bool,
) -> None:
    app = App()

    async def respond(_c, w):
        w.respond(b"x" * 1024, content_type)

    app.add(Route("GET /"), respond)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=-1,
            zstd_level=-1,
            gzip_level=6,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: gzip\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            header = (await read_response(reader)).split(b"\r\n\r\n", 1)[0]
            assert (b"content-encoding: gzip\r\n" in header + b"\r\n") is compressed
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_sse_gzip_flushes_each_write() -> None:
    app = App()
    first_written = asyncio.Event()
    release_second = asyncio.Event()
    state = {}

    async def stream(_c, w):
        state["exchange"] = w
        w.headers.set("content-type", "text/event-stream")
        w.write_headers(200)
        w.write(b"data: 0\n\n")
        first_written.set()
        await release_second.wait()
        w.write(b"data: 1\n\n")
        w.end()

    app.add(Route("GET /stream"), stream)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            brotli_level=-1,
            zstd_level=-1,
            gzip_level=6,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET /stream HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: gzip\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            assert header.startswith(b"HTTP/1.1 200 OK\r\n")
            assert b"content-encoding: gzip\r\n" in header
            assert b"transfer-encoding: chunked\r\n" in header

            await first_written.wait()
            compressed = [_read := await read_chunk(reader)]
            assert _read
            release_second.set()
            while chunk := await read_chunk(reader):
                compressed.append(chunk)

            assert gzip.decompress(b"".join(compressed)) == (b"data: 0\n\ndata: 1\n\n")
            assert state["exchange"].started
            assert state["exchange"].completed
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_sse_brotli_flushes_each_write() -> None:
    app = App()
    first_written = asyncio.Event()
    release_second = asyncio.Event()

    async def stream(_c, w):
        w.headers.set("content-type", "text/event-stream")
        w.write_headers(200)
        w.write(b"data: 0\n\n")
        first_written.set()
        await release_second.wait()
        w.write(b"data: 1\n\n")
        w.end()

    app.add(Route("GET /stream"), stream)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            brotli_level=4,
            brotli_window_log=18,
            zstd_level=-1,
            gzip_level=-1,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET /stream HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: br\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            assert b"content-encoding: br\r\n" in header
            assert b"transfer-encoding: chunked\r\n" in header

            await first_written.wait()
            first_chunk = await read_chunk(reader)
            assert first_chunk
            decoder = BrotliDecoder()
            decoded = [decoder.decompress(first_chunk)]
            assert decoded == [b"data: 0\n\n"]

            release_second.set()
            while chunk := await read_chunk(reader):
                decoded.append(decoder.decompress(chunk))
            assert b"".join(decoded) == b"data: 0\n\ndata: 1\n\n"
            assert decoder.is_finished()
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_sse_zstd_flushes_each_write() -> None:
    app = App()
    first_written = asyncio.Event()
    release_second = asyncio.Event()

    async def stream(_c, w):
        w.headers.set("content-type", "text/event-stream")
        w.write_headers(200)
        w.write(b"data: 0\n\n")
        first_written.set()
        await release_second.wait()
        w.write(b"data: 1\n\n")
        w.end()

    app.add(Route("GET /stream"), stream)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            brotli_level=-1,
            zstd_level=3,
            gzip_level=-1,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET /stream HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: zstd\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            assert b"content-encoding: zstd\r\n" in header
            assert b"transfer-encoding: chunked\r\n" in header

            await first_written.wait()
            first_chunk = await read_chunk(reader)
            assert first_chunk
            decoder = ZstdDecoder()
            decoded = [decoder.decompress(first_chunk)]
            assert decoded == [b"data: 0\n\n"]

            release_second.set()
            while chunk := await read_chunk(reader):
                decoded.append(decoder.decompress(chunk))
            assert b"".join(decoded) == b"data: 0\n\ndata: 1\n\n"
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_respond_plaintext_uses_the_registry_date() -> None:
    app = App()
    state = {}

    async def plaintext(_c, w):
        w.respond(b"Hello, World!", b"text/plain; charset=utf-8", 200)
        state["exchange"] = w
        state["status"] = w.status_code
        state["completed"] = w.completed

    app.add(Route("GET /"), plaintext)
    async with running_server(app, date=b"date: boxed\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            assert payload.startswith(b"HTTP/1.1 200 OK\r\n")
            assert b"date: boxed\r\n" in payload
            assert payload.endswith(b"Hello, World!")
            assert state["status"] == 200
            assert state["completed"]
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_rejects_negative_content_length() -> None:
    app = App()
    caught = asyncio.get_running_loop().create_future()

    async def invalid(_c, w):
        w.headers.unsafe_set(b"content-length", b"-1")
        try:
            w.write_headers(200)
        except StarioError as exc:
            caught.set_result(exc)
            w.abort()

    app.add(Route("GET /"), invalid)
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(2):
                exc = await caught
            assert "Invalid Content-Length" in str(exc)
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_accepts_list_of_bytes_parts() -> None:
    app = App()

    async def multi(_c, w):
        w.respond([b"hel", b"lo", b", ", b"parts"], b"text/plain; charset=utf-8")

    app.add(Route("GET /"), multi)
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            assert b"content-length: 12\r\n" in payload
            assert payload.endswith(b"hello, parts")
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_accepts_tuple_of_bytes_parts() -> None:
    app = App()

    async def multi(_c, w):
        w.respond((b"tup", b"le"), b"text/plain; charset=utf-8")

    app.add(Route("GET /"), multi)
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            assert b"content-length: 5\r\n" in payload
            assert payload.endswith(b"tuple")
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_write_known_length_accepts_list_of_bytes_parts() -> None:
    app = App()

    async def multi(_c, w):
        w.headers.unsafe_set(b"content-type", b"text/plain")
        w.headers.unsafe_set(b"content-length", b"11")
        w.write_headers(200)
        w.write([b"hello", b" ", b"world"])
        w.end()

    app.add(Route("GET /"), multi)
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            assert payload.endswith(b"hello world")
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_write_chunked_accepts_list_of_bytes_parts() -> None:
    app = App()

    async def multi(_c, w):
        w.headers.set("content-type", "text/plain")
        w.write_headers(200)
        w.write([b"aaa", b"bbb", b"ccc"])
        w.end()

    app.add(Route("GET /"), multi)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(brotli_level=-1, zstd_level=-1, gzip_level=-1),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            assert b"transfer-encoding: chunked" in header.lower()
            assert await read_chunk(reader) == b"aaabbbccc"
            assert await read_chunk(reader) == b""
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_list_parts_native_compression_round_trip() -> None:
    app = App()
    parts = [b"native ", b"list ", b"compression " * 32]

    async def compressed(_c, w):
        w.respond(parts, b"text/plain; charset=utf-8")

    app.add(Route("GET /"), compressed)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=4,
            brotli_window_log=18,
            zstd_level=-1,
            gzip_level=-1,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: br\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header, compressed_body = payload.split(b"\r\n\r\n", 1)
            assert b"content-encoding: br\r\n" in header + b"\r\n"
            assert brotli_decompress(compressed_body) == b"".join(parts)
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_rejects_non_bytes_parts() -> None:
    app = App()
    caught = asyncio.get_running_loop().create_future()

    async def bad(_c, w):
        try:
            w.respond([b"ok", "nope"], b"text/plain")
        except TypeError as exc:
            caught.set_result(exc)
            w.respond(b"err", b"text/plain", 500)

    app.add(Route("GET /"), bad)
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(2):
                exc = await caught
            assert "bytes-like" in str(exc)
            await read_response(reader)
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_writes_extra_headers_before_derived_type_and_length() -> None:
    app = App()

    async def extra(_c, w):
        w.headers.set("X-Custom", "present")
        w.headers.add("Set-Cookie", "a=1")
        w.headers.add("Set-Cookie", "b=2")
        w.respond(b"hello", b"text/plain; charset=utf-8")

    app.add(Route("GET /"), extra)
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header, body = payload.split(b"\r\n\r\n", 1)
            block = header + b"\r\n"
            assert body == b"hello"
            assert b"x-custom: present\r\n" in block
            assert b"set-cookie: a=1\r\n" in block
            assert b"set-cookie: b=2\r\n" in block
            assert b"content-type: text/plain; charset=utf-8\r\n" in block
            assert b"content-length: 5\r\n" in block
            extra_at = block.find(b"x-custom: present\r\n")
            type_at = block.find(b"content-type: text/plain; charset=utf-8\r\n")
            assert extra_at != -1
            assert extra_at < type_at
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_reads_input_headers_and_writes_cookies() -> None:
    app = App()

    async def echo(_c, w):
        assert _c.req.cookies.get("session") == "abc123"
        assert _c.req.headers.get("authorization") == "Bearer tok"
        assert _c.req.headers.get("x-request-id") == "in-1"
        w.headers.set("X-Request-ID", _c.req.headers.get("x-request-id") or "")
        cookies.set_cookie(w, "session", "abc123")
        cookies.set_cookie(w, "theme", "dark")
        w.respond(b"hello", b"text/plain; charset=utf-8")

    app.add(Route("GET /"), echo)
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Authorization: Bearer tok\r\n"
                b"Cookie: session=abc123\r\n"
                b"X-Request-ID: in-1\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header, body = payload.split(b"\r\n\r\n", 1)
            block = header + b"\r\n"
            assert body == b"hello"
            assert b"x-request-id: in-1\r\n" in block
            assert b"session=abc123" in block
            assert b"theme=dark" in block
            assert block.lower().count(b"set-cookie:") == 2
            assert b"content-type: text/plain; charset=utf-8\r\n" in block
            assert b"content-length: 5\r\n" in block
            cookies_at = block.lower().find(b"set-cookie:")
            type_at = block.find(b"content-type: text/plain; charset=utf-8\r\n")
            assert cookies_at != -1
            assert cookies_at < type_at
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_accepts_matching_owned_headers() -> None:
    app = App()

    async def matching(_c, w):
        w.headers.set("content-type", "text/plain; charset=utf-8")
        w.headers.set("content-length", "2")
        w.respond(b"ok", b"text/plain; charset=utf-8")

    app.add(Route("GET /"), matching)
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header, body = payload.split(b"\r\n\r\n", 1)
            block = header + b"\r\n"
            assert body == b"ok"
            assert block.count(b"content-type: text/plain; charset=utf-8\r\n") == 1
            assert block.count(b"content-length: 2\r\n") == 1
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prepare", "match"),
    [
        (
            lambda w: w.headers.set("content-type", "text/html"),
            "Content-Type",
        ),
        (
            lambda w: w.headers.set("content-length", "99"),
            "Content-Length",
        ),
        (
            lambda w: w.headers.set("date", "Tue, 01 Jan 2030 00:00:00 GMT"),
            "Date is emitted",
        ),
        (
            lambda w: w.headers.set("transfer-encoding", "chunked"),
            "Transfer-Encoding",
        ),
    ],
)
async def test_respond_errors_when_owned_headers_conflict(
    prepare,
    match: str,
) -> None:
    app = App()
    caught = asyncio.get_running_loop().create_future()

    async def bad(_c, w):
        prepare(w)
        try:
            w.respond(b"ok", b"text/plain; charset=utf-8")
        except StarioRuntime as exc:
            caught.set_result(exc)
            w.abort()

    app.add(Route("GET /"), bad)
    async with running_server(app, date=b"date: now\r\n") as port:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(2):
                exc = await caught
            assert match in str(exc)
        finally:
            writer.close()
            await writer.wait_closed()
