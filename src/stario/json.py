"""Process-wide JSON encode and decode for responses, Datastar, telemetry, and tests.

The codec is two callables. `encode` takes a value and returns one valid JSON
value as text or UTF-8 bytes; it raises `TypeError` or `ValueError` for an
unsupported value. `decode` accepts text, bytes, or a byte array and raises
`ValueError` for invalid JSON. `set_codec(encode, decode)` installs the pair —
a library's own functions plug in directly, for example
`set_codec(orjson.dumps, orjson.loads)`. Set it once at startup; the
functions must be safe to call from any thread.
"""

import json as json_module
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, cast, overload

if TYPE_CHECKING:
    from typing_extensions import deprecated
else:
    # The decorator only informs type checkers. Runtime warnings are explicit
    # warnings.warn calls, so warnings.deprecated would warn a second time.
    def deprecated(message: str, /) -> Callable[[Any], Any]:
        def decorator(obj: Any) -> Any:
            return obj

        return decorator


type JsonDefault = Callable[[object], object]
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonEncode = Callable[[Any], str | bytes]
type JsonDecode = Callable[[str | bytes | bytearray], Any]


@deprecated(
    "JsonCodec is deprecated: pass two functions to set_codec instead, "
    "set_codec(encode, decode). Removed in 6.0."
)
class JsonCodec(Protocol):
    """Serialize JSON as text or UTF-8 bytes and parse bytes or text.

    Deprecated. `set_codec` takes two functions now; see `JsonEncode` and
    `JsonDecode`. The methods below are the contract a codec object had:

    `dumps(value).encode("utf-8")` and `dumps_bytes(value)` must be identical.
    Both forms must contain one compact RFC 8259 JSON value without a byte-order
    mark or formatting line breaks.

    Stario forwards `default`, but each backend decides which native types it
    handles before calling that function. Dump methods must raise `TypeError` or
    `ValueError` when serialization fails. `loads` must raise `ValueError` when
    the input is not valid JSON.
    """

    def dumps(
        self,
        value: object,
        /,
        *,
        default: JsonDefault | None = None,
    ) -> str: ...

    def dumps_bytes(
        self,
        value: object,
        /,
        *,
        default: JsonDefault | None = None,
    ) -> bytes: ...

    def loads(self, data: str | bytes | bytearray, /) -> object: ...


@deprecated(
    "StdlibJsonCodec is deprecated: stario.json uses the standard library by "
    "default, so no set_codec call is needed. Removed in 6.0."
)
class StdlibJsonCodec:
    """Compact UTF-8 JSON implemented with the Python standard library."""

    __slots__ = ()

    def __init__(self) -> None:
        warnings.warn(
            "StdlibJsonCodec is deprecated: stario.json uses the standard "
            "library by default. Removed in 6.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    def dumps(
        self,
        value: object,
        /,
        *,
        default: JsonDefault | None = None,
    ) -> str:
        return json_module.dumps(
            value,
            allow_nan=False,
            default=default,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def dumps_bytes(
        self,
        value: object,
        /,
        *,
        default: JsonDefault | None = None,
    ) -> bytes:
        return self.dumps(value, default=default).encode("utf-8")

    def loads(self, data: str | bytes | bytearray, /) -> object:
        return json_module.loads(data)


# One encoder for every call: json.dumps with any non-default option builds
# a new JSONEncoder each time (~0.7 us of the ~1.8 us a small dict takes).
# encode() keeps no state between calls, so threads can share it.
_STDLIB_ENCODER = json_module.JSONEncoder(
    allow_nan=False, ensure_ascii=False, separators=(",", ":")
)


def _stdlib_encode(value: Any, /) -> str:
    return _STDLIB_ENCODER.encode(value)


def _stdlib_default_encode(value: Any, default: JsonDefault, /) -> str:
    return json_module.dumps(
        value,
        allow_nan=False,
        default=default,
        ensure_ascii=False,
        separators=(",", ":"),
    )


_encode: JsonEncode = _stdlib_encode
_decode: JsonDecode = json_module.loads
_legacy: JsonCodec | None = None


@overload
def set_codec(encode: JsonEncode, decode: JsonDecode, /) -> None: ...


@overload
@deprecated("Pass two functions instead: set_codec(orjson.dumps, orjson.loads)")
def set_codec(codec: JsonCodec, /) -> None: ...


def set_codec(encode: object, decode: object = None, /) -> None:
    """Replace the codec used by later JSON operations.

    Pass the library's own functions: `set_codec(orjson.dumps, orjson.loads)`.
    The encode callable may return text or bytes; Stario converts once where it
    needs the other form. The assignment is not synchronized with active work:
    values already serialized and operations already using the previous codec
    do not change.

    Passing a codec object with `dumps_bytes` and `loads` is deprecated and
    will be removed in 6.0.
    """
    global _encode, _decode, _legacy
    if decode is not None:
        if not callable(encode) or not callable(decode):
            raise TypeError("set_codec(encode, decode) takes two callables")
        _encode = cast(JsonEncode, encode)
        _decode = cast(JsonDecode, decode)
        _legacy = None
        return
    if hasattr(encode, "dumps_bytes") and hasattr(encode, "loads"):
        warnings.warn(
            "Passing a codec object to set_codec is deprecated: pass two "
            "functions instead, for example "
            "set_codec(orjson.dumps, orjson.loads). Removed in 6.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        codec = cast(JsonCodec, encode)
        _encode = codec.dumps_bytes
        _decode = codec.loads
        _legacy = codec
        return
    raise TypeError(
        "set_codec(encode, decode) takes two callables; "
        "a codec object argument is deprecated"
    )


@overload
def dumps(value: object, /) -> str: ...


@overload
@deprecated(
    "default= is deprecated; serialize custom types before encoding. Removed in 6.0."
)
def dumps(value: object, /, *, default: JsonDefault) -> str: ...


def dumps(value: object, /, *, default: JsonDefault | None = None) -> str:
    """Serialize a value as compact JSON text."""
    if default is None:
        out = _encode(value)
        return out if type(out) is str else cast(bytes, out).decode()
    warnings.warn(
        "default= on stario.json.dumps is deprecated and only applies to the "
        "standard library encoder. Removed in 6.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    if _legacy is not None:
        return _legacy.dumps(value, default=default)
    return _stdlib_default_encode(value, default)


@overload
def dumps_bytes(value: object, /) -> bytes: ...


@overload
@deprecated(
    "default= is deprecated; serialize custom types before encoding. Removed in 6.0."
)
def dumps_bytes(value: object, /, *, default: JsonDefault) -> bytes: ...


def dumps_bytes(value: object, /, *, default: JsonDefault | None = None) -> bytes:
    """Serialize a value as compact UTF-8 JSON bytes."""
    if default is None:
        out = _encode(value)
        return out if type(out) is bytes else cast(str, out).encode()
    warnings.warn(
        "default= on stario.json.dumps_bytes is deprecated and only applies "
        "to the standard library encoder. Removed in 6.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    if _legacy is not None:
        return _legacy.dumps_bytes(value, default=default)
    return _stdlib_default_encode(value, default).encode()


def loads(data: str | bytes | bytearray, /) -> object:
    """Parse JSON text, bytes, or a byte array into Python values."""
    return _decode(data)


__all__ = [
    "JsonCodec",
    "JsonDecode",
    "JsonDefault",
    "JsonEncode",
    "JsonScalar",
    "JsonValue",
    "StdlibJsonCodec",
    "dumps",
    "dumps_bytes",
    "loads",
    "set_codec",
]
