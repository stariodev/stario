<p align="center">
  <picture>
    <img alt="Stario" src="https://raw.githubusercontent.com/stariodev/stario/main/docs/img/stario.png" style="height: 160px; width: auto;">
  </picture>
</p>

<p align="center">
  <strong>Stario</strong><br>
  Craft realtime hypermedia apps that are a joy to write and ship.
</p>

<p align="center">
  <a href="https://stario.dev">Documentation</a>
  ·
  <a href="https://github.com/stariodev/stario">Source</a>
</p>

---

Stario is a small Python framework for enjoyable realtime hypermedia apps. It helps you build web apps where HTTP, HTML, and streaming stay visible in your code. Handlers are plain async functions; routes are registered explicitly; responses go through a dedicated writer. When the UI needs live updates, you can add [Datastar](https://stario.dev/docs/how-tos/datastar-sdk) and [Relay](https://stario.dev/docs/reference/toolbox#relay) without throwing away the same request/response mental model. The idea is [Go-to architecture](https://stario.dev/docs/explanation/go-to-architecture). The [SDK](https://stario.dev/docs/how-tos/datastar-sdk) and [tiles tutorial](https://stario.dev/docs/tutorials/realtime-tiles) live here.

Full guides, API reference, and tutorials live at [stario.dev](https://stario.dev). This page is a short orientation for people landing on the repository.

## Where Stario fits

Stario is an asyncio-native HTTP stack: you write async handlers and register routes on an `App`, and you start the built-in HTTP server (TCP or a Unix domain socket) with `asyncio.run(stario.serve(bootstrap))` (or `uvloop.run(...)`). It is not an ASGI application you mount in Uvicorn or Hypercorn; wiring goes through the `bootstrap` hook, `Context`, and `Writer` instead.

## Requirements

Python 3.12 or newer is required, on Linux (glibc), macOS, or Windows. The HTTP server is compiled: PyPI ships wheels for Linux x86_64/aarch64, macOS arm64, and Windows x86-64 with nghttp2, Brotli 1.2.1, and zstd bundled. Elsewhere `pip` builds from source and needs a C compiler, `pkg-config`, nghttp2 1.66+, Brotli with the CVE-2026-111497 fix (1.0.10+, 1.1.1+, or 1.2.1+), and the zstd development packages; on Windows, MSVC and [`scripts/build-native-deps.ps1`](scripts/build-native-deps.ps1), which builds those libraries with vcpkg and prints the `PKG_CONFIG` and `PKG_CONFIG_PATH` to set.

**Windows:** the server runs on the stdlib proactor loop with one event loop per process (`STARIO_THREADS` stays at 1 without `SO_REUSEPORT`), and has no Unix domain sockets. Ctrl+C or Ctrl+Break (`SIGBREAK`) starts a graceful shutdown; Windows never delivers `SIGTERM`. `reuse_addr` is ignored there, because `SO_REUSEADDR` on Windows lets another socket bind the same port. Winsock takes a whole send into kernel memory while the socket's send buffer has room, however large, so a big one-shot `respond()` to a client that is not reading is held by the kernel instead of tripping `write_timeout`; stream large bodies (`w.write` + `await w.drain()`, or `Files`) to keep that bounded per connection. `Files` opens files without delete sharing, so replacing a file while it is being streamed fails until that response ends.

**uvloop (optional):** Stario runs on whatever loop you start. For a faster event loop on Linux/macOS, install the optional extra and call `uvloop.run(serve(bootstrap))` instead of `asyncio.run(...)`:

```bash
uv add "stario[uvloop]"
# or: pip install "stario[uvloop]"
```

uvloop is not supported on Windows (the extra installs nothing there). `STARIO_THREADS=N` is N full servers on one TCP port (`SO_REUSEPORT`); each worker thread creates the same loop class as the loop running `serve()`. Without `SO_REUSEPORT` the process stays at one thread.

**Free-threaded workers (optional):** on Python 3.14t, `STARIO_THREADS=N` runs N event loops in one process so handler CPU can use more cores while `Relay` stays in-process. See [`docs/free-threading.md`](docs/free-threading.md).

### JSON codec

Stario uses one process-wide JSON codec for responses, Datastar signals,
telemetry, and the test client. The default codec uses the standard library and
emits compact UTF-8 JSON. Replace it explicitly when the application uses
another library — its encode and decode functions plug in directly:

```python
import msgspec

import stario.json as stario_json

stario_json.set_codec(msgspec.json.encode, msgspec.json.decode)
```

orjson has native byte output, so its byte path does not encode text first:

```python
import orjson

import stario.json as stario_json

stario_json.set_codec(orjson.dumps, orjson.loads)
```

`encode` returns one compact JSON value as text or UTF-8 bytes; Stario converts
once where it needs the other form. `dumps()` returns text, `dumps_bytes()`
returns UTF-8 bytes, and `loads()` accepts text, bytes, or a byte array. Stario
uses bytes for HTTP and SSE and text for HTML attributes and telemetry storage.
A byte-native encoder only decodes when a text consumer asks for `dumps()`.

Calling `set_codec()` again replaces the codec for later operations. Stario
does not synchronize replacement with active requests or telemetry writes.
Configure during application setup unless changing live serialization is
intentional. A codec may serialize its native datetime, UUID, Decimal, or model
types itself.

This is transport configuration only; validation and application models stay
in application code.

## Quick start

### From an example

The recommended first app is [Tiles](https://github.com/stariodev/tiles), a board that every visitor paints at once. It lives in its own repository, laid out as a full project with tests:

```bash
git clone https://github.com/stariodev/tiles
cd tiles
uv sync
uv run python -m app.main
```

The [Realtime tiles tutorial](https://stario.dev/docs/tutorials/realtime-tiles) builds it step by step. See [`examples/`](examples/) for **hello-world**, **jinja**, and **chat-room** (multi-file layout with SQLite).

### Manual setup

```bash
uv init my-app   # creates a new uv project (pyproject, layout)
cd my-app
uv add stario
```

For an app rather than a one-file experiment, start from the app guide in [stariodev/stario-create](https://github.com/stariodev/stario-create): copy its `AGENTS.md` into the repo root and install the skills with `npx skills add stariodev/stario-create`.

Put this in `main.py`:

```python
from stario import App, Context, Route, Span, Writer, h


async def home(c: Context, w: Writer) -> None:
    w.html(h.H1("Hello from Stario"))


HOME = Route("GET", "/")


async def bootstrap(app: App, span: Span):
    span.attr("app.name", "example")
    app.add(HOME, home)
    yield
```

Then serve it — `serve` is the only server entry point:

```python
import asyncio
from stario import serve

if __name__ == "__main__":
    asyncio.run(serve(bootstrap))
```

```bash
uv run python -m main
```

For development, reload on file changes with watchfiles:

```bash
uv run --with watchfiles watchfiles 'python -m main' .
```

Use `uvloop.run(serve(bootstrap))` when you want uvloop. When no
`config=` is passed, `serve` reads the `STARIO_*` environment variables;
otherwise pass `config=ServerConfig(host="0.0.0.0", port=9000)`.
`Server` takes a `ServerConfig` object.

Install with `pip install stario` if you are not using uv. During startup, `bootstrap` runs until its single `yield`: register routes and attach attributes to `span` before `yield`; put teardown after `yield` when needed. Server runtime policy (`STARIO_HOST`, `STARIO_PORT`, `STARIO_TRACER`, and related vars) is configured through environment variables — see `ServerConfig.from_env` (Stario does not load `.env` files; export vars in your shell or use your own dotenv tooling). See [Getting started](https://stario.dev/docs) for project layout. For containers, TLS, and production-oriented setup, see [Deployment, containers, and TLS](https://stario.dev/docs/how-tos/deployment-containers-and-tls).

### Filesystem URLs

Build `Assets` or `Files` at module level. Call `href()` there. Call
`await attach(app)` in bootstrap (register + load). `Assets` precompresses
by default; `Files` does not unless you pass `precompress=`:

```python
from stario import App, Assets, Files, Span

ASSETS = Assets("./static", "/static")
UPLOADS = Files("./uploads", "/data")
STYLE_CSS = ASSETS.href("css/style.css")


async def bootstrap(app: App, span: Span):
    span.attrs(await ASSETS.attach(app))
    await UPLOADS.attach(app, precompress=("br", "gzip"))
    yield
```

`Assets` hashes names and 307s the logical path. Both send strong ETags
and `X-Content-Type-Options: nosniff`.

## What you get

- Explicit wiring: async-generator `bootstrap(app, span)` with a single `yield`, `Route` endpoints, no hidden registration.
- Sharp primitives: `Context` for the request, `Writer` for the response, HTML/SVG trees via `stario.markup`, telemetry via `span`.
- Files: `Assets` and `Files` expose a directory at a URL prefix.
  `attach(app)` registers GET/HEAD and loads the tree. `Assets`
  hashes names and 307s the logical path. Both use strong ETags and 304.
  Import from `stario` or `stario.filesystem`.
- Hypermedia by default: HTML and SSE are first-class; realtime layers are optional when the product needs them.
- Observable runs: spans for startup and requests are part of how you structure apps, not an afterthought.

## What Stario is not

No bundled ORM, admin UI, or plugin discovery system. Databases, auth, and brokers stay in your code or thin adapters; the framework stays a focused HTTP and hypermedia core.

## Releases

The bump commit is the source of truth. The tag must name that version.
There is no autotag, and the build does not rewrite the version.

1. Keep notes under `## Unreleased` in [`CHANGELOG.md`](CHANGELOG.md).
2. When those notes are the release, one commit:
   - set `version` in `pyproject.toml` (for example `5.0.0`)
   - move `## Unreleased` to `## 5.0.0 - YYYY-MM-DD` and leave an empty
     `## Unreleased` above it
3. Tag that commit and push:

```bash
git tag 5.0.0
git push origin 5.0.0
```

A GitHub Release with the same tag is the same event. The workflow tests
3.12–3.14, checks that the tag, `pyproject.toml`, and changelog agree,
then uploads `stario-5.0.0` to PyPI. If the tag does not match the
committed version, the job fails.

## Contributing

From `stario/`:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyrefly check
uv run pytest
```

Before committing:

```bash
uv run ruff check . --fix
uv run ruff format .
```
