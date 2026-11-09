"""SSE fan-out client: N connections hold GET /sse; measure delivery latency.

    python sse_client.py http://127.0.0.1:3000/sse 5000 15

Every broadcast carries the server's send time in ``data-ts``; latency is
receive time minus send time on the same machine clock. Only events that
arrive after every connection is open are counted. ``delivered_ratio`` is
events received / (broadcasts seen x connections).
"""

import asyncio
import re
import sys
import time
import urllib.parse

from _client_common import connect_all, emit, pct, raise_nofile, run_workers

TS = re.compile(rb'data-ts="(\d+)"')


class SseConn(asyncio.Protocol):
    def __init__(self, sink):
        self.buf = b""
        self.sink = sink
        self.ok = False
        self.closed = False

    def data_received(self, data: bytes) -> None:
        now = time.time_ns()
        buf = self.buf + data
        if not self.ok:
            if b"\r\n\r\n" not in buf:
                self.buf = buf
                return
            head, _, buf = buf.partition(b"\r\n\r\n")
            self.ok = head.startswith(b"HTTP/1.1 200")
        end = 0
        for m in TS.finditer(buf):
            self.sink(now, int(m.group(1)))
            end = m.end()
        self.buf = buf[end:][-512:]

    def connection_lost(self, exc) -> None:
        self.closed = True


def worker(n: int, index: int, url: str, seconds: float, start_at: float) -> dict:
    raise_nofile()
    u = urllib.parse.urlsplit(url)
    req = (f"GET {u.path} HTTP/1.1\r\nHost: {u.netloc}\r\nAccept: text/event-stream\r\n"
           "Cache-Control: no-cache\r\n\r\n").encode()
    latencies: list[float] = []
    ticks: set[int] = set()
    measuring = False

    def sink(now: int, ts: int) -> None:
        if measuring and ts >= measure_from:
            latencies.append((now - ts) / 1e6)
            ticks.add(ts)

    async def main():
        nonlocal measuring, measure_from
        loop = asyncio.get_running_loop()

        async def make(i):
            _, proto = await loop.create_connection(lambda: SseConn(sink), u.hostname, u.port)
            proto.transport_ = _
            _.write(req)
            return proto

        conns = await connect_all(make, n)
        if not conns:
            return 0
        # Workers start measuring at the same wall-clock moment, after all
        # have had time to connect.
        await asyncio.sleep(max(0.0, start_at - time.time()))
        measure_from = time.time_ns()
        measuring = True
        await asyncio.sleep(seconds)
        measuring = False
        alive = sum(1 for c in conns if c.ok and not c.closed)
        for c in conns:
            c.transport_.close()
        return alive

    measure_from = 0
    alive = asyncio.run(main())
    return {"alive": alive, "latencies": latencies, "ticks": sorted(ticks)}


def main() -> None:
    url, n, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    connect_budget = 5 + n / 1000  # seconds allowed for all connections to open
    results = run_workers(worker, n, url, seconds, time.time() + connect_budget)
    lat = [x for r in results for x in r["latencies"]]
    connected = sum(r["alive"] for r in results)
    # A broadcast reaches every worker; count those seen by all of them.
    tick_sets = [set(r["ticks"]) for r in results if r["alive"]]
    ticks = set.intersection(*tick_sets) if tick_sets else set()
    expected = len(ticks) * connected
    emit({
        "clients": n,
        "connected": connected,
        "broadcasts": len(ticks),
        "events": len(lat),
        "delivered_ratio": round(len(lat) / expected, 4) if expected else 0.0,
        "p50_ms": round(pct(lat, 0.50), 2),
        "p99_ms": round(pct(lat, 0.99), 2),
        "max_ms": round(max(lat), 2) if lat else float("nan"),
    })


if __name__ == "__main__":
    main()
