# Benchmark results — Stario 5.7.0, November 2026

One full run of `server/bench.py` and `html/compare.py`, what it says about
Stario against other Python frameworks, and where Stario can get better.
How the suites work and how to rerun them: [`README.md`](README.md).

**Machine:** 4 vCPU Intel Xeon @ 2.10 GHz cloud VM, 15 GB RAM, Linux 6.18,
CPython 3.14.6 (3.14.6t for the free-threaded run). Each server is pinned to
one CPU; `wrk` and the helper clients get two other CPUs. Every number is the
median of 3 × 10 s runs. Run-to-run spread on this VM is about ±10% on
throughput and much larger on p99. Treat a gap under 15% as a tie.

**Versions:** stario 5.7.0 (the PyPI wheel, byte-identical Python sources to
this checkout) · fastapi 0.162.2 / starlette 1.10.0 / uvicorn 0.54.0 /
pydantic 2.14.3 · blacksheep 2.6.4 · granian 2.8.7 · sanic 25.12.1 ·
falcon 4.4.0 · robyn 0.88.0 · uvloop 0.23.0 · jinja2 3.1.6 · mako 1.4.3 ·
htpy 26.5.1 · dominate 3.1.0 · fastcore 2.2.39 · tdom 0.1.20.

## The short version

| Question | Stario | Best other | Typical other |
| --- | --- | --- | --- |
| Small JSON / plaintext, one core | 63–78k req/s | 45–49k (BlackSheep on Granian) | 10–23k |
| A real 16 KB HTML page, one core | 6.7k req/s (9.5k with `@baked`) | 2.5k (Falcon, Robyn) | 2.1–2.5k |
| Browser-shaped mix, one core | 13.3k req/s, p99 17 ms | 5.1k (BlackSheep/Uvicorn), p99 1.2 s | 3.7–4.8k |
| Same mix with compression on | 11.9k req/s, 792 B/req | 3.9k, 886 B/req | 2.6–3.2k |
| 10,000 users, one page view each per 2 s, one core | **all served**: p50 3.4 ms, p99 86 ms, 0 errors | none keep up (1.1–2.5k req/s, seconds of latency, thousands of errors) | |
| SSE fan-out to 5,000 clients | p50 54 ms, 0.19 CPU, 134 MB | on par (Sanic 55 ms, 0.18 CPU, 111 MB) | 70–80 ms |
| Template rendering (50-row page, no server) | 17 µs (`@baked`), 44 µs (naive tree) | 61 µs (mako) | 99 µs (jinja2), ~1 ms (htpy, dominate, fastcore, tdom) |
| 2 cores (free-threaded vs multi-process) | 5.7.0: page doesn't scale (0.94x). Immortalization branch: page 10.6k, mix 21.6k | page 5.9k, mix 11.7k (Granian, Robyn) | branch: 1.8x the best two-process setup |

On one core, Stario is **3–7x faster than FastAPI** and 1.4–2.7x faster
than the fastest other combination on every endpoint. On HTML pages, the
thing a hypermedia app actually serves, it is 2.6x faster than every other
framework, and up to 3.8x with `@baked`. Its weak spots: tail latency at
1,024+ concurrent connections (a uvloop scheduling issue it shares with
Uvicorn), and HTML building on free-threaded Python, which does not scale
across threads in 5.7.0. A branch fixes that: two-thread page throughput goes
from 0.94x to 1.69x. Both are covered under "Where Stario is weak".

## What changed in the benchmarks, and why

The previous `server/run.sh` and `html/compare.py` gave useful signal but had
several apples-to-oranges problems. This run fixes them:

1. **No CPU budget.** Servers and `wrk` shared all four CPUs, and Granian and
   Robyn run Rust I/O threads, so they could use more than one core while the
   "single-worker" Python servers used one. Now each server is pinned to one
   CPU, including its helper threads, and the load generator is pinned to
   others. The report includes server CPU; every target sits at 0.98–1.00.
2. **Robyn never ran its handlers.** `const=True` caches the first response in
   Rust. It is now off (`BENCH_ROBYN_CONST=1` restores it); Robyn's plaintext
   dropped from "cached" to 30k req/s.
3. **Hello-world only.** No endpoint rendered HTML, which is what a Stario
   app spends its time on. There is now a realistic `/page`: head, nav, a
   50-row × 6-column table with escaping, footer, ~16 KB. Every other target
   renders it with Jinja2. All outputs are checked to be the same document
   before timing.
4. **No correctness or error accounting.** Responses were never checked, and
   errors and non-2xx went unreported. Now every endpoint is validated first,
   and socket errors and non-2xx are printed beside each number. This caught
   a real difference: `wrk.format()` with custom headers drops `Host`, Stario
   answers that with 400 (as RFC 9112 requires), and every other server
   silently accepts it.
5. **Throughput only.** Now: p50/p99, a concurrency sweep, a browser-shaped
   mix, compression with bytes on the wire, SSE fan-out, many connected
   users, and multi-core.
6. **Defaults that cap concurrency.** Granian's default backpressure limits a
   worker to 1,024 concurrent connections (only 1,025 of 5,000 SSE clients
   connected), and Sanic's default listen backlog is 100. Both now use the
   2048 backlog that Stario and Uvicorn use, with Granian's cap lifted.
7. **HTML compare** added mako, FastHTML's `fastcore.xml`, and a hand-written
   f-string + markupsafe floor. It also adds an escape-heavy mode, since
   clean ASCII never exercises the escaper, and runs at 10/50/500 rows.

Remaining asymmetries, kept on purpose: FastAPI validates with Pydantic,
as its users would; Robyn's dicts go through its native orjson; Stario's
`/page` is the naive `h.*` tree rather than `@baked`, the conservative
choice.

## Server: one core

Requests/s at 128 connections, median of 3. The last column is the geometric
mean of the per-endpoint ratio to Stario.

| Target | plaintext | json | params | validate | page | vs stario |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **stario** | **77,601** | **75,381** | **62,888** | **53,346** | **6,666** | 1.00x |
| blacksheep-granian | 48,515 | 48,115 | 45,260 | 21,786 | 2,082 | 0.52x |
| robyn | 29,946 | 31,477 | 18,546 | 17,422 | 2,502 | 0.36x |
| blacksheep-uvicorn | 23,053 | 22,660 | 22,339 | 19,946 | 2,409 | 0.34x |
| falcon | 21,391 | 19,660 | 21,007 | 18,068 | 2,515 | 0.31x |
| sanic | 18,680 | 17,150 | 17,200 | 14,406 | 2,280 | 0.27x |
| fastapi | 11,505 | 10,773 | 9,857 | 8,581 | 2,205 | 0.18x |

Stario `/page-baked` (the same bytes via `@baked`): **9,513 req/s**,
1.43x its own `/page` and 3.8x the best Jinja2-backed server.

p99 latency (ms) at the same load:

| Target | plaintext | json | params | validate | page |
| --- | ---: | ---: | ---: | ---: | ---: |
| **stario** | **4.6** | **3.8** | **4.9** | **4.8** | **30** |
| blacksheep-granian | 5.2 | 4.6 | 5.2 | 15.6 | 98 |
| robyn | 6.3 | 6.1 | 11.0 | 11.3 | 78 |
| blacksheep-uvicorn | 11.9 | 10.1 | 9.5 | 10.6 | 89 |
| falcon | 9.2 | 10.2 | 9.6 | 10.2 | 2,140 |
| sanic | 12.6 | 11.5 | 9.9 | 25.5 | 2,330 |
| fastapi | 23.1 | 32.0 | 93.0 | 26.2 | 1,210 |

Response overhead is similar everywhere: a plaintext response is 130 B from
Stario, 147 B from Uvicorn and Granian, and 115–117 B from Robyn and Sanic.

**Why the gap is biggest on `/page`:** every other target spends about 295 µs
in Jinja2 per page. Stario builds the tree in ~90 µs and renders it in
~15 µs, and `@baked` brings the whole page to ~45 µs. On small responses,
the gap comes from the Cython HTTP core: the in-process benchmark
(`server/inproc.py`) puts Stario's own cost at 1.35 µs per plaintext
request, out of ~13 µs per request over a socket. The other ~90% is the
event loop and syscalls.

## Server: the concurrency sweep (`/page`, req/s · p99 ms)

| Target | c=8 | c=64 | c=256 | c=1024 |
| --- | ---: | ---: | ---: | ---: |
| **stario** | **6,716 · 3.0** | **6,582 · 20** | **6,579 · 74** | **6,368 · 2,280 (105 err)** |
| blacksheep-granian | 2,070 · 8.8 | 2,148 · 53 | 1,986 · 200 | 2,045 · 822 |
| robyn | 2,683 · 5.6 | 2,598 · 45 | 2,520 · 172 | 2,465 · 1,500 |
| falcon | 2,447 · 9.0 | 2,571 · 242 | 2,391 · 3,170 (69 err) | 2,386 · 1,720 (63 err) |
| blacksheep-uvicorn | 2,450 · 8.7 | 2,546 · 429 | 2,396 · 3,200 (62 err) | 2,443 · 1,750 (64 err) |
| sanic | 2,109 · 9.5 | 2,283 · 554 | 2,151 · 3,130 (67 err) | 2,076 · 1,510 (59 err) |
| fastapi | 2,185 · 8.8 | 2,248 · 324 | 2,131 · 3,220 (61 err) | 2,266 · 1,800 (68 err) |

Errors are `wrk` 5 s timeouts. Up to 256 connections, Stario has the lowest
latency of any target at 3x the throughput, and it is the only
asyncio-based server whose p99 stays bounded there. At 1,024 connections
the Rust-I/O servers (Granian, Robyn) keep their tail fair. Stario's p50
stays at 40 ms, but a minority of connections starve for seconds. See
"Where Stario is weak", item 1.

## Server: the day-to-day mix

`mixed.lua` sends 40% `/page`, 30% `/user/{id}`, 20% `/json`, and 10%
`POST /validate`. Every request carries a real Chrome header set (15
headers, cookies, `Accept-Encoding: gzip, deflate, br, zstd`).

| Target | req/s | p50 ms | p99 ms | req/s, compression on | bytes/req, compression on |
| --- | ---: | ---: | ---: | ---: | ---: |
| **stario** | **13,298** | **9.1** | **16.9** | **11,933** | **792** |
| blacksheep-uvicorn | 5,116 | 21.8 | 1,190 | 3,858 | 886 |
| falcon | 4,814 | 24.3 | 1,480 | — | — |
| robyn | 4,762 | 25.8 | 284 | — | — |
| blacksheep-granian | 4,309 | 24.6 | 90 | 3,163 | 885 |
| sanic | 4,127 | 26.8 | 549 | — | — |
| fastapi | 3,685 | 33.1 | 511 | 2,581 | 837 |

Sanic, Falcon, and Robyn have no built-in response compression. Real
deployments put them behind a proxy that compresses.

## Compression (`/page`, 16 KB of HTML)

Each framework with built-in compression, at its default settings:

| Target | Accept-Encoding | Coding chosen | Body | req/s | p99 ms |
| --- | --- | --- | ---: | ---: | ---: |
| **stario** | browser | **zstd (level 3)** | 1,609 B | **6,119** | 42 |
| stario | `gzip` only | gzip (level 1) | 2,102 B | 5,861 | 42 |
| stario | `br` only (Safari) | brotli (level 4) | 1,362 B | 3,914 | 65 |
| fastapi | browser | gzip (level 9) | 1,679 B | 1,502 | 3,560 |
| blacksheep-uvicorn | browser | gzip (level 5) | 1,802 B | 1,935 | 783 |
| blacksheep-granian | browser | gzip (level 5) | 1,802 B | 1,667 | 123 |

The same comparison with gzip forced to level 1 everywhere
(`--gzip-level 1`): Stario 4,647 req/s with a 2,102 B body against
1,745–1,890 req/s and 2,126 B for the others. The codec is the same zlib, so
the 2.5x gap is all framework cost. With compression on, Stario loses only
8% of its uncompressed `/page` throughput with zstd; the others lose 20–32%
to gzip.

## Day-to-day capacity: how many users?

**Connected users.** N keep-alive clients each fetch `/page` every 2 s, a
very active tab. All on one core:

| Target | 2,000 users (1k req/s offered) | 10,000 users (5k req/s offered) |
| --- | --- | --- |
| **stario** | p50 0.5 ms, p99 1.6 ms | **4,462 req/s, p50 3.4 ms, p99 86 ms, 0 errors** |
| robyn | p50 1.2 ms, p99 6.9 ms | 2,507 req/s, p50 2.6 s, 6,030 errors |
| blacksheep-granian | p50 2.5 ms, p99 95 ms | 2,023 req/s, p50 4.7 s |
| falcon | p50 1.3 ms, p99 87 ms | 1,971 req/s, 4,180 errors |
| sanic | p50 1.5 ms, p99 54 ms | 1,873 req/s, 3,780 errors |
| blacksheep-uvicorn | p50 1.4 ms, p99 131 ms | 1,567 req/s, 2,199 errors |
| fastapi | p50 1.6 ms, p99 49 ms | 1,125 req/s, p50 1.4 s, 2,467 errors |

At 2,000 users everyone is fine. At 10,000, Stario is the only target that
keeps up on one core. Achieved req/s includes the connect ramp, so ~90% of
offered counts as a full pass.

As a rule of thumb from the mix and `/page` numbers: one Stario core
sustains about 6,500 HTML page views/s, or 13,000 mixed requests/s,
before latency climbs. That is 2.6–3.6x what the same core gives FastAPI,
Sanic, Falcon, BlackSheep, or Robyn on the same page. If an active user
loads a page every 30 s, that is ~200k concurrently active users per core
for pages alone. Real handlers add a database round-trip that shrinks every
framework's number; the ratio is what carries over.

**SSE fan-out.** Clients hold a Datastar stream; the server pushes an HTML
fragment to all of them every 0.5 s:

| Target | 1,000 clients p50 / p99 | 5,000 clients p50 / p99 | CPU at 5k | RSS at 5k |
| --- | --- | --- | ---: | ---: |
| **stario** | 8.7 / 31 ms | 54 / 144 ms | 0.19 | 134 MB |
| sanic | 10.7 / 27 ms | 55 / 119 ms | 0.18 | 111 MB |
| blacksheep-granian | 14.0 / 24 ms | 72 / 146 ms | 0.19 | 275 MB |
| falcon | 9.9 / 22 ms | 71 / 187 ms | 0.24 | 136 MB |
| fastapi | 13.5 / 32 ms | 74 / 334 ms | 0.34 | 227 MB |
| blacksheep-uvicorn | 12.8 / 27 ms | 80 / 156 ms | 0.28 | 138 MB |

Every target delivered ≥98% of events; the shortfall is the measurement
window edge. Fan-out is where Stario is *at par*, not ahead: 10,000 events/s
of small fragments is cheap for any asyncio server, and part of the 5k
latency is the Python client itself. At 0.19 CPU for 5,000 clients × 2
events/s, a core would carry roughly 25,000 such clients (extrapolated, not
measured). Robyn is left out because its SSE model doesn't map onto this
broadcast.

## Two cores

Two workers on CPUs 0–1. Stario uses the free-threaded build with
`STARIO_THREADS=2` (two event loops, one process, one shared `Relay`); the
others use two processes.

**Update (same day, afternoon):** branch `claude/inspiring-davinci-ydskh0`
makes shared markup objects immortal on free-threaded builds (Tags, their
cached strings, the `classes()` / attribute / `data()` caches, `@baked`
static parts) and stops render from increfing the tree it walks. It was
measured against 5.7.0 back to back on the same machine, swapping only the
wheel. The machine ran ~15% faster in the afternoon than in the morning run
above, so compare within this table only.

| Target | plaintext | page | mix | p99 page |
| --- | ---: | ---: | ---: | ---: |
| **stario-ft × 2, branch** | **133,681** | **10,617** | **21,561** | **21 ms** |
| stario-ft × 1, branch | 73,194 | 6,270 | 12,591 | 32 ms |
| stario-ft × 2, 5.7.0 | 142,381 | 5,856 | 15,069 | 33 ms |
| stario-ft × 1, 5.7.0 | 68,964 | 6,210 | 12,332 | 33–58 ms |
| blacksheep-granian × 2 | 75,537 | 5,714 | 11,672 | 36 ms |
| robyn × 2 | 65,435 | 5,879 | 11,667 | 37 ms |
| sanic × 2 | 49,702 | 5,883 | 11,291 | 36 ms |
| fastapi × 2 | 26,262 | 5,152 | 8,879 | 46 ms |

5.7.0 rows are the mean of two runs (before and after the branch run; they
agree within 4%). Scaling from one thread to two:

| | plaintext | page | mix |
| --- | ---: | ---: | ---: |
| 5.7.0 | 2.06x | **0.94x** | 1.22x |
| branch | 1.83x | **1.69x** | 1.71x |

With the branch, two Stario threads serve 1.8x the HTML pages and 1.85x the
mix of the best two-process setup (Granian, Robyn, Sanic), in one process
with one shared `Relay`. The plaintext dip (134k vs 142k) is within this
VM's noise. The markup change does not touch that path, and two `wrk`
threads are close to their limit at that rate.

Why page scales 1.69x and not ~1.9x: `/page` reads the module-level `ROWS`
dicts and their strings from both threads. On 3.14t, plain Python over
shared dicts barely scales (1.13x in-process, no Stario involved), so this
benchmark caps the result, not Stario. In-process with per-thread data,
build+render on the branch scales 1.81x (5.7.0: 0.33x), and an
attribute-heavy tree 2.21x (5.7.0: 0.55x). An app that loads its rows per
request, from a database, gets the per-thread case.

The original morning run, 5.7.0 only:

| Target | plaintext | page | mix | p99 page |
| --- | ---: | ---: | ---: | ---: |
| stario-ft × 2 | 136,121 | 5,468 | 13,168 | 44 ms |
| stario-ft × 1 | 69,377 | 5,159 | 9,935 | 63 ms |
| blacksheep-granian × 2 | 72,186 | 4,458 | 10,103 | 49 ms |
| sanic × 2 | 44,379 | 5,087 | 9,944 | 57 ms |
| robyn × 2 | 60,404 | 4,875 | 8,567 | 63 ms |
| fastapi × 2 | 24,520 | 4,256 | 7,364 | 64 ms |

**Single-core regression check (GIL 3.14, throughput phase, same afternoon):**

| | plaintext | json | params | validate | page | page-baked |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 5.7.0 | 84,502 | 74,464 | 63,470 | 46,561 | 7,389 | 11,182 |
| branch | 85,774 | 71,752 | 66,465 | 47,900 | 7,533 | 12,244 |
| change | +1.5% | −3.6% | +4.7% | +2.9% | +1.9% | +9.5% |

All within noise except, possibly, `@baked` (+9.5%), which is consistent
with render no longer increfing. Both builds render byte-identical pages.
The free-threaded single-thread cost against the GIL build is now 15–17%
on both 5.7.0 and the branch.

## HTML generation, no server

`html/compare.py`, best-of-7 µs per page, CPU-pinned. The same product
list is rendered by every library and checked to be the same document.

| Library | 10 rows | 50 rows | 50 rows, escape-heavy | 500 rows | vs stario `@baked` (50) |
| --- | ---: | ---: | ---: | ---: | ---: |
| **stario `@baked`** | **4.3** | **17.1** | **19.7** | **202** | 1.0x |
| f-string + markupsafe (no library) | 7.1 | 34.5 | 41.3 | 379 | 2.0x |
| **stario naive tree** | **9.2** | **44.3** | **40.9** | **451** | 2.6x |
| mako | 15.7 | 61.2 | 79.3 | 749 | 3.6x |
| jinja2 | 25.5 | 99.2 | 132.4 | 972 | 5.8x |
| tdom (components) | 216 | 936 | 1,049 | 9,251 | 55x |
| fastcore.xml (FastHTML) | 229 | 1,092 | 1,006 | 10,404 | 64x |
| dominate | 264 | 1,063 | 1,219 | 11,405 | 62x |
| htpy | 248 | 1,152 | 1,108 | 10,928 | 67x |

- `@baked` is 2x faster than hand-written f-strings with markupsafe. The
  naive tree, where every element is a real object, is within 30% of them
  (and level with them on escape-heavy text).
- Against template engines: `@baked` is 3.6x mako and 5.8x Jinja2. The
  naive tree, which also builds the structure per call, still beats both.
- Against other builders, the pure-Python element-tree libraries (htpy,
  dominate, FastHTML's FT, tdom) are 21–26x slower than Stario's naive
  tree. htpy was cross-checked outside the harness on 3.13 and 3.14:
  ~940 µs for 50 rows, almost all of it in `str()`.
- Escape-heavy text costs Stario's `@baked` 15% and Jinja2 33%.

## Where Stario is weak, and angles to improve

Ordered by expected impact.

1. **HTML building did not scale on free-threaded Python. Fixed on branch
   `claude/inspiring-davinci-ydskh0`.** On 5.7.0, `h.*` tree building
   *anti-scales*: two threads with per-thread data together reach 0.33x of
   one thread, while plain Python scales 1.89x. The cause was reference-count
   contention on shared objects: every `Element` referenced its `Tag`,
   render increfed each Tag's cached strings, and Cython increfed the
   module-level cache dicts on every `classes()` call. The branch makes
   those objects immortal on free-threaded builds and reads them borrowed
   in render. Measured here: in-process build+render 0.33x → 1.81x on two
   threads; server `/page` 0.94x → 1.69x (capped by this benchmark's shared
   module-level data, see "Two cores"); mix 1.22x → 1.71x. No regression on
   the GIL build. Remaining limits:
   - A module-level `Element`, `SafeString`, or `Attrs` reused by every
     request still has its refcount written on each use (~0.7x on two
     threads, per the branch author). This benchmark's `/page` has no such
     constants (nav links are built per call), so it is not affected, but
     the docs should steer users toward parameterless `@baked` fragments
     for shared constants.
   - Shared plain-Python data (module-level dicts and lists read by every
     request) is CPython's limit, not Stario's.
   - Still to do: add a two-thread scaling case to `html/micro.py`.
2. **Tail latency at 1,024+ connections.** With uvloop, a minority of
   connections starve for seconds (p99 2–3 s, ~100 `wrk` timeouts per run)
   while p50 stays low. The same Stario on stdlib asyncio is perfectly fair
   at 1,024 connections (p99 43 ms, no timeouts) but has half the
   throughput. Uvicorn on uvloop shows the same starvation. So this is
   libuv's read/accept scheduling, not Stario's protocol, but Stario
   inherits it. Granian and Robyn keep a fair tail at the same load. Angles:
   reproduce with a minimal uvloop echo server and report upstream. Try
   capping work per readable event in `HttpProtocol` (e.g. yield after N
   pipelined requests). Check whether `pause_reading`/`resume_reading`
   during an eager handler ever leaves a socket unpolled.
3. **`/page` p99 at 128 connections is bimodal between runs**: 30 ms in
   some 10 s runs, 200–550 ms in others. It is not the cyclic GC (same with
   `gc.disable()`) and not VM steal. Likely the same scheduling unfairness
   as item 2 at a smaller scale, made visible by 16 KB responses. Worth a
   look alongside it.
4. **Tree building is ~80% of a naive page.** About 90 µs of the 112 µs is
   building `Element` objects, and `@baked` gives 1.43x end to end (2.4x
   in-process). Angles: steer the docs toward `@baked` for any list or
   table component, maybe with a debug-mode hint when a handler builds
   more than N elements per request. Explore caching static subtrees
   automatically, since a subtree with only constant children is the same
   object on every call.
5. **Brotli for dynamic responses is costly.** Safari sends no `zstd`, so
   Safari users get brotli level 4: 3,914 req/s against 6,119 with zstd,
   for 15% fewer bytes. Brotli levels 2–3 are usually much cheaper for
   similar ratios on 16 KB of HTML. Benchmark them as the dynamic default
   (keep high levels for precompressed `Files`).
6. **Free-threaded single-thread cost**: 3.14t is 11–18% slower on
   plaintext and 16–23% slower on `/page` than the GIL build (morning and
   afternoon runs). The immortalization branch does not change it on one
   thread (−17% on page, either build), so this part is CPython itself.
7. **Small-response throughput is syscall-bound.** Stario's own cost is
   ~1.35 µs of the ~13 µs per plaintext request, so more Python-level
   micro-optimization of the request path has little left to give. The
   remaining wins are fewer syscalls per request (batching writes across
   pipelined requests) and using more cores (item 1).
8. **Memory under 10,000 active connections** grew from 163 MB to 475 MB
   while serving 4.5k pages/s. The glibc trim threshold Stario sets (64 MiB
   per arena) keeps freed memory, and no other target served that load,
   so there is no direct comparison. A clean idle-connection memory test
   (connect, one request, measure RSS per connection, before any other
   phase) would show whether per-connection buffers can be smaller.

## Reproduce

```bash
python3 benchmarks/server/bench.py                                        # ~1 h, everything
python3 benchmarks/server/bench.py --phases multicore                      # needs a 3.14t env
python3 benchmarks/server/bench.py stario fastapi blacksheep-uvicorn blacksheep-granian \
  --phases compress --gzip-level 1
for rows in 10 50 500; do
  uv run --python 3.14 --with dominate --with htpy --with jinja2 --with tdom --with mako \
    --with fastcore --with markupsafe benchmarks/html/compare.py --rows $rows --escape
done
```

This run installed Stario from the 5.7.0 wheels (`STARIO_SPEC` /
`STARIO_FT_SPEC`), because the container could not fetch the Brotli source
that a checkout build needs.
