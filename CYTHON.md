# Cython HTTP core

HTTP/1 is [llhttp](https://github.com/nodejs/llhttp); HTTP/2 is nghttp2.
Both live in one `HttpProtocol` and share `RequestExchange` in
`src/stario_cython`. `serve()` is the only server entry point; uvloop is
optional (`uvloop.run(serve(bootstrap))`). The free-threaded runtime
(`STARIO_THREADS`, `SO_REUSEPORT`, CPU pinning):
[`docs/free-threading.md`](docs/free-threading.md).

Building needs `pkg-config`, nghttp2 1.66+ (older distro builds that carry
the security backports: set `STARIO_ALLOW_OLD_NGHTTP2=1`), Brotli with the
CVE-2026-111497 fix (1.0.10+, 1.1.1+, or 1.2.1+; `STARIO_ALLOW_OLD_BROTLI=1`
only when the distro backports that fix without a version bump), and zstd
development packages (`libnghttp2-dev libbrotli-dev libzstd-dev` on
Debian/Ubuntu, `brew install pkg-config nghttp2 brotli zstd` on macOS).
On Windows, `scripts/build-native-deps.ps1` (PowerShell 7, `git` on PATH,
network access for vcpkg's downloads) builds nghttp2, Brotli, zstd, zlib,
and pkgconf with vcpkg pinned to one commit, as static libraries on the
dynamic CRT, and prints the `$env:PKG_CONFIG` and `$env:PKG_CONFIG_PATH`
assignments `setup.py` reads; the extension build then needs MSVC (Visual
Studio Build Tools). Windows wheels bundle nghttp2 1.71.0 from that pin;
Linux and macOS wheels bundle 1.70.0 from `build-native-deps.sh`.
`PKG_CONFIG` names the pkg-config binary on every platform.
`scripts/build-native-deps.sh` always builds Brotli 1.2.1 (pass `brotli`
to build only that library). Gzip links system zlib (`-lz`; on Windows,
vcpkg's static zlib). Published
wheels bundle nghttp2, Brotli 1.2.1, and zstd. Python `CompressionConfig` /
`Files` use the same libs through `stario_cython.codecs`.

```bash
uv sync --all-extras          # editable install; compiles the extensions
cd examples/hello-world && uv run python -m main   # serve the smallest example
```

After editing a `.pyx` / `.pxd` / `.pxi` / `vendor/*.c`, rebuild in place
with `uv run python setup.py build_ext --inplace`.

The build leaves out debug info (`-g0`, and `-s` on Linux), so a wheel
stays small. To debug the extensions with `gdb` or `perf`, rebuild with
`STARIO_DEBUG_BUILD=1 uv run python setup.py build_ext --inplace`.

Direct TLS: `STARIO_SSL_CERTFILE` / `STARIO_SSL_KEYFILE` (or
`ServerConfig(ssl=…)`). ALPN advertises `h2` then `http/1.1`.
`STARIO_HOST` and `STARIO_PORT` set the bind address.

On glibc, the first `HttpProtocol` sets `M_MMAP_THRESHOLD` and
`M_TRIM_THRESHOLD` to 64 MiB for the whole process: request bodies,
compressor scratch, and compressed output between 128 KiB and 64 MiB stay on
the heap instead of costing an `mmap` / `munmap` each, and the heap is not
trimmed after every large response. The trim threshold is per malloc arena
(one per thread that allocates), so a process with N worker threads can
retain up to about N × 64 MiB of freed memory. `STARIO_MALLOC_TUNE=0` leaves
the allocator alone.

## Files

| File | Holds |
| --- | --- |
| `protocol.pyx` | `HttpProtocol`: socket, llhttp / nghttp2 session, pause/resume, pipeline queue, timeouts. `Connections`: one loop's registry with the C timeout sweep |
| `exchange.pyx` | `RequestExchange` (arena, upload body, response writer, HTTP/2 stream), `Request`, `RequestHeaders`, `RequestHandle` (`c` / `w`), the thread-local spare list |
| `path.pxi` | Request-target scan, percent decoding, canonical 308 path, host parsing |
| `query.pxi` | `ParsedQuery` |
| `cookies.pxi` | `ParsedCookies` |
| `headers.pxi` | `Headers` (response, `w.headers`), name interning |
| `router.pxi` | `CRouter` trie compiled from the Python `Router` |
| `codecs.pyx` | Brotli / zstd / gzip for Python callers (`Assets`, `Files`) |
| `markup.pyx` | `Tag.__call__` (element builder), the render walk, `escape_text` / `escape_attribute_value`; `stario.markup` re-exports them |
| `vendor/compression_buf.c` | Encoder pools (thread-local), one-shot Brotli, decoders |

## Request path

1. `data_received` / `buffer_updated` feed llhttp (or nghttp2) in C.
2. Header callbacks copy into the exchange arena; hot headers (Host, Cookie,
   Content-Length, Accept-Encoding, Expect, Connection) are indexed as they
   land.
3. `_bind_request` classifies the target: origin-form routes; `OPTIONS *` is
   204; absolute-form uses the URI authority as host; a bad escape, `#`, a
   decoded control byte, invalid UTF-8, or a NUL in the path **or the query**
   is 400; dot segments and a trailing slash are 308. Nothing is decoded yet.
4. `_dispatch` starts the handler Task (eager) or queues it behind the
   in-flight pipelined response. Bodies ≤ 256 KiB with a Content-Length
   complete before dispatch, so `body()` is already bytes.
5. `_start_exchange_inner` walks the compiled trie from the raw path, builds
   `Match` in C, and runs `handler(c, w)`. The trie compiles on the first
   lookup after a registration, not on every `app.add`. A loop with a task
   factory set gets `loop.create_task`; the default builds the Task directly.
6. `respond()` / `end()` free the connection via `response_completed`; the
   Task may outlive the response and joins `app.tasks` (bound once per
   connection) only if it did not finish eagerly.

## Class ownership

Three objects, no overlapping duties:

| Object | Owns | Lifetime |
| --- | --- | --- |
| `HttpProtocol` (`Connection`) | Socket, llhttp/nghttp2 session, pause/resume, pipeline queue, timeouts, one idle exchange | One TCP/TLS connection |
| `RequestExchange` | Keep-alive, Accept-Encoding, Expect, HEAD, arena, body, writer, H2 stream | One request; pooled on the connection then this thread |
| `RequestHandle` | Handler `c` / `w` (one object) | One dispatch; finished when the exchange recycles |

Keep-alive is set on the exchange at HTTP/1 headers-complete. Recycle
resets it (`_clear_request_binding`), so a 413/431 drain that still needs
the value snapshots it *before* `handler_finished()`. Encoding, Expect, and
HEAD stay on the exchange for the whole request; the protocol does not keep
a second copy.

Recycle uniqueness uses `PyUnstable_Object_IsUniquelyReferenced` on 3.14+
(else `Py_REFCNT == 1`) and a thread-local retained-copy counter. Do not
put a process-global increment on that path.

## Request lifetime and paths

- **Handle per dispatch.** The exchange (arena, buffers, compressors) is
  pooled; `c` / `w` is a small `RequestHandle` created per dispatch (one
  object for both). When the exchange recycles, the handle is finished:
  writes raise, `end()`/`abort()` no-op, reads still describe its request.
  Recycle checks uniqueness: if user code still holds the handle,
  `c.req`, or its header view, those take a private copy of the arena; if
  not, the handle and view are reused for the next request (no one can tell).
  `tests/cython/test_request_lifetime.py` pins that plain HTTP/1, pipelined,
  and HTTP/2 requests never take the copy path.
- **Paths.** The router gets the raw path, splits on `/`, and decodes each
  segment (no `str` for ASCII paths without `%`). `scan_request_path` does the
  RFC 3986 checks once in C: 400 for bad escapes / invalid UTF-8 / decoded
  controls / `#`; 308 for dot segments and trailing slashes. `query_has_nul`
  rejects `%00` in the query the same way. Invalid targets are answered in
  order through the exchange, so HTTP/1 keep-alive survives and HTTP/2 fails
  only that stream. `OPTIONS *` is 204; absolute-form uses the URI authority
  as the host.
- **Cost.** The handle and path checks cost about +525 instructions per
  keep-alive `GET /plaintext` in-process (callgrind). The hot-path cuts below
  more than pay for it: versus `ca93709` (before any of this), plaintext is
  14,580 → 13,498 instructions per request and `GET /user/{id}` 30,390 →
  25,868.

## Hot-path cuts

- `Match(pattern, params)` is built in C (allocate + slot fill + mappingproxy)
  instead of running `Match.__init__` in Python (~290 ns per param hit).
- All 100–599 status lines are prebuilt (was an `HTTPStatus` lookup per
  response for anything but ten codes).
- Route methods are interned at compile time and looked up with
  `PyDict_GetItem`; `Task(...)` gets a per-connection kwargs dict; transport
  `is_closing` / `writelines` and `Task.done` are bound once; `app.tasks` is
  bound once per connection (no `threading.local` lookup per request).
- The connection interface the exchange calls is `cdef` (no Python-override
  check per call); `get_buffer` / `buffer_updated` call C directly.
- `c.req.body()` on a body that is already complete (≤ 256 KiB with a
  Content-Length) returns the bytes without awaiting a second coroutine
  (`RequestExchange.c_read_now`).
- HTTP/2 DATA frames up to 4 KiB are copied into the outbound scratch with
  their header, so a small response is one `write` of one bytes object;
  bigger frames still go out as a zero-copy memoryview.
- `w.drain()`: write backpressure (transport `pause_writing`, HTTP/2 stream
  window). Used by `Files` / `Assets`.
- A complete Brotli body (`respond()`) uses the one-shot
  `BrotliEncoderCompress` into a thread-local scratch buffer. Brotli has no
  encoder reset, so the stream encoder was created and destroyed per
  response; streams (`write()`) still use it. zstd and gzip reset in place.

## Ordering, backpressure, drain

- A connection-closing protocol error behind in-flight pipelined requests is
  deferred: parsing stops, earlier responses go out, then the error and close.
  Keep-alive 413/431 go through `_dispatch` like any request.
- Body backpressure: HTTP/1 pauses the socket (bodies not yet read pause after
  64 KiB once the handler started); HTTP/2 withholds per-stream WINDOW_UPDATE
  (bounded by the 1 MiB stream window) and never pauses the socket.
- `close_if_idle()` on HTTP/2 sends GOAWAY(last processed stream); the socket
  closes when `nghttp2_session_want_read/write` both return 0.

## Timeouts

Header, idle, body-stall, write-stall, lingering close, and the HTTP/2
GOAWAY grace share **one** cleanup: `Connections` (one per loop) ticks
from a single `loop.call_later` handle while it holds a connection with
timeouts: one `loop.time()`, then one C walk over its live list comparing
stored deadlines. No Task, no per-request `TimerHandle`, no `call_later`
per chunk, and no wakeup while idle. The same object serves the `Date`
header from a per-second cache (one coarse wall-clock read per response,
reformatted in C only when the second changed; `pin_date()` fixes it for
tests).

| | |
| --- | --- |
| Header | 5s, while headers or a deferred small body are still arriving |
| Idle | 5s, only when the connection is idle |
| Body stall | 30s, generation counter on the exchange |
| Lingering close | 2s after the FIN, then the socket closes |
| GOAWAY grace | 1s for the shutdown PING ACK, then the final GOAWAY |
| Pipeline cap | 8 |
| Tests | `Connections(sweep_interval=0.05, date=...)`, or `sweep(now)` driven by the test; `tests/cython` runs on `stario.testing.VirtualTimeLoop`, so timers fire in virtual time |
| Off | `HttpProtocol(timeout_cleanup="off")` (the test client): no tick, and a lingering close closes at once |

A deadline is stored as `0.0` when armed and filled with `now + timeout` on
the next sweep, so an effective timeout is `T` to `T + 1s`. That trade keeps
`loop.time()` off the keep-alive path.

## Missing / not doing (on purpose)

`App` and `Router` stay Python; `find_handler` runs on the `CRouter` trie
compiled from them. Do not revive:

- a dual Cython App/Router
- a contiguous serializer
- pooled `asyncio.Event`
- static / pre-serialized handlers
- pooled / reset `Request` objects
- a second server entry point (`python -m stario_cython` was ~12% slower
  than `stario serve` on the same protocol and was removed)

picohttpparser is not the H1 parser: parser-only it is ~2.5–3.3× llhttp,
but end-to-end GET was even and a tiny JSON POST was slower.

nghttp2's `nghttp2_ssize` API (`nghttp2_session_mem_send2`, `mem_recv2`,
`nghttp2_data_provider2`, 1.60+) is not used on purpose. It only swaps
`ssize_t` for `ptrdiff_t` (a C-standard type whose range covers nghttp2's
error codes): no security or performance change, and the `ssize_t` API is
deprecated but kept. Migrating would break `STARIO_ALLOW_OLD_NGHTTP2=1`
source builds on distros that ship 1.59 (Ubuntu 24.04). Revisit when
nghttp2 announces removal. MSVC has no `ssize_t`, so `nghttp2.pxd`
defines it from `SSIZE_T` before including `nghttp2.h`.
