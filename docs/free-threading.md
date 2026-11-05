# Free-threaded Stario (Python 3.14t)

`STARIO_THREADS=N` (default `1`) runs **N event-loop threads** in one
process. Thread 0 bootstraps, installs signals, and is also a server.
Every thread binds the same TCP `host:port` with `SO_REUSEPORT` and runs
its own `create_server` — accept, parse, and handlers stay on that loop.
The kernel load-balances new connections. Every worker thread creates the
same loop class as the loop running `serve()` (stdlib asyncio or uvloop).

`STARIO_PIN_THREADS=1` pins each worker thread to one allowed CPU on Linux
(`sched_setaffinity`, cgroup aware). Thread 0 takes CPU slot 0; the allowed
set is read before any pin because a new thread inherits its parent's mask.
That inheritance is also why pinning is opt-in: `asyncio.to_thread`
executors and writer threads started from a pinned worker share its one
CPU. Measure before turning it on.

If this kernel cannot share the listen address with `SO_REUSEPORT`
(typical for Unix sockets; Windows has no flag), Stario stays at **one**
thread — the historical server. No fd-handoff fallback. `N>1` also
requires free-threaded Python 3.14t with the GIL still off, unless
`STARIO_THREADS_ALLOW_GIL=1`. Bootstrap still runs once. `Relay` stays
in-process.

The rest of this note is the design: why one loop per thread, what had to
be locked or made thread-local, and how app state should cross workers.

## Why this is worth it

Today `serve()` is one OS thread and one event loop. Concurrent
connections multiplex, but Python bytecode for handlers still runs serially.
That is the gap versus Granian-on-several-workers and versus any native
server that actually uses the other cores.

Multiprocess workers would close the CPU gap and **break** the product shape
that matters here: tiles / chat-room fan-out over a process-local `Relay`.
Those apps would need Redis/NATS the moment you fork. Free-threaded CPython
is the only way to keep that in-process bus **and** use N cores.

3.14t is slower than GIL 3.14 on a **single** thread (often high-single-digit
to tens of percent, workload-dependent). The bet is N loops on N cores, not
a faster hello-world on one core. Measure both; do not ship threads as the
default until the N=1 3.14t regression is known.

## Verdict: one event loop per thread

**Do not run one shared asyncio loop across worker threads.**

Python 3.14’s own guidance is explicit
([asyncio and free-threaded Python](https://docs.python.org/3.14/library/asyncio-threading.html)):

1. Each OS thread that runs asyncio gets **its own** loop. Do not share it.
2. Tasks and futures created on one loop must not be awaited or mutated from
   another thread.
3. Cross-thread hops use `loop.call_soon_threadsafe` or
   `asyncio.run_coroutine_threadsafe`.
4. `asyncio.Lock` / `Event` / `Queue` are **not** inter-thread primitives.
   Use `threading` / `queue.Queue`.

Kumar Aditya’s 3.14 work made **many loops in parallel** scale (per-thread
task lists, per-thread current task). It did **not** make one loop
multi-threaded. A loop still runs callbacks serially on the thread that
called `run_forever` / `asyncio.run`. One loop would keep the current CPU
ceiling.

That is also Granian 2’s free-thread model: workers become threads in one
interpreter; each ASGI/RSGI worker still has **its own** asyncio/uvloop
loop. We should copy that split, not invent a shared-loop scheduler.

```
                    ┌─────────────────────────────────────────┐
                    │  process (one interpreter, GIL off)     │
                    │                                         │
                    │  thread 0: signals, bootstrap, shutdown │
                    │            + a full SO_REUSEPORT server │
                    │         shared App/Router (frozen)      │
                    │         shared Relay, Tracer, Assets    │
                    │                                         │
                    │   thread 0          thread 1            │
                    │   ┌──────────┐      ┌──────────┐        │
  kernel SYN ──────┼──► │ loop 0   │      │ loop 1   │        │
  SO_REUSEPORT     │    │ listen   │      │ listen   │        │
                    │   │ conns A  │      │ conns B  │        │
                    │   │ tasks A  │      │ tasks B  │        │
                    │   │ date 0   │      │ date 1   │        │
                    │   └──────────┘      └──────────┘        │
                    │         │  publish / subscribe  │       │
                    │         └────────► Relay ◄──────┘       │
                    └─────────────────────────────────────────┘
```

**Connection affinity:** the thread that accepts a socket owns it for life.
`HttpProtocol`, `RequestExchange`, `Writer`, `c.alive()`, SSE, body streams,
and the handler task all stay on that loop. Never migrate a live exchange.

## Recommended runtime shape

Keep today’s `STARIO_THREADS` unset / `1` path bit-identical. Add an opt-in
worker count, not a second server.

| Knob | Proposal |
| --- | --- |
| `STARIO_THREADS` | `1` (default) = current `Server`. `N>1` = N worker loops. |
| Interpreter | Require a free-threaded build (`sysconfig.get_config_var("Py_GIL_DISABLED")`) **and** `not sys._is_gil_enabled()`. If an import re-enables the GIL, **refuse to start** (Granian does this). |
| GIL 3.14 | `N>1` should error. Extra threads fight the GIL and usually lose. |
| Loop impl | stdlib asyncio first. uvloop only when the installed wheel is `freethreading_compatible` on this interpreter (uvloop #693 landed 3.14t wheels; still verify at runtime). |
| Bootstrap | **Once**, on the main thread, before workers accept. One `App`, one closed-over `Game` / `Database` / `Relay`. Do **not** re-run bootstrap per thread (that is multiprocess with extra races). |
| Signals | Stay on the main thread (`signal.signal` + `call_soon_threadsafe` into every worker loop). |

### Accept: `SO_REUSEPORT`, each thread is a server

Sharing one `asyncio.Server` across threads is undefined. `N>1` does
**not** hand accepted fds across threads.

**TCP (Linux and other kernels that implement `SO_REUSEPORT`):** each
thread binds the same `host:port` with `SO_REUSEPORT` and runs
`create_server` on its own loop. The kernel distributes new SYNs.
Thread 0 also owns `signal.signal` and fans out shutdown. TLS handshake
stays on the accepting worker.

**If `SO_REUSEPORT` cannot share the address** (Unix sockets on many
kernels, Windows): stay at `N=1`. Do not `epoll` one listen fd from
every loop (thundering herd). Do not fall back to `connect_accepted_socket`
handoff.

### Split “app table” from “loop runtime”

`App` today mixes a frozen route table with loop-owned objects:

```35:51:src/stario/http/app.py
    def __init__(self) -> None:
        super().__init__()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise StarioError(
                "App() requires a running event loop",
                ...
            ) from exc

        self.shutdown = loop.create_future()
        self.tasks: set[asyncio.Task[Any]] = set()
```

That cannot be shared as-is.

| Stay on the shared `App` / `Router` | Move per worker loop |
| --- | --- |
| Compiled trie (read-only after bootstrap) | `shutdown` Future |
| Registered handlers / middleware | `tasks` set (per thread via `threading.local`; each protocol binds its loop's set once) |
| `host_routing` | `connections` set |
| | Date-header box + `Connections` registry (1s C sweep) |
| | `HttpProtocol.loop` |

Concrete shutdown:

- Process-wide `threading.Event` (or a flag + `os.write` to a wakeup fd).
- Each worker has `loop.create_future()` completed via
  `call_soon_threadsafe` when the Event is set.
- `App.shutting_down` reads the Event, not `future.done()` on a foreign loop.
- `c.alive()` must wait on **this loop’s** shutdown Future plus **this
  connection’s** `disconnect`. Today it does
  `asyncio.wait({disconnect, self.c.app.shutdown})` — that Future is
  created on the bootstrap loop and is illegal to await elsewhere.

`create_task` must keep using `get_running_loop()` / the protocol’s
`self.loop`. Never pass loop A a coroutine and run it on loop B.

After bootstrap, treat the router as frozen. Live `app.add` from a
request on thread 2 while thread 1 is in `_lookup` is a trie race we
should not support. If we ever need it, invalidate under a lock and
accept the cost.

## Relay — this is the cross-thread API we already have

`Relay` was written for this:

- `publish` is sync and lock-based; safe from any OS thread.
- Each subscription binds to the loop that `async with`’d it.
- Same-loop deliver is inline. A publish from another thread appends to
  the inbox under its lock and schedules one `call_soon_threadsafe` wake
  per burst, only while a `receive()` is blocked. A subscriber that keeps
  up costs its publisher no loop hop at all.
- Tests already cover worker-thread publish and two loops
  (`tests/test_relay.py`).

Keep Relay as the **only** first-party way to wake another thread’s
handler. Do not add a second bus.

### What still bites

**Unbounded inboxes.** A slow SSE subscriber on thread 1 plus a hot
publisher on thread 0 grows `deque` without bound. Unbounded is still the
default contract; threads make it easier to hit. `subscribe(maxsize=N)`
caps the buffer and drops the oldest message, `subscribe(latest=True)`
keeps only the newest message per subject — drops count in `sub.dropped`.
Apps that need real backpressure still want NATS/Redis.

**Payload aliasing.** `publish` hands the same `data` object to every
subscriber. If a handler mutates the dict, every other thread sees it.
Rule: publish immutable payloads (`str`, `tuple`, frozen structs, or a
fresh copy). Tiles publishes a `user_id: str` — fine. Publishing the
`Game` itself would not be.

**`Relay.lock` is a single mutex.** Fine for “tens of SSE clients, some
clicks.” A 100k req/s publish-per-request hot path will serialize here.
That is acceptable: Relay is for live UI, not for the plaintext bench.
If it ever shows up in profiles, shard by subject prefix, don’t make
publish lock-free on a shared dict without a new design.

**Lock-free snapshot of `active` / `loop` / `generation`.** `publish`
reads those fields without `_inbox_lock`, then `deliver` re-checks.
Documented as skip-or-harmless-schedule. Still correct under 3.14t
because the fields are simple Python attrs and `deliver` is the
authority. Do not “optimize” that without keeping the generation bump.

**Dead-loop reaping.** `drop_subscription` from `publish` can run on the
publisher’s thread while `__aexit__` runs on the subscriber’s. The
registry lock covers that; leave it.

**`asyncio.Event` inside apps is wrong for cross-thread.** Relay exists
so you do not wait on another loop’s Event.

### Pattern that stays nice

```python
# any thread / any handler
relay.publish("room.42.click", {"cell": cell_id, "user": user_id})

# only on the loop that opened the SSE
async with relay.subscribe("room.42.*") as sub:
    async for subject, payload in c.alive(sub):
        sse.patch_elements(view(snapshot))
```

For **mutable** process state, pick one of:

1. **Lock the object** (`threading.RLock` on `Game`). Simple. Tiles-sized.
   Hold the lock only for the mutation + snapshot, not across `await`.
2. **Actor loop.** One dedicated thread owns `Game`. Commands are Relay
   subjects; that loop applies them and publishes `"room.42.changed"`.
   Other threads only read an immutable snapshot (or re-query under the
   lock). This is the “nice” model for anything bigger than a dict.
3. **Don’t share.** Read-only after bootstrap (route table, hashed
   assets, baked markup plans).

There is no safe “just use the dict, 3.14t locks builtins.” Single
`d[k] = v` will not corrupt the dict. `Game.paint` in
[stariodev/tiles](https://github.com/stariodev/tiles) (`app/features/board/data.py`) is still a race:

```python
    def paint(self, user_id: str, cell: int) -> bool:
        if self.celebrating:
            return False
        color = self.players[user_id]
        if self.board.get(cell) == color:
            del self.board[cell]
        else:
            self.board[cell] = color
```

Two clicks interleave → lost toggle. `board_view` iterating `game.board`
while another thread mutates it can raise `RuntimeError: dictionary
changed size during iteration` even with builtin locks. Views must
snapshot (`tuple(items)`) under the same lock as writers, or read an
immutable copy published via Relay.

## What would not work without fixes

Grouped by “will crash / corrupt C state”, “will misbehave in Python”,
and “will quietly kill free-threading”.

### 1. Cython / C pools — must fix before `freethreading_compatible=True`

Importing an unmarked extension on 3.14t **re-enables the GIL** for the
whole process. Marking the module compatible without fixing shared C
state is how you get heisenbugs instead of a warning.

Cython 3.1: `# cython: freethreading_compatible=True` (or
`-X freethreading_compatible=True`) only **declares** safety. It does
not add locks.

| Site | Status |
| --- | --- |
| `exchange.pyx` exchange spare | Done. One idle `RequestExchange` stays on the connection (keep-alive). Overflow goes to a small `threading.local()` list (`POOL_MAX=16`) via `release_exchange` → `detach` / `recycle`. Same-OS-thread only — 3.14t never shares an exchange across loops. No process-global list. Recycle uniqueness and `_retained_detach_count` are `_Thread_local`. |
| `vendor/compression_buf.c` `brotli_pool[]` / `zstd_pool[]` / `gzip_pool[]` | Done. `_Thread_local` arrays (`STARIO_CODEC_POOL_MAX=32`). No process-global free-list. |
| `protocol.pyx` URL cache `_UC_*` | Gone. The compiled `CRouter` trie is the lookup path; no C URL table. |
| `_bind_settings()` / `_SETTINGS` | Done. Init once under `threading.Lock`, then immutable. |
| `HttpProtocol.connections` | Per-loop set (Date-tick sweeper walks that loop's set only). |
| `date_box` | Per-loop box. |
| `llhttp` / `nghttp2` session | Per-`HttpProtocol` — OK **if** the protocol never runs on two threads. Affinity is the invariant. |
| Brotli/zstd/gzip state on `RequestExchange` | Per-exchange — OK with affinity. Do not share an encoder. |

**Fix shape for pools:** thread-local pools (best: no lock on the
recycle path, matches affinity) or a `PyMutex` around the global pool.
Thread-local is the one that will not show up in plaintext profiles.

The Cython modules already set `freethreading_compatible=True`. Keep it
only while those pools stay per-thread (or locked at init). `tests/test_threads.py`
and the cython lifetime tests cover recycle + two-loop accept.

Native protocol talks to libbrotli / libzstd directly (`compression_buf.c`).
Python `CompressionConfig` and `Files` / `Assets` use the same libs through
`stario_cython.codecs` — no Python `brotli` / `zstandard` import, so those
packages cannot re-enable the GIL. Asset fingerprints use
`stario_cython.xxh64`, which is also a compiled module.

### 2. Server / App — will not boot correctly on N loops

| Object | Failure mode |
| --- | --- |
| `App.shutdown: Future` | Created on the bootstrap loop. Workers cannot `await` it. `signal_shutdown` / `set_result` from the wrong thread is undefined. |
| `c.alive()` | Waits on that Future. SSE would either never see shutdown or raise. |
| `App.tasks` | `set.add` + `discard` from N loops. Builtin set ops are individually safe; drain’s `list(self.tasks)` + `asyncio.wait` is not a consistent snapshot, and those Task objects are loop-affine. Per-loop sets, or trust `asyncio.all_tasks()`. |
| `Server._date_tick` | One task, one `date_box`, walks `_live_connections`. Must exist **once per loop**. |
| `Server._create_listener` | Captures `asyncio.get_running_loop()` into every protocol. Each worker must `create_server` on **its** loop. |
| `_signal_handlers` | Must remain on the main thread; fan out with `call_soon_threadsafe`. |
| Keep-alive + `app.shutdown.done()` in `response_completed` | Must read the process-wide flag. |

### 3. Router / caches — mostly OK if frozen

`find_handler` walks the `CRouter` trie compiled from the `Router` after
bootstrap. It has no cache and is only read while serving, so every loop
shares it safely.

Unsafe: `app.add` / `use` concurrent with lookups. Register routes in
bootstrap.

`stario.http.wire` path/accept-encoding LRUs and `markup.escape` caches
are the same story — OK.

The compiled trie is C structs plus read-only dicts; concurrent reads
need no lock.
Do not mutate.

### 4. Telemetry

Json/SQLite sinks already use `queue.Queue` + a writer thread. `on_end`
from N request threads is the design they have.

`RecordingSpan` is mutated on the request thread (`attr`, `event`,
`end`) with no lock. That is fine if **only that thread** touches the
span until `on_end`.

**TTY tracer is now fine.** `TTYTracer.create()` sets `span.mutex` to its
`_lock`, so every mutation (`attr`, `event`, `end`) serializes with the
refresh thread that walks in-progress spans and reads `.attributes` /
`.events` for the live footer. Without the GIL this would otherwise be a
dict mutation vs iteration crash and torn reads.

`create()` parent checks (`parent_id in _open_span_ids`) already take
`_lock`. Keep that.

### 5. Filesystem / JSON / markup

`Assets` / `Files`: after `load()`, `_cache` is read-mostly. Serving
hashed bytes from many threads is OK. `Files` can `del self._cache[path]`
on a stale mtime — that needs a lock or “don’t mutate the cache, replace
the entry atomically”. Streaming uses `asyncio.to_thread(os.pread)` —
already a worker thread; still fine.

`stario.json.set_codec` is a unsynchronized global. Configure at
startup only (already documented). Codecs themselves must be
thread-safe; stdlib `json` is.

`@baked` plans are immutable after decoration. Render is CPU we **want**
on every core.

### 6. Application code we ship as examples

[stariodev/tiles](https://github.com/stariodev/tiles), the demo in its own repository: `Game` has no
lock. `join` / `leave` / `paint` / `board_view` will lose updates and can
throw during iteration.

`examples/chat-room`: `Database` is `sqlite3.connect` with default
`check_same_thread=True` and the comment “Stario serves on a single
event-loop thread.” Other threads raise. Need
`check_same_thread=False` plus a mutex, or one connection per thread
(WAL), or move SQL to the actor thread.

`c.state` is per-request — OK.

### 7. Tests / TestClient

Stay single-loop. Do not require 3.14t to run the suite. Add a
**gated** module (skip unless `Py_GIL_DISABLED` and GIL actually off)
that: two loops, shared `Relay`, shared frozen `App`, fake protocols or
real sockets, recycle the thread-local exchange spare, hit compression.

## Races, leaks, locks — checklist

### Races (correctness / memory)

- Exchange pool reuse → two requests, one `RequestExchange` (mitigated: connection idle + thread-local spare; never share across loops).
- Compression C pool → two encoders, one `StarioBrotli*` (mitigated: `_Thread_local` pools).
- `connections` iterate vs add/discard (mitigated: per-loop set).
- TTY live view vs `RecordingSpan.attributes.update` (mitigated: the TTY
  tracer sets `span.mutex` to its `_lock`, so mutation and render
  serialize).
- `Game.board` / `user_colors` without a lock.
- `app.shutdown` Future touched from the wrong loop.
- Handler `w.write` from a non-owner thread (user error; must remain
  forbidden — Writer is not thread-safe and must not be).

### Leaks

- Relay inboxes if SSE tasks on a busy core cannot keep up.
- Worker tasks not cancelled on drain because they lived in a different
  `App.tasks` than the drainer looked at.
- Timeout sweeper tasks stored on `loop` (`_SWEEPS_ATTR`); must stop
  when **that** loop’s connection set empties, not a shared set.
- Exchange / codec pools that never release because `connection_lost`
  ran on the wrong thread (affinity bug).
- `call_soon_threadsafe` handles queued to a closing loop — Relay
  already swallows closed-loop `RuntimeError`; keep that discipline
  everywhere we hop.

### Locks (what we actually want)

| Lock | Where | Hold for |
| --- | --- | --- |
| `Relay.lock` | already | registry snapshot only, not `set_result` |
| `_inbox_lock` | already | inbox / waiter |
| thread-local or `PyMutex` | C/Cython pools, URL cache init | acquire/release, not encode |
| `threading.Event` + per-loop Future | shutdown | signal path |
| `threading.RLock` | user `Game` / sqlite | mutation + snapshot, never across `await` |
| TTY `_lock` | already; also assigned to `span.mutex` | span mutation + render snapshot |
| **None** | frozen Router, baked plans, hashed assets | — |

Avoid: one giant `Server` lock around dispatch (kills the point). Avoid
`asyncio.Lock` for Game (it is per-loop). Avoid sharing
`RequestExchange` / `Headers` / `Writer`.

Deadlock watch: `publish` must not call user code under `Relay.lock`
(it doesn’t). `drop_subscription` under the registry lock must not
wait on a worker. Drain must not join worker threads while holding a
lock those workers need for `connection_lost`.

## What a first implementation can look like

Order matters. Shipping `STARIO_THREADS` before the C pools are
thread-local will look “done” and corrupt.

1. **Detect 3.14t / GIL state.** Helper used by CLI. No behavior change.
2. **Cython hygiene.** Thread-local exchange pool, thread-local or mutex
   codec pool, mutex around URL cache **or** drop the C cache and keep
   the Python LRU, one-time `_SETTINGS` init. Then
   `freethreading_compatible=True`. A two-thread unittest that recycles
   exchanges and compresses.
3. **Loop runtime split.** Per-loop connections, date tick, task set,
   shutdown Future. Shared frozen `App` after one bootstrap. Still
   `STARIO_THREADS=1` on the old code path.
4. **N `SO_REUSEPORT` servers.** `STARIO_THREADS=N` on 3.14t only. Refuse
   if GIL came back. Each thread `create_server`s; fall back to `N=1`
   when the kernel cannot share the port. Graceful drain: flag → each
   loop’s Future → `_drain_listener` per thread → join extras →
   bootstrap teardown.
5. **Examples.** Lock or actor for `Game`; sqlite policy for chat-room.
6. **Benches.** Same suite as `benchmarks/server`, `THREADS=N` in wrk
   **and** `STARIO_THREADS` in the server, 3.14 vs 3.14t, N=1 and N=ncpu.
   Expect plaintext to move less than HTML/JSON/TLS.

Out of scope for v1: task stealing across loops, migrating keep-alive
connections, making `Writer` thread-safe, multiprocess + Relay.

## What not to do

- One loop, `run_in_executor` for async handlers. Handlers `await`
  Relay, body, SSE. They belong on a loop, not in a thread pool.
- Process workers as a substitute for this (Relay dies).
- `freethreading_compatible=True` as a flag flip without pool fixes.
- Sharing `asyncio.Server`, `Task`, `Future`, or `Event` across threads.
- Defaulting `STARIO_THREADS` to `ncpu` before N=1 3.14t is measured.

## References (code in this tree)

- Server / one loop: `src/stario/http/server.py`, `src/stario/http/app.py`
- Protocol dispatch: `src/stario_cython/protocol.pyx` (`asyncio.Task`,
  compiled trie lookup, `connections`, `date_box`)
- Pools: `src/stario_cython/exchange.pyx` (`_thread_pool`, `release_exchange`),
  `vendor/compression_buf.c`
- Relay: `src/stario/relay.py`, `tests/test_relay.py`
- Alive/shutdown: `src/stario/http/context.py`
- Tiles state: `app/features/board/data.py` (`Game`) in [stariodev/tiles](https://github.com/stariodev/tiles)
- Chat sqlite: `examples/chat-room/app/db.py`
)
