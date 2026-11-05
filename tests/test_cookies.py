"""Tests for `stario.cookies` helpers."""

from datetime import UTC, datetime

import pytest

import stario.cookies as cookies
from stario.exceptions import StarioError
from tests.helpers import make_request, make_writer


def _cookie_header(w) -> str:
    lines = w.headers.unsafe_getlist(b"set-cookie")
    return b";".join(lines).decode("latin-1")


def test_expires_int_and_datetime() -> None:
    w = make_writer()
    cookies.set_cookie(w, "sid", "v", expires=1_700_000_000)
    combined = _cookie_header(w).lower()
    assert "expires=" in combined
    assert "1970" not in combined

    w2 = make_writer()
    expires = datetime(2030, 1, 15, 12, 0, 0, tzinfo=UTC)
    cookies.set_cookie(w2, "sid", "v", expires=expires)
    combined2 = _cookie_header(w2).lower()
    assert "expires=" in combined2
    assert "2030" in combined2


def test_set_cookie_max_age_and_defaults() -> None:
    w = make_writer()
    cookies.set_cookie(w, "sid", "v", max_age=3600)
    combined = _cookie_header(w).lower()
    assert "max-age=3600" in combined
    assert "samesite=lax" in combined
    assert "path=/" in combined


def test_set_cookie_httponly_secure_domain_path() -> None:
    w = make_writer()
    cookies.set_cookie(
        w,
        "sid",
        "v",
        httponly=True,
        secure=True,
        domain="example.com",
        path="/app",
    )
    combined = _cookie_header(w).lower()
    assert "httponly" in combined
    assert "secure" in combined
    assert "domain=example.com" in combined
    assert "path=/app" in combined


def test_delete_cookie_clears_with_matching_scope() -> None:
    w = make_writer()
    cookies.delete_cookie(
        w,
        "sid",
        path="/app",
        domain="example.com",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    combined = _cookie_header(w).lower()
    assert "sid=" in combined
    assert "max-age=0" in combined
    assert "path=/app" in combined
    assert "domain=example.com" in combined
    assert "secure" in combined
    assert "httponly" in combined
    assert "samesite=strict" in combined


def test_parse_cookie_headers_quoted_semicolon() -> None:
    parsed = cookies.parse_cookie_headers(['x="a;b"; y=2'])
    assert parsed == {"x": "a;b", "y": "2"}


def test_parse_cookie_headers_later_header_wins() -> None:
    parsed = cookies.parse_cookie_headers(["a=1", "a=2; b=3"])
    assert parsed == {"a": "2", "b": "3"}


@pytest.mark.parametrize("explicit_secure", [False, True])
def test_samesite_none_sets_secure(explicit_secure: bool) -> None:
    w = make_writer()
    cookies.set_cookie(
        w,
        "sid",
        "v",
        samesite="none",
        secure=explicit_secure,
    )
    lines = w.headers.unsafe_getlist(b"set-cookie")
    assert lines
    combined = b";".join(lines).decode("latin-1").lower()
    assert "samesite=none" in combined
    assert "secure" in combined


def test_set_cookie_rejects_semicolon_in_path() -> None:
    w = make_writer()
    with pytest.raises(StarioError, match="Invalid cookie path"):
        cookies.set_cookie(w, "sid", "v", path="/; HttpOnly")


def test_set_cookie_rejects_comma_in_domain() -> None:
    w = make_writer()
    with pytest.raises(StarioError, match="Invalid cookie domain"):
        cookies.set_cookie(w, "sid", "v", domain="example.com, evil.test")


@pytest.mark.parametrize(
    "samesite", ["lax; Domain=evil.example", "none\r\nX: y", "loose"]
)
def test_invalid_samesite_is_rejected(samesite: str) -> None:
    w = make_writer()
    with pytest.raises(StarioError, match="samesite"):
        cookies.set_cookie(w, "sid", "v", samesite=samesite)  # type: ignore[arg-type]
    assert not w.headers.unsafe_getlist(b"set-cookie")


KEY = "k" * 32
OLD_KEY = "o" * 32


def _set_value(jar: cookies.SignedCookie, data: dict) -> str:
    w = make_writer()
    jar.set(w, data)
    header = _cookie_header(w)
    name_value = header.split(";", 1)[0].strip()
    name, _, value = name_value.partition("=")
    assert name == jar.name
    return value


def _get(jar: cookies.SignedCookie, value: str, name: str | None = None):
    req = make_request(headers={"cookie": f"{name or jar.name}={value}"})
    return jar.get(req)


class TestSignedCookie:
    def test_round_trip(self) -> None:
        jar = cookies.SignedCookie("session", keys=[KEY])
        value = _get(jar, _set_value(jar, {"user": "42", "epoch": 3}))
        assert value is not None
        assert value.data == {"user": "42", "epoch": 3}
        assert value.expires_at - value.issued_at == jar.max_age
        assert 0 <= value.age < 5

    def test_attributes(self) -> None:
        jar = cookies.SignedCookie("session", keys=[KEY], max_age=60)
        w = make_writer()
        jar.set(w, {"a": 1})
        header = _cookie_header(w).lower()
        for part in ("httponly", "secure", "samesite=lax", "max-age=60", "path=/"):
            assert part in header
        w2 = make_writer()
        jar.delete(w2)
        deleted = _cookie_header(w2).lower()
        assert "max-age=0" in deleted
        assert "secure" in deleted

    def test_missing_and_garbage(self) -> None:
        jar = cookies.SignedCookie("session", keys=[KEY])
        assert jar.get(make_request()) is None
        for bad in (
            "x",
            "v1.",
            "v1..",
            "v2.abc.def",
            "v1.abc.def",
            "...",
            "v1.\u00e9x.\u00e9y",
        ):
            assert _get(jar, bad) is None

    def test_tampered_payload_or_signature(self) -> None:
        jar = cookies.SignedCookie("session", keys=[KEY])
        value = _set_value(jar, {"user": "42"})
        version, payload, signature = value.split(".")
        forged = cookies._b64encode(b'{"d":{"user":"1"},"i":0,"e":9999999999}')
        assert _get(jar, f"{version}.{forged}.{signature}") is None
        flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
        assert _get(jar, f"{version}.{payload}.{flipped}") is None

    def test_value_does_not_move_to_another_cookie_name(self) -> None:
        session = cookies.SignedCookie("session", keys=[KEY])
        flash = cookies.SignedCookie("flash", keys=[KEY])
        value = _set_value(flash, {"user": "42"})
        assert _get(session, value) is None

    def test_other_key_fails(self) -> None:
        jar = cookies.SignedCookie("session", keys=[KEY])
        other = cookies.SignedCookie("session", keys=[OLD_KEY])
        assert _get(other, _set_value(jar, {"a": 1})) is None

    def test_key_rotation(self) -> None:
        old = cookies.SignedCookie("session", keys=[OLD_KEY])
        rotated = cookies.SignedCookie("session", keys=[KEY, OLD_KEY])
        old_value = _set_value(old, {"user": "42"})
        verified = _get(rotated, old_value)
        assert verified is not None
        assert verified.data == {"user": "42"}
        new_value = _set_value(rotated, {"user": "42"})
        assert _get(cookies.SignedCookie("session", keys=[KEY]), new_value) is not None
        assert _get(old, new_value) is None

    def test_expiry_is_checked_by_the_server(self, monkeypatch) -> None:
        jar = cookies.SignedCookie("session", keys=[KEY], max_age=60)
        value = _set_value(jar, {"a": 1})
        now = cookies.time.time()
        monkeypatch.setattr(cookies.time, "time", lambda: now + 59)
        assert _get(jar, value) is not None
        monkeypatch.setattr(cookies.time, "time", lambda: now + 61)
        assert _get(jar, value) is None

    def test_payload_must_be_an_object(self) -> None:
        jar = cookies.SignedCookie("session", keys=[KEY])
        key = jar._keys[0]
        for raw in (b"[1]", b'{"d":[1],"i":1,"e":9999999999}', b'{"d":{},"i":"1"}'):
            body = f"v1.{cookies._b64encode(raw)}"
            assert _get(jar, f"{body}.{jar._sign(key, body)}") is None

    def test_too_large(self) -> None:
        jar = cookies.SignedCookie("session", keys=[KEY])
        with pytest.raises(StarioError, match="too large"):
            jar.set(make_writer(), {"blob": "x" * 4000})

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"keys": []}, "at least one key"),
            ({"keys": ["short"]}, "at least 32 bytes"),
            ({"keys": [KEY, b"short"]}, "at least 32 bytes"),
            ({"keys": [KEY], "max_age": 0}, "max_age"),
        ],
    )
    def test_invalid_config(self, kwargs: dict, match: str) -> None:
        with pytest.raises(StarioError, match=match):
            cookies.SignedCookie("session", **kwargs)

    @pytest.mark.parametrize("name", ["", "a b", "a;b", "ż"])
    def test_invalid_name(self, name: str) -> None:
        with pytest.raises(StarioError, match="name"):
            cookies.SignedCookie(name, keys=[KEY])

    def test_host_prefix_round_trip(self) -> None:
        jar = cookies.SignedCookie("__Host-session", keys=[KEY])
        value = _get(jar, _set_value(jar, {"user": "42"}))
        assert value is not None
        assert value.data == {"user": "42"}

    @pytest.mark.parametrize(
        ("name", "kwargs"),
        [
            ("__Host-session", {"secure": False}),
            ("__Host-session", {"path": "/app"}),
            ("__host-session", {"domain": "example.com"}),
            ("__Secure-session", {"secure": False}),
        ],
    )
    def test_prefix_needs_its_flags(self, name: str, kwargs: dict) -> None:
        with pytest.raises(StarioError, match="cookie needs"):
            cookies.SignedCookie(name, keys=[KEY], **kwargs)


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("__Host-sid", {}),
        ("__Host-sid", {"secure": True, "path": "/app"}),
        ("__Host-sid", {"secure": True, "domain": "example.com"}),
        ("__Secure-sid", {}),
    ],
)
def test_set_cookie_prefix_needs_its_flags(name: str, kwargs: dict) -> None:
    with pytest.raises(StarioError, match="cookie needs"):
        cookies.set_cookie(make_writer(), name, "v", **kwargs)
    with pytest.raises(StarioError, match="cookie needs"):
        cookies.delete_cookie(make_writer(), name, **kwargs)


def test_set_cookie_prefix_with_its_flags() -> None:
    w = make_writer()
    cookies.set_cookie(w, "__Host-sid", "v", secure=True)
    cookies.set_cookie(w, "__Secure-sid", "v", samesite="none")
    assert len(w.headers.unsafe_getlist(b"set-cookie")) == 2


async def test_signed_cookie_through_the_test_client() -> None:
    from stario import App, Context, Route, Writer
    from stario.testing import TestClient

    jar = cookies.SignedCookie("session", keys=[KEY], secure=False)

    async def login(_c: Context, w: Writer) -> None:
        jar.set(w, {"user": "ada"})
        w.text("in")

    async def me(c: Context, w: Writer) -> None:
        value = jar.get(c.req)
        w.text(value.data["user"] if value else "nobody")

    async def logout(_c: Context, w: Writer) -> None:
        jar.delete(w)
        w.text("out")

    app = App()
    app.add(Route("POST /login"), login)
    app.add(Route("GET /me"), me)
    app.add(Route("POST /logout"), logout)
    async with TestClient(app) as client:
        before = await client.get("/me")
        await client.post("/login")
        during = await client.get("/me")
        await client.post("/logout")
        after = await client.get("/me")
    assert (before.text, during.text, after.text) == ("nobody", "ada", "nobody")
