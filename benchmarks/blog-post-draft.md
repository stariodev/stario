---
title: "How fast is Stario? Benchmarks against FastAPI, BlackSheep, Sanic, Falcon and Robyn"
date: 2026-11-09
---

<!--
Draft notes (delete before publishing):
- Links point to `main`; they work once branch claude/keen-johnson-qpqk7o is merged.
- The "Multi-core" section shows numbers from the unreleased immortalization
  branch (claude/inspiring-davinci-ydskh0). Keep it only if that ships in the
  release this post goes out with; otherwise drop the section or use the
  5.7.0 row only.
-->

We rebuilt Stario's benchmark suite to compare like with like across Python
web frameworks: the same endpoints, the same CPU budget, the same output,
checked before anything is timed. Here is how it works and what it shows.

## Methodology

**Server benchmarks** ([`benchmarks/server/bench.py`](https://github.com/stariodev/stario/blob/main/benchmarks/server/bench.py)):

- **One core per server.** Each server is pinned to a single CPU with
  `taskset`, Rust helper threads included (Granian, Robyn). The load
  generator, [`wrk`](https://github.com/wg/wrk), runs on separate CPUs. Every
  server is at ~100% of its core in every run, so the server is the
  bottleneck, not the client.
- **Same answers.** Before measuring, every endpoint of every framework is
  fetched and checked. The HTML page must be the same document everywhere.
  Socket errors and non-2xx responses are reported next to each number.
- **Real work, not just hello-world.** Besides plaintext and JSON, there is
  a 16 KB HTML page: a 50-row admin table, with escaping. Stario builds it with `h.*`; the
  other frameworks render it with Jinja2. There is also a browser-shaped
  mix: 40% page, 30% JSON with a path parameter, 20% JSON, 10% POST, each
  request carrying a real Chrome header set.
- **Handlers run per request.** No response caching: Robyn's `const=True` is
  off. Listen backlogs are 2048 everywhere (Sanic's default of 100 and
  Granian's 1,024-connection cap were raised to match).
- **Median of 3 runs** of 10 s each, after a warm-up.

**HTML benchmarks** ([`benchmarks/html/compare.py`](https://github.com/stariodev/stario/blob/main/benchmarks/html/compare.py)):
the same product list rendered by Stario, Jinja2, Mako, htpy, dominate,
FastHTML's `fastcore.xml`, and tdom, plus hand-written f-strings with
markupsafe as a floor. Every output is checked against Stario's. It runs at
10, 50, and 500 rows, with and without text that needs escaping.

Everything, including how to rerun it, is in
[`benchmarks/README.md`](https://github.com/stariodev/stario/blob/main/benchmarks/README.md).
The full numbers are in
[`benchmarks/RESULTS.md`](https://github.com/stariodev/stario/blob/main/benchmarks/RESULTS.md).

**Machine:** 4 vCPU Intel Xeon @ 2.10 GHz cloud VM, 15 GB RAM, Linux 6.18,
CPython 3.14.6, uvloop 0.23. Versions: Stario 5.7.0, FastAPI 0.162 (Uvicorn
0.54), BlackSheep 2.6.4 (on Uvicorn and on Granian 2.8.7), Sanic 25.12,
Falcon 4.4, Robyn 0.88. Expect about ±10% run-to-run noise on a VM like this.

## Results: one core

Requests per second at 128 connections. Higher is better.

| Framework | Plaintext | JSON | HTML page | Browser mix |
| --- | ---: | ---: | ---: | ---: |
| **Stario** | **77,601** | **75,381** | **6,666** | **13,298** |
| BlackSheep + Granian | 48,515 | 48,115 | 2,082 | 4,309 |
| Robyn | 29,946 | 31,477 | 2,502 | 4,762 |
| BlackSheep + Uvicorn | 23,053 | 22,660 | 2,409 | 5,116 |
| Falcon + Uvicorn | 21,391 | 19,660 | 2,515 | 4,814 |
| Sanic | 18,680 | 17,150 | 2,280 | 4,127 |
| FastAPI + Uvicorn | 11,505 | 10,773 | 2,205 | 3,685 |

On the HTML page, Stario is 2.6x the next framework. With
`@baked` (the same page, precompiled) it serves 9,513
pages/s, 3.8x the next framework. Latency follows: on the mix, Stario's p99
is 17 ms; the others range from 90 ms to 1.5 s.

**With compression on** (a browser's `Accept-Encoding`), Stario serves the
mix at 11,933 req/s and 792 B per response using zstd. Frameworks with
built-in gzip manage 2,600–3,900 req/s. Even with gzip forced to the same
level everywhere, Stario's HTML page is 2.5x faster, so the gap is framework
cost, not the codec.

## Results: day-to-day load

- **10,000 connected users**, each loading the HTML page every 2 seconds
  (5,000 req/s offered), on one core. Stario serves them all: p50 3.4 ms,
  p99 86 ms, zero errors. Every other framework falls behind, at 1,100–2,500
  req/s, with seconds of latency or thousands of errors.
- **Live updates:** 5,000 clients on a Datastar SSE stream, with an HTML
  fragment broadcast every 0.5 s. Stario delivers at p50 54 ms using 0.19
  of a core. This is on par with the fastest others (Sanic: 55 ms, 0.18
  core); fan-out is cheap for every async server.

## Results: HTML rendering alone

Microseconds per 50-row page, no server. Lower is better.

| Library | Clean text | Escape-heavy |
| --- | ---: | ---: |
| **Stario `@baked`** | **17** | **20** |
| f-strings + markupsafe (no library) | 35 | 41 |
| **Stario (plain `h.*` tree)** | **44** | **41** |
| Mako | 61 | 79 |
| Jinja2 | 99 | 132 |
| tdom, dominate, FastHTML FT, htpy | 940–1,150 | 970–1,220 |

`@baked` beats hand-written f-strings. The plain tree, where every element
is a real object you can inspect, beats both compiled template engines.

## Multi-core

Stario runs one process with one event loop per thread on free-threaded
Python 3.14t (`STARIO_THREADS`), so in-process features like `Relay` keep
working across cores. Two threads on two CPUs, against two-process setups
of the other frameworks:

| | Plaintext | HTML page | Browser mix |
| --- | ---: | ---: | ---: |
| **Stario, 2 threads** | **133,681** | **10,617** | **21,561** |
| BlackSheep + Granian, 2 workers | 75,537 | 5,714 | 11,672 |
| Robyn, 2 processes | 65,435 | 5,879 | 11,667 |
| Sanic, 2 workers | 49,702 | 5,883 | 11,291 |
| FastAPI, 2 workers | 26,262 | 5,152 | 8,879 |

(Measured on the same machine in a separate session; it ran ~15% faster
that afternoon, so compare within this table.)

## What we're still working on

Benchmarks are only useful if they show the weak spots too:

- At 1,024+ concurrent connections, a minority of requests on uvloop-based
  servers (Stario and Uvicorn) can wait seconds. Granian and Robyn keep a
  fairer tail there. We're investigating.
- Free-threaded Python costs ~15% on a single thread compared with the GIL
  build. That is CPython's cost; the win comes from the second core.

The numbers are from one machine. Run the suite on yours:

```bash
python3 benchmarks/server/bench.py            # all frameworks, all phases (~1 h)
benchmarks/server/run.sh                      # throughput only (~15 min)
```
