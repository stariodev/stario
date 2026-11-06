"""HTTP content-coding negotiation and one-shot encoding for precompressed files."""

import zlib
from collections.abc import Iterable
from functools import lru_cache

from stario._env import env_int, env_optional_int, env_optional_str
from stario.exceptions import StarioError
from stario_cython.codecs import brotli_compress, zstd_compress

# `negotiate_content_encoding` honors `Accept-Encoding` q-values, then prefers
# zstd -> br -> gzip when the client weights supported encodings equally.
# Dynamic and streamed responses pay encode CPU per call, so zstd leads: it is
# within ~8% of brotli on wire size at a fraction of the cost, and on a long
# SSE stream the rolling history window makes them equal.
# `filesystem` passes a brotli-first preference for precompressed assets,
# where bytes are encoded once at load and only wire size matters.
# Window bounds match libzstd / libbrotli (same libs the Cython writer uses).

_ZSTD_WINDOW_LOG_MIN = 10
_ZSTD_WINDOW_LOG_MAX = 31
_BROTLI_WINDOW_LOG_MIN = 10
_BROTLI_WINDOW_LOG_MAX = 24
_GZIP_WINDOW_BITS_MIN = 9
_GZIP_WINDOW_BITS_MAX = 15
_STREAM_WINDOW_LOG_MIN = 10
_STREAM_WINDOW_LOG_MAX = 24
_ENCODING_PREFERENCE = (b"zstd", b"br", b"gzip")
_ACCEPT_ENCODING_CACHE_MAX_BYTES = 512


def _normalize_preference(
    preference: Iterable[str | bytes] | None,
) -> tuple[bytes, bytes, bytes]:
    """Validate a codec order and append unlisted names in default order."""
    if preference is None:
        return _ENCODING_PREFERENCE
    names = [name if isinstance(name, bytes) else name.encode() for name in preference]
    seen: set[bytes] = set()
    for name in names:
        if name not in _ENCODING_PREFERENCE:
            raise StarioError(
                f"Unknown content encoding in preference: {name.decode('utf-8', 'replace')!r}",
                help_text="Use 'zstd', 'br', and/or 'gzip'.",
            )
        if name in seen:
            raise StarioError(
                f"Duplicate content encoding in preference: {name.decode('utf-8', 'replace')!r}",
                help_text="List each encoding at most once.",
            )
        seen.add(name)
    if not names:
        return _ENCODING_PREFERENCE
    ordered = names + [name for name in _ENCODING_PREFERENCE if name not in seen]
    return ordered[0], ordered[1], ordered[2]


def _parse_accept_encoding_uncached(accept_encoding: bytes) -> dict[bytes, float]:
    parsed: dict[bytes, float] = {}

    if b";" not in accept_encoding:
        for raw_part in accept_encoding.split(b","):
            token = raw_part.strip().lower()
            if token:
                parsed[token] = 1.0
        return parsed

    for raw_part in accept_encoding.split(b","):
        token, sep, params = raw_part.partition(b";")
        token = token.strip().lower()
        if not token:
            continue

        q = 1.0
        while sep:
            param, sep, params = params.partition(b";")
            key, eq, value = param.partition(b"=")
            if eq and key.strip().lower() == b"q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
                break

        parsed[token] = max(0.0, min(1.0, q))
    return parsed


@lru_cache(maxsize=64)
def _parse_accept_encoding_cached(accept_encoding: bytes) -> dict[bytes, float]:
    return _parse_accept_encoding_uncached(accept_encoding)


def parse_accept_encoding(accept_encoding: str | bytes) -> dict[bytes, float]:
    """Parse `Accept-Encoding` into lowercased token bytes -> q-value."""
    if isinstance(accept_encoding, str):
        accept_encoding = accept_encoding.encode("latin-1")
    if len(accept_encoding) > _ACCEPT_ENCODING_CACHE_MAX_BYTES:
        return _parse_accept_encoding_uncached(accept_encoding)
    return _parse_accept_encoding_cached(accept_encoding)


def negotiate_content_encoding(
    accept_encoding: str | bytes | None,
    available: Iterable[bytes],
    preference: Iterable[bytes] = _ENCODING_PREFERENCE,
) -> bytes | None:
    """Choose the best available content-coding from `Accept-Encoding`.

    `preference` lists the encodings this path serves, best first; it breaks
    ties between encodings the client weights equally.
    """
    if accept_encoding is None:
        return None

    accepted = parse_accept_encoding(accept_encoding)
    wildcard_q = accepted.get(b"*", 0.0)
    available_set = set(available)
    best_q = 0.0
    best_encoding: bytes | None = None

    for encoding in preference:
        if encoding not in available_set:
            continue
        q = accepted.get(encoding, wildcard_q)
        if q > best_q:
            best_q = q
            best_encoding = encoding

    if best_encoding is None:
        return None

    identity_q = accepted.get(b"identity")
    if identity_q is not None and identity_q >= best_q:
        return None

    return best_encoding


_NONCOMPRESSIBLE_CONTENT_TYPE_PREFIXES = (
    b"image/",
    b"audio/",
    b"video/",
)

_NONCOMPRESSIBLE_CONTENT_TYPES = (
    b"application/gzip",
    b"application/x-gzip",
    b"application/zip",
    b"application/x-zip-compressed",
    b"application/x-7z-compressed",
    b"application/vnd.rar",
    b"application/x-rar-compressed",
    b"application/x-bzip",
    b"application/x-bzip2",
    b"application/x-xz",
    b"application/zstd",
    b"application/x-zstd",
    b"font/woff",
    b"font/woff2",
)


def content_type_is_compressible(content_type: bytes) -> bool:
    """Return False for missing types, already-compressed/binary media types."""
    media_type = content_type.split(b";", 1)[0].strip().lower()
    if not media_type:
        return False

    if media_type in _NONCOMPRESSIBLE_CONTENT_TYPES:
        return False
    if media_type == b"image/svg+xml":
        return True

    return not media_type.startswith(_NONCOMPRESSIBLE_CONTENT_TYPE_PREFIXES)


DEFAULT_MIN_SIZE = 512
DEFAULT_ZSTD_LEVEL = 3
DEFAULT_ZSTD_WINDOW_LOG: int | None = None
DEFAULT_BROTLI_LEVEL = 4
DEFAULT_BROTLI_WINDOW_LOG: int | None = None
DEFAULT_GZIP_LEVEL = 1
DEFAULT_GZIP_WINDOW_BITS: int | None = None
DEFAULT_STREAM_WINDOW_LOG: int | None = None


class CompressionConfig:
    """Policy for picking `br` / `zstd` / `gzip` from `Accept-Encoding` and for streaming vs whole-body responses.

    Configure only via the constructor; fields are not meant for post-init mutation.

    `stream_window_log` is the window bound for streamed (`write()`)
    responses only — a live stream's encoder then holds a small window and
    capped match tables (brotli also drops to quality 3) instead of the
    one-shot memory footprint. `None` (the default) gives streams the
    one-shot settings exactly; `respond()` output never depends on it.
    A small window saves memory per idle stream but loses the previous
    repaint once a document outgrows it, so whole-document SSE repaints
    send far more bytes and cost more encode CPU.
    """

    __slots__ = (
        "_enabled_encodings",
        "brotli_level",
        "brotli_window_log",
        "gzip_level",
        "gzip_window_bits",
        "min_size",
        "preference",
        "stream_window_log",
        "zstd_level",
        "zstd_window_log",
    )

    def __init__(
        self,
        *,
        min_size: int = DEFAULT_MIN_SIZE,
        zstd_level: int = DEFAULT_ZSTD_LEVEL,
        zstd_window_log: int | None = DEFAULT_ZSTD_WINDOW_LOG,
        brotli_level: int = DEFAULT_BROTLI_LEVEL,
        brotli_window_log: int | None = DEFAULT_BROTLI_WINDOW_LOG,
        gzip_level: int = DEFAULT_GZIP_LEVEL,
        gzip_window_bits: int | None = DEFAULT_GZIP_WINDOW_BITS,
        stream_window_log: int | None = DEFAULT_STREAM_WINDOW_LOG,
        preference: Iterable[str | bytes] | None = None,
    ) -> None:
        if min_size < 0:
            raise StarioError(
                "min_size must be 0 or greater",
                help_text="Use 0 to compress all bodies or a positive byte threshold.",
            )
        if zstd_level >= 0 and not 1 <= zstd_level <= 22:
            raise StarioError(
                "zstd_level must be negative or between 1 and 22",
                help_text="Use -1 to disable zstd or a level in 1-22.",
            )
        if zstd_window_log is not None and not (
            _ZSTD_WINDOW_LOG_MIN <= zstd_window_log <= _ZSTD_WINDOW_LOG_MAX
        ):
            raise StarioError(
                "zstd_window_log must be between "
                f"{_ZSTD_WINDOW_LOG_MIN} and {_ZSTD_WINDOW_LOG_MAX}",
                help_text="Omit the setting to use the codec default.",
            )
        if brotli_level >= 0 and not 0 <= brotli_level <= 11:
            raise StarioError(
                "brotli_level must be negative or between 0 and 11",
                help_text="Use -1 to disable brotli or a quality in 0-11.",
            )
        if brotli_window_log is not None and not (
            _BROTLI_WINDOW_LOG_MIN <= brotli_window_log <= _BROTLI_WINDOW_LOG_MAX
        ):
            raise StarioError(
                "brotli_window_log must be between "
                f"{_BROTLI_WINDOW_LOG_MIN} and {_BROTLI_WINDOW_LOG_MAX}",
                help_text="Omit the setting to use the codec default.",
            )
        if gzip_level >= 0 and not 1 <= gzip_level <= 9:
            raise StarioError(
                "gzip_level must be negative or between 1 and 9",
                help_text="Use -1 to disable gzip or a level in 1-9.",
            )
        if gzip_window_bits is not None and not (
            _GZIP_WINDOW_BITS_MIN <= gzip_window_bits <= _GZIP_WINDOW_BITS_MAX
        ):
            raise StarioError(
                "gzip_window_bits must be between "
                f"{_GZIP_WINDOW_BITS_MIN} and {_GZIP_WINDOW_BITS_MAX}",
                help_text="Omit the setting to use the framework default.",
            )
        if stream_window_log is not None and not (
            _STREAM_WINDOW_LOG_MIN <= stream_window_log <= _STREAM_WINDOW_LOG_MAX
        ):
            raise StarioError(
                "stream_window_log must be between "
                f"{_STREAM_WINDOW_LOG_MIN} and {_STREAM_WINDOW_LOG_MAX}",
                help_text=(
                    "Pass None to use the one-shot codec settings for "
                    "streamed responses too."
                ),
            )

        self.min_size = min_size
        self.zstd_level = zstd_level
        self.zstd_window_log = zstd_window_log
        self.brotli_level = brotli_level
        self.brotli_window_log = brotli_window_log
        self.gzip_level = gzip_level
        self.gzip_window_bits = gzip_window_bits
        self.stream_window_log = stream_window_log
        self.preference = _normalize_preference(preference)
        self._enabled_encodings = self.enabled_encodings()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CompressionConfig):
            return NotImplemented
        return (
            self.min_size == other.min_size
            and self.zstd_level == other.zstd_level
            and self.zstd_window_log == other.zstd_window_log
            and self.brotli_level == other.brotli_level
            and self.brotli_window_log == other.brotli_window_log
            and self.gzip_level == other.gzip_level
            and self.gzip_window_bits == other.gzip_window_bits
            and self.stream_window_log == other.stream_window_log
            and self.preference == other.preference
        )

    def enabled_encodings(self) -> tuple[bytes, ...]:
        """Enabled encodings in `preference` order."""
        enabled = {
            b"br": self.brotli_level >= 0,
            b"zstd": self.zstd_level >= 0,
            b"gzip": self.gzip_level >= 0,
        }
        return tuple(name for name in self.preference if enabled[name])


def compress_bytes(config: CompressionConfig, encoding: bytes, data: bytes) -> bytes:
    """Encode `data` as one frame with the level and window `config` sets for `encoding`.

    The Cython writer encodes responses itself; `stario.filesystem` uses this
    to precompress files at load.
    """
    if encoding == b"br" and config.brotli_level >= 0:
        return brotli_compress(data, config.brotli_level, config.brotli_window_log or 0)
    if encoding == b"zstd" and config.zstd_level >= 0:
        return zstd_compress(data, config.zstd_level, config.zstd_window_log or 0)
    if encoding == b"gzip" and config.gzip_level >= 0:
        window = config.gzip_window_bits
        compressor = zlib.compressobj(
            config.gzip_level,
            zlib.DEFLATED,
            16 + (_GZIP_WINDOW_BITS_MAX if window is None else window),
        )
        return compressor.compress(data) + compressor.flush()
    raise StarioError(
        f"Unsupported or disabled content encoding: {encoding!r}",
        help_text="Pick an encoding enabled by CompressionConfig levels.",
    )


def compression_config_from_env() -> CompressionConfig:
    """Read `STARIO_COMPRESS_*` codec levels, thresholds, and preference."""
    try:
        raw_preference = env_optional_str("STARIO_COMPRESS_PREFERENCE")
        preference = (
            None
            if raw_preference is None
            else [name.strip() for name in raw_preference.split(",") if name.strip()]
        )
        raw_stream_window = env_optional_str("STARIO_COMPRESS_STREAM_WINDOW_LOG")
        stream_window_log: int | None
        if raw_stream_window is None:
            stream_window_log = DEFAULT_STREAM_WINDOW_LOG
        elif raw_stream_window.strip().lower() == "none":
            stream_window_log = None
        else:
            try:
                stream_window_log = int(raw_stream_window)
            except ValueError as exc:
                raise ValueError(
                    "STARIO_COMPRESS_STREAM_WINDOW_LOG must be an integer "
                    f"or 'none' (got {raw_stream_window!r})."
                ) from exc
        return CompressionConfig(
            min_size=env_int("STARIO_COMPRESS_MIN_SIZE", DEFAULT_MIN_SIZE),
            zstd_level=env_int("STARIO_COMPRESS_ZSTD_LEVEL", DEFAULT_ZSTD_LEVEL),
            zstd_window_log=env_optional_int("STARIO_COMPRESS_ZSTD_WINDOW_LOG"),
            brotli_level=env_int("STARIO_COMPRESS_BROTLI_LEVEL", DEFAULT_BROTLI_LEVEL),
            brotli_window_log=env_optional_int("STARIO_COMPRESS_BROTLI_WINDOW_LOG"),
            gzip_level=env_int("STARIO_COMPRESS_GZIP_LEVEL", DEFAULT_GZIP_LEVEL),
            gzip_window_bits=env_optional_int("STARIO_COMPRESS_GZIP_WINDOW_BITS"),
            stream_window_log=stream_window_log,
            preference=preference,
        )
    except ValueError as exc:
        raise StarioError(str(exc)) from exc
