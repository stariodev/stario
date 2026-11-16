"""Datastar action strings.

Use the exported `at` instance:

```python
from stario import Route
from stario.datastar import at, data

REFRESH = Route("GET /items")
h.Button(data.on("click", at.get(REFRESH.href())), "Refresh")
```

The methods return strings such as `@get('/items')` for use inside Datastar
attributes like `data.on(...)`. Build the URL with `route.href()` (with
`query=` for query parameters), then pick `at.get` / `at.post` / … at the
call site. The same call with the same arguments returns the same string
from a cache.

`include=` / `exclude=` take a list of signal paths (each matches itself and
its children) or a `str` regex. An `exclude=` never lets a local `_` signal
leave the browser. Retry times are durations with a unit: `"2s"`.
Datastar Pro actions (`clipboard`, `intl`, `fit`) say so in their docstring.
"""

import math
import warnings
from collections.abc import Mapping
from typing import Any, Literal, TypedDict, Unpack, cast
from urllib.parse import urlencode

from stario.exceptions import StarioError
from stario_cython.markup import fragment_cache, remember_str

from .format import (
    Duration,
    FilterValue,
    SignalValue,
    duration_ms,
    filter_js,
    js_object,
    literal,
    string_literal,
)

# Action strings by (method, url, queries, options); see remember_str.
_FETCH = fragment_cache()

ContentType = Literal["json", "form"]
RequestCancellation = Literal["auto", "cleanup", "disabled"]
Retry = Literal["auto", "error", "always", "never"]
IntlType = Literal[
    "datetime", "number", "pluralRules", "relativeTime", "list", "displayNames"
]


class FetchOptions(TypedDict, total=False):
    """Keyword options shared by `get`, `post`, `put`, `patch`, `delete`, and `query`.

    Each key maps to one option of Datastar's backend fetch actions. `None`
    omits the option, like leaving it out.
    """

    content_type: ContentType | str | None
    include: FilterValue | None
    exclude: FilterValue | None
    selector: str | None
    headers: dict[str, str] | None
    open_when_hidden: bool | None
    payload: Mapping[str, SignalValue] | str | None
    retry: Retry | str | None
    retry_interval: Duration | None
    retry_scaler: float | None
    retry_max_wait: Duration | None
    retry_max_count: int | None
    request_cancellation: RequestCancellation | str | None
    retry_interval_ms: int | None
    """Deprecated: write `retry_interval="1s"`. Removed in 6.0."""
    retry_max_wait_ms: int | None
    """Deprecated: write `retry_max_wait="30s"`. Removed in 6.0."""


def _js_number(option: str, value: object, *, integer: bool = False) -> str:
    """A number for the JS source: an int (or a finite float unless `integer`).

    These options are pasted into the action expression as they are, so a
    `str` here would be JavaScript.
    """
    value_type = type(value)
    if value_type is int or (
        not integer and value_type is float and math.isfinite(cast(float, value))
    ):
        return str(value)
    raise StarioError(
        f"Datastar option {option} must be "
        + ("an int" if integer else "a finite int or float")
        + f", not {value_type.__name__}",
        context={"option": option, "value": repr(value)[:100]},
    )


def _quote_strings(value: Any) -> Any:
    """`value` with every str, at any depth, as a JS string literal."""
    if isinstance(value, str):
        return string_literal(value)
    if isinstance(value, dict):
        return {k: _quote_strings(v) for k, v in cast(dict[Any, Any], value).items()}
    if isinstance(value, (list, tuple)):
        return [_quote_strings(v) for v in cast(list[Any], value)]
    return value


def _deprecated(old: str, new: str, stacklevel: int) -> None:
    """`stacklevel` counts from the caller of `_deprecated` (1 = that caller)."""
    warnings.warn(
        f"{old} is deprecated: {new}. Removed in 6.0.",
        DeprecationWarning,
        stacklevel=stacklevel + 1,
    )


def _frozen(value: object) -> object:
    """`value` as a cache key part: a dict or list becomes a tuple.

    One level deep: a nested list or dict stays unhashable, so that call is
    built every time instead of cached.
    """
    kind = type(value)
    if kind is dict:
        return (dict, *cast(dict[object, object], value).items())
    if kind is list:
        return (list, *cast(list[object], value))
    return value


def _retry_ms(
    options: FetchOptions, name: Literal["retry_interval", "retry_max_wait"]
) -> int | None:
    """`retry_interval` / `retry_max_wait` in milliseconds, from either spelling."""
    value = options.get(name)
    old_name = name + "_ms"
    old = options.get(old_name)
    if old is not None:
        if value is not None:
            raise StarioError(
                f"Pass {name}= or {old_name}=, not both",
                help_text=f"Write {name}='2s' and drop {old_name}.",
            )
        # user -> at.get -> _fetch -> _build -> _retry_ms
        _deprecated(f"{old_name}={old!r}", f"write {name}='{old}ms'", 5)
        return int(_js_number(old_name, old, integer=True))
    if value is None:
        return None
    return duration_ms(value, name)


class DatastarActions:
    """Namespace for Datastar `@...` action strings."""

    __slots__ = ()

    def peek(self, callable_expr: str) -> str:
        """Build `@peek(expr)` to read a value without subscribing."""
        return f"@peek({callable_expr})"

    def set_all(
        self,
        value: str,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> str:
        """Build `@setAll(...)` to assign matching signals in bulk."""
        filters = filter_js(include, exclude)
        if filters is not None:
            return f"@setAll({value}, {filters})"
        return f"@setAll({value})"

    def toggle_all(
        self,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> str:
        """Build `@toggleAll(...)` to flip matching boolean signals."""
        filters = filter_js(include, exclude)
        if filters is not None:
            return f"@toggleAll({filters})"
        return "@toggleAll()"

    def _fetch(
        self,
        method: str,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build a Datastar backend fetch action (see `get` for the options)."""
        if not options and queries is None:
            cache_key: tuple[object, ...] = (method, url)
            hit = _FETCH.get(cache_key)
            if hit is not None:
                return hit
        else:
            # A list or dict value (include=[...], headers={...}) is copied
            # into a tuple; scalar options (retry="always") hash as they are.
            plain = queries is None
            if plain:
                for value in options.values():
                    kind = type(value)
                    if kind is list or kind is dict:
                        plain = False
                        break
            if plain:
                cache_key = (method, url, *options.items())
            else:
                cache_key = (
                    method,
                    url,
                    _frozen(queries),
                    *[(name, _frozen(value)) for name, value in options.items()],
                )
            try:
                hit = _FETCH.get(cache_key)
            except TypeError:  # a value that cannot be a key: build, do not cache
                hit = None
                cache_key = ()
            if hit is not None:
                return hit
        text = self._build(method, url, queries, options)
        if not cache_key:
            return text
        return remember_str(_FETCH, cache_key, text)

    def _build(
        self,
        method: str,
        url: str,
        queries: Mapping[str, Any] | None,
        options: FetchOptions,
    ) -> str:
        if queries is not None:
            _deprecated(  # user -> at.get -> _fetch -> _build
                "at.get(url, queries)",
                "build the URL with ROUTE.href(query={...}); a $signal in "
                "queries was sent as literal text",
                4,
            )
        content_type = options.get("content_type")
        include = options.get("include")
        exclude = options.get("exclude")
        selector = options.get("selector")
        headers = options.get("headers")
        open_when_hidden = options.get("open_when_hidden")
        payload = options.get("payload")
        retry = options.get("retry")
        retry_interval_ms = _retry_ms(options, "retry_interval")
        retry_scaler = options.get("retry_scaler")
        retry_max_wait_ms = _retry_ms(options, "retry_max_wait")
        retry_max_count = options.get("retry_max_count")
        request_cancellation = options.get("request_cancellation")
        if selector is not None and content_type != "form":
            raise StarioError(
                "selector is only used with content_type='form'",
                context={"selector": selector, "content_type": str(content_type)},
                help_text=(
                    "The fetch `selector` option picks which <form> to submit; pass "
                    "content_type='form' alongside it, or drop the selector."
                ),
            )

        parts: list[str] = []

        if content_type is not None:
            parts.append(f"contentType: {string_literal(str(content_type))}")

        filters = filter_js(include, exclude, keep_local=True)
        if filters is not None:
            parts.append(f"filterSignals: {filters}")

        if selector is not None:
            parts.append(f"selector: {string_literal(selector)}")

        if headers is not None:
            headers_dict = {k: string_literal(v) for k, v in headers.items()}
            parts.append(f"headers: {js_object(headers_dict)}")

        if open_when_hidden is not None:
            parts.append(f"openWhenHidden: {'true' if open_when_hidden else 'false'}")

        if payload is not None:
            if include is not None or exclude is not None:
                raise StarioError(
                    "include= and exclude= have no effect with payload=",
                    context={"include": repr(include), "exclude": repr(exclude)},
                    help_text=(
                        "payload replaces the signals in the request body, and "
                        "include/exclude only filter the signals. Drop one."
                    ),
                )
            if type(payload) is str:
                # One JavaScript expression, like any str argument in at.*.
                parts.append(f"payload: {payload}")
            elif isinstance(payload, Mapping):
                # Data, like data.signals: {"title": "Milk"} is the text Milk.
                parts.append(f"payload: {literal(cast(SignalValue, payload))}")
            else:
                raise StarioError(
                    f"payload must be a dict or a str, not {type(payload).__name__}",
                    context={"payload": repr(payload)[:100]},
                    help_text=(
                        "A dict is data (sent as JSON values). A str is one "
                        "JavaScript expression, e.g. '{id: $id, title: el.value}'."
                    ),
                )

        if retry is not None:
            parts.append(f"retry: {string_literal(str(retry))}")

        if retry_interval_ms is not None:
            parts.append(f"retryInterval: {retry_interval_ms}")

        if retry_scaler is not None:
            parts.append("retryScaler: " + _js_number("retry_scaler", retry_scaler))

        if retry_max_wait_ms is not None:
            parts.append(f"retryMaxWait: {retry_max_wait_ms}")

        if retry_max_count is not None:
            parts.append(
                "retryMaxCount: "
                + _js_number("retry_max_count", retry_max_count, integer=True)
            )

        if request_cancellation is not None:
            parts.append(
                f"requestCancellation: {string_literal(str(request_cancellation))}"
            )

        full_url = url
        if queries:
            query_string = urlencode(queries, doseq=True)
            if query_string:
                separator = (
                    "" if url.endswith(("?", "&")) else "&" if "?" in url else "?"
                )
                full_url = url + separator + query_string
        if parts:
            return f"@{method}({string_literal(full_url)}, {{{', '.join(parts)}}})"
        return f"@{method}({string_literal(full_url)})"

    def get(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@get(...)`. Datastar sends the signals in the `datastar` query parameter.

        Data and code: `headers=` values and a `payload=` dict are data
        (`{"title": title}` sends the text). A `payload=` str is one
        JavaScript expression (`"{id: $id, title: el.value}"`). A payload
        replaces the signals, so it does not go with `include=` / `exclude=`.


        ```python
        SEARCH = Route("GET /items")
        h.Button(data.on("click", at.get(SEARCH.href())), "Search")
        h.Div(data.init(at.get(LIVE.href(), retry="always", retry_interval="2s")))
        ```

        `queries=` is deprecated: build the URL with `ROUTE.href(query={...})`.
        A signal travels in the signals, not in the URL.
        """
        return self._fetch("get", url, queries, **options)

    def post(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@post(...)` with the same option surface as `get`."""
        return self._fetch("post", url, queries, **options)

    def put(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@put(...)` with the same option surface as `get`."""
        return self._fetch("put", url, queries, **options)

    def patch(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@patch(...)` with the same option surface as `get`."""
        return self._fetch("patch", url, queries, **options)

    def delete(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@delete(...)` with the same option surface as `get`."""
        return self._fetch("delete", url, queries, **options)

    def query(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@query(...)` with the same option surface as `get`.

        Sends a `QUERY` request with signals in the body, like `post`.
        """
        return self._fetch("query", url, queries, **options)

    # --- Datastar Pro actions: the free bundle ignores them -----------------

    def clipboard(self, text: str, is_base64: bool = False) -> str:
        """Build `@clipboard(...)`. **Datastar Pro.**"""
        if is_base64:
            return f"@clipboard({string_literal(text)}, true)"
        return f"@clipboard({string_literal(text)})"

    def intl(
        self,
        type: IntlType | str,
        value: str,
        options: dict[str, Any] | None = None,
        locale: str | list[str] | None = None,
    ) -> str:
        """Build `@intl(...)`. **Datastar Pro.**

        `value` is a JavaScript expression. String values inside `options` are
        emitted as JavaScript string literals; numbers and booleans pass through.
        """
        intl_type = type
        action = f"@intl({string_literal(str(intl_type))}, {value}"
        if options is not None:
            action += f", {js_object(_quote_strings(options))}"
        elif locale is not None:
            action += ", undefined"
        if locale is not None:
            if isinstance(locale, str):
                action += f", {string_literal(locale)}"
            else:
                action += (
                    ", [" + ", ".join(string_literal(item) for item in locale) + "]"
                )
        return action + ")"

    def fit(
        self,
        v: str,
        old_min: float,
        old_max: float,
        new_min: float,
        new_max: float,
        should_clamp: bool = False,
        should_round: bool = False,
    ) -> str:
        """Build `@fit(...)` to remap a numeric expression. **Datastar Pro.**"""
        bounds = ", ".join(
            _js_number(name, value)
            for name, value in (
                ("old_min", old_min),
                ("old_max", old_max),
                ("new_min", new_min),
                ("new_max", new_max),
            )
        )
        return (
            f"@fit({v}, {bounds}, "
            f"{'true' if should_clamp else 'false'}, "
            f"{'true' if should_round else 'false'})"
        )


at = DatastarActions()
