import asyncio
from collections.abc import Iterator
from typing import Any

class Connections:
    """One event loop's live connections, their timeout tick, and the clock."""

    now: float
    max_connections: int
    rejected: int
    def __init__(
        self,
        *,
        sweep_interval: float = 1.0,
        max_connections: int = 0,
        date: bytes | None = None,
    ) -> None: ...
    def pin_date(self, line: bytes) -> None: ...
    @property
    def date_line(self) -> bytes: ...
    def add(self, proto: Any) -> None: ...
    def discard(self, proto: Any) -> None: ...
    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[Any]: ...
    def __contains__(self, proto: object) -> bool: ...
    def __bool__(self) -> bool: ...
    @property
    def sweep_interval(self) -> float: ...
    @property
    def sweeping(self) -> bool: ...
    def sweep(self, now: float) -> None: ...

# Runtime base is asyncio.BufferedProtocol. data_received is implemented too,
# so the class also satisfies asyncio.Protocol.
class HttpProtocol(asyncio.Protocol):
    loop: asyncio.AbstractEventLoop
    transport: asyncio.Transport | None
    disconnect: asyncio.Future[None] | None
    closed: bool
    parse_mode: int
    last_task: asyncio.Task[Any] | None
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        app: Any,
        tracer: Any,
        compression: Any,
        connections: Connections,
        max_header_bytes: int = ...,
        max_body_bytes: int = ...,
        header_timeout: float = ...,
        keep_alive_timeout: float = ...,
        body_timeout: float = ...,
        max_pipelined_requests: int = ...,
        write_timeout: float = ...,
        timeout_cleanup: str | int | None = None,
    ) -> None: ...
    def data_received(self, data: bytes | bytearray | memoryview) -> None: ...
    def get_buffer(self, sizehint: int) -> memoryview: ...
    def buffer_updated(self, nbytes: int) -> None: ...
    def check_timeouts(self, now: float) -> None: ...
    def close_if_idle(self) -> bool: ...
