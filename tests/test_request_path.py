"""Tests for request-path decoding and canonicalization."""

import pytest

from stario_cython.exchange import canonical_request_path, decode_request_path


class TestDecodeRequestPath:
    def test_plain_path(self) -> None:
        assert decode_request_path(b"/users") == "/users"
        assert decode_request_path(b"/") == "/"

    def test_percent_encoded_space(self) -> None:
        assert decode_request_path(b"/hello%20world") == "/hello world"

    def test_encoded_slash_is_fully_decoded(self) -> None:
        assert decode_request_path(b"/files/a%2Fb") == "/files/a/b"
        assert decode_request_path(b"/files/a%252Fb") == "/files/a%2Fb"

    def test_utf8(self) -> None:
        assert decode_request_path(b"/caf%C3%A9") == "/café"
        assert decode_request_path("/café".encode()) == "/café"

    @pytest.mark.parametrize(
        "raw",
        [
            b"/bad%",
            b"/bad%2",
            b"/bad%zz",
            b"/%00",
            b"/a%0D%0Ab",
            b"/%7F",
            b"/%C3%28",
            b"/%C0%AF",
            b"/%ED%A0%80",
            b"/%C3/%A9",
            b"/a#b",
            b"/a b",
            b"relative",
            b"",
        ],
    )
    def test_invalid_paths_raise(self, raw: bytes) -> None:
        with pytest.raises(ValueError, match="invalid request path"):
            decode_request_path(raw)


class TestCanonicalRequestPath:
    @pytest.mark.parametrize(
        "raw",
        [b"/", b"/users", b"/a//b", b"/a/%2F", b"/%2e%2ex", b"/a/...", b"/a;b"],
    )
    def test_canonical_paths_route_as_is(self, raw: bytes) -> None:
        assert canonical_request_path(raw) == (200, None)

    @pytest.mark.parametrize(
        ("raw", "target"),
        [
            (b"/users/", b"/users"),
            (b"/users//", b"/users"),
            (b"/a/./b", b"/a/b"),
            (b"/a/../b", b"/b"),
            (b"/../../a", b"/a"),
            (b"/a/%2e%2E/b", b"/b"),
            (b"/a/.%2e", b"/"),
            (b"/a/b/.", b"/a/b"),
            (b"/x%2Fy/./z", b"/x%2Fy/z"),
            (b"//evil.test/", b"/evil.test"),
            (b"/.//evil.test", b"/evil.test"),
            (b"/\\evil.test/", b"/%5Cevil.test"),
        ],
    )
    def test_redirect_targets(self, raw: bytes, target: bytes) -> None:
        assert canonical_request_path(raw) == (308, target)

    @pytest.mark.parametrize("raw", [b"/%zz", b"/%00", b"/\xc3\xa9", b"*", b"/a?b"])
    def test_invalid_paths(self, raw: bytes) -> None:
        assert canonical_request_path(raw) == (400, None)
