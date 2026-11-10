"""Concrete `Span` implementations used by bundled tracers and tests."""

from __future__ import annotations

import os
import threading
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .core import Attributes, EventBody, Span, Tracer
from .formatters import format_exception_for_telemetry, serialize_event_body

# CPython 3.14 `uuid.uuid7` (RFC 9562 Method 1). stdlib uuid7 is 3.14-only.
_RFC_4122_VERSION_7_FLAGS = 0x70008000000000000000
# One `os.urandom` syscall feeds this many bytes of ids (10 per fresh id).
_RANDOM_POOL_BYTES = 640
# Spans outside a live-rendering tracer mutate under this no-op context.
_NO_LOCK: AbstractContextManager[Any] = nullcontext()
# Events kept per span; later ones are counted, see `RecordingSpan`.
_MAX_EVENTS = 1000


class _Uuid7State(threading.local):
    """Per-thread uuid7 state: last timestamp and counter, plus a random pool."""

    def __init__(self) -> None:
        self.last_timestamp: int | None = None
        self.last_counter = 0
        self.pool = b""
        self.pos = 0


_uuid7_state = _Uuid7State()


def _span_id() -> UUID:
    """UUIDv7 for span and trace ids. Same bits as CPython 3.14 `uuid.uuid7`.

    The timestamp, counter, and random pool are per thread, so ids are
    monotonic within one thread only; the random counter and tail keep them
    unique across threads. (CPython's uuid7 is not locked either.)
    """
    state = _uuid7_state
    timestamp_ms = time.time_ns() // 1_000_000
    last_timestamp = state.last_timestamp
    pool = state.pool
    pos = state.pos
    if last_timestamp is not None and timestamp_ms <= last_timestamp:
        if timestamp_ms < last_timestamp:
            timestamp_ms = last_timestamp + 1
        counter = state.last_counter + 1
        if counter <= 0x3FF_FFFF_FFFF:
            # Same millisecond: next counter, 4 fresh random bytes of tail.
            end = pos + 4
            if end > len(pool):
                state.pool = pool = os.urandom(_RANDOM_POOL_BYTES)
                pos, end = 0, 4
            state.pos = end
            state.last_timestamp = timestamp_ms
            state.last_counter = counter
            return UUID(
                int=((timestamp_ms & 0xFFFF_FFFF_FFFF) << 80)
                | ((counter >> 30) << 64)
                | ((counter & 0x3FFF_FFFF) << 32)
                | int.from_bytes(pool[pos:end])
                | _RFC_4122_VERSION_7_FLAGS
            )
        timestamp_ms += 1
    # New millisecond (or counter overflow): random 42-bit counter (MSB 0)
    # and 32-bit tail from 10 pool bytes.
    end = pos + 10
    if end > len(pool):
        state.pool = pool = os.urandom(_RANDOM_POOL_BYTES)
        pos, end = 0, 10
    rand = int.from_bytes(pool[pos:end])
    counter = (rand >> 32) & 0x1FF_FFFF_FFFF
    state.pos = end
    state.last_timestamp = timestamp_ms
    state.last_counter = counter
    return UUID(
        int=((timestamp_ms & 0xFFFF_FFFF_FFFF) << 80)
        | ((counter >> 30) << 64)
        | ((counter & 0x3FFF_FFFF) << 32)
        | (rand & 0xFFFF_FFFF)
        | _RFC_4122_VERSION_7_FLAGS
    )


@dataclass(slots=True, eq=False)
class NoOpSpan:
    """Discards all span data; returned by every `NoOpTracer.create()`."""

    id: UUID
    trace_id: UUID
    parent_id: UUID | None

    @property
    def tracer(self) -> Tracer:
        from .noop import NOOP_TRACER

        return NOOP_TRACER

    def start(self) -> None:
        pass

    def __enter__(self) -> Span:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def event(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        pass

    def exception(
        self,
        exc: BaseException,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        pass

    def attr(self, name: str, value: Any) -> None:
        pass

    def attrs(self, attributes: Attributes) -> None:
        pass

    def __setitem__(self, name: str, value: Any) -> None:
        pass

    def step(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self

    def new_trace(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self

    def link(
        self,
        name: str,
        span_id: UUID,
        attributes: Attributes | None = None,
        /,
    ) -> None:
        pass

    def fail(self, message: str) -> None:
        pass

    def rename(self, name: str) -> None:
        pass

    def end(self) -> None:
        pass


class ProxySpan:
    """Stable span handle whose target can be swapped (bootstrap shutdown span).

    Delegates the full `Span` protocol to the current underlying span. `replace()`
    is the only extra surface — used when one variable must outlive a single span
    record (for example `server.startup` → `server.shutdown`).
    """

    __slots__ = ("_span",)

    def __init__(self, span: Span) -> None:
        self._span = span

    def replace(self, span: Span) -> None:
        self._span = span

    @property
    def id(self) -> UUID:
        return self._span.id

    @property
    def trace_id(self) -> UUID:
        return self._span.trace_id

    @property
    def parent_id(self) -> UUID | None:
        return self._span.parent_id

    @property
    def tracer(self) -> Tracer:
        return self._span.tracer

    def start(self) -> None:
        self._span.start()

    def __enter__(self) -> Span:
        self._span.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._span.__exit__(exc_type, exc_val, exc_tb)

    def event(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        self._span.event(name, attributes, body=body)

    def exception(
        self,
        exc: BaseException,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        self._span.exception(exc, attributes, body=body)

    def attr(self, name: str, value: Any) -> None:
        self._span.attr(name, value)

    def attrs(self, attributes: Attributes) -> None:
        self._span.attrs(attributes)

    def __setitem__(self, name: str, value: Any) -> None:
        self._span[name] = value

    def step(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self._span.step(name, attributes)

    def new_trace(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self._span.new_trace(name, attributes)

    def link(
        self,
        name: str,
        span_id: UUID,
        attributes: Attributes | None = None,
        /,
    ) -> None:
        self._span.link(name, span_id, attributes)

    def fail(self, message: str) -> None:
        self._span.fail(message)

    def rename(self, name: str) -> None:
        self._span.rename(name)

    def end(self) -> None:
        self._span.end()


@dataclass(slots=True)
class RecordedEvent:
    """One in-span timestamped event (exported with the parent span)."""

    time_ns: int
    name: str
    attributes: dict[str, Any] | None = None
    body: str | None = None


@dataclass(slots=True)
class RecordedLink:
    """Reference from this span to another span id (exported with the parent)."""

    name: str
    span_id: UUID
    attributes: dict[str, Any] | None = None


@dataclass(slots=True, eq=False)
class RecordingSpan:
    """Mutable span record; the creating tracer reads it in `on_end()`.

    Implements the `Span` protocol. `tracer` is the sink that created this span
    (`step()` / `new_trace()` call back into it). State properties (`started`,
    `in_progress`, `finished`, `failed`) mirror the lifecycle described on
    `Span` in `core.py`.

    Every mutation runs under `mutex`: a no-op context by default, or the
    lock a tracer that renders open spans assigns (`TTYTracer`). A span keeps
    at most 1000 events; later ones are dropped and counted, and `end()`
    records the count as the attribute `span.dropped_events`.
    """

    id: UUID
    tracer: Tracer
    trace_id: UUID
    parent_id: UUID | None
    name: str
    start_ns: int | None = None
    end_ns: int | None = None
    error: str | None = None
    attributes: dict[str, Any] | None = None
    events: list[RecordedEvent] | None = None
    links: list[RecordedLink] | None = field(default=None, repr=False)
    mutex: AbstractContextManager[Any] = field(
        default=_NO_LOCK, repr=False, compare=False
    )
    dropped_events: int = field(default=0, repr=False)

    @classmethod
    def create(
        cls,
        tracer: Tracer,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        parent: Span | None = None,
    ) -> RecordingSpan:
        span_id = _span_id()
        if parent is None:
            trace_id, parent_id = span_id, None
        else:
            trace_id, parent_id = parent.trace_id, parent.id
        return cls(
            span_id,
            tracer,
            trace_id,
            parent_id,
            name,
            attributes=dict(attributes) if attributes else None,
        )

    @property
    def started(self) -> bool:
        return self.start_ns is not None

    @property
    def in_progress(self) -> bool:
        return self.end_ns is None and self.started

    @property
    def finished(self) -> bool:
        return self.end_ns is not None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def start(self) -> None:
        with self.mutex:
            if self.start_ns is None:
                self.start_ns = time.time_ns()

    def __enter__(self) -> Span:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_val is not None:
            self.exception(exc_val)
            self.fail(str(exc_val) or type(exc_val).__name__)
        self.end()

    def event(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        with self.mutex:
            if self.start_ns is None or self.end_ns is not None:
                return
            events = self.events
            if events is not None and len(events) >= _MAX_EVENTS:
                self.dropped_events += 1
                return
            event = RecordedEvent(
                time.time_ns(),
                name,
                attributes=dict(attributes) if attributes else None,
                body=serialize_event_body(body),
            )
            if events is None:
                self.events = [event]
            else:
                events.append(event)

    def exception(
        self,
        exc: BaseException,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        if self.start_ns is None or self.end_ns is not None:
            return
        attrs = dict(attributes) if attributes else {}
        attrs["exc.type"] = type(exc).__name__
        attrs["exc.message"] = str(exc)
        if body is None:
            body = format_exception_for_telemetry(exc)
        self.event("exception", attrs, body=body)

    def attr(self, name: str, value: Any) -> None:
        with self.mutex:
            if self.end_ns is not None:
                return
            if self.attributes is None:
                self.attributes = {name: value}
            else:
                self.attributes[name] = value

    def attrs(self, attributes: Attributes) -> None:
        with self.mutex:
            if self.end_ns is not None:
                return
            if not attributes:
                return
            if self.attributes is None:
                self.attributes = dict(attributes)
            else:
                self.attributes.update(attributes)

    def __setitem__(self, name: str, value: Any) -> None:
        self.attr(name, value)

    def step(self, name: str, attributes: Attributes | None = None, /) -> Span:
        if not self.in_progress:
            raise RuntimeError(
                "Cannot create a child span from a span that is not open."
            )
        return self.tracer.create(name, attributes, parent=self)

    def new_trace(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self.tracer.create(name, attributes)

    def link(
        self,
        name: str,
        span_id: UUID,
        attributes: Attributes | None = None,
        /,
    ) -> None:
        with self.mutex:
            if self.start_ns is None or self.end_ns is not None:
                return
            link = RecordedLink(
                name,
                span_id,
                attributes=dict(attributes) if attributes else None,
            )
            if self.links is None:
                self.links = [link]
            else:
                self.links.append(link)

    def fail(self, message: str) -> None:
        with self.mutex:
            if self.start_ns is None or self.end_ns is not None:
                return
            self.error = message

    def rename(self, name: str) -> None:
        with self.mutex:
            if self.end_ns is not None:
                return
            self.name = name

    def end(self) -> None:
        with self.mutex:
            if self.end_ns is not None:
                return
            if self.start_ns is None:
                raise RuntimeError(
                    "Cannot end a span that was never started. "
                    "Call span.start() or use the span as a context manager."
                )
            if self.dropped_events:
                if self.attributes is None:
                    self.attributes = {}
                self.attributes["span.dropped_events"] = self.dropped_events
            self.end_ns = time.time_ns()
            self.tracer.on_end(self)
