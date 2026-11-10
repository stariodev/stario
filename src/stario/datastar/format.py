"""Low-level helpers for JS literals, object expressions, and modifier strings.

Used by attribute and action builders; you rarely import this module directly.
"""

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal, NoReturn, cast

from stario.exceptions import StarioError

_TIME_VALUE_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:ms|s)$")
# Characters that cannot appear inside an attribute key (HTML or Datastar
# parsing would break) — `__` starts a modifier, the rest break the tag.
_SIGNAL_KEY_BAD_RE = re.compile(r"__|\s|[\"'>/=]")

# Filter types for include/exclude parameters
FilterValue = str | Iterable[str]

type SignalValue = (
    str | int | float | bool | dict[str, SignalValue] | list[SignalValue] | None
)

# TimeValue: int -> Ns suffix, float -> seconds scaled to ms, str -> explicit `ms`/`s`.
type TimeValue = int | float | str


def require_mapping(name: str, payload: object) -> Mapping[str, Any]:
    """Reject non-mapping signal payloads with a stable TypeError."""
    if type(payload) is not dict and not isinstance(payload, Mapping):
        raise TypeError(f"{name} payload must be a mapping")
    return cast(Mapping[str, Any], payload)


def time_to_string(time: TimeValue | None) -> str:
    """Convert a time value to a Datastar-compatible time string.

    `int` values are whole seconds (`1` -> `"1s"`). `float` values are seconds
    converted to milliseconds (`1.0` -> `"1000ms"`). Strings must already be
    explicit whole millisecond or second values, such as `"150ms"` or `"2s"`.
    """
    if time is None:
        raise StarioError(
            f"Invalid Datastar time value: {time!r}",
            context={"time_value": repr(time), "time_type": type(time).__name__},
            help_text="Pass seconds as int/float, or a string like '150ms' or '2s'.",
        )
    if isinstance(time, bool):
        raise StarioError(
            f"Invalid Datastar time value: {time!r}",
            context={"time_value": repr(time), "time_type": type(time).__name__},
            help_text="Pass seconds as int/float, or a string like '150ms' or '2s'.",
        )
    if isinstance(time, float):
        if not math.isfinite(time) or time < 0:
            raise StarioError(
                f"Invalid Datastar time value: {time!r}",
                context={"time_value": repr(time), "time_type": type(time).__name__},
                help_text="Pass a finite non-negative number of seconds.",
            )
        return f"{int(time * 1000)}ms"
    if isinstance(time, int):
        if time < 0:
            raise StarioError(
                f"Invalid Datastar time value: {time!r}",
                context={"time_value": repr(time), "time_type": type(time).__name__},
                help_text="Pass a non-negative number of seconds.",
            )
        return f"{int(time)}s"
    if not _TIME_VALUE_RE.fullmatch(time):
        raise StarioError(
            f"Invalid Datastar time value: {time!r}",
            context={"time_value": time, "time_type": type(time).__name__},
            help_text=(
                "Use whole milliseconds or seconds, for example '150ms' or '2s'. "
                "For fractional seconds, pass a float such as 0.5."
            ),
        )
    return time


type Debounce = (
    TimeValue
    | tuple[TimeValue, Literal["leading", "notrailing"]]
    | tuple[
        TimeValue, Literal["leading", "notrailing"], Literal["leading", "notrailing"]
    ]
)

type Throttle = (
    TimeValue
    | tuple[TimeValue, Literal["noleading", "trailing"]]
    | tuple[
        TimeValue, Literal["noleading", "trailing"], Literal["noleading", "trailing"]
    ]
)


def debounce_to_string(debounce: Debounce) -> str:
    """Convert a debounce configuration to a Datastar modifier string."""
    if isinstance(debounce, (int, float, str)):
        return "debounce." + time_to_string(debounce)

    if len(debounce) not in (2, 3):
        raise StarioError(
            f"Invalid debounce configuration: {debounce}",
            context={
                "debounce_value": str(debounce),
                "debounce_type": type(debounce).__name__,
            },
            help_text="Debounce must be a time value or a tuple with time and modifiers.",
        )

    modifiers = debounce[1:]
    for modifier in modifiers:
        if modifier not in ("leading", "notrailing"):
            raise StarioError(
                f"Invalid debounce modifier: {modifier!r}",
                context={"debounce_modifier": str(modifier)},
                help_text="Debounce modifiers are 'leading' and 'notrailing'.",
            )

    return "debounce." + time_to_string(debounce[0]) + "." + ".".join(modifiers)


def throttle_to_string(throttle: Throttle) -> str:
    """Convert a throttle configuration to a Datastar modifier string."""
    if isinstance(throttle, (int, float, str)):
        return "throttle." + time_to_string(throttle)

    if len(throttle) not in (2, 3):
        raise StarioError(
            f"Invalid throttle configuration: {throttle}",
            context={
                "throttle_value": str(throttle),
                "throttle_type": type(throttle).__name__,
            },
            help_text="Throttle must be a time value or a tuple with time and modifiers.",
        )

    modifiers = throttle[1:]
    for modifier in modifiers:
        if modifier not in ("noleading", "trailing"):
            raise StarioError(
                f"Invalid throttle modifier: {modifier!r}",
                context={"throttle_modifier": str(modifier)},
                help_text="Throttle modifiers are 'noleading' and 'trailing'.",
            )

    return "throttle." + time_to_string(throttle[0]) + "." + ".".join(modifiers)


def string_literal(value: str) -> str:
    """Single-quoted JS string literal for Datastar action/object expressions.

    `<` is escaped as `\\x3c`, so the result is safe inside a `<script>`
    element and an HTML attribute — user text cannot close either early.

    ```python
    from stario.datastar.format import js_object, string_literal

    js_object(filterSignals={"include": string_literal(r"foo.*")})
    # → "{'filterSignals':{'include':'foo.*'}}"  (JS object literal string for options)
    ```
    """
    escaped = (
        # `\x5c`, not `\\`: Datastar's statement splitter reads `\"` and
        # `\'` as an escaped quote without counting backslashes, so a value
        # ending in an escaped backslash would hide the closing quote.
        value.replace("\\", "\\x5c")
        .replace("<", "\\x3c")
        .replace("'", "\\'")
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return f"'{escaped}'"


def _js_expr(value: Any) -> str:
    """Convert a Python value to a JavaScript expression."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StarioError(
                f"Cannot render non-finite float as JS expression: {value}",
                context={"value": str(value)},
                help_text="Pass a finite number, or pre-render a JavaScript expression string.",
            )
        return str(value)
    if isinstance(value, str):
        # String values are treated as JS expressions (e.g., signal names)
        return value
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        pair_parts: list[str] = []
        for key, item in mapping.items():
            pair_parts.append(f"{string_literal(str(key))}:{_js_expr(item)}")
        return "{" + ",".join(pair_parts) + "}"
    if isinstance(value, (list, tuple)):
        seq = cast(list[Any] | tuple[Any, ...], value)
        return "[" + ",".join(_js_expr(item) for item in seq) + "]"
    raise StarioError(
        f"Cannot render {type(value).__name__} as JS expression",
        context={"type": type(value).__name__, "value": repr(value)[:100]},
        help_text=(
            "Pass JSON-shaped values, a JavaScript expression string, or wrap text "
            "with string_literal()."
        ),
    )


def js_object(__obj: dict[str, Any] | None = None, /, **kwargs: Any) -> str:
    """Build a minified JavaScript object literal.

    String values are emitted as raw JavaScript expressions, not quoted text.
    Wrap text with `string_literal()` when you want a JavaScript string value.

    Warning: a plain `str` is a JavaScript expression evaluated in the
    browser. Never pass user-supplied text as a `str` value here — wrap it
    with `string_literal()` or send it as a signal instead.

    ```python
    from stario.datastar import at, format

    at.get("/api", headers={"X-Client": format.string_literal("stario")})
    # headers value becomes {'X-Client':'stario'} inside the @get options object
    at.post("/save", include=["draft"], payload={"title": "el.value"})
    format.js_object({"label": format.string_literal("Save")})
    ```
    """
    obj = __obj if __obj is not None else kwargs
    pairs = (f"{string_literal(str(k))}:{_js_expr(v)}" for k, v in obj.items())
    return "{" + ",".join(pairs) + "}"


def parse_filter_value(value: FilterValue) -> str:
    """Parse a filter value for include/exclude parameters in Datastar actions.

    A string is passed through as a Datastar regex. An iterable is treated as
    literal signal names joined with `|`, with regex metacharacters escaped.
    """
    if isinstance(value, str):
        return value
    return "|".join(re.escape(str(item)) for item in value)


def filter_js(
    include: FilterValue | None,
    exclude: FilterValue | None,
) -> str | None:
    """Build the `{'include':…,'exclude':…}` JS object used by signal filters.

    Returns `None` when neither filter is set, so callers can omit the option entirely.

    ```python
    filter_js(["draft", "attachments"], None)
    # → "{'include':'draft|attachments'}"
    ```
    """
    if include is None and exclude is None:
        return None
    if include is not None:
        include_js = "'include':" + string_literal(parse_filter_value(include))
        if exclude is None:
            return "{" + include_js + "}"
        exclude_js = "'exclude':" + string_literal(parse_filter_value(exclude))
        return "{" + include_js + "," + exclude_js + "}"

    assert exclude is not None
    return "{'exclude':" + string_literal(parse_filter_value(exclude)) + "}"


type Case = Literal["kebab", "snake", "pascal", "camel"]


def check_signal_name(name: object) -> None:
    """Reject only what cannot work as a Datastar signal name.

    Names pass through to Datastar unchanged. A dot is a nested path, so
    every segment must be non-empty. A leading `_` marks a Datastar local
    signal the browser never sends — allowed, not blocked.
    """
    if (
        not isinstance(name, str)
        or not name
        or any(part == "" for part in name.split("."))
    ):
        raise StarioError(
            f"Invalid Datastar signal name: {name!r}",
            context={"signal_name": repr(name)},
            help_text=(
                "Use a non-empty signal name. A dot is a nested path, so every "
                "segment must be non-empty — for example `ui.open`."
            ),
        )


def signal_object(name: str, value_js: str) -> str:
    """Value-form JS object literal assigning `value_js` to a (dotted) signal.

    ```python
    signal_object("ui._open", "0")
    # → "{'ui': {'_open': (0)}}"
    signal_object("myCount", "$a, $b")
    # → "{'myCount': ($a, $b)}"
    ```
    """
    check_signal_name(name)
    inner = f"({value_js})"
    for part in reversed(name.split(".")):
        inner = "{" + string_literal(part) + ": " + inner + "}"
    return inner


def _signal_conflict(key: str) -> NoReturn:
    raise StarioError(
        f"Conflicting Datastar signal paths: {key!r}",
        context={"signal_name": key},
        help_text=(
            "Do not set a signal both as a value and as the parent of "
            "a dotted path, and do not set one signal twice."
        ),
    )


def _is_mapping(value: object) -> bool:
    # isinstance against the Mapping ABC costs ~150 ns even for an int, and
    # this runs for every signal value on every patch: settle the common
    # types by identity first.
    t = type(value)
    if t is dict:
        return True
    if t is str or t is int or t is float or t is bool or t is list or value is None:
        return False
    return isinstance(value, Mapping)


def _check_signal_keys(mapping: Mapping[str, Any]) -> bool:
    """Validate every key; True when some key (at any depth) has a dot."""
    dotted = False
    for key, value in mapping.items():
        # The full check only matters for a dotted path (every segment
        # non-empty); a plain name just has to be a non-empty str.
        if type(key) is not str or not key:
            check_signal_name(key)
        elif "." in key:
            check_signal_name(key)
            dotted = True
        if _is_mapping(value) and _check_signal_keys(cast(Mapping[str, Any], value)):
            dotted = True
    return dotted


def _merge_signal(node: dict[str, Any], parts: list[str], value: Any, key: str) -> None:
    for part in parts[:-1]:
        if part in node:
            child: Any = node[part]
            if not isinstance(child, dict):
                _signal_conflict(key)
        else:
            child = {}
            node[part] = child
        node = cast(dict[str, Any], child)
    leaf = parts[-1]
    if _is_mapping(value):
        if leaf in node:
            child = node[leaf]
            if not isinstance(child, dict):
                _signal_conflict(key)
        else:
            child = {}
            node[leaf] = child
        for sub_key, sub_value in cast(Mapping[str, Any], value).items():
            _merge_signal(
                cast(dict[str, Any], child),
                sub_key.split("."),
                sub_value,
                sub_key,
            )
    else:
        if leaf in node:
            _signal_conflict(key)
        node[leaf] = value


def expand_signal_paths(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expand dotted signal keys into nested objects.

    Datastar's JSON signal patches treat each object key literally, so a
    wire `{"ui.open": true}` would create one signal named `ui.open` that
    `$ui.open` and `data-bind="ui.open"` cannot reach. Stario expands a dot to a nested
    path instead: `{"ui.open": True}` becomes `{"ui": {"open": True}}` —
    the same signal `data.signal("ui.open", ...)` sets.

    Mapping values merge into the path tree recursively; every other
    value — including `None`, which removes a signal — is a leaf.
    Conflicting paths raise `StarioError`.
    """
    if not _check_signal_keys(payload):
        # No dot anywhere — patch_signals runs per SSE event, so keep this cheap.
        return dict(payload)
    tree: dict[str, Any] = {}
    for key, value in payload.items():
        _merge_signal(tree, key.split("."), value, key)
    return tree


def _ds_camel(text: str) -> str:
    """Port of Datastar `modifyCasing` camel: `-x` becomes `X`."""
    return re.sub(r"-[a-z]", lambda m: m[0][1].upper(), text)


def _ds_pascal(text: str) -> str:
    """Port of Datastar `modifyCasing` pascal: capitalize, camel the rest."""
    return text[:1].upper() + _ds_camel(text[1:])


def signal_attr_key(name: str) -> str:
    """Attribute-key suffix whose client-side case conversion yields `name`.

    The key form lowercases in HTML before Datastar's `__case` conversion runs,
    so this picks a spelling that round-trips: names without uppercase take
    `__case.kebab` (a no-op), camelCase names get kebab-spelled (camel is the
    default conversion), and PascalCase names take `__case.pascal`.

    ```python
    signal_attr_key("isDark")
    # → "is-dark"
    signal_attr_key("is_dark")
    # → "is_dark__case.kebab"
    signal_attr_key("IsDark")
    # → "is-dark__case.pascal"
    ```
    """
    check_signal_name(name)
    if _SIGNAL_KEY_BAD_RE.search(name):
        raise StarioError(
            f"Signal name cannot be spelled as a Datastar attribute key: {name!r}",
            context={"signal_name": name},
            help_text=(
                "Use a name Datastar can spell in an attribute key, for example "
                "camelCase, snake_case, or kebab-case."
            ),
        )
    if not any(c.isupper() for c in name):
        return name + "__case.kebab"
    kebabed = re.sub(r"[A-Z]", lambda m: "-" + m[0].lower(), name)
    if name[0].isupper():
        candidate = kebabed.lstrip("-")
        if _ds_pascal(candidate) == name:
            return candidate + "__case.pascal"
    elif _ds_camel(kebabed) == name:
        return kebabed
    raise StarioError(
        f"Signal name cannot be spelled as a Datastar attribute key: {name!r}",
        context={"signal_name": name},
        help_text=(
            "Use a name Datastar can spell in an attribute key, for example "
            "camelCase, snake_case, or kebab-case."
        ),
    )


def key_case(name: str) -> tuple[str, Case | None]:
    """Attribute-key spelling and `__case` modifier that yield `name` on the client.

    HTML lowercases attribute names, so a camelCase or PascalCase name has to
    be kebab-spelled in the key and converted back by Datastar's `__case`
    modifier. Names without uppercase letters are returned unchanged with no
    modifier.

    ```python
    key_case("click")
    # → ("click", None)
    key_case("myEvent")
    # → ("my-event", "camel")
    key_case("DOMContentLoaded")
    # → ("d-o-m-content-loaded", "pascal")
    ```
    """
    if not any(c.isupper() for c in name):
        return name, None
    kebabed = re.sub(r"[A-Z]", lambda m: "-" + m[0].lower(), name)
    if name[0].isupper():
        candidate = kebabed.lstrip("-")
        if _ds_pascal(candidate) == name:
            return candidate, "pascal"
    elif _ds_camel(kebabed) == name:
        return kebabed, "camel"
    raise StarioError(
        f"Name cannot be spelled as a Datastar attribute key: {name!r}",
        context={"name": name},
        help_text=(
            "HTML lowercases attribute names, and Datastar can only restore "
            "camelCase or PascalCase. Use one of those, or kebab-case."
        ),
    )


def to_kebab_key(key: str) -> str:
    """Convert a Python/JS-ish key spelling to a Datastar attribute key suffix."""
    if not key:
        return ""

    if "_" in key:
        return key.replace("_", "-").lower()

    if "-" in key:
        return key.lower()

    if key.islower():
        return key

    if key[0].isupper():
        parts: list[str] = []
        for i, c in enumerate(key):
            if c.isupper():
                if i:
                    parts.append("-")
                parts.append(c.lower())
            else:
                parts.append(c)
        return "".join(parts)

    parts = []
    for c in key:
        if c.isupper():
            parts.append("-")
            parts.append(c.lower())
        else:
            parts.append(c)
    return "".join(parts)
