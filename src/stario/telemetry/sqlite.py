"""Persists finished spans to SQLite for local querying — not a distributed trace store."""

from __future__ import annotations

import sqlite3
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from stario._env import env_float, env_int, env_path

from .buffered import _BufferedTracer
from .formatters import serialize_json
from .spans import RecordingSpan

if TYPE_CHECKING:
    from typing_extensions import deprecated
else:
    # The decorator only informs type checkers. Runtime warnings are explicit
    # warnings.warn calls, so warnings.deprecated would warn a second time.
    def deprecated(message: str, /) -> Callable[[Any], Any]:
        def decorator(obj: Any) -> Any:
            return obj

        return decorator


_DEFAULT_SQLITE_PATH = "stario-traces.sqlite3"
_DEFAULT_FLUSH_INTERVAL = 0.25
_DEFAULT_MAX_PENDING_SPANS = 65536
_DEFAULT_MAX_BATCH_SPANS = 1024

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS spans (
        span_id BLOB PRIMARY KEY,
        trace_id BLOB NOT NULL,
        parent_id BLOB,
        name TEXT NOT NULL,
        start_ns INTEGER NOT NULL,
        end_ns INTEGER NOT NULL,
        duration_ns INTEGER NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        attrs_json TEXT
    ) WITHOUT ROWID;

    CREATE INDEX IF NOT EXISTS spans_trace_id_idx
        ON spans (trace_id);
    CREATE INDEX IF NOT EXISTS spans_end_ns_idx
        ON spans (end_ns);
    CREATE INDEX IF NOT EXISTS spans_error_end_ns_idx
        ON spans (end_ns)
        WHERE status = 'error';

    CREATE TABLE IF NOT EXISTS span_events (
        id INTEGER PRIMARY KEY,
        span_id BLOB NOT NULL,
        trace_id BLOB NOT NULL,
        time_ns INTEGER NOT NULL,
        name TEXT NOT NULL,
        attrs_json TEXT,
        body TEXT
    );

    CREATE INDEX IF NOT EXISTS span_events_name_time_ns_idx
        ON span_events (name, time_ns);
    CREATE INDEX IF NOT EXISTS span_events_trace_id_idx
        ON span_events (trace_id);

    CREATE TABLE IF NOT EXISTS span_links (
        id INTEGER PRIMARY KEY,
        span_id BLOB NOT NULL,
        trace_id BLOB NOT NULL,
        target_span_id BLOB NOT NULL,
        name TEXT NOT NULL,
        attrs_json TEXT
    );

    CREATE INDEX IF NOT EXISTS span_links_span_id_idx
        ON span_links (span_id);
    CREATE INDEX IF NOT EXISTS span_links_target_span_id_idx
        ON span_links (target_span_id);
    CREATE INDEX IF NOT EXISTS span_links_trace_id_idx
        ON span_links (trace_id);
"""

_INSERT_SPANS_SQL = """
    INSERT INTO spans (
        span_id,
        trace_id,
        parent_id,
        name,
        start_ns,
        end_ns,
        duration_ns,
        status,
        error,
        attrs_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_EVENTS_SQL = """
    INSERT INTO span_events (
        span_id,
        trace_id,
        time_ns,
        name,
        attrs_json,
        body
    ) VALUES (?, ?, ?, ?, ?, ?)
"""

_INSERT_LINKS_SQL = """
    INSERT INTO span_links (
        span_id,
        trace_id,
        target_span_id,
        name,
        attrs_json
    ) VALUES (?, ?, ?, ?, ?)
"""


def _event_rows(span: RecordingSpan) -> list[tuple[Any, ...]]:
    if not span.events:
        return []
    span_id = _uuid_blob(span.id)
    trace_id = _uuid_blob(span.trace_id)
    rows: list[tuple[Any, ...]] = []
    for event in span.events:
        rows.append(
            (
                span_id,
                trace_id,
                event.time_ns,
                event.name,
                serialize_json(event.attributes),
                event.body,
            )
        )
    return rows


def _link_rows(span: RecordingSpan) -> list[tuple[Any, ...]]:
    if not span.links:
        return []
    span_id = _uuid_blob(span.id)
    trace_id = _uuid_blob(span.trace_id)
    rows: list[tuple[Any, ...]] = []
    for link in span.links:
        rows.append(
            (
                span_id,
                trace_id,
                _uuid_blob(link.span_id),
                link.name,
                serialize_json(link.attributes),
            )
        )
    return rows


def _uuid_blob(value: UUID | None) -> bytes | None:
    if value is None:
        return None
    return value.bytes


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA_SQL)


@deprecated(
    "SqliteTracer is deprecated: use JsonTracer(path=...) and query the "
    "NDJSON file. Removed in 6.0."
)
class SqliteTracer(_BufferedTracer):
    """Local SQLite span store with a batched background writer.

    Span mutations stay on the request thread; finished spans queue until
    flush. Enter before `create()`. Schema is created on first open. On write
    failure the writer reconnects and retries; see `stats()` for drops and
    errors.
    """

    __slots__ = ("_path",)

    def __init__(
        self,
        path: str | Path = _DEFAULT_SQLITE_PATH,
        *,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
        max_pending_spans: int = _DEFAULT_MAX_PENDING_SPANS,
        max_batch_spans: int = _DEFAULT_MAX_BATCH_SPANS,
    ) -> None:
        warnings.warn(
            "SqliteTracer is deprecated: use JsonTracer(path=...) and query "
            "the NDJSON file. Removed in 6.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(
            tracer_label="sqlite tracer",
            tracer_type_name="SqliteTracer",
            flush_interval=flush_interval,
            max_pending_spans=max_pending_spans,
            max_batch_spans=max_batch_spans,
        )
        self._path = Path(path)

    def __enter__(self) -> SqliteTracer:
        super().__enter__()
        return self

    def _thread_name(self) -> str:
        return "stario-sqlite-tracer"

    def _on_start_writer(self) -> None:
        # Fail fast on schema/path errors before the writer thread starts.
        connection = self._open_connection()
        connection.close()

    def _merge_retry_batch(
        self,
        retry_batch: list[RecordingSpan],
        queued: list[RecordingSpan],
    ) -> list[RecordingSpan]:
        if not queued:
            return retry_batch
        available = self._max_pending_spans - len(retry_batch)
        if available <= 0:
            self._record_dropped_spans(len(queued))
            self._emit_stderr(
                "sqlite tracer retry backlog full "
                f"(max={self._max_pending_spans}); dropping {len(queued)} finished spans"
            )
            return retry_batch
        if len(queued) > available:
            self._record_dropped_spans(len(queued) - available)
            self._emit_stderr(
                "sqlite tracer retry backlog full "
                f"(max={self._max_pending_spans}); dropping "
                f"{len(queued) - available} finished spans"
            )
            queued = queued[:available]
        if retry_batch:
            retry_batch.extend(queued)
            return retry_batch
        return queued

    def _writer_loop(self) -> None:
        connection: sqlite3.Connection | None = None
        retry_batch: list[RecordingSpan] = []
        shutdown_error_seen = False
        try:
            while True:
                if connection is None:
                    try:
                        connection = self._open_connection()
                        shutdown_error_seen = False
                    except Exception as exc:
                        self._record_writer_error(exc)
                        self._emit_writer_error(exc)
                        if not self._running:
                            if shutdown_error_seen:
                                return
                            shutdown_error_seen = True
                            continue
                        time.sleep(self._flush_interval)
                        continue

                if self._running:
                    self._wake.wait(self._flush_interval)
                    self._wake.clear()
                else:
                    self._wake.clear()

                queued = self._take_pending()
                pending = self._merge_retry_batch(retry_batch, queued)
                retry_batch = []

                try:
                    if pending:
                        self._persist_batches(connection, pending)
                    shutdown_error_seen = False
                except Exception as exc:
                    retry_batch = pending
                    self._record_writer_error(exc)
                    self._emit_writer_error(exc)
                    connection.close()
                    connection = None
                    if not self._running:
                        if shutdown_error_seen:
                            return
                        shutdown_error_seen = True
                    continue

                if not self._running:
                    pending = self._take_pending()
                    if pending:
                        retry_batch = self._merge_retry_batch(retry_batch, pending)
                    if retry_batch:
                        try:
                            self._persist_batches(connection, retry_batch)
                            retry_batch = []
                        except Exception as exc:
                            self._record_writer_error(exc)
                            self._emit_writer_error(exc)
                            return
                    return
        finally:
            if connection is not None:
                connection.close()

    def _write_batches_safe(self, spans: list[RecordingSpan]) -> None:
        connection = self._open_connection()
        try:
            self._persist_batches(connection, spans)
        except Exception as exc:
            self._record_writer_error(exc)
            self._emit_writer_error(exc)
        finally:
            connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA busy_timeout=5000")
        _ensure_schema(connection)
        connection.commit()
        return connection

    def _emit_writer_error(self, exc: Exception) -> None:
        self._emit_stderr(f"sqlite tracer writer error: {type(exc).__name__}: {exc}")

    def _persist_batch(
        self,
        connection: sqlite3.Connection,
        spans: list[RecordingSpan],
    ) -> None:
        span_rows: list[tuple[Any, ...]] = []
        event_rows: list[tuple[Any, ...]] = []
        link_rows: list[tuple[Any, ...]] = []

        for span in spans:
            try:
                if span.start_ns is None or span.end_ns is None:
                    raise RuntimeError("SqliteTracer can only persist finished spans")
                span_rows.append(
                    (
                        _uuid_blob(span.id),
                        _uuid_blob(span.trace_id),
                        _uuid_blob(span.parent_id),
                        span.name,
                        span.start_ns,
                        span.end_ns,
                        span.end_ns - span.start_ns,
                        "error" if span.error else "ok",
                        span.error,
                        serialize_json(span.attributes),
                    )
                )
                event_rows.extend(_event_rows(span))
                link_rows.extend(_link_rows(span))
            except Exception as exc:
                self._record_serialization_error()
                self._emit_stderr(
                    "sqlite tracer serialization error for span "
                    f"{span.id} ({span.name!r}); span omitted: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

        if not span_rows:
            return

        with connection:
            connection.executemany(_INSERT_SPANS_SQL, span_rows)
            if event_rows:
                connection.executemany(_INSERT_EVENTS_SQL, event_rows)
            if link_rows:
                connection.executemany(_INSERT_LINKS_SQL, link_rows)

    def _persist_batches(
        self,
        connection: sqlite3.Connection,
        spans: list[RecordingSpan],
    ) -> None:
        for start in range(0, len(spans), self._max_batch_spans):
            self._persist_batch(
                connection, spans[start : start + self._max_batch_spans]
            )


def sqlite_tracer_from_env() -> SqliteTracer:
    """Build a `SqliteTracer` from `STARIO_TRACERS_SQLITE*` environment variables."""
    return SqliteTracer(
        path=env_path("STARIO_TRACERS_SQLITE", _DEFAULT_SQLITE_PATH),
        flush_interval=env_float(
            "STARIO_TRACERS_SQLITE_FLUSH_INTERVAL", _DEFAULT_FLUSH_INTERVAL
        ),
        max_pending_spans=env_int(
            "STARIO_TRACERS_SQLITE_MAX_PENDING_SPANS", _DEFAULT_MAX_PENDING_SPANS
        ),
        max_batch_spans=env_int(
            "STARIO_TRACERS_SQLITE_MAX_BATCH_SPANS", _DEFAULT_MAX_BATCH_SPANS
        ),
    )
