"""Low-level helpers for JS literals, object expressions, and modifier strings.

Used by attribute and action builders. `literal()`, `Debounce` and
`Throttle` are re-exported from `stario.datastar`; import them from there.
"""

import math
import re
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn, cast

from stario.exceptions import StarioError
from stario_cython.markup import fragment_cache

# Characters that cannot appear inside an attribute key (HTML or Datastar
# parsing would break) — `__` starts a modifier, the rest break the tag.
_SIGNAL_KEY_BAD_RE = re.compile(r"__|\s|[\"'>/=]")

# Filter types for include/exclude parameters: a list of signal names, or a
# `str` that is a Datastar (JavaScript) regex.
FilterValue = str | Iterable[str]

# Datastar's fetch default for `filterSignals.exclude`: local signals.
LOCAL_SIGNALS = r"(^|\.)_"

# Signal values going out (data.signals, SSE.patch_signals). Mapping and
# Sequence, not dict and list: those are invariant, so a `list[str]` variable
# would not pass as `list[SignalValue]`.
type SignalValue = (
    str | int | float | bool | Mapping[str, SignalValue] | Sequence[SignalValue] | None
)

# A duration is a string with a unit: whole milliseconds or seconds, "300ms"
# or "2s". Numbers raise: Datastar reads a bare number as milliseconds,
# Python code as seconds, and a reader cannot tell which one was meant.
type Duration = str
type TimeValue = Duration

# Validated durations (str -> int milliseconds), bounded by refusing new
# entries past the cap, never by clearing (another thread may be reading).
# Immortal on free-threaded builds, so loading it from every thread writes
# no refcount (docs/free-threading.md).
_DURATION_MS: dict[str, int] = fragment_cache()
_DURATION_CACHE_MAX = 256


def _plain_json(value: Any) -> Any:
    """`value` with every non-dict Mapping as a dict and every other Sequence
    (not str or bytes) as a list, recursively, for the JSON encoder."""
    t = type(value)
    if t is str or t is int or t is float or t is bool or value is None:
        return value
    if isinstance(value, Mapping):
        return {k: _plain_json(v) for k, v in cast(Mapping[Any, Any], value).items()}
    if isinstance(value, (str, bytes, bytearray)):
        return value
    if isinstance(value, Sequence):
        return [_plain_json(v) for v in cast(Sequence[Any], value)]
    return value


def encode_signals[R](encode: Callable[[Any], R], payload: Mapping[str, Any]) -> R:
    """Encode a signals payload; a Mapping or Sequence the codec refuses is
    copied to dict / list and encoded again.

    The copy only runs when the first try raises `TypeError`, so dict and
    list payloads cost nothing extra.
    """
    try:
        return encode(payload)
    except TypeError:
        # A value the codec cannot encode at all fails again here.
        return encode(_plain_json(payload))


def require_mapping(name: str, payload: object) -> Mapping[str, Any]:
    """Reject non-mapping signal payloads with a stable TypeError."""
    if type(payload) is not dict and not isinstance(payload, Mapping):
        raise TypeError(f"{name} payload must be a mapping")
    return cast(Mapping[str, Any], payload)


def _duration_error(name: str, value: object) -> StarioError:
    context = {"argument": name, "value": repr(value)[:100]}
    if type(value) is int or type(value) is float:
        number = cast(float, value)
        if type(value) is float and math.isfinite(number) and number >= 0:
            millis = number * 1000
            hint = (
                f"For {number:g} seconds write {name}='{millis:g}ms'."
                if millis == int(millis)
                else "Durations are whole milliseconds or seconds."
            )
        else:
            hint = (
                f"For {value} milliseconds write {name}='{value}ms'; "
                f"for {value} seconds write {name}='{value}s'."
            )
        return StarioError(
            f"{name}={value!r} has no unit",
            context=context,
            help_text=(
                "A duration is a string with a unit, because a bare number is "
                f"milliseconds to Datastar and seconds to Python. {hint}"
            ),
            example="data.on('input', at.get('/search'), debounce='300ms')",
        )
    if type(value) is str and "." in value:
        return StarioError(
            f"{name}={value!r} has a dot",
            context=context,
            help_text=(
                "A dot splits Datastar modifiers, so a duration is whole "
                "milliseconds or seconds: write '1500ms' for 1.5 seconds."
            ),
        )
    return StarioError(
        f"Invalid duration {name}={value!r}",
        context=context,
        help_text="Write whole milliseconds or seconds as a string: '300ms' or '2s'.",
    )


def duration_ms(value: object, name: str) -> int:
    """`value` ("300ms", "2s") in milliseconds; anything else raises with the fix."""
    if type(value) is str:
        cached = _DURATION_MS.get(value)
        if cached is not None:
            return cached
        digits = value[:-2] if value.endswith("ms") else value[:-1]
        unit_ok = value.endswith("ms") or value.endswith("s")
        # isdecimal() also accepts other scripts' digits: require ASCII.
        if (
            unit_ok
            and digits.isascii()
            and digits.isdecimal()
            and (digits == "0" or digits[0] != "0")
        ):
            ms = int(digits) if value.endswith("ms") else int(digits) * 1000
            if len(_DURATION_MS) < _DURATION_CACHE_MAX:
                _DURATION_MS[value] = ms
            return ms
    raise _duration_error(name, value)


def duration(value: object, name: str) -> str:
    """`value` checked as a Datastar duration ("300ms", "2s") and returned."""
    duration_ms(value, name)
    return cast(str, value)


def time_to_string(time: Duration) -> str:
    """`time` checked as a Datastar duration; kept for callers of 5.8."""
    return duration(time, "time")


@dataclass(frozen=True, slots=True)
class Debounce:
    """`debounce=` with edge flags. Defaults are Datastar's: trailing edge only.

    ```python
    data.on("input", at.get(SEARCH.href()), debounce=Debounce("300ms", leading=True))
    # data-on:input__debounce.300ms.leading
    ```

    Write the plain string (`debounce="300ms"`) when the defaults fit.
    """

    wait: Duration
    leading: bool = False
    trailing: bool = True
    modifier: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        text = "debounce." + duration(self.wait, "Debounce.wait")
        if self.leading:
            text += ".leading"
        if not self.trailing:
            if not self.leading:
                raise StarioError(
                    "Debounce(leading=False, trailing=False) never runs",
                    context={"wait": self.wait},
                    help_text="Keep the default trailing=True, or set leading=True.",
                )
            text += ".notrailing"
        object.__setattr__(self, "modifier", text)


@dataclass(frozen=True, slots=True)
class Throttle:
    """`throttle=` with edge flags. Defaults are Datastar's: leading edge only.

    ```python
    data.on("scroll", "$y = window.scrollY", throttle=Throttle("100ms", trailing=True))
    # data-on:scroll__throttle.100ms.trailing
    ```

    Write the plain string (`throttle="100ms"`) when the defaults fit.
    """

    wait: Duration
    leading: bool = True
    trailing: bool = False
    modifier: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        text = "throttle." + duration(self.wait, "Throttle.wait")
        if not self.leading:
            if not self.trailing:
                raise StarioError(
                    "Throttle(leading=False, trailing=False) never runs",
                    context={"wait": self.wait},
                    help_text="Keep the default leading=True, or set trailing=True.",
                )
            text += ".noleading"
        if self.trailing:
            text += ".trailing"
        object.__setattr__(self, "modifier", text)


def _legacy_edges(kind: str, value: tuple[Any, ...], allowed: tuple[str, str]) -> str:
    """5.8 tuple form `("300ms", "leading")`: deprecated, removed in 6.0."""
    replacement = "Debounce" if kind == "debounce" else "Throttle"
    warnings.warn(
        f"{kind}=(time, flag, ...) tuples are deprecated: write "
        f"{kind}={replacement}('300ms', ...) instead. Removed in 6.0.",
        DeprecationWarning,
        stacklevel=4,
    )
    if len(value) not in (2, 3):
        raise StarioError(
            f"Invalid {kind} configuration: {value!r}",
            context={kind: repr(value)},
            help_text=f"Write {kind}='300ms' or {kind}={replacement}('300ms', ...).",
        )
    for flag in value[1:]:
        if flag not in allowed:
            raise StarioError(
                f"Invalid {kind} modifier: {flag!r}",
                context={f"{kind}_modifier": repr(flag)},
                help_text=f"Write {kind}={replacement}('300ms', ...) with booleans.",
            )
    return f"{kind}.{duration(value[0], kind)}." + ".".join(value[1:])


def debounce_to_string(debounce: Duration | Debounce) -> str:
    """`debounce=` as its Datastar modifier (`debounce.300ms[.leading]`)."""
    if type(debounce) is str:
        return "debounce." + duration(debounce, "debounce")
    if type(debounce) is Debounce:
        return debounce.modifier
    if type(debounce) is tuple:
        return _legacy_edges(
            "debounce", cast(tuple[Any, ...], debounce), ("leading", "notrailing")
        )
    raise _duration_error("debounce", debounce)


def throttle_to_string(throttle: Duration | Throttle) -> str:
    """`throttle=` as its Datastar modifier (`throttle.100ms[.trailing]`)."""
    if type(throttle) is str:
        return "throttle." + duration(throttle, "throttle")
    if type(throttle) is Throttle:
        return throttle.modifier
    if type(throttle) is tuple:
        return _legacy_edges(
            "throttle", cast(tuple[Any, ...], throttle), ("noleading", "trailing")
        )
    raise _duration_error("throttle", throttle)


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
        # A NUL inside an HTML attribute arrives as U+FFFD.
        .replace("\x00", "\\x00")
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
    from stario.datastar import format

    format.js_object({"title": "el.value", "count": "$count + 1"})
    # {'title':el.value,'count':$count + 1}
    format.js_object({"label": format.string_literal("Save")})
    # {'label':'Save'}
    ```

    `at.*` does not need this: `headers=` values and a `payload=` dict are
    already data, and a `payload=` str is already an expression.
    """
    obj = __obj if __obj is not None else kwargs
    pairs = (f"{string_literal(str(k))}:{_js_expr(v)}" for k, v in obj.items())
    return "{" + ",".join(pairs) + "}"


def parse_filter_value(value: FilterValue) -> str:
    """Parse a filter value for include/exclude parameters in Datastar actions.

    A string is passed through as a Datastar (JavaScript) regex. An iterable
    is a list of signal paths: each matches itself and its children, and
    nothing else (`["id"]` matches `id` and `id.x`, never `video`).
    """
    if isinstance(value, str):
        return value
    return "^(?:" + "|".join(re.escape(str(item)) for item in value) + ")(?:\\.|$)"


def filter_js(
    include: FilterValue | None,
    exclude: FilterValue | None,
    *,
    keep_local: bool = False,
) -> str | None:
    """Build the `{'include':…,'exclude':…}` JS object used by signal filters.

    Returns `None` when neither filter is set, so callers can omit the option
    entirely. With `keep_local` (backend actions), an `exclude` adds to
    Datastar's default exclusion of local `_` signals instead of replacing
    it, so a local signal never leaves the browser.

    ```python
    filter_js(["draft", "attachments"], None)
    # → "{'include':'^(?:draft|attachments)(?:\\\\.|$)'}"
    ```
    """
    if include is None and exclude is None:
        return None
    exclude_js = ""
    if exclude is not None:
        pattern = parse_filter_value(exclude)
        if keep_local:
            pattern = LOCAL_SIGNALS + "|" + pattern
        exclude_js = "'exclude':" + string_literal(pattern)
    if include is None:
        return "{" + exclude_js + "}"
    include_js = "'include':" + string_literal(parse_filter_value(include))
    if exclude is None:
        return "{" + include_js + "}"
    return "{" + include_js + "," + exclude_js + "}"


def literal(value: SignalValue) -> str:
    """A Python value as a JavaScript literal, safe inside an expression.

    Every Python value that goes into a Datastar expression goes through
    here: a `str` argument to a helper is JavaScript, so text has to be
    quoted first. `<` is escaped, so the result is safe inside an HTML
    attribute and a `<script>` element.

    ```python
    data.on("click", f"$tags = $tags.filter(t => t !== {literal(tag)})")
    literal("O'Brien")          # → "'O\\'Brien'"
    literal({"n": 1, "ok": True})  # → "{'n':1,'ok':true}"
    ```
    """
    t = type(value)
    if t is str:
        return string_literal(cast(str, value))
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if t is int:
        return str(value)
    if t is float:
        if not math.isfinite(cast(float, value)):
            raise StarioError(
                f"literal() cannot write {value!r}: JavaScript JSON has no such number",
                context={"value": repr(value)},
                help_text="Pass a finite number, or None for null.",
            )
        return repr(value)
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key, item in cast(Mapping[object, SignalValue], value).items():
            if type(key) is not str:
                raise StarioError(
                    f"literal() object keys must be str, not {type(key).__name__}",
                    context={"key": repr(key)[:100]},
                )
            parts.append(string_literal(key) + ":" + literal(item))
        return "{" + ",".join(parts) + "}"
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "[" + ",".join(literal(item) for item in value) + "]"
    raise StarioError(
        f"literal() cannot write a {type(value).__name__}",
        context={"type": type(value).__name__, "value": repr(value)[:100]},
        help_text="Pass str, int, float, bool, None, or a mapping or list of those.",
    )


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
