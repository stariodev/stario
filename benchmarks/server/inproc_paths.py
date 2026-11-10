"""In-process benchmark of the request paths `inproc.py` does not take.

`inproc.py` measures the common fast paths. This file measures the rest of
what an application does: reading the query, cookies and headers, setting
response headers and cookies, a real route table, misses and redirects,
chunked and streamed uploads, forms, pipelining, a connection per request,
streamed responses, static assets, escaping-heavy and `@baked` pages, and
HTTP/2 uploads and multiplexing.

It exists to check a build against paths a profile was not trained on (see
`STARIO_PGO` in CYTHON.md), and to catch regressions outside the hot path.

    uv run benchmarks/server/inproc_paths.py             # all cases
    uv run benchmarks/server/inproc_paths.py cookies     # prefix match

Numbers are µs per request, best of N, for comparing two builds on the same
machine.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import stario  # noqa: F401  (package init before the extensions)
from stario import App, Assets, Route, classes, cookies, h
from stario.forms import read_form
from stario.markup import baked

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inproc

N = 10000
REPEAT = 5

COOKIE_HEADERS = (
    b"host: localhost\r\n"
    b"user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0\r\n"
    b"accept: text/html,application/xhtml+xml,*/*;q=0.8\r\n"
    b"accept-language: en-US,en;q=0.9,pl;q=0.8\r\n"
    b"accept-encoding: identity\r\n"
    b"cookie: _ga=GA1.2.123456789.1700000000; theme=dark; "
    b'session=abcdef0123456789abcdef0123456789; csrf="tok en"; lang=en\r\n'
)


def get(path: str, extra: bytes = COOKIE_HEADERS, method: str = "GET") -> bytes:
    return f"{method} {path} HTTP/1.1\r\n".encode() + extra + b"\r\n"


def post(path: str, body: bytes, content_type: str) -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\nhost: localhost\r\n"
        f"content-type: {content_type}\r\ncontent-length: {len(body)}\r\n\r\n"
    ).encode() + body


# --- handlers ---------------------------------------------------------------


async def search(c, w):
    q = c.req.query
    w.json(
        {
            "q": q.get("q"),
            "page": q.get("page", "1"),
            "tags": q.getlist("tag"),
            "sort": q.get("sort"),
            "missing": q.get("nope"),
        }
    )


async def whoami(c, w):
    jar = c.req.cookies
    w.text(f"{jar.get('session')}:{jar.get('theme')}:{jar.get('csrf')}")


async def echo_headers(c, w):
    hdrs = c.req.headers
    w.text(
        f"{hdrs.get('user-agent')}|{hdrs.get('accept-language')}|{hdrs.get('x-none')}"
    )


async def login(c, w):
    w.headers.set("cache-control", "no-store")
    w.headers.set("x-frame-options", "DENY")
    w.headers.set("x-request-id", "0123456789abcdef")
    cookies.set_cookie(w, "session", "abcdef0123456789", max_age=3600, secure=True)
    w.text("ok")


async def item(c, w):
    p = c.match.params
    w.json({"org": p["org"], "project": p["project"], "item": p["item"]})


async def ok(c, w):
    w.text("ok")


async def read_json(c, w):
    w.json({"n": len(json.loads(await c.req.body()))})


async def body_len(c, w):
    w.text(str(len(await c.req.body())))


async def stream_len(c, w):
    total = 0
    async for chunk in c.req.stream():
        total += len(chunk)
    w.text(str(total))


async def form(c, w):
    with await read_form(c.req) as data:
        upload = data.file("avatar")
        w.text(f"{data.get('name')}:{len(data.getlist('tag'))}:{upload is not None}")


CHUNK = b"<li>row of streamed output, sixty-odd bytes of text per write</li>\n"


async def streamed(c, w):
    w.headers.set("content-type", "text/html; charset=utf-8")
    w.write_headers(200)
    for _ in range(6):
        w.write(CHUNK)
    w.end()


RICH_ROWS = [
    (i, f'Tom & "Jerry" <{i}>', f"it's a <b>bold</b> claim & more #{i}", i % 3 == 0)
    for i in range(50)
]


async def rich_page(c, w):
    w.html(
        h.Html(
            h.Head(h.Title("Rich")),
            h.Body(
                {"class": "page", "data-view": "list"},
                h.Ul(
                    classes("list", "striped"),
                    *[
                        h.Li(
                            {"id": f"row-{i}", "title": name, "data-id": i},
                            classes("row", "selected" if sel else None),
                            h.A({"href": f"/items/{i}?ref=a&b"}, name),
                            h.Span({"class": "desc"}, desc),
                        )
                        for i, name, desc, sel in RICH_ROWS
                    ],
                ),
            ),
        )
    )


@baked
def _baked_row(ident, name, desc):
    return h.Li(
        {"class": "row", "id": ident},
        h.A({"href": "#", "class": "name"}, name),
        h.Span({"class": "desc"}, desc),
    )


@baked
def _baked_layout(title, rows):
    return h.Html(
        h.Head(h.Title(title)),
        h.Body({"class": "page"}, h.H1(title), h.Ul({"class": "list"}, rows)),
    )


async def baked_page(c, w):
    w.html(
        _baked_layout(
            "Baked",
            [_baked_row(f"row-{i}", name, desc) for i, name, desc, _ in RICH_ROWS],
        )
    )


async def build_app(static_dir: Path) -> tuple[App, str]:
    app = App()
    app.add(Route("GET /search"), search)
    app.add(Route("GET /whoami"), whoami)
    app.add(Route("GET /headers"), echo_headers)
    app.add(Route("GET /login"), login)
    app.add(Route("GET /ok"), ok)
    app.add(Route("POST /json"), read_json)
    app.add(Route("POST /upload"), body_len)
    app.add(Route("POST /stream"), stream_len)
    app.add(Route("POST /form"), form)
    app.add(Route("GET /streamed"), streamed)
    app.add(Route("GET /rich"), rich_page)
    app.add(Route("GET /baked"), baked_page)
    # A route table of realistic size around the one being looked up.
    for group in ("users", "teams", "billing", "reports", "settings", "audit"):
        for leaf in ("list", "new", "export", "import", "stats"):
            app.add(Route(f"GET /api/v1/{group}/{leaf}"), ok)
        app.add(Route(f"GET /api/v1/{group}/{{id}}"), ok)
        app.add(Route(f"POST /api/v1/{group}/{{id}}/archive"), ok)
    app.add(Route("GET /api/v1/orgs/{org}/projects/{project}/items/{item}"), item)
    (static_dir / "app.css").write_text(".row { color: #123; }\n" * 200)
    # Loaded into memory with precompressed variants, as `attach()` does in
    # a bootstrap; an unloaded Assets would stream every file from disk.
    assets = Assets(static_dir, "/static")
    await assets.load()
    assets.register(app)
    app.compiled()
    app.freeze()
    return app, assets.href("app.css")


# --- measurement ------------------------------------------------------------


async def settle(transport, app, before: int) -> None:
    """Wait until the request fed after `before` writes is answered and no
    handler is still running (a response can be several writes, with loop
    turns between them). A handler waiting on a worker thread needs the
    loop to poll, which a spin of zero-length sleeps never lets uvloop do,
    so a long wait starts sleeping for real."""
    spins = 0
    while transport.nwrites == before or app.tasks:
        spins += 1
        await asyncio.sleep(0 if spins < 500 else 0.0005)


async def bench(app, raw: bytes, per_feed: int = 1) -> float:
    """Feed `raw` on one keep-alive connection; wait for its response(s)."""
    loop = asyncio.get_running_loop()
    proto, transport = inproc.make_protocol(loop, app)
    feed = proto.data_received
    sleep = asyncio.sleep

    async def one() -> None:
        before = transport.nwrites
        feed(raw)
        await settle(transport, app, before)

    for _ in range(100):
        await one()
    await sleep(0)
    best = float("inf")
    n = max(N // per_feed, 200)
    for _ in range(REPEAT):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            await one()
        best = min(best, (time.perf_counter_ns() - t0) / (n * per_feed))
    assert not transport.closing, "connection closed during the benchmark"
    return best


async def bench_pieces(app, pieces: list[bytes]) -> float:
    """One request fed in several reads, with a loop turn between them."""
    loop = asyncio.get_running_loop()
    proto, transport = inproc.make_protocol(loop, app)
    feed = proto.data_received
    sleep = asyncio.sleep

    async def one() -> None:
        before = transport.nwrites
        for piece in pieces:
            feed(piece)
            await sleep(0)
        await settle(transport, app, before)

    for _ in range(20):
        await one()
    best = float("inf")
    n = 300
    for _ in range(REPEAT):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            await one()
        best = min(best, (time.perf_counter_ns() - t0) / n)
    assert not transport.closing, "connection closed during the benchmark"
    return best


async def bench_connection(app, raw: bytes) -> float:
    """A new connection per request: connect, one request, close."""
    loop = asyncio.get_running_loop()

    def one() -> None:
        proto, transport = inproc.make_protocol(loop, app)
        proto.data_received(raw)
        if not transport.closing:
            transport.closing = True
        proto.connection_lost(None)

    for _ in range(100):
        one()
    await asyncio.sleep(0)
    best = float("inf")
    n = N // 2
    for _ in range(REPEAT):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            one()
        await asyncio.sleep(0)
        best = min(best, (time.perf_counter_ns() - t0) / n)
    return best


def _h2_post_block(path: str, length: int) -> bytes:
    return (
        bytes((0x83, 0x86))  # :method POST, :scheme http
        + inproc._literal(4, path.encode())
        + inproc._literal(1, b"localhost")
        + inproc._literal(31, b"application/json")  # content-type
        + inproc._literal(28, str(length).encode())  # content-length
    )


async def bench_h2(app, frames_for, per_feed: int = 1) -> float:
    """`frames_for(stream_id)` returns one feed and how many ids it used."""
    loop = asyncio.get_running_loop()
    proto, transport = inproc.make_protocol(loop, app)
    feed = proto.data_received
    frame = inproc._frame
    feed(
        inproc.H2_PREFACE
        + frame(0x4, 0, 0, (4).to_bytes(2, "big") + (0x7FFFFFFF).to_bytes(4, "big"))
        + frame(0x8, 0, 0, (64 * 1024 * 1024 - 65535).to_bytes(4, "big"))
    )
    feed(frame(0x4, 0x1, 0))
    stream_id = 1
    credited = transport.nbytes
    count = 0

    def one() -> None:
        nonlocal stream_id, credited, count
        feed(frames_for(stream_id))
        stream_id += 2 * per_feed
        count += 1
        if count % 200 == 0:
            used = transport.nbytes - credited
            credited = transport.nbytes
            feed(frame(0x8, 0, 0, used.to_bytes(4, "big")))

    for _ in range(100):
        one()
    await asyncio.sleep(0)
    best = float("inf")
    n = N // per_feed
    for _ in range(REPEAT):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            one()
        await asyncio.sleep(0)
        best = min(best, (time.perf_counter_ns() - t0) / (n * per_feed))
    assert not transport.closing, "connection closed during the benchmark"
    return best


JSON_BODY = json.dumps(
    {"items": [{"id": i, "name": f"n{i}"} for i in range(20)]}
).encode()
BOUNDARY = "----stariobench7MA4YWxkTrZu0gW"


def _multipart() -> bytes:
    def part(headers: str, content: bytes) -> bytes:
        return f"--{BOUNDARY}\r\n{headers}\r\n\r\n".encode() + content + b"\r\n"

    return (
        part('Content-Disposition: form-data; name="name"', b"Ada Lovelace")
        + part('Content-Disposition: form-data; name="tag"', b"math")
        + part('Content-Disposition: form-data; name="tag"', b"engines")
        + part(
            'Content-Disposition: form-data; name="avatar"; filename="a.png"\r\n'
            "Content-Type: image/png",
            b"\x89PNG" + b"\x00" * 2000,
        )
        + f"--{BOUNDARY}--\r\n".encode()
    )


def _chunked(body: bytes, size: int) -> bytes:
    out = bytearray(
        b"POST /json HTTP/1.1\r\nhost: localhost\r\n"
        b"content-type: application/json\r\ntransfer-encoding: chunked\r\n\r\n"
    )
    for start in range(0, len(body), size):
        piece = body[start : start + size]
        out += f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
    out += b"0\r\n\r\n"
    return bytes(out)


async def main(only: str | None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app, css_href = await build_app(Path(tmp))
        cases: list[tuple[str, object]] = []

        def add(name: str, run) -> None:
            if only is None or name.startswith(only):
                cases.append((name, run))

        query = "/search?q=hello+world&page=2&tag=a&tag=b%20c&sort=price%3Adesc&x=1"
        add("query_read", lambda: bench(app, get(query)))
        add("cookies_read", lambda: bench(app, get("/whoami")))
        add("headers_read", lambda: bench(app, get("/headers")))
        add("set_headers_cookie", lambda: bench(app, get("/login")))
        add(
            "route_table_3_params",
            lambda: bench(app, get("/api/v1/orgs/acme/projects/web%20app/items/42")),
        )
        add("not_found", lambda: bench(app, get("/api/v1/nothing/here")))
        add("method_not_allowed", lambda: bench(app, get("/ok", method="DELETE")))
        add("redirect_308", lambda: bench(app, get("/ok/")))
        add("head_request", lambda: bench(app, get("/ok", method="HEAD")))
        add("pipelined_x4", lambda: bench(app, get("/ok") * 4, per_feed=4))
        add(
            "connection_per_request",
            lambda: bench_connection(
                app, get("/ok", COOKIE_HEADERS + b"connection: close\r\n")
            ),
        )
        add(
            "bad_request_close",
            lambda: bench_connection(app, b"GET / HTTP/9.9\r\n\r\n"),
        )
        add(
            "post_json_1k",
            lambda: bench(app, post("/json", JSON_BODY, "application/json")),
        )
        add("post_chunked", lambda: bench(app, _chunked(JSON_BODY, 100)))
        add(
            "post_64k",
            lambda: bench(
                app, post("/upload", b"x" * 65536, "application/octet-stream")
            ),
        )
        big = post("/stream", b"y" * (1024 * 1024), "application/octet-stream")
        add(
            "upload_stream_1m",
            lambda: bench_pieces(
                app, [big[i : i + 65536] for i in range(0, len(big), 65536)]
            ),
        )
        add(
            "form_urlencoded",
            lambda: bench(
                app,
                post(
                    "/form",
                    b"name=Ada+Lovelace&tag=math&tag=engines&bio=%C5%81%C3%B3d%C5%BA&age=36",
                    "application/x-www-form-urlencoded",
                ),
            ),
        )
        add(
            "form_multipart",
            lambda: bench(
                app,
                post(
                    "/form", _multipart(), f"multipart/form-data; boundary={BOUNDARY}"
                ),
            ),
        )
        add("streamed_identity", lambda: bench(app, get("/streamed")))
        gz = COOKIE_HEADERS.replace(b"identity", b"gzip")
        zs = COOKIE_HEADERS.replace(b"identity", b"zstd")
        br = COOKIE_HEADERS.replace(b"identity", b"gzip, deflate, br, zstd")
        add("streamed_gzip", lambda: bench(app, get("/streamed", gz)))
        add("streamed_zstd", lambda: bench(app, get("/streamed", zs)))
        add("asset_identity", lambda: bench(app, get(css_href)))
        add("asset_negotiated", lambda: bench(app, get(css_href, br)))
        add("page_rich_escaped", lambda: bench(app, get("/rich")))
        add("page_baked", lambda: bench(app, get("/baked")))

        frame = inproc._frame
        post_block = _h2_post_block("/json", len(JSON_BODY))
        add(
            "h2_post_json_1k",
            lambda: bench_h2(
                app,
                lambda sid: (
                    frame(0x1, 0x4, sid, post_block) + frame(0x0, 0x1, sid, JSON_BODY)
                ),
            ),
        )
        ok_block = inproc.h2_request("/ok")
        add(
            "h2_multiplex_x10",
            lambda: bench_h2(
                app,
                lambda sid: b"".join(
                    frame(0x1, 0x5, sid + 2 * k, ok_block) for k in range(10)
                ),
                per_feed=10,
            ),
        )
        rich_block = inproc.h2_request("/rich", accept_encoding="br")
        add(
            "h2_page_rich_br",
            lambda: bench_h2(app, lambda sid: frame(0x1, 0x5, sid, rich_block)),
        )

        print(f"{'case':<28}{'µs/req':>10}")
        for name, run in cases:
            print(f"{name:<28}{await run() / 1000:>10.2f}")


if __name__ == "__main__":
    case = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        import uvloop
    except ImportError:
        asyncio.run(main(case))
        print("(asyncio)")
    else:
        uvloop.run(main(case))
        print("(uvloop)")
