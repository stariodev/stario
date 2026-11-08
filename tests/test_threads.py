"""Worker-thread runtime: shutdown fan-out, leader-loop mirroring, STARIO_THREADS."""

import asyncio
import gc
import json
import os
import queue
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Coroutine
from io import StringIO
from typing import Any

import pytest

from stario import App, Route
from stario.exceptions import StarioError
from stario.http.config import ServerConfig
from stario.http.server import (
    Server,
    gil_is_enabled,
    loop_implementation,
    require_thread_workers,
    reuseport_supported,
)
from stario.telemetry.json import JsonTracer
from tests.test_server import _connect_with_retry, _read_http_response, _serve


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _connect_tcp(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    async with asyncio.timeout(2.0):
        while True:
            try:
                return await asyncio.open_connection(host, port)
            except OSError:
                await asyncio.sleep(0.005)


def _run_server(
    server: Server,
    runner: Callable[[Coroutine[Any, Any, None]], Any] = asyncio.run,
) -> tuple[threading.Thread, list[BaseException]]:
    """Serve on a fresh thread whose leader loop comes from `runner`."""
    errors: list[BaseException] = []

    def body() -> None:
        try:
            with server.tracer:
                runner(server.serve())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=body, name="stario-main")
    thread.start()
    return thread, errors


@pytest.fixture
def short_socket_path():
    """AF_UNIX paths are limited to ~104 bytes on macOS; pytest's tmp_path is too long."""
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no Unix domain sockets on this platform")
    path = os.path.join(
        tempfile.gettempdir(), f"stario-thr-{os.getpid()}-{os.urandom(3).hex()}.sock"
    )
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_require_thread_workers_allows_one() -> None:
    require_thread_workers(1)


def test_require_thread_workers_rejects_gil(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STARIO_THREADS_ALLOW_GIL", raising=False)
    if not gil_is_enabled():
        pytest.skip("GIL is already disabled")
    with pytest.raises(StarioError, match="STARIO_THREADS>1"):
        require_thread_workers(2)


def test_require_thread_workers_allow_gil_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    require_thread_workers(4)


def test_require_thread_workers_gil_reenabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STARIO_THREADS_ALLOW_GIL", raising=False)
    monkeypatch.setattr("stario.http.server.gil_is_enabled", lambda: True)
    monkeypatch.setattr(
        "stario.http.server.sysconfig.get_config_var",
        lambda name: 1 if name == "Py_GIL_DISABLED" else None,
    )
    with pytest.raises(StarioError, match="GIL to stay disabled"):
        require_thread_workers(2)


async def test_signal_shutdown_completes_other_loop() -> None:
    app = App()
    started = threading.Event()
    results: queue.Queue[object] = queue.Queue()

    def run_other() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def wait_shutdown() -> None:
            app.attach_loop()
            started.set()
            await app.shutdown

        try:
            loop.run_until_complete(wait_shutdown())
            results.put("ok")
        except BaseException as exc:
            results.put(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=run_other)
    thread.start()
    assert started.wait(timeout=2.0)
    app.signal_shutdown()
    thread.join(timeout=2.0)
    assert results.get(timeout=1.0) == "ok"
    assert app.shutting_down


def test_loop_implementation_classifies_running_loop() -> None:
    seen: list[str] = []

    async def probe() -> None:
        seen.append(loop_implementation())

    asyncio.run(probe(), loop_factory=asyncio.SelectorEventLoop)
    assert seen == ["asyncio"]


def test_reuseport_supported_on_tcp() -> None:
    if sys.platform == "win32":
        pytest.skip("SO_REUSEPORT is not available on Windows")
    assert reuseport_supported(unix=False) is True


def test_unix_reuseport_probe_does_not_raise() -> None:
    supported = reuseport_supported(unix=True)
    assert supported in (True, False)


@pytest.mark.asyncio
async def test_unix_threads_fall_back_to_one(
    monkeypatch: pytest.MonkeyPatch, short_socket_path: str
) -> None:
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    monkeypatch.setattr(
        "stario.http.server.reuseport_supported", lambda **_kwargs: False
    )
    output = StringIO()
    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            w.text("one-thread")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(output),
        config=ServerConfig(
            unix_socket=short_socket_path,
            graceful_shutdown_timeout=0.5,
            threads=4,
        ),
    )

    run_task = asyncio.create_task(_serve(server))
    try:
        reader, writer = await _connect_with_retry(short_socket_path)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"one-thread"
    finally:
        apps[0].signal_shutdown()
        async with asyncio.timeout(5.0):
            await run_task

    spans = [json.loads(line) for line in output.getvalue().splitlines()]
    startup = next(s for s in spans if s["name"] == "server.startup")
    assert startup["attributes"]["server.threads"] == 1
    assert startup["attributes"]["server.threads_requested"] == 4
    assert "server.listen_balance" not in startup["attributes"]


@pytest.mark.asyncio
async def test_threaded_workers_mirror_asyncio_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An asyncio leader produces asyncio worker threads."""
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    kinds: list[str] = []
    modules: list[str] = []
    apps: list[App] = []
    port = _free_tcp_port()

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            kinds.append(loop_implementation())
            modules.append(type(asyncio.get_running_loop()).__module__)
            w.text("hello-threads")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
        ),
    )

    thread, errors = _run_server(server)
    try:
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-threads"
        assert kinds == ["asyncio"]
        assert modules
        assert all(not module.startswith("uvloop") for module in modules)
    finally:
        if apps:
            apps[0].signal_shutdown()
        thread.join(5.0)
        assert errors == []


@pytest.mark.asyncio
async def test_threaded_serve_records_thread_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    output = StringIO()
    apps: list[App] = []
    port = _free_tcp_port()

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            w.text("ok")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(output),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
        ),
    )

    thread, errors = _run_server(server)
    try:
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        await _read_http_response(reader)
        writer.close()
    finally:
        if apps:
            apps[0].signal_shutdown()
        thread.join(5.0)
        assert errors == []

    spans = [json.loads(line) for line in output.getvalue().splitlines()]
    startup = next(s for s in spans if s["name"] == "server.startup")
    assert startup["attributes"]["server.threads"] == 2
    assert "server.threads_requested" not in startup["attributes"]
    assert startup["attributes"]["server.event_loop"] == "asyncio"
    assert startup["attributes"]["server.worker_event_loop"] == "asyncio"
    assert "server.listen_balance" not in startup["attributes"]


@pytest.mark.asyncio
async def test_threaded_workers_mirror_uvloop_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uvloop leader produces uvloop worker threads."""
    uvloop = pytest.importorskip("uvloop")
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    output = StringIO()
    loop_modules: list[str] = []
    kinds: list[str] = []
    apps: list[App] = []
    port = _free_tcp_port()

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            loop_modules.append(type(asyncio.get_running_loop()).__module__)
            kinds.append(loop_implementation())
            w.text("hello-uvloop")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(output),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
        ),
    )

    thread, errors = _run_server(server, runner=uvloop.run)
    try:
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-uvloop"
        assert loop_modules
        assert all(module.startswith("uvloop") for module in loop_modules)
        assert kinds == ["uvloop"]
    finally:
        if apps:
            apps[0].signal_shutdown()
        thread.join(5.0)
        assert errors == []

    spans = [json.loads(line) for line in output.getvalue().splitlines()]
    startup = next(s for s in spans if s["name"] == "server.startup")
    assert startup["attributes"]["server.event_loop"] == "uvloop"
    assert startup["attributes"]["server.worker_event_loop"] == "uvloop"


@pytest.mark.asyncio
async def test_threaded_serve_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    port = _free_tcp_port()
    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            w.text("hello-tcp")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
        ),
    )

    run_task = asyncio.create_task(_serve(server))
    try:
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-tcp"
        gc.collect()
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-tcp"
    finally:
        apps[0].signal_shutdown()
        async with asyncio.timeout(5.0):
            await run_task


def test_pin_worker_cpu_wraps_over_the_allowed_set(monkeypatch) -> None:
    from stario.http import server as server_mod

    pinned: list[set[int]] = []
    monkeypatch.setattr(
        os,
        "sched_setaffinity",
        lambda pid, cpus: pinned.append(set(cpus)),
        raising=False,
    )
    allowed = [2, 5, 7]
    assert server_mod.pin_worker_cpu(0, allowed) == 2
    assert server_mod.pin_worker_cpu(1, allowed) == 5
    assert server_mod.pin_worker_cpu(3, allowed) == 2
    assert pinned == [{2}, {5}, {2}]
    # Nothing to pin to: a no-op, never an error.
    assert server_mod.pin_worker_cpu(0, []) is None


def test_pin_worker_cpu_is_a_no_op_without_sched_setaffinity(monkeypatch) -> None:
    from stario.http import server as server_mod

    monkeypatch.delattr(os, "sched_setaffinity", raising=False)
    assert server_mod.pin_worker_cpu(0, [0, 1]) is None


def test_pin_threads_config_from_env(monkeypatch) -> None:
    from stario.http.config import server_config_from_env

    assert ServerConfig().pin_threads is False
    monkeypatch.setenv("STARIO_PIN_THREADS", "1")
    assert server_config_from_env().pin_threads is True


@pytest.mark.asyncio
async def test_threaded_shutdown_names_tasks_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workers drain on their own loops; thread 0 records their cancels."""
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    output = StringIO()
    apps: list[App] = []
    port = _free_tcp_port()
    requests = 6

    async def deaf_loop() -> None:
        while True:
            await asyncio.sleep(3600)

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def start(_c: Any, w: Any) -> None:
            app.create_task(deaf_loop(), name="deaf")
            w.text("ok")

        app.add(Route("GET", "/"), start)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(output),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.05,
            threads=2,
        ),
    )

    thread, errors = _run_server(server)
    try:
        for _ in range(requests):
            reader, writer = await _connect_tcp("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await _read_http_response(reader)
            writer.close()
    finally:
        if apps:
            apps[0].signal_shutdown()
        thread.join(5.0)
        assert errors == []

    spans = [json.loads(line) for line in output.getvalue().splitlines()]
    shutdown = next(s for s in spans if s["name"] == "server.shutdown")
    events = [
        e for e in shutdown["events"] if e["name"] == "server.shutdown.task_cancelled"
    ]
    assert shutdown["attributes"]["server.shutdown.cancelled_tasks"] == requests
    assert len(events) == requests
    assert {e["attributes"]["task.name"] for e in events} == {"deaf"}
    workers = [e for e in events if "server.thread" in e["attributes"]]
    assert all(e["attributes"]["server.thread"] == 1 for e in workers)
