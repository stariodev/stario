"""Safe redirect target validation and encoding."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from stario.exceptions import StarioError

if TYPE_CHECKING:
    from stario.http.writer import Writer


def normalized_location(url: str) -> str:
    """Validate a redirect target and return a percent-encoded URL string."""
    target = str(url)
    if "\r" in target or "\n" in target:
        raise StarioError(
            "Redirect target must not contain control characters",
            context={"url": target},
            help_text="Remove CR/LF characters from redirect and navigation targets.",
        )
    if "\\" in target:
        raise StarioError(
            "Redirect target must not contain backslashes",
            context={"url": target},
            help_text="Use forward slashes in redirect targets.",
        )

    parsed = urlsplit(target)
    if target.startswith("/"):
        pass  # app-relative path or scheme-relative host href (`//host/path`)
    elif parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise StarioError(
                "Redirect target must be an app-relative path or absolute http(s) URL",
                context={"url": target},
                help_text="Use '/path', 'https://…', or 'http://…' for redirect targets.",
            )
    else:
        raise StarioError(
            "Redirect target must be an app-relative path or absolute http(s) URL",
            context={"url": target},
            help_text="Relative redirect paths must start with '/'.",
        )

    return quote(target, safe=":/%#?=@[]!$&'()*+,;")


def datastar_navigate(w: Writer, location: str) -> None:
    """Answer a Datastar request's redirect: an SSE `navigate`, then end.

    `w.redirect()` calls this when the request carries `Datastar-Request`.
    Datastar's fetch follows a 3xx itself and patches the page with the
    answer, so the address bar would never change; `navigate` sets
    `window.location` instead. An SSE stream the handler already opened
    gets the event as its last one.
    """
    from stario.datastar.sse import SSE

    SSE(w).navigate(location)
    w.end()


__all__ = ["datastar_navigate", "normalized_location"]
