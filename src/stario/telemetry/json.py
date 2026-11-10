"""Emits finished spans as newline-delimited JSON for log pipelines and collectors."""

from __future__ import annotations

import os
import sys
import threading
from json.encoder import encode_basestring
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

import stario.json as _stario_json
from stario._env import env_float, env_int, env_optional_str

from .buffered import _BufferedTracer
from .formatters import dumps_json
from .spans import RecordingSpan

_DEFAULT_FLUSH_INTERVAL = 0.125
_DEFAULT_MAX_PENDING_SPANS = 65536
_DEFAULT_MAX_BATCH_SPANS = 512


def _uuid_str(value: UUID) -> str:
    """`str(value)`: the 32 hex digits with the four dashes."""
    h = value.hex
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _serialize_span(span: RecordingSpan) -> dict[str, Any]:
    """Serialize one finished span."""
    start_ns = span.start_ns
    end_ns = span.end_ns
    if start_ns is None or end_ns is None:
        raise RuntimeError("JsonTracer can only serialize finished spans")
    span_id = _uuid_str(span.id)
    # A root's trace id is its own id object; format it once.
    trace_id = span_id if span.trace_id is span.id else _uuid_str(span.trace_id)
    error = span.error
    result: dict[str, Any] = {
        "span_id": span_id,
        "trace_id": trace_id,
        "name": span.name,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "duration_ns": end_ns - start_ns,
        "status": "ok" if error is None else "error",
    }

    if span.parent_id is not None:
        result["parent_id"] = _uuid_str(span.parent_id)

    if error is not None:
        result["error"] = error

    if span.attributes:
        result["attributes"] = span.attributes

    if span.events:
        events: list[dict[str, Any]] = []
        for event in span.events:
            ev: dict[str, Any] = {
                "time_ns": event.time_ns,
                "name": event.name,
            }
            if event.attributes:
                ev["attributes"] = event.attributes
            if event.body is not None:
                ev["body"] = event.body
            events.append(ev)
        result["events"] = events

    if span.links:
        links: list[dict[str, Any]] = []
        for link in span.links:
            item: dict[str, Any] = {
                "name": link.name,
                "span_id": _uuid_str(link.span_id),
            }
            if link.attributes:
                item["attributes"] = link.attributes
            links.append(item)
        result["links"] = links

    return result


def _stdlib_codec() -> bool:
    """True while `stario.json` encodes with its standard-library default."""
    return _stario_json._encode is _stario_json._stdlib_encode


def _fast_line(span: RecordingSpan) -> str | None:
    """`dumps_json(_serialize_span(span))` for the common span, or None.

    Only valid with the standard-library codec. The fixed fields are
    formatted directly (exact `str` / `int` only, so the text is what the
    encoder would write) and only `attributes` goes through the encoder:
    about half the cost on the writer thread, which holds the GIL while it
    works. Spans with events or links, or anything unusual, take the
    general path.
    """
    start_ns = span.start_ns
    end_ns = span.end_ns
    name = span.name
    error = span.error
    if (
        type(start_ns) is not int
        or type(end_ns) is not int
        or type(name) is not str
        or span.events
        or span.links
        or (error is not None and type(error) is not str)
    ):
        return None
    span_id = _uuid_str(span.id)
    trace_id = span_id if span.trace_id is span.id else _uuid_str(span.trace_id)
    line = (
        f'{{"span_id":"{span_id}","trace_id":"{trace_id}",'
        f'"name":{encode_basestring(name)},"start_ns":{start_ns},'
        f'"end_ns":{end_ns},"duration_ns":{end_ns - start_ns},'
        f'"status":"{"ok" if error is None else "error"}"'
    )
    if span.parent_id is not None:
        line += f',"parent_id":"{_uuid_str(span.parent_id)}"'
    if error is not None:
        line += f',"error":{encode_basestring(error)}'
    if span.attributes:
        line += f',"attributes":{dumps_json(span.attributes)}'
    return line + "}"


class JsonTracer(_BufferedTracer):
    """NDJSON span sink writing one JSON object per line.

    Enter before `create()`. A background thread batches finished spans;
    `flush_each` controls whether each batch flushes the output stream.
    Overflow drops spans and increments `stats().dropped_spans`.

    Spans go to `output`, or stdout when it is omitted. `path` names an
    NDJSON file instead: it opens in append mode on enter — missing parent
    directories are created — and the tracer flushes and closes it on exit.
    `output` and `path` cannot both be given.
    """

    __slots__ = ("_flush_each", "_output", "_path", "_write_lock")

    def __init__(
        self,
        output: TextIO | None = None,
        *,
        path: str | os.PathLike[str] | None = None,
        flush_each: bool = True,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
        max_pending_spans: int = _DEFAULT_MAX_PENDING_SPANS,
        max_batch_spans: int = _DEFAULT_MAX_BATCH_SPANS,
    ) -> None:
        if output is not None and path is not None:
            raise ValueError("JsonTracer takes output or path, not both")
        super().__init__(
            tracer_label="json tracer",
            tracer_type_name="JsonTracer",
            flush_interval=flush_interval,
            max_pending_spans=max_pending_spans,
            max_batch_spans=max_batch_spans,
        )
        self._path = Path(path) if path is not None else None
        self._output: TextIO = output if output is not None else sys.stdout
        self._write_lock = threading.Lock()
        self._flush_each = flush_each

    def __enter__(self) -> JsonTracer:
        super().__enter__()
        return self

    def _on_start_writer(self) -> None:
        # Fail fast on path errors before the writer thread starts.
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._output = self._path.open("a", encoding="utf-8", newline="\n")

    def _on_exit(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        try:
            with self._write_lock:
                self._output.flush()
        except Exception as exc:
            self._record_writer_error(exc)
            self._emit_stderr(f"json tracer flush error: {type(exc).__name__}: {exc}")
        if self._path is not None:
            try:
                self._output.close()
            except Exception as exc:
                self._record_writer_error(exc)
                self._emit_stderr(
                    f"json tracer close error: {type(exc).__name__}: {exc}"
                )

    def _thread_name(self) -> str:
        return "stario-json-tracer"

    def _write_batch(self, spans: list[RecordingSpan]) -> None:
        """Serialize and write one finished batch."""
        lines: list[str] = []
        fast = _stdlib_codec()
        for span in spans:
            try:
                line = _fast_line(span) if fast else None
                if line is None:
                    line = dumps_json(_serialize_span(span))
                lines.append(line)
            except Exception as exc:
                self._record_serialization_error()
                self._emit_stderr(
                    "json tracer serialization error for span "
                    f"{span.id} ({span.name!r}); "
                    f"span omitted: {type(exc).__name__}: {exc}"
                )
                continue

        if not lines:
            return

        payload = "\n".join(lines)
        payload += "\n"
        with self._write_lock:
            self._output.write(payload)
            if self._flush_each:
                self._output.flush()


def json_tracer_from_env() -> JsonTracer:
    """Build a `JsonTracer` from `STARIO_TRACERS_JSON*` environment variables.

    `STARIO_TRACERS_JSON` names the NDJSON file; unset or empty writes to
    stdout.
    """
    return JsonTracer(
        path=env_optional_str("STARIO_TRACERS_JSON"),
        flush_interval=env_float(
            "STARIO_TRACERS_JSON_FLUSH_INTERVAL", _DEFAULT_FLUSH_INTERVAL
        ),
        max_pending_spans=env_int(
            "STARIO_TRACERS_JSON_MAX_PENDING_SPANS", _DEFAULT_MAX_PENDING_SPANS
        ),
        max_batch_spans=env_int(
            "STARIO_TRACERS_JSON_MAX_BATCH_SPANS", _DEFAULT_MAX_BATCH_SPANS
        ),
    )
