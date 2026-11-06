"""A test event loop whose clock jumps instead of waiting.

`VirtualTimeLoop` is a stdlib `SelectorEventLoop` whose `time()` is virtual.
The clock only moves when the loop would otherwise block on a timer: the
selector is polled for real I/O for a short grace period, and if nothing
arrives the clock jumps straight to the next timer. Every `asyncio.sleep`,
`loop.call_later`, `loop.time()` deadline, and Stario's own connection
timeout tick runs on the loop's clock, so a 30-second idle timeout fires in
milliseconds of wall time and in exactly 30 virtual seconds, with the real
protocol, real sockets, and no change to the code under test.

Use it where the test runner lets you pick the loop. With pytest-asyncio::

    def pytest_asyncio_loop_factories(config, item):
        return {"virtual_time": VirtualTimeLoop}

in a `conftest.py` applies it to every test under that directory.

Limits: it is a stdlib loop (uvloop has no hook for this). A thread that
takes longer than `idle_grace` to answer looks like elapsed time to the
loop; raise `idle_grace` on the loop for a test that blocks in a thread.
The wall clock (`time.time()`, cookie expiry, trace timestamps) is untouched.
"""

from __future__ import annotations

import asyncio
import selectors
import time
from typing import Any, cast


class _JumpingSelector:
    """Polls the real selector briefly, then jumps the loop's clock to the next timer."""

    def __init__(self, inner: selectors.BaseSelector, loop: VirtualTimeLoop) -> None:
        self._inner = inner
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        if timeout is None or timeout <= 0:
            return self._inner.select(timeout)
        events = self._inner.select(min(timeout, self._loop.idle_grace))
        if not events:
            self._loop._virtual_now += timeout
            self._loop.jumps += 1
        return events

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """`SelectorEventLoop` with a clock that advances only across timer waits.

    `jumps` counts the clock jumps, for tests that want to assert the loop
    never waited for real.
    """

    # Real seconds to wait for I/O before jumping to the next timer.
    idle_grace: float = 0.02

    def __init__(self, selector: selectors.BaseSelector | None = None) -> None:
        super().__init__(selector)
        self._virtual_now = time.monotonic()
        self.jumps = 0
        inner: selectors.BaseSelector = cast(Any, self)._selector
        self._selector = _JumpingSelector(inner, self)

    def time(self) -> float:
        return self._virtual_now
