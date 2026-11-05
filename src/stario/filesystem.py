"""A directory on disk, exposed at a URL prefix.

`Assets` fingerprints names (immutable, 307 from the logical path).
`Files` serves the live path (ETag, 304). Build at import. Call `href()`
there. Call `await attach(app)` in bootstrap (register + load).

    STATIC = Assets("./static", "/static")
    UPLOADS = Files("./uploads", "/data")
    css = STATIC.href("css/app.css")

    async def bootstrap(app, span):
        span.attrs(await STATIC.attach(app))
        await UPLOADS.attach(app)
        yield

The `Assets` directory must exist at construction.
"""

import asyncio
import errno
import os
import stat as stat_module
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from os import stat_result
from pathlib import Path
from typing import Any, Final, NamedTuple, TypeGuard

from stario.exceptions import StarioError, StarioRuntime
from stario.http.app import App
from stario.http.compression import (
    CompressionConfig,
    content_type_is_compressible,
    merge_vary,
    negotiate_content_encoding,
)
from stario.http.context import Context
from stario.http.headers import RequestHeaders, encode_header_value
from stario.http.route import Route, append_query_fragment, public_prefix
from stario.http.writer import Writer
from stario_cython.xxh64 import xxh64

DEFAULT_TYPE: Final = b"application/octet-stream"
CONTENT_TYPES: Final = {
    ".html": b"text/html; charset=utf-8",
    ".htm": b"text/html; charset=utf-8",
    ".css": b"text/css; charset=utf-8",
    ".js": b"application/javascript; charset=utf-8",
    ".mjs": b"application/javascript; charset=utf-8",
    ".json": b"application/json; charset=utf-8",
    ".xml": b"application/xml; charset=utf-8",
    ".txt": b"text/plain; charset=utf-8",
    ".md": b"text/markdown; charset=utf-8",
    ".png": b"image/png",
    ".jpg": b"image/jpeg",
    ".jpeg": b"image/jpeg",
    ".gif": b"image/gif",
    ".svg": b"image/svg+xml; charset=utf-8",
    ".ico": b"image/x-icon",
    ".webp": b"image/webp",
    ".avif": b"image/avif",
    ".woff": b"font/woff",
    ".woff2": b"font/woff2",
    ".ttf": b"font/ttf",
    ".otf": b"font/otf",
    ".pdf": b"application/pdf",
    ".zip": b"application/zip",
    ".mp3": b"audio/mpeg",
    ".mp4": b"video/mp4",
    ".webm": b"video/webm",
    ".wasm": b"application/wasm",
}
# Precompressed variants a static file can be served as, best first: br wins
# ties because these bytes are encoded once at load, so only wire size counts.
CODECS: Final = (b"br", b"zstd", b"gzip")
PATH_ERRNOS: Final = frozenset((errno.EACCES, errno.ENOENT, errno.ENOTDIR, errno.ELOOP))
OPEN_FLAGS: Final = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
if hasattr(os, "pread"):
    _pread = os.pread
else:  # Windows has no os.pread; each fd here is exclusive to one response.

    def _pread(fd: int, size: int, offset: int, /) -> bytes:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, size)


COMPRESSION: Final = CompressionConfig(
    min_size=256,
    zstd_level=9,
    zstd_window_log=21,
    brotli_level=9,
    brotli_window_log=22,
    gzip_level=7,
    gzip_window_bits=15,
)
HASH_CHUNK: Final = 4 << 20
STREAM_SPAN: Final = 1 << 20


UNSAT: Final = object()


class _Range(NamedTuple):
    start: int
    end: int


@dataclass(slots=True, frozen=True)
class _Hashed:
    hashed_path: str
    url: str
    source: Path
    size: int
    modified_ns: int
    digest: str


@dataclass(slots=True, frozen=True)
class _Cached:
    """Held body. `identity` is live-only. Empty `encodings` means identity only."""

    content_type: bytes
    content: bytes
    identity: tuple[int, int, int, int] | None = None
    encodings: dict[bytes, bytes] = field(default_factory=dict[bytes, bytes])


def _identity(stat: stat_result) -> tuple[int, int, int, int]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _is_relative(path: object) -> TypeGuard[str]:
    return (
        type(path) is str
        and bool(path)
        and "\0" not in path
        and "\\" not in path
        and not path.startswith("/")
        and not any(part in {"", ".", ".."} for part in path.split("/"))
    )


def _bad_path(path: object) -> StarioError:
    return StarioError(
        "File path must be a non-empty relative POSIX path",
        context={"path": path},
        help_text="Use a path like 'css/app.css'. Do not use '..', a leading slash, or backslashes.",
        example='files.href("css/app.css")',
    )


def _missing(path: str | Path) -> StarioError:
    return StarioError(
        "File not found",
        context={"path": str(path)},
        help_text="The path must name a regular file inside the directory.",
        example='root.href("css/app.css")',
    )


def _changed(path: str | Path) -> StarioRuntime:
    return StarioRuntime(
        "File changed while hashing",
        context={"path": str(path)},
        help_text=(
            "Finish writing the file before href() or load(). "
            "Restart after an Assets file changes."
        ),
    )


def _header(value: str | bytes, *, field: str) -> bytes:
    try:
        text = value.decode("latin-1") if isinstance(value, bytes) else value
        return encode_header_value(text)
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise StarioError(
            "Header value must be text or bytes without control characters",
            context={"field": field},
            help_text="Pass a str or latin-1 bytes with no CR, LF, or NUL.",
        ) from exc


def _fingerprint(path: Path) -> str:
    hasher = xxh64()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def _open_regular(path: Path | str) -> tuple[int, stat_result] | None:
    """Open a regular file without following a final symlink. Caller closes the fd."""
    try:
        fd = os.open(path, OPEN_FLAGS)
    except OSError as exc:
        if exc.errno in PATH_ERRNOS:
            return None
        raise
    try:
        file_stat = os.fstat(fd)
        if not stat_module.S_ISREG(file_stat.st_mode):
            os.close(fd)
            return None
    except BaseException:
        os.close(fd)
        raise
    return fd, file_stat


def _close_unclaimed(future: asyncio.Future[Any]) -> None:
    """Close the fd a worker thread opened after its awaiting task was cancelled."""
    try:
        opened = future.result()
    except BaseException:
        return
    if opened is not None:
        os.close(opened[0])


async def _to_thread_open[P](
    open_in_thread: Callable[[P], tuple[Any, ...] | None], arg: P
) -> tuple[Any, ...] | None:
    """Resolve/open in a worker thread; a cancelled waiter still frees the fd."""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, open_in_thread, arg)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        future.add_done_callback(_close_unclaimed)
        raise


def _parse_range(header: bytes, size: int) -> _Range | object | None:
    """Inclusive start/end, `UNSAT`, or `None` when Range is absent or ignored."""
    if not header:
        return None
    unit, sep, spec = header.partition(b"=")
    spec = spec.strip()
    if sep != b"=" or unit.strip().lower() != b"bytes":
        return None
    if b"," in spec or b"-" not in spec or size <= 0:
        return UNSAT
    start_text, _, end_text = spec.partition(b"-")
    try:
        if not start_text:
            length = int(end_text)
            return _Range(max(size - length, 0), size - 1) if length > 0 else UNSAT
        start = int(start_text)
        if start < 0 or start >= size:
            return UNSAT
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return UNSAT
    return UNSAT if end < start else _Range(start, min(end, size - 1))


def _etag_matches(value: bytes, etag: bytes) -> bool:
    current = etag.removeprefix(b"W/")
    return any(
        item.strip() == b"*" or item.strip().removeprefix(b"W/") == current
        for item in value.split(b",")
    )


def _etag(base: str, encoding: bytes | None) -> bytes:
    """Strong validator, distinct per content-coding (RFC 9110 §8.8.1)."""
    if encoding is None:
        return f'"{base}"'.encode()
    return f'"{base}-{encoding.decode()}"'.encode()


def _stat_etag(stat: stat_result, encoding: bytes | None) -> bytes:
    return _etag(
        f"{stat.st_dev:x}-{stat.st_ino:x}-{stat.st_mtime_ns:x}-{stat.st_size:x}",
        encoding,
    )


def _last_modified(modified: float) -> bytes:
    return format_datetime(
        datetime.fromtimestamp(int(modified), UTC), usegmt=True
    ).encode("ascii")


def _range_header(
    request: RequestHeaders, identity_etag: bytes, modified: float
) -> bytes:
    range_header = request.unsafe_get(b"range", b"")
    if (if_range := request.unsafe_get(b"if-range")) is None:
        return range_header
    stripped = if_range.strip()
    if stripped.startswith(b"W/"):
        return b""
    if stripped.startswith(b'"'):
        return range_header if stripped == identity_etag else b""
    return range_header if _date_matches(stripped, modified) else b""


def _fresh(request: RequestHeaders, etag: bytes, modified: float) -> bool:
    if_none_match = request.unsafe_get(b"if-none-match")
    if if_none_match is None:
        since = request.unsafe_get(b"if-modified-since")
        return since is not None and _date_matches(since, modified)
    return _etag_matches(if_none_match, etag)


def _not_found(writer: Writer) -> None:
    writer.headers.unsafe_set(b"x-content-type-options", b"nosniff")
    writer.text("Not Found", 404)


def _date_matches(value: bytes, modified: float) -> bool:
    try:
        parsed = parsedate_to_datetime(value.decode("ascii"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(modified) <= int(parsed.timestamp())
    except (UnicodeDecodeError, OverflowError, TypeError, ValueError):
        return False


def _empty_stats() -> dict[str, int]:
    return {
        "files": 0,
        "cached_files": 0,
        "streamed_files": 0,
        "budget_skipped_files": 0,
        "retained_bytes": 0,
        "compressed_files": 0,
    }


def _tally(stats: dict[str, int], entry: _Cached) -> None:
    stats["cached_files"] += 1
    stats["retained_bytes"] += len(entry.content) + sum(
        len(data) for data in entry.encodings.values()
    )
    if entry.encodings:
        stats["compressed_files"] += 1


async def _stream(writer: Writer, fd: int, start: int, length: int) -> None:
    """Send `length` bytes from `start` in an already-open fd. Caller closes it.

    Regular files have no asyncio readiness API (`epoll` reports them ready).
    Each span is one `os.pread` in a worker thread.
    """
    offset = start
    remaining = length
    while remaining:
        chunk = await asyncio.to_thread(_pread, fd, min(STREAM_SPAN, remaining), offset)
        if not chunk:
            break
        offset += len(chunk)
        remaining -= len(chunk)
        if writer.closing:
            return
        writer.write(chunk)
        # Bound memory for slow clients: do not read ahead of the socket.
        await writer.drain()
    writer.end()


class _Mount:
    """Directory, prefix, and the shared register / load / read path."""

    _pin_root = False
    _default_prefix = "/data"
    _default_cache_control = "private, no-cache"
    _default_codecs: tuple[str, ...] = ()

    __slots__ = (
        "_cache",
        "_cache_control",
        "_compressors",
        "_content_types",
        "_loaded",
        "_min_size",
        "_route",
        "directory",
        "follow_symlinks",
        "include_hidden",
        "max_bytes",
        "max_file_size",
        "prefix",
        "stats",
    )

    stats: dict[str, int]

    def __init__(
        self,
        directory: Path | str,
        url_prefix: str | None = None,
        *,
        include_hidden: bool = False,
        follow_symlinks: bool = False,
        content_types: Mapping[str, str | bytes] | None = None,
    ) -> None:
        kind = type(self).__name__
        if any(type(flag) is not bool for flag in (include_hidden, follow_symlinks)):
            raise StarioError(
                "include_hidden and follow_symlinks must be bool",
                help_text="Pass True or False, not 1 or 0.",
            )
        self.directory = (
            Path(directory).resolve() if self._pin_root else Path(directory).absolute()
        )
        if self._pin_root and not self.directory.is_dir():
            raise StarioError(
                f"{kind} directory not found: {self.directory}",
                context={"path": str(self.directory)},
                help_text=f"Create the directory before {kind}(...).",
            )
        if url_prefix is not None and type(url_prefix) is not str:
            raise StarioError(
                "URL prefix must be a string",
                context={"url_prefix": url_prefix},
                help_text='Use "/static" or "//cdn.example.com/static".',
            )
        prefix = self._default_prefix if url_prefix is None else url_prefix
        self.prefix = public_prefix(prefix)
        self._route = Route(
            "GET",
            "/{path...}" if self.prefix == "/" else f"{self.prefix}/{{path...}}",
        )
        self.include_hidden = include_hidden
        self.follow_symlinks = follow_symlinks
        types = dict(CONTENT_TYPES)
        if content_types is not None:
            for suffix, content_type in content_types.items():
                if type(suffix) is not str or not suffix.startswith("."):
                    raise StarioError(
                        "content type keys must be file suffixes",
                        context={"key": suffix},
                        help_text='Use keys like ".css" or ".webmanifest".',
                    )
                types[suffix.lower()] = _header(
                    content_type, field=f"content_types[{suffix!r}]"
                )
        self._content_types = types
        self._cache: dict[str, _Cached] = {}
        self._loaded = False

    def register(
        self,
        app: App,
        *,
        cache_control: str | None = None,
    ) -> None:
        """Attach the GET route (it answers HEAD too). Prefer `attach()` in bootstrap."""
        kind = type(self).__name__
        if self.prefix.startswith("//"):
            raise StarioError(
                f"{kind} can only serve an app-relative prefix",
                context={"url_prefix": self.prefix},
                help_text="Use a host prefix on href() only. attach() needs /static or /data.",
            )
        if not self.directory.is_dir():
            raise StarioError(
                f"{kind} directory not found: {self.directory}",
                context={"path": str(self.directory)},
                help_text="Create the directory before attach().",
            )
        self._cache_control = _header(
            self._default_cache_control if cache_control is None else cache_control,
            field="cache_control",
        )
        app.add(self._route, self)

    async def load(
        self,
        *,
        max_bytes: int = 64 << 20,
        max_file_size: int = 1 << 20,
        precompress: Iterable[str] | None = None,
        compression: CompressionConfig = COMPRESSION,
    ) -> Mapping[str, int]:
        """Walk the directory, hold small files, and compress them. `Assets` also hashes the rest.

        Stats: `files`, `cached_files`, `streamed_files`, `budget_skipped_files`,
        `retained_bytes`, `compressed_files`.
        """
        kind = type(self).__name__
        if self._loaded:
            raise StarioRuntime(
                f"{kind} is already loaded",
                help_text=f"You can only load a {kind} object once.",
            )
        if not self.directory.is_dir():
            raise StarioError(
                f"{kind} directory not found: {self.directory}",
                context={"path": str(self.directory)},
                help_text="Create the directory before load().",
            )
        if type(compression) is not CompressionConfig or compression.min_size < 0:
            raise StarioError(
                "compression must be a CompressionConfig with min_size >= 0",
                help_text="Pass CompressionConfig(...) or omit compression=.",
            )
        for name, value in (
            ("max_bytes", max_bytes),
            ("max_file_size", max_file_size),
        ):
            if type(value) is not int or value <= 0:
                raise StarioError(
                    f"{name} must be a positive integer",
                    help_text="Pass a byte count greater than zero.",
                )
        if isinstance(precompress, str | bytes):
            raise StarioError(
                "precompress must be an iterable of codec names",
                help_text='Pass ("br", "zstd", "gzip") or a subset. Do not pass a string.',
                example='await files.load(precompress=("br", "gzip"))',
            )
        source = self._default_codecs if precompress is None else precompress
        try:
            names = set(source)
        except TypeError as exc:
            raise StarioError(
                "precompress must be an iterable of codec names",
                help_text='Pass ("br", "zstd", "gzip") or a subset. Do not pass a string.',
                example='await files.load(precompress=("br", "gzip"))',
            ) from exc
        if bad := [
            name
            for name in names
            if type(name) is not str or name.encode() not in CODECS
        ]:
            raise StarioError(
                "precompress contains unsupported codecs",
                context={"unsupported": bad},
                help_text="Supported codecs are br, zstd, and gzip.",
            )
        wanted = {name.encode() for name in names}
        enabled = compression.enabled_encodings()
        if disabled := [name for name in names if name.encode() not in enabled]:
            raise StarioError(
                "precompress selects disabled codecs",
                context={"disabled": disabled},
                help_text="Enable the codec on CompressionConfig or omit it from precompress.",
            )
        self.max_bytes = max_bytes
        self.max_file_size = max_file_size
        self._min_size = compression.min_size
        self._compressors = tuple(
            (token, compression.make_compressor(token))
            for token in CODECS
            if token in wanted
        )
        await asyncio.to_thread(self._load)
        self._loaded = True
        return self.stats

    async def attach(
        self,
        app: App,
        *,
        cache_control: str | None = None,
        max_bytes: int = 64 << 20,
        max_file_size: int = 1 << 20,
        precompress: Iterable[str] | None = None,
        compression: CompressionConfig = COMPRESSION,
    ) -> Mapping[str, int]:
        """Register GET (and so HEAD), then load the tree. Returns the stats of `load()`."""
        self.register(app, cache_control=cache_control)
        if self._loaded:
            return self.stats
        return await self.load(
            max_bytes=max_bytes,
            max_file_size=max_file_size,
            precompress=precompress,
            compression=compression,
        )

    def _load(self) -> None:
        raise NotImplementedError

    def resolve(self, path: str) -> tuple[Path, stat_result] | None:
        if not _is_relative(path):
            return None
        parts = Path(*path.split("/"))
        if not self.include_hidden and any(
            part.startswith(".") for part in parts.parts
        ):
            return None
        try:
            root = self.directory.resolve(strict=True)
            if not self.follow_symlinks:
                current = self.directory
                for part in parts.parts:
                    current /= part
                    if stat_module.S_ISLNK(current.lstat().st_mode):
                        return None
            resolved = self.directory.joinpath(parts).resolve(strict=True)
            if not resolved.is_relative_to(root):
                return None
            file_stat = resolved.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in PATH_ERRNOS:
                return None
            raise
        if not stat_module.S_ISREG(file_stat.st_mode):
            return None
        return resolved, file_stat

    def _walk(self) -> list[tuple[str, Path, stat_result]]:
        root = self.directory
        resolved_root = root if self._pin_root else root.resolve(strict=True)
        root_stat = resolved_root.stat()
        visited = {(root_stat.st_dev, root_stat.st_ino)}
        stack = [(os.fspath(root), "")]
        found: list[tuple[str, Path, stat_result]] = []
        follow = self.follow_symlinks
        while stack:
            current, prefix = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        name = entry.name
                        if not self.include_hidden and name.startswith("."):
                            continue
                        relative = f"{prefix}/{name}" if prefix else name
                        if not follow and entry.is_symlink():
                            continue
                        try:
                            file_stat = entry.stat(follow_symlinks=follow)
                            path = Path(entry.path)
                            if follow:
                                path = path.resolve(strict=True)
                                if not path.is_relative_to(resolved_root):
                                    continue
                        except OSError as exc:
                            if exc.errno in PATH_ERRNOS:
                                continue
                            raise
                        if stat_module.S_ISDIR(file_stat.st_mode):
                            key = (file_stat.st_dev, file_stat.st_ino)
                            if key not in visited:
                                visited.add(key)
                                stack.append((entry.path, relative))
                        elif stat_module.S_ISREG(file_stat.st_mode):
                            found.append((relative, path, file_stat))
            except OSError as exc:
                if exc.errno not in PATH_ERRNOS:
                    raise
        found.sort(key=lambda item: item[0])
        return found

    def _read(
        self,
        path: Path,
        *,
        size: int,
        budget: int,
        identity: tuple[int, int, int, int] | None = None,
        content: bytes | None = None,
    ) -> _Cached | None:
        content_type = self._content_types.get(path.suffix.lower(), DEFAULT_TYPE)
        if size > self.max_file_size or size > budget:
            return None
        if content is None:
            content = path.read_bytes()
        encodings: dict[bytes, bytes] = {}
        retained = len(content)
        if (
            self._compressors
            and retained >= self._min_size
            and content_type_is_compressible(content_type)
        ):
            for codec, compressor in self._compressors:
                compressed = compressor.frame(content)
                if len(compressed) >= len(content):
                    continue
                if retained + len(compressed) > budget:
                    continue
                encodings[codec] = compressed
                retained += len(compressed)
        return _Cached(content_type, content, identity, encodings)

    def _emit_cached(
        self,
        context: Context,
        writer: Writer,
        cached: _Cached,
        *,
        encoding: bytes | None,
    ) -> None:
        headers = writer.headers
        if encoding is None:
            body = cached.content
        else:
            body = cached.encodings[encoding]
            headers.unsafe_set(b"content-encoding", encoding)
        headers.unsafe_set(b"content-length", b"%d" % len(body))
        writer.write_headers(200).end(body)

    def _headers(
        self,
        writer: Writer,
        content_type: bytes,
        etag: bytes,
        modified: float,
        encodings: Mapping[bytes, bytes] | None = None,
    ) -> None:
        headers = writer.headers
        headers.unsafe_set(b"cache-control", self._cache_control)
        headers.unsafe_set(b"content-type", content_type)
        headers.unsafe_set(b"x-content-type-options", b"nosniff")
        headers.unsafe_set(b"etag", etag)
        headers.unsafe_set(b"last-modified", _last_modified(modified))
        headers.unsafe_set(b"accept-ranges", b"bytes")
        if encodings:
            merge_vary(headers, b"accept-encoding")

    async def _emit_file(
        self, context: Context, writer: Writer, fd: int, size: int
    ) -> None:
        writer.headers.unsafe_set(b"content-length", b"%d" % size)
        if context.req.method == "HEAD":
            writer.write_headers(200).end()
            return
        writer.write_headers(200)
        await _stream(writer, fd, 0, size)

    async def __call__(self, context: Context, writer: Writer) -> None:
        raise NotImplementedError

    async def _write_range(
        self,
        context: Context,
        writer: Writer,
        byte_range: _Range | object,
        *,
        size: int,
        source: int | None = None,
        body: bytes | None = None,
    ) -> None:
        headers = writer.headers
        if byte_range is UNSAT:
            headers.unsafe_set(b"content-range", b"bytes */%d" % size)
            headers.unsafe_set(b"content-length", b"0")
            writer.write_headers(416).end()
            return
        if not isinstance(byte_range, _Range):
            return
        start, end = byte_range
        headers.unsafe_set(b"content-range", b"bytes %d-%d/%d" % (start, end, size))
        headers.unsafe_set(b"content-length", b"%d" % (end - start + 1))
        if context.req.method == "HEAD":
            writer.write_headers(206).end()
            return
        if body is not None:
            writer.write_headers(206).end(body[start : end + 1])
            return
        assert source is not None
        writer.write_headers(206)
        await _stream(writer, source, start, end - start + 1)


class Assets(_Mount):
    """Fingerprinted files. `href()` hashes and pins. GET uses the hashed URL."""

    _pin_root = True
    _default_prefix = "/static"
    _default_cache_control = "public, max-age=31536000, immutable"
    _default_codecs = ("br", "zstd", "gzip")
    __slots__ = ("_catalog", "_hashed")

    def __init__(
        self,
        directory: Path | str,
        url_prefix: str | None = None,
        *,
        include_hidden: bool = False,
        follow_symlinks: bool = False,
        content_types: Mapping[str, str | bytes] | None = None,
    ) -> None:
        super().__init__(
            directory,
            url_prefix,
            include_hidden=include_hidden,
            follow_symlinks=follow_symlinks,
            content_types=content_types,
        )
        self._catalog: dict[str, _Hashed] = {}
        self._hashed: dict[str, _Hashed] = {}

    def href(
        self,
        path: str,
        /,
        *,
        query: Mapping[str, object] | None = None,
        fragment: str | None = None,
    ) -> str:
        if not _is_relative(path):
            raise _bad_path(path)
        return append_query_fragment(
            self._hash(path).url, query=query, fragment=fragment
        )

    def _hash(self, logical: str) -> _Hashed:
        if logical in self._catalog:
            return self._catalog[logical]
        resolved = self.resolve(logical)
        if resolved is None:
            raise _missing(logical)
        path, file_stat = resolved
        try:
            digest = _fingerprint(path)
            after = path.stat()
        except FileNotFoundError as exc:
            raise _changed(logical) from exc
        if (after.st_size, after.st_mtime_ns) != (
            file_stat.st_size,
            file_stat.st_mtime_ns,
        ):
            raise _changed(logical)
        return self._pin(logical, path, digest, after.st_size, after.st_mtime_ns)

    def _pin(
        self, logical: str, path: Path, digest: str, size: int, modified_ns: int
    ) -> _Hashed:
        parent, slash, name = logical.rpartition("/")
        stem, dot, extension = name.rpartition(".")
        hashed = f"{stem}.{digest}.{extension}" if dot and stem else f"{name}.{digest}"
        hashed_path = f"{parent}/{hashed}" if slash else hashed
        self._catalog[logical] = entry = _Hashed(
            hashed_path,
            self._route.href(hashed_path),
            path,
            size,
            modified_ns,
            digest,
        )
        self._hashed[hashed_path] = entry
        return entry

    def _load(self) -> None:
        bodies: dict[str, bytes] = {}
        for relative, path, file_stat in self._walk():
            if relative in self._catalog:
                continue
            try:
                if file_stat.st_size <= self.max_file_size:
                    content = path.read_bytes()
                    digest = xxh64(content).hexdigest()
                    bodies[relative] = content
                else:
                    digest = _fingerprint(path)
                after = path.stat()
            except FileNotFoundError as exc:
                raise _changed(relative) from exc
            if (after.st_size, after.st_mtime_ns) != (
                file_stat.st_size,
                file_stat.st_mtime_ns,
            ):
                raise _changed(relative)
            self._pin(relative, path, digest, after.st_size, after.st_mtime_ns)
        stats = _empty_stats()
        for logical, entry in self._catalog.items():
            try:
                digest = _fingerprint(entry.source)
                file_stat = entry.source.stat()
            except FileNotFoundError as exc:
                raise _changed(entry.source) from exc
            if digest != entry.digest or (file_stat.st_size, file_stat.st_mtime_ns) != (
                entry.size,
                entry.modified_ns,
            ):
                raise _changed(entry.source)
            loaded = self._read(
                entry.source,
                size=entry.size,
                content=bodies.get(logical),
                budget=self.max_bytes - stats["retained_bytes"],
            )
            if loaded is None:
                if entry.size <= self.max_file_size:
                    stats["budget_skipped_files"] += 1
                continue
            self._cache[entry.hashed_path] = loaded
            _tally(stats, loaded)
        stats["files"] = len(self._catalog)
        stats["streamed_files"] = stats["files"] - stats["cached_files"]
        self.stats = stats

    async def __call__(self, context: Context, writer: Writer) -> None:
        path = context.match.params.get("path", "")
        if not _is_relative(path):
            _not_found(writer)
            return
        hashed = self._hashed.get(path)
        cached = self._cache.get(path)
        if cached is not None and hashed is not None:
            request = context.req.headers
            identity_etag = _etag(hashed.digest, None)
            modified = hashed.modified_ns / 1_000_000_000
            byte_range = _parse_range(
                _range_header(request, identity_etag, modified), len(cached.content)
            )
            encodings = cached.encodings
            encoding = (
                None
                if byte_range is not None or not encodings
                else negotiate_content_encoding(
                    request.unsafe_get(b"accept-encoding", b""),
                    encodings,
                    CODECS,
                )
            )
            etag = identity_etag if encoding is None else _etag(hashed.digest, encoding)
            self._headers(writer, cached.content_type, etag, modified, encodings)
            if _fresh(request, etag, modified):
                writer.write_headers(304).end()
                return
            if byte_range is not None:
                await self._write_range(
                    context,
                    writer,
                    byte_range,
                    size=len(cached.content),
                    body=cached.content,
                )
                return
            self._emit_cached(context, writer, cached, encoding=encoding)
            return
        if hashed is not None:
            await self._stream_pinned(context, writer, hashed)
            return
        if logical := self._catalog.get(path):
            writer.redirect(logical.url, 307)
            return
        _not_found(writer)

    async def _stream_pinned(
        self, context: Context, writer: Writer, entry: _Hashed
    ) -> None:
        """Stream a hashed file that is not in memory. 404 if the pin drifted."""
        opened = await _to_thread_open(_open_regular, entry.source)
        if opened is None:
            _not_found(writer)
            return
        fd, file_stat = opened
        try:
            if (file_stat.st_size, file_stat.st_mtime_ns) != (
                entry.size,
                entry.modified_ns,
            ):
                _not_found(writer)
                return
            request = context.req.headers
            etag = _etag(entry.digest, None)
            modified = entry.modified_ns / 1_000_000_000
            byte_range = _parse_range(
                _range_header(request, etag, modified), entry.size
            )
            self._headers(
                writer,
                self._content_types.get(entry.source.suffix.lower(), DEFAULT_TYPE),
                etag,
                modified,
            )
            if _fresh(request, etag, modified):
                writer.write_headers(304).end()
                return
            if byte_range is not None:
                await self._write_range(
                    context, writer, byte_range, size=entry.size, source=fd
                )
                return
            await self._emit_file(context, writer, fd, entry.size)
        finally:
            os.close(fd)


# Media types that can execute or render active content in the browser.
_ACTIVE_TYPES: Final = frozenset(
    {
        b"text/html",
        b"application/xhtml+xml",
        b"image/svg+xml",
        b"text/xml",
        b"application/xml",
        b"text/javascript",
        b"application/javascript",
        b"application/ecmascript",
    }
)


class Files(_Mount):
    """Live files. `href()` checks the path exists. GET uses ETag and Range.

    Pass `untrusted=True` for directories holding user uploads or other
    untrusted bytes: every served response then carries
    `content-security-policy: sandbox`, and active media types (HTML, XML,
    SVG, JavaScript) also get `content-disposition: attachment` so browsers
    download them instead of executing them in the site's origin. The 404
    for missing paths is left unchanged.
    """

    __slots__ = ("untrusted",)

    def __init__(
        self,
        directory: Path | str,
        url_prefix: str | None = None,
        *,
        include_hidden: bool = False,
        follow_symlinks: bool = False,
        content_types: Mapping[str, str | bytes] | None = None,
        untrusted: bool = False,
    ) -> None:
        if type(untrusted) is not bool:
            raise StarioError(
                "untrusted must be bool",
                help_text="Pass True or False, not 1 or 0.",
            )
        super().__init__(
            directory,
            url_prefix,
            include_hidden=include_hidden,
            follow_symlinks=follow_symlinks,
            content_types=content_types,
        )
        self.untrusted = untrusted

    def href(
        self,
        path: str,
        /,
        *,
        query: Mapping[str, object] | None = None,
        fragment: str | None = None,
    ) -> str:
        if not _is_relative(path):
            raise _bad_path(path)
        if self.resolve(path) is None:
            raise _missing(path)
        return self._route.href(path, query=query, fragment=fragment)

    def _load(self) -> None:
        stats = _empty_stats()
        for relative, path, file_stat in self._walk():
            identity = _identity(file_stat)
            try:
                entry = self._read(
                    path,
                    size=file_stat.st_size,
                    identity=identity,
                    budget=self.max_bytes - stats["retained_bytes"],
                )
                after = path.stat()
            except FileNotFoundError:
                continue
            stats["files"] += 1
            if entry is None or _identity(after) != identity:
                if entry is None and file_stat.st_size <= self.max_file_size:
                    stats["budget_skipped_files"] += 1
                continue
            self._cache[relative] = entry
            _tally(stats, entry)
        stats["streamed_files"] = stats["files"] - stats["cached_files"]
        self.stats = stats

    def _resolve_open(self, path: str) -> tuple[int, Path, stat_result] | None:
        """Resolve then open in one worker-thread hop; fd first for cleanup."""
        found = self.resolve(path)
        if found is None:
            return None
        source, _ = found
        opened = _open_regular(source)
        if opened is None:
            return None
        fd, file_stat = opened
        return fd, source, file_stat

    async def __call__(self, context: Context, writer: Writer) -> None:
        path = context.match.params.get("path", "")
        if not _is_relative(path):
            _not_found(writer)
            return
        opened = await _to_thread_open(self._resolve_open, path)
        if opened is None:
            _not_found(writer)
            return
        fd, source, file_stat = opened
        try:
            entry = self._cache.get(path)
            if entry is not None and entry.identity != _identity(file_stat):
                del self._cache[path]
                entry = None
            size = file_stat.st_size
            modified = file_stat.st_mtime
            request = context.req.headers
            identity_etag = _stat_etag(file_stat, None)
            byte_range = _parse_range(
                _range_header(request, identity_etag, modified), size
            )
            encodings = None if entry is None else entry.encodings
            # Range is always identity bytes, so negotiate encoding only for a full reply.
            encoding = (
                None
                if byte_range is not None or not encodings
                else negotiate_content_encoding(
                    request.unsafe_get(b"accept-encoding", b""),
                    encodings,
                    CODECS,
                )
            )
            etag = (
                identity_etag if encoding is None else _stat_etag(file_stat, encoding)
            )
            content_type = (
                entry.content_type
                if entry is not None
                else self._content_types.get(source.suffix.lower(), DEFAULT_TYPE)
            )
            self._headers(writer, content_type, etag, modified, encodings)
            if self.untrusted:
                headers = writer.headers
                headers.unsafe_set(b"content-security-policy", b"sandbox")
                media_type = content_type.split(b";", 1)[0].strip().lower()
                if media_type in _ACTIVE_TYPES:
                    headers.unsafe_set(b"content-disposition", b"attachment")
            if _fresh(request, etag, modified):
                writer.write_headers(304).end()
                return
            if byte_range is not None:
                await self._write_range(
                    context,
                    writer,
                    byte_range,
                    size=size,
                    body=None if entry is None else entry.content,
                    source=fd,
                )
                return
            if entry is not None:
                self._emit_cached(context, writer, entry, encoding=encoding)
                return
            await self._emit_file(context, writer, fd, size)
        finally:
            os.close(fd)


__all__ = ["Assets", "Files"]
