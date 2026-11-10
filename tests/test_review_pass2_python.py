"""Regression tests from the second review pass over the pure-Python layer.

Each test failed before its fix.
"""

from __future__ import annotations

import asyncio
import http.cookies
import tracemalloc
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stario import App, Assets
from stario.cookies import set_cookie
from stario.exceptions import RequestBodyError, StarioError
from stario.forms import read_form
from stario.http.config import RequestPolicy, ServerConfig, request_policy_from_env
from stario.http.headers import Headers
from stario.relay import Relay
from stario.testing import TestClient
from tests.helpers import make_request

# --- relay ---------------------------------------------------------------


def _deep_subject(dots: int) -> str:
    # What `relay.publish(f"room.{room_id}", ...)` gets for a path parameter
    # like `/room/a.a.a.a...`: a segment can hold dots, but not `/`.
    return "room." + "a." * dots + "z"


def test_publish_of_a_subject_with_many_dots_stays_linear() -> None:
    relay: Relay[int] = Relay()
    subject = _deep_subject(20_000)
    tracemalloc.start()
    try:
        relay.publish(subject, 1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Every prefix of a 40 KB subject is 400 MB (and was kept in a cache).
    assert peak < 2_000_000


async def test_wildcards_still_match_deep_and_shallow_subjects() -> None:
    relay: Relay[int] = Relay()
    subject = _deep_subject(2_000)
    async with (
        relay.subscribe("room.*") as room,
        relay.subscribe("room.a.a.*") as nested,
        relay.subscribe("*") as everything,
        relay.subscribe(subject) as exact,
        relay.subscribe("other.*") as other,
    ):
        relay.publish(subject, 1)
        relay.publish("room.b", 2)
        relay.publish("room.a.a.x", 3)
        assert await room.receive() == (subject, 1)
        assert await room.receive() == ("room.b", 2)
        assert await nested.receive() == (subject, 1)
        assert await nested.receive() == ("room.a.a.x", 3)
        assert await exact.receive() == (subject, 1)
        assert [await everything.receive() for _ in range(3)] == [
            (subject, 1),
            ("room.b", 2),
            ("room.a.a.x", 3),
        ]
        await asyncio.sleep(0)
        assert other.dropped == 0
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(other.receive(), 0.01)
    # Every subscription left: no prefix stays registered.
    assert relay.subs == {}
    assert relay._prefixes == ()


async def test_a_wildcard_registered_after_a_publish_is_matched() -> None:
    relay: Relay[int] = Relay()
    relay.publish("room.1.msg", 0)  # caches the lookup keys of this subject
    async with relay.subscribe("room.1.*") as sub:
        relay.publish("room.1.msg", 1)
        assert await sub.receive() == ("room.1.msg", 1)
    relay.publish("room.1.msg", 2)


# --- multipart filename* charset -------------------------------------------

BOUNDARY = "b0und"
MULTIPART = f"multipart/form-data; boundary={BOUNDARY}"


def _file_star_body(filename_star: str, parts: int = 1) -> bytes:
    one = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="f"; filename*={filename_star}\r\n'
        "\r\nx\r\n"
    )
    return (one * parts + f"--{BOUNDARY}--\r\n").encode()


async def test_filename_star_with_an_unsupported_codec_is_not_a_crash() -> None:
    # `idna` cannot decode with errors="replace": the UnicodeError escaped
    # read_form as an unhandled exception (a 500).
    req = make_request(
        method="POST",
        headers={"content-type": MULTIPART},
        body=_file_star_body("idna''%61%62"),
    )
    with await read_form(req) as form:
        upload = form.file("f")
        assert upload is not None
        assert upload.filename == "ab"


async def test_filename_star_cannot_pick_a_quadratic_codec() -> None:
    # punycode decodes in quadratic time: a 15 KB part header cost ~70 ms of
    # event loop, for each part of one request.
    import random

    rng = random.Random(1)
    text = "".join(chr(0x4E00 + rng.randrange(20_000)) for _ in range(5_000))
    encoded = quote(text.encode("punycode").decode("ascii"), safe="")
    req = make_request(
        method="POST",
        headers={"content-type": MULTIPART},
        body=_file_star_body(f"punycode''%61{encoded}"),
    )
    with await read_form(req) as form:
        upload = form.file("f")
        assert upload is not None
        # Decoded as UTF-8: the percent-escaped ASCII comes back as it is.
        assert upload.filename.startswith("a")
        assert not any("一" <= ch <= "鿿" for ch in upload.filename)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("UTF-8''%C3%A9t%C3%A9.txt", "été.txt"),
        ("utf-8''plain.txt", "plain.txt"),
        ("ISO-8859-1''%E9t%E9.txt", "été.txt"),
        ("''%C3%A9.txt", "é.txt"),
        ("koi8-r''%C3%A9.txt", "é.txt"),
    ],
)
async def test_filename_star_charsets(value: str, expected: str) -> None:
    req = make_request(
        method="POST",
        headers={"content-type": MULTIPART},
        body=_file_star_body(value),
    )
    with await read_form(req) as form:
        upload = form.file("f")
        assert upload is not None
        assert upload.filename == expected


async def test_filename_star_still_counts_against_max_files() -> None:
    req = make_request(
        method="POST",
        headers={"content-type": MULTIPART},
        body=_file_star_body("utf-8''a.txt", parts=3),
    )
    with pytest.raises(RequestBodyError) as info:
        await read_form(req, max_files=2)
    assert info.value.status_code == 413


# --- Assets fast path ------------------------------------------------------


async def test_held_asset_fast_path_answers_like_the_generic_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.css").write_text(".row { color: #123; }\n" * 200)
    (tmp_path / "logo.png").write_bytes(b"\x89PNG" + b"\0" * 600)
    assets = Assets(tmp_path)
    app = App()
    await assets.attach(app, cache_control="public, max-age=60")
    assert assets._ready  # the fast path is armed
    css, png = assets.href("app.css"), assets.href("logo.png")
    etag_css = f'"{assets._catalog["app.css"].digest}-br"'
    requests = [
        (url, headers)
        for url in (css, png)
        for headers in (
            {"accept-encoding": "identity"},
            {"accept-encoding": "gzip, deflate, br, zstd"},
            {"accept-encoding": "gzip;q=1, br;q=0.5"},
            {"accept-encoding": "br;q=0, *"},
            {"accept-encoding": "br", "if-none-match": etag_css},
            {"accept-encoding": "zstd", "if-none-match": etag_css},
            {
                "accept-encoding": "identity",
                "if-modified-since": "Fri, 01 Jan 2100 00:00:00 GMT",
            },
            {"accept-encoding": "br", "if-range": '"x"'},
            {"accept-encoding": "br", "range": "bytes=0-9"},
            {"accept-encoding": "identity", "range": "bytes=9999999-"},
        )
    ]

    def snapshot(response: Any) -> tuple[Any, ...]:
        return (response.status_code, list(response.headers.items()), response.content)

    async with TestClient(app) as client:
        fast = [snapshot(await client.get(u, headers=h)) for u, h in requests]
        fast.append(snapshot(await client.head(css, headers={"accept-encoding": "br"})))
        ready, assets._ready = assets._ready, {}
        generic = [snapshot(await client.get(u, headers=h)) for u, h in requests]
        generic.append(
            snapshot(await client.head(css, headers={"accept-encoding": "br"}))
        )
        assets._ready = ready
    assert [f[0] for f in fast].count(304) == 3
    for one, other in zip(fast, generic, strict=True):
        assert one[0] == other[0]
        assert one[2] == other[2]
        assert [kv for kv in one[1] if kv[0] != "date"] == [
            kv for kv in other[1] if kv[0] != "date"
        ]


async def test_assets_registered_after_load_still_serve_from_memory(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.js").write_text("x" * 1000)
    assets = Assets(tmp_path)
    await assets.load()
    assert assets._ready == {}  # no cache-control yet
    app = App()
    assets.register(app)
    assert set(assets._ready) == {assets._catalog["a.js"].hashed_path}
    async with TestClient(app) as client:
        response = await client.get(assets.href("a.js"))
    assert response.status_code == 200
    assert response.headers.get("cache-control") == (
        "public, max-age=31536000, immutable"
    )


# --- set_cookie without SimpleCookie --------------------------------------


def _simplecookie_line(
    name: str,
    value: str,
    *,
    max_age: int | None,
    expires: str | None,
    path: str,
    domain: str | None,
    secure: bool,
    httponly: bool,
    samesite: str | None,
) -> str | None:
    """What set_cookie sent through `http.cookies` (None: it raised)."""
    cookie: http.cookies.BaseCookie[str] = http.cookies.SimpleCookie()
    try:
        cookie[name] = value
        if max_age is not None:
            cookie[name]["max-age"] = str(max_age)
        if expires is not None:
            cookie[name]["expires"] = expires
        if path:
            cookie[name]["path"] = path
        if domain:
            cookie[name]["domain"] = domain
        if httponly:
            cookie[name]["httponly"] = True
        if samesite:
            cookie[name]["samesite"] = samesite
        if secure or (samesite is not None and samesite.lower() == "none"):
            cookie[name]["secure"] = True
        return cookie.output(header="").strip()
    except (http.cookies.CookieError, ValueError):
        return None


class _HeadersOnly:
    def __init__(self) -> None:
        self.headers = Headers()


_text = st.text(
    alphabet=st.sampled_from(
        list("aZ09-_.!~:;, =\"\\'()/<>@[]{}?\t\r\n\x00\x7f\x1f\u00e9\u00a0\u2028 ")
    ),
    max_size=12,
)


@settings(max_examples=400, deadline=None)
@given(
    name=st.one_of(_text, st.sampled_from(["sid", "Path", "max-age", "a:b"])),
    value=_text,
    max_age=st.one_of(st.none(), st.integers(-5, 10**6)),
    expires=st.one_of(st.none(), _text),
    path=_text,
    domain=st.one_of(st.none(), _text),
    secure=st.booleans(),
    httponly=st.booleans(),
    samesite=st.sampled_from([None, "lax", "Strict", "none"]),
)
def test_set_cookie_writes_what_simplecookie_wrote(
    name: str,
    value: str,
    max_age: int | None,
    expires: str | None,
    path: str,
    domain: str | None,
    secure: bool,
    httponly: bool,
    samesite: Any,
) -> None:
    kwargs: dict[str, Any] = dict(
        max_age=max_age,
        expires=expires,
        path=path,
        domain=domain,
        secure=secure,
        httponly=httponly,
        samesite=samesite,
    )
    w = _HeadersOnly()
    try:
        set_cookie(cast(Any, w), name, value, **kwargs)
    except StarioError:
        got = None
    else:
        got = w.headers.get("set-cookie")
    expected = _simplecookie_line(name, value, **kwargs)
    if expected is not None:
        # set_cookie refuses some lines before http.cookies sees them
        # (prefix rules, `;` or `,` in path/domain/expires) or after
        # (header encoding): only compare lines both would send.
        try:
            Headers().add("set-cookie", expected)
        except ValueError:
            expected = None
    if got is not None:
        assert got == expected
    elif expected is not None:
        assert (
            name.lower().startswith(("__host-", "__secure-"))
            or any(c in (path or "") for c in ";,")
            or any(c in (domain or "") for c in ";,")
            or ";" in (expires or "")
        )


def test_set_cookie_output_examples() -> None:
    w = _HeadersOnly()
    set_cookie(cast(Any, w), "session", "abc", max_age=3600, secure=True)
    set_cookie(cast(Any, w), "e", "", domain="example.com", samesite=None)
    set_cookie(cast(Any, w), "q", 'a b"c\\', expires="Thu, 01 Jan 2099 00:00:00 GMT")
    assert w.headers.getlist("set-cookie") == [
        "session=abc; HttpOnly; Max-Age=3600; Path=/; SameSite=lax; Secure",
        'e=""; Domain=example.com; HttpOnly; Path=/',
        'q="a b\\"c\\\\"; expires=Thu, 01 Jan 2099 00:00:00 GMT; HttpOnly; '
        "Path=/; SameSite=lax",
    ]


# --- TTY tracer ------------------------------------------------------------


def test_tty_tracer_escapes_bidi_controls_from_request_data() -> None:
    from tests.test_telemetry_tty import make_span, render

    # GET /%E2%80%AEtxt.exe puts U+202E (right-to-left override) in
    # request.path; the terminal would show the rest of the line reversed.
    rlo, isolate = "‮", "⁦"
    root = make_span(
        f"GET /{rlo}gpj.exe",
        attributes={"request.path": f"/{rlo}gpj.exe", "q": f"a{isolate}b"},
    )
    text = render(root, width=120)
    assert rlo not in text
    assert isolate not in text
    assert "\\u202e" in text
    assert "\\u2066" in text


# --- config ----------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["header_timeout", "body_timeout", "keep_alive_timeout", "write_timeout"],
)
def test_request_policy_rejects_nan_timeouts(field: str) -> None:
    # Every deadline comparison with NaN is false: the timeout never fires.
    with pytest.raises(StarioError, match=field):
        RequestPolicy(**cast(dict[str, Any], {field: float("nan")}))


def test_nan_timeout_from_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STARIO_REQUESTS_HEADER_TIMEOUT", "nan")
    with pytest.raises(StarioError, match="header_timeout"):
        request_policy_from_env()


def test_server_config_rejects_nan_shutdown_timeout() -> None:
    with pytest.raises(StarioError, match="graceful_shutdown_timeout"):
        ServerConfig(graceful_shutdown_timeout=float("nan"))
