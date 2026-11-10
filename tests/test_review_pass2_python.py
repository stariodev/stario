"""Regression tests from the second review pass over the pure-Python layer.

Each test failed before its fix.
"""

from __future__ import annotations

import asyncio
import tracemalloc
from urllib.parse import quote

import pytest

from stario.exceptions import RequestBodyError
from stario.forms import read_form
from stario.relay import Relay
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
        f"Content-Disposition: form-data; name=\"f\"; filename*={filename_star}\r\n"
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
