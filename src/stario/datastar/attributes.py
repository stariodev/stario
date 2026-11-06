"""Datastar HTML attribute helpers.

Use the exported `data` instance:

```python
from stario.datastar import at, data

h.Button(data.on("click", at.post("/cart")), "Add")
```

Each method returns a pre-rendered `Attrs` fragment (opening-tag attribute bytes).
The default instance emits normal `data-*` attributes. Create another instance when serving a
custom Datastar bundle with an aliased prefix:

```python
from stario.datastar import DatastarAttributes

star = DatastarAttributes("data-star-")
h.Div(star.text("$title"))
```

Reference: https://data-star.dev/reference/attributes
"""

from collections.abc import Mapping
from typing import Literal, cast

from stario.exceptions import StarioError
from stario.json import dumps as json_dumps
from stario.markup.escape import escape_attribute_value as escape_attr
from stario.markup.escape import escape_sq_attribute_value, validate_attribute_key
from stario.markup.types import Attrs

from ._jsevents import JSEvent
from .format import (
    Case,
    Debounce,
    FilterValue,
    SignalValue,
    Throttle,
    TimeValue,
    check_signal_name,
    debounce_to_string,
    expand_signal_paths,
    filter_js,
    js_object,
    key_case,
    require_mapping,
    signal_attr_key,
    signal_object,
    throttle_to_string,
    time_to_string,
    to_kebab_key,
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

    __slots__ = ("prefix",)

    def __init__(self, prefix: str = "data-") -> None:
        if not prefix:
            raise ValueError("Datastar attribute prefix cannot be empty.")
        self.prefix = prefix if prefix.endswith("-") else prefix + "-"

    def attr(self, key: str, expression: str) -> Attrs:
        """Set one HTML attribute from a reactive expression.

        `expression` is JavaScript evaluated in the browser — never pass
        user-supplied text; use a signal reference or `string_literal()`.

        ```python
        data.attr("title", "$item.label")
        # Attrs(' data-attr:title="$item.label"')
        ```
        """
        validate_attribute_key(key)
        _reject_uppercase_key("attr", key, "data.attrs({key: expression})")
        return Attrs(f' {self.prefix}attr:{key}="{escape_attr(expression)}"')

    def attrs(self, mapping: dict[str, str]) -> Attrs:
        """Set several HTML attributes from a mapping of expressions.

        Values are JavaScript expressions evaluated in the browser — never
        pass user-supplied text; use a signal reference or `string_literal()`.

        ```python
        data.attrs({"open": "sidebarOpen"})
        # Attrs(' data-attr="{'open':sidebarOpen}"')
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
        return Attrs(f' {self.prefix}bind{modifiers}="{escape_attr(signal_name)}"')

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
        user-supplied text; use a signal reference or `string_literal()`.

        ```python
        data.computed("fullName", "$first + $last")
        # Attrs(' data-computed="{'fullName':() =&gt; ($first + $last)}"')
        ```
        """
        return self.computeds({key: expression})

    def computeds(self, mapping: dict[str, str]) -> Attrs:
        """Create several computed signals in one `data-computed` object.

        Values are JavaScript expressions evaluated in the browser — never
        pass user-supplied text; use a signal reference or `string_literal()`.
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

    def effect(self, expression: str) -> Attrs:
        """Run a side effect when the element initializes or updates.

        ```python
        data.effect("el.focus()")
        # Attrs(' data-effect="el.focus()"')
        ```
        """
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
        expression: str,
        *,
        delay: TimeValue | None = None,
        view_transition: bool = False,
    ) -> Attrs:
        """Run an expression on element initialization.

        ```python
        data.init("setup()")
        # Attrs(' data-init="setup()"')

        data.init("setup()", delay="200ms", view_transition=True)
        # Attrs(' data-init__delay.200ms__viewtransition="setup()"')
        ```
        """
        if delay is None:
            if view_transition:
                return Attrs(
                    f' {self.prefix}init__viewtransition="{escape_attr(expression)}"'
                )
            return Attrs(f' {self.prefix}init="{escape_attr(expression)}"')

        modifiers = "delay." + time_to_string(delay)
        if view_transition:
            modifiers += "__viewtransition"
        return Attrs(f' {self.prefix}init__{modifiers}="{escape_attr(expression)}"')

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
        # Attrs(' data-json-signals__terse="{'include':'email|password'}"')
        ```
        """
        filters = filter_js(include, exclude)
        value: str | bool = filters if filters is not None else True
        if terse:
            key = self.prefix + "json-signals__terse"
        else:
            key = self.prefix + "json-signals"
        if value is True:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(cast(str, value))}"')

    def on(
        self,
        event: JSEvent | str,
        expression: str,
        *,
        once: bool = False,
        passive: bool = False,
        capture: bool = False,
        delay: TimeValue | None = None,
        debounce: Debounce | None = None,
        throttle: Throttle | None = None,
        view_transition: bool = False,
        target: Literal["window", "document", "outside"] | None = None,
        prevent: bool = False,
        stop: bool = False,
        case: Case = "kebab",
    ) -> Attrs:
        """Listen for an event.

        ```python
        data.on("click", "@get('/cart/count')")
        # Attrs(' data-on:click="@get('/cart/count')"')

        data.on("click", "$open = false", target="outside")
        # Attrs(' data-on:click__outside="$open = false"')
        ```
        """
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

        if (
            not once
            and not passive
            and not capture
            and delay is None
            and debounce is None
            and throttle is None
            and not view_transition
            and target is None
            and not prevent
            and not stop
            and case == "kebab"
            and event.islower()
            and "_" not in event
        ):
            return Attrs(f' {self.prefix}on:{event}="{escape_attr(expression)}"')

        modifiers: list[str] = []
        if once:
            modifiers.append("once")
        if passive:
            modifiers.append("passive")
        if capture:
            modifiers.append("capture")
        if target is not None:
            modifiers.append(target)
        if prevent:
            modifiers.append("prevent")
        if stop:
            modifiers.append("stop")
        if delay is not None:
            modifiers.append("delay." + time_to_string(delay))
        if debounce is not None:
            modifiers.append(debounce_to_string(debounce))
        if throttle is not None:
            modifiers.append(throttle_to_string(throttle))
        if view_transition:
            modifiers.append("viewtransition")

        kebab_event = to_kebab_key(event)
        if case != "kebab":
            modifiers.append("case." + case)
        else:
            # `myEvent` has to travel as `my-event__case.camel`: the HTML
            # parser lowercases the key and Datastar's default conversion
            # for `data-on` is kebab.
            _kebab, inferred = key_case(event)
            if inferred is not None:
                modifiers.append("case." + inferred)

        if modifiers:
            return Attrs(
                f' {self.prefix}on:{kebab_event}__{"__".join(modifiers)}="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(f' {self.prefix}on:{kebab_event}="{escape_attr(expression)}"')

    def on_intersect(
        self,
        expression: str,
        *,
        threshold: float | Literal["half", "full"] | str | None = None,
        once: bool = False,
        exit: bool = False,
        delay: TimeValue | None = None,
        debounce: Debounce | None = None,
        throttle: Throttle | None = None,
        view_transition: bool = False,
    ) -> Attrs:
        """React to viewport intersection.

        ```python
        data.on_intersect("load()", threshold=0.25, once=True, view_transition=True)
        # Attrs(' data-on-intersect__threshold.25__once__viewtransition="load()"')
        ```
        """
        modifiers: list[str] = []
        if threshold is not None:
            if isinstance(threshold, str):
                modifiers.append(
                    threshold
                    if threshold in ("half", "full")
                    else "threshold." + threshold
                )
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
        if delay is not None:
            modifiers.append("delay." + time_to_string(delay))
        if debounce is not None:
            modifiers.append(debounce_to_string(debounce))
        if throttle is not None:
            modifiers.append(throttle_to_string(throttle))
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
        expression: str,
        *,
        duration: TimeValue = "1s",
        leading: bool = False,
        view_transition: bool = False,
    ) -> Attrs:
        """Run an expression on an interval.

        ```python
        data.on_interval("tick()")
        # Attrs(' data-on-interval="tick()"')

        data.on_interval("tick()", duration="2s", leading=True)
        # Attrs(' data-on-interval__duration.2s.leading="tick()"')
        ```
        """
        if duration == "1s" and not leading:
            if view_transition:
                return Attrs(
                    f' {self.prefix}on-interval__viewtransition="'
                    f'{escape_attr(expression)}"'
                )
            return Attrs(f' {self.prefix}on-interval="{escape_attr(expression)}"')

        modifiers = "duration." + time_to_string(duration)
        if leading:
            modifiers += ".leading"
        if view_transition:
            modifiers += "__viewtransition"
        return Attrs(
            f' {self.prefix}on-interval__{modifiers}="{escape_attr(expression)}"'
        )

    def on_signal_patch(
        self,
        expression: str,
        *,
        delay: TimeValue | None = None,
        debounce: Debounce | None = None,
        throttle: Throttle | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> Attrs:
        """React to signal patches.

        ```python
        data.on_signal_patch("save()", debounce="500ms", include=["draft"])
        # Attrs(' data-on-signal-patch__debounce.500ms="save()" data-on-signal-patch-filter="{'include':'draft'}"')
        ```
        """
        modifiers: list[str] = []
        if delay is not None:
            modifiers.append("delay." + time_to_string(delay))
        if debounce is not None:
            modifiers.append(debounce_to_string(debounce))
        if throttle is not None:
            modifiers.append(throttle_to_string(throttle))

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
        """Patch one signal from a Datastar expression.

        `expression` is JavaScript evaluated in the browser — never pass
        user-supplied text; use a signal reference or `string_literal()`.
        The value is wrapped in parentheses, so a top-level comma in the
        expression stays part of the signal's value.

        ```python
        data.signal("myCount", "0", if_missing=True)
        # Attrs(' data-signals__ifmissing="{'myCount': (0)}"')
        ```
        """
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
        dotted key is a nested path — `{"ui.open": True}` sets the same
        signal as `data.signal("ui.open", ...)`.

        ```python
        data.signals({"count": 0, "open": False})
        # Attrs(" data-signals='{\"count\":0,\"open\":false}'")
        ```
        """
        value = escape_sq_attribute_value(
            json_dumps(expand_signal_paths(require_mapping("signals", payload)))
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

    def animate(self, expression: str) -> Attrs:
        """Animate element attributes over time. **Datastar Pro only.**

        ```python
        data.animate("$x")
        # Attrs(' data-animate="$x"')
        ```
        """
        return Attrs(f' {self.prefix}animate="{escape_attr(expression)}"')

    def custom_validity(self, expression: str) -> Attrs:
        """Set a custom validity message. **Datastar Pro only.**

        ```python
        data.custom_validity("$msg")
        # Attrs(' data-custom-validity="$msg"')
        ```
        """
        return Attrs(f' {self.prefix}custom-validity="{escape_attr(expression)}"')

    def match_media(self, signal_name: str, expression: str) -> Attrs:
        """Sync a signal with `matchMedia`. **Datastar Pro only.**

        `data-match-media` has no value form, so the key is spelled to
        survive HTML lowercasing and Datastar's case conversion and still
        yield `signal_name` exactly.

        ```python
        data.match_media("isDark", "'prefers-color-scheme: dark'")
        # Attrs(' data-match-media:is-dark="'prefers-color-scheme: dark'"')
        data.match_media("is_dark", "'prefers-color-scheme: dark'")
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
        throttle: Throttle | None = None,
    ) -> Attrs:
        """Run an expression on every animation frame. **Datastar Pro only.**

        ```python
        data.on_raf("draw()", throttle="100ms")
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
        debounce: Debounce | None = None,
        throttle: Throttle | None = None,
    ) -> Attrs:
        """React to element resize. **Datastar Pro only.**

        ```python
        data.on_resize("layout()", debounce="50ms", throttle="100ms")
        # Attrs(' data-on-resize__debounce.50ms__throttle.100ms="layout()"')
        ```
        """
        modifiers: list[str] = []
        if debounce is not None:
            modifiers.append(debounce_to_string(debounce))
        if throttle is not None:
            modifiers.append(throttle_to_string(throttle))

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
        """Persist signals. **Datastar Pro only.**

        ```python
        data.persist(include="draft", storage_key="prefs", session=True)
        # Attrs(' data-persist:prefs__session="{'include':'draft'}"')
        ```
        """
        filters = filter_js(include, exclude)
        value: str | bool = filters if filters is not None else True
        if storage_key is not None:
            validate_attribute_key(storage_key)
        key = (
            self.prefix + "persist"
            if storage_key is None
            else f"{self.prefix}persist:{storage_key}"
        )
        if session:
            key += "__session"
        if value is True:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(cast(str, value))}"')

    def query_string(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        filter_empty: bool = False,
        history: bool = False,
    ) -> Attrs:
        """Sync signals with the URL. **Datastar Pro only.**

        ```python
        data.query_string(include="page", history=True)
        # Attrs(' data-query-string__history="{'include':'page'}"')
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
        value: str | bool = filters if filters is not None else True
        if value is True:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(cast(str, value))}"')

    def replace_url(self, expression: str) -> Attrs:
        """Replace the current browser URL. **Datastar Pro only.**

        ```python
        data.replace_url("`/page/${$page}`")
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
        """Scroll this element into view. **Datastar Pro only.**

        ```python
        data.scroll_into_view(behavior="smooth", vertical="center", focus=True)
        # Attrs(' data-scroll-into-view__smooth__vcenter__focus')
        ```
        """
        modifiers: list[str] = []
        if behavior is not None:
            modifiers.append(behavior)
        if horizontal is not None:
            modifiers.append("h" + horizontal)
        if vertical is not None:
            modifiers.append("v" + vertical)
        if focus:
            modifiers.append("focus")

        if modifiers:
            return Attrs(f" {self.prefix}scroll-into-view__{'__'.join(modifiers)}")
        return Attrs(f" {self.prefix}scroll-into-view")

    def view_transition(self, expression: str) -> Attrs:
        """Set `view-transition-name`. **Datastar Pro only.**

        ```python
        data.view_transition("$id")
        # Attrs(' data-view-transition="$id"')
        ```
        """
        return Attrs(f' {self.prefix}view-transition="{escape_attr(expression)}"')


data = DatastarAttributes()
