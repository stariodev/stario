"""HTTP stack for Stario apps and servers.

**Address and table** — `Route`, `Match`, and `Router`. `App` subclasses
`Router` and tracks tasks. The protocol runs `find_handler` then the
matched handler as a task.

**Message** — `Request` (read-only `RequestHeaders`), `Writer`, `Headers`,
`ParsedQuery` for one HTTP exchange.

**Process** — `await serve(bootstrap, …)` on a running loop, or
`asyncio.run(stario.serve(bootstrap))` / `uvloop.run(...)`; `ServerConfig`
(also importable from `stario`).
Lower-level embedding imports the submodules:

```python
import asyncio
from stario import serve
from stario.http.bootstrap import bootstrap_run
from stario.http.compression import CompressionConfig
from stario.http.config import RequestPolicy, ServerConfig, server_config_from_env
from stario.http.redirect import normalized_location
from stario.http.server import Server

asyncio.run(serve(bootstrap))
```

Filesystem serving lives in `stario.filesystem` (`Files`).
For tests, import `aload_app` from `stario.testing`.
"""

from stario.http.app import App
from stario.http.bootstrap import AppRun, run_app
from stario.http.config import ServerConfig
from stario.http.context import Context, Handler, Match, Middleware
from stario.http.dispatch import Router, default_not_found, method_not_allowed_handler
from stario.http.headers import Headers, RequestHeaders
from stario.http.middleware import catch_errors, same_origin, security_headers
from stario.http.query import ParsedQuery
from stario.http.redirect import normalized_location
from stario.http.request import ParsedCookies, Request
from stario.http.route import Route
from stario.http.writer import Writer

__all__ = [
    "App",
    "AppRun",
    "Context",
    "Handler",
    "Headers",
    "Match",
    "Middleware",
    "ParsedCookies",
    "ParsedQuery",
    "Request",
    "RequestHeaders",
    "Route",
    "Router",
    "ServerConfig",
    "Writer",
    "catch_errors",
    "default_not_found",
    "method_not_allowed_handler",
    "normalized_location",
    "run_app",
    "same_origin",
    "security_headers",
]
