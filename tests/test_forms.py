"""`stario.forms`: the multipart parser alone, then `read_form` end to end."""

from __future__ import annotations

import random
from typing import ClassVar

import pytest

from stario import App, Context, Route, Writer
from stario.exceptions import RequestBodyError
from stario.forms import Form, UploadFile, read_form
from stario.forms._multipart import (
    MultipartError,
    MultipartLimitError,
    MultipartParser,
    PartEnd,
    PartStart,
    parse_options,
)
from stario.http.config import RequestPolicy
from stario.testing import TestClient
from tests.helpers import make_request

BOUNDARY = "xYzZy"
MULTIPART = f"multipart/form-data; boundary={BOUNDARY}"


def multipart_body(*parts: tuple[str, bytes], preamble: bytes = b"") -> bytes:
    """Join (headers, content) parts with BOUNDARY and a close delimiter."""
    out = [preamble]
    for headers, content in parts:
        out += [f"--{BOUNDARY}\r\n{headers}\r\n\r\n".encode(), content, b"\r\n"]
    out.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(out)


def field(name: str) -> str:
    return f'Content-Disposition: form-data; name="{name}"'


def file_part(name: str, filename: str, ctype: str = "text/plain") -> str:
    return (
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}"
    )


def parse(body: bytes, chunks: list[int] | None = None) -> list[object]:
    """Feed `body` in the given chunk sizes and merge adjacent content events."""
    parser = MultipartParser(BOUNDARY.encode(), max_header_bytes=16 * 1024)
    events: list[object] = []
    sizes = chunks or [len(body)]
    pos = 0
    for size in sizes:
        events.extend(parser.feed(body[pos : pos + size]))
        pos += size
    if pos < len(body):
        events.extend(parser.feed(body[pos:]))
    parser.close()
    merged: list[object] = []
    for event in events:
        if isinstance(event, bytes) and merged and isinstance(merged[-1], bytes):
            merged[-1] = merged[-1] + event
        else:
            merged.append(event)
    return merged


class TestParseOptions:
    def test_type_and_params(self):
        assert parse_options('Multipart/Form-Data; Boundary="a;b"; x=1') == (
            "multipart/form-data",
            {"boundary": "a;b", "x": "1"},
        )

    def test_quoted_escapes_and_first_wins(self):
        kind, params = parse_options(
            r'form-data; name="say \"hi\""; filename="a.txt"; name=other'
        )
        assert kind == "form-data"
        assert params == {"name": 'say "hi"', "filename": "a.txt"}

    def test_part_headers_keep_backslashes(self):
        _, params = parse_options(
            r'form-data; name="f"; filename="C:\Users\me.png"',
            backslash_escapes=False,
        )
        assert params["filename"] == r"C:\Users\me.png"

    def test_empty_and_valueless(self):
        assert parse_options("text/plain;;flag;  ") == ("text/plain", {})


class TestMultipartParser:
    BODY = multipart_body(
        (field("title"), b"Lost cat"),
        (file_part("photo", "cat.jpg", "image/jpeg"), b"\xff\xd8 binary \r\n--x"),
        (field("empty"), b""),
        preamble=b"ignored preamble\r\n",
    )
    EXPECTED: ClassVar[list[object]] = [
        PartStart("title", None, None),
        b"Lost cat",
        PartEnd(),
        PartStart("photo", "cat.jpg", "image/jpeg"),
        b"\xff\xd8 binary \r\n--x",
        PartEnd(),
        PartStart("empty", None, None),
        PartEnd(),
    ]

    def test_whole_body(self):
        assert parse(self.BODY) == self.EXPECTED

    def test_one_byte_at_a_time(self):
        assert parse(self.BODY, [1] * len(self.BODY)) == self.EXPECTED

    @pytest.mark.parametrize("seed", range(20))
    def test_random_chunks(self, seed: int):
        rng = random.Random(seed)
        sizes = [rng.randint(1, 40) for _ in range(len(self.BODY))]
        assert parse(self.BODY, sizes) == self.EXPECTED

    def test_epilogue_and_padding(self):
        body = (
            f"--{BOUNDARY} \t\r\n{field('a')}\r\n\r\n1\r\n--{BOUNDARY}--\r\nepilogue"
        ).encode()
        assert parse(body) == [PartStart("a", None, None), b"1", PartEnd()]

    @pytest.mark.parametrize(
        "body",
        [
            multipart_body((field("a"), b"1"))[:-12],  # no close delimiter
            f"--{BOUNDARY}\r\nContent-Type: text/plain\r\n\r\nx\r\n--{BOUNDARY}--".encode(),
            f"--{BOUNDARY}\r\n\r\nx\r\n--{BOUNDARY}--".encode(),
            f'--{BOUNDARY}\r\nContent-Disposition: attachment; name="a"\r\n\r\n'
            f"x\r\n--{BOUNDARY}--".encode(),
            f"--{BOUNDARY}\r\nno colon\r\n\r\nx\r\n--{BOUNDARY}--".encode(),
            f"--{BOUNDARY}junk\r\n{field('a')}\r\n\r\nx\r\n--{BOUNDARY}--".encode(),
        ],
    )
    def test_malformed(self, body: bytes):
        with pytest.raises(MultipartError):
            parse(body)

    def test_header_limit(self):
        parser = MultipartParser(BOUNDARY.encode(), max_header_bytes=64)
        body = f"--{BOUNDARY}\r\n{field('a' * 100)}\r\n\r\n".encode()
        with pytest.raises(MultipartLimitError):
            list(parser.feed(body))


async def echo(c: Context, w: Writer) -> None:
    limit = c.req.query.get("max_size")
    with await read_form(
        c.req,
        max_size=int(limit) if limit else None,
        max_fields=3,
        max_field_size=100,
        max_files=2,
        max_file_size=1000,
        spool_size=10,
    ) as form:
        lines = [f"{k}={v}" for k, v in form.items()]
        lines += [
            f"{k}:{f.filename}:{f.content_type}:{f.size}:{await f.read()!r}"
            for k, f in form.file_items()
        ]
    w.text("\n".join(lines))


def form_app() -> App:
    app = App()
    app.add(Route("GET /form"), echo)
    app.add(Route("POST /form"), echo)
    return app


class TestReadForm:
    async def test_urlencoded(self):
        async with TestClient(form_app()) as client:
            r = await client.post(
                "/form", data=[("a", "1"), ("b", "hello world"), ("a", "✓")]
            )
        assert r.status_code == 200
        assert r.text == "a=1\nb=hello world\na=✓"

    async def test_get_form_reads_the_query(self):
        async with TestClient(form_app()) as client:
            r = await client.get("/form?q=cat&page=2")
        assert r.text == "q=cat\npage=2"

    async def test_multipart_fields_and_files(self):
        async with TestClient(form_app()) as client:
            r = await client.post(
                "/form",
                data={"title": "Lost cat"},
                files=[
                    ("photo", ("cat.jpg", b"x" * 500, "image/jpeg")),
                    ("photo", ("C:\\Users\\me\\dog.png", b"png", "image/png")),
                ],
            )
        assert r.status_code == 200
        assert r.text.splitlines() == [
            "title=Lost cat",
            f"photo:cat.jpg:image/jpeg:500:{b'x' * 500!r}",
            "photo:dog.png:image/png:3:b'png'",
        ]

    async def test_empty_file_input_adds_no_file(self):
        body = multipart_body(
            (field("title"), b"t"),
            (file_part("photo", "", "application/octet-stream"), b""),
        )
        async with TestClient(form_app()) as client:
            r = await client.post(
                "/form", content=body, headers={"content-type": MULTIPART}
            )
        assert r.text == "title=t"

    @pytest.mark.parametrize(
        ("kwargs", "detail"),
        [
            ({"data": {f"f{i}": "v" for i in range(4)}}, "Too many form fields"),
            (
                {"data": {"a": "x" * 101}, "files": {"f": ("f.txt", b"1")}},
                "Form field too large",
            ),
            (
                {"files": [("f", (f"{i}.txt", b"1")) for i in range(3)]},
                "Too many files",
            ),
            ({"files": {"f": ("f.txt", b"x" * 1001)}}, "File too large"),
        ],
    )
    async def test_limits_are_413(self, kwargs: dict, detail: str):
        async with TestClient(form_app()) as client:
            r = await client.post("/form", **kwargs)
        assert (r.status_code, r.text) == (413, detail)

    async def test_max_size_is_the_body_limit(self):
        files = {"f": ("f.txt", b"x" * 900)}
        policy = RequestPolicy(max_body_bytes=500)
        async with TestClient(form_app(), requests=policy) as client:
            default = await client.post("/form", files=files)
            raised = await client.post("/form?max_size=5000", files=files)
        assert default.status_code == 413
        assert raised.status_code == 200

    @pytest.mark.parametrize(
        "content_type",
        [None, "application/json", "application/x-www-form-urlencoded; charset=latin1"],
    )
    async def test_other_content_types_are_415(self, content_type):
        headers = {"content-type": content_type} if content_type else {}
        async with TestClient(form_app()) as client:
            r = await client.post("/form", content=b"a=1", headers=headers)
        assert r.status_code == 415

    @pytest.mark.parametrize(
        ("content_type", "body"),
        [
            ("multipart/form-data", b"--x--"),
            ("multipart/form-data; boundary=" + "b" * 71, b""),
            (MULTIPART, b"--xYzZy\r\n\r\nno headers\r\n--xYzZy--"),
            (MULTIPART, multipart_body((field("a"), b"1"))[:-12]),
        ],
    )
    async def test_malformed_is_400(self, content_type: str, body: bytes):
        async with TestClient(form_app()) as client:
            r = await client.post(
                "/form", content=body, headers={"content-type": content_type}
            )
        assert r.status_code == 400

    async def test_part_header_limit_is_413(self):
        req = make_request(
            method="POST",
            headers={"content-type": MULTIPART},
            body=f"--{BOUNDARY}\r\n{field('a' * 200)}\r\n\r\n".encode(),
        )
        with pytest.raises(RequestBodyError) as info:
            await read_form(req, max_part_header_bytes=64)
        assert info.value.status_code == 413


class TestFormObject:
    async def test_large_file_spools_to_disk(self):
        req = make_request(
            method="POST",
            headers={"content-type": MULTIPART},
            body=multipart_body((file_part("f", "big.bin"), b"y" * 5000)),
        )
        form = await read_form(req, spool_size=1024)
        upload = form.file("f")
        assert isinstance(upload, UploadFile)
        assert upload.file._rolled  # pyrefly: ignore[missing-attribute]
        assert await upload.read() == b"y" * 5000
        form.close()
        assert upload.file.closed

    def test_access(self):
        a = UploadFile.__new__(UploadFile)
        form = Form([("a", "1"), ("b", "2"), ("a", "3")], [("f", a)])
        assert form.get("a") == "1"
        assert form.get("x", "d") == "d"
        assert form.getlist("a") == ["1", "3"]
        assert form.as_dict() == {"a": "1", "b": "2"}
        assert form.as_dict(last=True) == {"a": "3", "b": "2"}
        assert form.file("f") is a
        assert form.file("a") is None
        assert form.filelist("f") == [a]
        assert "a" in form
        assert "f" in form
        assert "x" not in form


class TestReadFormEdges:
    async def test_filename_star_is_a_file(self):
        req = make_request(
            method="POST",
            headers={"content-type": MULTIPART},
            body=multipart_body(
                (
                    'Content-Disposition: form-data; name="f"; '
                    "filename*=UTF-8''kot%20%C5%BC%C3%B3%C5%82ty.jpg",
                    b"jpeg",
                )
            ),
        )
        with await read_form(req) as form:
            upload = form.file("f")
            assert upload is not None
            assert upload.filename == "kot żółty.jpg"
            assert await upload.read() == b"jpeg"
            assert form.items() == []

    @pytest.mark.parametrize("boundary", ["abc ", "a" * 71, "a;b", "a{b}"])
    async def test_invalid_boundary_is_400(self, boundary: str):
        req = make_request(
            method="POST",
            headers={"content-type": f'multipart/form-data; boundary="{boundary}"'},
            body=b"--x--",
        )
        with pytest.raises(RequestBodyError) as info:
            await read_form(req)
        assert info.value.status_code == 400

    async def test_get_query_has_no_field_limit(self):
        req = make_request(query={f"f{i}": "v" for i in range(20)})
        form = await read_form(req, max_fields=3)
        assert len(form.items()) == 20

    async def test_large_upload_streams_in_chunks(self):
        """Past SMALL_BODY the body streams after dispatch, chunk by chunk."""
        payload = bytes(range(256)) * 2000  # 512 KB, every byte value
        async with TestClient(form_app()) as client:
            r = await client.post(
                "/form?max_size=1000000",
                files={"f": ("big.bin", payload)},
            )
        assert r.status_code == 413  # max_file_size=1000 in the echo handler

        async def keep(c: Context, w: Writer) -> None:
            with await read_form(c.req, spool_size=4096) as form:
                upload = form.file("f")
                assert upload is not None
                data = await upload.read()
                w.text(f"{upload.size} {data == payload}")

        app = App()
        app.add(Route("POST /keep"), keep)
        async with TestClient(app) as client:
            r = await client.post("/keep", files={"f": ("big.bin", payload)})
        assert (r.status_code, r.text) == (200, f"{len(payload)} True")

    async def test_quote_cr_lf_escapes_round_trip(self):
        """Browsers write `"`, CR, LF as %22, %0D, %0A; the reader decodes them."""

        async def names(c: Context, w: Writer) -> None:
            with await read_form(c.req) as form:
                (field_name, value), *_ = form.items()
                upload = form.file('photo "1"')
                assert upload is not None
                w.text(f"{field_name}|{value}|{upload.filename}")

        app = App()
        app.add(Route("POST /names"), names)
        async with TestClient(app) as client:
            r = await client.post(
                "/names",
                data={'say "hi"\n': "v"},
                files={'photo "1"': ('kot "a".jpg', b"x")},
            )
        assert (r.status_code, r.text) == (200, 'say "hi"\n|v|kot "a".jpg')
