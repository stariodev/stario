"""Connected-users client: N keep-alive connections, each GETs /page every 2 s.

    python conn_client.py 127.0.0.1 3000 10000 20

Models N browser tabs that are each active (one page view every 2 s) and
keep their connection open between requests. Reports latency, errors, and
how many connections the server still holds at the end.
"""

import asyncio
import random
import sys
import time

from _client_common import emit, pct, raise_nofile, run_workers

INTERVAL = 2.0


class Conn(asyncio.Protocol):
    def __init__(self):
        self.buf = b""
        self.waiter = None
        self.closed = False

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.buf += data
        self._try_complete()

    def _try_complete(self):
        if self.waiter is None or self.waiter.done():
            return
        head_end = self.buf.find(b"\r\n\r\n")
        if head_end < 0:
            return
        head = self.buf[:head_end].lower()
        body = self.buf[head_end + 4:]
        if b"content-length:" in head:
            length = int(head.split(b"content-length:")[1].split(b"\r\n")[0])
            if len(body) < length:
                return
            rest = body[length:]
        elif b"chunked" in head:
            end = body.find(b"\r\n0\r\n\r\n")
            if end < 0 and not body.startswith(b"0\r\n\r\n"):
                return
            rest = body[end + 7:] if end >= 0 else body[5:]
        else:
            return
        status = int(self.buf[9:12])
        self.buf = rest
        self.waiter.set_result(status)

    def connection_lost(self, exc) -> None:
        self.closed = True
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_exception(ConnectionError("closed"))


def worker(n: int, index: int, host: str, port: int, seconds: float, start_at: float) -> dict:
    raise_nofile()
    req = (f"GET /page HTTP/1.1\r\nHost: {host}:{port}\r\n"
           "User-Agent: Mozilla/5.0 bench\r\nAccept: text/html\r\n"
           "Cookie: session=9f2c1e7b4a6d8f0e3b5c7a9d1f3e5b7c\r\n\r\n").encode()
    latencies: list[float] = []
    errors = 0
    done = 0

    reconnects = 0

    async def user(conn: Conn, stop: float, make):
        # Start within one interval of connecting, so no connection sits idle
        # past a 5 s keep-alive timeout before its first request.
        nonlocal errors, done, reconnects
        loop = asyncio.get_running_loop()
        await asyncio.sleep(random.random() * INTERVAL)
        while time.time() < stop:
            if conn.closed:
                # A browser reconnects transparently; count it.
                reconnects += 1
                try:
                    conn = await asyncio.wait_for(make(0), 10)
                except (TimeoutError, OSError):
                    errors += 1
                    await asyncio.sleep(INTERVAL)
                    continue
                conns.append(conn)
            t0 = time.perf_counter()
            conn.waiter = loop.create_future()
            conn.transport.write(req)
            try:
                status = await asyncio.wait_for(conn.waiter, 10)
            except TimeoutError:
                errors += 1
                conn.transport.close()
                continue
            except ConnectionError:
                continue  # closed between requests: reconnect and retry
            if status != 200:
                errors += 1
            latencies.append((time.perf_counter() - t0) * 1000)
            done += 1
            await asyncio.sleep(max(0.0, INTERVAL - (time.perf_counter() - t0)))
        open_conns.append(conn)

    conns: list[Conn] = []
    open_conns: list[Conn] = []

    async def main():
        loop = asyncio.get_running_loop()
        sem = asyncio.Semaphore(200)

        async def make(i):
            _, proto = await loop.create_connection(Conn, host, port)
            return proto

        failed = 0
        tasks = []

        async def start(i):
            nonlocal failed
            async with sem:
                try:
                    conn = await asyncio.wait_for(make(i), 10)
                except (TimeoutError, OSError):
                    failed += 1
                    return
            conns.append(conn)
            tasks.append(asyncio.create_task(user(conn, stop, make)))

        stop = time.time() + seconds
        t0 = time.time()
        await asyncio.gather(*(start(i) for i in range(n)))
        await asyncio.gather(*tasks)
        elapsed = time.time() - t0
        open_ = sum(1 for c in open_conns if not c.closed)
        for c in conns:
            c.transport.close()
        return failed, open_, elapsed

    failed, open_, elapsed = asyncio.run(main())
    return {"latencies": latencies, "errors": errors + failed, "open": open_,
            "done": done, "elapsed": elapsed, "reconnects": reconnects}


def main() -> None:
    host, port, n, seconds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
    results = run_workers(worker, n, host, port, seconds, 0.0)
    lat = [x for r in results for x in r["latencies"]]
    elapsed = max(r["elapsed"] for r in results)
    emit({
        "clients": n,
        "open": sum(r["open"] for r in results),
        "errors": sum(r["errors"] for r in results),
        "reconnects": sum(r["reconnects"] for r in results),
        "rps": round(sum(r["done"] for r in results) / elapsed, 1),
        "p50_ms": round(pct(lat, 0.50), 2),
        "p99_ms": round(pct(lat, 0.99), 2),
    })


if __name__ == "__main__":
    main()
