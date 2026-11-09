"""Shared pieces of the helper load clients (stdlib only)."""

import asyncio
import contextlib
import json
import multiprocessing as mp
import os
import resource


def raise_nofile() -> None:
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def run_workers(worker, total: int, *args) -> list[dict]:
    """Split `total` connections across one process per allowed CPU."""
    procs = max(1, len(os.sched_getaffinity(0)))
    shares = [total // procs + (1 if i < total % procs else 0) for i in range(procs)]
    with mp.get_context("fork").Pool(procs) as pool:
        return pool.starmap(worker, [(share, i, *args) for i, share in enumerate(shares)])


def emit(result: dict) -> None:
    print(json.dumps(result), flush=True)


async def connect_all(make, n: int, parallel: int = 200):
    """Open n connections, at most `parallel` handshakes at a time."""
    sem = asyncio.Semaphore(parallel)
    out = []

    async def one(i):
        async with sem:
            # The caller counts connections that never opened.
            with contextlib.suppress(TimeoutError, OSError):
                out.append(await asyncio.wait_for(make(i), 10))

    await asyncio.gather(*(one(i) for i in range(n)))
    return out
