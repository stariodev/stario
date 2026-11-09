"""N GIL Stario processes on one port (SO_REUSEPORT), for the multicore phase.

Stario's own multi-core mode is STARIO_THREADS on free-threaded Python: one
process, so in-process state such as Relay is shared. This launcher is the
classic alternative the other frameworks use (Uvicorn, Granian, Sanic
workers): fork N independent processes that share the listen port through
the kernel. Each process has its own Relay, so it fits stateless HTTP only.
Benchmark use only; Stario does not ship a multi-process mode.

    python -m apps.stario_mp 2
"""

import contextlib
import os
import signal
import sys

import uvloop


class ReusePortLoop(uvloop.Loop):
    async def create_server(self, *args, **kwargs):
        kwargs["reuse_port"] = True
        return await super().create_server(*args, **kwargs)


def worker() -> None:
    import asyncio

    from apps.stario_app import bootstrap
    from stario import serve

    asyncio.run(serve(bootstrap), loop_factory=ReusePortLoop)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    children = []
    for _ in range(n):
        pid = os.fork()
        if pid == 0:
            worker()
            os._exit(0)
        children.append(pid)

    def stop(signum, frame):
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for pid in children:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


if __name__ == "__main__":
    main()
