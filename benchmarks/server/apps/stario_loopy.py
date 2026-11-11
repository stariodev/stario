"""The Stario app on loopyloop instead of uvloop, for the loop comparison.

loopyloop (github.com/Bobowski/loopyloop) is an asyncio event loop on io_uring
with an epoll fallback. The app, Stario and its settings are the same as the
`stario` target; only the loop differs.

    LOOPYLOOP_BACKEND=uring|epoll python -m apps.stario_loopy

STARIO_THREADS=N (free-threaded build) runs N loop threads. Stario after 5.8.2
gives every extra thread the main loop's class. Stario 5.8.2 itself mirrored
only uvloop and left other loops' extra threads on a stdlib
`SelectorEventLoop`, so on that version this launcher points the factory at
loopyloop. Benchmark use only.
"""

import asyncio
import os
import sys

import loopyloop
import stario.http.server as stario_server
from stario import serve

from apps.stario_app import bootstrap

WANT = os.environ.get("LOOPYLOOP_BACKEND")


def fail(message: str) -> None:
    print(f"stario_loopy: {message}", file=sys.stderr, flush=True)
    os._exit(1)


if os.environ.get("PYTHON_GIL") == "0" and sys._is_gil_enabled():
    fail("PYTHON_GIL=0 was set but the GIL is enabled")


def extra_loop() -> asyncio.AbstractEventLoop:
    loop = loopyloop.new_event_loop()
    print(f"stario_loopy: extra thread on {type(loop).__module__}.{type(loop).__name__}",
          file=sys.stderr, flush=True)
    return loop


if not hasattr(stario_server, "_same_loop_factory"):  # Stario 5.8.2
    stario_server._stdlib_loop_factory = extra_loop


async def main() -> None:
    loop = asyncio.get_running_loop()
    if WANT and loop.backend != WANT:
        fail(f"asked for backend {WANT!r}, got {loop.backend!r}")
    gil = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
    print(f"stario_loopy: loopyloop backend={loop.backend} gil={gil}", file=sys.stderr, flush=True)
    await serve(bootstrap)


loopyloop.run(main())
