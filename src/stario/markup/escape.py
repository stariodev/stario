"""HTML escaping for text nodes and attribute names/values.

The three escapers run in C (`stario_cython.markup`): one pass, and the
same object back when nothing needs escaping. The names stay public here.
"""

import re
from functools import lru_cache

from stario.exceptions import StarioError
from stario_cython.markup import (
    escape_attribute_value,
    escape_sq_attribute_value,
    escape_text,
)

__all__ = [
    "escape_attribute_value",
    "escape_sq_attribute_value",
    "escape_text",
    "validate_attribute_key",
]


# Entities are NOT decoded inside attribute names, so escaping cannot repair a
# bad name — it can only mangle it. Invalid names are programmer errors: reject
# whitespace, controls, and every character that could break out of the name
# position ("'<>/=&`\).
_INVALID_KEY_CHARS = re.compile(r"[\x00-\x1f\x7f\s\"'<>/=&`\\]")


@lru_cache(maxsize=256)
def validate_attribute_key(k: str) -> str:
    """Validate an attribute `name` for the wire format (dynamic keys, `data-*` suffixes, …).

    Returns the name unchanged or raises. Cached so tag rendering and helper
    functions share one memoization path for each distinct key.
    """
    if type(k) is not str:
        raise StarioError(
            f"Invalid attribute name type: {type(k).__name__}",
            context={"attribute": str(k), "attribute_type": type(k).__name__},
            help_text="HTML attribute names must be strings.",
            example='h.Div({"id": "main"})',
        )

    if not k:
        raise StarioError(
            "Invalid attribute name: ''",
            context={"attribute": ""},
            help_text=(
                "Attribute names must be non-empty and must not contain whitespace, "
                "control characters, or any of: \" ' < > / = & ` \\"
            ),
            example='h.Div({"id": "main"})',
        )

    if _INVALID_KEY_CHARS.search(k) is None:
        return k

    raise StarioError(
        f"Invalid attribute name: {k!r}",
        context={"attribute": k[:100]},
        help_text=(
            "Attribute names must be non-empty and must not contain whitespace, "
            "control characters, or any of: \" ' < > / = & ` \\"
        ),
        example='h.Div({"data-user-id": "123"})',
    )
