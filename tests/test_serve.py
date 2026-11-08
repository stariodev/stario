"""Tests for `stario.serve` and `Server` construction defaults."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
from io import StringIO
from typing import Any

import pytest

from stario import App, Route
from stario.http.config import ServerConfig
from stario.http.server import Server, serve
from stario.telemetry.json import JsonTracer
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.tty import TTYTracer


async def bootstrap(app: Any, span: Any):
    yield


def test_server_uses_config_defaults() -> None:
    server = Server(bootstrap, NoOpTracer())
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 8000
    assert server.config.unix_socket is None


async def test_serve_reads_env_when_config_omitted(
    monkeypatch: Any,
) -> None:
    captured: list[ServerConfig] = []

    class FakeServer:
        def __init__(
            self, bootstrap: object, tracer: object, *, config: ServerConfig
        ) -> None:
            captured.append(config)

        async def serve(self) -> None:
            return None

    monkeypatch.setenv("STARIO_PORT", "8123")
    monkeypatch.setenv("STARIO_HOST", "0.0.0.0")
    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap)
    assert captured[0].port == 8123
    assert captured[0].host == "0.0.0.0"


async def test_serve_config_ignores_env(monkeypatch: Any) -> None:
    captured: list[ServerConfig] = []
    cfg = ServerConfig(port=9001)

    class FakeServer:
        def __init__(
            self, bootstrap: object, tracer: object, *, config: ServerConfig
        ) -> None:
            captured.append(config)

        async def serve(self) -> None:
            return None

    monkeypatch.setenv("STARIO_PORT", "8123")
    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap, config=cfg)
    assert captured[0] is cfg
    assert captured[0].port == 9001


def test_server_config_does_not_read_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("STARIO_PORT", "9000")
    monkeypatch.setenv("STARIO_HOST", "0.0.0.0")

    server = Server(bootstrap, NoOpTracer(), config=ServerConfig(port=8123))
    assert server.config.port == 8123
    assert server.config.host == "127.0.0.1"


async def test_serve_creates_default_tty_tracer(
    monkeypatch: Any,
) -> None:
    captured: list[object] = []

    class FakeServer:
        def __init__(self, bootstrap: object, tracer: object, **kwargs: object) -> None:
            captured.append(tracer)

        async def serve(self) -> None:
            return None

    monkeypatch.setenv("STARIO_TRACER", "tty")
    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap)
    assert isinstance(captured[0], TTYTracer)


async def test_serve_enters_and_exits_default_tracer(
    monkeypatch: Any,
) -> None:
    events: list[str] = []

    class Probe:
        def __enter__(self) -> Probe:
            events.append("enter")
            return self

        def __exit__(self, *args: object) -> None:
            events.append("exit")

        def create(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("create should not run in this test")

        def on_end(self, span: object) -> None:
            return None

        def stats(self) -> None:
            return None

    def probe_factory() -> Probe:
        return Probe()

    class FakeServer:
        def __init__(self, bootstrap: object, tracer: object, **kwargs: object) -> None:
            assert events == ["enter"]

        async def serve(self) -> None:
            assert events == ["enter"]

    monkeypatch.setattr("stario.http.server.tracer_from_env", probe_factory)
    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap)
    assert events == ["enter", "exit"]


async def test_serve_creates_default_json_tracer(
    monkeypatch: Any,
) -> None:
    captured: list[object] = []

    class FakeServer:
        def __init__(self, bootstrap: object, tracer: object, **kwargs: object) -> None:
            captured.append(tracer)

        async def serve(self) -> None:
            return None

    monkeypatch.setenv("STARIO_TRACER", "json")
    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap)
    assert isinstance(captured[0], JsonTracer)


@pytest.fixture
def short_socket_path():
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no Unix domain sockets on this platform")
    path = os.path.join(
        tempfile.gettempdir(), f"stario-serve-{os.getpid()}-{os.urandom(3).hex()}.sock"
    )
    yield path
    if os.path.exists(path):
        os.unlink(path)


async def test_serve_then_caller_continues(short_socket_path: str) -> None:
    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(c: Any, w: Any) -> None:
            w.text("hello")

        app.add(Route("GET", "/"), hello)
        yield

    tracer = JsonTracer(StringIO())
    with tracer:
        task = asyncio.create_task(
            serve(
                serve_bootstrap,
                tracer=tracer,
                config=ServerConfig(
                    unix_socket=short_socket_path,
                    graceful_shutdown_timeout=0.5,
                ),
            )
        )
        try:
            reader, writer = await _connect_with_retry(short_socket_path)
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            status = int(head.split(b" ", 2)[1])
            body = b""
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    body = await reader.readexactly(int(line.split(b":", 1)[1]))
            writer.close()

            assert status == 200
            assert body == b"hello"
        finally:
            apps[0].signal_shutdown()
            async with asyncio.timeout(2.0):
                await task

        # Caller still owns the tracer after the server stops.
        span = tracer.create("after.serve")
        span.start()
        span.end()

    assert not os.path.exists(short_socket_path)


async def _connect_with_retry(
    path: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    async with asyncio.timeout(2.0):
        while True:
            try:
                return await asyncio.open_unix_connection(path)
            except (ConnectionRefusedError, FileNotFoundError):
                await asyncio.sleep(0.005)


def test_package_root_exports_serve() -> None:
    import stario

    assert stario.serve is serve
    assert "serve" in stario.__all__
