"""Test-side telemetry tracer for `TestClient`."""

import bisect
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

from stario.telemetry.core import Attributes, Span, TelemetryStats
from stario.telemetry.spans import RecordingSpan
from stario.testing.models import TelemetryEvent, TelemetryLink, TelemetrySpan

_UNSET: Any = object()


def _telemetry_from_recording(span: RecordingSpan) -> TelemetrySpan:
    if span.start_ns is None or span.end_ns is None:
        raise RuntimeError("Cannot snapshot an unfinished telemetry span.")
    events = tuple(
        TelemetryEvent(
            name=e.name,
            time_ns=e.time_ns,
            attributes=e.attributes.copy() if e.attributes else {},
            body=e.body,
        )
        for e in span.events or ()
    )
    links = tuple(
        TelemetryLink(
            name=link.name,
            span_id=link.span_id,
            attributes=link.attributes.copy() if link.attributes else {},
        )
        for link in span.links or ()
    )
    return TelemetrySpan(
        id=span.id,
        name=span.name,
        parent_id=span.parent_id,
        start_ns=span.start_ns,
        end_ns=span.end_ns,
        status="error" if span.error is not None else "ok",
        error=span.error,
        attributes=dict(span.attributes or {}),
        events=events,
        links=links,
    )


class TestTracer:
    """Test-side view of telemetry for `TestClient`.

    Implements the `stario.telemetry.Tracer` protocol for request dispatch.
    Assertions in tests should use the query helpers below;
    they only return finished span snapshots.
    """

    __test__ = False

    def __init__(self) -> None:
        self._entry_depth = 0
        self._open: set[UUID] = set()
        self._finished: list[TelemetrySpan] = []
        self._finished_by_id: dict[UUID, TelemetrySpan] = {}
        # Id of the newest root span. `TestClient` reads it right after feeding
        # a request, which is when the protocol opens that request's span.
        self.last_created: UUID | None = None

    def __enter__(self) -> Self:
        self._entry_depth += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._entry_depth > 0:
            self._entry_depth -= 1
        return None

    def create(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        parent: Span | None = None,
    ) -> Span:
        if self._entry_depth <= 0:
            raise RuntimeError("TestTracer must be entered before creating spans.")
        span = RecordingSpan.create(self, name, attributes, parent=parent)
        self._open.add(span.id)
        if parent is None:
            self.last_created = span.id
        return span

    def on_end(self, span: Span) -> None:
        if self._entry_depth <= 0:
            raise RuntimeError("TestTracer must be entered before ending spans.")
        span = cast(RecordingSpan, span)
        finished = _telemetry_from_recording(span)
        bisect.insort_right(
            self._finished,
            finished,
            key=lambda s: (s.start_ns, s.end_ns),
        )
        self._finished_by_id[finished.id] = finished
        self._open.discard(finished.id)

    def stats(self) -> TelemetryStats:
        return TelemetryStats()

    def get_span(self, span_id: UUID) -> TelemetrySpan | None:
        """Return the finished `TelemetrySpan` for `span_id`, or `None`.

        Open spans and unknown ids yield `None`.
        """

        return self._finished_by_id.get(span_id)

    def find_span(
        self,
        name: str,
        *,
        root_id: UUID | None = None,
        parent_id: UUID | None = None,
    ) -> TelemetrySpan | None:
        """First finished span named `name`, in start-time order.

        When several requests reuse the same span name, pass `root_id=r.span_id`
        (from the matching `TestResponse`) so you match the right subtree.
        `parent_id` requires an exact parent link.
        """

        for s in self._finished:
            if s.name != name:
                continue
            if parent_id is not None and s.parent_id != parent_id:
                continue
            if root_id is not None:
                cur: TelemetrySpan | None = s
                seen: set[UUID] = set()
                under = False
                while cur is not None:
                    if cur.id == root_id:
                        under = True
                        break
                    if cur.id in seen:
                        break
                    seen.add(cur.id)
                    if cur.parent_id is None:
                        break
                    cur = self.get_span(cur.parent_id)
                if not under:
                    continue
            return s
        return None

    def get_events(
        self,
        span_id: UUID,
        *,
        name: str | None = None,
    ) -> tuple[TelemetryEvent, ...]:
        """Events recorded on a finished span; filter with `name` when set."""

        span = self.get_span(span_id)
        if span is None:
            return ()
        if name is None:
            return span.events
        return tuple(e for e in span.events if e.name == name)

    def get_event(
        self,
        span_id: UUID,
        event_name: str,
        *,
        index: int = 0,
    ) -> TelemetryEvent | None:
        """The `index`-th `TelemetryEvent` named `event_name`, or `None`."""

        matches = [e for e in self.get_events(span_id, name=event_name)]
        if index < 0 or index >= len(matches):
            return None
        return matches[index]

    def has_event(self, span_id: UUID, event_name: str) -> bool:
        """Whether `get_events(span_id)` contains at least one `event_name`."""

        return any(e.name == event_name for e in self.get_events(span_id))

    def has_attribute(
        self,
        span_id: UUID,
        key: str,
        value: Any = _UNSET,
    ) -> bool:
        """Whether the finished span's attributes include `key`.

        Pass `value` to require equality; omit it to assert presence only.
        """

        span = self.get_span(span_id)
        if span is None:
            return False
        if key not in span.attributes:
            return False
        if value is _UNSET:
            return True
        return span.attributes[key] == value

    def has_open_spans(self, *, besides: UUID | None = None) -> bool:
        """`True` while any span created through this tracer has not been ended.

        `besides` skips one span, such as the open `server.shutdown` span.
        """

        if besides is None:
            return bool(self._open)
        return any(span_id != besides for span_id in self._open)

    def finished_spans(self) -> tuple[TelemetrySpan, ...]:
        """Finished spans in start-time order."""

        return tuple(self._finished)
