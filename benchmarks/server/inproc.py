"""In-process protocol benchmark: no sockets, no load generator.

Feeds keep-alive HTTP/1.1 requests straight into ``HttpProtocol`` with a
transport that discards writes, and reports the best-of-N cost per request.
This isolates framework cost (parse, route, dispatch, handler, writer) from
syscalls and the client, so a change of a few hundred nanoseconds is visible.

    uv run benchmarks/server/inproc.py            # all cases
    uv run benchmarks/server/inproc.py plaintext  # one case (prefix match)

Cases:

- ``plaintext`` / ``json`` / ``user_42`` — the ``benchmarks/server`` route mix.
- ``browser_headers`` — plaintext with a real browser's 15 request headers.
- ``validate_post`` — small JSON POST read with ``c.req.body()``.
- ``await_once`` — the handler suspends once (non-eager Task path).
- ``page_*`` — a 50-row HTML table built per call with ``h.*`` and sent
  with ``w.html``; ``identity`` plus each content coding.
- ``traced_json`` — plaintext with ``JsonTracer`` writing to ``/dev/null``.
- ``sse_patch`` — one SSE stream; cost of ``patch_elements`` and
  ``patch_signals`` per event, identity and zstd.
- ``h2_*`` — the same handlers over cleartext HTTP/2 (one HEADERS frame per
  request, one stream per request); also prints transport writes per
  request, since a response is one HEADERS plus one or more DATA frames.

Numbers are for comparing two checkouts on the same machine, not for
comparing machines.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import stario  # noqa: F401  (package init before the extensions)
from stario import App, Route, h
from stario.datastar import SSE
from stario.http.compression import CompressionConfig
from stario.telemetry.json import JsonTracer
from stario.telemetry.noop import NoOpTracer
from stario_cython.protocol import Connections, HttpProtocol

DATE = b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"
N = 20000
REPEAT = 5


class NullTransport:
    """Discards writes; counts bytes and calls."""

    __slots__ = ("closing", "nbytes", "nwrites")

    def __init__(self) -> None:
        self.nbytes = 0
        self.nwrites = 0
        self.closing = False

    def write(self, data) -> None:
        self.nwrites += 1
        self.nbytes += len(data)

    def writelines(self, parts) -> None:
        self.nwrites += 1
        for part in parts:
            self.nbytes += len(part)

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closing = True

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def get_extra_info(self, name, default=None):
        return default

    def get_write_buffer_size(self) -> int:
        return 0


ROWS = [
    (i, f"Product {i}", f"Description of product {i} with some text", i * 3.5)
    for i in range(50)
]


def page():
    return h.Html(
        h.Head(h.Title("Products")),
        h.Body(
            h.H1("Products"),
            h.Table(
                h.Thead(
                    h.Tr(h.Th("Id"), h.Th("Name"), h.Th("Description"), h.Th("Price"))
                ),
                h.Tbody(
                    *[
                        h.Tr(h.Td(str(i)), h.Td(name), h.Td(desc), h.Td(f"{price:.2f}"))
                        for i, name, desc, price in ROWS
                    ]
                ),
            ),
        ),
    )


async def plaintext(c, w):
    w.text("Hello, World!")


async def json_endpoint(c, w):
    w.json({"message": "Hello, World!"})


async def get_user(c, w):
    uid = c.match.params["user_id"]
    w.json({"id": uid, "name": f"User {uid}"})


async def html_page(c, w):
    w.html(page())


async def validate(c, w):
    body = json.loads(await c.req.body())
    w.json({"name": body["name"], "age": body["age"], "valid": True})


async def awaits_once(c, w):
    await asyncio.sleep(0)
    w.text("Hello, World!")


BIG_BODY = b"x" * (64 * 1024)


async def big_body(c, w):
    # Four 16 KiB DATA frames on HTTP/2.
    w.respond(BIG_BODY, b"application/octet-stream", 200)


async def build_app() -> App:
    app = App()
    app.add(Route("GET /plaintext"), plaintext)
    app.add(Route("GET /json"), json_endpoint)
    app.add(Route("GET /user/{user_id}"), get_user)
    app.add(Route("GET /page"), html_page)
    app.add(Route("GET /await"), awaits_once)
    app.add(Route("POST /validate"), validate)
    app.add(Route("GET /big"), big_body)
    app.compiled()
    app.freeze()
    return app


def request(
    path: str,
    *,
    accept_encoding: str | None = None,
    body: bytes | None = None,
    method: str = "GET",
) -> bytes:
    lines = [
        f"{method} {path} HTTP/1.1",
        "host: localhost",
        "user-agent: bench/1.0",
        "accept: */*",
    ]
    if accept_encoding:
        lines.append(f"accept-encoding: {accept_encoding}")
    if body is not None:
        lines.append("content-type: application/json")
        lines.append(f"content-length: {len(body)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode()
    return raw + (body or b"")


BROWSER_HEADERS = (
    b"GET /plaintext HTTP/1.1\r\nhost: localhost\r\n"
    b"user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36\r\n"
    b"accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8\r\n"
    b"accept-language: en-US,en;q=0.9\r\naccept-encoding: gzip, deflate, br, zstd\r\n"
    b'sec-ch-ua: "Chromium";v="130"\r\nsec-ch-ua-mobile: ?0\r\nsec-ch-ua-platform: "Linux"\r\n'
    b"sec-fetch-site: none\r\nsec-fetch-mode: navigate\r\nsec-fetch-user: ?1\r\nsec-fetch-dest: document\r\n"
    b"upgrade-insecure-requests: 1\r\ncookie: session=abcdef0123456789; theme=dark\r\n\r\n"
)

# Eager cases: the handler finishes inside data_received, so one call per request.
EAGER = {
    "plaintext": request("/plaintext"),
    "json": request("/json"),
    "user_42": request("/user/42"),
    "browser_headers": BROWSER_HEADERS,
    "validate_post": request(
        "/validate", body=b'{"name":"Ada","age":42}', method="POST"
    ),
    "page_html_identity": request("/page"),
    "page_html_zstd": request("/page", accept_encoding="zstd, br, gzip"),
    "page_html_br": request("/page", accept_encoding="br"),
    "page_html_gzip": request("/page", accept_encoding="gzip"),
}


def make_protocol(loop, app, tracer=None):
    proto = HttpProtocol(
        loop,
        app,
        tracer or NoOpTracer(),
        CompressionConfig(),
        Connections(date=DATE),
    )
    transport = NullTransport()
    proto.connection_made(transport)
    return proto, transport


async def bench_eager(app, raw: bytes, tracer=None) -> float:
    loop = asyncio.get_running_loop()
    proto, transport = make_protocol(loop, app, tracer)
    feed = proto.data_received
    for _ in range(200):
        feed(raw)
    await asyncio.sleep(0)
    best = float("inf")
    for _ in range(REPEAT):
        t0 = time.perf_counter_ns()
        for _ in range(N):
            feed(raw)
        await asyncio.sleep(0)
        best = min(best, (time.perf_counter_ns() - t0) / N)
    assert not transport.closing, "connection closed during the benchmark"
    return best


async def bench_await_once(app) -> float:
    """Feed one request, run the loop twice so the suspended handler finishes."""
    loop = asyncio.get_running_loop()
    proto, transport = make_protocol(loop, app)
    feed = proto.data_received
    raw = request("/await")
    sleep = asyncio.sleep
    for _ in range(200):
        feed(raw)
        await sleep(0)
        await sleep(0)
    n = N // 4
    best = float("inf")
    for _ in range(REPEAT):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            feed(raw)
            await sleep(0)
            await sleep(0)
        best = min(best, (time.perf_counter_ns() - t0) / n)
    base = float("inf")
    for _ in range(REPEAT):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            await sleep(0)
            await sleep(0)
        base = min(base, (time.perf_counter_ns() - t0) / n)
    assert not transport.closing
    return best - base


async def bench_sse(accept_encoding: str | None) -> tuple[float, float]:
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    element = h.Div({"id": "tick", "class": "card"}, h.Span("tick"), h.B("bold"))
    n = N // 2

    async def handler(c, w):
        sse = SSE(w)
        for _ in range(200):
            sse.patch_elements(element)
        t0 = time.perf_counter_ns()
        for _ in range(n):
            sse.patch_elements(element)
        elements = (time.perf_counter_ns() - t0) / n
        t1 = time.perf_counter_ns()
        for i in range(n):
            sse.patch_signals({"count": i, "name": "x"})
        signals = (time.perf_counter_ns() - t1) / n
        done.set_result((elements, signals))

    app = App()
    app.add(Route("GET /sse"), handler)
    app.compiled()
    app.freeze()
    proto, _transport = make_protocol(loop, app)
    proto.data_received(request("/sse", accept_encoding=accept_encoding))
    return await asyncio.wait_for(done, 60)


# --- HTTP/2: minimal frames and HPACK (literal fields, no dynamic table) ---

H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"


def _frame(ftype: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    return (
        len(payload).to_bytes(3, "big")
        + bytes((ftype, flags))
        + stream_id.to_bytes(4, "big")
        + payload
    )


def _hpack_int(value: int, prefix_bits: int, tag: int) -> bytes:
    limit = (1 << prefix_bits) - 1
    if value < limit:
        return bytes((tag | value,))
    out = bytearray((tag | limit,))
    value -= limit
    while value >= 128:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _literal(index: int, value: bytes) -> bytes:
    """Literal field without indexing, static-table name."""
    return _hpack_int(index, 4, 0x00) + _hpack_int(len(value), 7, 0x00) + value


def h2_request(path: str, *, accept_encoding: str | None = None) -> bytes:
    """HPACK block: :method GET, :scheme http, :path, :authority, accept."""
    block = (
        bytes((0x82, 0x86))  # indexed :method GET, :scheme http
        + _literal(4, path.encode())  # :path
        + _literal(1, b"localhost")  # :authority
        + _literal(19, b"*/*")  # accept
    )
    if accept_encoding:
        block += _literal(16, accept_encoding.encode())  # accept-encoding
    return block


H2_CASES = {
    "h2_plaintext": h2_request("/plaintext"),
    "h2_json": h2_request("/json"),
    "h2_page_html_identity": h2_request("/page"),
    "h2_page_html_zstd": h2_request("/page", accept_encoding="zstd"),
    "h2_big_64k": h2_request("/big"),
}


async def bench_h2(app, block: bytes) -> tuple[float, float]:
    """Best ns per request, and transport write calls per request."""
    loop = asyncio.get_running_loop()
    proto, transport = make_protocol(loop, app)
    feed = proto.data_received
    # Client SETTINGS: a 2 GiB stream window, so no response body waits on
    # stream credit; the connection window starts at 64 MiB and is topped up
    # by what the responses used every 500 requests (frame headers counted
    # too, so the credit only creeps up, never runs dry).
    feed(
        H2_PREFACE
        + _frame(0x4, 0, 0, (4).to_bytes(2, "big") + (0x7FFFFFFF).to_bytes(4, "big"))
        + _frame(0x8, 0, 0, (64 * 1024 * 1024 - 65535).to_bytes(4, "big"))
    )
    feed(_frame(0x4, 0x1, 0))  # ACK the server's SETTINGS
    stream_id = 1
    credited = transport.nbytes

    def one() -> None:
        nonlocal stream_id, credited
        feed(_frame(0x1, 0x4 | 0x1, stream_id, block))  # END_HEADERS|END_STREAM
        stream_id += 2
        if stream_id % 1000 == 1:
            used = transport.nbytes - credited
            credited = transport.nbytes
            feed(_frame(0x8, 0, 0, used.to_bytes(4, "big")))

    for _ in range(200):
        one()
    await asyncio.sleep(0)
    best = float("inf")
    writes = 0.0
    for _ in range(REPEAT):
        w0 = transport.nwrites
        t0 = time.perf_counter_ns()
        for _ in range(N):
            one()
        await asyncio.sleep(0)
        best = min(best, (time.perf_counter_ns() - t0) / N)
        writes = (transport.nwrites - w0) / N
    assert not transport.closing, "connection closed during the benchmark"
    return best, writes


async def main(only: str | None) -> None:
    app = await build_app()
    print(f"{'case':<28}{'µs/req':>10}")

    def show(name: str, ns: float) -> None:
        if only is None or name.startswith(only):
            print(f"{name:<28}{ns / 1000:>10.2f}")

    for name, raw in EAGER.items():
        if only is None or name.startswith(only):
            show(name, await bench_eager(app, raw))
    if only in (None, "await_once"):
        show("await_once", await bench_await_once(app))
    if only in (None, "traced_json"):
        # A queue deep enough that the writer thread never drops a span
        # while the request loop runs ahead of it.
        with JsonTracer(path="/dev/null", max_pending_spans=1 << 20) as tracer:
            show("traced_json", await bench_eager(app, EAGER["plaintext"], tracer))
            await asyncio.sleep(0.5)
    if only in (None, "sse_patch"):
        for ae in (None, "zstd"):
            elements, signals = await bench_sse(ae)
            label = "identity" if ae is None else ae
            show(f"sse_patch_elements_{label}", elements)
            show(f"sse_patch_signals_{label}", signals)
    for name, block in H2_CASES.items():
        if only is None or name.startswith(only):
            ns, writes = await bench_h2(app, block)
            print(f"{name:<28}{ns / 1000:>10.2f}   ({writes:.1f} writes/req)")


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
