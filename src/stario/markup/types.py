"""HTML types and small helpers.

`SafeString`, `Attrs`, and `Element` are final types in `stario_cython.markup`:
immutable, and `Element` has no public constructor (only a `Tag` call makes
one). This module re-exports them and keeps the type aliases.
"""

from collections.abc import Mapping
from typing import Any, Literal

from stario.exceptions import StarioError
from stario_cython.markup import Attrs, Element, SafeString

from .escape import escape_text

__all__ = [
    "AttributeValue",
    "Attrs",
    "ClassInput",
    "Comment",
    "Element",
    "HtmlElement",
    "HtmlElementTuple",
    "Omitted",
    "SafeString",
    "StyleDeclarations",
    "StyleValue",
    "TagAttributeValue",
    "TagAttributes",
]


# Attribute values
type AttributeValue = str | SafeString | bool | int | float | None

# Omitted helper values (styles/class conditionals).
type Omitted = Literal[False] | None

# Inline style declarations — str/int/float only (no SafeString; escape always).
type StyleValue = str | int | float
type StyleDeclarations = Mapping[str, StyleValue | Omitted]

# Class tokens are wire names (escaped); conditionals use truthy mapping values.
type ClassInput = str | Omitted | Mapping[str, Any]

# Tag call arguments. A mapping value may also be an `Attrs` fragment for that
# one attribute: `{"class": classes("card", {"on": active})}`.
type TagAttributeValue = AttributeValue | Attrs
type TagAttributes = Mapping[str, TagAttributeValue] | Attrs

# Render tree node built by a Tag call (kept under its older alias too).
type HtmlElementTuple = Element
# `list[Element]` and `list[str]` are spelled out: `list` is invariant, so a
# variable `items = [h.Li(t) for t in tags]` (a `list[Element]`) would not
# pass as `list[HtmlElement]`. With them it passes wherever `HtmlElement` is
# expected: a child, or a function annotated `-> HtmlElement`.
type HtmlElement = (
    str
    | int
    | float
    | SafeString
    | Element
    | list[HtmlElement]
    | list[Element]
    | list[str]
    | None
)


def Comment(
    content: str | SafeString | int | float | None = "",
) -> SafeString:
    """`<!-- ... -->` with textual content escaped so `-->` cannot break out.

    Parsers do not decode entities inside comments, so escaped characters
    (e.g. `&gt;`) read back literally: escaping protects against breakout,
    not byte-for-byte fidelity. Pass `SafeString` when you control the bytes
    and need them verbatim.
    """
    if content is None:
        comment_text = ""
    elif type(content) is bool:
        raise StarioError(
            "Invalid comment content type: bool",
            context={
                "content_type": type(content).__name__,
                "content_value": str(content)[:100],
            },
            help_text="Comment content supports: str, SafeString, int, float, or None. Convert bool to text explicitly if needed.",
            example="render(Comment(str(is_enabled)))",
        )
    elif type(content) is SafeString:
        comment_text = content.rendered
    elif type(content) is str:
        # Escape the body so `-->` stays inert.
        comment_text = escape_text(content)
    elif type(content) is int or type(content) is float:
        comment_text = str(content)
    else:
        raise StarioError(
            f"Invalid comment content type: {type(content).__name__}",
            context={
                "content_type": type(content).__name__,
                "content_value": str(content)[:100],
            },
            help_text="Comment content supports: str, SafeString, int, float, or None.",
            example='render(Comment("Build marker"))',
        )

    return SafeString(f"<!--{comment_text}-->")
