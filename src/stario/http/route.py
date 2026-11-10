"""HTTP address: one method on one path.

`Route("GET /home")` or `Route("POST", ROOM + "/send")`. Positionals
join as `" ".join(parts)` → `<METHOD> <url>`. `method=`, `host=`, and
`path=` are either in that string or in the keyword, not both.

`href()` compiles a join of literals and placeholder names.
`.pattern` is the address line and the span name. Pass `query=` and
`fragment=` to `href()` only. Host and path are parsed once at construct.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import NoReturn, cast
from urllib.parse import quote, urlencode

from stario.exceptions import StarioError
from stario.http.segment import Segment, parse_route

HTTP_METHODS = ("GET", "QUERY", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


def normalize_path(path: str) -> str:
    """Canonical URL path: leading `/`, no trailing slash (`/` alone for root).

    Leading and trailing slashes are stripped before re-prefixing, so runs of
    leading slashes collapse (`//host/` → `/host`). Internal `//` segments
    are preserved.
    """
    return "/" + path.strip("/")


def split_target(target: str, *, host: str | None = None) -> tuple[str, str | None]:
    """Split `/path` or `//host/path` into `(path, host)`."""
    if not target.startswith("/"):
        raise StarioError(
            "Path must start with '/' or '//'",
            context={"path": target},
            help_text="Use '/users' or '//api.example.com/users'.",
        )
    parsed_host: str | None = None
    path = target
    if target.startswith("//"):
        rest = target[2:]
        slash = rest.find("/")
        if slash < 0:
            parsed_host, path = rest, "/"
        else:
            parsed_host, path = rest[:slash], rest[slash:]
        if not parsed_host:
            raise StarioError(
                "Host must not be empty",
                context={"path": target},
                help_text="Use '//api.example.com/users', not '///users'.",
            )
    if host is not None and parsed_host is not None:
        raise StarioError(
            "Host given twice",
            context={"path": target, "host": host},
            help_text="Use '//host/path' or host=, not both.",
        )
    return path, host if parsed_host is None else parsed_host


def public_prefix(path: str) -> str:
    """Canonical `/path` or `//host/path` for file hrefs. No placeholders."""
    split_target(path)
    if "{" in path:
        raise StarioError(
            "URL prefix must not contain placeholders",
            context={"url_prefix": path},
            help_text='Use a fixed prefix such as "/static".',
        )
    if path != "/" and path.endswith("/"):
        return path.rstrip("/")
    return path


def append_query_fragment(
    href: str,
    *,
    query: Mapping[str, object] | None = None,
    fragment: str | None = None,
) -> str:
    """Append optional query string and fragment to `href`."""
    if query is not None:
        pairs = [(key, value) for key, value in query.items() if value is not None]
        if pairs:
            href = f"{href}?{urlencode(pairs, doseq=True)}"
    if fragment is not None:
        href = f"{href}#{quote(fragment, safe="/?:@!$&'()*+,;=")}"
    return href


def _method_token(method: str) -> str:
    token = method.strip().upper()
    if not token or any(ch.isspace() for ch in token):
        raise StarioError(
            "Route method must be a single HTTP token",
            context={"method": method},
        )
    return token


def _value_error(
    message: str,
    name: str,
    *,
    value: object | None = None,
) -> NoReturn:
    context: dict[str, object] = {"parameter": name}
    if value is not None:
        context["value"] = value
    raise StarioError(message, context=context)


def _require(value: object, name: str, *, where: str) -> str:
    if value is None:
        _value_error(f"Route {where} parameter must not be None", name)
    text = str(value)
    if not text:
        _value_error(f"Route {where} parameter must not be empty", name)
    return text


def _enc_path(value: object, name: str) -> str:
    text = _require(value, name, where="path")
    if "/" in text:
        _value_error("Route path parameter contains '/'", name, value=text)
    # A browser resolves `.` and `..` in a link; the server answers 308.
    if text == "." or text == "..":
        _value_error("Route path parameter is a dot segment", name, value=text)
    return quote(text, safe="")


def _enc_catchall_path(value: object, name: str) -> str:
    text = _require(value, name, where="path")
    parts = text.split("/")
    if "" in parts:
        _value_error(
            "Route catchall parameter contains empty path segment",
            name,
            value=text,
        )
    if "." in parts or ".." in parts:
        _value_error(
            "Route catchall parameter contains a dot segment", name, value=text
        )
    return "/".join(quote(part, safe="") for part in parts)


def _host_text(value: object, name: str) -> str:
    text = _require(value, name, where="host").lower()
    if any(ch in text for ch in "/:@[]?#\\ \t\r\n"):
        _value_error(
            "Route host parameter contains invalid character", name, value=text
        )
    return text


def _enc_host(value: object, name: str) -> str:
    text = _host_text(value, name)
    if "." in text:
        _value_error("Route host parameter contains '.'", name, value=text)
    return quote(text, safe="")


def _enc_catchall_host(value: object, name: str) -> str:
    text = _host_text(value, name)
    labels = text.split(".")
    if "" in labels:
        _value_error("Route host parameter contains empty host label", name, value=text)
    return ".".join(quote(part, safe="") for part in labels)


_HREF_NS: dict[str, object] = {
    "P": _enc_path,
    "C": _enc_catchall_path,
    "H": _enc_host,
    "K": _enc_catchall_host,
}


def _compile_href(
    host_segments: tuple[Segment, ...],
    path_segments: tuple[Segment, ...],
    target: str,
) -> Callable[..., str] | None:
    names: list[str] = []
    pieces: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        text = "".join(pending)
        pending.clear()
        pieces.append(repr(text))

    def literal(text: str) -> None:
        pending.append(text)

    def param(segment: Segment, where: str) -> None:
        flush()
        names.append(segment.name)
        slot = "K" if where == "host" else "C"
        if segment.kind != "catchall":
            slot = "H" if where == "host" else "P"
        pieces.append(f"{slot}({segment.name},{segment.name!r})")

    if host_segments:
        literal("//")
        for i, segment in enumerate(host_segments):
            if i:
                literal(".")
            if segment.kind == "exact":
                literal(segment.name)
            else:
                param(segment, "host")
    if not path_segments:
        literal("/")
    else:
        for segment in path_segments:
            literal("/")
            if segment.kind == "exact":
                literal(segment.name)
            else:
                param(segment, "path")

    if not names:
        return None
    flush()
    source = (
        f"def href({', '.join(names)}):\n    return ''.join(({', '.join(pieces)},))\n"
    )
    namespace = dict(_HREF_NS)
    exec(compile(source, f"<stario href {target}>", "exec"), namespace)
    return cast(Callable[..., str], namespace["href"])


def _fill_href(
    fn: Callable[..., str],
    args: tuple[object, ...],
    params: Mapping[str, object],
    target: str,
) -> str:
    names = fn.__code__.co_varnames[: fn.__code__.co_argcount]
    if len(args) > len(names):
        raise StarioError(
            "Route too many positional parameters",
            context={"target": target},
        )
    for key in params:
        if key not in names:
            raise StarioError(
                "Route unknown parameter",
                context={"target": target},
            )
    for i, name in enumerate(names):
        if i < len(args):
            if name in params:
                raise StarioError(
                    "Route parameter given more than once",
                    context={"target": target},
                )
        elif name not in params:
            raise StarioError(
                "Route parameter missing",
                context={"target": target},
            )
    return fn(*args, **params)


class Route:
    """One HTTP method on one path template.

    ```python
    HOME = Route("GET /")
    SEND = Route("POST", ROOM + "/send")

    app.add(SEND, send)
    at.post(SEND.href(room.id))
    ```
    """

    __slots__ = (
        "_host_segments",
        "_href",
        "_path_segments",
        "host",
        "method",
        "path",
        "pattern",
        "target",
    )

    method: str
    path: str
    host: str | None
    target: str
    pattern: str
    _host_segments: tuple[Segment, ...]
    _path_segments: tuple[Segment, ...]
    _href: Callable[..., str] | None

    def __init__(
        self,
        *parts: str,
        method: str | None = None,
        host: str | None = None,
        path: str | None = None,
    ) -> None:
        if parts and not parts[0].strip():
            _method_token(parts[0])

        joined = " ".join(parts).strip()
        if not joined:
            method_text, location = "", ""
        elif joined.startswith("/"):
            method_text, location = "", joined
        else:
            method_text, _sep, rest = joined.partition(" ")
            location = rest.strip()
        if method is not None and method_text:
            raise StarioError("Method given twice")
        if method is not None:
            token = _method_token(method)
        elif method_text:
            token = _method_token(method_text)
        else:
            token = ""

        path_text = ""
        parsed_host: str | None = None

        if path is not None:
            if location:
                raise StarioError("Path given twice")
            location = path

        if "?" in location or "#" in location:
            raise StarioError(
                "Route does not store query or fragment",
                context={"location": location},
                help_text="Pass query= and fragment= to href() only.",
            )

        if location:
            path_text, parsed_host = split_target(location, host=host)
            path_text = normalize_path(path_text)

        if not path_text:
            raise StarioError("Route needs a path", context={"spec": joined})

        host_segments, path_segments = parse_route(parsed_host, path_text)
        path_text = (
            "/" + "/".join(segment.pattern for segment in path_segments)
            if path_segments
            else "/"
        )
        host_text = (
            ".".join(segment.pattern for segment in host_segments)
            if host_segments
            else None
        )
        target = f"//{host_text}{path_text}" if host_text is not None else path_text
        object.__setattr__(self, "method", token)
        object.__setattr__(self, "path", path_text)
        object.__setattr__(self, "_host_segments", host_segments)
        object.__setattr__(self, "_path_segments", path_segments)
        object.__setattr__(self, "host", host_text)
        object.__setattr__(self, "target", target)
        object.__setattr__(
            self, "pattern", target if not token else f"{token} {target}"
        )
        object.__setattr__(
            self, "_href", _compile_href(host_segments, path_segments, target)
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Route is immutable")

    @property
    def host_segments(self) -> tuple[Segment, ...]:
        return self._host_segments

    @property
    def path_segments(self) -> tuple[Segment, ...]:
        return self._path_segments

    @property
    def has_params(self) -> bool:
        return self._href is not None

    def href(
        self,
        *args: object,
        query: Mapping[str, object] | None = None,
        fragment: str | None = None,
        **params: object,
    ) -> str:
        """Browser URL string. Placeholders are strings; all must be provided."""
        if self is EMPTY_ROUTE:
            raise StarioError("Cannot build an href from the unmatched route")
        fn = self._href
        if fn is None:
            if args or params:
                raise StarioError(
                    "Route unknown parameter"
                    if params
                    else "Route too many positional parameters",
                    context={"target": self.target},
                )
            built = self.target
        else:
            for arg in args:
                if isinstance(arg, Mapping):
                    raise StarioError(
                        "Route path values must not be a mapping",
                        context={"target": self.target},
                    )
            built = _fill_href(fn, args, params, self.target)
        if query is None and fragment is None:
            return built
        return append_query_fragment(built, query=query, fragment=fragment)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Route)
            and self.method == other.method
            and self.path == other.path
            and self.host == other.host
        )

    def __hash__(self) -> int:
        return hash((self.method, self.path, self.host))

    def __repr__(self) -> str:
        if self is EMPTY_ROUTE:
            return "Route.empty"
        return f"Route({self.pattern!r})"

    @classmethod
    def empty(cls) -> Route:
        """Unmatched 404 / 405 sentinel. Do not register it."""
        return EMPTY_ROUTE


EMPTY_ROUTE = Route.__new__(Route)
object.__setattr__(EMPTY_ROUTE, "method", "")
object.__setattr__(EMPTY_ROUTE, "host", None)
object.__setattr__(EMPTY_ROUTE, "path", "")
object.__setattr__(EMPTY_ROUTE, "target", "")
object.__setattr__(EMPTY_ROUTE, "pattern", "")
object.__setattr__(EMPTY_ROUTE, "_href", None)
object.__setattr__(EMPTY_ROUTE, "_host_segments", ())
object.__setattr__(EMPTY_ROUTE, "_path_segments", ())
