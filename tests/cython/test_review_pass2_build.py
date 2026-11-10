"""Second review pass: vendored llhttp, native decoders, and build flags."""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

import pytest

from stario import App, Route
from stario.exceptions import StarioError
from stario_cython.codecs import (
    BrotliDecoder,
    BrotliEncoder,
    ZstdDecoder,
    ZstdEncoder,
    brotli_compress,
    brotli_decompress,
    zstd_compress,
    zstd_decompress,
)
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


def test_zstd_decompress_reads_every_concatenated_frame() -> None:
    # RFC 8878 3: a zstd stream is any number of frames. Only the first was
    # returned; the rest was dropped without an error.
    data = zstd_compress(b"first ") + zstd_compress(b"second")
    assert zstd_decompress(data) == b"first second"


def test_zstd_decoder_reads_every_frame_in_one_push() -> None:
    decoder = ZstdDecoder()
    data = zstd_compress(b"a" * 1000) + zstd_compress(b"b" * 1000)
    assert decoder.decompress(data) == b"a" * 1000 + b"b" * 1000
    assert decoder.is_finished()


def test_zstd_decoder_is_unfinished_inside_a_second_frame() -> None:
    decoder = ZstdDecoder()
    second = zstd_compress(b"b" * 1000)
    assert decoder.decompress(zstd_compress(b"a") + second[:5]) == b"a"
    assert not decoder.is_finished()
    assert decoder.decompress(second[5:]) == b"b" * 1000
    assert decoder.is_finished()


def test_zstd_decompress_rejects_trailing_garbage_and_truncation() -> None:
    frame = zstd_compress(b"payload")
    with pytest.raises(StarioError):
        zstd_decompress(frame + b"\x00junk")
    with pytest.raises(StarioError):
        zstd_decompress(frame[:-2])


def test_brotli_decompress_rejects_bytes_after_the_stream() -> None:
    # A brotli stream ends at its last meta-block: bytes after it are not
    # part of the body (the reference decoder rejects them).
    with pytest.raises(StarioError):
        brotli_decompress(brotli_compress(b"payload") + b"junk")
    assert brotli_decompress(brotli_compress(b"payload")) == b"payload"


def test_brotli_decoder_rejects_bytes_after_the_stream() -> None:
    decoder = BrotliDecoder()
    assert decoder.decompress(brotli_compress(b"payload")) == b"payload"
    assert decoder.is_finished()
    with pytest.raises(StarioError):
        decoder.decompress(b"junk")


def _hammer(encoder, barrier: threading.Barrier, errors: list[BaseException]) -> None:
    barrier.wait()
    try:
        for _ in range(50):
            encoder.block(b"x" * 512)
        encoder.finish(b"end")
    except StarioError as exc:  # another thread finished it first
        errors.append(exc)


def test_stream_encoder_survives_concurrent_use() -> None:
    """Free-threaded builds: two threads on one encoder raced on its C state,
    and two finish() calls could both return it to the pool (a later double
    use). With the GIL this only checks the results stay consistent."""
    for cls in (ZstdEncoder, BrotliEncoder):
        for _ in range(20):
            encoder = cls(3)
            start = threading.Barrier(4)
            errors: list[BaseException] = []
            threads = [
                threading.Thread(target=_hammer, args=(encoder, start, errors))
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert encoder.finish() == b""
            assert all("finished" in str(exc) for exc in errors)
