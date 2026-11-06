# Stario benchmarks

Two suites, one per layer we care about:

- `html/` — HTML generation speed: stario against other Python renderers, plus
  microbenchmarks for stario's own hot paths.
- `server/` — end-to-end HTTP throughput: the current Stario checkout against a
  few Python framework/server combinations under `wrk`.

The goal is repeatable local signal, not lab-grade numbers. Run on a quiet
machine and compare repeated runs before drawing conclusions.

## HTML generation (`html/`)

### Comparison: `html/compare.py`

Renders the same 50-row product page (autoescaping on everywhere) with:

- `stario @baked` — precompiled layout, slots spliced per call
- `stario` naive — full tree built per call
- `jinja2` — compiled template, autoescape
- `htpy`
- `dominate`
- `tdom` — Python 3.14 t-strings ([tdom](https://github.com/t-strings/tdom));
  naive inline template and Page/Row component functions

Times are best-of-N microseconds per full page render; lower is better.
Install competitor packages on demand, or run from a virtual environment that
already has them:

```bash
cd projects/stario
uv run --with dominate --with htpy --with jinja2 --with tdom benchmarks/html/compare.py
```

### Microbenchmarks: `html/micro.py`

Hot-path timings for stario only — tag construction with each attribute shape,
`@baked` splice calls (positional and keyword), and the render walk. Use these
to catch regressions when touching `tag.py`, `attributes.py`, `baked.py`, or
`render.py`. No extra dependencies:

```bash
cd projects/stario
uv run benchmarks/html/micro.py
```

## In-process protocol (`server/inproc.py`)

Feeds keep-alive HTTP/1.1 requests straight into `HttpProtocol` with a
transport that discards writes and prints the best-of-N microseconds per
request: plaintext, JSON, a path parameter, a browser's full header set, a
small POST, a 50-row HTML page with each content coding, a traced request,
and SSE `patch_elements` / `patch_signals` per event. No sockets, no load
generator, no extra packages, about a minute:

```bash
cd projects/stario
uv run benchmarks/server/inproc.py            # all cases
uv run benchmarks/server/inproc.py plaintext  # one case
```

Use it to compare two checkouts on the same machine; a change of a few
hundred nanoseconds on the plaintext case is visible. Record both numbers
in a change that touches the request path or the markup builder.

## HTTP server (`server/`)

Compares the current Stario checkout with a few Python web framework/server
combinations. The route mix follows Cemrehan Çavdar's framework comparison:
https://cemrehancavdar.com/2026/02/10/framework-benchmark/

The runner keeps the setup explicit: one process per target, one load
generator, dedicated virtual environments, and separate ports so a stale
server cannot be reused by accident.

Targets:

- `stario` — this checkout with no-op tracing
- `fastapi` — FastAPI on one Uvicorn worker
- `blacksheep-uvicorn` — BlackSheep on one Uvicorn worker
- `blacksheep-granian` — BlackSheep on one Granian worker
- `sanic` — Sanic in one process
- `falcon` — Falcon ASGI on one Uvicorn worker
- `robyn` — Robyn on one process and one worker thread

### Benchmark shape

- One worker/process per framework.
- Same paths: `/plaintext`, `/json`, `/user/42`, and `POST /validate`.
- Same `wrk` settings for every run.
- Stario response compression disabled with negative codec levels.
- JSON response bodies use `ujson` for Stario and FastAPI to match Sanic's
  `ujson` dependency more closely.
- Stario runs with `STARIO_TRACER=noop`; FastAPI, BlackSheep/Uvicorn,
  BlackSheep/Granian, Falcon, and Sanic run with access logging disabled.
- Robyn runs with `--processes 1 --workers 1 --log-level WARN
  --disable-openapi` (no per-request access log middleware; OpenAPI routes off).
  Handlers are sync, static GET routes use `const=True`, and handlers return bare
  `str`/`dict` so Robyn's Rust executor can serve them (manual `Response` objects
  force a slower Python path). JSON on the fast path uses Robyn's native orjson
  serialization; validate still parses with `ujson` for parity with other targets.

FastAPI uses Pydantic validation, matching the referenced benchmark. Stario,
BlackSheep, Falcon, Robyn, and Sanic validate the JSON body manually because
they do not bundle a Pydantic-style request validation layer.

### Requirements

- `uv`
- `wrk`
- Python compatible with Stario (3.12+)

All targets run on `uvloop`: Stario starts its server with `uvloop.run`,
FastAPI/Uvicorn uses `--loop uvloop`, and Sanic uses its uvloop-backed
default when installed.

The runner creates dedicated virtual environments under
`benchmarks/server/.venvs/` with `uv venv` and `uv pip install`, then starts
each server from its own environment. The Stario target installs this checkout
as `stario @ file://...`. Use `REFRESH_ENVS=1` after changing dependencies or
upgrading framework versions.

### Run

```bash
cd projects/stario
benchmarks/server/run.sh
```

The default run is the standard comparison shape:

- `DURATION=10s`
- `THREADS=2`
- `CONNECTIONS=128`
- `PORT=3000` as the base port
- one process or worker per target

Use those defaults when you want numbers that are easiest to compare with
other local runs. The generated `config.txt` records the exact settings for
that run.

Common options:

```bash
DURATION=30s THREADS=2 CONNECTIONS=128 benchmarks/server/run.sh
benchmarks/server/run.sh stario blacksheep-granian
PORT=3999 benchmarks/server/run.sh
REFRESH_ENVS=1 benchmarks/server/run.sh
KEEP_RAW=1 benchmarks/server/run.sh
```

`PORT` is a base port, not a shared port. The runner assigns fixed offsets per
target (`stario` on `PORT`, `fastapi` on `PORT+1`, and so on) and checks each
port before starting a server.

Each run writes a timestamped directory under `benchmarks/server/results/`:

- `summary.md` — markdown tables suitable for copying into notes or docs.
- `config.txt` — run settings.

Successful runs keep only `summary.md` and `config.txt` by default. Use
`KEEP_RAW=1` to keep the per-endpoint `wrk` output and server logs. Failed
runs leave the logs in place so startup issues can be inspected.

The `POST /validate` benchmark uses `benchmarks/server/validate.lua` so every
endpoint runs through `wrk`.

The Lua file is intentionally small:

```lua
wrk.method = "POST"
wrk.body = '{"name":"Ada","age":42}'
wrk.headers["Content-Type"] = "application/json"
```
