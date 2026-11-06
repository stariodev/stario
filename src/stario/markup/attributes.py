"""Attribute rendering and small helpers for common attribute patterns.

The hot path is deliberately small: `Tag` renders plain mappings as
`key=value` pairs and accepts pre-rendered `Attrs` fragments.
"""

import re
from collections.abc import Mapping
from typing import Any, cast

from stario.exceptions import StarioError

from .escape import escape_attribute_value, validate_attribute_key
from .slots import BakeSlot, bake_slot_if_present, slot_name
from .types import AttributeValue, Attrs, ClassInput, StyleDeclarations
from .wire import wire_scalar_attr_fragment

# A property name or value that could start another declaration or block.
_STYLE_KEY_BAD = re.compile(r"[:;{}\\]")
_STYLE_VALUE_BAD = re.compile(r"[;{}\\]")
# Rendered `class="..."` fragments for all-str token tuples (Tailwind lists repeat).
_CLASSES_CACHE: dict[tuple[Any, ...], Attrs] = {}
_CLASSES_CACHE_MAX = 1024


def styles(declarations: StyleDeclarations) -> Attrs:
    """Return a pre-rendered `style` attribute fragment."""
    parts: list[str] = []

    for key, value in declarations.items():
        if value is None or value is False:
            continue

        if type(key) is not str:
            raise StarioError(
                f"Invalid CSS property name type: {type(key).__name__}",
                context={"property": str(key), "property_type": type(key).__name__},
                help_text="CSS property names must be strings.",
                example='h.Div(styles({"color": "red"}))',
            )

        if key.startswith("@"):
            raise StarioError(
                f"Inline style attributes do not support at-rules like '{key}'",
                context={"property": key},
                help_text="Use normal CSS property names inside styles(), not @rules.",
                example='h.Div(styles({"color": "red"}))',
            )

        if _STYLE_KEY_BAD.search(key) is not None:
            raise StarioError(
                f"Invalid CSS property name: {key!r}",
                context={"property": key},
                help_text=(
                    "CSS property names must not contain ':', ';', '{', '}', or '\\' characters."
                ),
                example='h.Div(styles({"background-color": "red"}))',
            )

        rendered_key = escape_attribute_value(key)

        if type(value) is BakeSlot:
            raise StarioError(
                f"@baked: parameters are not supported inside styles() values (property '{rendered_key}')",
                context={"property": rendered_key, "parameter": slot_name(value)},
                help_text=(
                    "Build the style string before calling the baked template and pass "
                    "it as one whole attribute value."
                ),
            )

        value_type = type(value)
        if value_type is str:
            rendered_value = escape_attribute_value(cast(str, value))
            if _STYLE_VALUE_BAD.search(rendered_value) is not None:
                raise StarioError(
                    f"Invalid CSS value for property '{rendered_key}'",
                    context={"property": rendered_key, "value": rendered_value[:100]},
                    help_text=(
                        "CSS values in styles() must not contain ';', '{', '}', "
                        "or '\\' so they cannot inject extra declarations."
                    ),
                    example='h.Div(styles({"color": "red"}))',
                )
        elif value_type is int or value_type is float:
            rendered_value = str(value)
        else:
            raise StarioError(
                f"Invalid CSS value type for property '{rendered_key}': {type(value).__name__}",
                context={
                    "property": rendered_key,
                    "value_type": type(value).__name__,
                    "value": str(value)[:100],
                },
                help_text="CSS values must be str, int, or float.",
                example='h.Div(styles({"color": "red"}))',
            )

        parts.append(f"{rendered_key}:{rendered_value};")

    return Attrs(f' style="{"".join(parts)}"')


def classes(*tokens: ClassInput) -> Attrs:
    """Return a pre-rendered `class` attribute fragment.

    Mapping values are truthy-tested (not strictly bool): any truthy value
    includes the class name.
    """
    cached = None
    for token in tokens:
        if type(token) is not str:
            break
    else:
        cached = _CLASSES_CACHE.get(tokens)
        if cached is not None:
            return cached

    rendered: list[str] = []

    for token in tokens:
        if type(token) is str:
            rendered.append(escape_attribute_value(token))
            continue

        if token is None or token is False:
            continue

        if type(token) is dict or isinstance(token, Mapping):
            for name, include in cast(Mapping[str, Any], token).items():
                bake_slot = bake_slot_if_present(include)
                if bake_slot is not None:
                    raise StarioError(
                        f"@baked: parameters are not supported inside classes() conditional values (class {name!r})",
                        context={"class": name, "parameter": slot_name(bake_slot)},
                        help_text=(
                            "Pass the finished class mapping from outside the baked "
                            "builder, or use a whole classes() fragment as one parameter."
                        ),
                    )

                if include:
                    if type(name) is not str:
                        raise StarioError(
                            f"Invalid class token type: {type(name).__name__}",
                            context={"token_type": type(name).__name__},
                            help_text="Class tokens must be strings. Use None or False to omit a token.",
                            example='h.Div(classes({"btn": is_active}))',
                        )
                    rendered.append(escape_attribute_value(name))
            continue

        raise StarioError(
            f"Invalid class token type: {type(token).__name__}",
            context={"token_type": type(token).__name__},
            help_text="Class tokens must be strings. Use None or False to omit a token.",
            example='h.Div(classes("btn", "primary"))',
        )

    result = Attrs(f' class="{" ".join(rendered)}"')
    if cached is None and all(type(token) is str for token in tokens):
        if len(_CLASSES_CACHE) >= _CLASSES_CACHE_MAX:
            _CLASSES_CACHE.clear()
        _CLASSES_CACHE[tokens] = result
    return result


def prefixed(prefix: str, attrs: Mapping[str, AttributeValue]) -> Attrs:
    """Return pre-rendered `prefix-name` attribute fragments from a flat mapping.

    Internal primitive for `data()` and `aria()`. For other prefixes import
    from this submodule, e.g. `from stario.markup.attributes import prefixed`.
    """
    prefix = validate_attribute_key(prefix)
    out: list[str] = []

    for key, value in attrs.items():
        wire_key = f"{prefix}-{validate_attribute_key(key)}"
        bake_slot = bake_slot_if_present(value)
        if bake_slot is not None:
            raise StarioError(
                f"@baked: parameters are not supported inside {prefix}() helper values ('{wire_key}')",
                context={"attribute": wire_key, "parameter": slot_name(bake_slot)},
                help_text=(
                    "Pass the flat attribute directly to the baked template instead, "
                    f"e.g. h.Div({{{wire_key!r}: value}})."
                ),
            )

        fragment = wire_scalar_attr_fragment(wire_key, value)
        if fragment is not None:
            out.append(fragment)

    return Attrs("".join(out))


def data(attrs: Mapping[str, AttributeValue]) -> Attrs:
    """Return pre-rendered `data-*` attribute fragments from a flat mapping."""
    return prefixed("data", attrs)


def aria(attrs: Mapping[str, AttributeValue]) -> Attrs:
    """Return pre-rendered `aria-*` attribute fragments from a flat mapping."""
    return prefixed("aria", attrs)
