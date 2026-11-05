"""Request body encoding and response parsing for TestClient."""

import zlib
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from stario.http.compression import brotli_decompress, zstd_decompress
from stario.http.headers import Headers
from stario.json import dumps_bytes as json_dumps_bytes
from stario.testing.cookies import parse_set_cookie_headers
from stario.testing.types import FileData, FormData, QueryParamInput


def expand_pairs(items: QueryParamInput | FormData) -> list[tuple[str, str]]:
    seq = items.items() if isinstance(items, Mapping) else items
    out: list[tuple[str, str]] = []
    for key, value in seq:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for v in value:
                sv = ("true" if v else "false") if isinstance(v, bool) else str(v)
                out.append((str(key), sv))
        else:
            v = value
            sv = ("true" if v else "false") if isinstance(v, bool) else str(v)
            out.append((str(key), sv))
    return out


def _quote_disposition(value: str) -> str:
    # As browsers do (HTML multipart/form-data encoding): no backslash escapes.
    return value.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")


def _encode_multipart(
    data: FormData | bytes | str | None,
    files: FileData,
) -> tuple[bytes, str]:
    if isinstance(data, (bytes, str)):
        raise ValueError(
            "Multipart requests accept mapping or sequence form `data` only."
        )

    boundary = f"stario-boundary-{uuid4().hex}"
    parts: list[bytes] = []

    if data is not None:
        for name, value in expand_pairs(data):
            esc = _quote_disposition(name)
            disp = f'Content-Disposition: form-data; name="{esc}"'
            parts.extend(
                (
                    f"--{boundary}\r\n".encode("ascii"),
                    disp.encode("utf-8"),
                    b"\r\n\r\n",
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )

    file_items = files.items() if isinstance(files, Mapping) else files

    for field_name, file_value in file_items:
        if isinstance(file_value, (bytes, str)):
            filename, payload, content_type = (
                field_name,
                file_value,
                "application/octet-stream",
            )
        elif len(file_value) == 2:
            filename, payload = file_value
            content_type = "application/octet-stream"
        else:
            filename, payload, content_type = file_value
        payload_bytes = (
            payload if isinstance(payload, bytes) else payload.encode("utf-8")
        )
        esc = _quote_disposition(field_name)
        sf = _quote_disposition(filename)
        disp = f'Content-Disposition: form-data; name="{esc}"; filename="{sf}"'
        parts.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                disp.encode("utf-8"),
                b"\r\n",
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                payload_bytes,
                b"\r\n",
            )
        )

    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def encode_request_body(
    *,
    json: Any | None,
    data: FormData | bytes | str | None,
    files: FileData | None,
    content: bytes | str | None,
) -> tuple[bytes, str | None]:
    if json is not None:
        return (
            json_dumps_bytes(json),
            "application/json; charset=utf-8",
        )
    if files is not None:
        return _encode_multipart(data, files)
    if isinstance(content, bytes):
        return content, None
    if isinstance(content, str):
        return content.encode("utf-8"), None
    if isinstance(data, bytes):
        return data, None
    if isinstance(data, str):
        return data.encode("utf-8"), "text/plain; charset=utf-8"
    if data is not None:
        return (
            urlencode(expand_pairs(data), doseq=True).encode("utf-8"),
            "application/x-www-form-urlencoded",
        )
    return b"", None


def decode_content_encoding(body: bytes, encoding: str | None) -> bytes:
    # HEAD responses carry Content-Encoding with an empty body; nothing to decode.
    if not encoding or not body:
        return body
    normalized = encoding.lower()
    if normalized == "gzip":
        return zlib.decompress(body, wbits=31)
    if normalized == "deflate":
        return zlib.decompress(body)
    if normalized == "br":
        return brotli_decompress(body)
    if normalized == "zstd":
        return zstd_decompress(body)
    raise RuntimeError(
        f"TestClient does not support Content-Encoding={encoding!r}; "
        "use Accept-Encoding that yields identity."
    )


def parse_response_cookies(headers: Headers) -> dict[str, str]:
    return parse_set_cookie_headers(headers.getlist("set-cookie"))
