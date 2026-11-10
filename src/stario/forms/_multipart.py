"""Streaming `multipart/form-data` parser (RFC 7578, RFC 2046 5.1).

`MultipartParser.feed()` takes body chunks in any size and yields events in
order: `PartStart` (the part headers), `bytes` (part content), and `PartEnd`.
It keeps at most one delimiter of look-behind, so a file streams through
without the whole body in memory. `read_form` decides what to keep.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote

_PREAMBLE = 0
_AFTER_DELIMITER = 1
_HEADERS = 2
_CONTENT = 3
_DONE = 4

# The rest of a delimiter line: optional transport padding, then CRLF.
_MAX_DELIMITER_LINE = 1024
# `filename*` charsets decoded as Latin-1; every other one is read as UTF-8.
_LATIN1_CHARSETS = frozenset(("iso-8859-1", "latin-1", "latin1"))


class MultipartError(ValueError):
    """The body is not valid multipart data (an answer of 400)."""


class MultipartLimitError(MultipartError):
    """A part's header block is larger than the limit (an answer of 413)."""


@dataclass(slots=True, frozen=True)
class PartStart:
    name: str
    filename: str | None
    content_type: str | None


@dataclass(slots=True, frozen=True)
class PartEnd:
    pass


_PART_END = PartEnd()


def parse_options(
    value: str, *, backslash_escapes: bool = True
) -> tuple[str, dict[str, str]]:
    """Split `type; key=value; key="quoted"` into a lowercased type and params.

    Parameter names are lowercased, and the first occurrence of a name wins.
    A quoted value keeps its content. HTTP headers (RFC 9110 5.6.4) use
    backslash escapes in it. A multipart part header does not: browsers write
    `"` as `%22` and keep `\\` as it is (a Windows path in a filename).
    """
    head, _, rest = value.partition(";")
    params: dict[str, str] = {}
    i = 0
    n = len(rest)
    while i < n:
        while i < n and rest[i] in " \t;":
            i += 1
        start = i
        while i < n and rest[i] not in "=;":
            i += 1
        key = rest[start:i].strip().lower()
        if i >= n or rest[i] == ";":
            continue
        i += 1  # "="
        while i < n and rest[i] in " \t":
            i += 1
        if i < n and rest[i] == '"':
            i += 1
            chars: list[str] = []
            while i < n and rest[i] != '"':
                if backslash_escapes and rest[i] == "\\" and i + 1 < n:
                    i += 1
                chars.append(rest[i])
                i += 1
            i += 1  # closing quote
            item = "".join(chars)
        else:
            start = i
            while i < n and rest[i] != ";":
                i += 1
            item = rest[start:i].strip()
        if key and key not in params:
            params[key] = item
    return head.strip().lower(), params


# Part header blocks up to this size are memoized: a form posts the same
# few blocks (`form-data; name="title"`) on every submission.
_CACHED_HEADER_BYTES = 512


def _parse_part_headers(block: bytes) -> PartStart:
    """The `PartStart` of one part header block (`MultipartError` if invalid)."""
    if len(block) <= _CACHED_HEADER_BYTES:
        return _parse_part_headers_cached(block)
    return _parse_part_headers_uncached(block)


def _parse_part_headers_uncached(block: bytes) -> PartStart:
    disposition: str | None = None
    content_type: str | None = None
    for line in block.split(b"\r\n"):
        name, sep, value = line.partition(b":")
        if not sep or not name or name[:1] in b" \t":
            raise MultipartError("Invalid part header line")
        key = name.strip().lower()
        text = value.strip().decode("utf-8", "replace")
        if key == b"content-disposition":
            disposition = text
        elif key == b"content-type":
            content_type = text
    if disposition is None:
        raise MultipartError("Part has no Content-Disposition")
    kind, params = parse_options(disposition, backslash_escapes=False)
    if kind != "form-data" or "name" not in params:
        raise MultipartError("Part is not form-data with a name")
    filename = params.get("filename")
    if filename is not None:
        filename = _unescape_quoted(filename)
    elif "filename*" in params:
        # RFC 5987 (charset''percent-encoded), which RFC 7578 forbids but
        # some non-browser clients still send.
        # Only the charsets RFC 8187 requires: the client must not choose
        # the codec (punycode decodes in quadratic time; idna raises).
        charset, _, encoded = params["filename*"].partition("''")
        if charset.strip().lower() not in _LATIN1_CHARSETS:
            charset = "utf-8"
        filename = unquote(encoded, encoding=charset, errors="replace")
    return PartStart(_unescape_quoted(params["name"]), filename, content_type)


# PartStart is frozen, and an invalid block raises, which is not cached.
_parse_part_headers_cached = lru_cache(maxsize=256)(_parse_part_headers_uncached)


def _unescape_quoted(value: str) -> str:
    # HTML multipart/form-data encoding: browsers (and curl) write `"`, CR
    # and LF in a name or filename as %22, %0D and %0A. Only these three.
    return value.replace("%22", '"').replace("%0D", "\r").replace("%0A", "\n")


class MultipartParser:
    """Incremental parser for one `multipart/form-data` body."""

    __slots__ = ("_buf", "_delimiter", "_max_header_bytes", "_state")

    def __init__(self, boundary: bytes, *, max_header_bytes: int) -> None:
        self._delimiter = b"\r\n--" + boundary
        # The first delimiter may start the body with no CRLF before it.
        self._buf = bytearray(b"\r\n")
        self._max_header_bytes = max_header_bytes
        self._state = _PREAMBLE

    def feed(self, chunk: bytes) -> Iterator[PartStart | bytes | PartEnd]:
        if self._state == _DONE:
            return  # The epilogue after the close delimiter is ignored.
        buf = self._buf
        buf += chunk
        delimiter = self._delimiter
        keep = len(delimiter) - 1
        while True:
            state = self._state
            if state == _CONTENT:
                at = buf.find(delimiter)
                if at < 0:
                    safe = len(buf) - keep
                    if safe > 0:
                        yield bytes(buf[:safe])
                        del buf[:safe]
                    return
                if at:
                    yield bytes(buf[:at])
                del buf[: at + len(delimiter)]
                yield _PART_END
                self._state = _AFTER_DELIMITER
            elif state == _PREAMBLE:
                at = buf.find(delimiter)
                if at < 0:
                    if len(buf) > keep:
                        del buf[: len(buf) - keep]
                    return
                del buf[: at + len(delimiter)]
                self._state = _AFTER_DELIMITER
            elif state == _AFTER_DELIMITER:
                if len(buf) < 2:
                    return
                if buf[:2] == b"--":
                    self._state = _DONE
                    buf.clear()
                    return
                end = buf.find(b"\r\n")
                if end < 0:
                    if len(buf) > _MAX_DELIMITER_LINE:
                        raise MultipartError("Invalid delimiter line")
                    return
                if buf[:end].strip(b" \t"):
                    raise MultipartError("Invalid delimiter line")
                del buf[: end + 2]
                self._state = _HEADERS
            elif state == _HEADERS:
                if buf[:2] == b"\r\n":
                    raise MultipartError("Part has no Content-Disposition")
                end = buf.find(b"\r\n\r\n")
                if end < 0:
                    if len(buf) > self._max_header_bytes:
                        raise MultipartLimitError("Part headers too large")
                    return
                if end > self._max_header_bytes:
                    raise MultipartLimitError("Part headers too large")
                start = _parse_part_headers(bytes(buf[:end]))
                del buf[: end + 4]
                self._state = _CONTENT
                yield start
            else:
                return

    def close(self) -> None:
        """Raise if the body ended before its close delimiter."""
        if self._state != _DONE:
            raise MultipartError("Multipart body ended early")
