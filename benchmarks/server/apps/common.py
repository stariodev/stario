"""Data and the Jinja2 template shared by the benchmark apps.

Every target renders the same ``/page``: a product admin page with a head,
nav, a 50-row table, and a footer (about 16 KB). One row in seven carries
text that must be escaped, so autoescaping does real work. Stario builds it
with ``h.*`` (see ``stario_app.py``); every other target uses Jinja2, which
is what their docs reach for. Rows are dicts, so the template indexes them
(``r['id']``): ``r.id`` would make Jinja2 try ``getattr`` first on every
lookup, which roughly doubles its render time. ``bench.py`` checks that all targets produce
the same document before it measures anything.

Standard library only: imported from every target's virtual environment.
"""

import os

HELLO = "Hello, World!"

# BENCH_COMPRESS=1 turns on each framework's response compression (where it
# has one); bench.py starts a separate server process for that phase.
COMPRESS = os.environ.get("BENCH_COMPRESS") == "1"
# Interval between SSE broadcasts, in seconds.
SSE_INTERVAL = float(os.environ.get("BENCH_SSE_INTERVAL", "0.5"))

NAV = [
    ("/", "Dashboard"),
    ("/products", "Products"),
    ("/orders", "Orders"),
    ("/customers", "Customers"),
    ("/settings", "Settings"),
]

STATUSES = ("active", "draft", "archived")


def _name(i: int) -> str:
    if i % 7 == 3:
        return f'Widget "Pro" <v{i}> & Sons'
    return f"Product {i}"


ROWS = [
    {
        "id": i,
        "sku": f"SKU-{i:05d}",
        "name": _name(i),
        "desc": f"Hand-made item number {i}, ships in {i % 5 + 1} days",
        "price": f"{(i * 37) % 1000 + 0.99:.2f}",
        "status": STATUSES[i % 3],
    }
    for i in range(50)
]

PAGE_TEMPLATE = (
    '<html lang="en"><head><meta charset="utf-8"/>'
    '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
    "<title>Products · Admin</title>"
    '<link rel="stylesheet" href="/static/app.css"/>'
    '<script type="module" src="/static/datastar.js"></script></head>'
    '<body class="min-h-screen bg-base"><nav class="navbar">'
    '{% for href, label in nav %}<a href="{{ href }}" class="nav-link">{{ label }}</a>{% endfor %}'
    '</nav><main class="container mx-auto p-6"><h1 class="title">Products</h1>'
    '<table class="table"><thead><tr><th>SKU</th><th>Name</th><th>Description</th>'
    "<th>Price</th><th>Status</th><th></th></tr></thead><tbody>"
    "{% for r in rows %}"
    '<tr id="row-{{ r["id"] }}" class="row row-{{ r["status"] }}">'
    '<td class="mono">{{ r["sku"] }}</td><td>{{ r["name"] }}</td><td class="muted">{{ r["desc"] }}</td>'
    '<td class="num">${{ r["price"] }}</td>'
    '<td><span class="badge badge-{{ r["status"] }}">{{ r["status"] }}</span></td>'
    '<td><a href="/products/{{ r["id"] }}" class="btn btn-sm">Edit</a></td></tr>'
    "{% endfor %}"
    '</tbody></table></main><footer class="footer"><p>50 products</p></footer></body></html>'
)


def jinja_page():
    """Return a zero-argument callable that renders the page with Jinja2."""
    import jinja2

    template = jinja2.Environment(autoescape=True).from_string(PAGE_TEMPLATE)
    return lambda: template.render(nav=NAV, rows=ROWS)


def sse_fragment(tick: int, ts_ns: int) -> str:
    """The HTML fragment every SSE client receives per broadcast.

    The send timestamp sits in ``data-ts`` so the client can measure
    delivery latency on the same machine clock.
    """
    return (
        f'<div id="ticker" data-ts="{ts_ns}"><span class="tick">{tick}</span>'
        f'<span class="label">orders today</span></div>'
    )


class Hub:
    """In-process broadcast: one asyncio.Queue per subscriber.

    The non-Stario targets use this for SSE fan-out; Stario uses its own
    ``Relay``. A full queue drops the oldest event, so a slow client
    cannot grow memory without bound.
    """

    def __init__(self) -> None:
        self.queues = set()

    def subscribe(self):
        import asyncio

        queue = asyncio.Queue(maxsize=16)
        self.queues.add(queue)
        return queue

    def unsubscribe(self, queue) -> None:
        self.queues.discard(queue)

    def publish(self, item) -> None:
        for queue in self.queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)


async def ticker(publish) -> None:
    """Publish a (tick, ts_ns) pair every SSE_INTERVAL seconds."""
    import asyncio
    import time

    tick = 0
    while True:
        await asyncio.sleep(SSE_INTERVAL)
        tick += 1
        publish((tick, time.time_ns()))


def datastar_event(fragment: str) -> bytes:
    """One Datastar ``patch-elements`` SSE event, as the SDKs frame it."""
    return f"event: datastar-patch-elements\ndata: elements {fragment}\n\n".encode()
