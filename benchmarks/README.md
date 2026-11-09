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

Renders the same product page (autoescaping on everywhere) with:

- `stario @baked` — precompiled layout, slots spliced per call
- `stario` naive — full tree built per call
- `jinja2` — compiled template, autoescape
- `mako` — compiled template, `h` (HTML-escape) default filter
- `htpy`
- `fastcore.xml` — FastHTML's FT components, `to_xml(indent=False)`
- `dominate`
- `tdom` — Python 3.14 t-strings ([tdom](https://github.com/t-strings/tdom));
  naive inline template and Page/Row component functions
- `f-string + markupsafe` and `f-string + html.escape` — no library, just
  escape and join: baselines to measure the libraries against

Two categories are being compared, and both are fair fights: template
engines compile once and fill values per call (jinja2, mako, and
`stario @baked`), builders construct a tree per call (htpy, dominate,
fastcore, tdom, and stario naive).

Every contender's output is checked against stario's before timing
(doctype and inter-tag whitespace stripped, entities decoded, since
escapers spell `"` differently). Times are best-of-7 microseconds per full
page; lower is better.

- `--rows N` (default 50) changes the row count.
- `--escape` puts `<>&"'` in every row's name, so escaping does real work
  instead of scanning clean ASCII.

```bash
cd projects/stario
uv run --with dominate --with htpy --with jinja2 --with tdom --with mako \
  --with fastcore --with markupsafe benchmarks/html/compare.py --rows 50 --escape
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

The last section runs only on a free-threaded build (3.14t): each of 1, then
2, threads builds and renders its own 50-row table, and it prints aggregate
throughput and the 2-thread scale. Pin it to two idle CPUs; below 1.7x it
flags the line, which means something every thread reads (a `Tag`, one of
its strings, a cache) has its refcount written again. See
[`docs/free-threading.md`](../docs/free-threading.md).

```bash
taskset -c 0,1 uv run benchmarks/html/micro.py --threads
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

`server/bench.py` compares the current Stario checkout with other Python
frameworks over real sockets. It sets up one virtual environment per target
under `server/.venvs/`, starts one server at a time, checks it, loads it, and
writes `results/<timestamp>/summary.md` plus `results.json`. Standard library
only; it needs `uv`, `wrk`, and `taskset` (util-linux), and Linux for the CPU
pinning and `/proc` sampling.

```bash
cd projects/stario
python3 benchmarks/server/bench.py                          # every phase, every target (~1 h)
python3 benchmarks/server/bench.py stario fastapi           # some targets
python3 benchmarks/server/bench.py --phases throughput,mixed
benchmarks/server/run.sh                                    # throughput phase only (~15 min)
```

Targets (one worker each unless the phase says otherwise):

| Target | Server |
| --- | --- |
| `stario` | this checkout, `uvloop.run(serve(...))`, `STARIO_TRACER=noop` |
| `fastapi` | FastAPI + Pydantic on Uvicorn (`httptools`, `uvloop`) |
| `blacksheep-uvicorn` | BlackSheep on Uvicorn |
| `blacksheep-granian` | BlackSheep on Granian (`--runtime-threads 1`) |
| `sanic` | Sanic, single process |
| `falcon` | Falcon ASGI on Uvicorn |
| `robyn` | Robyn, one process, one worker |
| `stario-ft` | free-threaded 3.14t build, `STARIO_THREADS=N` (multicore phase only) |
| `stario-mp` | N GIL Stario processes sharing the port with `SO_REUSEPORT`, via the benchmark launcher `apps/stario_mp.py` (multicore phase only; Stario ships no multi-process mode, and each process has its own `Relay`) |

The Stario target installs `stario @ file://<checkout>` (a source build:
needs the native libraries in [`CYTHON.md`](../CYTHON.md)). To benchmark a
released wheel instead, set `STARIO_SPEC` (and `STARIO_FT_SPEC` for the
free-threaded target) to a path or requirement, e.g.
`STARIO_SPEC=/path/to/stario-5.7.0-cp314-...whl`. `--refresh-envs`
rebuilds the environments.

### Fairness rules

- **Same CPU budget.** Each server is pinned to `SERVER_CPUS` (default CPU 0)
  with `taskset`, including any helper threads Granian or Robyn start in
  Rust; `wrk` and the helper clients are pinned to `CLIENT_CPUS` (default
  2,3), so the load generator never steals the server's core. The
  throughput table reports server CPU per run; every target should be at
  ~1.00, i.e. the server, not the client, is the bottleneck.
- **Same answers.** Before measuring, every endpoint of every target is
  fetched and checked (status, JSON body, an invalid POST rejected), and
  every target's `/page` must match Stario's document. A run with
  socket errors or non-2xx responses prints them next to the number.
- **Handlers run per request.** Robyn's `const=True` caches a route's first
  response in Rust and never calls the handler again; no other target can do
  that, so it is off (`BENCH_ROBYN_CONST=1` turns it on).
- **JSON** goes through `ujson` explicitly where a framework allows it
  (Stario, FastAPI, BlackSheep, Falcon); Sanic uses its default (`ujson` when
  installed); Robyn serializes dicts with its built-in orjson.
- **Validation:** FastAPI validates `POST /validate` with Pydantic, as its
  users would; the others check the two fields by hand.
- **HTML:** Stario builds `/page` with `h.*` per request (the naive tree, what
  most handlers do). Every other target renders the same page with a compiled
  Jinja2 template with autoescape, the renderer their docs reach for. Stario
  also serves `/page-baked`, the same bytes through `@baked`, reported as an
  extra line.
- **Compression** is off in the main phases (`wrk` sends no
  `Accept-Encoding` anyway; Stario's codecs are also disabled with negative
  levels). The `compress` phase restarts each target with its built-in
  compression: Stario's codecs at their defaults (zstd 3, brotli 4, gzip 1),
  Starlette's `GZipMiddleware` (level 9), BlackSheep's `GzipMiddleware`
  (level 5). Sanic, Falcon, and Robyn have no built-in response compression
  and are left out of that phase. `--gzip-level N` forces one gzip level in
  every framework for a like-for-like codec comparison.
- **Median of `--repeat` runs** (default 3) of `--duration` seconds each
  (default 10), after a warm-up. Each reported metric (req/s, p50,
  p99, CPU) is its own median across the runs; errors are summed.

### Phases

| Phase | What it measures |
| --- | --- |
| `throughput` | `/plaintext`, `/json`, `/user/42`, `POST /validate`, `/page` at 128 connections: req/s, p50/p99, server CPU, bytes per response |
| `sweep` | `/page` at 8, 64, 256, 1024 connections: how latency grows with load |
| `mixed` | `mixed.lua`: 40% page, 30% path-param JSON, 20% JSON, 10% POST, every request with a real browser's header set and cookies |
| `compress` | `/page` with a browser's `Accept-Encoding` (and gzip-only, brotli-only for Stario), and the mix: req/s and bytes on the wire |
| `sse` | `sse_client.py`: 1,000 and 5,000 clients hold a Datastar SSE stream; the server broadcasts an HTML fragment every 0.5 s. Delivery latency (send to receive, same clock), share of events delivered, server CPU, peak RSS |
| `conns` | `conn_client.py`: 2,000 and 10,000 keep-alive clients each `GET /page` every 2 s (1,000 and 5,000 req/s offered): achieved req/s, latency, errors, reconnects, peak RSS |
| `multicore` | `MC_WORKERS` (2) workers on `MC_CPUS` (0,1): Stario free-threaded with N loop threads, Stario as N GIL processes, and multi-process FastAPI, Granian, Sanic, Robyn. The Stario targets also run with one worker as a same-run reference. For drift, run it several times with the targets in a different order each time and take medians |

The `sse` and `conns` clients are asyncio programs that split connections
across one process per client CPU. At 5,000 SSE clients they handle 10,000
events per second, so part of the delivery latency at that size can come
from the client, but it is the same client for every target.

`mixed.lua` sets `Host` by hand: `wrk.format()` with a custom header table
does not add one, and Stario (correctly, per RFC 9112) answers a Host-less
HTTP/1.1 request with 400 where the other servers accept it.

Knobs (flags, or the environment variable in brackets): `--duration`
(`DURATION`), `--repeat` (`REPEAT`), `--connections` (`CONNECTIONS`),
`--threads` (`THREADS`), `--sweep` (`SWEEP`), `--sse-clients`,
`--conn-clients`, `--server-cpus` (`SERVER_CPUS`), `--client-cpus`
(`CLIENT_CPUS`), `--port` (`PORT`, base port; targets use fixed offsets),
`--python` (`PYTHON`, default 3.14). `python3 benchmarks/server/bench.py -h`
lists them all.

The numbers are local signal, not lab-grade: run on a quiet machine with at
least four CPUs, and compare repeated runs before drawing conclusions.
[`RESULTS.md`](RESULTS.md) has the latest full run and what it says.
