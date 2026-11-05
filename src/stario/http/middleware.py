"""Middleware helpers for handler wrappers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Literal
from urllib.parse import quote

from stario.exceptions import StarioError
from stario.http.context import Context, Handler, Middleware
from stario.http.request import Request
from stario.http.writer import Writer

type ErrorResponder = Callable[[Context, Writer, BaseException], Awaitable[None]]


def catch_errors(
    *exc_types: type[BaseException],
    respond: ErrorResponder,
) -> Middleware:
    """Wrap a handler so listed exceptions become HTTP responses when nothing was sent.

    If the handler already started a response (`w.started`), the exception is
    re-raised so the framework can abort the in-flight body.
    """
    if not exc_types:
        raise StarioError(
            "catch_errors requires at least one exception type",
            help_text="Pass one or more exception classes, e.g. catch_errors(MyError, respond=...).",
        )

    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            try:
                await handler(c, w)
            except exc_types as exc:
                if w.started:
                    raise
                await respond(c, w, exc)

        return wrapped

    return middleware


_FETCH_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_FETCH_SITES_OK = frozenset({"same-origin", "none"})
_FETCH_SITES_OK_SAME_SITE = _FETCH_SITES_OK | {"same-site"}


def _authority(value: str) -> str:
    """`host[:port]`, lowercased, default ports stripped; "" when no host."""
    value = value.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    # Authority is up to the first `/`; drop any userinfo just in case.
    authority = value.split("/", 1)[0].rsplit("@", 1)[-1].lower()
    if authority.endswith(":80") or authority.endswith(":443"):
        authority = authority.rsplit(":", 1)[0]
    return authority


def _origin(value: str) -> str:
    """`scheme://host[:port]` lowercased, default port stripped; "" if not one."""
    scheme, sep, rest = value.strip().lower().partition("://")
    if not sep or scheme not in ("http", "https") or not rest or "/" in rest:
        return ""
    authority = rest.rsplit("@", 1)[-1]
    default = ":443" if scheme == "https" else ":80"
    if authority.endswith(default):
        authority = authority[: -len(default)]
    return f"{scheme}://{authority}" if authority else ""


def _exempt_prefixes(paths: Iterable[str]) -> tuple[bytes, ...]:
    # Compared with the raw path: the decoded path of `/hooks%2Fx` starts
    # with `/hooks/`, but the router matches it as one segment elsewhere.
    out: list[bytes] = []
    for path in paths:
        if not path.startswith("/"):
            raise StarioError(
                "same_origin exempt paths must start with '/'",
                context={"path": path},
            )
        out.append(quote(path.rstrip("/"), safe="/!$&'()*+,;=:@").encode("ascii"))
    return tuple(out)


def same_origin(
    *,
    allow_same_site: bool = False,
    trusted_origins: Iterable[str] = (),
    exempt: Iterable[str] = (),
) -> Middleware:
    """CSRF protection: reject cross-origin state-changing requests (403).

    Browsers send `Sec-Fetch-Site` (all major ones since 2023) and `Origin`,
    and a page cannot set either one. So a form or `fetch` on another site
    cannot post here with the user's cookies. This needs no tokens and no
    state. Use it on every app with a cookie login:

    ```python
    app.use("/", same_origin(exempt=["/hooks"]))
    ```

    For methods other than GET/HEAD/OPTIONS/TRACE:

    - `Sec-Fetch-Site` present: `same-origin` and `none` pass (`same-site`
      too with `allow_same_site=True`).
    - `Sec-Fetch-Site` absent, `Origin` present: the Origin authority must
      equal the `Host` authority; `null` fails.
    - Neither header: pass. The client is not a browser (curl, a webhook from
      a server), so it has no cookies of the user to misuse.
    - `trusted_origins` (`"https://pay.example.com"`): a request whose
      `Origin` is in the list passes, for a cross-site form that may post
      here (a payment return).
    - `exempt` (`"/hooks"`): requests under these path prefixes skip the
      check (`/hooks` covers `/hooks` and `/hooks/...`, not `/hooksx`). The
      prefix matches the path as sent, so `/hooks%2Fx` is not exempt.

    The check does not cover GET: a GET must never change state. The only
    gap is a browser older than about 2023 with neither header; CSRF tokens
    would add only that.
    """
    trusted: set[str] = set()
    for value in trusted_origins:
        origin = _origin(value)
        if not origin:
            raise StarioError(
                "same_origin trusted_origins must be origins like 'https://host[:port]'",
                context={"origin": value},
            )
        trusted.add(origin)
    prefixes = _exempt_prefixes(exempt)
    allowed_sites = _FETCH_SITES_OK_SAME_SITE if allow_same_site else _FETCH_SITES_OK

    def blocked(req: Request) -> bool:
        if req.method.upper() in _FETCH_SAFE_METHODS:
            return False
        if prefixes:
            raw = req.raw_path
            if any(raw == p or raw.startswith(p + b"/") for p in prefixes):
                return False
        fetch_site = req.headers.get("sec-fetch-site")
        origin = req.headers.get("origin")
        if fetch_site is not None:
            cross = fetch_site.lower() not in allowed_sites
        elif origin is not None:
            host = req.headers.get("host") or ""
            cross = origin.strip() == "null" or _authority(origin) != _authority(host)
        else:
            return False
        return cross and (origin is None or _origin(origin) not in trusted)

    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            if blocked(c.req):
                w.text("Cross-origin request blocked", 403)
                return
            await handler(c, w)

        return wrapped

    return middleware


def security_headers(
    *,
    frame: Literal["DENY", "SAMEORIGIN"] | None = "DENY",
    referrer: str | None = "strict-origin-when-cross-origin",
    hsts: int | None = None,
) -> Middleware:
    """Set baseline security response headers before the handler runs.

    Sets `x-content-type-options: nosniff` always; `x-frame-options` to
    `frame` and `referrer-policy` to `referrer` unless `None`;
    `strict-transport-security: max-age=<hsts>; includeSubDomains` when
    `hsts` (seconds) is given — use it only when the whole site is HTTPS.
    Headers are set before `handler` runs, so a handler may override one.
    """
    if frame is not None and frame not in ("DENY", "SAMEORIGIN"):
        raise StarioError(
            "security_headers frame must be 'DENY', 'SAMEORIGIN' or None",
            context={"frame": frame},
        )
    if hsts is not None and hsts < 0:
        raise StarioError(
            "security_headers hsts must be >= 0",
            context={"hsts": hsts},
        )

    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            w.headers.set("x-content-type-options", "nosniff")
            if frame is not None:
                w.headers.set("x-frame-options", frame)
            if referrer is not None:
                w.headers.set("referrer-policy", referrer)
            if hsts is not None:
                w.headers.set(
                    "strict-transport-security",
                    f"max-age={hsts}; includeSubDomains",
                )
            await handler(c, w)

        return wrapped

    return middleware
