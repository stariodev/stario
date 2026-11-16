"""Datastar HTML attribute helpers.

Use the exported `data` instance:

```python
from stario.datastar import at, data

h.Button(data.on("click", at.post("/cart")), "Add")
```

Each method returns a pre-rendered `Attrs` fragment (opening-tag attribute bytes).
The same call with the same arguments returns the same fragment from a cache,
so a helper inside a render loop costs a lookup. The default instance emits
normal `data-*` attributes. Create another instance when serving a custom
Datastar bundle with an aliased prefix:

```python
from stario.datastar import DatastarAttributes

star = DatastarAttributes("data-star-")
h.Div(star.text("$title"))
```

Rules:

- A `str` argument is JavaScript (`"$open = !$open"`). Signal names, event
  names and modifier values are names. Put a Python value into code with
  `literal(value)`.
- `on`, `init` and `effect` take several statements: `data.on("submit",
  at.post(SAVE.href()), "$_edit.close()")` runs them in order.
- A duration (`delay=`, `debounce=`, `throttle=`, `duration=`) is a string
  with a unit: `"300ms"` or `"2s"`. A number raises with the fix.
  `Debounce("300ms", leading=True)` and `Throttle("1s", trailing=True)` set
  the edge flags.
- Datastar Pro attributes live under `data.pro`.

Reference: https://data-star.dev/reference/attributes
"""

import warnings
from collections.abc import Mapping
from typing import Literal

from stario.exceptions import StarioError
from stario.json import dumps as json_dumps
from stario.markup.escape import escape_attribute_value as escape_attr
from stario.markup.escape import escape_sq_attribute_value, validate_attribute_key
from stario.markup.types import Attrs
from stario_cython.markup import fragment_cache, remember_attrs

from ._jsevents import JSEvent
from .format import (
    Case,
    Debounce,
    Duration,
    FilterValue,
    SignalValue,
    Throttle,
    check_signal_name,
    debounce_to_string,
    duration,
    encode_signals,
    expand_signal_paths,
    filter_js,
    js_object,
    key_case,
    require_mapping,
    signal_attr_key,
    signal_object,
    throttle_to_string,
    to_kebab_key,
)

# Fragment caches, one per helper (see stario_cython.markup.remember_attrs).
_ON = fragment_cache()
_INIT = fragment_cache()
_BIND = fragment_cache()
_ATTR = fragment_cache()

_duration = duration  # on_interval has a parameter named `duration`


def _code(helper: str, statements: tuple[str, ...]) -> str:
    """Statements joined into one Datastar expression."""
    if not statements:
        raise StarioError(
            f"data.{helper}() needs at least one statement",
            help_text="Pass the JavaScript to run, for example an at.post(...) action.",
            example=f'data.{helper}("click", at.post(SAVE.href()))'
            if helper == "on"
            else f'data.{helper}("$ready = true")',
        )
    return "; ".join(statements)


def _deprecated(old: str, new: str) -> None:
    warnings.warn(
        f"{old} is deprecated: {new}. Removed in 6.0.",
        DeprecationWarning,
        stacklevel=3,
    )


def _reject_uppercase_key(helper: str, key: str, alternative: str) -> None:
    """Key-form attributes travel in the attribute name, which HTML lowercases."""
    if any(c.isupper() for c in key):
        raise StarioError(
            f"data.{helper}() cannot use a name with uppercase letters: {key!r}",
            context={"helper": helper, "name": key},
            help_text=(
                "The name is part of the HTML attribute name, which the browser "
                f"lowercases before Datastar reads it. Use {alternative}, where "
                "the name is a JavaScript object key and keeps its case."
            ),
        )


def _reject_uppercase_modifier(helper: str, modifier: str, value: str) -> None:
    if any(c.isupper() for c in value):
        raise StarioError(
            f"data.{helper}() {modifier}={value!r} cannot carry uppercase letters",
            context={"helper": helper, "modifier": modifier, "value": value},
            help_text=(
                "Modifier values are part of the HTML attribute name, which the "
                "browser lowercases before Datastar reads it."
            ),
        )


def _timing(
    delay: Duration | None,
    debounce: Duration | Debounce | None,
    throttle: Duration | Throttle | None,
) -> list[str]:
    modifiers: list[str] = []
    if delay is not None:
        modifiers.append("delay." + duration(delay, "delay"))
    if debounce is not None:
        modifiers.append(debounce_to_string(debounce))
    if throttle is not None:
        modifiers.append(throttle_to_string(throttle))
    return modifiers


class DatastarAttributes:
    """Namespace for Datastar `data-*` attributes.

    Each helper returns an `Attrs` fragment (pre-rendered opening-tag bytes).
    JavaScript expressions and `js_object()` / `filter_js()` output are
    trusted Datastar content. Helpers escape them for HTML attribute boundaries,
    but do not parse or sanitize the JavaScript itself. `signals()` wraps JSON
    in single-quoted attributes and escapes only `'`, `&`, and `<`/`>` —
    so JSON double quotes stay literal on the wire.

    Signal names pass through to Datastar unchanged — a dot is a nested path
    and a leading `_` marks a local signal the browser does not send.

    `prefix` defaults to `"data-"`. Pass an explicit Datastar alias prefix, such
    as `"data-star-"`, when loading a custom aliased Datastar bundle.
    """

    __slots__ = ("prefix", "pro")

    def __init__(self, prefix: str = "data-") -> None:
        if not prefix:
            raise ValueError("Datastar attribute prefix cannot be empty.")
        self.prefix = prefix if prefix.endswith("-") else prefix + "-"
        # Every helper writes the prefix into an attribute name.
        validate_attribute_key(self.prefix)
        # Datastar Pro attributes (`data.pro.persist(...)`).
        self.pro = DatastarProAttributes(self.prefix)

    def attr(self, key: str, expression: str) -> Attrs:
        """Set one HTML attribute from a reactive expression.

        `expression` is JavaScript evaluated in the browser — never pass
        user-supplied text; use a signal reference or `literal()`.

        ```python
        data.attr("title", "$item.label")
        # Attrs(' data-attr:title="$item.label"')
        ```
        """
        cache_key = (self.prefix, key, expression)
        hit = _ATTR.get(cache_key)
        if hit is not None:
            return hit
        validate_attribute_key(key)
        _reject_uppercase_key("attr", key, "data.attrs({key: expression})")
        return remember_attrs(
            _ATTR, cache_key, f' {self.prefix}attr:{key}="{escape_attr(expression)}"'
        )

    def attrs(self, mapping: dict[str, str]) -> Attrs:
        """Set several HTML attributes from a mapping of expressions.

        Values are JavaScript expressions evaluated in the browser — never
        pass user-supplied text; use a signal reference or `literal()`.

        ```python
        data.attrs({"open": "$sidebarOpen"})
        # Attrs(' data-attr="{'open':$sidebarOpen}"')
        ```
        """
        return Attrs(f' {self.prefix}attr="{escape_attr(js_object(mapping))}"')

    def bind(
        self,
        signal_name: str,
        *,
        prop: str | None = None,
        event: str | None = None,
    ) -> Attrs:
        """Two-way bind an element value to a signal.

        ```python
        data.bind("email")
        # Attrs(' data-bind="email"')

        data.bind("isChecked", prop="checked", event="change")
        # Attrs(' data-bind__prop.checked__event.change="isChecked"')
        ```
        """
        cache_key = (self.prefix, signal_name, prop, event)
        hit = _BIND.get(cache_key)
        if hit is not None:
            return hit
        check_signal_name(signal_name)
        if prop is not None:
            validate_attribute_key(prop)
            _reject_uppercase_modifier("bind", "prop", prop)
        if event is not None:
            validate_attribute_key(event)
            _reject_uppercase_modifier("bind", "event", event)
        modifiers = ""
        if prop is not None:
            modifiers += f"__prop.{prop}"
        if event is not None:
            modifiers += f"__event.{event}"
        return remember_attrs(
            _BIND,
            cache_key,
            f' {self.prefix}bind{modifiers}="{escape_attr(signal_name)}"',
        )

    def class_(self, name: str, expression: str) -> Attrs:
        """Toggle one CSS class from a reactive expression.

        ```python
        data.class_("hidden", "!$expanded")
        # Attrs(' data-class:hidden="!$expanded"')
        ```
        """
        validate_attribute_key(name)
        key, case = key_case(name)
        if case is not None:
            key = f"{key}__case.{case}"
        return Attrs(f' {self.prefix}class:{key}="{escape_attr(expression)}"')

    def classes(self, mapping: dict[str, str]) -> Attrs:
        """Toggle several CSS classes from a mapping of expressions.

        ```python
        data.classes({"loading": "$pending"})
        # Attrs(' data-class="{'loading':$pending}"')
        ```
        """
        return Attrs(f' {self.prefix}class="{escape_attr(js_object(mapping))}"')

    def computed(self, key: str, expression: str) -> Attrs:
        """Create one computed signal from a reactive expression.

        `expression` is JavaScript evaluated in the browser — never pass
        user-supplied text; use a signal reference or `literal()`.

        ```python
        data.computed("fullName", "$first + $last")
        # Attrs(' data-computed="{'fullName':() =&gt; ($first + $last)}"')
        ```
        """
        return self.computeds({key: expression})

    def computeds(self, mapping: dict[str, str]) -> Attrs:
        """Create several computed signals in one `data-computed` object.

        Values are JavaScript expressions evaluated in the browser — never
        pass user-supplied text; use a signal reference or `literal()`.
        Dotted keys merge into one nested object; using a name both as a
        signal and as a path prefix raises `StarioError`.

        ```python
        data.computeds({"fullName": "$a", "ui.label": "$b"})
        # Attrs(' data-computed="{'fullName':() =&gt; ($a),'ui':{'label':() =&gt; ($b)}}"')
        ```
        """
        tree = expand_signal_paths(
            {name: f"() => ({expr})" for name, expr in mapping.items()}
        )
        return Attrs(f' {self.prefix}computed="{escape_attr(js_object(tree))}"')

    def effect(self, *statements: str) -> Attrs:
        """Run statements when the element initializes and when a signal they read changes.

        ```python
        data.effect("el.focus()")
        # Attrs(' data-effect="el.focus()"')
        ```
        """
        expression = (
            statements[0] if len(statements) == 1 else _code("effect", statements)
        )
        return Attrs(f' {self.prefix}effect="{escape_attr(expression)}"')

    def ignore(self, self_only: bool = False) -> Attrs:
        """Skip Datastar processing for this element or subtree.

        ```python
        data.ignore()
        # Attrs(' data-ignore')

        data.ignore(self_only=True)
        # Attrs(' data-ignore__self')
        ```
        """
        if self_only:
            return Attrs(f" {self.prefix}ignore__self")
        return Attrs(f" {self.prefix}ignore")

    def ignore_morph(self) -> Attrs:
        """Prevent backend patches from morphing this subtree.

        ```python
        data.ignore_morph()
        # Attrs(' data-ignore-morph')
        ```
        """
        return Attrs(f" {self.prefix}ignore-morph")

    def indicator(self, signal_name: str) -> Attrs:
        """Track in-flight fetch state in a signal.

        ```python
        data.indicator("saving")
        # Attrs(' data-indicator="saving"')
        ```
        """
        check_signal_name(signal_name)
        return Attrs(f' {self.prefix}indicator="{escape_attr(signal_name)}"')

    def init(
        self,
        *statements: str,
        delay: Duration | None = None,
        view_transition: bool = False,
    ) -> Attrs:
        """Run statements when the element initializes.

        ```python
        data.init("setup()")
        # Attrs(' data-init="setup()"')

        data.init("setup()", delay="200ms", view_transition=True)
        # Attrs(' data-init__delay.200ms__viewtransition="setup()"')
        ```
        """
        expression = (
            statements[0] if len(statements) == 1 else _code("init", statements)
        )
        cache_key = (self.prefix, expression, delay, view_transition)
        hit = _INIT.get(cache_key)
        if hit is not None:
            return hit
        key = self.prefix + "init"
        if delay is not None:
            key += "__delay." + duration(delay, "delay")
        if view_transition:
            key += "__viewtransition"
        return remember_attrs(_INIT, cache_key, f' {key}="{escape_attr(expression)}"')

    def json_signals(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        terse: bool = False,
    ) -> Attrs:
        """Render signals as JSON text.

        ```python
        data.json_signals()
        # Attrs(' data-json-signals')

        data.json_signals(include=["email", "password"], terse=True)
        # data-json-signals__terse="{'include':'^(?:email|password)(?:\\\\.|$)'}"
        ```
        """
        filters = filter_js(include, exclude)
        if terse:
            key = self.prefix + "json-signals__terse"
        else:
            key = self.prefix + "json-signals"
        if filters is None:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(filters)}"')

    def on(
        self,
        event: JSEvent | str,
        *statements: str,
        once: bool = False,
        passive: bool = False,
        capture: bool = False,
        delay: Duration | None = None,
        debounce: Duration | Debounce | None = None,
        throttle: Duration | Throttle | None = None,
        view_transition: bool = False,
        window: bool = False,
        document: bool = False,
        outside: bool = False,
        prevent: bool = False,
        stop: bool = False,
        case: Case = "kebab",
        target: Literal["window", "document", "outside"] | None = None,
    ) -> Attrs:
        """Listen for an event and run the statements in order.

        ```python
        data.on("click", at.get("/cart/count"))
        # Attrs(' data-on:click="@get('/cart/count')"')

        data.on("submit", at.post("/rename"), "$_edit = false", prevent=True)
        # Attrs(' data-on:submit__prevent="@post('/rename'); $_edit = false"')

        data.on("click", "$open = false", outside=True)
        # Attrs(' data-on:click__outside="$open = false"')

        data.on("input", at.get("/search"), debounce="300ms")
        # Attrs(' data-on:input__debounce.300ms="@get('/search')"')
        ```

        Modifiers have Datastar's names: `window`, `document` and `outside`
        pick where the listener sits (one at most). `target=` is the
        deprecated spelling of the same three.
        """
        expression = statements[0] if len(statements) == 1 else _code("on", statements)
        if (
            not once
            and not passive
            and not capture
            and delay is None
            and debounce is None
            and throttle is None
            and not view_transition
            and not window
            and not document
            and not outside
            and not prevent
            and not stop
            and case == "kebab"
            and target is None
        ):
            cache_key: tuple[object, ...] = (self.prefix, event, expression)
            hit = _ON.get(cache_key)
            if hit is not None:
                return hit
        else:
            cache_key = (
                self.prefix,
                event,
                expression,
                once,
                passive,
                capture,
                delay,
                debounce,
                throttle,
                view_transition,
                window,
                document,
                outside,
                prevent,
                stop,
                case,
                target,
            )
            try:
                hit = _ON.get(cache_key)
            except TypeError:  # an unhashable argument: the checks below name it
                hit = None
                cache_key = ()
            if hit is not None:
                return hit

        if passive and prevent:
            raise StarioError(
                "passive and prevent contradict each other",
                context={"event": event},
                help_text=(
                    "`passive` promises the listener never calls preventDefault; "
                    "`prevent` calls it. Pass only one."
                ),
            )
        validate_attribute_key(event)

        if target is not None:
            validate_attribute_key(target)
            _deprecated(f"data.on(target={target!r})", f"write {target}=True")
            if target == "window":
                window = True
            elif target == "document":
                document = True
            elif target == "outside":
                outside = True
            else:
                raise StarioError(
                    f"Unknown data.on target: {target!r}",
                    help_text="Write window=True, document=True or outside=True.",
                )
        if window + document + outside > 1:
            raise StarioError(
                "data.on() takes one of window, document and outside",
                context={"event": event},
                help_text=(
                    "Each one moves the listener to a different place. Datastar "
                    "would silently pick one; pass only the one you mean."
                ),
            )

        modifiers: list[str] = []
        if once:
            modifiers.append("once")
        if passive:
            modifiers.append("passive")
        if capture:
            modifiers.append("capture")
        if window:
            modifiers.append("window")
        if document:
            modifiers.append("document")
        if outside:
            modifiers.append("outside")
        if prevent:
            modifiers.append("prevent")
        if stop:
            modifiers.append("stop")
        modifiers += _timing(delay, debounce, throttle)
        if view_transition:
            modifiers.append("viewtransition")

        kebab_event = to_kebab_key(event)
        if case != "kebab":
            validate_attribute_key(case)
            modifiers.append("case." + case)
        else:
            # `myEvent` has to travel as `my-event__case.camel`: the HTML
            # parser lowercases the key and Datastar's default conversion
            # for `data-on` is kebab.
            _kebab, inferred = key_case(event)
            if inferred is not None:
                modifiers.append("case." + inferred)

        if modifiers:
            text = (
                f' {self.prefix}on:{kebab_event}__{"__".join(modifiers)}="'
                f'{escape_attr(expression)}"'
            )
        else:
            text = f' {self.prefix}on:{kebab_event}="{escape_attr(expression)}"'
        if not cache_key:
            return Attrs(text)
        return remember_attrs(_ON, cache_key, text)

    def on_intersect(
        self,
        *statements: str,
        threshold: float | Literal["half", "full"] | str | None = None,
        once: bool = False,
        exit: bool = False,
        delay: Duration | None = None,
        debounce: Duration | Debounce | None = None,
        throttle: Duration | Throttle | None = None,
        view_transition: bool = False,
    ) -> Attrs:
        """React to viewport intersection.

        ```python
        data.on_intersect("load()", threshold=0.25, once=True, view_transition=True)
        # Attrs(' data-on-intersect__threshold.25__once__viewtransition="load()"')
        ```
        """
        expression = (
            statements[0] if len(statements) == 1 else _code("on_intersect", statements)
        )
        modifiers: list[str] = []
        if threshold is not None:
            if isinstance(threshold, str):
                if threshold in ("half", "full"):
                    modifiers.append(threshold)
                else:
                    validate_attribute_key(threshold)
                    modifiers.append("threshold." + threshold)
            elif 0.0 <= threshold <= 1.0:
                modifiers.append(f"threshold.{round(threshold * 100)}")
            else:
                raise StarioError(
                    f"Invalid intersection threshold: {threshold}",
                    context={
                        "threshold_value": str(threshold),
                        "threshold_type": type(threshold).__name__,
                    },
                    help_text=(
                        "Numeric thresholds are the visible fraction of the element and "
                        "must be between 0.0 and 1.0, or the strings 'half' / 'full'."
                    ),
                )
        if once:
            modifiers.append("once")
        if exit:
            modifiers.append("exit")
        modifiers += _timing(delay, debounce, throttle)
        if view_transition:
            modifiers.append("viewtransition")

        if modifiers:
            return Attrs(
                f' {self.prefix}on-intersect__{"__".join(modifiers)}="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(f' {self.prefix}on-intersect="{escape_attr(expression)}"')

    def on_interval(
        self,
        *statements: str,
        duration: Duration = "1s",
        leading: bool = False,
        view_transition: bool = False,
    ) -> Attrs:
        """Run statements on an interval.

        ```python
        data.on_interval("tick()")
        # Attrs(' data-on-interval="tick()"')

        data.on_interval("tick()", duration="2s", leading=True)
        # Attrs(' data-on-interval__duration.2s.leading="tick()"')
        ```
        """
        expression = (
            statements[0] if len(statements) == 1 else _code("on_interval", statements)
        )
        key = self.prefix + "on-interval"
        if duration != "1s" or leading:
            key += "__duration." + _duration(duration, "duration")
            if leading:
                key += ".leading"
        if view_transition:
            key += "__viewtransition"
        return Attrs(f' {key}="{escape_attr(expression)}"')

    def on_signal_patch(
        self,
        *statements: str,
        delay: Duration | None = None,
        debounce: Duration | Debounce | None = None,
        throttle: Duration | Throttle | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> Attrs:
        """React to signal patches.

        ```python
        data.on_signal_patch("save()", debounce="500ms", include=["draft"])
        # data-on-signal-patch__debounce.500ms="save()"
        # data-on-signal-patch-filter="{'include':'^(?:draft)(?:\\\\.|$)'}"
        ```
        """
        expression = (
            statements[0]
            if len(statements) == 1
            else _code("on_signal_patch", statements)
        )
        modifiers = _timing(delay, debounce, throttle)
        if modifiers:
            key = f"{self.prefix}on-signal-patch__{'__'.join(modifiers)}"
        else:
            key = self.prefix + "on-signal-patch"

        filters = filter_js(include, exclude)
        if filters is not None:
            return Attrs(
                f' {key}="{escape_attr(expression)}"'
                f' {self.prefix}on-signal-patch-filter="'
                f'{escape_attr(filters)}"'
            )
        return Attrs(f' {key}="{escape_attr(expression)}"')

    def preserve_attr(self, attrs: str | list[str]) -> Attrs:
        """Preserve selected attributes during DOM morphing.

        ```python
        data.preserve_attr(["data-testid", "id"])
        # Attrs(' data-preserve-attr="data-testid id"')
        ```
        """
        value = attrs if isinstance(attrs, str) else " ".join(attrs)
        return Attrs(f' {self.prefix}preserve-attr="{escape_attr(value)}"')

    def ref(self, signal_name: str) -> Attrs:
        """Store the current element in a signal.

        ```python
        data.ref("searchInput")
        # Attrs(' data-ref="searchInput"')
        ```
        """
        check_signal_name(signal_name)
        return Attrs(f' {self.prefix}ref="{escape_attr(signal_name)}"')

    def show(self, expression: str) -> Attrs:
        """Show or hide an element from a boolean expression.

        ```python
        data.show("$error != null")
        # Attrs(' data-show="$error != null"')
        ```
        """
        return Attrs(f' {self.prefix}show="{escape_attr(expression)}"')

    def signal(
        self,
        name: str,
        expression: str,
        *,
        if_missing: bool = False,
    ) -> Attrs:
        """Deprecated: patch one signal from a Datastar expression.

        `expression` is JavaScript, unlike the values of `data.signals()`, so
        `data.signal("name", "Bob")` reads a variable named `Bob`. Write
        `data.signals({"name": "Bob"})` for a value. Removed in 6.0.
        """
        _deprecated(
            "data.signal(name, expression)",
            "write data.signals({name: value}) for a value, or "
            "data.computed(name, expression) for an expression",
        )
        obj = signal_object(name, expression)
        if if_missing:
            return Attrs(f' {self.prefix}signals__ifmissing="{escape_attr(obj)}"')
        return Attrs(f' {self.prefix}signals="{escape_attr(obj)}"')

    def signals(
        self,
        payload: Mapping[str, SignalValue],
        *,
        if_missing: bool = False,
    ) -> Attrs:
        """Patch several signals.

        Keys are sent as JSON object keys, so any signal name works. A
        dotted key is a nested path — `{"ui.open": True}` sets `$ui.open`.
        Values are JSON values, not JavaScript: `{"name": "Bob"}` is the
        text Bob. Values may be any Mapping or Sequence (a `list[str]`
        variable, a tuple), sent as JSON objects and arrays.

        ```python
        data.signals({"count": 0, "open": False})
        # Attrs(" data-signals='{\"count\":0,\"open\":false}'")
        ```
        """
        # A JSON backslash is always `\\`; write it as `\` so no string
        # ends in `\\"`, which Datastar's statement splitter takes for an
        # escaped quote (it does not count the backslashes before it).
        value = escape_sq_attribute_value(
            encode_signals(
                json_dumps,
                expand_signal_paths(require_mapping("signals", payload)),
            ).replace("\\\\", "\\u005c")
        )
        if if_missing:
            return Attrs(f" {self.prefix}signals__ifmissing='{value}'")
        return Attrs(f" {self.prefix}signals='{value}'")

    def style(self, prop: str, expression: str) -> Attrs:
        """Set one inline style property from a reactive expression.

        ```python
        data.style("width", "$pct + '%'")
        # Attrs(' data-style:width="$pct + '%'"')
        ```
        """
        validate_attribute_key(prop)
        _reject_uppercase_key("style", prop, "data.styles({prop: expression})")
        return Attrs(f' {self.prefix}style:{prop}="{escape_attr(expression)}"')

    def styles(self, mapping: dict[str, str]) -> Attrs:
        """Set several inline style properties from expressions.

        ```python
        data.styles({"opacity": "$visible ? '1' : '0'"})
        # Attrs(' data-style="{'opacity':$visible ? '1' : '0'}"')
        ```
        """
        return Attrs(f' {self.prefix}style="{escape_attr(js_object(mapping))}"')

    def text(self, expression: str) -> Attrs:
        """Bind text content to a Datastar expression.

        ```python
        data.text("$greeting")
        # Attrs(' data-text="$greeting"')
        ```
        """
        return Attrs(f' {self.prefix}text="{escape_attr(expression)}"')

    # --- Datastar Pro, deprecated here: use data.pro.* -----------------------

    def animate(self, expression: str) -> Attrs:
        """Deprecated: `data.pro.animate`. Removed in 6.0."""
        _deprecated("data.animate", "write data.pro.animate (Datastar Pro)")
        return self.pro.animate(expression)

    def custom_validity(self, expression: str) -> Attrs:
        """Deprecated: `data.pro.custom_validity`. Removed in 6.0."""
        _deprecated(
            "data.custom_validity", "write data.pro.custom_validity (Datastar Pro)"
        )
        return self.pro.custom_validity(expression)

    def match_media(self, signal_name: str, expression: str) -> Attrs:
        """Deprecated: `data.pro.match_media`. Removed in 6.0."""
        _deprecated("data.match_media", "write data.pro.match_media (Datastar Pro)")
        return self.pro.match_media(signal_name, expression)

    def on_raf(
        self, expression: str, *, throttle: Duration | Throttle | None = None
    ) -> Attrs:
        """Deprecated: `data.pro.on_raf`. Removed in 6.0."""
        _deprecated("data.on_raf", "write data.pro.on_raf (Datastar Pro)")
        return self.pro.on_raf(expression, throttle=throttle)

    def on_resize(
        self,
        expression: str,
        *,
        debounce: Duration | Debounce | None = None,
        throttle: Duration | Throttle | None = None,
    ) -> Attrs:
        """Deprecated: `data.pro.on_resize`. Removed in 6.0."""
        _deprecated("data.on_resize", "write data.pro.on_resize (Datastar Pro)")
        return self.pro.on_resize(expression, debounce=debounce, throttle=throttle)

    def persist(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        storage_key: str | None = None,
        session: bool = False,
    ) -> Attrs:
        """Deprecated: `data.pro.persist`. Removed in 6.0."""
        _deprecated("data.persist", "write data.pro.persist (Datastar Pro)")
        return self.pro.persist(
            include=include, exclude=exclude, storage_key=storage_key, session=session
        )

    def query_string(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        filter_empty: bool = False,
        history: bool = False,
    ) -> Attrs:
        """Deprecated: `data.pro.query_string`. Removed in 6.0."""
        _deprecated("data.query_string", "write data.pro.query_string (Datastar Pro)")
        return self.pro.query_string(
            include=include, exclude=exclude, filter_empty=filter_empty, history=history
        )

    def replace_url(self, expression: str) -> Attrs:
        """Deprecated: `data.pro.replace_url`. Removed in 6.0.

        To change the URL from a handler, use `SSE(w).replace_url(url)`,
        which works with the free bundle.
        """
        _deprecated(
            "data.replace_url",
            "write data.pro.replace_url (Datastar Pro), or SSE(w).replace_url(url) "
            "from a handler",
        )
        return self.pro.replace_url(expression)

    def scroll_into_view(
        self,
        *,
        behavior: Literal["smooth", "instant", "auto"] | None = None,
        horizontal: Literal["start", "center", "end", "nearest"] | None = None,
        vertical: Literal["start", "center", "end", "nearest"] | None = None,
        focus: bool = False,
    ) -> Attrs:
        """Deprecated: `data.pro.scroll_into_view`. Removed in 6.0."""
        _deprecated(
            "data.scroll_into_view", "write data.pro.scroll_into_view (Datastar Pro)"
        )
        return self.pro.scroll_into_view(
            behavior=behavior, horizontal=horizontal, vertical=vertical, focus=focus
        )

    def view_transition(self, expression: str) -> Attrs:
        """Deprecated: `data.pro.view_transition`. Removed in 6.0."""
        _deprecated(
            "data.view_transition", "write data.pro.view_transition (Datastar Pro)"
        )
        return self.pro.view_transition(expression)


class DatastarProAttributes:
    """Datastar Pro attributes: `data.pro.persist(...)`.

    They do nothing with the free Datastar bundle. Load a Pro bundle with
    `ModuleScript(src=...)` before you use them.
    """

    __slots__ = ("prefix",)

    def __init__(self, prefix: str = "data-") -> None:
        self.prefix = prefix

    def animate(self, expression: str) -> Attrs:
        """Animate element attributes over time.

        ```python
        data.pro.animate("$x")
        # Attrs(' data-animate="$x"')
        ```
        """
        return Attrs(f' {self.prefix}animate="{escape_attr(expression)}"')

    def custom_validity(self, expression: str) -> Attrs:
        """Set a custom validity message.

        ```python
        data.pro.custom_validity("$msg")
        # Attrs(' data-custom-validity="$msg"')
        ```
        """
        return Attrs(f' {self.prefix}custom-validity="{escape_attr(expression)}"')

    def match_media(self, signal_name: str, expression: str) -> Attrs:
        """Sync a signal with `matchMedia`.

        `data-match-media` has no value form, so the key is spelled to
        survive HTML lowercasing and Datastar's case conversion and still
        yield `signal_name` exactly.

        ```python
        data.pro.match_media("isDark", "'prefers-color-scheme: dark'")
        # Attrs(' data-match-media:is-dark="'prefers-color-scheme: dark'"')
        data.pro.match_media("is_dark", "'prefers-color-scheme: dark'")
        # Attrs(' data-match-media:is_dark__case.kebab="'prefers-color-scheme: dark'"')
        ```
        """
        return Attrs(
            f' {self.prefix}match-media:{signal_attr_key(signal_name)}="'
            f'{escape_attr(expression)}"'
        )

    def on_raf(
        self,
        expression: str,
        *,
        throttle: Duration | Throttle | None = None,
    ) -> Attrs:
        """Run an expression on every animation frame.

        ```python
        data.pro.on_raf("draw()", throttle="100ms")
        # Attrs(' data-on-raf__throttle.100ms="draw()"')
        ```
        """
        if throttle is not None:
            return Attrs(
                f' {self.prefix}on-raf__{throttle_to_string(throttle)}="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(f' {self.prefix}on-raf="{escape_attr(expression)}"')

    def on_resize(
        self,
        expression: str,
        *,
        debounce: Duration | Debounce | None = None,
        throttle: Duration | Throttle | None = None,
    ) -> Attrs:
        """React to element resize.

        ```python
        data.pro.on_resize("layout()", debounce="50ms", throttle="100ms")
        # Attrs(' data-on-resize__debounce.50ms__throttle.100ms="layout()"')
        ```
        """
        modifiers = _timing(None, debounce, throttle)
        if modifiers:
            return Attrs(
                f' {self.prefix}on-resize__{"__".join(modifiers)}="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(f' {self.prefix}on-resize="{escape_attr(expression)}"')

    def persist(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        storage_key: str | None = None,
        session: bool = False,
    ) -> Attrs:
        """Persist signals.

        ```python
        data.pro.persist(include=["draft"], storage_key="prefs", session=True)
        # data-persist:prefs__session="{'include':'^(?:draft)(?:\\\\.|$)'}"
        ```
        """
        filters = filter_js(include, exclude)
        if storage_key is not None:
            validate_attribute_key(storage_key)
        key = (
            self.prefix + "persist"
            if storage_key is None
            else f"{self.prefix}persist:{storage_key}"
        )
        if session:
            key += "__session"
        if filters is None:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(filters)}"')

    def query_string(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        filter_empty: bool = False,
        history: bool = False,
    ) -> Attrs:
        """Sync signals with the URL.

        ```python
        data.pro.query_string(include=["page"], history=True)
        # data-query-string__history="{'include':'^(?:page)(?:\\\\.|$)'}"
        ```
        """
        modifiers: list[str] = []
        if filter_empty:
            modifiers.append("filter")
        if history:
            modifiers.append("history")

        if modifiers:
            key = f"{self.prefix}query-string__{'__'.join(modifiers)}"
        else:
            key = self.prefix + "query-string"
        filters = filter_js(include, exclude)
        if filters is None:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(filters)}"')

    def replace_url(self, expression: str) -> Attrs:
        """Replace the current browser URL.

        ```python
        data.pro.replace_url("`/page/${$page}`")
        # Attrs(' data-replace-url="`/page/${$page}`"')
        ```
        """
        return Attrs(f' {self.prefix}replace-url="{escape_attr(expression)}"')

    def scroll_into_view(
        self,
        *,
        behavior: Literal["smooth", "instant", "auto"] | None = None,
        horizontal: Literal["start", "center", "end", "nearest"] | None = None,
        vertical: Literal["start", "center", "end", "nearest"] | None = None,
        focus: bool = False,
    ) -> Attrs:
        """Scroll this element into view.

        ```python
        data.pro.scroll_into_view(behavior="smooth", vertical="center", focus=True)
        # Attrs(' data-scroll-into-view__smooth__vcenter__focus')
        ```
        """
        modifiers: list[str] = []
        if behavior is not None:
            modifiers.append(validate_attribute_key(behavior))
        if horizontal is not None:
            modifiers.append("h" + validate_attribute_key(horizontal))
        if vertical is not None:
            modifiers.append("v" + validate_attribute_key(vertical))
        if focus:
            modifiers.append("focus")

        if modifiers:
            return Attrs(f" {self.prefix}scroll-into-view__{'__'.join(modifiers)}")
        return Attrs(f" {self.prefix}scroll-into-view")

    def view_transition(self, expression: str) -> Attrs:
        """Set `view-transition-name`.

        ```python
        data.pro.view_transition("$id")
        # Attrs(' data-view-transition="$id"')
        ```
        """
        return Attrs(f' {self.prefix}view-transition="{escape_attr(expression)}"')


data = DatastarAttributes()
