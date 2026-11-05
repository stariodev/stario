"""Tests for the request view used by handlers (Cython Request)."""

import pytest

from stario.exceptions import RequestBodyError
from stario.http.headers import Headers, RequestHeaders
from stario.http.host import host_without_port
from stario_cython.exchange import Request
from tests.helpers import make_request as _make_request


class TestRequestBasic:
    def test_defaults_and_overrides(self):
        req = _make_request()
        assert req.method == "GET"
        assert req.path == "/"
        assert req.protocol_version == "1.1"
        assert req.keep_alive is True

        post = _make_request(method="POST", path="/users/123")
        assert post.method == "POST"
        assert post.path == "/users/123"

    def test_query_is_built_on_first_read(self):
        req = Request(method="GET", path="/", headers=Headers(), body=b"")
        first = req.query
        second = req.query
        assert first is second
        assert first.get("missing") is None
        assert bool(first) is False


class TestRequestCookies:
    def test_multiple_cookie_headers_merge(self):
        hdrs = Headers()
        hdrs.add("Cookie", "a=1")
        hdrs.add("Cookie", "b=2")
        req = Request(method="GET", path="/", headers=hdrs)
        assert req.cookies == {"a": "1", "b": "2"}
        assert req.cookies.get("a") == "1"
        assert "b" in req.cookies

    def test_get_does_not_require_as_dict(self):
        hdrs = Headers()
        hdrs.add("Cookie", "session=abc; unused=1; unused=2")
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("session") == "abc"
        assert req.cookies.get("missing") is None
        assert "session" in req.cookies
        assert "unused" in req.cookies
        assert "missing" not in req.cookies

    def test_get_later_header_and_pair_win(self):
        hdrs = Headers()
        hdrs.add("Cookie", "a=1; session=old")
        hdrs.add("Cookie", "a=2; session=new; a=3")
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("session") == "new"
        assert req.cookies.get("a") == "3"

    def test_get_quoted_semicolon_value(self):
        hdrs = Headers()
        hdrs.add("Cookie", 'x="a;b"; y=2')
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("x") == "a;b"
        assert req.cookies.get("y") == "2"

    def test_empty_value_is_present(self):
        hdrs = Headers()
        hdrs.add("Cookie", "session=")
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("session") == ""
        assert "session" in req.cookies
        assert bool(req.cookies) is True

    def test_get_latin1_cookie_name(self):
        hdrs = Headers()
        hdrs.add("Cookie", "café=1")
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("café") == "1"


class TestRequestHost:
    def test_host_strips_port(self):
        req = _make_request(headers={"Host": "Example.COM:8080"})
        assert req.host == "example.com"

    def test_host_ipv6_with_port(self):
        req = _make_request(headers={"Host": "[::1]:8000"})
        assert req.host == "[::1]"

    def test_host_strips_whitespace(self):
        req = _make_request(headers={"Host": "  Example.COM:8080  "})
        assert req.host == "example.com"

    def test_request_host_normalization(self):
        cases = {
            "": "",
            "  ": "",
            "Example.COM:8080": "example.com",
            "  Example.COM:8080  ": "example.com",
            "[::1]:8000": "[::1]",
            "[::1]": "[::1]",
            "localhost": "localhost",
            "example.com:": "example.com:",
            "[::1]foo": "[::1]foo",
            "Example.COM:80a": "example.com:80a",
            "EXAMPLE.COM": "example.com",
            "example.com.:443": "example.com",
            "example.com..": "example.com..",
        }
        for raw, expected in cases.items():
            hdrs = Headers()
            if raw:
                hdrs.set("Host", raw)
            req = Request(method="GET", path="/", headers=hdrs, body=b"")
            assert req.host == expected, raw
            assert host_without_port(raw) == expected, raw

    def test_host_is_lowercased_before_routing(self):
        req = _make_request(headers={"Host": "API.Example.COM"})
        assert req.host == "api.example.com"


class TestRequestBody:
    async def test_no_body(self):
        req = _make_request()
        body = await req.body()
        assert body == b""

    async def test_body_multiple_reads(self):
        req = _make_request(body=b"data")
        body1 = await req.body()
        body2 = await req.body()
        assert body1 == body2 == b"data"

    async def test_body_max_size_is_per_call_limit(self):
        req = Request(method="POST", path="/", body=b"hello")

        with pytest.raises(RequestBodyError) as excinfo:
            await req.body(max_size=4)

        assert excinfo.value.status_code == 413
        assert await req.body(max_size=5) == b"hello"

    async def test_body_none_returns_empty(self):
        req = Request(method="GET", path="/", body=b"")
        assert await req.body() == b""

    async def test_stream_yields_a_bytes_body_once(self):
        req = Request(method="POST", path="/", body=b"chunk")
        assert [chunk async for chunk in req.stream()] == [b"chunk"]

    async def test_stream_max_size_limits_a_bytes_body(self):
        req = Request(method="POST", path="/", body=b"chunk")

        with pytest.raises(RequestBodyError) as excinfo:
            _ = [chunk async for chunk in req.stream(max_size=4)]

        assert excinfo.value.status_code == 413
        assert [chunk async for chunk in req.stream(max_size=5)] == [b"chunk"]

    async def test_negative_stream_max_size_is_rejected(self):
        req = Request(method="POST", path="/", body=b"chunk")
        with pytest.raises(ValueError, match="max_size"):
            _ = [chunk async for chunk in req.stream(max_size=-1)]

    async def test_huge_max_size_is_accepted(self):
        req = Request(method="POST", path="/", body=b"chunk")
        assert await req.body(max_size=2**70) == b"chunk"
        assert [chunk async for chunk in req.stream(max_size=2**70)] == [b"chunk"]


class TestRequestHeaders:
    def test_constructed_request_gets_read_only_request_headers(self):
        req = Request(headers={"Host": "Example.com", "X-Many": ["a", "b"]})
        assert type(req.headers) is RequestHeaders
        assert req.headers.get("host") == "Example.com"
        assert req.headers.getlist("x-many") == ["a", "b"]
        assert req.host == "example.com"
        with pytest.raises(Exception, match="read-only"):
            req.headers.set("x", "y")  # pyrefly: ignore[missing-attribute]
        with pytest.raises(AttributeError):
            req.headers = RequestHeaders()  # type: ignore[misc]

    def test_request_headers_copy_response_headers(self):
        hdrs = Headers()
        hdrs.add("Cookie", "a=1")
        view = RequestHeaders(hdrs)
        hdrs.add("Cookie", "b=2")
        assert view.getlist("cookie") == ["a=1"]
        assert Request(headers=view).headers is view
