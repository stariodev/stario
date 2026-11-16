"""Tests for the process-wide Stario JSON codec."""

import json as json_module
import warnings

import pytest

import stario.json as stario_json
from stario import App, Context, Route, Writer
from stario.datastar import SSE, data, read_signals
from stario.telemetry.formatters import dumps_json
from stario.testing import TestClient
from tests.helpers import make_request, make_writer_raw


class RecordingCodec:
    def __init__(self) -> None:
        self.dumped: list[tuple[object, stario_json.JsonDefault | None]] = []
        self.dumped_bytes: list[tuple[object, stario_json.JsonDefault | None]] = []
        self.loaded: list[str | bytes | bytearray] = []

    def dumps(
        self,
        value: object,
        /,
        *,
        default: stario_json.JsonDefault | None = None,
    ) -> str:
        self.dumped.append((value, default))
        return json_module.dumps(
            value,
            default=default,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def dumps_bytes(
        self,
        value: object,
        /,
        *,
        default: stario_json.JsonDefault | None = None,
    ) -> bytes:
        self.dumped_bytes.append((value, default))
        return json_module.dumps(
            value,
            default=default,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def loads(self, value: str | bytes | bytearray, /) -> object:
        self.loaded.append(value)
        return json_module.loads(value)


def _recorders():
    encoded: list[object] = []
    loaded: list[object] = []

    def encode(value: object) -> str:
        encoded.append(value)
        return json_module.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def decode(raw: str | bytes | bytearray) -> object:
        loaded.append(raw)
        return json_module.loads(raw)

    return encoded, loaded, encode, decode


@pytest.fixture(autouse=True)
def _restore_codec():
    encode = stario_json._encode
    decode = stario_json._decode
    legacy = stario_json._legacy
    yield
    stario_json._encode = encode
    stario_json._decode = decode
    stario_json._legacy = legacy


def test_default_codec_uses_compact_utf8_bytes_and_accepts_text() -> None:
    assert stario_json.dumps({"msg": "日本語"}) == '{"msg":"日本語"}'
    assert stario_json.dumps_bytes({"ok": True}) == b'{"ok":true}'
    assert stario_json.dumps({"same": [1, 2]}).encode() == stario_json.dumps_bytes(
        {"same": [1, 2]}
    )
    assert stario_json.loads('{"count":2}') == {"count": 2}
    assert stario_json.loads(b'["a",1]') == ["a", 1]
    assert stario_json.loads(bytearray(b'{"mutable":true}')) == {"mutable": True}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_default_codec_rejects_non_json_floats(value: float) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        stario_json.dumps(value)
    with pytest.raises(ValueError, match="JSON compliant"):
        stario_json.dumps_bytes(value)


def test_set_codec_accepts_an_encode_that_returns_bytes() -> None:
    calls = 0

    def encode(value: object) -> bytes:
        nonlocal calls
        calls += 1
        return json_module.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode()

    stario_json.set_codec(encode, json_module.loads)
    assert stario_json.dumps({"a": 1}) == '{"a":1}'
    assert calls == 1
    assert stario_json.dumps_bytes({"a": 1}) == b'{"a":1}'
    assert calls == 2


def test_set_codec_accepts_an_encode_that_returns_text() -> None:
    calls = 0

    def encode(value: object) -> str:
        nonlocal calls
        calls += 1
        return json_module.dumps(value, ensure_ascii=False, separators=(",", ":"))

    stario_json.set_codec(encode, json_module.loads)
    assert stario_json.dumps({"a": 1}) == '{"a":1}'
    assert calls == 1
    assert stario_json.dumps_bytes({"a": 1}) == b'{"a":1}'
    assert calls == 2


def test_loads_uses_decode_for_text_bytes_and_bytearray() -> None:
    loaded: list[object] = []

    def decode(raw: str | bytes | bytearray) -> object:
        loaded.append(raw)
        return json_module.loads(raw)

    stario_json.set_codec(json_module.dumps, decode)
    for raw in ('{"a":1}', b'{"a":1}', bytearray(b'{"a":1}')):
        assert stario_json.loads(raw) == {"a": 1}
    assert loaded == ['{"a":1}', b'{"a":1}', bytearray(b'{"a":1}')]


def test_set_codec_accepts_a_deprecated_codec_object() -> None:
    codec = RecordingCodec()
    with pytest.warns(DeprecationWarning, match="two functions"):
        stario_json.set_codec(codec)

    assert stario_json.dumps({"a": 1}) == '{"a":1}'
    assert stario_json.dumps_bytes({"a": 1}) == b'{"a":1}'
    assert stario_json.loads('{"a":1}') == {"a": 1}
    assert codec.dumped_bytes == [({"a": 1}, None), ({"a": 1}, None)]
    assert codec.loaded == ['{"a":1}']

    marker = object()
    with pytest.warns(DeprecationWarning, match="default="):
        out = stario_json.dumps(marker, default=str)
    assert codec.dumped == [(marker, str)]
    assert out.startswith('"<object object')


def test_default_kwarg_warns_and_falls_back_to_stdlib() -> None:
    stario_json.set_codec(lambda _v: "{}", json_module.loads)
    marker = object()
    with pytest.warns(DeprecationWarning, match="default="):
        out = stario_json.dumps(marker, default=str)
    assert out.startswith('"<object object')
    with pytest.warns(DeprecationWarning, match="default="):
        out_bytes = stario_json.dumps_bytes(marker, default=str)
    assert out_bytes == out.encode()


def test_stdlib_codec_construction_warns() -> None:
    with pytest.warns(DeprecationWarning, match="StdlibJsonCodec"):
        stario_json.StdlibJsonCodec()


def test_set_codec_rejects_non_callables() -> None:
    with pytest.raises(TypeError):
        stario_json.set_codec(lambda _v: b"")  # pyrefly: ignore[bad-argument-type]
    with pytest.raises(TypeError):
        stario_json.set_codec(1, 2)  # pyrefly: ignore[bad-argument-type]


def test_dumps_json_falls_back_to_stdlib_without_a_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert dumps_json({"a": object()}).startswith('{"a":"<object object')


def test_set_codec_can_replace_a_codec_after_use() -> None:
    seen_first: list[object] = []
    seen_second: list[object] = []

    def encode_first(value: object) -> str:
        seen_first.append(value)
        return "{}"

    def encode_second(value: object) -> str:
        seen_second.append(value)
        return "{}"

    stario_json.set_codec(encode_first, json_module.loads)
    stario_json.dumps({"first": True})
    stario_json.set_codec(encode_second, json_module.loads)
    stario_json.dumps_bytes({"second": True})

    assert seen_first == [{"first": True}]
    assert seen_second == [{"second": True}]


async def test_framework_json_paths_use_configured_codec() -> None:
    encoded, loaded, encode, decode = _recorders()
    stario_json.set_codec(encode, decode)

    json_writer, _sink, loop = make_writer_raw()
    writer, _sink2, _loop2 = make_writer_raw()
    try:
        json_writer.json({"response": True})
        assert json_writer.body == b'{"response":true}'
        SSE(writer).patch_signals({"sse": True})
    finally:
        loop.close()

    data.signals({"attribute": True})

    assert await read_signals(
        make_request(method="POST", body=b'{"request":true}')
    ) == {"request": True}
    assert dumps_json({"unknown": object()}).startswith('{"unknown":"<object object')

    assert {"response": True} in encoded
    assert {"sse": True} in encoded
    assert {"attribute": True} in encoded
    assert loaded == [b'{"request":true}']


async def test_writer_json_uses_configured_codec_through_testclient() -> None:
    """`w.json` on the real RequestHandle honour `set_codec`, not the stdlib."""
    encoded, _loaded, encode, decode = _recorders()
    stario_json.set_codec(encode, decode)

    async def handler(_c: Context, w: Writer) -> None:
        w.json({"response": True})

    app = App()
    app.add(Route("GET /"), handler)
    async with TestClient(app) as client:
        r = await client.get("/")

    assert r.status_code == 200
    assert {"response": True} in encoded
    assert (
        r.content
        == json_module.dumps(
            {"response": True}, ensure_ascii=False, separators=(",", ":")
        ).encode()
    )
