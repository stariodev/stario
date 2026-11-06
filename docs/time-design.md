# Time in Stario

How the server reads the clock, and how tests avoid waiting for it. The
mechanics are in `CYTHON.md` (Timeouts) and the docstrings of
`stario_cython.protocol.Connections` and `stario.testing.vtime`; this note
keeps the reasons.

## One clock per event loop

`Connections` is one object per event loop, handed to every protocol on
that loop, and it is the only thing in the server that reads a clock:

- **The tick.** While a connection with timeouts exists, one
  `loop.call_later` handle fires once a second, reads `loop.time()` once,
  and walks the live connections in C comparing stored deadlines. There is
  no Task, no Python call per connection, and no wakeup on an idle loop.
  A C walk costs a few nanoseconds per connection, so 100k connections
  cost about a millisecond per second; the previous shape (a tuple
  snapshot of a set, `isinstance`, and a Python `is_closing()` call per
  connection) cost 36 ms per second at that size, as one stall.
- **The `Date` header.** The same object serves it from a per-second
  cache: one coarse wall-clock read per response (`CLOCK_REALTIME_COARSE`,
  about 4 ns, no syscall), and a reformat in C only when the second
  changed. Nothing is shared between loops, so nothing is locked, and a
  busy loop can never serve a stale `Date`. `pin_date()` fixes the line for
  tests.
- **Every timer is a deadline on the tick.** Header, idle, body stall,
  write stall, HTTP/2 stream deadlines, the lingering-close wait and the
  GOAWAY grace are all fields compared by the sweep. The protocol holds no
  `TimerHandle` at all.

Deadlines are armed as `0.0` and filled with `now + T` by the next tick.
That looks like a quirk but is deliberate: it keeps `loop.time()` (55–78 ns
from Python) off the request path entirely, and the resulting timeout is
never early and at most one tick late. Exact deadlines would need either a
Python clock call per request or a C clock that disagrees with the loop's
clock under virtual time; neither is worth the sub-second precision.

## Why handlers get no `app.sleep()`

A handler-facing time dependency would exist only so tests can advance
time. It adds API surface, every handler has to remember to use it, and
third-party code (a client library's `asyncio.sleep`, a driver's timeout)
would not honour it. Stario's rule is simpler: behaviour reads the loop's
clock, never the wall clock, and tests replace the loop's clock.

The wall clock stays where it means wall time: cookie expiry and trace
timestamps. A test of those passes explicit values.

## Virtual time in tests

`stario.testing.VirtualTimeLoop` is a stdlib `SelectorEventLoop` whose
`time()` is virtual. The clock advances only when the loop would otherwise
block on a timer: the selector is polled for real I/O for a short grace
period, and if nothing arrives the clock jumps to that timer. Every
`asyncio.sleep`, `loop.call_later`, `loop.time()` deadline, the connection
tick and `c.alive()` advance together, with real sockets and the real
protocol, in code the tests do not change. `tests/cython` runs on it
through a `pytest_asyncio_loop_factories` hook; a 5 s timeout costs
milliseconds of wall time and the margins are exact instead of
sleep-sized.

Its limits, so nobody is surprised:

- stdlib loop only; uvloop has no hook for this.
- a thread that takes longer than `idle_grace` (20 ms) to answer looks
  like elapsed time to the loop. Raise it on the loop for a test that
  blocks in a thread.
- it wraps the loop's private `_selector`, which CPython's own asyncio
  test loop relies on in the same way.
- the wall clock is untouched.
