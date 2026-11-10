"""Datastar action strings.

Use the exported `at` instance:

```python
from stario import Route
from stario.datastar import at, data

REFRESH = Route("GET /items")
h.Button(data.on("click", at.get(REFRESH.href())), "Refresh")
```

The methods return strings such as `@get('/items')` for use inside Datastar
attributes like `data.on(...)`. Build the URL with `route.href()`, then
pick `at.get` / `at.post` / … at the call site.
"""

import math
from collections.abc import Mapping
from typing import Any, Literal, TypedDict, Unpack, cast
from urllib.parse import urlencode

from stario.exceptions import StarioError

from .format import FilterValue, filter_js, js_object, string_literal

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
    payload: dict[str, Any] | None
    retry: Retry | str | None
    retry_interval_ms: int | None
    retry_scaler: float | None
    retry_max_wait_ms: int | None
    retry_max_count: int | None
    request_cancellation: RequestCancellation | str | None


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
        """Build a Datastar backend fetch action.

        Values in `payload` are JavaScript expressions evaluated in the
        browser. Never pass user-supplied text as a `payload` value — wrap
        literal text with `string_literal()` or send it as a signal. Values in
        `headers` are already string literals and are safe for literal text.
        """
        content_type = options.get("content_type")
        include = options.get("include")
        exclude = options.get("exclude")
        selector = options.get("selector")
        headers = options.get("headers")
        open_when_hidden = options.get("open_when_hidden")
        payload = options.get("payload")
        retry = options.get("retry")
        retry_interval_ms = options.get("retry_interval_ms")
        retry_scaler = options.get("retry_scaler")
        retry_max_wait_ms = options.get("retry_max_wait_ms")
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

        filters = filter_js(include, exclude)
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
            parts.append(f"payload: {js_object(payload)}")

        if retry is not None:
            parts.append(f"retry: {string_literal(str(retry))}")

        if retry_interval_ms is not None:
            parts.append(
                "retryInterval: "
                + _js_number("retry_interval_ms", retry_interval_ms, integer=True)
            )

        if retry_scaler is not None:
            parts.append("retryScaler: " + _js_number("retry_scaler", retry_scaler))

        if retry_max_wait_ms is not None:
            parts.append(
                "retryMaxWait: "
                + _js_number("retry_max_wait_ms", retry_max_wait_ms, integer=True)
            )

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
        """Build `@get(...)`.

        Warning: `payload` string values are JavaScript expressions evaluated
        in the browser — never pass user-supplied text there; wrap it with
        `string_literal()` or send it as a signal.

        ```python
        h.Button(data.on("click", at.get("/items", {"q": "$query"})), "Search")
        ```
        """
        return self._fetch("get", url, queries, **options)

    def post(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@post(...)` with the same option surface as `get`.

        Warning: `payload` string values are JavaScript expressions evaluated
        in the browser — never pass user-supplied text there; wrap it with
        `string_literal()` or send it as a signal.
        """
        return self._fetch("post", url, queries, **options)

    def put(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@put(...)` with the same option surface as `get`.

        Warning: `payload` string values are JavaScript expressions evaluated
        in the browser — never pass user-supplied text there; wrap it with
        `string_literal()` or send it as a signal.
        """
        return self._fetch("put", url, queries, **options)

    def patch(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@patch(...)` with the same option surface as `get`.

        Warning: `payload` string values are JavaScript expressions evaluated
        in the browser — never pass user-supplied text there; wrap it with
        `string_literal()` or send it as a signal.
        """
        return self._fetch("patch", url, queries, **options)

    def delete(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@delete(...)` with the same option surface as `get`.

        Warning: `payload` string values are JavaScript expressions evaluated
        in the browser — never pass user-supplied text there; wrap it with
        `string_literal()` or send it as a signal.
        """
        return self._fetch("delete", url, queries, **options)

    def query(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        **options: Unpack[FetchOptions],
    ) -> str:
        """Build `@query(...)` with the same option surface as `get`.

        Sends a `QUERY` request with signals in the body, like `post`.
        Warning: `payload` string values are JavaScript expressions evaluated
        in the browser — never pass user-supplied text there; wrap it with
        `string_literal()` or send it as a signal.
        """
        return self._fetch("query", url, queries, **options)

    def clipboard(self, text: str, is_base64: bool = False) -> str:
        """Build `@clipboard(...)`. **Datastar Pro only.**"""
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
        """Build `@intl(...)`. **Datastar Pro only.**

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
        """Build `@fit(...)` to remap a numeric expression. **Datastar Pro only.**"""
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
