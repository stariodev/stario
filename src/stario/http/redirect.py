"""Safe redirect target validation and encoding."""

from urllib.parse import quote, urlsplit

from stario.exceptions import StarioError


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


__all__ = ["normalized_location"]
