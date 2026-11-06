"""HTTP/2 on the Cython protocol: cleartext prior knowledge and TLS ALPN."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stario import App, Route
from stario.datastar import SSE
from stario.http.tls import load_tls_context
from tests.cython.h2wire import (
    FLAG_ACK,
    FLAG_END_HEADERS,
    FLAG_END_STREAM,
    H2_PREFACE,
    NGHTTP2_CANCEL,
    NGHTTP2_PROTOCOL_ERROR,
    SETTINGS_HEADER_TABLE_SIZE,
    SETTINGS_MAX_HEADER_LIST_SIZE,
    SETTINGS_NO_RFC7540_PRIORITIES,
    TYPE_CONTINUATION,
    TYPE_DATA,
    TYPE_HEADERS,
    TYPE_RST_STREAM,
    TYPE_SETTINGS,
    TYPE_WINDOW_UPDATE,
    collected_settings,
    encode_request,
    h2_handshake,
    has_goaway,
    has_rst,
    headers_duplicate_method,
    headers_duplicate_path,
    pack_frame,
    parse_frames,
    read_stream,
    rst_code,
    stream_data,
    stream_ended,
    stream_headers_blob,
)
from tests.cython.http import (
    RecordingTransport,
    connection_set,
    free_port,
    make_protocol,
    response_status,
)


async def _curl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run curl without blocking the event loop (the server lives on it)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["curl", "-sS", "--max-time", "5", *args],
            check=False,
            capture_output=True,
            text=True,
        ),
    )


def _make_self_signed(dir_path: Path) -> tuple[Path, Path]:
    cert = dir_path / "cert.pem"
    key = dir_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cert, key


@pytest.mark.asyncio
async def test_h2_prior_knowledge_plaintext() -> None:
    app = App()

    async def hello(c, w) -> None:
        assert c.req.protocol_version == "2"
        assert c.req.query.get("q") == "1"
        w.text(f"h2:{c.req.path}")

    app.add(Route("GET /hi"), hello)
    loop = asyncio.get_running_loop()
    connections = connection_set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            f"http://127.0.0.1:{port}/hi?q=1",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "h2:/hi"
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_trailing_slash_redirects() -> None:
    """H2 trailing-slash 308 is frames, not HTTP/1.1 text (same rule as H1)."""
    app = App()

    async def search(c, w) -> None:
        w.text("nope")

    app.add(Route("GET /search"), search)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-D",
            "-",
            "-o",
            "/dev/null",
            f"http://127.0.0.1:{port}/search/?q=1",
        )
        assert result.returncode == 0, result.stderr
        assert "HTTP/2 308" in result.stdout
        assert "location: /search?q=1" in result.stdout.lower()
        assert "nope" not in result.stdout
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_empty_body() -> None:
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.add(Route("POST /echo"), echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-X",
            "POST",
            "-H",
            "Content-Length: 0",
            f"http://127.0.0.1:{port}/echo",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_large_body() -> None:
    """Bodies larger than 256KiB still dispatch at headers; body() waits on DATA."""
    payload = b"x" * (257 * 1024)
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.add(Route("POST /echo"), echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(payload)
            tmp.flush()
            result = await _curl(
                "--http2-prior-knowledge",
                "-X",
                "POST",
                "--data-binary",
                f"@{tmp.name}",
                f"http://127.0.0.1:{port}/echo",
            )
        assert result.returncode == 0, result.stderr
        assert result.stdout.encode("latin-1") == payload
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


def _iter_h2_frames(blob: bytes):
    """Yield (type, flags, stream_id, payload) from a wire dump."""
    i = 0
    while i + 9 <= len(blob):
        length = int.from_bytes(blob[i : i + 3], "big")
        end = i + 9 + length
        if end > len(blob):
            break
        yield (
            blob[i + 3],
            blob[i + 4],
            int.from_bytes(blob[i + 5 : i + 9], "big") & 0x7FFFFFFF,
            blob[i + 9 : end],
        )
        i = end


@pytest.mark.asyncio
async def test_h2_keep_alive_returns_window_credit() -> None:
    """Small POSTs on one connection must return recv credit when streams end.

    nghttp2 consume() only emits WINDOW_UPDATE at 50% of the local window
    (2MiB for a 4MiB connection). A client that only has ~1MiB of send
    credit then deadlocks. Credit must be submitted, not merely consumed.
    """
    if shutil.which("h2load") is None:
        pytest.skip("h2load not installed")
    payload = b"x" * 256
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.add(Route("POST /echo"), echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    nreq = 20_000
    try:
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(payload)
            tmp.flush()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [
                        "h2load",
                        "-n",
                        str(nreq),
                        "-c",
                        "1",
                        "-m",
                        "1",
                        "-d",
                        tmp.name,
                        f"http://127.0.0.1:{port}/echo",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                ),
            )
        assert result.returncode == 0, result.stderr
        assert f"{nreq} succeeded" in result.stdout, result.stdout
        assert " 0 failed" in result.stdout
        assert " 0 errored" in result.stdout
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_body() -> None:
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.add(Route("POST /echo"), echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-X",
            "POST",
            "--data-binary",
            "abcde",
            f"http://127.0.0.1:{port}/echo",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "abcde"
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_tls_http11_and_h2_alpn() -> None:
    app = App()

    async def hello(c, w) -> None:
        w.text(f"v={c.req.protocol_version}")

    app.add(Route("GET /tls"), hello)
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = _make_self_signed(Path(tmp))
        ctx = load_tls_context(certfile=cert, keyfile=key)
        port = free_port()
        server = await loop.create_server(
            lambda: make_protocol(loop, app),
            "127.0.0.1",
            port,
            ssl=ctx,
        )
        try:
            h1 = await _curl(
                "--http1.1",
                "-k",
                f"https://127.0.0.1:{port}/tls",
            )
            assert h1.returncode == 0, h1.stderr
            assert h1.stdout == "v=1.1"

            h2 = await _curl(
                "--http2",
                "-k",
                f"https://127.0.0.1:{port}/tls",
            )
            assert h2.returncode == 0, h2.stderr
            assert h2.stdout == "v=2"
        finally:
            server.close()
            await server.wait_closed()
            await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_connect_is_rejected() -> None:
    """No tunnels or WebSockets: CONNECT must not dispatch a handler."""
    app = App()
    seen: list[str] = []

    async def boom(c, w) -> None:
        seen.append(c.req.method)
        w.text("nope")

    app.add(Route("GET /"), boom)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-X",
            "CONNECT",
            f"http://127.0.0.1:{port}/",
        )
        assert seen == []
        assert "nope" not in result.stdout
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_preface_on_recording_transport_does_not_raise() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
        assert proto.parse_mode == 2
        assert transport.writes  # server SETTINGS
        wire = b"".join(transport.writes)
        conn_updates = [
            int.from_bytes(payload, "big") & 0x7FFFFFFF
            for ftype, _flags, stream_id, payload in _iter_h2_frames(wire)
            if ftype == 0x8 and stream_id == 0 and len(payload) == 4
        ]
        # Connection window starts at 65535; we enlarge it to 4MiB.
        assert conn_updates
        assert sum(conn_updates) >= (4 * 1024 * 1024) - 65535
        frames, _rest = parse_frames(wire)
        settings = collected_settings(frames)
        assert settings.get(SETTINGS_MAX_HEADER_LIST_SIZE) == 64 * 1024
        assert settings.get(SETTINGS_NO_RFC7540_PRIORITIES) == 1
        assert settings.get(SETTINGS_HEADER_TABLE_SIZE) == 4096
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


def _h2_oversize_rejected(frames: list, stream_id: int, hits: int) -> None:
    """Oversize H2 headers: no handler, this stream dies, connection stays up."""
    assert hits == 0
    assert not has_goaway(frames)
    assert stream_ended(frames, stream_id)
    blob = stream_headers_blob(frames, stream_id)
    assert b"431" in blob or has_rst(frames, stream_id)


@pytest.mark.asyncio
async def test_h2_settings_advertise_header_budget() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    proto = make_protocol(loop, app, max_header_bytes=512)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
        frames, _rest = parse_frames(b"".join(transport.writes))
        settings = collected_settings(frames)
        assert settings.get(SETTINGS_MAX_HEADER_LIST_SIZE) == 512
        assert settings.get(SETTINGS_NO_RFC7540_PRIORITIES) == 1
        assert settings.get(SETTINGS_HEADER_TABLE_SIZE) == 4096
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_small_get_under_header_budget() -> None:
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("ok")

    app.add(Route("GET /"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_header_bytes=512),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert not has_goaway(frames)
            assert stream_data(frames, 1) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_header_over_limit_returns_431_without_dispatch() -> None:
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("should not run")

    app.add(Route("GET /"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_header_bytes=512),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-D",
            "-",
            "-o",
            "/dev/null",
            "-H",
            "X-Pad: " + ("x" * 600),
            f"http://127.0.0.1:{port}/",
        )
        assert result.returncode == 0, result.stderr
        assert "HTTP/2 431" in result.stdout
        assert "should not run" not in result.stdout
        assert hits == 0
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_oversize_headers_do_not_kill_connection() -> None:
    """431 is per-stream: a later GET on the same connection still runs."""
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("ok")

    app.add(Route("GET /"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_header_bytes=512),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(
                        path="/",
                        extra=[(b"x-pad", b"x" * 600)],
                    ),
                )
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1)
            _h2_oversize_rejected(frames, 1, hits)

            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_rst(frames, 3)
            assert not has_goaway(frames)
            assert stream_data(frames, 3) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_oversize_path_returns_431() -> None:
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("should not run")

    app.add(Route("GET /{path...}"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_header_bytes=512),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(path="/" + ("a" * 600)),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            _h2_oversize_rejected(frames, 1, hits)
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_declared_body_over_limit_returns_413() -> None:
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        nonlocal hits
        hits += 1
        await c.req.body()
        w.text("should not run")

    app.add(Route("POST /"), read_body)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_body_bytes=20),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-D",
            "-",
            "-o",
            "/dev/null",
            "-X",
            "POST",
            "-d",
            "x" * 100,
            f"http://127.0.0.1:{port}/",
        )
        assert result.returncode == 0, result.stderr
        assert "HTTP/2 413" in result.stdout
        assert "should not run" not in result.stdout
        assert hits == 1
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_body_over_default_with_larger_max_size_is_read() -> None:
    app = App()

    async def read_body(c, w) -> None:
        w.text(str(len(await c.req.body(max_size=200))))

    app.add(Route("POST /"), read_body)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_body_bytes=20),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-X",
            "POST",
            "-d",
            "x" * 100,
            f"http://127.0.0.1:{port}/",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "100"
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_content_length_above_read_cap_is_413_before_handler() -> None:
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        nonlocal hits
        hits += 1
        await c.req.body(max_size=2**40)
        w.text("ok")

    app.add(Route("POST /"), read_body)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(
                        method="POST",
                        path="/",
                        extra=[(b"content-length", b"2147483648")],
                    ),
                )
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1)
            assert hits == 0
            assert not has_goaway(frames)
            assert b"413" in stream_headers_blob(frames, 1)
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_oversize_body_does_not_kill_connection() -> None:
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        await c.req.body()
        w.text("should not run")

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("ok")

    app.add(Route("GET /"), hello)
    app.add(Route("POST /"), read_body)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_body_bytes=20),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(
                        method="POST",
                        path="/",
                        extra=[(b"content-length", b"1000000")],
                    ),
                )
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1)
            assert hits == 0
            assert not has_goaway(frames)
            blob = stream_headers_blob(frames, 1)
            assert b"413" in blob or has_rst(frames, 1)

            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_rst(frames, 3)
            assert not has_goaway(frames)
            assert stream_data(frames, 3) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_no_cl_body_overflow_does_not_kill_connection() -> None:
    """No-CL DATA over max_body_bytes is a stream 413; neighbors still run."""
    app = App()
    hits = 0

    async def read_body(c, w) -> None:
        await c.req.body()
        w.text("should not run")

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("ok")

    app.add(Route("POST /"), read_body)
    app.add(Route("GET /"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_body_bytes=20),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(method="POST", path="/"),
                )
                + pack_frame(TYPE_DATA, FLAG_END_STREAM, 1, b"y" * 100)
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1)
            assert not has_goaway(frames)
            blob = stream_headers_blob(frames, 1)
            assert b"413" in blob or has_rst(frames, 1)
            assert stream_data(frames, 1) != b"should not run"

            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_rst(frames, 3)
            assert not has_goaway(frames)
            assert stream_data(frames, 3) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_discarded_oversize_data_does_not_kill_connection() -> None:
    """Extra DATA after the handler finished must not close the connection."""
    app = App()
    hits = 0

    async def ignore_body(_c, w) -> None:
        w.text("ok")

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("ok")

    app.add(Route("POST /"), ignore_body)
    app.add(Route("GET /"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_body_bytes=20),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(method="POST", path="/"),
                )
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1)
            assert stream_data(frames, 1) == b"ok"
            assert not has_goaway(frames)

            writer.write(pack_frame(TYPE_DATA, FLAG_END_STREAM, 1, b"y" * 100))
            await writer.drain()
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_goaway(frames)
            assert not has_rst(frames, 3)
            assert stream_data(frames, 3) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_incomplete_headers_timeout_does_not_kill_connection() -> None:
    """A stalled HEADERS block is RST on that stream; a later GET still runs."""
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        w.text("ok")

    app.add(Route("GET /"), hello)
    loop = asyncio.get_running_loop()
    connections = connection_set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections, header_timeout=0.15),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    0,
                    1,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1, timeout=2.0)
            assert hits == 0
            assert not has_goaway(frames)
            assert has_rst(frames, 1)
            code = rst_code(frames, 1)
            if code is not None:
                assert code == NGHTTP2_CANCEL

            # Header block is still open until END_HEADERS (RFC 9113). RST
            # does not end it; a CONTINUATION must follow before stream 3.
            writer.write(
                pack_frame(TYPE_CONTINUATION, FLAG_END_HEADERS, 1)
                + pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_rst(frames, 3)
            assert stream_data(frames, 3) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_without_content_length_reads_data() -> None:
    """HEADERS without END_STREAM and no Content-Length still arms the body."""
    app = App()
    seen: list[bytes] = []

    async def echo(c, w) -> None:
        body = await c.req.body()
        seen.append(body)
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.add(Route("POST /echo"), echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            payload = b"no-cl-body"
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(method="POST", path="/echo"),
                )
                + pack_frame(TYPE_DATA, FLAG_END_STREAM, 1, payload)
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert stream_data(frames, 1) == payload
            assert seen == [payload]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_without_content_length_keep_alive_isolates_bodies() -> None:
    """Recycled exchanges must not ingest DATA from a later stream."""
    app = App()
    seen: list[bytes] = []

    async def echo(c, w) -> None:
        body = await c.req.body()
        seen.append(body)
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.add(Route("POST /echo"), echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            for stream_id, payload in ((1, b"aaa"), (3, b"bbb")):
                writer.write(
                    pack_frame(
                        TYPE_HEADERS,
                        FLAG_END_HEADERS,
                        stream_id,
                        encode_request(method="POST", path="/echo"),
                    )
                    + pack_frame(TYPE_DATA, FLAG_END_STREAM, stream_id, payload)
                )
                await writer.drain()
                frames, buf = await read_stream(reader, buf, stream_id)
                assert not has_rst(frames, stream_id)
                assert stream_data(frames, stream_id) == payload
            assert seen == [b"aaa", b"bbb"]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [headers_duplicate_path(), headers_duplicate_method()],
    ids=["path", "method"],
)
async def test_h2_duplicate_pseudo_header_is_rejected(headers: bytes) -> None:
    app = App()
    seen: list[str] = []

    async def boom(c, w) -> None:
        seen.append(c.req.method)
        w.text("nope")

    app.add(Route("GET /"), boom)
    app.add(Route("GET /other"), boom)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    headers,
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert has_rst(frames, 1) or has_goaway(frames)
            code = rst_code(frames, 1)
            if code is not None:
                assert code == NGHTTP2_PROTOCOL_ERROR
            assert seen == []
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_authority_host_mismatch_is_rejected() -> None:
    app = App()
    seen: list[str] = []

    async def boom(c, w) -> None:
        seen.append(c.req.host)
        w.text("nope")

    app.add(Route("GET /"), boom)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(
                        path="/",
                        authority="example.com",
                        extra=[(b"host", b"other.com")],
                    ),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert has_rst(frames, 1) or has_goaway(frames)
            assert seen == []
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_authority_host_match_keeps_one_host() -> None:
    app = App()
    seen: list[tuple[str, list[str]]] = []

    async def hello(c, w) -> None:
        seen.append((c.req.host, c.req.headers.getlist("host")))
        w.text(c.req.host)

    app.add(Route("GET /"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(
                        path="/",
                        authority="example.com",
                        extra=[(b"host", b"example.com")],
                    ),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert stream_data(frames, 1) == b"example.com"
            assert seen == [("example.com", ["example.com"])]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_host_routing_normalizes_authority() -> None:
    app = App()

    async def hello(c, w) -> None:
        w.text(f"host:{c.req.host}")

    app.add(Route("GET //example.com/"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(path="/", authority="Example.COM:80"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert stream_data(frames, 1) == b"host:example.com"
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_coalesced_posts_keep_bodies_on_correct_streams() -> None:
    app = App()
    seen: list[bytes] = []

    async def echo(c, w) -> None:
        body = await c.req.body()
        seen.append(body)
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.add(Route("POST /"), echo)
    loop = asyncio.get_running_loop()
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(H2_PREFACE + pack_frame(TYPE_SETTINGS, 0, 0))
        extra = [(b"content-length", b"3")]
        proto.data_received(
            pack_frame(
                TYPE_HEADERS,
                FLAG_END_HEADERS,
                1,
                encode_request(method="POST", extra=extra),
            )
            + pack_frame(TYPE_DATA, FLAG_END_STREAM, 1, b"aaa")
            + pack_frame(
                TYPE_HEADERS,
                FLAG_END_HEADERS,
                3,
                encode_request(method="POST", extra=extra),
            )
            + pack_frame(TYPE_DATA, FLAG_END_STREAM, 3, b"bbb")
        )
        await app.drain_tasks()
        frames, _rest = parse_frames(b"".join(transport.writes))
        assert stream_data(frames, 1) == b"aaa"
        assert stream_data(frames, 3) == b"bbb"
        assert seen == [b"aaa", b"bbb"]
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_head_omits_data_payload() -> None:
    app = App()

    async def hello(_c, w) -> None:
        w.text("hello")

    app.add(Route("HEAD /"), hello)
    loop = asyncio.get_running_loop()
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(H2_PREFACE + pack_frame(TYPE_SETTINGS, 0, 0))
        proto.data_received(
            pack_frame(
                TYPE_HEADERS,
                FLAG_END_HEADERS | FLAG_END_STREAM,
                1,
                encode_request(method="HEAD", path="/"),
            )
        )
        await app.drain_tasks()
        frames, _rest = parse_frames(b"".join(transport.writes))
        assert stream_data(frames, 1) == b""
        assert stream_ended(frames, 1)
        assert not has_rst(frames, 1)
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_tls_http11_alpn_does_not_sniff_h2_preface() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    proto = make_protocol(loop, app)

    class _AlpnTransport(RecordingTransport):
        def get_extra_info(self, name, default=None):
            if name == "ssl_object":
                return SimpleNamespace(selected_alpn_protocol=lambda: "http/1.1")
            return default

    transport = _AlpnTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(H2_PREFACE)
        await app.drain_tasks()
        assert proto.parse_mode == 1
        assert response_status(transport.writes) == 400
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_skips_connection_specific_response_headers() -> None:
    app = App()

    async def hello(_c, w) -> None:
        w.headers.set("connection", "close")
        w.headers.set("keep-alive", "timeout=5")
        w.text("ok")

    app.add(Route("GET /"), hello)
    loop = asyncio.get_running_loop()
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(H2_PREFACE + pack_frame(TYPE_SETTINGS, 0, 0))
        proto.data_received(
            pack_frame(
                TYPE_HEADERS,
                FLAG_END_HEADERS | FLAG_END_STREAM,
                1,
                encode_request(path="/"),
            )
        )
        await app.drain_tasks()
        frames, _rest = parse_frames(b"".join(transport.writes))
        assert stream_data(frames, 1) == b"ok"
        blob = stream_headers_blob(frames, 1)
        assert b"keep-alive" not in blob
        assert b"connection" not in blob
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_authority_with_question_mark_is_rejected() -> None:
    app = App()

    async def boom(_c, w) -> None:
        w.text("nope")

    app.add(Route("GET /"), boom)
    loop = asyncio.get_running_loop()
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(H2_PREFACE + pack_frame(TYPE_SETTINGS, 0, 0))
        proto.data_received(
            pack_frame(
                TYPE_HEADERS,
                FLAG_END_HEADERS | FLAG_END_STREAM,
                1,
                encode_request(path="/", authority="evil?.example.com"),
            )
        )
        await app.drain_tasks()
        frames, _rest = parse_frames(b"".join(transport.writes))
        assert has_rst(frames, 1) or has_goaway(frames)
        assert stream_data(frames, 1) != b"nope"
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


SETTINGS_INITIAL_WINDOW_SIZE = 0x4


async def _h2_handshake_window(
    host: str, port: int, stream_window: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    """Handshake advertising ``stream_window`` as the initial stream window."""
    reader, writer = await asyncio.open_connection(host, port)
    settings = SETTINGS_INITIAL_WINDOW_SIZE.to_bytes(2, "big") + stream_window.to_bytes(
        4, "big"
    )
    writer.write(H2_PREFACE + pack_frame(TYPE_SETTINGS, 0, 0, settings))
    await writer.drain()
    buf = b""
    async with asyncio.timeout(2):
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                raise ConnectionError("closed during HTTP/2 handshake")
            buf += chunk
            frames, buf = parse_frames(buf)
            if any(
                frame.type == TYPE_SETTINGS and not (frame.flags & FLAG_ACK)
                for frame in frames
            ):
                writer.write(pack_frame(TYPE_SETTINGS, FLAG_ACK, 0))
                await writer.drain()
                return reader, writer, buf


async def _collect(reader, buf, seconds):
    """Read raw bytes for ``seconds``; return (frames, buf, eof)."""
    frames = []
    eof = False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        parsed, buf = parse_frames(buf)
        frames.extend(parsed)
        try:
            async with asyncio.timeout(max(0.01, deadline - time.monotonic())):
                chunk = await reader.read(65536)
        except TimeoutError:
            break
        if not chunk:
            eof = True
            break
        buf += chunk
    parsed, buf = parse_frames(buf)
    frames.extend(parsed)
    return frames, buf, eof


@pytest.mark.asyncio
async def test_h2_stalled_response_resets_stream() -> None:
    """A zero-window client that never opens the stream window gets
    RST_STREAM(CANCEL) at write_timeout; the connection stays usable."""
    app = App()

    async def big(_c, w) -> None:
        w.respond(b"x" * (2 * 1024 * 1024), b"application/octet-stream", 200)

    async def ok(_c, w) -> None:
        w.text("ok")

    app.add(Route("GET /big"), big)
    app.add(Route("GET /ok"), ok)
    loop = asyncio.get_running_loop()
    connections = connection_set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections, write_timeout=0.15),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await _h2_handshake_window("127.0.0.1", port, 0)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(method="GET", path="/big"),
                )
            )
            await writer.drain()
            frames, buf, _eof = await _collect(reader, buf, 0.8)
            assert has_rst(frames, 1)
            assert rst_code(frames, 1) == NGHTTP2_CANCEL
            assert not has_goaway(frames)
            assert not stream_data(frames, 1)

            # The connection survives: raise the initial window (a fresh
            # SETTINGS applies to streams opened after it) and ask again.
            settings = SETTINGS_INITIAL_WINDOW_SIZE.to_bytes(2, "big") + (
                65535
            ).to_bytes(4, "big")
            writer.write(
                pack_frame(TYPE_SETTINGS, 0, 0, settings)
                + pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(method="GET", path="/ok"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_rst(frames, 3)
            assert stream_data(frames, 3) == b"ok"
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_stalled_drain_handler_resets_stream() -> None:
    """write+drain under a zero window: the drain wait ends via the reset,
    the handler task unwinds, and the stream does not pin forever."""
    app = App()
    finished = asyncio.Event()

    async def dribble(c, w) -> None:
        w.write_headers(200)
        try:
            for _ in range(32):
                w.write(b"y" * 65536)
                await w.drain()
            w.end()
        finally:
            finished.set()

    app.add(Route("GET /stream"), dribble)
    loop = asyncio.get_running_loop()
    connections = connection_set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections, write_timeout=0.15),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await _h2_handshake_window("127.0.0.1", port, 0)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(method="GET", path="/stream"),
                )
            )
            await writer.drain()
            frames, buf, _eof = await _collect(reader, buf, 0.8)
            assert has_rst(frames, 1)
            assert rst_code(frames, 1) == NGHTTP2_CANCEL
            await asyncio.wait_for(finished.wait(), timeout=2)
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_send_progress_keeps_stalled_stream_alive() -> None:
    """A slow but progressing reader is not cut: WINDOW_UPDATE credit every
    ~0.1s keeps the stream below the write deadline."""
    app = App()

    async def big(_c, w) -> None:
        w.respond(b"x" * (512 * 1024), b"application/octet-stream", 200)

    app.add(Route("GET /big"), big)
    loop = asyncio.get_running_loop()
    connections = connection_set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections, write_timeout=0.5),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await _h2_handshake_window("127.0.0.1", port, 8192)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(method="GET", path="/big"),
                )
            )
            await writer.drain()
            # Trickling credit past several write deadlines must not reset.
            # The connection window needs the same trickle as the stream's.
            sent = 0
            for _ in range(8):
                await asyncio.sleep(0.1)
                writer.write(
                    pack_frame(TYPE_WINDOW_UPDATE, 0, 0, (8192).to_bytes(4, "big"))
                    + pack_frame(TYPE_WINDOW_UPDATE, 0, 1, (8192).to_bytes(4, "big"))
                )
                await writer.drain()
                parsed, buf = parse_frames(buf)
                sent += sum(
                    len(f.payload)
                    for f in parsed
                    if f.type == TYPE_DATA and f.stream_id == 1
                )
                assert not has_rst(parsed, 1)
            # Open both windows fully and read the rest of the body.
            writer.write(
                pack_frame(
                    TYPE_WINDOW_UPDATE, 0, 0, (4 * 1024 * 1024).to_bytes(4, "big")
                )
                + pack_frame(
                    TYPE_WINDOW_UPDATE, 0, 1, (4 * 1024 * 1024).to_bytes(4, "big")
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1, timeout=3)
            # What the credit trickle withheld now arrives.
            assert len(stream_data(frames, 1)) + sent == 512 * 1024
            assert not has_rst(frames, 1)
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_deferred_small_body_progress_outlives_header_timeout() -> None:
    """Small bodies defer dispatch until END_STREAM; incoming DATA resets the
    deadline so a slow upload does not die at the header timeout."""
    app = App()
    got: list[int] = []

    async def echo(c, w) -> None:
        body = await c.req.body()
        got.append(len(body))
        w.respond(body, b"application/octet-stream")

    app.add(Route("POST /echo"), echo)
    loop = asyncio.get_running_loop()
    connections = connection_set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(
            loop, app, connections=connections, header_timeout=0.15, body_timeout=5.0
        ),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(
                        method="POST",
                        path="/echo",
                        extra=[(b"content-length", b"64")],
                    ),
                )
            )
            await writer.drain()
            # Past the header deadline, a dribbled body must still land.
            for chunk in (b"d" * 24, b"d" * 24):
                await asyncio.sleep(0.35)
                writer.write(pack_frame(TYPE_DATA, 0, 1, chunk))
                await writer.drain()
            writer.write(pack_frame(TYPE_DATA, FLAG_END_STREAM, 1, b"d" * 16))
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert stream_data(frames, 1) == b"d" * 64
            assert got == [64]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_stalled_deferred_body_resets_stream() -> None:
    """A stream whose deferred body never arrives gets RST_STREAM(CANCEL) at
    body_timeout; a sibling stream on the same connection still answers."""
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"application/octet-stream")

    async def ok(_c, w) -> None:
        w.text("ok")

    app.add(Route("POST /echo"), echo)
    app.add(Route("GET /ok"), ok)
    loop = asyncio.get_running_loop()
    connections = connection_set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(
            loop, app, connections=connections, header_timeout=5.0, body_timeout=0.15
        ),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(
                        method="POST",
                        path="/echo",
                        extra=[(b"content-length", b"64")],
                    ),
                )
            )
            await writer.drain()
            frames, buf, _eof = await _collect(reader, buf, 0.8)
            assert has_rst(frames, 1)
            assert rst_code(frames, 1) == NGHTTP2_CANCEL
            assert not has_goaway(frames)

            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(method="GET", path="/ok"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert stream_data(frames, 3) == b"ok"
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_stream_reset_disconnects_only_that_stream() -> None:
    """c.disconnect follows the stream, not the connection: resetting one
    SSE stream ends its alive() loop cleanly while a sibling keeps running."""
    app = App()
    after: dict[int, str] = {}
    ready = [asyncio.Event(), asyncio.Event()]
    disconn: dict[int, Any] = {}

    def make_handler(idx: int):
        async def handler(c, w) -> None:
            disconn[idx] = c
            w.write_headers(200)
            ready[idx].set()
            async with c.alive():
                await asyncio.Event().wait()
            after[idx] = "clean"

        return handler

    app.add(Route("GET /a"), make_handler(0))
    app.add(Route("GET /b"), make_handler(1))
    loop = asyncio.get_running_loop()
    connections = connection_set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections),
        "127.0.0.1",
        port,
    )
    try:
        _reader, writer, _buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(method="GET", path="/a"),
                )
                + pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(method="GET", path="/b"),
                )
            )
            await writer.drain()
            await asyncio.wait_for(asyncio.gather(ready[0].wait(), ready[1].wait()), 2)

            # Reset stream 1 only; stream 3 must keep running.
            writer.write(
                pack_frame(TYPE_RST_STREAM, 0, 1, NGHTTP2_CANCEL.to_bytes(4, "big"))
            )
            await writer.drain()
            async with asyncio.timeout(2):
                while 0 not in after:
                    await asyncio.sleep(0.01)
            assert after == {0: "clean"}
            assert disconn[0].disconnected
            assert not disconn[1].disconnected
        finally:
            # Close the socket; stream 3's alive() must also unwind cleanly.
            writer.close()
            await writer.wait_closed()
        async with asyncio.timeout(2):
            while 1 not in after:
                await asyncio.sleep(0.01)
        assert after[1] == "clean"
        assert disconn[1].disconnected
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_get_route_answers_head_without_a_body() -> None:
    app = App()

    async def page(_c, w) -> None:
        w.text("hello world")

    app.add(Route("GET /"), page)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-I",
            f"http://127.0.0.1:{port}/",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("HTTP/2 200")
        assert "content-length: 11" in result.stdout
        assert "hello world" not in result.stdout
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_head_on_sse_ends_at_the_headers() -> None:
    app = App()
    done = asyncio.Event()

    async def feed(c, w) -> None:
        sse = SSE(w)
        try:
            async with c.alive():
                while True:
                    sse.patch_signals({"n": 1})
                    await asyncio.sleep(0.01)
        finally:
            done.set()

    app.add(Route("GET /feed"), feed)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-I",
            f"http://127.0.0.1:{port}/feed",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("HTTP/2 200")
        assert "text/event-stream" in result.stdout
        await asyncio.wait_for(done.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_multipart_upload_streams(tmp_path) -> None:
    from stario.forms import read_form

    payload = bytes(range(256)) * 2000
    photo = tmp_path / "photo.bin"
    photo.write_bytes(payload)
    app = App()

    async def report(c, w) -> None:
        with await read_form(c.req, max_size=1_000_000) as form:
            upload = form.file("photo")
            assert upload is not None
            data = await upload.read()
            w.text(f"{form.get('name')}|{upload.filename}|{data == payload}")

    app.add(Route("POST /"), report)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_body_bytes=1000),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-F",
            "name=Mruczek",
            "-F",
            f"photo=@{photo};filename=kot.jpg",
            f"http://127.0.0.1:{port}/",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "Mruczek|kot.jpg|True"
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_body_overflow_during_read_is_the_handler_413() -> None:
    """A running handler answers its own 413 (via RequestBodyError), so the
    protocol must not pre-empt it: that made the handler path a "response
    already started" failure on the span while the client still got 413."""
    from stario.testing.tracer import TestTracer

    app = App()

    async def read_body(c, w) -> None:
        await c.req.body()
        w.text("should not run")

    app.add(Route("POST /"), read_body)
    loop = asyncio.get_running_loop()
    port = free_port()
    with TestTracer() as tracer:
        server = await loop.create_server(
            lambda: make_protocol(loop, app, max_body_bytes=20, tracer=tracer),
            "127.0.0.1",
            port,
        )
        try:
            reader, writer, buf = await h2_handshake("127.0.0.1", port)
            try:
                writer.write(
                    pack_frame(
                        TYPE_HEADERS,
                        FLAG_END_HEADERS,
                        1,
                        encode_request(method="POST", path="/"),
                    )
                    + pack_frame(TYPE_DATA, 0, 1, b"y" * 100)
                )
                await writer.drain()
                frames, buf = await read_stream(reader, buf, 1)
                assert not has_goaway(frames)
                assert b"413" in stream_headers_blob(frames, 1)
                assert stream_data(frames, 1) == b"Request body too large"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
            await app.drain_tasks()
        spans = [s for s in tracer.finished_spans() if s.name == "POST /"]
        assert len(spans) == 1
        assert spans[0].status == "ok", spans[0].error
        assert spans[0].attributes["response.status_code"] == 413


@pytest.mark.asyncio
async def test_h2_backlog_written_under_a_small_window_arrives_in_order() -> None:
    """Writes queued while the stream window is shut are kept in order once
    the client opens the window: the unsent tail is compacted lazily."""
    app = App()
    chunks = [bytes([i % 251]) * 4096 for i in range(48)]

    async def stream(_c, w) -> None:
        w.write_headers(200)
        for chunk in chunks:
            w.write(chunk)
        w.end()

    app.add(Route("GET /stream"), stream)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, write_timeout=30.0), "127.0.0.1", port
    )
    try:
        reader, writer, buf = await _h2_handshake_window("127.0.0.1", port, 16384)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(method="GET", path="/stream"),
                )
            )
            await writer.drain()
            frames, buf, _eof = await _collect(reader, buf, 0.3)
            first = stream_data(frames, 1)
            assert 0 < len(first) <= 16384
            # Open the window in two steps so the server sends from a buffer
            # whose sent prefix is first below, then above, the compaction mark.
            received = first
            for increment in (16384, 1 << 20):
                writer.write(
                    pack_frame(TYPE_WINDOW_UPDATE, 0, 1, increment.to_bytes(4, "big"))
                    + pack_frame(TYPE_WINDOW_UPDATE, 0, 0, increment.to_bytes(4, "big"))
                )
                await writer.drain()
                frames, buf, _eof = await _collect(reader, buf, 0.3)
                received += stream_data(frames, 1)
            assert received == b"".join(chunks)
            assert any(
                f.type == TYPE_DATA and f.stream_id == 1 and (f.flags & FLAG_END_STREAM)
                for f in frames
            )
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()
