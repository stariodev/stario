"""HTTP testing: the production protocol on an in-memory connection, no port.

`TestClient` turns each call into HTTP/1.1 bytes and feeds them to the same
`HttpProtocol` that `serve()` runs. Routing, target validation (308 /
400), framing, compression, the request span, `c.disconnect`: all production
code. The client only builds requests and parses responses.

Public surface: `TestClient`, `TestResponse`, `TestStreamResponse`,
`TestTracer`, `aload_app` (bootstrap loading for integration tests),
`VirtualTimeLoop` (an event loop whose clock jumps over timer waits), and
`check_pages` / `check_page` (every HTML page checked for 5xx, broken links
and duplicate ids).
Tests need an async runner (for example pytest-asyncio).
"""

from stario.testing.client import TestClient
from stario.testing.load import aload_app
from stario.testing.pages import HtmlPage, PageProblems, check_page, check_pages
from stario.testing.response import TestResponse, TestStreamResponse
from stario.testing.tracer import TestTracer
from stario.testing.vtime import VirtualTimeLoop

__all__ = [
    "HtmlPage",
    "PageProblems",
    "TestClient",
    "TestResponse",
    "TestStreamResponse",
    "TestTracer",
    "VirtualTimeLoop",
    "aload_app",
    "check_page",
    "check_pages",
]
