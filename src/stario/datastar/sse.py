"""Datastar Server-Sent Events bound to one response writer.

Create one `SSE` per response: `sse = SSE(w)`. The stream opens when you call
`sse.open()` or when the first event is written.
"""

from collections.abc import Mapping
from typing import Any, Literal

from stario.exceptions import StarioError, StarioRuntime
from stario.http.redirect import normalized_location
from stario.http.writer import Writer
from stario.json import JsonValue
from stario.json import dumps as json_dumps
from stario.json import dumps_bytes as json_dumps_bytes
from stario.markup.render import render_bytes

from .format import expand_signal_paths, require_mapping

type JsonObject = Mapping[str, JsonValue]
type PatchMode = Literal[
    "outer", "inner", "replace", "prepend", "append", "before", "after"
]
type Namespace = Literal["svg", "mathml"]

_PATCH_MODE_NAMES = (
    "outer",
    "inner",
    "replace",
    "prepend",
    "append",
    "before",
    "after",
)
_PATCH_MODES = frozenset(_PATCH_MODE_NAMES)
_PATCH_MODE_HELP = "Use one of: " + ", ".join(_PATCH_MODE_NAMES) + "."
_NAMESPACES = ("svg", "mathml")
_EVENT_PATCH_ELEMENTS = b"event: datastar-patch-elements"
_EVENT_PATCH_SIGNALS = b"event: datastar-patch-signals"
# Datastar SDK default; `retry=1000` means "do not emit the field".
_SSE_RETRY_DEFAULT = 1000


def _sse_field_value(name: str, value: str) -> bytes:
    """Encode one logical SSE field value; payload newlines are handled elsewhere."""
    if "\r" in value or "\n" in value:
        raise StarioError(
            "Datastar SSE field values must not contain line breaks",
            context={"field": name, "value": value[:100]},
            help_text=(
                "Only event payloads may be multiline. Keep selector and option "
                "values on one line."
            ),
        )
    return value.encode()


def _sse_payload_lines(data: bytes) -> list[bytes]:
    """Split an SSE payload on CR, LF, or CRLF (HTML event-stream line endings).

    Rendered markup and compact JSON carry no line breaks, so the common
    case is one `data:` line and no copies of the payload.
    """
    # Byte values, not b"\n": `int in bytes` is one memchr, while a bytes
    # needle goes through the buffer protocol (several times slower).
    if 10 not in data and 13 not in data:  # LF, CR
        return [data]
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")


def _sse_event_meta(event_id: str | None, retry: int | None) -> list[bytes]:
    """`id:`/`retry:` fields placed right after the `event:` line."""
    lines: list[bytes] = []
    if event_id is not None:
        if "\x00" in event_id:
            raise StarioError(
                "Datastar SSE event_id must not contain NUL",
                context={"event_id": event_id[:100]},
            )
        lines.append(b"id: " + _sse_field_value("event_id", event_id))
    if retry is not None:
        if type(retry) is not int or retry <= 0:
            raise StarioError(
                "Datastar SSE retry must be a positive int",
                context={"retry": repr(retry)},
                help_text="Milliseconds for the client to wait before reconnecting.",
            )
        if retry != _SSE_RETRY_DEFAULT:
            lines.append(f"retry: {retry}".encode("ascii"))
    return lines


class SSE:
    """Datastar SSE response bound to one `Writer`.

    `open()` sends headers immediately. If you skip it, the first event opens the
    stream. Event bytes still go through `Writer.write()`, so normal streaming
    compression policy applies. `patch_signals()` keys pass through to Datastar
    unchanged; names starting with `_` are local signals the browser does not
    send. A dot in a key is a nested path — `{"ui.open": True}` patches
    `$ui.open`.
    """

    __slots__ = ("w",)

    def __init__(self, w: Writer) -> None:
        self.w = w

    def open(self) -> None:
        """Send `text/event-stream` headers now, before the first event."""
        self._prepare_headers()
        if not self.w.started:
            self.w.write_headers(200)

    def patch_elements(
        self,
        content: Any,
        *,
        mode: PatchMode | None = None,
        selector: str | None = None,
        namespace: Namespace | None = None,
        view_transition: bool = False,
        view_transition_selector: str | None = None,
        event_id: str | None = None,
        retry: int | None = None,
    ) -> None:
        """Patch DOM elements with Stario markup, or trusted `bytes` / `str` HTML.

        Markup built with `h.*` is rendered with escaping. `bytes` and `str`
        are sent as they are, like `w.html(str)`: they are trusted HTML, so
        never pass user text this way — wrap it in an element instead.
        """
        if mode is not None and mode not in _PATCH_MODES:
            raise StarioError(
                "Unknown Datastar patch mode",
                context={"mode": str(mode)},
                help_text=_PATCH_MODE_HELP,
            )
        if namespace is not None and namespace not in _NAMESPACES:
            raise StarioError(
                "Unknown Datastar patch namespace",
                context={"namespace": str(namespace)},
                help_text="Use 'svg', 'mathml', or omit namespace.",
            )

        lines = [_EVENT_PATCH_ELEMENTS, *_sse_event_meta(event_id, retry)]
        if mode is not None:
            lines.append(f"data: mode {mode}".encode("ascii"))
        if selector:
            lines.append(b"data: selector " + _sse_field_value("selector", selector))
        if namespace:
            lines.append(f"data: namespace {namespace}".encode("ascii"))
        if view_transition:
            lines.append(b"data: useViewTransition true")
        if view_transition_selector is not None:
            lines.append(
                b"data: viewTransitionSelector "
                + _sse_field_value("viewTransitionSelector", view_transition_selector)
            )

        if isinstance(content, bytes):
            html_bytes = content
        elif isinstance(content, str):
            html_bytes = content.encode("utf-8")
        else:
            html_bytes = render_bytes(content)

        # SSE represents multiline payloads as repeated `data:` lines.
        # Split on CR, LF, and CRLF: a bare CR is a line terminator in the
        # event-stream parser and must not remain inside a `data:` field.
        for line in _sse_payload_lines(html_bytes):
            lines.append(b"data: elements " + line)

        self._prepare_headers()
        self.w.write(b"\n".join(lines) + b"\n\n")

    def patch_signals(
        self,
        payload: JsonObject,
        *,
        only_if_missing: bool = False,
        event_id: str | None = None,
        retry: int | None = None,
    ) -> None:
        """Patch Datastar signals from a JSON-shaped mapping.

        Dotted keys expand to nested objects, so `{"ui.open": True}`
        patches `$ui.open`.
        """
        data = expand_signal_paths(require_mapping("patch_signals", payload))
        json_bytes = json_dumps_bytes(data)
        lines = [_EVENT_PATCH_SIGNALS, *_sse_event_meta(event_id, retry)]
        if only_if_missing:
            lines.append(b"data: onlyIfMissing true")
        for line in _sse_payload_lines(json_bytes):
            lines.append(b"data: signals " + line)
        self._prepare_headers()
        self.w.write(b"\n".join(lines) + b"\n\n")

    def navigate(
        self,
        url: str,
        *,
        event_id: str | None = None,
        retry: int | None = None,
    ) -> None:
        """Navigate the browser from an SSE response; not an HTTP redirect."""
        safe_url = json_dumps(normalized_location(url))
        self.execute_script(
            f"setTimeout(() => window.location = {safe_url});",
            event_id=event_id,
            retry=retry,
        )

    def execute_script(
        self,
        code: str,
        *,
        auto_remove: bool = True,
        event_id: str | None = None,
        retry: int | None = None,
    ) -> None:
        """Run trusted developer-authored JavaScript by appending a script element.

        The code is streamed verbatim; do not interpolate untrusted input.
        """
        code_bytes = code.encode("utf-8")
        if auto_remove:
            script = b'<script data-effect="el.remove();">' + code_bytes + b"</script>"
        else:
            script = b"<script>" + code_bytes + b"</script>"
        self.patch_elements(
            script,
            mode="append",
            selector="body",
            event_id=event_id,
            retry=retry,
        )

    def remove(
        self,
        selector: str,
        *,
        event_id: str | None = None,
        retry: int | None = None,
    ) -> None:
        """Remove nodes matching `selector`."""
        lines = [
            _EVENT_PATCH_ELEMENTS,
            *_sse_event_meta(event_id, retry),
            b"data: mode remove",
            b"data: selector " + _sse_field_value("selector", selector),
        ]
        self._prepare_headers()
        self.w.write(b"\n".join(lines) + b"\n\n")

    def ping(self, comment: str = "") -> None:
        """Write an SSE comment (`: ...`) to keep proxies and clients alive.

        A heartbeat loop looks like:

        ```python
        async with c.alive():
            while True:
                sse.ping()
                await asyncio.sleep(15)
        ```
        """
        self._prepare_headers()
        self.w.write(b": " + _sse_field_value("comment", comment) + b"\n\n")

    def _prepare_headers(self) -> None:
        w = self.w
        if w.completed:
            raise StarioRuntime(
                "Cannot send SSE events after the response is completed",
                help_text=(
                    "Create and use `SSE(w)` before response helpers like "
                    "`w.html()`, `w.redirect()`, `w.empty()`, or `w.end()`."
                ),
                example=(
                    "from stario.datastar import SSE\n\n"
                    "sse = SSE(w)\n"
                    "sse.patch_elements(view)\n"
                    "sse.patch_signals({'count': 1})"
                ),
            )

        if w.started:
            content_type = w.headers.unsafe_get(b"content-type")
            if content_type is None or b"text/event-stream" not in content_type.lower():
                raise StarioRuntime(
                    "Cannot send Datastar SSE events: response already started without "
                    "Content-Type: text/event-stream",
                    help_text=(
                        "Create `SSE(w)` and send SSE before any other body or "
                        "non-SSE Content-Type."
                    ),
                )
            return

        w.headers.unsafe_set(b"content-type", b"text/event-stream")
        w.headers.unsafe_set(b"cache-control", b"no-cache")
