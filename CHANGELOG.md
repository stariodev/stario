# Changelog

All notable changes to Stario are documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- `RequestPolicy.max_body_bytes` (`STARIO_REQUESTS_MAX_BODY_BYTES`) is now
  the default limit for one read, not a cap for the server. A handler
  passes `max_size=` to `body()` or `stream()` to accept more or less. An
  upload route reads `await c.req.body(max_size=6_000_000)`, and every
  other route keeps the default.
- The server no longer answers 413 before the handler runs when
  `Content-Length` is above the default. The read raises 413 instead. It
  checks the declared length first, so it allocates nothing and sends no
  `100 Continue`. Only a length above 2^31-1 bytes still gets a 413 before
  the handler.
- A body that the handler does not read drains up to the default, then the
  HTTP/1 connection closes. A small declared body (256 KiB or less) still
  drains and keeps the connection.
- A GET route also answers HEAD (RFC 9110 9.3.2). The handler runs with
  `c.req.method == "HEAD"`, and the server drops the body. Before, HEAD on
  a GET route was 405, and each route needed a HEAD twin. An explicit HEAD
  route still wins. The `Allow` header of a 405 now lists `HEAD` next to
  `GET`.
- `read_signals()` reads the `datastar` query parameter for HEAD, as for
  GET.
- A HEAD response ends at its headers (RFC 9112 6.3). The connection then
  serves the next request, and later `write()` / `end()` calls are no-ops.
  For HEAD, `c.disconnect` resolves and `c.disconnected` is true once the
  response is sent, so `c.alive()` stops an SSE handler. Before, an SSE
  handler kept running under HEAD and held a keep-alive connection.

### Added

- `stream(max_size=)` limits the total bytes of a streamed body.
- `read_signals(req, max_size=)` passes the limit to `body()`, for signals
  that carry a `FileSignal`.
- `stario.forms`: `read_form(req)` returns a `Form` with the text fields and
  the uploaded files of an HTML form. GET and HEAD read the query string.
  Other methods read an `application/x-www-form-urlencoded` or
  `multipart/form-data` body. Multipart bodies stream: each file is an
  `UploadFile` in a `SpooledTemporaryFile` that moves to disk past
  `spool_size`. Writes to disk and `await upload.read()` run in a thread,
  so the event loop keeps serving. `Form.get` / `getlist` / `items` / `as_dict` match
  `req.query`. `max_size`, `max_fields`, `max_field_size`, `max_files`, and
  `max_file_size` limit the body (413). A malformed body is 400, and another
  `Content-Type` is 415.
- `RequestBodyError` accepts 415.
- The test client quotes multipart names and filenames as browsers do
  (`"` as `%22`), not with backslashes. `read_form` decodes `%22`, `%0D`, and
  `%0A` in names and filenames back to `"`, CR, and LF.
- `same_origin(trusted_origins=[...])` lets a listed origin post cross-site
  (a payment return), and `same_origin(exempt=["/hooks"])` skips the check
  under a path prefix. The docstring now names `same_origin()` as the CSRF
  protection for current browsers: CSRF tokens would only add browsers from
  before about 2023.
- `stario.cookies.SignedCookie`: a cookie with a JSON payload that the
  client can read but not change. `set(w, data)` signs it with HMAC-SHA256,
  and `get(req)` returns a `SignedValue` (`data`, `issued_at`, `expires_at`,
  `age`) only when the signature is valid and the expiry, which the server
  checks, has not passed. The first of `keys` signs and every key verifies,
  for rotation. Each key is 32 bytes or more. The signing key is derived
  from the key and the cookie name, so a value cannot move to another
  cookie. A value past about 3.9 KB raises an error instead of being
  dropped by the browser. The defaults are `HttpOnly`, `Secure`, and
  `SameSite=Lax`.

## 5.3.4 - 2026-11-04

### Changed

- The wheels ship without debug info. The extensions compile with `-g0`,
  and on Linux they link with `-s`. The cp314 manylinux aarch64 wheel goes
  from 5.1 MB to 2.0 MB, and the `stario_cython` modules go from 14.3 MB
  to 2.3 MB on disk. The source and the code that runs do not change.
- `STARIO_DEBUG_BUILD=1` builds the extensions with debug info and symbols,
  for `gdb` or `perf`. A source build without it also leaves them out.

## 5.3.3 - 2026-11-04

### Fixed

- `c.alive()` no longer leaves callbacks on the app's shutdown future. Each
  scope read `app.shutdown`, which is a new `asyncio.shield`, and a shield
  that stays pending keeps its callbacks on the shared future until
  shutdown: two for each request on Python 3.14. A server that streamed
  many SSE requests grew in memory for its whole life. `c.alive()` now
  hooks the loop's shutdown future directly; a done-callback cannot cancel
  it.
- `app.shutdown` no longer uses `asyncio.shield`. On Python 3.14 a shield
  leaves a callback on the shared future even after it is cancelled, so
  `asyncio.wait_for(app.shutdown, delay)` leaked one callback for each
  call. `app.shutdown` now returns a Future linked to the loop's shared
  one. Cancelling it still leaves the shared future alone, and the link
  unhooks itself when it completes or is cancelled.
- The `App.shutdown` docstring says that a pending link keeps one callback
  until shutdown, so a loop that waits on each pass reads `app.shutdown`
  once, before the loop.

## 5.3.2 - 2026-11-04

### Changed

- The `server.shutdown` span names each task that the drain cancels. Each
  cancel is a `server.shutdown.task_cancelled` event with `task.name`: the
  name given to `create_task`, else the coroutine's `__qualname__`. A stuck
  request handler shows the handler's own name. `create_task` and the
  request path do not change; the name is read only at cancel time.
- With `STARIO_THREADS>1`, each worker thread hands its cancelled tasks to
  thread 0. Thread 0 records them on `server.shutdown` with `server.thread`,
  and `server.shutdown.cancelled_tasks` counts every thread.
- `TestClient` exit opens `server.shutdown` before the drain, the same order
  as `Server`. A bootstrap run uses its own shutdown span; a built `App`
  gets a new `server.shutdown` span. Tests can assert on the cancel events
  with `client.tracer`. With `owns_shutdown=False` there is no span.
- `TestTracer.has_open_spans()` takes `besides=` to skip one span id.

## 5.3.1 - 2026-11-04

### Fixed

- `Match.pattern` and `Match.params` have type annotations. Before, a
  type checker saw `c.match.params` as unknown, and pyrefly and ty
  reported it as a missing attribute. The `stats` slot of a static
  mount has an annotation too.

### Changed

- Type checks run with pyrefly instead of pyright.

## 5.3.0 - 2026-10-29

### Added

- `JsonTracer` writes to a file. `JsonTracer(path=...)` opens the file on
  enter in append mode — one JSON object per line, the same NDJSON that
  went to stdout — creates missing parent directories, and closes it on
  exit. Enter and exit again appends to the same file. `output` and `path`
  cannot both be given. `STARIO_TRACERS_JSON` is the environment form:
  `STARIO_TRACER=json` with `STARIO_TRACERS_JSON=/var/log/app/spans.ndjson`
  writes there instead of stdout; unset or empty keeps stdout.

### Deprecated

- `SqliteTracer` and `STARIO_TRACER=sqlite` are deprecated and will be
  removed in 6.0. Constructing the class warns with `DeprecationWarning`;
  the environment path writes one line to stderr. Migrate with
  `STARIO_TRACER=json` and `STARIO_TRACERS_JSON=<path>`, then query the
  NDJSON file with DuckDB `read_json` or the SQLite JSON functions.

## 5.2.1 - 2026-10-29

### Changed

- The TTY tracer has a calmer layout. Attributes print as `key=value`.
  Keys with the same dotted prefix share one row under a dim label
  (`compression  zstd_level=3 gzip_level=1`). A `*_bytes` value prints as
  `10MiB`, a boolean as `true`/`false`, and a path in the home directory
  starts with `~`. Each span line has one right-hand column, the duration
  (`7.3ms`, `28.26s`). The start offsets and the trace id tail are gone.
  The status code follows the request name. A child connector shows the
  child's status (`├─` done, `├×` failed, `├⠋` running). A child that took
  half of its root or more has a bold duration. Lines stop at 100 cells on
  a wide terminal. Below 50 columns, a root has no time prefix and a group
  label goes on its own row. A tree gets a blank line before and after it;
  one-line requests stack like an access log. `http.route` is hidden when
  it repeats the path.
- The TTY tracer works on every terminal theme and font. It uses only the
  eight base colors plus bold and dim. Dim marks structure (time, keys,
  rails) and never names, values, durations, or tracebacks. The glyphs come
  from the WGL4 set that every common monospace font has (`×` for a failure,
  `○` for waiting, `!` for an orphan), except the braille spinner. A stream
  that cannot encode UTF-8 gets an ASCII set (`*`, `x`, `|-`, `` `- ``).
  `STARIO_TRACERS_TTY_ASCII=1` forces the ASCII set and `0` forbids it;
  `TTYTracer(ascii=...)` does the same in code. `TERM=dumb` turns off color
  and the live footer.
- `server.startup` no longer records `server.listen_balance`; more than one
  `server.threads` always means `SO_REUSEPORT`. `server.threads_requested`
  is recorded only when it differs from `server.threads`.

## 5.2.0 - 2026-10-25

### Changed

- `string_literal` escapes `<` as `\x3c`, so its result is safe inside a
  `<script>` element and an HTML attribute — user text passed to
  `SSE.execute_script` or an action expression can no longer close either
  early.
- `TestClient` exit now matches the server's graceful shutdown: it signals
  `app.signal_shutdown()` before draining, so `while not app.shutting_down`
  work loops stop, and cancels tasks still pending after `shutdown_timeout`
  (default 5 s, like `STARIO_GRACEFUL_SHUTDOWN_TIMEOUT`; `None` restores the
  old wait-forever behavior). `drain_tasks(timeout=)` takes the same value.
- The examples use page classes — one `@dataclass(frozen=True, slots=True)`
  page per screen whose fields are its dependencies, `register(app)` wires
  each route to a method, and commands reply before they change state and
  publish on a `Relay[dict]`.

## 5.1.1 - 2026-10-24

### Added

- `stario_cython.codecs.brotli_decoder_version()` reports the linked
  libbrotlidec as `(major, minor, patch)`.

### Fixed

- Wheels bundle Brotli 1.2.1. The decoder in 1.0.3 through 1.2.0 writes
  out of bounds on a crafted stream (CVE-2026-111497). Source builds reject
  a `libbrotlidec` in that range. `STARIO_ALLOW_OLD_BROTLI=1` is only for a
  distro package that backports the fix without changing its version.
  Request bodies stay as the client sent them. `brotli_decompress`,
  `BrotliDecoder`, and `TestClient` are the paths that decode Brotli.

## 5.1.0 - 2026-10-20

### Added

- `set_codec(encode, decode)` takes two functions, e.g.
  `set_codec(orjson.dumps, orjson.loads)`.
- `ServerConfig(max_connections=)` / `STARIO_MAX_CONNECTIONS`. Over the cap,
  cleartext gets `503`, TLS is closed. The cap is split across worker loops.
- `relay.subscribe(..., maxsize=, latest=)` bounds the inbox (drop oldest, or
  newest per subject); drops count in `sub.dropped`. Default stays unbounded.
- Middleware `same_origin()` (403 for cross-origin unsafe methods via
  `Sec-Fetch-Site`/`Origin`) and `security_headers()` (nosniff, frame,
  referrer, optional HSTS).
- `SSE.ping()` heartbeat comment; `event_id=` and `retry=` on SSE events.
- `Files(..., untrusted=True)`: `CSP: sandbox` on every response, and
  `content-disposition: attachment` for HTML, XML, SVG, and JavaScript.
- `CompressionConfig(stream_window_log=)` / `STARIO_COMPRESS_STREAM_WINDOW_LOG`:
  a smaller encoder for streamed `write()` responses. Default `None` (as
  before). `16` cuts an idle stream to ~110 KiB (zstd) from ~3.6 MiB, but
  whole-document repaints larger than the window send far more bytes.

### Changed

- Signal names pass to Datastar unchanged; the snake_case rule and the
  `__case.snake` modifier are gone. `data.signal`/`computed`/`bind` render
  Datastar's value form. Dotted keys in `data.signals()` and
  `SSE.patch_signals()` are nested paths.
- `set_cookie` / `delete_cookie` default to `httponly=True`.
- Routes freeze after bootstrap: `app.add`/`use`/`not_found`/
  `method_not_allowed` raise `StarioError`.
- `read_signals` raises `RequestBodyError(400)` for invalid signals, so the
  client gets `400`, not `500`. `RequestBodyError` accepts `400`.
- A handler failure is recorded once: on the span, or in the `stario.http`
  log when there is no span.
- The TTY tracer is rewritten: finished requests print as trees, open
  requests repaint in a live area.
- `build-native-deps.sh` checks a pinned SHA-256 for each tarball.

### Deprecated

- A codec object in `set_codec`, `JsonCodec`, `StdlibJsonCodec`, and
  `default=` on `dumps()`/`dumps_bytes()`. Removed in 6.0.

### Fixed

- HTTP/2: a response stalled by flow control is reset after
  `write_timeout`. It could pin the stream and handler forever.
- HTTP/2: `c.disconnect`/`c.disconnected` follow the stream, so a reset
  stream ends its own `alive()` only.
- Shutdown no longer hangs on a client that stopped reading.
- A small request body sent slowly uses `body_timeout`, not
  `header_timeout`. A stalled one gets `408`.
- A kept `c` no longer keeps the closed connection and its buffers alive.
- `respond()` copies a mutable body when custom headers are set.
- `c.alive()` swallows only its own cancel and does not cancel a task that
  is already unwinding.
- A handler that returns without a response after its client left aborts
  quietly (`request.client_left` on the span), not with a `500`.
- `Files`/`Assets` open files in a worker thread, not on the event loop.
- A failed input-buffer resize closes the connection instead of dropping
  bytes.
- Telemetry no longer raises on NaN or infinite span attributes.

## 5.0.4 - 2026-10-13

### Changed

- Content negotiation now prefers `zstd` over `br` for dynamic and streamed
  responses when the client weights both equally. zstd is within ~8% of brotli
  on wire size at a fraction of the encode CPU, which matters per request and
  per SSE event. Precompressed `Assets`/`Files` replies still prefer `br`:
  those bytes are encoded once at load, so only wire size counts.
  The tie-break order is configurable through `CompressionConfig(preference=…)`
  or `STARIO_COMPRESS_PREFERENCE` (comma-separated names; unlisted encodings
  fill in the default order). `negotiate_content_encoding` takes a
  `preference` argument for the order.

## 5.0.3 - 2026-10-13

### Added

- `at.query(...)` builds `@query(...)`, the Datastar 1.0.4 action that sends a
  `QUERY` request with signals in the body. `Route("QUERY", ...)` and
  `read_signals` already cover it.

### Changed

- `ModuleScript()` now defaults to the Datastar 1.0.4 bundle
  (`@v1.0.4/bundles/datastar.js`). Vendored copies in the examples and in
  `stario-create` moved to 1.0.4 as well.

## 5.0.2 - 2026-10-13

### Changed

- `xxhash` is no longer a dependency. Asset fingerprints come from
  `stario_cython.xxh64`, a small compiled xxHash64 module that keeps the same
  digests. The runtime has no Python package dependencies.

## 5.0.1 - 2026-10-13

### Changed

- `typing_extensions` is no longer a dependency. `_is_relative` uses
  `typing.TypeGuard` (stdlib since 3.10) instead of `TypeIs` (3.13+).
  The runtime depends only on `xxhash`.

### Fixed

- A handler that streamed a body and returned without calling `end()` now
  finishes the response cleanly instead of aborting the connection mid-body.
  One-shot `SSE` writes (`sse.navigate(...)`, `sse.patch_elements(...)` as the
  last handler statement) work again as they did in 4.x. The 500 path still
  applies only when the handler never sent anything, and a handler that fails
  after headers still aborts.

## 5.0.0 - 2026-10-12

### Breaking changes

- Cython llhttp/nghttp2 is the production HTTP runtime. The Python httptools
  protocol is gone. `serve()` runs the compiled protocol.
- **Stario is a compiled package.** PyPI ships wheels for Linux (x86_64,
  aarch64; glibc) and macOS (arm64) on CPython 3.12–3.14 and 3.14t, with
  nghttp2, Brotli, and zstd bundled. Other platforms build from the sdist and
  need a C compiler, `pkg-config`, and the nghttp2 (1.66+), Brotli, and zstd
  development packages. Windows and musl are not supported. An older distro
  nghttp2 that carries the security backports builds with
  `STARIO_ALLOW_OLD_NGHTTP2=1`.
- `stario_cython.request` is gone; import `Request` from `stario.http.request`
  (typing) or `stario_cython.exchange`.
- `Headers`, `ParsedQuery`, `ParsedCookies`, and `Request` are typed as the
  concrete Cython classes (`stario_cython/*.pyi`), so `Headers()` /
  `ParsedQuery(b"")` / `Request(...)` type-check.
- **`c.req.headers` is a read-only `RequestHeaders`**, and is typed that way
  (it was typed as `Headers`, so `set()` / `add()` type-checked and then
  raised). `Request(headers=...)` accepts `Headers` or a mapping and stores a
  `RequestHeaders`; build one directly with `RequestHeaders(...)`
  (`stario.http.headers`). `req.headers` can no longer be reassigned.
- `stario.testing.harness.TestRequest` is gone: `TestClient` and test helpers
  build the production `Request`.
- Removed the APIs deprecated in 4.2.0:
  - `UrlPath` / `as_target`. Pass `/path` or `//host/path` strings, or use
    `Route`. `app.use` / `not_found` / `method_not_allowed` take `str` only.
  - `app.get` / `app.post` / `app.handle` / … and `Route.get` /
    `Route.query` / …. Use `app.add(Route("GET /"), handler)`.
  - `at.fetch`. Name the verb at the call site: `at.get(route.href())`,
    `at.post(route.href())`.
  - `stario.staticassets` (`AssetManifest`, `StaticAssets`). Use `Assets` /
    `Files` and `await attach(app)`.
- `App.on_error` and exception-type mapping are gone. Uncaught handler
  exceptions are logged. If the handler sent nothing, the framework writes
  **500**; a response already on the wire is not rewritten. Handlers must
  write a complete response (`respond` / `end`) or use
  `stario.http.middleware.catch_errors` to map app exceptions.
- **`HttpException` removed.** Body read failures raise `RequestBodyError`
  (408/413). Map them with `catch_request_body_errors()` or custom middleware.
- Route handlers must be `async def` (or a callable whose `__call__` is async).
- The HTTP protocol schedules `find_handler` then `create_task(handler(c, w))`
  instead of `create_task(app(c, w))`. Trailing-slash 308 is written inline in
  the Cython protocol (no handler task).
- **`c` / `w` belong to one handler call.** Each dispatch gets a fresh handle
  (one object is both `c` and `w`). Once the handler has returned and the
  response is complete, the handle is finished: `respond()`, `write()`,
  `write_headers()`, and `w.headers` raise `StarioRuntime`; `end()` /
  `abort()` are no-ops; `req`, `match`, `state`, `span`, and `status_code`
  still describe that request. A `c.req` (or its headers, query, cookies)
  kept past the handler keeps its own data. Its body is still readable if the
  handler read it, otherwise `body()` raises `StarioRuntime`.
- **Paths follow RFC 3986 / RFC 9112.** Routing splits the raw path on `/`
  and percent-decodes each segment, so `%2F` is data inside one segment and
  params are fully decoded (`/files/a%2Fb` → `name="a/b"`, `%252F` →
  `"%2F"`). `req.path` is the fully decoded path and the new `req.raw_path`
  is the path as sent. `find_handler(host, path, method)` takes the raw path.
  Dot segments (`.`, `..`, `%2E` forms) 308 to the normalized path, like a
  trailing slash. 400 (in order, keep-alive preserved, HTTP/2 stream only)
  for a malformed `%XX`, invalid UTF-8, a decoded control byte, a `#`, or
  `*` with any method but `OPTIONS`. `OPTIONS *` answers 204. HTTP/1
  absolute-form (`GET http://host/path`) routes on its path and uses the URI
  authority as the host. Route patterns reject `.` / `..` segments.
- 204 and 304 responses no longer send `Content-Length` (or `Content-Type`).
  `respond()` / `write_headers()` reject statuses outside 200–599.
- **Pipelined errors keep response order.** A protocol error (bad request,
  429 over the pipeline cap) behind in-flight pipelined requests is written
  only after their responses, then the connection closes. Keep-alive 413/431
  are answered in order like any response. Previously the error could be
  read as the answer to an earlier request whose handler still ran.
- **Upload backpressure before the body is read.** If a handler has started
  but not asked for the body, HTTP/1 reading pauses after 64 KiB instead of
  buffering up to `max_body_bytes`; the declared `Content-Length` is only
  reserved once `body()` reads it. HTTP/2 holds that stream's WINDOW_UPDATE
  instead of pausing the socket, so one slow consumer no longer stalls
  other streams.
- **HTTP/2 graceful drain.** Shutdown sends GOAWAY with the last processed
  stream; in-flight streams finish, new ones are refused, and the connection
  closes when nghttp2 is done (also after a client GOAWAY).
- **The CLI is gone** — `stario serve`, `stario watch`, the
  `[project.scripts]` entry point, and the `watchfiles` dependency. Run the
  app with `python -m …` under `asyncio.run(serve(bootstrap))` (or
  `uvloop.run`); use an external reloader such as
  `uvx watchfiles 'python -m app.main' app/` in development.
- `serve(bootstrap, *, config=None, tracer=None)` is the only server entry
  point: `Server.run()`, `Server.loop_runner()`, and the `**options` keyword
  form are gone. `config=None` reads `STARIO_*` via `ServerConfig.from_env()`;
  a passed config is used as-is — no merging. `tracer=None` reads
  `STARIO_TRACER` via `tracer_from_env()` (now in `stario.telemetry`) and
  scopes the tracer to the call.
- `STARIO_LOOP` / `ServerConfig.event_loop` are gone. Worker threads
  (`STARIO_THREADS>1`) create the same loop class as the loop running
  `serve()`, so `uvloop.run(serve(bootstrap))` gives uvloop workers.
- `stario.responses` is gone: the response helpers are `Writer` methods —
  `w.text`, `w.json`, `w.html`, `w.redirect`, `w.empty`. `w.json` honours
  `stario.json.set_codec`; `JsonValue` moved to `stario.json`. The root
  exports `h` (`stario.markup.html`), `classes`, `ServerConfig`, `JsonValue`;
  the documented handler import is
  `from stario import App, Context, Route, Span, Writer, h`.
- `Writer.write_headers` lost the `body=` keyword. HEAD is automatic: the
  declared `Content-Length` keeps the GET entity size and the body is dropped.
- `catch_request_body_errors` / `respond_request_body_error` are gone. The
  server answers `RedirectException` (3xx) and `RequestBodyError` (413/408)
  itself when no response has started; a 408 carries `Connection: close`.
- **Middleware composes at compile time.** `app.use` and `app.add` work in
  any order (the late-`use` error is gone), and scope middleware now also
  wraps the 404/405 answers under its prefix — including the defaults.
  An app that wrapped `not_found` in middleware by hand now runs it twice:
  drop the manual wrap.
- **`TestClient` runs the production protocol.** Each request becomes
  HTTP/1.1 bytes fed to the real `HttpProtocol` on an in-memory transport —
  routing, 308/400 answers, framing, compression, and the request span are
  the server's own. `App.__call__`, `stario.testing.harness`,
  `stario.testing.dispatch`, and `stario.testing.transport` are removed;
  unit-test a handler with your own doubles or go through `TestClient`.
  `requests=` (a `RequestPolicy`) sets server limits; a `timeout` hangs up
  and cancels that handler; leaving `client.stream(...)` disconnects and
  `c.disconnected` / `c.alive()` see it; protocol-answered spans are named
  by method (a 308 by the route it lands on).
- `python -m stario_cython` / `stario_cython.serve` are gone; `serve()` is
  the only entry point. `STARIO_CYTHON_TIMEOUTS` /
  `STARIO_CYTHON_TIMEOUT_SWEEP` are gone — a `Connections` registry sweeps
  timeouts from the Server's Date tick (`timeout_cleanup=` on `HttpProtocol`
  for direct use).
- Idle keep-alive connections hold ~8 KiB instead of ~69 KiB (the read
  buffer is no longer zero-filled).
- Each header field counts 32 bytes toward `max_header_bytes` (RFC 7541
  §4.1), bounding the field count. `Host: example.com.` matches
  `example.com`. A failure while starting a handler (e.g. a tracer error)
  answers 500 instead of stalling the connection.

### Added

- `run_app` / `AppRun` (`stario.http.bootstrap`, exported from
  `stario.http`): the single app lifecycle. `Server.serve` and the test
  `aload_app` both run on it — one `server.startup` span, one
  `server.shutdown` span, same drain.
- `ServerConfig.pin_threads` / `STARIO_PIN_THREADS=1` (opt-in): with
  `STARIO_THREADS>1` each worker pins to one allowed CPU on Linux. Off by
  default — threads a pinned worker spawns inherit its mask.

- Native `zstd` on the Cython writer (`libzstd`), negotiated
  `br` → `zstd` → `gzip` like Python `CompressionConfig`. Wheels bundle
  libzstd; source builds need `libzstd-dev` (or `brew install zstd`).
  `Files` / `Assets`, TestClient, and `CompressionConfig` use the same
  libbrotli / libzstd via `stario_cython.codecs`. The Python `brotli`
  and `zstandard` packages are no longer dependencies.
- `await w.drain()` — write-side backpressure for streaming handlers.
  `write()` never blocks; `drain()` waits while the transport has paused
  writing or an HTTP/2 stream has more than 256 KiB of unsent DATA, and
  returns immediately once the client is gone or the request has finished.
  `Files` / `Assets` await it after each chunk, so a slow client no longer
  makes the server read a whole file into memory. TestClient's writer and
  the `Writer` protocol have it too.

- `RequestPolicy.write_timeout` / `STARIO_REQUESTS_WRITE_TIMEOUT` (default
  30s): a connection whose output stays stuck that long is aborted (see
  Fixed).
- `STARIO_THREADS` — opt-in worker count (`1` default). `N>1` runs N
  event-loop threads, each a full `create_server` on the same TCP port
  via `SO_REUSEPORT` (thread 0 also owns signals and shutdown). The
  kernel load-balances new connections. `STARIO_LOOP` is resolved once
  and audited per thread. If `SO_REUSEPORT` cannot share the listen
  address (Unix sockets on many kernels, Windows), Stario stays at one
  thread. Requires free-threaded Python 3.14t unless
  `STARIO_THREADS_ALLOW_GIL=1`. See `docs/free-threading.md`.
- `stario.http.middleware.catch_errors` — wrap handlers so listed exceptions
  become HTTP responses when nothing was sent yet. Presets:
  `catch_request_body_errors()` and `respond_request_body_error`.
- Cython HTTP/2 via nghttp2 on the same connection class. Switch once per
  socket (TLS ALPN `h2` or the cleartext connection preface). Responses go
  out as frames, not HTTP/1.1 text.
- Direct TLS: `ServerConfig(ssl=…)` or `STARIO_SSL_CERTFILE` /
  `STARIO_SSL_KEYFILE`. Context is TLS 1.2+ with ALPN `h2`, `http/1.1`.

### Fixed

- HTTP/1 keep-alive is stored only on the request exchange. A 413/431
  that still needs the value after the exchange recycles snapshots it
  first, so recycle cannot flip a `Connection: close` request back to
  keep-alive.
- Recycle uniqueness uses the 3.14 unique-ref API (else `Py_REFCNT`)
  and a thread-local retained-copy counter, so free-threaded workers do
  not race a process-global increment.

- HEAD requests with `Accept-Encoding` no longer get a compressed body
  after the headers (which desynced keep-alive).
- HEAD responses carry the same fields as GET (RFC 9110 §9.3.2): a
  streaming handler no longer answers HEAD with `content-length: 0`, and a
  compressible `respond()` gets GET's `Content-Encoding` and `Vary`. HEAD
  omits Content-Length when GET's is only known by producing the body.
- A client that stops reading no longer keeps its connection and buffered
  responses forever. Timeouts used a graceful close that waits for the
  write buffer to drain, and pipelined requests kept being answered into
  it. Output stuck for `write_timeout` now aborts the connection, and
  pipelined requests wait while writes are paused.
- HTTP/2 idle and header timeouts send GOAWAY (last processed stream)
  before closing. A server-side failure on one stream (dispatch error,
  rejected response headers) resets that stream instead of closing the
  connection.
- `Headers({...})` encodes `str` keys and values and raises `TypeError` for
  other types; it read the `str` object's memory as bytes before.
- The TLS handshake times out after `header_timeout` instead of asyncio's
  60s.
- `set_cookie` rejects an invalid `samesite` (it could inject cookie
  attributes) and emits through the validating header API.
- `write()` past a declared `Content-Length` raises `StarioRuntime` before
  sending anything; previously the extra bytes went out and corrupted the
  next response on the connection.
- HTTP/1 chunked trailers are dropped (RFC 9110 §6.5) instead of being
  merged into `c.req.headers` after the handler started.
- HTTP/2 copies a caller's `bytearray` passed to `write()` / `respond()`;
  mutating it afterwards no longer changes what is sent.
- HTTP/2 `end()` no longer cancels a handler that keeps running after it,
  and a client `RST_STREAM` is not echoed back.
- A pipelined request trickling its headers now hits the header timeout
  after the earlier response finishes (it used to get no deadline).
- HTTP/1.0 requests with `Transfer-Encoding` close after the response
  (RFC 9112 §6.1).
- `Headers.unsafe_*` raise `TypeError` for non-`bytes` arguments instead of
  reading invalid memory. `memoryview` bodies must be flat, contiguous
  bytes (`itemsize` 1, one dimension); others raise `TypeError` before
  anything is sent.
- Streaming to an HTTP/1.0 client without `Content-Length` sends a
  close-delimited body with `Connection: close` instead of chunked framing
  the client cannot parse.
- Chunked and gzip writes accept `memoryview` parts, and gzip handles
  inputs over 4 GiB.
- HTTP/2: client PINGs no longer keep an idle connection open; a stream
  whose response is done but whose request body is still open is reset
  (`NO_ERROR`) once the handler finishes; paused streams no longer let the
  connection window grow past its limit; fatal protocol errors flush the
  GOAWAY before closing; exceptions in header/data callbacks reset the
  stream instead of being ignored.
- HTTP/2 drain is two-phase: a shutdown-notice GOAWAY and PING, then the
  final GOAWAY on the PING ACK (or after 1 s), so requests in flight are
  not refused.
- HTTP/1: bare CRLFs between requests no longer keep an idle connection
  open.
- A body `stream()` or `body()` call that outlives its request raises
  `StarioRuntime` instead of reading the next request's body on the
  pooled exchange.
- Failures swallowed inside the server (nghttp2 callbacks, dispatch,
  exchange allocation, tracer spans, protocol error responses) are logged
  with tracebacks.
- A `%00` (or bare NUL) in the query is answered 400 like one in the path;
  it used to become a 500. A hand-built `ParsedQuery` decodes `%00` like
  `urllib.parse.unquote_plus`.
- `RequestHeaders.get()` / `getlist()` return the default for a name no
  request can carry, matching `in` (was `ValueError`).
- `app.add` no longer recompiles the router on every call; the first lookup
  compiles once and `aload_app` / `Server.serve` compile eagerly at startup
  (3000 routes: ~20 s → under a second).
- Handler tasks honour `loop.set_task_factory`; the default path still
  builds the `Task` directly.

### Changed

- `find_handler` walks a compiled trie with no cache in front. A static
  route returns a prebuilt `(handler, route, Match)`; a parameterized route
  allocates one `Match`. Exact hosts have their own path trie. One cursor
  walks the trie, one segment per node, so route insertion order never
  changes a match.
- Handler-task finish is `stario.http.invoke.on_handler_done`: log, write 500
  if nothing was sent, abort if a body was started but not finished, close
  the span. No auto-`end()`. A write-then-raise still logs (`Handler failed`);
  the response already on the wire is not rewritten.
- Every request that writes an HTTP status gets a started-and-ended span:
  handler responses, trailing-slash 308, and protocol 400 / 413 / 431 / 429
  / 503. Protocol outcomes are not `fail`ed. `NoOpSpan` still skips start/end.
  Matched routes rename the span to `Route.pattern` and set `http.route`.
- Cython GET path: skip upload state when there is no body (`mark_nobody`),
  and arm idle timeouts on the Date-tick sweeper instead of `loop.time()`
  per keep-alive request.
- Cython uploads: Content-Length bodies ≤ 256 KiB dispatch after the
  message is complete (`body()` is already bytes). `stream()` with a known
  Content-Length yields `min(length, 256 KiB)` instead of a fixed 64 KiB.
- HTTP/2 POST without Content-Length is END_STREAM-delimited (like H1
  chunked). Recycle clears nghttp2 stream user_data. Duplicate `:method` /
  `:path` / `:authority` are RST; `:authority` and `Host` must be equal.
- `ParsedQuery` first read fills a C name/value span table. HTTP/1 stays on
  [llhttp](https://github.com/nodejs/llhttp).
- HTTP/2 receive window is 1MiB per stream / 4MiB per connection. RST-stream
  flood is rate-limited. Header budget 431 / body 413 apply per stream.
- Cython request headers are a standalone read-only arena view (no unused
  `Headers` pair list). Timeout cleanup is chosen in `HttpProtocol.__init__`
  (`timeout_cleanup=`, env as default). Cookie `as_dict()` is cached. H1/H2
  methods share one byte table. Compressibility uses the Python helper.
- `stario.http.host.host_without_port` re-exports the Cython Host parser
  so request routing and the helper agree.
- A stalled body upload raises `RequestBodyError` 408 as
  `Request timeout: body upload too slow.`

### Performance

- `Relay`: a cross-thread `publish` hops to the owning loop only while a
  `receive()` is blocked, once per burst — 20,000 messages cost a handful of
  `call_soon_threadsafe` calls instead of 20,000.
- A complete Brotli body uses the one-shot encoder into a thread-local
  scratch buffer (no stream encoder per response).
- `app.tasks` is bound once per connection; `c.alive()` uses done-callbacks
  instead of a watcher `Task`.
- The timeout sweep is a C loop over one registry per event loop (was a
  Python sweep over connection attributes).

### Internal

- `exchange.pyx` is split into `.pxi` files (`path`, `query`, `cookies`,
  `headers`, `router`).
- CI runs the suite on Python 3.14t (free-threaded) and asserts the GIL
  stays off after importing the extensions.


## 4.3.0 - 2026-09-25

### Added

- `stario.serve(bootstrap, …)` — run the HTTP server on a loop you start:
  `asyncio.run(stario.serve(bootstrap, port=9000))` or `uvloop.run(...)`.
  Listen settings are keywords (`host`, `port`, `unix_socket`, …) or a
  prepared `config=`. After shutdown the coroutine finishes and the caller
  can continue. Omit `tracer` to get a TTY or JSON tracer for the call.

### Changed

- Python 3.12 and 3.13 are supported. The package requires Python 3.12 or newer.
- zstd uses the `zstandard` package on every Python. Stario no longer imports
  stdlib `compression.zstd`.
- Span and trace ids use the CPython 3.14 UUIDv7 bit layout
  (RFC 9562 Method 1) in `RecordingSpan.create`.
- `Server` raises if `unix_socket` is set and the platform has no `AF_UNIX`.
  `stario serve` / `stario watch` no longer check this before start.
- Startup span `server.event_loop` is the loop that is running, not
  `ServerConfig.event_loop`.

## 4.2.0 - 2026-09-21

### Added

- `Assets` and `Files` — a directory at a URL prefix. Call `href()` at import.
  Call `await attach(app)` in bootstrap to register GET/HEAD and load the tree.
  `register()` and `load()` stay available. `Assets` hashes names and 307s the
  logical path. Both send strong ETags, 304, and `nosniff`. Pass
  `content_types=` to add or override MIME types.
- `stario.json` — one process-wide codec for JSON responses, Datastar signals,
  telemetry, and the test client. Replace the standard-library default with
  `set_codec()`.

### Changed

- `Route` is the HTTP address: `Route("GET /home")` or
  `Route("POST", ROOM + "/send")`. After a match, read `c.match`. Matching
  lives in `stario.http`.
- Static routes hit an exact map. Parameterized routes walk a compressed trie.
  Pass `Request.host` (already folded). The matcher does not lowercase host.
- File streaming uses `os.pread` in a worker thread. The `aiofiles`
  dependency is gone.

### Fixed

- TTY tracer live footer — skip the write when text and width do not change.
  Cap the live block so erase cannot clear scrollback.
- `Assets.load()` re-hashes files already pinned by `href()`, so a same-size
  content edit is not missed.

### Deprecated

These still work. They will be removed in 5.0.

- `UrlPath` — prefer `Route` or a `/` / `//` string. `Files` and `Assets`
  take strings.
- `app.get` / `app.post` / `app.handle` and `Route.get` / `Route.post` / …
  — use `app.add(Route("GET /"), handler)`. `Route.query(path)` stays until
  5.0; `Route("QUERY /feed")` is the replacement.
- `at.fetch` — build the URL with `route.href()` and name the verb at the
  call site (`at.get(...)`, `at.post(...)`).
- `stario.staticassets` (`AssetManifest`, `StaticAssets`) — use `Assets` or
  `Files` and `await attach(app)`.

### Removed

- `c.route` (`RouteMatch`) — use `c.match` (`Match`).
- `stario.routing` — import `Route` and `UrlPath` from `stario` or
  `stario.http`.

## 4.1.1 - 2026-08-31

### Fixed

- `data.bind()` with `prop` or `event` — emit a key-only attribute. Datastar bind is exclusive (signal in the key or the value, not both); the previous form also set the value and Datastar raised `KeyAndValueProvided`.
- `data.persist(session=True)` — emit `data-persist__session` when `storage_key` is omitted.

### Added

- `data.on_intersect(..., view_transition=True)` — Datastar `__viewtransition` modifier.

## 4.1.0 - 2026-08-17

### Added

- `Route` — one HTTP method on one `UrlPath`. Declare endpoints with `Route.get` / `Route.post` / …, register them with `app.add(route, handler)`, and emit Datastar fetches with `at.fetch(route, params)`. `app.handle(method, path, handler)` and `app.get` / `app.post` stay the path + method contract. `at.fetch` reads the method from the `Route` and builds the URL with the same `href()` contract (`params`, `query=`, `fragment=`). `UrlPath` stays the method-free location for composition, `href()`, and middleware prefixes.
- HTTP `QUERY` ([RFC 10008](https://www.rfc-editor.org/rfc/rfc10008.html)) — `Route.query`, `app.query`, and `TestClient.query`. Safe and idempotent; the request body carries the query. `at.fetch` does not emit `@query` (Datastar has no such action).
- `STARIO_TRACER=module` — import the module and call `make_tracer()`. Use `module:callable` when the factory has another name.

## 4.0.1 - 2026-07-17

### Fixed

- `stario watch` on Windows — `subprocess.list2cmdline` for reload subprocess quoting; POSIX `shlex.quote` broke watchfiles spawn.

### Changed

- `debug_inspector()` — draggable signal overlay (`@baked`, yellow debug chrome, pointer capture, `data-ignore-morph`). Bottom-right only; `position=` removed. Tiles example includes it.

## 4.0.0 - 2026-06-25

Major release from 3.4. Delete old `stario-traces.sqlite3` files before upgrading — there is no in-place SQLite migration.

### Breaking changes

**Bootstrap and app lifecycle**

- `bootstrap(app, span)` — async generator with a single `yield`: startup before `yield`, teardown after.
- `App()` — requires a running event loop; exposes `shutdown` for server drain.
- `App.on_error` — handlers must be `async def`.

**Routing and URLs**

- Register full path patterns on `App`; scope middleware with `app.use(pattern, *middleware)`.
- `app.not_found(pattern, handler)` and `app.method_not_allowed(pattern, handler)` — prefix-scoped 404 / 405 handlers (inherited down the route branch).
- Build URLs with `UrlPath(...).href()` or `AssetManifest.href()`.
- `UrlPath` — typed path patterns and link generation; optional `host=` for host-aware routes (`UrlPath("/users", host="api.example.com")` or host placeholders such as `{tenant}.example.com`).

**Static assets**

- `AssetManifest(directory, …)` — scan, fingerprint, and `href()` at import time.
- `StaticAssets(manifest, …).register(app)` — serve and pre-compress in bootstrap.
- Hidden files excluded by default; pass `include_hidden=True` when dotfiles are intentional.

**HTTP exceptions and errors**

- `HttpException` — 4xx/5xx response bodies only; use `RedirectException` for 3xx and `responses.*` for 2xx.
- `RedirectException` — standalone type; `location` holds the target URL.
- Default `on_error` handlers call `responses.text` and `responses.redirect` directly.
- `ClientDisconnected` — default handler calls `Writer.abort()` (no response body).

**CLI and server configuration**

- Configure the server with `STARIO_*` environment variables — see `stario serve --help`. Stario does not auto-load dotenv files.
- `stario serve` / `stario watch` take only the app spec; `stario watch` keeps `--watch` / `--watch-ignore`.
- CLI entry point: `stario.cli.main:main`.
- Telemetry: set `STARIO_TRACER` and optional `STARIO_TRACERS_*`; construct `JsonTracer`, `SqliteTracer`, etc. directly in library code.

**Markup**

- Import HTML/SVG from `stario.markup` (for example `from stario.markup import html as h`).
- Package root re-exports framework primitives only — import `responses`, `cookies`, `Request`, and `Router` from their modules.

**Datastar**

- `from stario.datastar import data, at, SSE, read_signals, ModuleScript` — attribute and action helpers on `data.*` and `at.*`.
- One `SSE(w)` per response — create it once, then call `sse.patch_elements()`, `sse.patch_signals()`, `sse.navigate()`, and so on so the stream owns `Writer` headers and `Content-Type: text/event-stream` for every event.
- Signal names and dotted paths use Python `snake_case`; fetch options use `None` to omit.

**Telemetry**

- `Tracer` — `create(..., parent=)` and `on_end(span)`; call methods on the span handle.
- `Span.step()` for child spans; `Span.new_trace()` for a detached root; `Span.link(name, span_id, …)` for cross-span references.
- `RecordingSpan`, `NoOpSpan`, `ProxySpan`, `RecordedEvent`, `RecordedLink`, `TelemetryStats` exported from `stario.telemetry`.
- Backend env vars: `STARIO_TRACERS_SQLITE*`, `STARIO_TRACERS_JSON*`.
- `tracer.stats()` → `TelemetryStats` for sink health counters.
- Event `body` — `str`, `BaseException`, or `None`; structured data goes in attributes.
- Finished spans ignore mutations after `end()`.
- Traceback formatting: `stario.telemetry.formatters`.

### Removed

- `Router.mount`, `Router.push_middleware`, `App.url_for`, route `name=`
- `HttpException.respond()`, bundled tracer `from_env()` classmethods
- `stario init`, packaged CLI templates, CLI runtime flags (`--host`, `--port`, `--tracer`, compression, limits, timeouts, `--loop`, `--unix-socket`)
- `stario.telemetry.tracebacks`
- Flat `from stario import datastar as ds` namespace and module-level `datastar.sse.*` helpers

### Added

- `ServerConfig` and `RequestPolicy` — listen, compression, shutdown, and request limits (`stario.http.config`).
- `AssetManifest` and `StaticAssets.stats`.
- Static serving — `precompress=` codec selection, per-instance `content_types=` overrides, and `Range: bytes=…` on large streamed files (206 / 416; one range per request).
- `STARIO_REUSE_ADDR` — TCP `SO_REUSEADDR` (default `1`).
- `normalized_location` — shared redirect URL safety for `responses.redirect` and SSE navigation.

## 3.4.0 - 2026-05-27

- `STARIO_TRACER` and `from_env()` on bundled tracers — configure SQLite/JSON sinks from the environment without CLI flags.

## Earlier 3.x

- 3.3.0 — `NoOpTracer`, HTTP hot-path performance, HTML rendering internals.
- 3.2.0 — `Context.closing`, `App.wait_shutdown()`, Datastar v1.0.1 in examples.
- 3.1.0 — Datastar 1.0 compatibility, `Relay.subscribe(*patterns)`.
- 3.0.x — `App` replaces `Stario`, explicit bootstrap, `stario.datastar`, docs site at [stario.dev](https://stario.dev). See git tags for full 3.0.0 migration notes from 2.x.
