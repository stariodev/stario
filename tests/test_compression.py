"""Unit tests for compression negotiation and one-shot encoding helpers."""

import zlib
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from stario.exceptions import StarioError
from stario.http.compression import (
    CompressionConfig,
    compress_bytes,
    compression_config_from_env,
    content_type_is_compressible,
    negotiate_content_encoding,
    parse_accept_encoding,
)
from stario.http.headers import Headers
from stario_cython.codecs import brotli_decompress, zstd_decompress


def test_parse_accept_encoding_simple_list() -> None:
    assert parse_accept_encoding("gzip, br") == {b"gzip": 1.0, b"br": 1.0}


def test_parse_accept_encoding_q_values() -> None:
    parsed = parse_accept_encoding("gzip;q=0.5, br;q=1")
    assert parsed[b"gzip"] == 0.5
    assert parsed[b"br"] == 1.0


def test_parse_accept_encoding_wildcard() -> None:
    assert parse_accept_encoding("*;q=0.8")[b"*"] == 0.8


def test_parse_accept_encoding_long_header_skips_the_cache() -> None:
    header = ", ".join(f"x{i}" for i in range(200))
    assert len(header) > 512
    assert parse_accept_encoding(header)[b"x199"] == 1.0
    assert parse_accept_encoding(header.encode())[b"x0"] == 1.0


def test_negotiate_prefers_br_over_gzip() -> None:
    assert negotiate_content_encoding("gzip, br", (b"br", b"gzip")) == b"br"


def test_negotiate_prefers_zstd_over_br_by_default() -> None:
    # Dynamic/streamed responses prefer zstd: near-equal wire size, less CPU.
    assert negotiate_content_encoding("br, zstd", (b"br", b"zstd")) == b"zstd"


def test_negotiate_custom_preference_can_lead_with_br() -> None:
    # Precompressed statics prefer br: encode-once bytes, so wire size wins.
    assert (
        negotiate_content_encoding(
            "br, zstd", (b"br", b"zstd"), (b"br", b"zstd", b"gzip")
        )
        == b"br"
    )


def test_negotiate_identity_wins_when_higher_q() -> None:
    assert negotiate_content_encoding("identity;q=1, gzip;q=0.5", (b"gzip",)) is None


def test_merge_vary_appends_and_skips_duplicates() -> None:
    headers = Headers()
    headers.unsafe_merge_vary(b"accept-encoding")
    assert headers.unsafe_get(b"vary") == b"accept-encoding"

    headers.unsafe_set(b"vary", b"accept-language")
    headers.unsafe_merge_vary(b"accept-encoding")
    assert headers.unsafe_get(b"vary") == b"accept-language, accept-encoding"

    headers.unsafe_set(b"vary", b"Accept-Encoding")
    headers.unsafe_merge_vary(b"accept-encoding")
    assert headers.unsafe_get(b"vary") == b"Accept-Encoding"

    headers.unsafe_set(b"vary", b"*")
    headers.unsafe_merge_vary(b"accept-encoding")
    assert headers.unsafe_get(b"vary") == b"*"


def test_content_type_is_compressible_skips_images() -> None:
    assert content_type_is_compressible(b"image/png") is False
    assert content_type_is_compressible(b"image/svg+xml") is True
    assert content_type_is_compressible(b"image/svg+xml; charset=utf-8") is True
    assert content_type_is_compressible(b"text/html; charset=utf-8") is True
    assert content_type_is_compressible(b"") is False
    assert content_type_is_compressible(b"; charset=utf-8") is False


def test_negotiation_follows_config_preference() -> None:
    custom = CompressionConfig(preference=("br",))
    assert (
        negotiate_content_encoding(
            "br, zstd", custom.enabled_encodings(), custom.preference
        )
        == b"br"
    )
    default = CompressionConfig()
    for accept in ("br, zstd", "gzip, zstd"):
        assert (
            negotiate_content_encoding(
                accept, default.enabled_encodings(), default.preference
            )
            == b"zstd"
        )


def test_preference_rejects_unknown_and_duplicate_names() -> None:
    for bad in (("deflate",), ("br", "br"), ("zstd", "br", "zstd")):
        with pytest.raises(StarioError):
            CompressionConfig(preference=bad)


def test_preference_fills_unlisted_names_in_default_order() -> None:
    compression = CompressionConfig(preference=("br",))
    assert compression.preference == (b"br", b"zstd", b"gzip")


def test_enabled_encodings_follows_preference_order() -> None:
    compression = CompressionConfig(preference=("gzip", "br"))
    assert compression.enabled_encodings() == (b"gzip", b"br", b"zstd")


def test_compression_config_from_env_reads_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STARIO_COMPRESS_PREFERENCE", "br, gzip")
    config = compression_config_from_env()
    assert config.preference == (b"br", b"gzip", b"zstd")


def _load_brotli_policy() -> Callable[[tuple[int, ...]], bool]:
    # Importing setup.py runs setuptools, so exec only the policy function.
    source = Path("setup.py").read_text()
    start = source.index("def _brotli_has_cve_2026_111497_fix")
    end = source.index("def _require_brotli_version")
    namespace: dict[str, object] = {}
    exec(source[start:end], namespace)
    return cast(
        Callable[[tuple[int, ...]], bool],
        namespace["_brotli_has_cve_2026_111497_fix"],
    )


_brotli_has_cve_2026_111497_fix = _load_brotli_policy()


@pytest.mark.parametrize(
    ("version", "fixed"),
    [
        ((1, 0, 2), False),
        ((1, 0, 9), False),
        ((1, 0, 10), True),
        ((1, 1, 0), False),
        ((1, 1, 1), True),
        ((1, 2, 0), False),
        ((1, 2, 1), True),
        ((1, 3, 0), True),
        ((2, 0, 0), True),
    ],
)
def test_brotli_cve_2026_111497_version_policy(
    version: tuple[int, ...], fixed: bool
) -> None:
    assert _brotli_has_cve_2026_111497_fix(version) is fixed


def test_linked_brotli_decoder_rejects_invalid_block_switch() -> None:
    # CVE-2026-111497. Node's regression input from
    # test/parallel/test-zlib-invalid-input.js: a block switch in a stream
    # that declared a single block type. A fixed decoder reports a format
    # error instead of continuing.
    from stario_cython.codecs import BrotliDecoder, brotli_decoder_version

    version = brotli_decoder_version()
    assert _brotli_has_cve_2026_111497_fix(version), version

    invalid = bytes.fromhex("200010414141fcffff07002228f00b60f7f41f000000")
    with pytest.raises(StarioError, match="brotli decompression failed"):
        brotli_decompress(invalid)

    decoder = BrotliDecoder()

    def decode_each_byte() -> None:
        for offset in range(len(invalid)):
            decoder.decompress(invalid[offset : offset + 1])

    with pytest.raises(StarioError, match="brotli decompression failed"):
        decode_each_byte()


def test_compress_bytes_round_trips_each_codec() -> None:
    body = b"one-shot precompressed body " * 64
    config = CompressionConfig(
        zstd_window_log=21, brotli_window_log=22, gzip_window_bits=15
    )
    assert brotli_decompress(compress_bytes(config, b"br", body)) == body
    assert zstd_decompress(compress_bytes(config, b"zstd", body)) == body
    assert zlib.decompress(compress_bytes(config, b"gzip", body), wbits=31) == body
    default = CompressionConfig()
    assert zlib.decompress(compress_bytes(default, b"gzip", body), wbits=31) == body
    with pytest.raises(StarioError, match="disabled"):
        compress_bytes(CompressionConfig(gzip_level=-1), b"gzip", body)
    with pytest.raises(StarioError, match="Unsupported"):
        compress_bytes(default, b"deflate", body)


def test_stream_window_log_default_and_bounds() -> None:
    assert CompressionConfig().stream_window_log is None
    assert CompressionConfig(stream_window_log=16).stream_window_log == 16
    assert CompressionConfig(stream_window_log=24).stream_window_log == 24
    for bad in (9, 25):
        with pytest.raises(StarioError, match="stream_window_log"):
            CompressionConfig(stream_window_log=bad)
    assert CompressionConfig() == CompressionConfig()
    assert CompressionConfig() == CompressionConfig(stream_window_log=None)
    assert CompressionConfig() != CompressionConfig(stream_window_log=20)


def test_compression_config_from_env_stream_window_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STARIO_COMPRESS_STREAM_WINDOW_LOG", "18")
    assert compression_config_from_env().stream_window_log == 18
    monkeypatch.setenv("STARIO_COMPRESS_STREAM_WINDOW_LOG", "none")
    assert compression_config_from_env().stream_window_log is None
    monkeypatch.delenv("STARIO_COMPRESS_STREAM_WINDOW_LOG")
    assert compression_config_from_env().stream_window_log is None
    monkeypatch.setenv("STARIO_COMPRESS_STREAM_WINDOW_LOG", "bogus")
    with pytest.raises(StarioError, match="STREAM_WINDOW_LOG"):
        compression_config_from_env()
