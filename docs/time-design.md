# Time in Stario: one clock per loop, virtual time in tests

A design study with measurements, November 2026. It answers three questions
the audit left open: how the server should read the clock (sweeps, the
`Date` header, deadlines), whether handlers need a time dependency such as
`app.sleep()`, and how the test suite can stop waiting for real timeouts.
Nothing here is implemented yet except as throwaway prototypes in a
scratch directory; the numbers come from those.

## 1. What exists today

Time reaches the server through five paths:

| Path | Where | Cost today |
| --- | --- | --- |
| `Server._date_tick` | `server.py:816-850`: a coroutine per loop, `await asyncio.sleep(1)`, `datetime.now(UTC)` + `email.utils.format_datetime`, then `connections.sweep(loop.time())` | one Task wake per second per loop, ~2 µs to format, runs even with no connections |
| `Connections.sweep(now)` | `protocol.pyx:679-691`: `tuple(set)` snapshot, `isinstance` per protocol, `cpdef check_timeouts`, which calls `transport.is_closing()` in Python for every connection | measured 360 ns per connection: 3.6 ms per second at 10k connections, 36 ms at 100k, as one stall |
| deadlines | `_arm_timeout` stores `0.0`; the next sweep fills `now + T`, so a timeout fires between T and T + 1 s. This keeps `loop.time()` (55–78 ns in Python) off the request path | 0 ns per request |
| `loop.call_later` | lingering close (2 s), HTTP/2 GOAWAY grace (1 s) | rare |
| `date_box` | a one-element list threaded through `Server`, `_LoopWorker`, `HttpProtocol`, `RequestExchange`, and `TestClient`; each response reads `date_box[0]` | ~5 ns per response |

Handlers use `asyncio.sleep` and `loop.time()`. Cookies and telemetry use
the wall clock (`time.time()`, `time.time_ns()`), which is correct for
HTTP dates and trace timestamps and should stay that way.

Tests pay for all of this in real seconds: about 45 timeout tests sleep
through header, idle, body-stall and write-stall windows and account for
roughly 16 of the suite's 24 seconds, with thin margins (a 150 ms timeout
checked after 400 ms, a 50 ms sweep).

## 2. Measurements

Throwaway Cython module and scripts, Python 3.14, this container (4 vCPU).

Clock reads, per call:

| Read | ns |
| --- | ---: |
| `clock_gettime(CLOCK_MONOTONIC_COARSE)` in C | 4.4 |
| `clock_gettime(CLOCK_MONOTONIC)` in C | 32 |
| `loop.time()` (uvloop) from Python | 56 |
| `loop.time()` (asyncio) from Python | 78 |
| `time.monotonic()` from Python | 104 |

`Date` header:

| Operation | ns |
| --- | ---: |
| `datetime.now(UTC)` + `format_datetime` + `%` (today, once a second) | 1 929 |
| C formatter from an epoch second (`gmtime_r`, fixed layout) | 45 |
| on-demand `date()`: coarse realtime read, compare the second, refresh when it changed | 4.5 per call |

Sweep, per connection, with the fields `check_timeouts` touches
(`Conn` mock, 10k and 100k connections):

| Shape | 10k | 100k |
| --- | ---: | ---: |
| today: `tuple(set)`, `isinstance`, `cpdef`, Python `is_closing()` | 41 ns | 49 ns |
| same, but `is_closing()` replaced by a C flag | 9 ns | 17 ns |
| `cdef list` walked in C, inline check, C flag | 1 ns | 5 ns |
| intrusive doubly linked list, inline check, C flag | 3 ns | 7 ns |

The real `check_timeouts` measured 360 ns per connection with real
`HttpProtocol` objects, because its body is larger than the mock and the
transport's `is_closing()` is a Python-level call; the shape comparison
says the Python call is about three quarters of it and the container
snapshot most of the rest. A C walk with a C-side closing flag lands near
5–10 ns per connection: 100k connections then cost 0.5–1 ms per second
instead of 36 ms.

Virtual time. A prototype loop (40 lines, below) whose `time()` is virtual
and only advances when the loop would otherwise block on a timer ran seven
timeout tests from the suite unchanged, with real sockets and the real
protocol:

| Test | virtual s | wall s, real loop | wall s, virtual loop |
| --- | ---: | ---: | ---: |
| `test_idle_keep_alive_times_out` | 0.40 | 0.403 | 0.027 |
| `test_partial_headers_time_out` | 0.40 | 0.401 | 0.026 |
| `test_stalled_chunked_body_aborts_without_hanging` | 0.40 | 0.401 | 0.043 |
| `test_deferred_small_body_progress_outlives_header_timeout` | 1.20 | 1.204 | 0.126 |
| `test_client_that_stops_reading_is_aborted_after_write_timeout` | 3.00 | 3.044 | 0.067 |
| `test_h2_stalled_response_resets_stream` | 0.98 | 0.803 | 0.108 |
| `test_h2_send_progress_keeps_stalled_stream_alive` | 0.80 | 0.811 | 0.089 |
| total | | 7.07 | 0.49 |

Every test passed on both loops and observed the same virtual elapsed
time it was written for. Nothing in `src/` changed.

## 3. Proposed design

### 3.1 The loop's clock lives on `Connections` (Cython)

`Connections` is already one object per event loop, handed to every
protocol on that loop. It becomes the only thing that reads the clock:

- `now`: coarse monotonic seconds, refreshed by `tick()` and readable
  inline from C by every protocol and exchange on the loop.
- `date()`: the `date: …\r\n` line, refreshed on demand from
  `CLOCK_REALTIME_COARSE` when the second changed (4.5 ns per response,
  always within one second of true time even when the loop is busy). The
  `date_box` list and the `Server._date_tick` coroutine go away; a worker
  with no connections makes no timer wakeups at all.
- a `cdef list` of live protocols with an index on each protocol for O(1)
  swap-remove, walked in C by `tick()`; `check_timeouts` becomes an inline
  `cdef` with no Python call per connection. The write-stall check keeps a
  C flag set by the protocol's own close paths and `connection_lost`
  instead of calling `transport.is_closing()` per sweep.
- `tick()` is a `loop.call_later(1.0, …)` handle (no coroutine, no Task),
  scheduled only while the list is non-empty.
- deadlines are stored at arm time as `now + T` from the coarse clock
  (4 ns), so a timeout fires at T, not between T and T + 1 s, and the sweep
  only compares. The lingering-close and GOAWAY timers become deadlines on
  the protocol swept by the same `tick()`, so no `TimerHandle` is left in
  the protocol at all.
- tests call `connections.tick(now)` with a chosen `now`, as one test
  already does, or run on the virtual-time loop and let the real `tick`
  fire.

What this changes: `protocol.pyx` (registry, clock, tick, inline checks,
deadline arming: 150–250 lines), `exchange.pyx` / `.pxd` (`_date_box` →
the loop clock: ~20 lines), `server.py` (remove `_date_tick`, `date_box`,
`_LoopWorker.date_box`: ~40 lines removed), `testing/wire.py` and
`client.py` (the frozen `Date` becomes a pinned value on the clock), and
the ~30 test sites that build `HttpProtocol(loop, app, tracer, [date],
…)` by hand (mechanical). `CYTHON.md` and `docs/free-threading.md`
describe the per-loop clock instead of the date box.

What it does not change: the request hot path still reads no clock
(1.8 µs plaintext stays 1.8 µs), and the free-threading model stays one
clock per loop.

### 3.2 No `app.sleep` / `c.sleep`

A handler-facing time dependency would exist only so tests can advance
time. It costs API surface, every handler has to remember to use it, and
third-party code (`asyncio.sleep` in a client library, a database
driver's timeout) would not honour it. The virtual-time loop in §3.3
makes `asyncio.sleep`, `loop.call_later`, `loop.time()` deadlines, the
sweep tick and `c.alive()` all advance together, including inside code
Stario does not own. So the recommendation is: handlers keep using
`asyncio.sleep` and `loop.time()`; Stario's rule is "behaviour reads the
loop's clock, never the wall clock", and tests replace the loop's clock.

Wall-clock reads stay where they mean wall time: cookie expiry and trace
timestamps. A `now=` parameter on `SignedCookie.get()` is enough for a
cookie test; it does not need the loop.

### 3.3 Virtual time for tests: `stario.testing.VirtualTimeLoop`

The prototype, verbatim:

```python
class _JumpingSelector:
    def __init__(self, inner, loop):
        self._inner = inner
        self._loop = loop

    def select(self, timeout=None):
        if timeout is None or timeout <= 0:
            return self._inner.select(timeout)
        # Only timers are pending. Give real I/O a short window; if
        # nothing arrives, jump the clock to the next timer.
        events = self._inner.select(min(timeout, self._loop.idle_grace))
        if not events:
            self._loop._vt_now += timeout
            self._loop.jumps += 1
        return events

    def __getattr__(self, name):
        return getattr(self._inner, name)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    idle_grace = 0.005  # real seconds to wait for I/O before a jump

    def __init__(self, selector=None):
        super().__init__(selector)
        self._vt_now = time.monotonic()
        self.jumps = 0
        self._selector = _JumpingSelector(self._selector, self)

    def time(self):
        return self._vt_now
```

The loop computes its select timeout as `next_timer.when - self.time()`;
the wrapped selector polls real I/O for at most `idle_grace`, and if
nothing arrived it moves the clock to that timer. Callbacks from other
threads (`call_soon_threadsafe`, used by Relay and the telemetry writer)
wake the selector through its self-pipe, so they are never skipped.

Wiring: pytest-asyncio 1.4 takes `@pytest.mark.asyncio(loop_factory=
VirtualTimeLoop)` per test, and a `conftest.py` hook can add that marker
to every test under `tests/cython`. `TestClient` can offer the same loop
through `aload_app(loop_factory=…)` so app tests with SSE loops and
`asyncio.sleep` run in milliseconds.

Limits, stated plainly:

- stdlib `SelectorEventLoop` only; uvloop has no hook for this. The suite
  already runs on the stdlib loop; the two uvloop-gated tests stay real.
- a thread hop slower than `idle_grace` looks like elapsed time. The
  default should be 10–20 ms, and a test that blocks in a thread for
  longer can raise it. The CPython asyncio test loop and Trio's autojump
  clock make the same trade.
- `_selector` is a private attribute of `BaseEventLoop`. It has not moved
  since 3.4 and CPython's own asyncio tests rely on the same shape, but
  it is a dependency to note in the module docstring.
- the wall clock is untouched: a test that checks cookie expiry or trace
  timestamps uses explicit values, as now.

## 4. Impact, in numbers

| Measure | Today | After §3.1 + §3.3 |
| --- | --- | --- |
| per-request clock cost | 0 ns (deadline filled on the next sweep) | 4 ns (deadline exact at arm time) |
| `Date` refresh | 1.9 µs once a second, in a Task | 45 ns when the second changes, no Task |
| idle worker loop | one wakeup per second | none |
| sweep at 10k connections | 3.6 ms per second | about 0.1 ms |
| sweep at 100k connections | 36 ms stall per second | about 1 ms |
| timeout accuracy | T to T + 1 s | T to T + tick |
| timers in `protocol.pyx` | 2 `call_later` + sweep | sweep only |
| timeout tests (about 45) | about 16 s wall, sleep-driven, thin margins | about 1.5 s wall, deterministic |
| whole suite | 24 s | about 9 s |
| new app API | | none |

## 5. Assessment

Pursue §3.3 first. It is small, has no production risk, makes the
timeout tests deterministic, and removes the reason to add a time
dependency to handlers. It should land before any sweep refactor, so the
refactor's own tests are fast.

Pursue §3.1 second. Its win is at scale and in simplicity rather than in
the per-request number: the hot path already avoids the clock, so
`plaintext` stays where it is. What it buys is a single time source in C,
one fewer Task per loop, no idle wakeups, exact deadlines, no `date_box`
threaded through five modules, and a sweep that costs 1 ms instead of
36 ms per second at 100k connections (which today is a once-a-second
latency spike of that size). It is a contained change to one Cython file
plus mechanical test updates. It ranks below finishing the markup work in
value per hour, and above the remaining load items.

Do not add `app.sleep()` or `c.sleep()`.

## 6. Also still open: markup

The audit's step 4 (fuse `tag_start + attrs + ">"` at build time so the
render walk appends one string per element) measured 1.55× on the render
pass, about 15 µs of the 50-row page's 177 µs. Step 11, a compiled
element builder and render walk, is where the remaining 100+ µs of pure
Python per page go; `@baked` at 41 µs shows what the same page costs once
the per-element Python work is gone.
