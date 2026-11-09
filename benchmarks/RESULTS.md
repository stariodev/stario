# Benchmark results — Stario 5.7.0, November 2026

One full run of `server/bench.py` and `html/compare.py`, what it says about
Stario against other Python frameworks, and where Stario can get better.
How the suites work and how to rerun them: [`README.md`](README.md).

**Machine:** 4 vCPU Intel Xeon @ 2.10 GHz cloud VM, 15 GB RAM, Linux 6.18,
CPython 3.14.6 (3.14.6t for the free-threaded run). Each server is pinned to
one CPU; `wrk` and the helper clients get two other CPUs. Each throughput and
latency number is the median of 3 × 10 s runs, metric by metric. The SSE and
connected-users numbers come from a single run each. The VM's speed drifts
during the day (±15% between morning and evening runs), so compare numbers
within one table. Every table below except SSE and multi-core comes from one
contiguous run (`results/20261109T164834Z`).

**Versions:** stario 5.7.0 (the PyPI wheel, byte-identical Python sources to
this checkout) · fastapi 0.162.2 / starlette 1.10.0 / uvicorn 0.54.0 /
pydantic 2.14.3 · blacksheep 2.6.4 · granian 2.8.7 · sanic 25.12.1 ·
falcon 4.4.0 · robyn 0.88.0 · uvloop 0.23.0 · jinja2 3.1.6 · mako 1.4.3 ·
htpy 26.5.1 · dominate 3.1.0 · fastcore 2.2.39 · tdom 0.1.20.

## Corrections after review

An independent review of the first version of this file found that the
Jinja2 templates (server `/page` and `html/compare.py`) wrote `{{ r.id }}`
on dicts. Jinja2 tries `getattr` first on every such lookup, which doubled
its render time (258 µs → 134 µs per server page). Every page-derived ratio in
the first version was inflated as a result. The templates now index
(`r["id"]`), and every affected phase was rerun. Other changes from the
review:

- Reported p50/p99 are now each metric's median across the 3 runs, not the
  latencies of the run with the median req/s.
- The HTML comparison adds an f-string + `html.escape` baseline, which is
  faster than markupsafe; the f-string rows are no longer called a "floor".
- The multi-core numbers are labelled as an unreleased branch, and the
  missing Stario multi-process baseline is stated.

## The short version

| Question | Stario | Best other | FastAPI |
| --- | --- | --- | --- |
| Small responses, one core | 49–83k req/s | 26–53k (BlackSheep on Granian) | 10–13k |
| A 16 KB HTML page, one core | 7.8k req/s (12.3k with `@baked`) | 5.0k (Falcon, Robyn, Sanic) | 4.0k |
| Browser-shaped mix, one core | 14.5k req/s, p99 15 ms | 9.0k (BlackSheep), p99 17–36 ms | 5.9k, p99 309 ms |
| Same mix, compression on | 13.2k req/s, 792 B/req | 6.0k, 886 B/req | 3.8k |
| 10,000 connections, `GET /page` each every 2 s | 4.5k of 5k req/s offered, p50 1.1 ms, p99 26 ms, 0 errors | similar req/s, p50 0.25–1.7 s | 2.8k req/s, 4k errors |
| SSE fan-out to 5,000 clients | p50 54 ms, 0.19 CPU | on par (Sanic) | p50 74 ms, 0.34 CPU |
| Rendering a 50-row page, no server | 18 µs (`@baked`), 41 µs (plain tree) | 49 µs (Jinja2) | — |

On one core, Stario is 1.1–1.9x the fastest other combination on small
responses, and 5–6x FastAPI. On the HTML page, the thing a hypermedia app
serves, it is 1.6x the next framework, and 2.5x with `@baked`. Its weak
spots are tail latency at 1,024+ concurrent connections (a uvloop scheduling
issue it shares with Uvicorn), and HTML building on free-threaded Python,
which does not scale across threads in 5.7.0 (fixed on an unreleased branch).

## Method notes and asymmetries

These are the choices a maintainer of another framework would ask about:

- Pinning every server to one core favors single-threaded event loops.
  Granian and Robyn run Rust I/O threads, and here those share the one core
  with the Python thread. Granian's single-core `/page` comes out slightly
  below Uvicorn's, and Granian scales super-linearly with two workers; both
  are signs of this.
- Every target except Stario renders `/page` with Jinja2. Stario uses its
  own `h.*` builder; that difference is the point of the comparison.
- FastAPI validates `POST /validate` with Pydantic, as its users would; the
  others check the two fields by hand.
- Stario runs with `STARIO_TRACER=noop` (its default JSON tracer off), the
  counterpart of turning off access logs in the other servers.
- The listen backlog is 2048 for Stario, Uvicorn, Granian, and Sanic. Robyn
  exposes no backlog setting, so it runs with its built-in value.
- Robyn is left out of the SSE test: its SSE model doesn't map onto this
  broadcast.
- Server CPU sits at 0.99–1.00 in every `wrk` phase. The SSE and
  connected-users phases don't saturate the server, by design.

## Server: one core

Requests/s at 128 connections. The last column is the geometric mean of the
per-endpoint ratio to Stario.

| Target | plaintext | json | params | validate | page | vs stario |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **stario** | **82,506** | **66,895** | **61,001** | **49,288** | **7,789** | 1.00x |
| blacksheep-granian | 52,571 | 52,793 | 53,484 | 25,737 | 4,659 | 0.67x |
| robyn | 31,893 | 30,616 | 20,609 | 19,981 | 4,952 | 0.43x |
| blacksheep-uvicorn | 24,924 | 25,019 | 22,806 | 20,713 | 4,862 | 0.41x |
| falcon | 24,196 | 23,235 | 22,907 | 20,648 | 4,965 | 0.40x |
| sanic | 23,425 | 22,305 | 21,695 | 18,718 | 4,920 | 0.38x |
| fastapi | 13,067 | 12,833 | 11,245 | 9,917 | 4,017 | 0.23x |

Stario `/page-baked` (the same bytes via `@baked`): **12,329 req/s**, 1.58x
its own `/page` and 2.5x the best Jinja2-backed server.

p99 latency (ms) at the same load:

| Target | plaintext | json | params | validate | page |
| --- | ---: | ---: | ---: | ---: | ---: |
| **stario** | **3.2** | **3.4** | **3.5** | **4.0** | **24** |
| blacksheep-granian | 3.8 | 3.9 | 3.9 | 10.5 | 40 |
| robyn | 5.6 | 6.1 | 8.1 | 9.0 | 41 |
| blacksheep-uvicorn | 8.7 | 7.5 | 9.4 | 9.3 | 218 |
| falcon | 8.3 | 8.9 | 8.1 | 7.9 | 184 |
| sanic | 8.0 | 8.5 | 7.9 | 10.4 | 261 |
| fastapi | 12.4 | 15.9 | 17.3 | 18.6 | 356 |

Response overhead is similar everywhere: a plaintext response is 130 B from
Stario, 147 B from Uvicorn and Granian, and 115–117 B from Robyn and Sanic.

**Where the page time goes:** Jinja2 renders this page in ~134 µs. Stario
builds the `h.*` tree in ~90 µs and renders it in ~15 µs, and `@baked`
brings the whole page to ~45 µs. On small responses, the gap comes from the
Cython HTTP core: `server/inproc.py` puts Stario's own cost at 1.35 µs per
plaintext request, out of ~12 µs per request over a socket. The other ~90%
is the event loop and syscalls.

## Server: the concurrency sweep (`/page`, req/s · p99 ms)

| Target | c=8 | c=64 | c=256 | c=1024 |
| --- | ---: | ---: | ---: | ---: |
| **stario** | **7,659 · 2.0** | **7,780 · 12.6** | **7,652 · 121** | **7,736 · 2,400 (294 err)** |
| blacksheep-granian | 4,750 · 3.4 | 4,761 · 20.5 | 4,752 · 83 | 4,612 · 282 |
| robyn | 4,920 · 3.0 | 4,976 · 21.7 | 4,751 · 80 | 5,082 · 532 |
| falcon | 4,861 · 3.4 | 4,844 · 23.0 | 4,948 · 2,330 (34 err) | 4,918 · 2,220 (225 err) |
| sanic | 4,875 · 3.2 | 4,957 · 20.7 | 4,828 · 2,110 | 4,808 · 2,200 (212 err) |
| blacksheep-uvicorn | 4,780 · 3.4 | 4,831 · 23.2 | 4,629 · 3,330 (44 err) | 4,863 · 2,150 (214 err) |
| fastapi | 4,069 · 3.9 | 4,124 · 33.8 | 4,022 · 3,230 (29 err) | 3,850 · 2,070 (201 err) |

Errors are `wrk` 5 s timeouts, summed over the 3 runs. Up to 64 connections
Stario has the lowest latency at 1.6x the throughput. From 256 connections
on, the Rust-I/O servers (Granian, Robyn) keep the fairest tail. At 1,024,
Stario's p50 stays at 37 ms, but a minority of connections starve for
seconds, as they do on every uvloop server here. See "Where Stario is weak",
item 2.

## Server: the day-to-day mix

`mixed.lua` sends 40% `/page`, 30% `/user/{id}`, 20% `/json`, and 10%
`POST /validate`. Every request carries a real Chrome header set (15
headers, cookies, `Accept-Encoding: gzip, deflate, br, zstd`).

| Target | req/s | p50 ms | p99 ms | req/s, compression on | bytes/req, compression on |
| --- | ---: | ---: | ---: | ---: | ---: |
| **stario** | **14,526** | **8.5** | **15.2** | **13,240** | **792** |
| blacksheep-granian | 9,010 | 12.4 | 36.0 | 5,915 | 886 |
| blacksheep-uvicorn | 8,929 | 14.1 | 17.4 | 6,003 | 886 |
| falcon | 8,507 | 14.6 | 108.8 | — | — |
| sanic | 8,333 | 15.1 | 55.3 | — | — |
| robyn | 8,315 | 15.0 | 24.1 | — | — |
| fastapi | 5,947 | 20.8 | 308.7 | 3,798 | 836 |

Sanic, Falcon, and Robyn have no built-in response compression. Real
deployments put them behind a proxy that compresses.

## Compression (`/page`, 16 KB of HTML)

Each framework with built-in compression, at its default settings:

| Target | Accept-Encoding | Coding chosen | Body | req/s | p99 ms |
| --- | --- | --- | ---: | ---: | ---: |
| **stario** | browser | **zstd (level 3)** | 1,609 B | **6,708** | 31 |
| stario | `gzip` only | gzip (level 1) | 2,102 B | 6,154 | 36 |
| stario | `br` only (Safari) | brotli (level 4) | 1,362 B | 4,607 | 51 |
| blacksheep-granian | browser | gzip (level 5) | 1,802 B | 3,339 | 60 |
| blacksheep-uvicorn | browser | gzip (level 5) | 1,802 B | 3,174 | 67 |
| fastapi | browser | gzip (level 9) | 1,679 B | 2,319 | 2,380 |

The same comparison with gzip forced to level 1 everywhere
(`--gzip-level 1`, `results/20261109T173739Z`): Stario 6,680 req/s with a
2,102 B body, against 3,188–3,814 req/s and 2,126 B for the others. At equal
codec settings, the 1.75x gap over the best of them matches the uncompressed
`/page` gap, so compression costs every framework about the same; the lead
comes from the rest of the request. At default settings, zstd costs Stario
14% of its uncompressed `/page` throughput; the others' default gzip costs
them 28–42%.

## Day-to-day capacity

**Connected clients.** N keep-alive connections each fetch `/page` every 2 s.
That is much heavier per connection than a real user, who loads assets once
and then reads.

| Target | 2,000 connections (1k req/s offered) | 10,000 connections (5k req/s offered) |
| --- | --- | --- |
| **stario** | p50 0.3 ms, p99 1.1 ms | **4,498 req/s, p50 1.1 ms, p99 26 ms, 0 errors** |
| robyn | p50 0.5 ms, p99 2.1 ms | 4,489 req/s, p50 950 ms, 0 errors |
| sanic | p50 0.5 ms, p99 1.7 ms | 4,298 req/s, p50 391 ms, 419 errors |
| blacksheep-granian | p50 0.5 ms, p99 2.1 ms | 4,297 req/s, p50 1.7 s, 0 errors |
| blacksheep-uvicorn | p50 0.5 ms, p99 2.0 ms | 4,234 req/s, p50 325 ms, 629 errors |
| falcon | p50 0.5 ms, p99 1.7 ms | 4,130 req/s, p50 239 ms, 722 errors |
| fastapi | p50 0.6 ms, p99 2.4 ms | 2,849 req/s, p50 343 ms, 4,170 errors |

Achieved req/s includes the connect ramp, so about 90% of offered counts as
a full pass. 5k req/s is near the other frameworks' single-core `/page`
capacity (~5k), so at 10,000 connections they queue (latency in the hundreds
of milliseconds to seconds) while Stario has headroom. This restates the
throughput result as latency under a fixed load; it is not separate evidence.

**SSE fan-out** (morning run, `results/20261109T095611Z`; the template fix
does not touch this test). Clients hold a Datastar stream; the server pushes
an HTML fragment to all of them every 0.5 s:

| Target | 1,000 clients p50 / p99 | 5,000 clients p50 / p99 | CPU at 5k | RSS at 5k |
| --- | --- | --- | ---: | ---: |
| **stario** | 8.7 / 31 ms | 54 / 144 ms | 0.19 | 134 MB |
| sanic | 10.7 / 27 ms | 55 / 119 ms | 0.18 | 111 MB |
| blacksheep-granian | 14.0 / 24 ms | 72 / 146 ms | 0.19 | 275 MB |
| falcon | 9.9 / 22 ms | 71 / 187 ms | 0.24 | 136 MB |
| fastapi | 13.5 / 32 ms | 74 / 334 ms | 0.34 | 227 MB |
| blacksheep-uvicorn | 12.8 / 27 ms | 80 / 156 ms | 0.28 | 138 MB |

Fan-out is where Stario is at par, not ahead: 10,000 small events/s is cheap
for any asyncio server, and part of the latency at 5k is the Python client.
Every target delivered ≥98% of events; the shortfall is the measurement
window edge.

## Two cores (unreleased branch)

Two workers on CPUs 0–1. Stario uses the free-threaded build with
`STARIO_THREADS=2`: two event loops in one process, with one shared
`Relay`. The others use two processes. Two caveats: there is no Stario
two-process (GIL) row, which would likely beat the free-threaded one because
3.14t costs ~15% per thread; and the competitor rows ran after the Stario
rows, not interleaved. The free-threaded model is shown because it keeps
in-process state such as `Relay` shared across cores.

Afternoon run. 5.7.0 is the released wheel; "branch" is
`claude/inspiring-davinci-ydskh0` (immortal shared markup objects), built
from source; neither is in a release yet.

| Target | plaintext | page | mix | p99 page |
| --- | ---: | ---: | ---: | ---: |
| stario-ft × 2, branch (unreleased) | 133,681 | 10,617 | 21,561 | 21 ms |
| stario-ft × 1, branch (unreleased) | 73,194 | 6,270 | 12,591 | 32 ms |
| stario-ft × 2, 5.7.0 | 142,381 | 5,856 | 15,069 | 33 ms |
| stario-ft × 1, 5.7.0 | 68,964 | 6,210 | 12,332 | 33–58 ms |
| blacksheep-granian × 2 | 75,537 | 5,714 | 11,672 | 36 ms |
| robyn × 2 | 65,435 | 5,879 | 11,667 | 37 ms |
| sanic × 2 | 49,702 | 5,883 | 11,291 | 36 ms |
| fastapi × 2 | 26,262 | 5,152 | 8,879 | 46 ms |

These competitor rows used the old Jinja2 template (`r.id`), so their page
and mix numbers are understated. With the corrected template, expect two
Jinja2 workers near 2 × 5k page req/s, about level with the branch.
Scaling from one thread to two:

| | plaintext | page | mix |
| --- | ---: | ---: | ---: |
| 5.7.0 | 2.06x | **0.94x** | 1.22x |
| branch | 1.83x | **1.69x** | 1.71x |

Released 5.7.0 doesn't scale HTML work across threads at all. The branch
does, up to a ceiling set by this benchmark: `/page` reads module-level row
dicts from both threads, and plain Python over shared dicts scales only
1.13x on 3.14t. In-process with per-thread data, build+render on the branch
scales 1.81x (5.7.0: 0.33x). The single-core GIL throughput is the same on
both builds within noise, and both render byte-identical pages.

## HTML generation, no server

`html/compare.py`, best-of-7 µs per page, CPU-pinned. Every library renders
the same product list, checked to be the same document.

| Library | 10 rows | 50 rows | 50 rows, escape-heavy | 500 rows | vs `@baked` (50) |
| --- | ---: | ---: | ---: | ---: | ---: |
| **stario `@baked`** | **3.9** | **17.7** | **20.0** | **168** | 1.0x |
| f-string + `html.escape` | 4.3 | 20.6 | 29.4 | 201 | 1.2x |
| f-string + markupsafe | 7.7 | 35.6 | 38.7 | 366 | 2.0x |
| **stario plain tree** | **9.2** | **40.7** | **39.5** | **373** | 2.3x |
| jinja2 | 14.2 | 48.9 | 54.6 | 441 | 2.8x |
| mako | 15.9 | 59.1 | 62.1 | 648 | 3.3x |
| tdom, fastcore.xml, htpy, dominate | 210–233 | 919–1,045 | 908–1,067 | 8,840–10,802 | 52–59x |

- `@baked` is the fastest way to produce this HTML in Python that we found.
  It beats hand-written f-strings, which have no structure or reuse, by
  15–30%.
- The plain tree, where every element is a real object, is 1.2–1.4x
  Jinja2 and level with f-strings plus markupsafe.
- The pure-Python element libraries are about 23x slower than the plain
  tree. htpy was cross-checked outside the harness on 3.13 and 3.14: about
  940 µs for 50 rows, almost all of it in `str()`.

## Where Stario is weak, and angles to improve

Ordered by expected impact.

1. **HTML building does not scale on free-threaded Python in 5.7.0.** On
   5.7.0, two threads building trees from per-thread data reach 0.33x of one
   thread together, while plain Python scales 1.89x. The cause is
   reference-count contention on shared objects: every `Element` referenced
   its `Tag`, render increfed each Tag's cached strings, and Cython increfed
   the module-level cache dicts on every `classes()` call. Branch
   `claude/inspiring-davinci-ydskh0` makes those objects immortal on
   free-threaded builds. Measured here, on the branch:
   - in-process build+render: 0.33x → 1.81x on two threads
   - server `/page`: 0.94x → 1.69x
   - mix: 1.22x → 1.71x
   - no regression on the GIL build

   Two limits remain. A module-level `Element` reused by every request
   still contends; a parameterless `@baked` fragment does not. Shared
   plain-Python data is CPython's limit, not Stario's. Still to do: add a
   two-thread scaling case to `html/micro.py`.
2. **Tail latency at 1,024+ connections.** With uvloop, a minority of
   connections starve for seconds (p99 2.4 s, ~100 `wrk` timeouts per run)
   while p50 stays low. The same Stario on stdlib asyncio is fair at 1,024
   connections (p99 43 ms, no timeouts) but has half the throughput. Uvicorn
   on uvloop starves the same way, and Granian and Robyn keep a fair tail.
   So this is libuv's scheduling, but Stario inherits it. Angles:
   - Reproduce with a minimal uvloop echo server and report upstream.
   - Cap the work per readable event in `HttpProtocol`.
   - Check whether `pause_reading`/`resume_reading` during an eager handler
     ever leaves a socket unpolled.
3. **`/page` p99 at 128–256 connections varies run to run**: from 30 ms to
   550 ms on single 10 s runs (now reported as the median of 3). It is not
   the cyclic GC (same with `gc.disable()`) and not VM steal. Probably item
   2 at a smaller scale.
4. **Tree building is most of a plain-tree page.** About 90 µs of ~105 µs is
   building `Element` objects, and `@baked` gives 1.58x end to end. Angles:
   - Steer the docs toward `@baked` for list and table components.
   - Cache static subtrees automatically.
5. **Brotli for dynamic responses is costly.** Safari sends no `zstd`, so it
   gets brotli level 4: 4,607 req/s against 6,708 with zstd, for 15% fewer
   bytes. Benchmark brotli levels 2–3 as the dynamic default, and keep high
   levels for precompressed `Files`.
6. **Free-threaded single-thread cost**: 3.14t is 11–18% slower on
   plaintext and 16–23% slower on `/page` than the GIL build. The
   immortalization branch does not change it on one thread, so this part is
   CPython itself.
7. **Small-response throughput is syscall-bound.** Stario's own cost is
   ~1.35 µs of the ~12 µs per plaintext request, so more Python-level
   micro-optimization has little left to give. The remaining wins are fewer
   syscalls per request and more cores (item 1).
8. **Memory under 10,000 active connections** peaked at 466 MB, against
   126–411 MB for the others, while serving 4.5k pages/s. The glibc trim
   threshold Stario sets (64 MiB per arena) keeps freed memory. A clean
   idle-connection memory test would show whether per-connection buffers can
   be smaller.

## Reproduce

```bash
python3 benchmarks/server/bench.py --phases throughput,sweep,mixed,compress,conns
python3 benchmarks/server/bench.py --phases sse
python3 benchmarks/server/bench.py --phases multicore                      # needs a 3.14t env
python3 benchmarks/server/bench.py stario fastapi blacksheep-uvicorn blacksheep-granian \
  --phases compress --gzip-level 1
for rows in 10 50 500; do
  uv run --python 3.14 --with dominate --with htpy --with jinja2 --with tdom --with mako \
    --with fastcore --with markupsafe benchmarks/html/compare.py --rows $rows --escape
done
```

This run installed Stario from the 5.7.0 wheels (`STARIO_SPEC` /
`STARIO_FT_SPEC`). The branch build in the multi-core section was compiled
from source with `STARIO_ALLOW_OLD_BROTLI=1`, for local benchmarking only.
