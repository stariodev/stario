"""Request-scoped handler bundle: Protocol plus route match and ``alive()``.

Production ``c`` is the Cython ``RequestHandle`` (one per handler call, in front
of the pooled ``RequestExchange``). TestClient supplies its own
context. ``Match`` and ``_Alive`` stay as small Python helpers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, overload

from stario.telemetry.core import Span

_NO_PARAMS: MappingProxyType[str, str] = MappingProxyType({})


class Match:
    """Immutable hit: pattern plus this request's captures."""

    __slots__ = ("params", "pattern")

    pattern: str
    params: Mapping[str, str]

    def __init__(
        self,
        pattern: str,
        params: Mapping[str, str] = _NO_PARAMS,
    ) -> None:
        object.__setattr__(self, "pattern", pattern)
        if not params:
            frozen: Mapping[str, str] = _NO_PARAMS
        elif isinstance(params, MappingProxyType):
            frozen = params
        else:
            frozen = MappingProxyType(params)
        object.__setattr__(self, "params", frozen)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Match is immutable")

    def __repr__(self) -> str:
        if not self.pattern:
            return "Match.empty"
        return f"Match({self.pattern!r})"

    @classmethod
    def empty(cls) -> Match:
        """Unmatched 404 / 405 sentinel."""
        return EMPTY_MATCH


EMPTY_MATCH = Match.__new__(Match)
object.__setattr__(EMPTY_MATCH, "pattern", "")
object.__setattr__(EMPTY_MATCH, "params", _NO_PARAMS)

if TYPE_CHECKING:
    from stario.http.app import App
    from stario.http.request import Request
    from stario.http.writer import Writer


class Context(Protocol):
    """Per-request bundle passed to every handler and middleware."""

    app: App
    req: Request
    span: Span
    state: dict[str, Any]
    match: Match

    @property
    def disconnect(self) -> asyncio.Future[None]:
        """Completes when the client closes this request's connection."""
        ...

    @property
    def disconnected(self) -> bool:
        """``True`` when the client closed this request's connection."""
        ...

    @property
    def shutting_down(self) -> bool:
        """``True`` when the server is draining this app."""
        ...

    @property
    def closing(self) -> bool:
        """``True`` when handler work should stop (client left or app draining)."""
        ...

    @overload
    def alive(self, source: None = None) -> _Alive[None]: ...

    @overload
    def alive[T](self, source: AsyncIterable[T]) -> _Alive[T]: ...

    def alive[T](
        self,
        source: AsyncIterable[T] | None = None,
    ) -> _Alive[T] | _Alive[None]:
        """Watch client disconnect and app shutdown; cancel this task when either happens.

        Use `async with c.alive(): ...` for scoped work, or
        `async for item in c.alive(source): ...` to stream from `source` until
        disconnect or shutdown. Do not use `async for` without `source`; the
        context-manager form is the supported no-source pattern.

        The `async for` form swallows the disconnect/shutdown cancel only
        while it waits on `source`. If the loop body itself awaits, that
        cancel lands inside the body and propagates. To have it swallowed
        anywhere in the block, wrap the iteration instead:

        ```python
        async with c.alive():
            async for item in source:
                await send(item)
        ```
        """
        ...


@dataclass(slots=True)
class _Alive[T]:
    """Connection lifecycle helper bound to a request context.

    A done-callback on the disconnect/shutdown futures cancels the handler
    task; ``__aexit__`` swallows only that cancel. The bookkeeping mirrors
    ``asyncio.timeout``: ``uncancel()`` below the count recorded at enter
    means the cancel was ours — a foreign cancel raced with ours still
    propagates, and a foreign cancel alone never sets ``expired``.

    ``_armed`` guards the window between a queued ``_fire`` and
    ``remove_done_callback``: callbacks already scheduled via ``call_soon``
    cannot be removed, so a fire after ``__aexit__`` must no-op instead of
    cancelling the task inside its post-scope cleanup.

    With ``source`` set, ``__aiter__`` scopes the cancel to its own ``async
    for`` on the source; a cancel landing in the caller's loop body is not
    ours to swallow. ``async with`` around the source loop covers the body.
    """

    c: Context
    source: AsyncIterable[T] | None = None
    expired: bool = False
    _task: asyncio.Task[Any] | None = None
    _futures: tuple[asyncio.Future[None], ...] = ()
    _armed: bool = False
    _cancelling: int = 0

    async def __aiter__(self) -> AsyncIterator[T]:
        if self.source is None:
            raise RuntimeError(
                "Use `async with c.alive():` when not streaming a source."
            )
        async with self:
            async for item in self.source:
                yield item

    def _fire(self, _fut: asyncio.Future[None]) -> None:
        """One-shot: the first completion wins and disarms the rest."""
        if not self._armed:
            return
        self._armed = False
        for fut in self._futures:
            fut.remove_done_callback(self._fire)
        task = self._task
        if (
            task is not None
            and not task.done()
            and task.cancelling() == self._cancelling
        ):
            self.expired = True
            task.cancel()

    async def __aenter__(self) -> _Alive[T]:
        self._task = asyncio.current_task()
        self._futures = (self.c.disconnect, self.c.app.shutdown)
        if self._task is not None:
            self._cancelling = self._task.cancelling()
        self._armed = True
        for fut in self._futures:
            fut.add_done_callback(self._fire)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> bool:
        self._armed = False
        for fut in self._futures:
            fut.remove_done_callback(self._fire)

        if (
            exc_type is None
            or not issubclass(exc_type, asyncio.CancelledError)
            or not self.expired
        ):
            return False
        # Swallow only our own cancel: after uncancel(), anything left in the
        # count is a request somebody else made — it must propagate.
        task = asyncio.current_task()
        return task is not None and task.uncancel() <= self._cancelling


type Handler = Callable[[Context, "Writer"], Awaitable[None]]
type Middleware = Callable[[Handler], Handler]
