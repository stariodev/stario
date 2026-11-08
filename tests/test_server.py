"""Tests for server-managed request tasks and shutdown handling."""

import asyncio
import json
import os
import signal
import socket
import stat
import sys
import tempfile
import time
from io import StringIO
from typing import Any

import pytest

import stario.http.server as server_module
from stario import App, Route
from stario.exceptions import StarioError
from stario.http.config import RequestPolicy, ServerConfig
from stario.http.server import SHUTDOWN_SIGNALS, Server
from stario.telemetry.json import JsonTracer
from tests.helpers import make_context


async def bootstrap(app: Any, span: Any):
    yield


class TestContextCreateTask:
    async def test_registers_task_and_removes_it_after_completion(self) -> None:
        loop = asyncio.get_running_loop()
        context = make_context(loop=loop)

        async def worker() -> int:
            await asyncio.sleep(0)
            return 42

        task = context.app.create_task(worker(), name="managed-worker")

        assert task in context.app.tasks
        assert task.get_name() == "managed-worker"
        assert await task == 42

        await asyncio.sleep(0)
        assert not context.app.tasks

    async def test_eager_task_completes_without_registration(self) -> None:
        context = make_context(loop=asyncio.get_running_loop())
        started = False

        async def worker() -> int:
            nonlocal started
            started = True
            return 42

        task = context.app.create_task(worker(), eager_start=True)

        assert started
        assert task.done()
        assert task.result() == 42
        assert task not in context.app.tasks

    async def test_task_construction_bypasses_loop_task_factory(self) -> None:
        loop = asyncio.get_running_loop()
        context = make_context(loop=loop)
        calls = 0
        previous = loop.get_task_factory()

        def factory(loop, coro, **kwargs):
            nonlocal calls
            calls += 1
            return asyncio.Task(coro, loop=loop, **kwargs)

        async def worker() -> int:
            await asyncio.sleep(0)
            return 42

        loop.set_task_factory(factory)
        try:
            task = context.app.create_task(worker(), name="direct-task")
        finally:
            loop.set_task_factory(previous)

        assert calls == 0
        assert task.get_loop() is loop
        assert task.get_name() == "direct-task"
        assert await task == 42


class TestServerConstructorValidation:
    def test_header_limit_below_minimum_raises(self) -> None:
        with pytest.raises(StarioError, match="max_header_bytes"):
            make_server_with(max_header_bytes=255)

    def test_body_limit_below_minimum_raises(self) -> None:
        with pytest.raises(StarioError, match="max_body_bytes"):
            make_server_with(max_body_bytes=0)


def make_server_with(**kwargs: Any) -> Server:
    request_fields = {
        "max_header_bytes",
        "max_body_bytes",
        "header_timeout",
        "body_timeout",
        "keep_alive_timeout",
    }
    requests = RequestPolicy(
        **{key: value for key, value in kwargs.items() if key in request_fields}
    )
    server_kwargs = {
        key: value for key, value in kwargs.items() if key not in request_fields
    }
    if any(key in kwargs for key in request_fields):
        server_kwargs["requests"] = requests
    return Server(
        bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(**server_kwargs),
    )


async def _serve(server: Server) -> None:
    with server.tracer:
        await server.serve()


async def _read_http_response(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    body = b""
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            body = await reader.readexactly(int(line.split(b":", 1)[1]))
    return status, body


async def _connect_with_retry(
    path: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    async with asyncio.timeout(2.0):
        while True:
            try:
                return await asyncio.open_unix_connection(path)
            except (ConnectionRefusedError, FileNotFoundError):
                await asyncio.sleep(0.005)


class _ListenTarget:
    """Where a lifecycle test's server listens: a Unix socket where the
    platform has them, else loopback TCP (Windows), so drain runs on both."""

    def __init__(self, unix_path: str | None, port: int = 0) -> None:
        self.unix_path = unix_path
        self.port = port

    def config(self, **kwargs: Any) -> ServerConfig:
        if self.unix_path is not None:
            return ServerConfig(unix_socket=self.unix_path, **kwargs)
        return ServerConfig(host="127.0.0.1", port=self.port, **kwargs)

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.unix_path is not None:
            return await _connect_with_retry(self.unix_path)
        async with asyncio.timeout(10.0):
            # Windows takes ~2 s to refuse a loopback connect: wait for the
            # listener first. The probe binds and closes between awaits, so
            # it cannot hold the port while the server binds.
            while not self._port_taken():
                await asyncio.sleep(0.005)
            while True:
                try:
                    return await asyncio.open_connection("127.0.0.1", self.port)
                except ConnectionRefusedError:
                    await asyncio.sleep(0.005)

    def released(self) -> bool:
        """The listener is gone: a Unix socket path is unlinked on shutdown;
        a TCP port can be bound again (on Windows without SO_REUSEADDR, past
        the TIME_WAIT the drain leaves, as ServerConfig documents)."""
        if self.unix_path is not None:
            return not os.path.exists(self.unix_path)
        return not self._port_taken()

    def _port_taken(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if sys.platform != "win32":
                # POSIX: rebind past TIME_WAIT, still refused by a listener.
                # Windows: SO_REUSEADDR would let the probe share the port.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", self.port))
            except OSError:
                return True
            return False


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def listen_target():
    if not hasattr(socket, "AF_UNIX"):
        yield _ListenTarget(None, _free_tcp_port())
        return
    # AF_UNIX paths are limited to ~104 bytes on macOS; pytest's tmp_path is too long.
    path = os.path.join(
        tempfile.gettempdir(), f"stario-test-{os.getpid()}-{os.urandom(3).hex()}.sock"
    )
    yield _ListenTarget(path)
    if os.path.exists(path):
        os.unlink(path)


class TestServerRunLifecycle:
    async def test_run_serves_requests_and_shuts_down_gracefully(
        self, listen_target
    ) -> None:
        output = StringIO()
        tracer = JsonTracer(output)
        apps: list[App] = []

        async def serve_bootstrap(app: App, span: Any):
            apps.append(app)

            async def hello(c, w):
                w.text("hello")

            app.add(Route("GET /"), hello)
            yield

        server = Server(
            serve_bootstrap,
            tracer,
            config=listen_target.config(graceful_shutdown_timeout=0.5),
        )

        run_task = asyncio.create_task(_serve(server))
        try:
            reader, writer = await listen_target.connect()
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
            await writer.drain()
            status, body = await _read_http_response(reader)
            writer.close()

            assert status == 200
            assert body == b"hello"
        finally:
            # Trigger shutdown the same way the signal handler does.
            apps[0].signal_shutdown()
            async with asyncio.timeout(2.0):
                await run_task

        assert listen_target.released()

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        names = [s["name"] for s in spans]
        assert "server.startup" in names
        assert "GET /" in names
        request_span = next(s for s in spans if s["name"] == "GET /")
        assert request_span["attributes"]["request.method"] == "GET"
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["attributes"]["server.shutdown.trigger"] == "expected_stop"
        assert shutdown_span["status"] == "ok"

        # The instance is single-use.
        with pytest.raises(StarioError, match="already used"):
            await _serve(server)

    async def test_serve_startup_failure_marks_startup_failed_no_shutdown(
        self,
    ) -> None:
        output = StringIO()
        tracer = JsonTracer(output)

        async def failing_bootstrap(app: App, span: Any):
            raise RuntimeError("boot failed")
            yield

        server = Server(
            failing_bootstrap,
            tracer,
        )

        with pytest.raises(RuntimeError, match="boot failed"):
            await _serve(server)

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        startup = next(s for s in spans if s["name"] == "server.startup")
        assert startup["status"] == "error"
        assert startup["error"] == "boot failed"
        assert not any(s["name"] == "server.shutdown" for s in spans)

    @pytest.mark.parametrize("sig", SHUTDOWN_SIGNALS, ids=lambda sig: sig.name)
    async def test_shutdown_signal_drains_and_stops(
        self, listen_target, sig: signal.Signals
    ) -> None:
        """SIGINT and SIGTERM everywhere, SIGBREAK (Ctrl+Break) on Windows."""
        output = StringIO()
        server = Server(
            bootstrap,
            JsonTracer(output),
            config=listen_target.config(graceful_shutdown_timeout=0.5),
        )

        run_task = asyncio.create_task(_serve(server))
        _reader, writer = await listen_target.connect()
        writer.close()
        signal.raise_signal(sig)
        async with asyncio.timeout(2.0):
            await run_task

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["status"] == "ok"
        assert shutdown_span["attributes"]["server.shutdown.trigger"] == "expected_stop"

    async def test_shutdown_closes_unstarted_idle_connections(
        self, listen_target
    ) -> None:
        output = StringIO()
        tracer = JsonTracer(output)
        apps: list[App] = []

        async def capture_bootstrap(app: App, span: Any):
            apps.append(app)
            yield

        server = Server(
            capture_bootstrap,
            tracer,
            config=listen_target.config(graceful_shutdown_timeout=0.05),
        )

        run_task = asyncio.create_task(_serve(server))
        reader, writer = await listen_target.connect()
        try:
            # Idle connection: never sends a request; drain closes it immediately.
            apps[0].signal_shutdown()
            async with asyncio.timeout(2.0):
                await run_task

            assert await reader.read() == b""  # server closed the socket
        finally:
            writer.close()

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        attrs = shutdown_span["attributes"]
        assert attrs["server.shutdown.open_connections"] == 1
        assert attrs["server.shutdown.urgent"] is False
        assert attrs["server.shutdown.idle_closed"] == 1
        assert attrs["server.shutdown.force_closed"] == 0
        assert attrs["server.shutdown.stale_connections"] == 0
        assert attrs["server.shutdown.cancelled_tasks"] == 0

    async def test_shutdown_closes_idle_keep_alive_after_request(
        self, listen_target
    ) -> None:
        output = StringIO()
        tracer = JsonTracer(output)
        apps: list[App] = []

        async def serve_bootstrap(app: App, span: Any):
            apps.append(app)

            async def hello(c, w):
                w.text("hello")

            app.add(Route("GET /"), hello)
            yield

        server = Server(
            serve_bootstrap,
            tracer,
            config=listen_target.config(graceful_shutdown_timeout=0.5),
        )

        run_task = asyncio.create_task(_serve(server))
        reader, writer = await listen_target.connect()
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: keep-alive\r\n\r\n")
            await writer.drain()
            status, body = await _read_http_response(reader)
            assert status == 200
            assert body == b"hello"

            apps[0].signal_shutdown()
            async with asyncio.timeout(2.0):
                await run_task

            assert await reader.read() == b""
        finally:
            writer.close()

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["status"] == "ok"
        assert shutdown_span["attributes"]["server.shutdown.urgent"] is False
        assert shutdown_span["attributes"]["server.shutdown.idle_closed"] == 1

    async def test_second_shutdown_signal_forces_drain_without_failed_span(
        self, listen_target
    ) -> None:
        output = StringIO()
        tracer = JsonTracer(output)
        apps: list[App] = []
        handler_started = asyncio.Event()

        async def serve_bootstrap(app: App, span: Any):
            apps.append(app)

            async def slow(_c, _w):
                handler_started.set()
                await asyncio.Event().wait()

            app.add(Route("GET /"), slow)
            yield

        server = Server(
            serve_bootstrap,
            tracer,
            config=listen_target.config(graceful_shutdown_timeout=5.0),
        )

        run_task = asyncio.create_task(_serve(server))
        reader, writer = await listen_target.connect()
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
            await writer.drain()
            await handler_started.wait()

            apps[0].signal_shutdown()
            server._urgent_drain = True

            async with asyncio.timeout(2.0):
                await run_task
            assert await reader.read() == b""
        finally:
            writer.close()

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["status"] == "ok"
        assert shutdown_span["attributes"]["server.shutdown.urgent"] is True
        assert shutdown_span["attributes"]["server.shutdown.cancelled_tasks"] == 1
        (event,) = (
            e
            for e in shutdown_span["events"]
            if e["name"] == "server.shutdown.task_cancelled"
        )
        assert event["attributes"]["task.name"].endswith("<locals>.slow")

    async def test_shutdown_names_each_cancelled_task(self, listen_target) -> None:
        """A loop that ignores shutdown shows up by name on the shutdown span."""
        output = StringIO()
        tracer = JsonTracer(output)
        apps: list[App] = []

        async def deaf_loop() -> None:
            while True:
                await asyncio.sleep(60)

        async def deaf_bootstrap(app: App, span: Any):
            apps.append(app)
            app.create_task(deaf_loop())
            app.create_task(deaf_loop(), name="ticker")
            yield

        server = Server(
            deaf_bootstrap,
            tracer,
            config=listen_target.config(graceful_shutdown_timeout=0.05),
        )

        run_task = asyncio.create_task(_serve(server))
        _reader, writer = await listen_target.connect()
        writer.close()
        apps[0].signal_shutdown()
        async with asyncio.timeout(2.0):
            await run_task

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["attributes"]["server.shutdown.cancelled_tasks"] == 2
        names = sorted(
            e["attributes"]["task.name"]
            for e in shutdown_span["events"]
            if e["name"] == "server.shutdown.task_cancelled"
        )
        assert names == [
            "TestServerRunLifecycle.test_shutdown_names_each_cancelled_task.<locals>.deaf_loop",
            "ticker",
        ]

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Winsock takes a whole send into the kernel while the send buffer "
        "has room (KB214397): the 10 MiB flushes and nothing is left to abort",
    )
    async def test_shutdown_aborts_write_blocked_transport(self, listen_target) -> None:
        """A client that never reads must not stall wait_closed(): the drain
        aborts transports that close() would flush forever."""
        output = StringIO()
        tracer = JsonTracer(output)
        apps: list[App] = []

        async def serve_bootstrap(app: App, span: Any):
            apps.append(app)

            async def big(_c, w) -> None:
                w.write_headers(200)
                for _ in range(160):  # ~10 MiB, no drain
                    w.write(b"z" * 65536)
                w.end()

            app.add(Route("GET /big"), big)
            yield

        server = Server(
            serve_bootstrap,
            tracer,
            config=listen_target.config(
                requests=RequestPolicy(write_timeout=60.0),
                graceful_shutdown_timeout=0.1,
            ),
        )

        run_task = asyncio.create_task(_serve(server))
        reader, writer = await listen_target.connect()
        try:
            writer.write(b"GET /big HTTP/1.1\r\nHost: t\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(0.1)  # let the write buffer fill
            await reader.read(1)  # response bytes are flowing

            t0 = time.monotonic()
            apps[0].signal_shutdown()
            # graceful(0.1) + force cap(0.1) + abort — never write_timeout.
            async with asyncio.timeout(4.0):
                await run_task
            assert time.monotonic() - t0 < 3.0
        finally:
            writer.close()

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["attributes"]["server.shutdown.aborted"] >= 1

    async def test_shutdown_aborts_a_drain_paced_writer(self, listen_target) -> None:
        """A handler that drains between writes, to a client that stopped
        reading, is aborted by the drain too. Unlike one big write, this
        stalls on Windows as well: Winsock takes one send whole, then blocks."""
        output = StringIO()
        apps: list[App] = []

        async def serve_bootstrap(app: App, span: Any):
            apps.append(app)

            async def paced(_c, w) -> None:
                w.write_headers(200)
                for _ in range(1024):  # up to 64 MiB; stalls long before
                    w.write(b"z" * 65536)
                    await w.drain()
                w.end()

            app.add(Route("GET /paced"), paced)
            yield

        server = Server(
            serve_bootstrap,
            JsonTracer(output),
            config=listen_target.config(
                requests=RequestPolicy(write_timeout=60.0),
                graceful_shutdown_timeout=0.1,
            ),
        )

        run_task = asyncio.create_task(_serve(server))
        reader, writer = await listen_target.connect()
        try:
            writer.write(b"GET /paced HTTP/1.1\r\nHost: t\r\n\r\n")
            await writer.drain()
            await reader.read(1)  # response bytes are flowing
            await asyncio.sleep(0.3)  # the client stops reading; output stalls
            t0 = time.monotonic()
            apps[0].signal_shutdown()
            async with asyncio.timeout(4.0):
                await run_task
            assert time.monotonic() - t0 < 3.0
        finally:
            writer.close()

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["attributes"]["server.shutdown.aborted"] >= 1

    async def test_shutdown_delivers_response_to_steady_reader(
        self, listen_target
    ) -> None:
        """A close()d transport gets the whole force-close budget to flush;
        a client that keeps reading must see the complete body, not an abort."""
        output = StringIO()
        tracer = JsonTracer(output)
        apps: list[App] = []
        body_len = 4 * 1024 * 1024

        async def serve_bootstrap(app: App, span: Any):
            apps.append(app)

            async def big(_c, w) -> None:
                w.write_headers(200)
                for _ in range(body_len // 65536):
                    w.write(b"z" * 65536)
                w.end()

            app.add(Route("GET /big"), big)
            yield

        server = Server(
            serve_bootstrap,
            tracer,
            config=listen_target.config(
                requests=RequestPolicy(write_timeout=60.0),
                graceful_shutdown_timeout=0.5,
            ),
        )

        run_task = asyncio.create_task(_serve(server))
        reader, writer = await listen_target.connect()
        try:
            writer.write(b"GET /big HTTP/1.1\r\nHost: t\r\n\r\n")
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            assert b" 200 " in head

            # The client pauses so bytes are still buffered when drain begins.
            await asyncio.sleep(0.05)
            apps[0].signal_shutdown()

            received = bytearray()
            async with asyncio.timeout(4.0):
                # Steady reads: the transport flush completes inside the budget.
                while chunk := await reader.read(65536):
                    received += chunk
                await run_task
            # Chunked framing: the terminator and every payload byte arrived.
            assert received.endswith(b"0\r\n\r\n")
            assert received.count(b"z") == body_len
        finally:
            writer.close()

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["attributes"]["server.shutdown.aborted"] == 0

    async def test_shutdown_span_survives_second_sigint_after_stop_requested(
        self, listen_target
    ) -> None:
        """Mimic watch Ctrl+C: stop requested, then a second SIGINT during teardown."""
        output = StringIO()
        tracer = JsonTracer(output)
        apps: list[App] = []

        async def serve_bootstrap(app: App, span: Any):
            apps.append(app)

            async def hello(c, w):
                w.text("hello")

            app.add(Route("GET /"), hello)
            yield

        server = Server(
            serve_bootstrap,
            tracer,
            config=listen_target.config(graceful_shutdown_timeout=0.5),
        )

        run_task = asyncio.create_task(_serve(server))
        reader, writer = await listen_target.connect()
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: keep-alive\r\n\r\n")
            await writer.drain()
            await _read_http_response(reader)

            apps[0].signal_shutdown()
            await asyncio.sleep(0.01)
            signal.raise_signal(signal.SIGINT)

            async with asyncio.timeout(2.0):
                await run_task
        finally:
            writer.close()

        spans = [json.loads(line) for line in output.getvalue().splitlines()]
        shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
        assert shutdown_span["status"] == "ok"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix domain sockets")
def test_create_unix_socket_uses_hardened_default_mode() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "stario.sock")
        server = Server(
            bootstrap,
            JsonTracer(StringIO()),
            config=ServerConfig(unix_socket=path),
        )

        with server._unix_listen_socket() as sock:
            assert sock is not None
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o660


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix domain sockets")
def test_unix_socket_cleanup_only_unlinks_owned_socket() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "stario.sock")
        server = Server(
            bootstrap,
            JsonTracer(StringIO()),
            config=ServerConfig(unix_socket=path),
        )
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with server._unix_listen_socket() as sock:
                assert sock is not None
                os.unlink(path)
                replacement.bind(path)

            assert os.path.exists(path)
        finally:
            replacement.close()
            if os.path.exists(path):
                os.unlink(path)


def test_reuse_addr_is_ignored_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """SO_REUSEADDR on Windows lets a second socket bind the same port."""
    server = Server(
        bootstrap, JsonTracer(StringIO()), config=ServerConfig(reuse_addr=True)
    )
    monkeypatch.setattr(server_module, "WINDOWS", True)
    assert server._reuse_addr() is False
    monkeypatch.setattr(server_module, "WINDOWS", False)
    assert server._reuse_addr() is True


_SIGNAL_SERVER = """
import asyncio
import sys

from stario import serve
from stario.http.config import ServerConfig
from stario.telemetry.json import JsonTracer


async def bootstrap(app, span):
    yield


async def main():
    tracer = JsonTracer(sys.stdout)
    with tracer:
        config = ServerConfig(host="127.0.0.1", port=int(sys.argv[1]))
        await serve(bootstrap, config=config, tracer=tracer)


asyncio.run(main())
"""


@pytest.mark.timeout(90)
def test_os_signal_stops_a_server_process_gracefully(tmp_path: Any) -> None:
    """The real delivery path: Ctrl+Break (CTRL_BREAK_EVENT to the process
    group) on Windows, SIGTERM elsewhere, arriving while the loop is blocked
    in its selector or IOCP wait. raise_signal() in-process cannot show this."""
    import subprocess

    script = tmp_path / "serve.py"
    script.write_text(_SIGNAL_SERVER)
    port = _free_tcp_port()
    if sys.platform == "win32":
        flags, stop = subprocess.CREATE_NEW_PROCESS_GROUP, signal.CTRL_BREAK_EVENT
    else:
        flags, stop = 0, signal.SIGTERM
    proc = subprocess.Popen(
        [sys.executable, str(script), str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=flags,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, proc.communicate()[1]
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "server never listened"
                time.sleep(0.05)
        proc.send_signal(stop)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, err
    spans = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    shutdown_span = next(s for s in spans if s["name"] == "server.shutdown")
    assert shutdown_span["status"] == "ok"
