---
title: How fast is Stario? Benchmarks against FastAPI, BlackSheep, Sanic, Falcon, and Robyn
slug: stario-5-7-benchmarks
description: Stario against five Python frameworks — plaintext, JSON, a 16 KB HTML page, a browser-shaped request mix, and compression on one core (5.7), two cores (5.8), and HTML rendering on its own. The method, the machine, the numbers, and where Stario is still weak.
date: 2026-11-09
---

We rebuilt Stario's benchmark suite so that it compares like with like: the same endpoints in every framework, the same CPU budget, and the same output, checked before anything is timed. This post covers how it works and what it shows: Stario 5.7.0 on one core, and Stario 5.8.0 on two. The code, the raw results, and a longer write-up are in the Stario repository: [`benchmarks/`](https://github.com/stariodev/stario/tree/b096db9f0b89522fb3e02406291423c6322e15cf/benchmarks).

## Method

The server suite is [`benchmarks/server/bench.py`](https://github.com/stariodev/stario/blob/b096db9f0b89522fb3e02406291423c6322e15cf/benchmarks/server/bench.py). It sets up a virtual environment per framework and runs one server at a time. Each server is pinned to a single CPU with `taskset`, including any Rust helper threads. The load generator, `wrk`, runs on two other CPUs. Every server sat at 99–100% of its core in every `wrk` run, so the server, not the client, was the limit.

Before measuring, the runner fetches every endpoint of every framework and checks the answers. The HTML page has to be the same document everywhere. Socket errors and non-2xx responses are reported next to each number. Each number is the median of three 10-second runs, after a warm-up.

The endpoints are the usual plaintext, JSON, a path parameter, and a small JSON `POST`, plus two that look more like a real app:

- `/page`, a 16 KB admin page with a 50-row table, where one row in seven needs escaping. Stario builds it with `h.*`; the other frameworks render it with a compiled Jinja2 template, which is what their docs reach for.
- A browser-shaped mix: 40% page, 30% JSON with a path parameter, 20% JSON, 10% `POST`. Every request carries a real Chrome header set with cookies.

The HTML suite, [`benchmarks/html/compare.py`](https://github.com/stariodev/stario/blob/b096db9f0b89522fb3e02406291423c6322e15cf/benchmarks/html/compare.py), renders the same product list with Stario, Jinja2, Mako, htpy, dominate, FastHTML's `fastcore.xml`, and tdom. Hand-written f-strings serve as baselines. Every output is checked against Stario's.

Some choices favor one side, and you should know them:

- Pinning everything to one core favors single-threaded event loops. Granian and Robyn run Rust I/O threads, which here share the core with Python.
- FastAPI validates the `POST` with Pydantic, as its users would; the others check fields by hand.
- Stario runs with its tracer off, the counterpart of the access logs turned off elsewhere.
- Robyn's `const=True` response cache is off, so every framework runs its handler per request.
- The listen backlog is 2048 everywhere it can be set. Robyn has no setting for it.

An independent review of the first draft caught a bug that doubled Jinja2's render time: the template used `r.id` on dicts, and Jinja2 tries attribute access before indexing. The numbers below are from the rerun with that fixed.

## The machine

The VM had 4 vCPUs (Intel Xeon at 2.10 GHz), 15 GB of RAM, and ran Linux 6.18. Everything ran on CPython 3.14.6 with uvloop 0.23 (3.14.6t for free-threaded Stario). Versions: Stario 5.7.0 for one core and 5.8.0 for two, FastAPI 0.162 on Uvicorn 0.54, BlackSheep 2.6.4 on Uvicorn and on Granian 2.8.7, Sanic 25.12, Falcon 4.4, and Robyn 0.88. A cloud VM drifts during the day, so all the server numbers below come from one contiguous run. The exceptions are the SSE test, which ran earlier the same day and doesn't involve the template, and the two-core section, described there.

## One core

Requests per second at 128 connections:

| Framework | Plaintext | JSON | HTML page | Browser mix |
| --- | ---: | ---: | ---: | ---: |
| Stario | 82,506 | 66,895 | 7,789 | 14,526 |
| BlackSheep + Granian | 52,571 | 52,793 | 4,659 | 9,010 |
| BlackSheep + Uvicorn | 24,924 | 25,019 | 4,862 | 8,929 |
| Falcon + Uvicorn | 24,196 | 23,235 | 4,965 | 8,507 |
| Sanic | 23,425 | 22,305 | 4,920 | 8,333 |
| Robyn | 31,893 | 30,616 | 4,952 | 8,315 |
| FastAPI + Uvicorn | 13,067 | 12,833 | 4,017 | 5,947 |

On small responses, Stario ranges from level with the fastest other setup, BlackSheep on Granian, to 1.9x it, and is 5–6x FastAPI. Most of that comes from the compiled HTTP core. On the HTML page it is 1.6x the next framework. The same page through `@baked`, which compiles the static parts once, serves 12,329 pages/s, 2.5x the next framework.

Latency follows throughput. In the browser mix, Stario's p99 is 15 ms. The other frameworks range from 17 ms (BlackSheep on Uvicorn) to 309 ms (FastAPI).

## With compression

With a browser's `Accept-Encoding`, Stario picks zstd and serves the mix at 13,240 req/s, at 792 bytes per response. FastAPI and BlackSheep compress with gzip and manage 3,800–6,000 req/s. Sanic, Falcon, and Robyn have no built-in compression and usually sit behind a proxy that does it.

With gzip forced to level 1 everywhere, Stario serves the HTML page at 1.75x the best of the others, against 1.6x uncompressed. Compression costs Stario a little less, about 14% of its page throughput against 18–25% for the others, and the rest of the gap comes from the request path.

## A heavier load

10,000 keep-alive connections each fetch the HTML page every 2 seconds. That's 5,000 page requests per second, close to what the other frameworks can serve on one core. Stario serves it with p50 1.1 ms, p99 26 ms, and no errors. Most of the others serve 4,100–4,500 req/s but queue, with median latency from 240 ms to 1.7 s; FastAPI falls to 2,850 req/s. Four of the six report errors. This is the throughput result again, seen as latency under a fixed load.

For live updates, 5,000 clients hold a Datastar SSE stream while the server pushes an HTML fragment to all of them twice a second. Stario delivers at a median of 54 ms using 0.19 of a core. Sanic is level with it, and the others are close. Fan-out of small events is cheap for any async server, so this is not where Stario stands out.

## HTML rendering on its own

Microseconds per 50-row page, with no server involved:

| Library | Clean text | Escape-heavy |
| --- | ---: | ---: |
| Stario `@baked` | 17.7 | 20.0 |
| f-strings + `html.escape` | 20.6 | 29.4 |
| f-strings + markupsafe | 35.6 | 38.7 |
| Stario plain `h.*` tree | 40.7 | 39.5 |
| Jinja2 | 48.9 | 54.6 |
| Mako | 59.1 | 62.1 |
| tdom, FastHTML FT, htpy, dominate | 919–1,045 | 908–1,067 |

`@baked` is slightly faster than hand-written f-strings. The plain tree, where every element is an object you can inspect, comes in 1.2–1.4x faster than Jinja2. The pure-Python element libraries are about 20x slower than either.

## Two cores with Stario 5.8

Stario's multi-core mode runs on free-threaded Python 3.14t: `STARIO_THREADS=2` starts two event loops in one process, so in-process state such as `Relay` is shared across cores. The other frameworks start two processes. For a like-for-like baseline we also ran two regular Stario processes sharing the port. Stario doesn't ship that mode; the benchmark has [a small launcher](https://github.com/stariodev/stario/blob/b096db9f0b89522fb3e02406291423c6322e15cf/benchmarks/server/apps/stario_mp.py) for it, and each process would have its own `Relay`.

The setup: two server CPUs, three rounds with the frameworks in a different order each time, and the median of the three. Stario 5.8.0 was built from its release commit, `03d54a9`, because the PyPI wheels were not out yet; a later run with the published wheels came out within 4% on the page and mix.

| Setup on 2 cores | Plaintext | HTML page | Browser mix |
| --- | ---: | ---: | ---: |
| Stario, 2 processes | 228,895 | 15,559 | 31,466 |
| Stario, 2 threads (free-threaded) | 140,298 | 10,534 | 22,496 |
| Robyn, 2 processes | 65,933 | 10,039 | 17,484 |
| Sanic, 2 workers | 50,540 | 10,040 | 16,529 |
| BlackSheep + Granian, 2 workers | 75,394 | 8,753 | 18,042 |
| FastAPI, 2 workers | 26,739 | 8,243 | 11,903 |

Read the page and mix columns: plaintext is near the load generator's limit at these rates and shifts with the tool used to measure it, so its ratios are rough. Two Stario processes (the benchmark launcher, not a shipped mode) serve 1.55x the pages and 1.74x the mix of the best two-process setup. Two free-threaded threads match Robyn and Sanic on the page and lead the mix by 1.25x, from a single process.

5.8.0 is what made the threaded row possible. In 5.7.0, threads contended on the reference counts of shared markup objects, and a second thread made the page slower (0.94x). In 5.8.0, two threads serve 1.65x the pages of one, while two processes still serve 2.05x the pages of one. Part of that gap is 3.14t's per-thread cost; part is this benchmark's page reading the same module-level rows from both threads, which plain Python on 3.14t doesn't scale well either.

## Where Stario is still weak

At 1,024 concurrent connections, a minority of requests on uvloop servers, Stario and Uvicorn alike, wait several seconds while the median stays low. Granian and Robyn keep a fair tail at that load. The same Stario on stdlib asyncio is fair too, at half the throughput, so we are looking at how libuv schedules reads.

On free-threaded Python, a thread costs Stario about 16% against the regular build, and HTML work still scales less well across threads than across processes (see "Two cores with Stario 5.8").

## Run it yourself

Numbers from one machine are a starting point. The suite runs on any Linux box with four or more CPUs, `uv`, and `wrk`:

```bash
python3 benchmarks/server/bench.py   # every framework, every phase: about an hour
benchmarks/server/run.sh             # throughput only: about 15 minutes
```

The [results write-up](https://github.com/stariodev/stario/blob/b096db9f0b89522fb3e02406291423c6322e15cf/benchmarks/RESULTS.md) has every table, the concurrency sweep, memory, and the list of what we plan to improve.
