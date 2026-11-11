# Benchmark results — Stario 5.8.2, November 2026

One full run of `server/bench.py` against Stario 5.8.2 from the PyPI wheels,
what it says about Stario against other Python frameworks, and where Stario
can get better. How the suite works and how to rerun it:
[`README.md`](README.md). Raw data: `server/results/20261111T130305Z`
(`results.json` keeps every individual `wrk` run).

**Machine:** 4 vCPU Intel Xeon @ 2.10 GHz cloud VM (KVM), 15 GB RAM,
Linux 6.18, CPython 3.14.6 (3.14.6t for the free-threaded run). Each server
is pinned to CPU 0, `wrk` and the helper clients to CPUs 2–3, and every
other process on the machine, the harness included, is moved to CPU 1
(`--isolate`). The two-core phase uses CPUs 0–1 for the server.

**Versions:** stario 5.8.2 (PyPI wheels, cp314 and cp314t) · fastapi 0.162.4
/ starlette 1.10.0 / uvicorn 0.54.0 / pydantic 2.14.3 · blacksheep 2.6.4 ·
granian 2.8.7 · sanic 25.12.1 · falcon 4.4.0 · robyn 0.88.0 · uvloop 0.23.0 ·
orjson 3.13.0 · msgspec 0.22.0 · jinja2 3.1.6.

## How this run differs from the previous one

**Every app follows its framework's documented approach.** Before this run,
each competitor app was rewritten to use the fast path that framework's docs
recommend, instead of one generic pattern copied across all of them (table in
[`README.md`](README.md#fairness-rules)):

| Framework | JSON out | `POST /validate` | `/page` | SSE |
| --- | --- | --- | --- | --- |
| FastAPI | Pydantic return types, serialized in Rust (`ORJSONResponse` is deprecated) | Pydantic model with `Field` limits, 422 | `Jinja2Templates.TemplateResponse` | `fastapi.sse.EventSourceResponse` |
| BlackSheep | `json_settings.use(orjson)` | `FromJSON[Model]` (Pydantic), 400 | `view()` with its Jinja2 renderer | `TextServerSentEvent` generator |
| Sanic | `Sanic(dumps=orjson.dumps)` | checked by hand, 400 (Sanic's own validation needs sanic-ext, which also switches on CORS and OpenAPI) | Jinja2 | `response.send` stream |
| Falcon | msgspec `JSONHandler` | msgspec `Struct`, 422 | Jinja2 | `resp.sse` with `SSEvent` |
| Robyn | return a dict (orjson in Rust) | Pydantic body, 422 | `JinjaTemplate` | `SSEResponse` (new in this run) |
| Stario | `w.json` with orjson as its codec | parsed and checked by hand, 400 | `h.*` tree | `Relay` subscription + `SSE.patch_elements` |

Before the timed runs start, every app is checked against Stario's answers:
the same JSON on every endpoint, a 400 or 422 for an invalid body, and the
same page once the doctype spelling, whitespace between tags, and entity
escaping are normalized. FastAPI turns off its built-in telemetry, the counterpart
of `STARIO_TRACER=noop`. The Falcon app answered `/plaintext` with
`Content-Type: application/json` before this run; that bug is fixed.

**Run order and machine noise are controlled.** The previous file measured
each target in one sitting. This run:

- Runs **5 interleaved rounds** (3 for the SSE and connected-clients tests).
  Each round starts a fresh server for every target, and the target order
  rotates and reverses from round to round, so no framework always runs
  first, last, or right after the same neighbor. Every number below is the
  median across rounds, metric by metric. `wrk` errors are summed.
- Waits before every measurement until every CPU has been under 5% busy for
  a second (`/proc/stat`), and records the measurements that started without
  that.
- Records hypervisor steal on the server CPU and how busy the load
  generator's CPUs are during every `wrk` run.
- Runs a small stdlib-only speed probe (`server/probe.py`) on the server CPU
  before each target in each round, to show how much the VM itself drifts.

## Run quality

| Check | Result |
| --- | --- |
| `wrk` runs | 540, no failed targets |
| Hypervisor steal on the server CPU | median 0.7%, max 1.9% |
| Load-generator CPUs busy | median 6%, max 68% (never the bottleneck) |
| Server CPU during every `wrk` test | 0.98–0.99 (every target saturated its core) |
| Measurements started on a busy machine | 12 of ~600. All were BlackSheep's connected-clients tests, which reuse the server from its SSE test: BlackSheep was still at 12–20% CPU winding down 5,000 SSE streams. |
| Speed probe on the idle server CPU | 12.5k–26.2k units/s, median 19.4k |

**The VM is noisy, and it shows.** The speed probe varies by 70% on an idle
CPU with almost no reported steal: the host underneath is shared, and the
noise doesn't show up as steal. Throughput across rounds varies by 6–39%
((max − min) / median). On a host this noisy, a single 10 s run can be badly
off, and only rotating the order with medians over several rounds gives a
fair comparison. Two checks show that the noise doesn't change the
conclusions:

- When the host slowed down (round 3 of `/page`), every framework slowed
  together: Stario 4,460, FastAPI 1,966, Falcon 2,196, Sanic 2,489 req/s,
  all their lowest rounds.
- **Stario's slowest round beats every other framework's fastest round on
  every endpoint.** For example, `/plaintext`: Stario 64k at worst against
  46k at best for BlackSheep on Granian; `/page`: 4,460 against 3,384
  (Robyn's best).

Absolute numbers are 25–40% below the previous file's for every target,
Stario included (`stario-mp × 1` page: 5.6k now, 7.6k on Nov 9), because the
VM was slower on this day. **Compare numbers within this file, not with the
previous one.**

**Asymmetries that remain.** Pinning every server to one core favors
single-threaded event loops: Granian and Robyn run Rust I/O threads that
share that core with Python, and both scale super-linearly to two workers
(Robyn's page goes from 2.75k to 5.79k). Every target except Stario renders
`/page` with Jinja2, and Stario uses its own `h.*` builder; that difference
is the point of the comparison. The listen backlog is 2048 everywhere except
Robyn, which exposes no setting.

## The short version

| Question | Stario | Best other | FastAPI |
| --- | --- | --- | --- |
| Small responses, one core | 45–72k req/s | 17–46k (BlackSheep on Granian) | 6–9k |
| A 16 KB HTML page, one core | 5.4k req/s (8.4k with `@baked`) | 2.9k (BlackSheep on Uvicorn) | 2.2k |
| Browser-shaped mix, one core | 11.0k req/s, p99 18 ms | 5.4k (Falcon), p99 310 ms | 3.2k, p99 651 ms |
| Same mix, compression on | 10.0k req/s, 807 B/req | 3.4k, 898 B/req | 2.3k, 840 B/req |
| HTML page, two cores | 11.1k (2 processes), 8.2k (2 threads, free-threaded) | 5.8k (Robyn) | 4.8k |
| SSE fan-out to 5,000 clients | p50 60 ms, 0.20 CPU | level (Sanic: 61 ms, 0.20 CPU) | 3,114 of 5,000 connected, CPU saturated |
| Memory after start | 32 MB | 43 MB (Falcon) | 58 MB |

On one core, Stario serves 1.4–2.6x the fastest other framework on small
responses and 8x FastAPI. On the HTML page, the thing a hypermedia app
serves, it does 1.9x the next framework (2.9x with `@baked`). On the
browser-shaped mix it does 2.0x the next one, 3.0x with compression on. Its
weak spot is still tail latency at 256+ concurrent connections.

## Server: one core

Requests/s at 128 connections, median of 5 rounds. The last column is the
geometric mean of the per-endpoint ratio to Stario.

| Target | plaintext | json | params | validate | page | vs stario |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **stario** | **71,793** | **70,301** | **58,441** | **44,612** | **5,401** | 1.00x |
| blacksheep-granian | 45,563 | 42,563 | 40,782 | 17,325 | 2,563 | 0.55x |
| robyn | 23,592 | 23,357 | 14,466 | 12,960 | 2,750 | 0.33x |
| blacksheep-uvicorn | 17,723 | 18,609 | 17,877 | 12,794 | 2,878 | 0.31x |
| sanic | 16,689 | 15,811 | 16,517 | 13,098 | 2,840 | 0.30x |
| falcon | 16,936 | 14,889 | 15,008 | 13,230 | 2,627 | 0.28x |
| fastapi | 8,627 | 7,548 | 7,356 | 5,796 | 2,222 | 0.15x |

Stario `/page-baked` (the same bytes via `@baked`): **8,375 req/s**, 1.55x
its own `/page`.

p99 latency (ms) at the same load:

| Target | plaintext | json | params | validate | page |
| --- | ---: | ---: | ---: | ---: | ---: |
| **stario** | **3.7** | **3.6** | **4.3** | **4.9** | 91 |
| blacksheep-granian | 5.2 | 4.8 | 5.3 | 22.5 | 75 |
| robyn | 7.2 | 8.6 | 11.7 | 13.6 | **69** |
| blacksheep-uvicorn | 10.9 | 11.1 | 11.6 | 17.8 | 861 |
| sanic | 12.8 | 12.9 | 13.9 | 15.9 | 875 |
| falcon | 12.9 | 11.8 | 16.2 | 14.7 | 854 |
| fastapi | 105 | 28.4 | 126 | 147 | 814 |

- `validate` is where the documented approaches cost the most. Pydantic via
  `FromJSON` drops BlackSheep on Granian from 41k to 17k req/s; in the
  previous run, with hand checks, it went from 53k to 26k. Stario checks by
  hand, so its 2.6x lead on this endpoint is partly that difference.
- FastAPI is lower relative to the others than in the previous run (0.15x
  of Stario, was 0.23x). The documented approach runs every response
  through a Pydantic return model, and the p99 of 100+ ms on small
  responses suggests pauses on top of that.
- On `/page`, Stario has the highest throughput, but the Rust-I/O servers
  (Robyn, Granian) have the fairest tail: 69–75 ms against Stario's 91 ms,
  with half the throughput. The uvloop servers on Uvicorn, Sanic, and Falcon
  queue to 850 ms.
- A plaintext response is 130 B from Stario, 147 B from Uvicorn and
  Granian, and 115–117 B from Robyn and Sanic.

## Server: the concurrency sweep (`/page`, req/s · p99 ms)

| Target | c=8 | c=64 | c=256 | c=1024 |
| --- | ---: | ---: | ---: | ---: |
| **stario** | **5,598 · 3.8** | **5,304 · 20** | **4,833** · 1,640 | **5,005** · 237 (147 err) |
| blacksheep-granian | 2,567 · 6.7 | 2,616 · 38 | 2,466 · 147 | 2,545 · 560 |
| robyn | 2,728 · 4.7 | 2,477 · 36 | 2,887 · **137** | 2,678 · 800 |
| falcon | 2,830 · 7.2 | 2,844 · 41 | 2,685 · 2,850 (137 err) | 2,637 · 348 (99 err) |
| sanic | 2,952 · 7.1 | 2,622 · 38 | 2,627 · 2,830 (140 err) | 2,630 · 338 (100 err) |
| blacksheep-uvicorn | 2,638 · 7.3 | 2,782 · 138 | 2,876 · 2,650 (100 err) | 2,567 · 375 (80 err) |
| fastapi | 2,194 · 8.8 | 2,348 · 182 | 2,432 · 2,920 (194 err) | 2,291 · 405 (98 err) |

Errors are `wrk` 5 s timeouts, summed over 5 rounds. Up to 64 connections,
Stario has the lowest latency at about 2x the throughput. From 256
connections on, only the Rust-I/O servers (Granian, Robyn) finish every
request: on every uvloop server, Stario included, a minority of connections
wait seconds while the rest are served quickly. At 1,024 connections, `wrk`
reports p99 over the requests that finished, so the timeouts don't show in
the p99 column. See "Where Stario is weak", item 1.

## Server: the browser-shaped mix

`mixed.lua` sends 40% `/page`, 30% `/user/{id}`, 20% `/json`, and 10%
`POST /validate`. Every request carries a real Chrome header set (15
headers, cookies, `Accept-Encoding: gzip, deflate, br, zstd`).

| Target | req/s | p50 ms | p99 ms | req/s, compression on | bytes/req, compression on |
| --- | ---: | ---: | ---: | ---: | ---: |
| **stario** | **10,953** | **10.0** | **18.4** | **10,039** | **807** |
| falcon | 5,442 | 20.5 | 310 | — | — |
| blacksheep-uvicorn | 5,022 | 22.6 | 357 | 3,367 | 898 |
| sanic | 4,645 | 23.3 | 260 | — | — |
| robyn | 4,594 | 24.6 | 40.1 | — | — |
| blacksheep-granian | 4,500 | 28.1 | 76.0 | 3,388 | 898 |
| fastapi | 3,154 | 42.6 | 651 | 2,294 | 840 |

Stario serves 2.0x the next framework, with the lowest p99. With
compression on, the lead grows to 3.0x, because Stario's zstd is cheaper than
the others' default gzip (next section). Sanic, Falcon, and Robyn have no
built-in compression; real deployments put them behind a proxy that
compresses.

## Compression (`/page`, 16 KB of HTML)

Each framework with built-in compression, at its default settings:

| Target | Accept-Encoding | Coding chosen | Body | req/s | p99 ms |
| --- | --- | --- | ---: | ---: | ---: |
| **stario** | browser | **zstd** | 1,624 B | **4,486** | 265 |
| stario | `gzip` only | gzip (level 1) | 2,113 B | 4,325 | 50 |
| stario | `br` only (Safari) | brotli (level 4) | 1,379 B | 3,114 | 485 |
| blacksheep-uvicorn | browser | gzip | 1,810 B | 1,836 | 484 |
| blacksheep-granian | browser | gzip | 1,810 B | 1,779 | 109 |
| fastapi | browser | gzip | 1,688 B | 1,376 | 133 |

Compression costs Stario 17% of its uncompressed `/page` throughput (zstd),
and the others' default gzip costs them 30–38%. The p99 column is the
noisiest in this file: these are single-core runs at full load, and on this
host the page p99 moves by hundreds of milliseconds between rounds.

## Day-to-day capacity

**Connected clients.** N keep-alive connections each fetch `/page` every
2 s, which is much heavier per connection than a real user, who loads assets
once and then reads. Median of 3 rounds.

| Target | 2,000 connections (1k req/s offered) | 10,000 connections (5k req/s offered) |
| --- | --- | --- |
| **stario** | **p50 0.6 ms, p99 2.4 ms** | **4,422 req/s**, p50 119 ms, p99 2.8 s, **0 errors** |
| robyn | p50 1.1 ms, p99 5.6 ms | 2,697 req/s, p50 2.5 s, 5,234 errors |
| blacksheep-granian | p50 1.6 ms, p99 119 ms | 2,360 req/s, p50 3.9 s, 0 errors |
| falcon | p50 1.0 ms, p99 4.8 ms | 2,226 req/s, p50 1.9 s, 5,173 errors |
| sanic | p50 1.1 ms, p99 5.1 ms | 2,010 req/s, p50 272 ms, 3,051 errors |
| blacksheep-uvicorn | p50 1.7 ms, p99 265 ms | 1,834 req/s, p50 282 ms, 4,128 errors |
| fastapi | p50 1.4 ms, p99 6.7 ms | 1,712 req/s, p50 139 ms, 3,429 errors |

Achieved req/s includes the connect ramp, so about 90% of offered counts as
a full pass. On this day's slower VM, 5k pages/s is close to Stario's own
single-core `/page` capacity (5.4k), so Stario gets through at full rate
with no errors but queues (p50 119 ms). Every other framework tops out at
about half the offered load, with errors and reconnects or multi-second
waits. This restates the throughput result as latency under a fixed load;
it isn't separate evidence. The BlackSheep rows started with BlackSheep
still busy from its SSE test (see "Run quality").

**SSE fan-out.** Clients hold a Datastar stream, and the server pushes an
HTML fragment to all of them every 0.5 s. Median of 3 rounds.

| Target | 1,000 clients p50 / p99 | 5,000 clients p50 / p99 | delivered at 5k | CPU at 5k | RSS at 5k |
| --- | --- | --- | ---: | ---: | ---: |
| **stario** | **10.5 / 24 ms** | **60 / 174 ms** | 100% | **0.20** | 113 MB |
| sanic | 11.0 / 28 ms | 61 / 181 ms | 100% | 0.20 | 112 MB |
| falcon | 13.0 / 38 ms | 76 / 270 ms | 100% | 0.28 | 131 MB |
| blacksheep-granian | 14.3 / 38 ms | 89 / 188 ms | 100% | 0.23 | 230 MB |
| blacksheep-uvicorn | 16.1 / 37 ms | 113 / 327 ms | 100% | 0.39 | 150 MB |
| robyn | 44.6 / 87 ms | 1,283 / 3,421 ms | 91% | 0.63 | 181 MB |
| fastapi | 102 / 302 ms | 473 / 1,157 ms (3,114 connected) | 94% | 0.99 | 247 MB |

Stario and Sanic are level, at the top. Two results changed with the
documented approaches. FastAPI's `EventSourceResponse` is several times heavier
per client than the hand-built `StreamingResponse` of the previous run
(0.38 CPU at 1,000 clients, against 0.34 at 5,000 then). It saturates the
core at 5,000 clients, and only 3,114 of them get connected. Robyn,
measured here for the first time with its `SSEResponse`, falls behind at
5,000 clients.

## Two cores

Two workers on CPUs 0–1, `wrk` on CPUs 2–3, median of 5 interleaved rounds.
Stario appears twice:

- `stario-ft × 2`: free-threaded 3.14t, `STARIO_THREADS=2`. One process
  with two event loops, so in-process state such as `Relay` is shared across
  cores. This is Stario's multi-core mode.
- `stario-mp × 2`: two GIL processes sharing the port with `SO_REUSEPORT`,
  started by the benchmark launcher `apps/stario_mp.py`. This is how the
  other frameworks scale. Stario ships no such mode, and each process would
  have its own `Relay`, so it suits stateless HTTP only.

| Target | plaintext | page | mix | p99 page |
| --- | ---: | ---: | ---: | ---: |
| **stario-mp × 2** (GIL, 2 processes) | **170,288** | **11,060** | **22,320** | **18 ms** |
| **stario-ft × 2** (free-threaded, 2 threads) | **135,406** | **8,192** | **19,592** | 23 ms |
| robyn × 2 | 49,916 | 5,789 | 10,336 | 38 ms |
| blacksheep-granian × 2 | 67,604 | 5,544 | 10,666 | 39 ms |
| sanic × 2 | 35,730 | 5,490 | 9,972 | 43 ms |
| fastapi × 2 | 19,796 | 4,797 | 7,340 | 47 ms |
| stario-mp × 1 (reference) | 80,832 | 5,598 | 11,396 | 36 ms |
| stario-ft × 1 (reference) | 67,190 | 4,931 | 10,276 | 47 ms |

- Two GIL Stario processes serve 1.9x the pages and 2.1x the mix of the
  best two-process competitor, and scale 1.98x on the page.
- Two free-threaded threads serve 1.4x the pages and 1.8x the mix of the
  best two-process competitor, in one process with a shared `Relay`. They
  scale 1.66x on the page and 1.91x on the mix (5.8.0 measured 1.65x on the
  page).
- Free-threading costs a quarter of the page throughput against two
  processes (8.2k vs 11.1k). About 12% is 3.14t's per-thread cost (4.9k vs
  5.6k on one worker). The rest is scaling over the shared module-level row
  dicts.

## Event loop: uvloop vs loopyloop

The same Stario 5.8.2 wheel and the same app, on
[loopyloop](https://github.com/Bobowski/loopyloop) (commit `b10b23b`, an
asyncio event loop on io_uring with an epoll fallback, built from source)
instead of uvloop 0.23.0. A separate run on the same afternoon with the same
method: 5 interleaved rounds (3 for SSE and connections), every measurement
gated on a quiet machine, 0 noisy starts, steal median 0.7%
(`results/20261111T161849Z`). Each launcher checks that loopyloop is running
the backend it asked for, and the server logs confirm it every time.

**One core** (req/s at 128 connections, median of 5 rounds):

| | plaintext | json | params | validate | page | page `@baked` | mix | mix, compressed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| uvloop | 75,929 | 71,980 | 67,984 | 50,689 | 5,681 | 8,621 | 12,617 | 10,227 |
| **loopyloop, io_uring** | **136,392** | 128,274 | **116,079** | **105,579** | **6,531** | 11,346 | 15,159 | 12,362 |
| **loopyloop, epoll** | **140,754** | **128,447** | 113,623 | 99,767 | 6,356 | **12,568** | **15,328** | **12,766** |
| io_uring vs uvloop | 1.80x | 1.78x | 1.71x | 2.08x | 1.15x | 1.32x | 1.20x | 1.21x |

| Tail and capacity | uvloop | loopyloop, io_uring | loopyloop, epoll |
| --- | --- | --- | --- |
| p99, plaintext / page at 128 connections | 4.1 / 41 ms | 2.0 / 37 ms | 2.3 / 42 ms |
| `/page` at 256 connections | 5,821 req/s, p99 1,400 ms | 6,720 req/s, p99 64 ms | 5,989 req/s, p99 93 ms |
| `/page` at 1,024 connections | 5,706 req/s, 158 timeouts | 6,480 req/s, 0 timeouts | 6,380 req/s, 0 timeouts |
| `/page` zstd / brotli | 4,971 / 2,903 req/s | 5,720 / 3,629 req/s | 5,591 / 3,571 req/s |
| SSE, 5,000 clients | p50 61 ms, p99 205 ms, 0.20 CPU | p50 69 ms, p99 153 ms, 0.16 CPU | p50 70 ms, p99 148 ms, 0.16 CPU |
| 10,000 connected clients | p50 140 ms, p99 1.19 s, peak 476 MB | p50 54 ms, p99 653 ms, peak 476 MB | p50 143 ms, p99 450 ms, peak 252 MB |
| RSS after start | 32 MB | 31 MB | 30 MB |

**Two cores:**

| | plaintext | page | mix | p99 page |
| --- | ---: | ---: | ---: | ---: |
| 2 processes, uvloop | 166,623 | 9,703 | 19,253 | 18.7 ms |
| **2 processes, loopyloop** | **254,346** | **10,081** | **22,814** | 19.9 ms |
| 2 threads (free-threaded), uvloop | 123,547 | 7,843 | 18,097 | 23.9 ms |
| **2 threads (free-threaded), loopyloop** (experimental) | **235,906** | **8,920** | **20,234** | 24.7 ms |

- **Small responses run 1.7–2.1x faster on loopyloop**, with half the
  p99. This holds in every round: loopyloop's slowest round beats uvloop's
  fastest on plaintext, json, params, validate, and `@baked`.
  `validate` gains the most (2.1x), probably because a request body that
  arrives with the headers needs no extra trip through uvloop.
- **The HTML page gains less: 15% plain, 32–46% with `@baked`.** Most of a
  plain-tree page's time is building HTML in Python, which the loop doesn't
  touch. The less time the handler takes, the more the loop matters. The
  `/page` and mix ranges overlap between rounds, so their gains are solid as
  medians but not round by round.
- **loopyloop fixes Stario's worst weakness, the tail at high
  concurrency.** At 256 connections the `/page` p99 drops from 1.4 s to
  64 ms, and at 1,024 connections the 158 timeouts drop to 0. The starved
  connections were uvloop's scheduling, not Stario's.
- **io_uring and epoll come out about level on this VM.** epoll is slightly
  ahead on plaintext and the mix, io_uring on validate, page, and the
  256-connection tail. loopyloop's own repository reports io_uring well
  ahead of epoll (2.37x vs 1.71x of uvloop on plaintext); this harness
  doesn't reproduce that gap. Its epoll backend matters, because container
  runtimes often block io_uring.
- **On two cores, the gain shrinks on the page** (+4% for processes, within
  noise; +14% for threads) but holds on the mix (+18%, +12%). Plaintext on
  two cores is limited by the load generator against loopyloop (wrk at 89%
  of its two CPUs), so 254k is a lower bound.
- **The free-threaded loopyloop row is experimental.** loopyloop's
  extension doesn't declare free-threading support, so it runs with
  `PYTHON_GIL=0`, which its author lists as untested. It passed the
  correctness checks and every run, and its launcher confirms the GIL stays
  off. Stario 5.8.2 gives extra threads either a uvloop or a stdlib loop,
  so `apps/stario_loopy.py` points that factory at loopyloop for this run.
  Since then, Stario creates extra-thread loops of the main loop's own
  class (CHANGELOG, Unreleased).

## Footprint

| Target | RSS after start |
| --- | ---: |
| **stario** | **32 MB** |
| falcon | 43 MB |
| sanic | 44 MB |
| robyn | 50 MB |
| blacksheep-uvicorn | 54 MB |
| fastapi | 58 MB |
| blacksheep-granian | 99 MB |

The page is 16,032 B from Stario and 16,088 B from the Jinja2 apps: the
same document, differing only in whitespace and escaping.

## HTML generation, no server

Not rerun for 5.8.2: this is the Nov 9 run on 5.7.0. The HTML builder's
GIL-build code path hasn't changed since.

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

1. **Tail latency at 256+ connections.** loopyloop removes it (see "Event
   loop"): p99 64 ms at 256 connections and no timeouts at 1,024. On uvloop, a minority of
   connections wait seconds while p50 stays low: 147 `wrk` timeouts at 1,024
   connections over 5 rounds, and a 1.6 s p99 at 256. Uvicorn, Sanic, and
   Falcon (all on uvloop) behave the same way, while Granian and Robyn
   (Rust I/O) keep a fair tail at half the throughput. An earlier test found
   Stario on stdlib asyncio fair at 1,024 connections, at half the
   throughput. Angles:
   - Reproduce with a minimal uvloop echo server and report upstream.
   - Cap the work per readable event in `HttpProtocol`.
   - Check whether `pause_reading`/`resume_reading` during an eager handler
     ever leaves a socket unpolled.
2. **`/page` p99 at 128 connections** (91 ms) is above Robyn and Granian
   (69–75 ms), at twice their throughput. It is probably item 1 at a smaller
   scale.
3. **Free-threaded scaling.** Two threads give 1.66x one thread on the
   page, against 1.98x for two processes, plus 3.14t's ~12% per-thread
   cost. Measure an app that loads rows per request (from SQLite) instead of
   reading shared module-level dicts. That is the realistic case and should
   scale closer to 2x.
4. **Tree building is most of a plain-tree page.** `@baked` gives 1.55x end
   to end. Steer the docs toward `@baked` for list and table components, or
   cache static subtrees automatically.
5. **Brotli for dynamic responses is costly.** Safari sends no `zstd`, so it
   gets brotli level 4: 3,114 req/s against 4,486 with zstd, for 15% fewer
   bytes. Benchmark brotli levels 2–3 as the dynamic default, and keep high
   levels for precompressed `Files`.
6. **Memory under 10,000 active connections** peaked at 480 MB, against
   110–355 MB for the others, though those served half as many pages. The
   glibc trim threshold Stario sets (64 MiB per arena) keeps freed memory.
   A clean idle-connection memory test would show whether per-connection
   buffers can be smaller.
7. **Small-response throughput is syscall-bound.** Stario's own cost is
   about 1.35 µs of the ~12 µs per plaintext request (`server/inproc.py`),
   so more Python-level micro-optimization has little left to give.

## Reproduce

```bash
STARIO_SPEC="stario==5.8.2" STARIO_FT_SPEC="stario==5.8.2" \
  python3 benchmarks/server/bench.py --rounds 5 --capacity-rounds 3 --isolate \
  --phases throughput,sweep,mixed,compress,sse,conns,multicore
```

`--isolate` moves every other process on the machine to the housekeeping CPU
for the run and restores them afterwards. It needs root. The full run took
2 h 49 min. Nothing else should run on the machine meanwhile. The speed
probe and the quiet-machine check show the noise you can't control; they
don't remove it.
