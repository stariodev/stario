"""
Shared counter with Jinja templates, Datastar, and SSE.

Run with: uv run python -m main
"""

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from stario import App, Context, Relay, Route, Span, Writer
from stario.datastar import DATASTAR_CDN_URL, SSE

HOME = Route("GET", "/")
LIVE = Route("GET", "/live")
INCREMENT = Route("POST", "/increment")
RESET = Route("POST", "/reset")

# Shared changes publish {} here; the stream re-renders the counter.
CHANGED = "counter.changed"

templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
)


class Counter:
    """In-memory counter state for this demo process."""

    def __init__(self, count: int = 0) -> None:
        self.count = count

    def increment(self) -> None:
        self.count += 1

    def reset(self) -> None:
        self.count = 0


def counter_view(counter: Counter) -> str:
    return templates.get_template("counter.html.jinja").render(count=counter.count)


@dataclass(frozen=True, slots=True)
class CounterPage:
    counter: Counter
    relay: Relay[dict]

    def register(self, app: App) -> None:
        app.add(HOME, self.home)
        app.add(LIVE, self.live)
        app.add(INCREMENT, self.increment)
        app.add(RESET, self.reset)

    async def home(self, c: Context, w: Writer) -> None:
        page = templates.get_template("home.html.jinja").render(
            title="Shared counter",
            count=self.counter.count,
            datastar_url=DATASTAR_CDN_URL,
            subscribe_url=LIVE.href(),
            increment_url=INCREMENT.href(),
            reset_url=RESET.href(),
        )
        w.html(page, trusted=True)

    async def live(self, c: Context, w: Writer) -> None:
        # Subscribe before the first render so updates cannot fall into a gap.
        async with self.relay.subscribe(CHANGED, latest=True) as events:
            sse = SSE(w)
            sse.patch_elements(counter_view(self.counter), trusted=True)
            async for _ in c.alive(events):
                sse.patch_elements(counter_view(self.counter), trusted=True)

    async def increment(self, c: Context, w: Writer) -> None:
        w.empty()
        self.counter.increment()
        self.relay.publish(CHANGED, {})

    async def reset(self, c: Context, w: Writer) -> None:
        w.empty()
        self.counter.reset()
        self.relay.publish(CHANGED, {})


async def bootstrap(app: App, span: Span):
    span.attr("app.name", "jinja")
    counter = Counter()
    relay: Relay[dict] = Relay()

    CounterPage(counter, relay).register(app)
    yield


if __name__ == "__main__":
    import asyncio

    from stario import serve

    asyncio.run(serve(bootstrap))
