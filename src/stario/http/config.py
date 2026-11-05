"""Process-level HTTP server configuration (listen, limits, compression, shutdown).

Body and header size defaults for `RequestPolicy` are defined in `request.py`;
this module re-exports them for env wiring.
"""

from collections.abc import Callable
from pathlib import Path
from ssl import SSLContext

from stario._env import (
    env_bool,
    env_float,
    env_int,
    env_octal_mode,
    env_optional_int,
    env_optional_str,
    env_str,
)
from stario.exceptions import StarioError
from stario.http.tls import load_tls_context

from .compression import CompressionConfig, compression_config_from_env
from .request import (
    DEFAULT_BODY_TIMEOUT,
    DEFAULT_MAX_BODY_SIZE,
    DEFAULT_MAX_HEADER_BYTES,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT = 5.0
DEFAULT_BACKLOG = 2048
DEFAULT_UNIX_SOCKET_MODE = 0o660
DEFAULT_HEADER_TIMEOUT = 5.0
DEFAULT_KEEP_ALIVE_TIMEOUT = 5.0
DEFAULT_WRITE_TIMEOUT = 30.0
DEFAULT_REUSE_ADDR = True
DEFAULT_MAX_PIPELINED_REQUESTS = 8
DEFAULT_PIN_THREADS = False
DEFAULT_THREADS = 1


class RequestPolicy:
    """Header size cap, body read default, pipelining limit, and timeouts.

    `max_body_bytes` is the default limit for `body()` and `stream()`. A
    handler passes `max_size=` to accept more or less on one read.
    """

    __slots__ = (
        "body_timeout",
        "header_timeout",
        "keep_alive_timeout",
        "max_body_bytes",
        "max_header_bytes",
        "max_pipelined_requests",
        "write_timeout",
    )

    def __init__(
        self,
        *,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_body_bytes: int = DEFAULT_MAX_BODY_SIZE,
        header_timeout: float = DEFAULT_HEADER_TIMEOUT,
        body_timeout: float = DEFAULT_BODY_TIMEOUT,
        keep_alive_timeout: float = DEFAULT_KEEP_ALIVE_TIMEOUT,
        max_pipelined_requests: int = DEFAULT_MAX_PIPELINED_REQUESTS,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
    ) -> None:
        if max_header_bytes < 256:
            raise StarioError(
                "max_header_bytes must be at least 256",
                help_text="Increase the limit or use the default Server settings.",
            )
        if max_header_bytes > 2_147_483_647:
            raise StarioError(
                "max_header_bytes must be at most 2^31-1",
                help_text="The Cython protocol stores this cap as a 32-bit int.",
            )
        if max_body_bytes < 1:
            raise StarioError(
                "max_body_bytes must be at least 1",
                help_text="Use a positive byte limit for request bodies.",
            )
        if max_body_bytes > 2_147_483_647:
            raise StarioError(
                "max_body_bytes must be at most 2^31-1",
                help_text="The Cython protocol stores this cap as a 32-bit int.",
            )
        for field, value in (
            ("header_timeout", header_timeout),
            ("body_timeout", body_timeout),
            ("keep_alive_timeout", keep_alive_timeout),
            ("write_timeout", write_timeout),
        ):
            if value <= 0:
                raise StarioError(
                    f"{field} must be greater than 0",
                    help_text="Use a positive timeout in seconds.",
                )
        if max_pipelined_requests < 1:
            raise StarioError(
                "max_pipelined_requests must be at least 1",
                help_text="Use 1 to disable pipelining beyond the in-flight request.",
            )

        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.header_timeout = header_timeout
        self.body_timeout = body_timeout
        self.keep_alive_timeout = keep_alive_timeout
        self.max_pipelined_requests = max_pipelined_requests
        self.write_timeout = write_timeout


def _config_from_env[T](build: Callable[[], T]) -> T:
    try:
        return build()
    except ValueError as exc:
        raise StarioError(str(exc)) from exc


def request_policy_from_env() -> RequestPolicy:
    """Read `STARIO_REQUESTS_*` header cap, body read default, and timeouts."""
    return _config_from_env(
        lambda: RequestPolicy(
            max_header_bytes=env_int(
                "STARIO_REQUESTS_MAX_HEADER_BYTES", DEFAULT_MAX_HEADER_BYTES
            ),
            max_body_bytes=env_int(
                "STARIO_REQUESTS_MAX_BODY_BYTES", DEFAULT_MAX_BODY_SIZE
            ),
            header_timeout=env_float(
                "STARIO_REQUESTS_HEADER_TIMEOUT", DEFAULT_HEADER_TIMEOUT
            ),
            body_timeout=env_float(
                "STARIO_REQUESTS_BODY_TIMEOUT", DEFAULT_BODY_TIMEOUT
            ),
            keep_alive_timeout=env_float(
                "STARIO_REQUESTS_KEEP_ALIVE_TIMEOUT", DEFAULT_KEEP_ALIVE_TIMEOUT
            ),
            max_pipelined_requests=env_int(
                "STARIO_REQUESTS_MAX_PIPELINED_REQUESTS", DEFAULT_MAX_PIPELINED_REQUESTS
            ),
            write_timeout=env_float(
                "STARIO_REQUESTS_WRITE_TIMEOUT", DEFAULT_WRITE_TIMEOUT
            ),
        )
    )


class ServerConfig:
    """Listen address, transport policy, request policy, compression, and shutdown.

    When `unix_socket` is set, `host`, `port`, and `reuse_addr` are ignored; only
    `backlog` and `unix_socket_mode` apply to the Unix listener.

    Nested `requests` and `compression` objects are stored by reference; treat
    as immutable after construction.

    `graceful_shutdown_timeout` is the phase-1 drain window (handlers finish,
    idle connections close). Shutdown may continue up to one additional second
    in a force-close loop before orphaned tasks are cancelled (see
    `Server._drain_listener`).
    """

    __slots__ = (
        "backlog",
        "compression",
        "graceful_shutdown_timeout",
        "host",
        "max_connections",
        "pin_threads",
        "port",
        "requests",
        "reuse_addr",
        "ssl",
        "threads",
        "unix_socket",
        "unix_socket_mode",
    )

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        unix_socket: str | None = None,
        unix_socket_mode: int = DEFAULT_UNIX_SOCKET_MODE,
        requests: RequestPolicy | None = None,
        compression: CompressionConfig | None = None,
        graceful_shutdown_timeout: float = DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT,
        backlog: int = DEFAULT_BACKLOG,
        reuse_addr: bool = DEFAULT_REUSE_ADDR,
        threads: int = DEFAULT_THREADS,
        pin_threads: bool = DEFAULT_PIN_THREADS,
        max_connections: int | None = None,
        ssl: SSLContext | None = None,
        ssl_certfile: str | Path | None = None,
        ssl_keyfile: str | Path | None = None,
    ) -> None:
        if not 1 <= port <= 65535:
            raise StarioError(
                "port must be between 1 and 65535",
                help_text="Use a valid TCP port number.",
            )
        if graceful_shutdown_timeout < 0:
            raise StarioError(
                "graceful_shutdown_timeout must be greater than or equal to 0",
                help_text="Use zero for immediate shutdown or a positive drain window in seconds.",
            )
        if backlog < 1:
            raise StarioError(
                "backlog must be at least 1",
                help_text="Use a positive socket listen backlog.",
            )
        if not 0 <= unix_socket_mode <= 0o7777:
            raise StarioError(
                "unix_socket_mode must be between 0 and 7777 (octal)",
                help_text="Use a standard Unix file mode such as 660.",
            )
        if unix_socket is not None and not unix_socket.strip():
            raise StarioError(
                "unix_socket must be a non-empty path",
                help_text="Omit unix_socket for TCP listen or set STARIO_UNIX_SOCKET to a socket path.",
            )
        if unix_socket is None and not host.strip():
            raise StarioError(
                "host must be non-empty for TCP listen",
                help_text="Set STARIO_HOST or pass a non-empty host to ServerConfig.",
            )
        if threads < 1:
            raise StarioError(
                "threads must be at least 1",
                help_text="Set STARIO_THREADS to 1 (default) or a positive worker count.",
            )
        if threads > 256:
            raise StarioError(
                "threads must be at most 256",
                help_text="Set STARIO_THREADS to a smaller worker count.",
            )
        if max_connections is not None and max_connections < 1:
            raise StarioError(
                "max_connections must be at least 1",
                help_text="Set STARIO_MAX_CONNECTIONS to a positive cap.",
            )
        if ssl is not None and (ssl_certfile is not None or ssl_keyfile is not None):
            raise StarioError(
                "pass either ssl= or ssl_certfile=, not both",
                help_text="Use a prepared SSLContext or certificate paths.",
            )
        if ssl_keyfile is not None and ssl_certfile is None:
            raise StarioError(
                "ssl_keyfile requires ssl_certfile",
                help_text="Set STARIO_SSL_CERTFILE with STARIO_SSL_KEYFILE.",
            )

        self.host = host
        self.port = port
        self.unix_socket = unix_socket
        self.unix_socket_mode = unix_socket_mode
        self.requests = requests if requests is not None else RequestPolicy()
        self.compression = (
            compression if compression is not None else CompressionConfig()
        )
        self.graceful_shutdown_timeout = graceful_shutdown_timeout
        self.backlog = backlog
        self.reuse_addr = reuse_addr
        self.threads = threads
        # With threads>1, each worker thread sticks to one allowed CPU
        # (Linux sched_setaffinity). Opt-in: threads a pinned worker spawns
        # (asyncio.to_thread executors, writer threads) inherit that one-CPU
        # mask. Off: the kernel migrates threads freely.
        self.pin_threads = pin_threads
        # Process-wide live-connection cap, split evenly across worker loops.
        self.max_connections = max_connections
        if ssl is not None:
            self.ssl = ssl
        elif ssl_certfile is not None:
            self.ssl = load_tls_context(
                certfile=ssl_certfile,
                keyfile=ssl_keyfile,
            )
        else:
            self.ssl = None

    @classmethod
    def from_env(cls) -> "ServerConfig":
        """Read `STARIO_*` listen, limit, compression, and shutdown settings."""
        return server_config_from_env()


def server_config_from_env() -> ServerConfig:
    """Read `STARIO_*` listen, limit, compression, and shutdown settings."""
    return _config_from_env(
        lambda: ServerConfig(
            host=env_str("STARIO_HOST", DEFAULT_HOST),
            port=env_int("STARIO_PORT", DEFAULT_PORT),
            unix_socket=env_optional_str("STARIO_UNIX_SOCKET"),
            unix_socket_mode=env_octal_mode(
                "STARIO_UNIX_SOCKET_MODE", DEFAULT_UNIX_SOCKET_MODE
            ),
            compression=compression_config_from_env(),
            requests=request_policy_from_env(),
            graceful_shutdown_timeout=env_float(
                "STARIO_GRACEFUL_SHUTDOWN_TIMEOUT", DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT
            ),
            backlog=env_int("STARIO_BACKLOG", DEFAULT_BACKLOG),
            reuse_addr=env_bool("STARIO_REUSE_ADDR", DEFAULT_REUSE_ADDR),
            threads=env_int("STARIO_THREADS", DEFAULT_THREADS),
            pin_threads=env_bool("STARIO_PIN_THREADS", DEFAULT_PIN_THREADS),
            max_connections=env_optional_int("STARIO_MAX_CONNECTIONS"),
            ssl_certfile=env_optional_str("STARIO_SSL_CERTFILE"),
            ssl_keyfile=env_optional_str("STARIO_SSL_KEYFILE"),
        )
    )
