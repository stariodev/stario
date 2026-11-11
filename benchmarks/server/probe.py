"""Machine-speed probe: a fixed CPU + allocation workload for ~1 s.

    taskset -c 0 python probe.py

Prints work units per second. bench.py runs it on the server CPU before
every target in every round, so a slow-down of the host (noisy neighbours,
frequency changes) shows up as a drop here rather than being blamed on
whichever framework happened to run at that moment. Standard library only.
"""

import html
import json
import sys
import time

ROWS = [
    {"id": i, "name": f'Widget "Pro" <v{i}> & Sons' if i % 7 == 3 else f"Product {i}",
     "price": f"{(i * 37) % 1000 + 0.99:.2f}"}
    for i in range(50)
]


def unit() -> int:
    parts = []
    for r in ROWS:
        parts.append(f'<tr id="row-{r["id"]}"><td>{html.escape(r["name"])}</td>'
                     f'<td>{r["price"]}</td></tr>')
    page = "".join(parts).encode()
    return len(page) + len(json.dumps(ROWS))


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    for _ in range(200):  # warm up
        unit()
    n = 0
    t0 = time.perf_counter()
    deadline = t0 + seconds
    while time.perf_counter() < deadline:
        for _ in range(50):
            unit()
        n += 50
    print(round(n / (time.perf_counter() - t0)))


if __name__ == "__main__":
    main()
