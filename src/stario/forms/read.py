"""`read_form`: the fields and files of an HTML form submission.

The method decides where the data is, as in `read_signals`: GET and HEAD
read the query string; other methods read the body by its `Content-Type`
(`application/x-www-form-urlencoded` or `multipart/form-data`).
"""

import re
from functools import lru_cache, partial
from tempfile import SpooledTemporaryFile

from stario.exceptions import RequestBodyError
from stario.http.request import Request
from stario_cython.exchange import ParsedQuery

from ._multipart import (
    MultipartError,
    MultipartLimitError,
    MultipartParser,
    PartEnd,
    PartStart,
    parse_options,
)
from .form import Form, UploadFile, in_thread

_URLENCODED = "application/x-www-form-urlencoded"
_MULTIPART = "multipart/form-data"

DEFAULT_MAX_FIELDS = 1000
DEFAULT_MAX_FIELD_SIZE = 1024 * 1024
DEFAULT_MAX_FILES = 10
DEFAULT_SPOOL_SIZE = 1024 * 1024
DEFAULT_MAX_PART_HEADER_BYTES = 16 * 1024
# Bytes of a file past `spool_size` gathered before one thread hop writes them.
_WRITE_BATCH = 1024 * 1024

# RFC 2046 5.1.1: 1 to 70 bchars, not ending with a space.
_BOUNDARY = re.compile(r"[0-9A-Za-z'()+_,\-./:=? ]{0,69}[0-9A-Za-z'()+_,\-./:=?]")


async def read_form(
    req: Request,
    *,
    max_size: int | None = None,
    max_fields: int = DEFAULT_MAX_FIELDS,
    max_field_size: int = DEFAULT_MAX_FIELD_SIZE,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_size: int | None = None,
    spool_size: int = DEFAULT_SPOOL_SIZE,
    max_part_header_bytes: int = DEFAULT_MAX_PART_HEADER_BYTES,
) -> Form:
    """Parse the form a client submitted.

    - `max_size`: limit of the whole body, as in `Request.body()` (default:
      `RequestPolicy.max_body_bytes`). Raise it on an upload route.
    - `max_fields` / `max_field_size`: count of text fields in a body, and
      bytes of one multipart text field. GET reads the query, which the
      header limit of the server already bounds.
    - `max_files` / `max_file_size`: count of files in a body, and bytes of
      one file (default: no limit below `max_size`). A file input left empty
      is not a file; it counts toward `max_fields` like an empty field.
    - `spool_size`: bytes of one file kept in memory before it moves to disk.

    Raise `RequestBodyError`: 400 for a malformed body, 413 past a limit, 415
    for another `Content-Type`. Values are untrusted: validate them. A file
    input left empty (no filename, no content) adds no file.

    ```python
    from stario.forms import read_form

    async def report_pet(c, w):
        with await read_form(c.req, max_size=6_000_000) as form:
            name = form.get("name", "")
            photo = form.file("photo")
    ```
    """
    if req.method.upper() in ("GET", "HEAD"):
        # The query is already bounded by the header limit of the server.
        return Form(req.query.items())

    raw_type = req.headers.get("content-type")
    if raw_type is None:
        raise RequestBodyError(415, "Expected a form body")
    media_type, charset, boundary = _content_type(raw_type)
    if charset not in ("utf-8", "utf8"):
        raise RequestBodyError(415, "Form charset must be UTF-8")
    if media_type == _URLENCODED:
        body = await req.body(max_size=max_size)
        # Count before decoding: `a&a&a...` up to max_size would otherwise
        # build millions of pairs just to be refused.
        if _too_many_pairs(body, max_fields):
            raise RequestBodyError(413, "Too many form fields")
        return _fields_form(ParsedQuery(body).items(), max_fields)
    if media_type == _MULTIPART:
        if _BOUNDARY.fullmatch(boundary) is None:
            raise RequestBodyError(400, "Invalid multipart boundary")
        return await _read_multipart(
            req,
            boundary.encode("ascii"),
            max_size=max_size,
            max_fields=max_fields,
            max_field_size=max_field_size,
            max_files=max_files,
            max_file_size=max_file_size,
            spool_size=spool_size,
            max_part_header_bytes=max_part_header_bytes,
        )
    raise RequestBodyError(415, "Expected a form body")


def _content_type(raw: str) -> tuple[str, str, str]:
    """Media type, lowercased charset, and boundary of a `Content-Type` value."""
    if len(raw) <= 256:
        return _content_type_cached(raw)
    return _parse_content_type(raw)


def _parse_content_type(raw: str) -> tuple[str, str, str]:
    media_type, params = parse_options(raw)
    return (
        media_type,
        params.get("charset", "utf-8").lower(),
        params.get("boundary", ""),
    )


# One app sees few distinct Content-Type values; a browser's multipart
# boundary changes per submission, so this only spares the parse on repeats.
_content_type_cached = lru_cache(maxsize=64)(_parse_content_type)


def _too_many_pairs(body: bytes, limit: int) -> bool:
    # At most one pair more than there are separators, so the usual form is
    # settled by one count; only a body with that many `&` is scanned.
    if body.count(b"&") < limit:
        return False
    return any(
        count > limit for count, _ in enumerate(re.finditer(rb"[^&]+", body), start=1)
    )


def _fields_form(items: list[tuple[str, str]], max_fields: int) -> Form:
    if len(items) > max_fields:
        raise RequestBodyError(413, "Too many form fields")
    return Form(items)


def _filename(raw: str) -> str:
    # Some clients send a full path (C:\Users\...\photo.jpg): keep the last part.
    return raw.replace("\\", "/").rsplit("/", 1)[-1]


async def _read_multipart(
    req: Request,
    boundary: bytes,
    *,
    max_size: int | None,
    max_fields: int,
    max_field_size: int,
    max_files: int,
    max_file_size: int | None,
    spool_size: int,
    max_part_header_bytes: int,
) -> Form:
    parser = MultipartParser(boundary, max_header_bytes=max_part_header_bytes)
    fields: list[tuple[str, str]] = []
    files: list[tuple[str, UploadFile]] = []
    file_parts = 0
    empty_parts = 0
    part: PartStart | None = None
    text = bytearray()
    spool: SpooledTemporaryFile[bytes] | None = None
    pending = bytearray()  # bytes past spool_size not yet written to disk
    size = 0

    def open_spool() -> SpooledTemporaryFile[bytes]:
        # Counted when the spool opens, so a flood of empty file parts
        # cannot allocate one spool each; a real file costs one of
        # `max_files` whether or not it is kept.
        nonlocal file_parts
        file_parts += 1
        if file_parts > max_files:
            raise RequestBodyError(413, "Too many files")
        # Owned by the UploadFile (or closed by _discard), not a block.
        return SpooledTemporaryFile(max_size=spool_size, mode="w+b")

    try:
        async for chunk in req.stream(max_size=max_size):
            for event in parser.feed(chunk):
                if isinstance(event, PartStart):
                    part = event
                    size = 0
                    if event.filename is None:
                        if len(fields) >= max_fields:
                            raise RequestBodyError(413, "Too many form fields")
                        text.clear()
                    elif not _filename(event.filename):
                        # A file input left empty (`filename=""`): no spool
                        # unless bytes arrive, and bounded like a field, so
                        # a form with many optional file inputs still posts.
                        empty_parts += 1
                        if len(fields) + empty_parts > max_fields:
                            raise RequestBodyError(413, "Too many form fields")
                    else:
                        spool = open_spool()
                elif isinstance(event, PartEnd):
                    assert part is not None
                    if part.filename is None:
                        fields.append((part.name, text.decode("utf-8", "replace")))
                    elif spool is not None:
                        if pending:
                            await in_thread(partial(spool.write, pending))
                            pending.clear()
                        spool.seek(0)
                        upload = UploadFile(
                            _filename(part.filename), part.content_type, spool, size
                        )
                        files.append((part.name, upload))
                        spool = None
                    part = None
                else:
                    assert part is not None
                    size += len(event)
                    if part.filename is None:
                        if size > max_field_size:
                            raise RequestBodyError(413, "Form field too large")
                        text += event
                    else:
                        if spool is None:
                            # Bytes under an empty filename: a file after all.
                            spool = open_spool()
                        if max_file_size is not None and size > max_file_size:
                            raise RequestBodyError(413, "File too large")
                        if size > spool_size:
                            # On disk now: gather chunks and write them in one
                            # thread hop per _WRITE_BATCH, so the event loop
                            # stays free without a hop per body chunk.
                            pending += event
                            if len(pending) >= _WRITE_BATCH:
                                await in_thread(partial(spool.write, pending))
                                pending.clear()
                        else:
                            spool.write(event)
        parser.close()
    except MultipartLimitError as exc:
        _discard(files, spool)
        raise RequestBodyError(413, "Form part headers too large") from exc
    except MultipartError as exc:
        _discard(files, spool)
        raise RequestBodyError(400, "Invalid multipart form data") from exc
    except BaseException:
        _discard(files, spool)
        raise
    return Form(fields, files)


def _discard(
    files: list[tuple[str, UploadFile]], spool: SpooledTemporaryFile[bytes] | None
) -> None:
    for _, upload in files:
        upload.close()
    if spool is not None:
        spool.close()
