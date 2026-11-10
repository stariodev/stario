"""Attribute rendering and small helpers for common attribute patterns.

The hot path is deliberately small: `Tag` renders plain mappings as
`key=value` pairs and accepts pre-rendered `Attrs` fragments.
"""

from typing import Any, cast

from stario.exceptions import StarioError
from stario_cython.markup import CLASSES_CACHE_MAX as _CLASSES_CACHE_MAX_C
from stario_cython.markup import aria as aria
from stario_cython.markup import classes as classes
from stario_cython.markup import classes_cache as _classes_cache
from stario_cython.markup import data as data
from stario_cython.markup import prefixed as prefixed
from stario_cython.markup import styles as styles

from .escape import escape_attribute_value
from .slots import BakeSlot, slot_name
from .types import Attrs, StyleDeclarations

# `classes()` runs in `stario_cython.markup`. Its cache holds rendered
# `class="..."` fragments for all-str token tuples (Tailwind lists repeat),
# bounded by refusing new entries past the cap, never by clearing: a clear in
# place could free an entry another thread is still reading (free-threaded
# builds). See docs/free-threading.md.
_CLASSES_CACHE: dict[tuple[Any, ...], Attrs] = _classes_cache
_CLASSES_CACHE_MAX: int = _CLASSES_CACHE_MAX_C


def _styles_checked(declarations: StyleDeclarations) -> Attrs:
    """`styles()` with every check; `stario_cython.markup.styles` handles
    the plain case itself and calls this for anything else, so the error
    messages live here only."""
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

        # A property name or value that could start another declaration or
        # block. Literal checks, not a module-level regex: free-threaded
        # CPython increfs a module global (a compiled pattern) on every load,
        # from every thread, and string constants are immortal.
        if ":" in key or ";" in key or "{" in key or "}" in key or "\\" in key:
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
            if (
                ";" in rendered_value
                or "{" in rendered_value
                or "}" in rendered_value
                or "\\" in rendered_value
            ):
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
