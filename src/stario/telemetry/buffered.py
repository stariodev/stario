"""Shared bounded queue, writer thread, and self-metrics for Json/SQLite sinks."""

import sys
import threading
import time
from collections import deque
from typing import Any, Self, cast

from .core import Attributes, Span, TelemetryStats, Tracer
from .spans import RecordingSpan

# Subclassed by JsonTracer and SqliteTracer.
__all__ = ["_BufferedTracer"]

# A full queue says so on stderr at most this often.
_DROP_REPORT_INTERVAL_NS = 1_000_000_000


class _BufferedTracer:
    """Background writer thread with a bounded pending queue and drop metrics.

    Subclasses implement `_write_batch()` and `_thread_name()`. Not part of the
    public API — use `JsonTracer` or `SqliteTracer`.
    """

    __slots__ = (
        "_control_lock",
        "_drop_reported_at_ns",
        "_dropped_spans",
        "_dropped_unreported",
        "_flush_interval",
        "_last_writer_error",
        "_last_writer_error_at_ns",
        "_max_batch_spans",
        "_max_pending_spans",
        "_metrics_lock",
        "_pending",
        "_pending_lock",
        "_running",
        "_serialization_error_count",
        "_stop_requested",
        "_thread",
        "_tracer_label",
        "_tracer_type_name",
        "_wake",
        "_wake_batch_spans",
        "_writer_error_count",
    )

    def __init__(
        self,
        *,
        tracer_label: str,
        tracer_type_name: str,
        flush_interval: float,
        max_pending_spans: int,
        max_batch_spans: int,
    ) -> None:
        if flush_interval <= 0:
            raise ValueError("flush_interval must be greater than zero")
        if max_pending_spans <= 0:
            raise ValueError("max_pending_spans must be greater than zero")
        if max_batch_spans <= 0:
            raise ValueError("max_batch_spans must be greater than zero")

        self._tracer_label = tracer_label
        self._tracer_type_name = tracer_type_name
        self._control_lock = threading.Lock()
        self._flush_interval = flush_interval
        self._max_pending_spans = max_pending_spans
        self._max_batch_spans = min(max_batch_spans, max_pending_spans)
        self._wake_batch_spans = self._max_batch_spans
        # A deque under a plain lock: queue.Queue's Condition bookkeeping cost
        # ~1.4 µs per finished span on the request thread.
        self._pending: deque[RecordingSpan] = deque()
        self._pending_lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_requested = False
        self._metrics_lock = threading.Lock()
        self._dropped_spans = 0
        self._dropped_unreported = 0
        self._drop_reported_at_ns: int | None = None
        self._serialization_error_count = 0
        self._writer_error_count = 0
        self._last_writer_error: str | None = None
        self._last_writer_error_at_ns: int | None = None

    def __enter__(self: Self) -> Tracer:
        self._start_writer()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._stop_writer()
        self._on_exit(exc_type, exc_val, exc_tb)

    def _on_exit(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Subclass hook after the writer thread stops."""

    def stats(self) -> TelemetryStats:
        with self._metrics_lock:
            return TelemetryStats(
                dropped_spans=self._dropped_spans,
                serialization_error_count=self._serialization_error_count,
                writer_error_count=self._writer_error_count,
                last_writer_error=self._last_writer_error,
                last_writer_error_at_ns=self._last_writer_error_at_ns,
            )

    def create(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        parent: Span | None = None,
    ) -> RecordingSpan:
        if not self._running:
            raise RuntimeError(
                f"{self._tracer_type_name} must be entered before creating spans."
            )
        return RecordingSpan.create(
            cast(Tracer, self),
            name,
            attributes,
            parent=parent,
        )

    def on_end(self, span: Span) -> None:
        span = cast(RecordingSpan, span)
        if not self._running:
            self._record_dropped_spans(1)
            self._emit_stderr(
                f"{self._tracer_label} dropped finished span while stopped: "
                f"{span.id} ({span.name!r})"
            )
            return
        self._enqueue_finished(span)

    def _start_writer(self) -> None:
        with self._control_lock:
            if self._running:
                return
            self._on_start_writer()
            self._running = True
            self._stop_requested = False
            self._thread = threading.Thread(
                target=self._writer_loop,
                name=self._thread_name(),
                daemon=True,
            )
            self._thread.start()

    def _stop_writer(self) -> None:
        with self._control_lock:
            thread = self._thread
            if thread is None:
                return
            self._thread = None
            self._stop_requested = True
            self._wake.set()
        # Spans still queue while the writer finishes: its last pass and the
        # drain below take them. Only spans ending after that are dropped.
        thread.join()
        with self._control_lock:
            self._running = False
        pending = self._take_pending()
        if pending:
            self._write_batches_safe(pending)

    def _on_start_writer(self) -> None:
        """Subclass hook run once before the writer thread starts."""

    def _thread_name(self) -> str:
        raise NotImplementedError

    def _writer_loop(self) -> None:
        while True:
            self._wake.wait(self._flush_interval)
            self._wake.clear()

            pending = self._take_pending()
            if pending:
                self._write_batches_safe(pending)

            if self._stop_requested:
                pending = self._take_pending()
                if pending:
                    self._write_batches_safe(pending)
                return

    def _enqueue_finished(self, span: RecordingSpan) -> None:
        with self._pending_lock:
            pending = self._pending
            size = len(pending)
            if size < self._max_pending_spans:
                pending.append(span)
                size += 1
            else:
                size = -1
        wake = self._wake
        if size < 0:
            self._report_dropped(span)
            wake.set()
            return
        # Event.set takes a Condition; skip it while the writer is already
        # signalled (it clears the flag before taking the batch).
        if size >= self._wake_batch_spans and not wake.is_set():
            wake.set()

    def _take_pending(self) -> list[RecordingSpan]:
        with self._pending_lock:
            pending = self._pending
            self._pending = deque()
        return list(pending)

    def _report_dropped(self, span: RecordingSpan) -> None:
        """Count a span the full queue dropped; say so at most once a second."""
        now = time.monotonic_ns()
        with self._metrics_lock:
            self._dropped_spans += 1
            self._dropped_unreported += 1
            last = self._drop_reported_at_ns
            if last is not None and now - last < _DROP_REPORT_INTERVAL_NS:
                return
            count = self._dropped_unreported
            self._dropped_unreported = 0
            self._drop_reported_at_ns = now
        self._emit_stderr(
            f"{self._tracer_label} pending queue full "
            f"(max={self._max_pending_spans}); dropped {count} finished "
            f"span{'' if count == 1 else 's'} since the last report, "
            f"latest {span.id} ({span.name!r})"
        )

    def _record_dropped_spans(self, count: int) -> None:
        if count <= 0:
            return
        with self._metrics_lock:
            self._dropped_spans += count

    def _record_writer_error(self, exc: Exception) -> None:
        with self._metrics_lock:
            self._writer_error_count += 1
            self._last_writer_error = f"{type(exc).__name__}: {exc}"
            self._last_writer_error_at_ns = time.time_ns()

    def _record_serialization_error(self) -> None:
        with self._metrics_lock:
            self._serialization_error_count += 1

    def _emit_stderr(self, message: str) -> None:
        try:
            sys.stderr.write(f"[stario] {message}\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _write_batches_safe(self, spans: list[RecordingSpan]) -> None:
        try:
            self._write_batches(spans)
        except Exception as exc:
            self._record_writer_error(exc)
            self._emit_stderr(
                f"{self._tracer_label} write error for {len(spans)} finished spans: "
                f"{type(exc).__name__}: {exc}"
            )

    def _write_batches(self, spans: list[RecordingSpan]) -> None:
        for start in range(0, len(spans), self._max_batch_spans):
            self._write_batch(spans[start : start + self._max_batch_spans])

    def _write_batch(self, spans: list[RecordingSpan]) -> None:
        raise NotImplementedError
