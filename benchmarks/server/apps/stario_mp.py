"""N GIL Stario processes on one port (SO_REUSEPORT), for the multicore phase.

Stario's own multi-core mode is STARIO_THREADS on free-threaded Python: one
process, so in-process state such as Relay is shared. This launcher is the
classic alternative the other frameworks use (Uvicorn, Granian, Sanic
workers): fork N independent processes that share the listen port through
the kernel. Each process has its own Relay, so it fits stateless HTTP only.
Benchmark use only; Stario does not ship a multi-process mode.

    python -m apps.stario_mp 2
    BENCH_LOOP=loopyloop python -m apps.stario_mp 2   # on loopyloop instead of uvloop
"""

import contextlib
import os
import signal
import sys

if os.environ.get("BENCH_LOOP") == "loopyloop":  # stario-mp-loopy
    import loopyloop

    BaseLoop = loopyloop.Loop  # backend from LOOPYLOOP_BACKEND
else:
    import uvloop

    BaseLoop = uvloop.Loop


class ReusePortLoop(BaseLoop):
    async def create_server(self, *args, **kwargs):
        kwargs["reuse_port"] = True
        return await super().create_server(*args, **kwargs)


def worker() -> None:
    import asyncio

    from apps.stario_app import bootstrap
    from stario import serve

    async def main() -> None:
        loop = asyncio.get_running_loop()
        want = os.environ.get("LOOPYLOOP_BACKEND")
        if hasattr(loop, "backend"):  # loopyloop: a silent fallback would skew the run
            if want and loop.backend != want:
                print(f"stario_mp: asked for {want!r}, got {loop.backend!r}", file=sys.stderr)
                os._exit(1)
            print(f"stario_mp: pid {os.getpid()} loopyloop backend={loop.backend}",
                  file=sys.stderr, flush=True)
        await serve(bootstrap)

    asyncio.run(main(), loop_factory=ReusePortLoop)


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
