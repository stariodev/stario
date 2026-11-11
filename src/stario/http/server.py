"""
Runs `App` behind an asyncio listener: signal handling, bootstrap, graceful drain, then socket teardown.

Bootstrap startup completes before `start_serving`; exceptions there fail startup loudly. Transport policy
(TCP vs Unix, backlog, compression defaults) lives here so `Router`/`App` stay free of process-level concerns.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import stat
import sys
import sysconfig
import threading
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from types import FrameType
from typing import Any, Literal

from stario._env import env_bool
from stario.exceptions import StarioError
from stario.http.drain import cancel_pending, record_cancelled
from stario.telemetry.core import Span, Tracer
from stario.telemetry.env import tracer_from_env
from stario.telemetry.spans import ProxySpan
from stario_cython.protocol import Connections, HttpProtocol

from .app import App
from .bootstrap import AppRun, Bootstrap, run_app
from .compression import CompressionConfig
from .config import RequestPolicy, ServerConfig

type Connection = Any
type ProtocolMaker = Callable[
    [
        asyncio.AbstractEventLoop,
        App,
        Tracer,
        CompressionConfig,
        Connections,
        RequestPolicy,
    ],
    asyncio.Protocol,
]


def _make_http_protocol(
    loop: asyncio.AbstractEventLoop,
    app: App,
    tracer: Tracer,
    compression: CompressionConfig,
    connections: Connections,
    requests: RequestPolicy,
) -> asyncio.Protocol:
    return HttpProtocol(
        loop,
        app,
        tracer,
        compression,
        connections,
        max_header_bytes=requests.max_header_bytes,
        max_body_bytes=requests.max_body_bytes,
        header_timeout=requests.header_timeout,
        keep_alive_timeout=requests.keep_alive_timeout,
        body_timeout=requests.body_timeout,
        max_pipelined_requests=requests.max_pipelined_requests,
        write_timeout=requests.write_timeout,
    )


type SignalHandler = Callable[[int, FrameType | None], object]
type PreviousSignalHandler = signal.Handlers | int | SignalHandler | None

WINDOWS = sys.platform == "win32"
# Windows never delivers SIGTERM to a console process; Ctrl+Break (or
# GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT) from a supervisor) is SIGBREAK.
if sys.platform == "win32":
    SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK)
else:
    SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)

type EventLoopKind = Literal["asyncio", "uvloop"]

# Upper bound on the force-close loop after the graceful wait (see _drain_listener).
_FORCE_CLOSE_CAP = 1.0


# Yield to the event loop this many times while waiting for connection_made to register.
_ACCEPT_REGISTER_YIELDS = 10


def loop_implementation(
    loop: asyncio.AbstractEventLoop | None = None,
) -> EventLoopKind:
    """Classify the running (or given) loop as `asyncio` or `uvloop`."""
    if loop is None:
        loop = asyncio.get_running_loop()
    module = type(loop).__module__
    if module == "uvloop" or module.startswith("uvloop."):
        return "uvloop"
    return "asyncio"


def _same_loop_factory(
    loop: asyncio.AbstractEventLoop,
) -> Callable[[], asyncio.AbstractEventLoop]:
    """Factory for extra worker threads: new loops of `loop`'s own class.

    Works for stdlib asyncio, uvloop, and third-party loops (loopyloop,
    rloop, a `uvloop.Loop` subclass, ...), and ignores any process-wide
    loop policy. The class must be constructible without arguments, as all
    of those are.
    """
    loop_class = type(loop)

    def new_loop() -> asyncio.AbstractEventLoop:
        try:
            return loop_class()
        except TypeError as exc:
            name = f"{loop_class.__module__}.{loop_class.__qualname__}"
            raise StarioError(
                f"STARIO_THREADS>1 could not create a {name} for a worker thread",
                help_text=(
                    "Worker threads run the same loop class as the loop running "
                    "serve(), built without arguments. Use a loop class that "
                    "supports that, or set STARIO_THREADS=1."
                ),
            ) from exc

    return new_loop


def gil_is_enabled() -> bool:
    """True when this process cannot run Python bytecode in parallel."""
    check = getattr(sys, "_is_gil_enabled", None)
    if check is None:
        return True
    return bool(check())


def require_thread_workers(threads: int) -> None:
    """Refuse `threads>1` when the GIL is on, unless `STARIO_THREADS_ALLOW_GIL=1`."""
    if threads <= 1:
        return
    if not gil_is_enabled():
        return
    if env_bool("STARIO_THREADS_ALLOW_GIL", False):
        return
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        raise StarioError(
            "STARIO_THREADS>1 needs the GIL to stay disabled",
            help_text=(
                "An extension re-enabled the GIL. Use free-threaded wheels "
                "or set STARIO_THREADS=1."
            ),
        )
    raise StarioError(
        "STARIO_THREADS>1 requires free-threaded Python (3.14t)",
        help_text="Run a 3.14t interpreter or set STARIO_THREADS=1.",
    )


def allowed_cpus() -> list[int]:
    """CPUs this process may run on (cgroup / ``taskset`` aware). Empty if unknown."""
    getter = getattr(os, "sched_getaffinity", None)
    if getter is None:
        return []
    try:
        return sorted(getter(0))
    except OSError:
        return []


def pin_worker_cpu(index: int, allowed: list[int]) -> int | None:
    """Pin the calling thread to one of ``allowed``; return the CPU, or None.

    Worker ``index`` takes the ``index``-th allowed CPU, wrapping when there
    are more workers than CPUs. Linux only; elsewhere a no-op. ``allowed``
    is read once before any thread is pinned: a new thread inherits its
    parent's mask, so a pinned leader must not be the source of truth.
    A pinned loop keeps its caches and its ``SO_REUSEPORT`` connections on
    one core instead of migrating between them.
    """
    setter = getattr(os, "sched_setaffinity", None)
    if setter is None or not allowed:
        return None
    cpu = allowed[index % len(allowed)]
    try:
        setter(0, {cpu})
    except OSError:
        return None
    return cpu


_tcp_reuseport_ok: bool | None = None
_unix_reuseport_ok: bool | None = None


def reuseport_supported(*, unix: bool = False) -> bool:
    """True when this kernel can bind two SOCK_STREAM sockets with SO_REUSEPORT."""
    flag = getattr(socket, "SO_REUSEPORT", None)
    if flag is None or sys.platform == "win32":
        return False
    if unix:
        return _probe_unix_reuseport(flag)
    return _probe_tcp_reuseport(flag)


def _probe_tcp_reuseport(flag: int) -> bool:
    global _tcp_reuseport_ok
    if _tcp_reuseport_ok is not None:
        return _tcp_reuseport_ok
    first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    second: socket.socket | None = None
    try:
        first.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        first.setsockopt(socket.SOL_SOCKET, flag, 1)
        first.bind(("127.0.0.1", 0))
        port = int(first.getsockname()[1])
        first.listen(1)
        second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        second.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        second.setsockopt(socket.SOL_SOCKET, flag, 1)
        second.bind(("127.0.0.1", port))
        second.listen(1)
        _tcp_reuseport_ok = True
    except OSError:
        _tcp_reuseport_ok = False
    finally:
        first.close()
        if second is not None:
            second.close()
    return _tcp_reuseport_ok


def _probe_unix_reuseport(flag: int) -> bool:
    global _unix_reuseport_ok
    if _unix_reuseport_ok is not None:
        return _unix_reuseport_ok
    path = os.path.join(
        os.getenv("TMPDIR", "/tmp"), f"stario-reuseport-{os.getpid()}.sock"
    )
    first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    second: socket.socket | None = None
    try:
        first.setsockopt(socket.SOL_SOCKET, flag, 1)
        first.bind(path)
        first.listen(1)
        second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second.setsockopt(socket.SOL_SOCKET, flag, 1)
        second.bind(path)
        second.listen(1)
        _unix_reuseport_ok = True
    except OSError:
        _unix_reuseport_ok = False
    finally:
        first.close()
        if second is not None:
            second.close()
        with suppress(FileNotFoundError, OSError):
            os.unlink(path)
    return _unix_reuseport_ok


class _LoopWorker:
    """One extra OS thread: its own loop, SO_REUSEPORT listener, and connections."""

    def __init__(
        self,
        *,
        index: int,
        app: App,
        server: Server,
        loop_factory: Callable[[], asyncio.AbstractEventLoop],
        connections: Connections,
    ) -> None:
        self.index = index
        self.app = app
        self.server = server
        self.loop_factory = loop_factory
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_kind: EventLoopKind | None = None
        self.connections = connections
        self.listen_sock: socket.socket | None = None
        self.cpu: int | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.cancelled: list[str] = []
        self.thread = threading.Thread(
            target=self._run,
            name=f"stario-worker-{index}",
        )

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def _run(self) -> None:
        try:
            if self.server.config.pin_threads:
                self.cpu = pin_worker_cpu(self.index, self.server.cpus)
            with asyncio.Runner(loop_factory=self.loop_factory) as runner:
                runner.run(self.server.run_reuseport_worker(self))
        except BaseException as exc:
            self.error = exc
            with suppress(Exception):
                self.app.signal_shutdown()
        finally:
            self.ready.set()


async def serve(
    bootstrap: Bootstrap,
    *,
    config: ServerConfig | None = None,
    tracer: Tracer | None = None,
) -> None:
    """Serve HTTP until shutdown on the running event loop.

    Start a loop with the implementation you want::

        asyncio.run(serve(bootstrap))
        uvloop.run(serve(bootstrap))

    When `config=` is omitted, listen settings come from the `STARIO_*`
    environment variables (see `ServerConfig.from_env`). When it is passed,
    it is used as-is — environment variables are not merged in.

    A tracer you pass in must already be entered and is not closed here.
    When omitted, `STARIO_TRACER` is read via `tracer_from_env()` and the
    tracer is scoped to this call.
    """
    resolved = config if config is not None else ServerConfig.from_env()
    if tracer is None:
        with tracer_from_env() as env_tracer:
            await Server(bootstrap, env_tracer, config=resolved).serve()
        return
    await Server(bootstrap, tracer, config=resolved).serve()


class Server:
    """Binds a listener, runs bootstrap on a fresh app, serves until SIGINT/SIGTERM, then drains.

    One instance, one `serve()` — create a new `Server` to restart.

    Typical embedding is `await stario.serve(bootstrap)` or
    `await Server(bootstrap, tracer, config=cfg).serve()` inside `asyncio.run`.

    `STARIO_THREADS>1` is N `SO_REUSEPORT` listeners (thread 0 included).
    Without kernel support the server stays single-threaded.
    """

    def __init__(
        self,
        bootstrap: Bootstrap,
        tracer: Tracer,
        *,
        config: ServerConfig | None = None,
        make_protocol: ProtocolMaker | None = None,
    ) -> None:
        """Configure listening, bootstrap, telemetry, and per-connection compression.

        - `bootstrap`: Async generator `(app, span)` with a single `yield`.
        - `tracer`: Telemetry backend. Must already be entered; not closed here.
        - `config`: Listen address, limits, compression, and shutdown policy.
        - `make_protocol`: Optional factory; default is the Cython HTTP protocol.
        """
        self.bootstrap = bootstrap
        self.config = config if config is not None else ServerConfig()
        self.tracer = tracer
        self.make_protocol = make_protocol

        self._used = False
        self._urgent_drain = False
        self._thread_count = 1
        self._leader_sock: socket.socket | None = None
        # Allowed CPUs, read before any thread is pinned (see pin_worker_cpu).
        self.cpus: list[int] = []

    def _new_connections(self) -> Connections:
        """One loop's registry and clock; the process cap is split across loops.

        The registry ticks itself once a second from a ``call_later`` handle
        while it holds a connection, sweeping timeouts in C; the ``Date``
        header comes from its per-second cache.
        """
        limit = self.config.max_connections
        if limit is None:
            return Connections()
        return Connections(max_connections=-(-limit // max(self._thread_count, 1)))

    async def serve(self) -> None:
        """Run until SIGINT/SIGTERM (or fatal error); requires a running event loop.

        The tracer must already be entered. This method does not enter or exit it.
        """
        if self._used:
            raise StarioError(
                "Server instance already used",
                help_text="Create a new Server instance to run again.",
            )
        self._used = True

        self._thread_count = self._effective_threads()
        if self._thread_count > 1:
            require_thread_workers(self._thread_count)

        async with run_app(self.bootstrap, self.tracer) as run:
            app = run.app
            self._record_startup_attrs(run.span)
            # Signal handlers stay active through the shutdown span; see _signal_handlers.
            with self._signal_handlers(app):
                if self._thread_count > 1:
                    await self._serve_reuseport(run)
                else:
                    with self._listen_socket() as listen_sock:
                        async with self._listener(listen_sock, run):
                            # Blocks until a signal (or test code) completes app.shutdown.
                            await app.shutdown

    async def _create_listener(
        self,
        listen_sock: socket.socket | None,
        app: App,
        connections: Connections,
        *,
        reuse_port: bool = False,
    ) -> asyncio.Server:
        loop = asyncio.get_running_loop()
        make_protocol = self.make_protocol

        def protocol_factory() -> asyncio.Protocol:
            factory = (
                make_protocol if make_protocol is not None else _make_http_protocol
            )
            return factory(
                loop,
                app,
                self.tracer,
                self.config.compression,
                connections,
                self.config.requests,
            )

        tls: dict[str, Any] = {}
        if self.config.ssl is not None:
            # A TLS handshake is part of sending the request head; asyncio's
            # default would let a silent client hold the socket for 60s.
            tls = {
                "ssl": self.config.ssl,
                "ssl_handshake_timeout": self.config.requests.header_timeout,
            }
        if listen_sock is not None:
            if listen_sock.family == socket.AF_UNIX:
                return await loop.create_unix_server(
                    protocol_factory, sock=listen_sock, **tls
                )
            return await loop.create_server(protocol_factory, sock=listen_sock, **tls)
        return await loop.create_server(
            protocol_factory,
            self.config.host,
            self.config.port,
            backlog=self.config.backlog,
            reuse_address=self._reuse_addr(),
            reuse_port=reuse_port,
            **tls,
        )

    async def _drain_listener(
        self,
        server: asyncio.Server,
        app: App,
        connections: Connections,
        span: ProxySpan | None = None,
    ) -> list[str]:
        """Stop accepting, drain in-flight work, then tear down transports and tasks.

        Shutdown proceeds in ordered phases:

          0. Idle keep-alive sockets (no in-flight handler) close immediately.
          1. Graceful wait — let handlers finish and remaining connections close.
          2. Force-close loop — cap `min(timeout, _FORCE_CLOSE_CAP)` for stuck transports.

        Total wall time can exceed the config value by up to `_FORCE_CLOSE_CAP` seconds.
        Returns the names of the cancelled `app.tasks`; a worker thread has no
        span of its own and hands them to thread 0.
        """
        loop = asyncio.get_running_loop()

        # --- stop accepting ---------------------------------------------------
        server.close()

        # connection_made may not have run yet; yield so open count is accurate.
        for _ in range(_ACCEPT_REGISTER_YIELDS):
            if connections:
                break
            await asyncio.sleep(0)

        open_connections = len(connections)

        # Idle keep-alive sockets are not tied to app.tasks; close them now.
        idle_closed = sum(
            1 for protocol in list(connections) if protocol.close_if_idle()
        )
        if idle_closed:
            await asyncio.sleep(0)

        # --- phase 1: graceful wait (full graceful_shutdown_timeout) --------
        await self._wait_for_managed_work_to_drain(connections, app.tasks)

        # --- phase 2: force-close stuck transports ----------------------------
        force_closed = await self._force_close_open_transports(connections)
        force_close_budget = min(
            max(self.config.graceful_shutdown_timeout, 0.0), _FORCE_CLOSE_CAP
        )
        close_deadline = loop.time() + force_close_budget
        while connections and not self._urgent_drain and loop.time() < close_deadline:
            force_closed += await self._force_close_open_transports(connections)
            await asyncio.sleep(0)

        # A close()d transport flushes forever against a peer that stopped
        # reading; wait_closed() would hang on it. abort() drops the socket.
        aborted = 0
        for protocol in list(connections):
            transport = protocol.transport
            if transport is not None:
                transport.abort()
                aborted += 1
        if aborted:
            await asyncio.sleep(0)

        # --- phase 3: cancel orphaned app.create_task work ------------------
        cancelled = await cancel_pending(app.tasks)

        # Protocols with transport=None never leave the set; count for telemetry.
        stale_connections = len(connections)

        if span is not None:
            span.attrs(
                {
                    "server.shutdown.open_connections": open_connections,
                    "server.shutdown.urgent": self._urgent_drain,
                    "server.shutdown.idle_closed": idle_closed,
                    "server.shutdown.force_closed": force_closed,
                    "server.shutdown.aborted": aborted,
                    "server.shutdown.stale_connections": stale_connections,
                    "server.shutdown.cancelled_tasks": len(cancelled),
                }
            )
            record_cancelled(span, cancelled)
            span.event("server.shutdown.closed")

        await server.wait_closed()
        return cancelled

    async def _wait_for_managed_work_to_drain(
        self,
        connections: Connections,
        tasks: set[asyncio.Task[Any]],
    ) -> None:
        # Wait until no open connections and no pending app.create_task work, or timeout.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self.config.graceful_shutdown_timeout, 0.0)

        while not self._urgent_drain and loop.time() < deadline:
            pending = self._pending_tasks(tasks)
            if not connections and not pending:
                return

            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            timeout = min(0.05, remaining)

            if pending:
                await asyncio.wait(
                    pending,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(timeout)

    async def _force_close_open_transports(self, connections: Connections) -> int:
        transports = [
            protocol.transport
            for protocol in connections
            if protocol.transport and not protocol.transport.is_closing()
        ]
        for transport in transports:
            transport.close()
        if transports:
            # Let connection_lost run so the shared connections set updates.
            await asyncio.sleep(0)
        return len(transports)

    def _pending_tasks(self, tasks: set[asyncio.Task[Any]]) -> list[asyncio.Task[Any]]:
        return [task for task in tasks if not task.done()]

    def _reuse_addr(self) -> bool:
        """`reuse_addr`, except on Windows, where SO_REUSEADDR does not mean
        "rebind past TIME_WAIT" but "let another socket bind this port too"."""
        return self.config.reuse_addr and not WINDOWS

    def _effective_threads(self) -> int:
        """`STARIO_THREADS` if SO_REUSEPORT can share the listen address, else 1."""
        requested = self.config.threads
        if requested <= 1:
            return 1
        unix = self.config.unix_socket is not None
        if reuseport_supported(unix=unix):
            return requested
        return 1

    def _bind_reuseport(self, *, leader: bool) -> socket.socket:
        if self.config.unix_socket is not None:
            return self._bind_unix_reuseport(leader=leader)
        return self._bind_tcp_reuseport()

    def _bind_tcp_reuseport(self) -> socket.socket:
        """One TCP listener in the SO_REUSEPORT group."""
        flag = getattr(socket, "SO_REUSEPORT", None)
        if flag is None:
            raise StarioError(
                "SO_REUSEPORT is not available",
                help_text="Set STARIO_THREADS=1.",
            )
        infos = socket.getaddrinfo(
            self.config.host,
            self.config.port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            flags=socket.AI_PASSIVE,
        )
        if not infos:
            raise StarioError(
                f"could not resolve listen address {self.config.host!r}",
                help_text="Set STARIO_HOST to a bindable address.",
            )
        family, socktype, proto, _canon, sockaddr = infos[0]
        sock = socket.socket(family, socktype, proto)
        try:
            if self.config.reuse_addr:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, flag, 1)
            sock.bind(sockaddr)
            sock.listen(self.config.backlog)
            sock.setblocking(False)
            return sock
        except Exception:
            sock.close()
            raise

    def _bind_unix_reuseport(self, *, leader: bool) -> socket.socket:
        """Join (or create) the Unix SO_REUSEPORT group on `unix_socket`."""
        path = self.config.unix_socket
        flag = getattr(socket, "SO_REUSEPORT", None)
        if path is None or flag is None:
            raise StarioError(
                "Unix SO_REUSEPORT is not available",
                help_text="Set STARIO_THREADS=1 or listen on TCP.",
            )
        if leader and os.path.exists(path):
            st_mode = os.stat(path).st_mode
            if stat.S_ISSOCK(st_mode):
                os.unlink(path)
            else:
                raise StarioError(
                    f"Unix socket path exists and is not a socket: {path}",
                    help_text="Remove the file or choose a different STARIO_UNIX_SOCKET path.",
                )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, flag, 1)
            sock.bind(path)
            if leader:
                os.chmod(path, self.config.unix_socket_mode)
            sock.listen(self.config.backlog)
            sock.setblocking(False)
            return sock
        except Exception:
            sock.close()
            if leader:
                with suppress(FileNotFoundError, OSError):
                    os.unlink(path)
            raise

    def _unlink_unix_listener(self, file_id: tuple[int, int] | None) -> None:
        path = self.config.unix_socket
        if path is None or file_id is None:
            return
        try:
            current = os.stat(path)
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(current.st_mode)
            and (current.st_dev, current.st_ino) == file_id
        ):
            with suppress(FileNotFoundError, OSError):
                os.unlink(path)

    @contextmanager
    def _listen_socket(self) -> Generator[socket.socket | None]:
        """N=1 listen socket. `None` = TCP via `create_server`."""
        if self.config.unix_socket is not None:
            with self._unix_listen_socket() as sock:
                yield sock
            return
        yield None

    @contextmanager
    def _signal_handlers(
        self,
        app: App,
    ) -> Generator[None]:
        """Install SIGINT/SIGTERM (and Windows SIGBREAK) handlers for graceful shutdown.

        Contract:
          - 1st signal → `app.signal_shutdown()` and start drain
          - 2nd signal → set `_urgent_drain` (skip remaining graceful wait)
          - after shutdown started → `SIG_IGN` until exit so extra signals
            during tracer flush do not become `KeyboardInterrupt`
        """
        loop = asyncio.get_running_loop()

        def on_signal() -> None:
            if app.shutting_down:
                self._urgent_drain = True
                return
            app.signal_shutdown()

        previous_handlers: dict[signal.Signals, PreviousSignalHandler] = {}
        for sig in SHUTDOWN_SIGNALS:
            try:
                previous_handlers[sig] = signal.getsignal(sig)

                def _on_signal(_signum: int, _frame: FrameType | None) -> None:
                    loop.call_soon_threadsafe(on_signal)

                signal.signal(sig, _on_signal)
            except (RuntimeError, ValueError):
                continue

        try:
            yield
        finally:
            for sig, previous in previous_handlers.items():
                try:
                    if app.shutting_down:
                        signal.signal(sig, signal.SIG_IGN)
                    else:
                        signal.signal(sig, previous)
                except (RuntimeError, ValueError):
                    continue

    @contextmanager
    def _unix_listen_socket(self) -> Generator[socket.socket | None]:
        """Bind a Unix listen socket, or yield `None` for TCP listen."""
        path = self.config.unix_socket
        if path is None:
            yield None
            return
        if not hasattr(socket, "AF_UNIX"):
            raise StarioError(
                "Unix domain sockets are not available on this platform",
                help_text="Pass unix_socket=None or use host and port.",
            )

        if os.path.exists(path):
            st_mode = os.stat(path).st_mode
            if stat.S_ISSOCK(st_mode):
                os.unlink(path)
            else:
                raise StarioError(
                    f"Unix socket path exists and is not a socket: {path}",
                    help_text="Remove the file or choose a different STARIO_UNIX_SOCKET path.",
                )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        bound_file_id: tuple[int, int] | None = None
        try:
            sock.bind(path)
            bound_stat = os.stat(path)
            bound_file_id = (
                bound_stat.st_dev,
                bound_stat.st_ino,
            )  # unlink only our inode
            os.chmod(path, self.config.unix_socket_mode)
            sock.listen(self.config.backlog)
            yield sock
        finally:
            sock.close()
            if bound_file_id is not None:
                try:
                    current_stat = os.stat(path)
                except FileNotFoundError:
                    pass
                else:
                    # Skip unlink if another process rebound the path after we closed.
                    if (
                        stat.S_ISSOCK(current_stat.st_mode)
                        and (current_stat.st_dev, current_stat.st_ino) == bound_file_id
                    ):
                        os.unlink(path)

    @asynccontextmanager
    async def _listener(
        self,
        listen_sock: socket.socket | None,
        run: AppRun,
    ) -> AsyncGenerator[asyncio.Server]:
        """Bind on enter; drain in-flight work on exit."""
        app = run.app
        connections = self._new_connections()
        listener = await self._create_listener(listen_sock, app, connections)

        # Startup span ends once we are listening; shutdown span opens on exit.
        run.span.attr("server.listening", True)
        run.end_startup()
        try:
            yield listener
        except BaseException:
            run.begin_shutdown("runtime_failure")
            raise
        else:
            run.begin_shutdown("expected_stop")  # signal or app.shutdown
        finally:
            await self._drain_listener(listener, app, connections, run.span)

    async def run_reuseport_worker(self, worker: _LoopWorker) -> None:
        """Extra thread: bind SO_REUSEPORT, serve until shutdown, then drain."""
        loop = asyncio.get_running_loop()
        kind = loop_implementation(loop)
        worker.loop = loop
        worker.loop_kind = kind
        worker.app.attach_loop(loop)
        unix = self.config.unix_socket is not None
        sock = self._bind_reuseport(leader=False) if unix else None
        try:
            listener = await self._create_listener(
                sock,
                worker.app,
                worker.connections,
                reuse_port=not unix,
            )
            worker.listen_sock = sock
            sock = None
            worker.ready.set()
            try:
                await worker.app.shutdown
            finally:
                worker.cancelled = await self._drain_listener(
                    listener, worker.app, worker.connections
                )
        finally:
            if sock is not None:
                sock.close()

    async def _serve_reuseport(self, run: AppRun) -> None:
        """Thread 0 listens and orchestrates; extras are the same kind of server."""
        app = run.app
        span = run.span
        # Extra workers create the same loop class as the loop running serve().
        kind = loop_implementation()
        loop_factory = _same_loop_factory(asyncio.get_running_loop())
        extras = [
            _LoopWorker(
                index=index,
                app=app,
                server=self,
                loop_factory=loop_factory,
                connections=self._new_connections(),
            )
            for index in range(1, self._thread_count)
        ]
        if self.config.pin_threads:
            # Thread 0 is a full worker too; it takes CPU slot 0. Read the
            # mask first: the extra threads inherit whatever is set here.
            self.cpus = allowed_cpus()
            leader_cpu = pin_worker_cpu(0, self.cpus)
            span.attr("server.pin_threads", leader_cpu is not None)
        unix_socket = self.config.unix_socket
        unix = unix_socket is not None
        sock = self._bind_reuseport(leader=True) if unix else None
        unix_file_id: tuple[int, int] | None = None
        if unix_socket is not None:
            bound = os.stat(unix_socket)
            unix_file_id = (bound.st_dev, bound.st_ino)
        connections = self._new_connections()
        join_timeout = (
            max(self.config.graceful_shutdown_timeout, 0.0) + _FORCE_CLOSE_CAP + 1.0
        )
        cancelled: list[str] = []
        try:
            listener = await self._create_listener(
                sock,
                app,
                connections,
                reuse_port=not unix,
            )
            self._leader_sock = sock
            sock = None
            for worker in extras:
                worker.start()
            try:
                for worker in extras:
                    started = await asyncio.to_thread(worker.ready.wait, 5.0)
                    if not started:
                        raise StarioError(
                            f"worker {worker.index} failed to start",
                            help_text="Check worker thread logs.",
                        )
                    if worker.error is not None:
                        raise worker.error
                span.attr("server.worker_event_loop", kind)
                span.attr("server.listening", True)
                run.end_startup()
                try:
                    await app.shutdown
                except BaseException:
                    run.begin_shutdown("runtime_failure")
                    raise
                else:
                    run.begin_shutdown("expected_stop")
                finally:
                    cancelled = await self._drain_listener(
                        listener, app, connections, run.span
                    )
            finally:
                app.signal_shutdown()
                for worker in extras:
                    await asyncio.to_thread(worker.join, join_timeout)
                # Workers drain on their own loops; thread 0 owns the span.
                if any(worker.cancelled for worker in extras):
                    for worker in extras:
                        record_cancelled(
                            span, worker.cancelled, {"server.thread": worker.index}
                        )
                    span.attr(
                        "server.shutdown.cancelled_tasks",
                        len(cancelled)
                        + sum(len(worker.cancelled) for worker in extras),
                    )
                errors = [worker.error for worker in extras if worker.error is not None]
                if errors:
                    raise errors[0]
        except BaseException:
            app.signal_shutdown()
            for worker in extras:
                await asyncio.to_thread(worker.join, min(join_timeout, 1.0))
            raise
        finally:
            if sock is not None:
                sock.close()
            self._unlink_unix_listener(unix_file_id)

    def _record_startup_attrs(self, span: Span) -> None:
        attrs: dict[str, str | int | float] = {
            "server.backlog": self.config.backlog,
            "server.graceful_shutdown_timeout": self.config.graceful_shutdown_timeout,
            "server.compression.min_size": self.config.compression.min_size,
            "server.compression.zstd_level": self.config.compression.zstd_level,
            "server.compression.brotli_level": self.config.compression.brotli_level,
            "server.compression.gzip_level": self.config.compression.gzip_level,
            "server.timeout.request_header": self.config.requests.header_timeout,
            "server.timeout.request_body": self.config.requests.body_timeout,
            "server.timeout.keep_alive": self.config.requests.keep_alive_timeout,
            "server.timeout.response_write": self.config.requests.write_timeout,
            "server.event_loop": loop_implementation(),
            "server.threads": self._thread_count,
            "server.tls": self.config.ssl is not None,
        }
        # More than one thread always means SO_REUSEPORT balancing, so the
        # thread count says it all; the request only matters when it was cut.
        if self.config.threads != self._thread_count:
            attrs["server.threads_requested"] = self.config.threads
        if self.config.compression.zstd_window_log is not None:
            attrs["server.compression.zstd_window_log"] = (
                self.config.compression.zstd_window_log
            )
        if self.config.compression.brotli_window_log is not None:
            attrs["server.compression.brotli_window_log"] = (
                self.config.compression.brotli_window_log
            )
        if self.config.compression.gzip_window_bits is not None:
            attrs["server.compression.gzip_window_bits"] = (
                self.config.compression.gzip_window_bits
            )
        if self.config.compression.stream_window_log is not None:
            attrs["server.compression.stream_window_log"] = (
                self.config.compression.stream_window_log
            )
        if self.config.unix_socket:
            attrs["server.listen_mode"] = "unix_socket"
            attrs["server.unix_socket"] = self.config.unix_socket
            attrs["server.unix_socket_mode"] = oct(self.config.unix_socket_mode)
        else:
            attrs["server.listen_mode"] = "tcp"
            attrs["server.host"] = self.config.host
            attrs["server.port"] = self.config.port
            attrs["server.reuse_addr"] = self._reuse_addr()
        attrs["server.limits.max_request_header_bytes"] = (
            self.config.requests.max_header_bytes
        )
        attrs["server.limits.max_request_body_bytes"] = (
            self.config.requests.max_body_bytes
        )
        if self.config.max_connections is not None:
            attrs["server.limits.max_connections"] = self.config.max_connections
        span.attrs(attrs)
