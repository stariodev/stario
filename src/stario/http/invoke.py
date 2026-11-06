"""Handler-task finish: record failures on the span, send 500, close the span.

The framework's own exceptions (`RedirectException`, `RequestBodyError`) map to
HTTP when nothing was sent; everything else is recorded and becomes a 500.
A write-then-raise keeps the bytes already on the wire. Cancellation still aborts.
A handler that returns with no bytes is a span error while the client is still
connected. A client that already left is not: abort, mark the span with
`request.client_left`, and do not fail or log.
Without a recording span the failure falls back to the `stario.http` logger —
otherwise the crash would leave no trace at all.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from stario.exceptions import RedirectException, RequestBodyError, StarioError
from stario.http.context import Context
from stario.http.writer import Writer
from stario.telemetry.core import Span
from stario.telemetry.spans import NoOpSpan

_log = logging.getLogger("stario.http")

_INTERNAL_ERROR = b"Internal Server Error"
_PLAIN = b"text/plain; charset=utf-8"


def _client_left(c: Context, w: Writer) -> bool:
    """True when this request's client is gone.

    App shutdown alone is not enough: a connected client still needs a response.
    """
    if c.disconnected:
        return True
    return w.closing and not c.shutting_down


def _finish_incomplete(w: Writer) -> None:
    """500 if the handler never started a response; otherwise abort."""
    if w.completed:
        return
    if w.started:
        w.abort()
        return
    w.headers.remove("transfer-encoding")
    w.headers.set("connection", "close")
    w.respond(_INTERNAL_ERROR, _PLAIN, 500)


def _answer_framework_exception(w: Writer, exc: BaseException) -> bool:
    """Map Stario's own exceptions to a response; ``False`` = log + 500 path."""
    try:
        if isinstance(exc, RedirectException):
            w.redirect(exc.location, exc.status_code)
            return True
        if isinstance(exc, RequestBodyError):
            if exc.status_code == 408:
                # The upload stalled; the connection is not worth keeping
                # (RFC 9110 15.5.9: a 408 carries the close option).
                w.headers.set("connection", "close")
            w.text(exc.detail, exc.status_code)
            return True
    except StarioError:
        return False
    return False


def finish_request_span(
    span: Span | None,
    *,
    status: int | None = None,
    method: str | None = None,
    path: str | None = None,
) -> None:
    """Start and end a request span. Skips `None` / `NoOpSpan`. Does not `fail`."""
    if span is None or type(span) is NoOpSpan:
        return
    span.start()
    if method is not None or path is not None:
        attrs: dict[str, str] = {}
        if method is not None:
            attrs["request.method"] = method
        if path is not None:
            attrs["request.path"] = path
        span.attrs(attrs)
    if status is not None:
        span.attr("response.status_code", status)
    span.end()


def on_handler_done(c: Context, w: Writer, task: asyncio.Task[Any]) -> None:
    """Run after the handler task finishes (success, failure, or cancel)."""
    if not task.done():
        return
    span = c.span
    record = type(span) is not NoOpSpan

    if task.cancelled():
        if not w.completed:
            if record and _client_left(c, w):
                # alive() cancels the handler on disconnect; not a failure.
                span.attr("request.client_left", True)
            w.abort()
    else:
        exc = task.exception()
        if exc is not None:
            if w.started or not _answer_framework_exception(w, exc):
                if record:
                    # The span carries status, message, and traceback.
                    span.fail(str(exc) or type(exc).__name__)
                    span.exception(exc)
                else:
                    _log.error("Handler failed", exc_info=exc)
                _finish_incomplete(w)
        elif w.started and not w.completed:
            # Streamed body, clean return: finish the frame the handler left open.
            w.end()
        elif not w.completed:
            if _client_left(c, w):
                # A 500 here is text/plain. Datastar retries that, so a
                # client that already left becomes a request storm.
                if record:
                    span.attr("request.client_left", True)
                w.abort()
            else:
                if record:
                    span.fail(
                        "Handler returned without sending a response "
                        f"({c.req.method} {c.req.path})"
                    )
                else:
                    _log.error(
                        "Handler returned without sending a response (%s %s)",
                        c.req.method,
                        c.req.path,
                    )
                _finish_incomplete(w)

    if record:
        finish_request_span(span, status=w.status_code)
