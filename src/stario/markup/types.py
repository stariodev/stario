"""HTML types and small helpers."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from stario.exceptions import StarioError

from .escape import escape_text


@dataclass(frozen=True, slots=True)
class SafeString:
    """Markup inserted verbatim into rendered HTML (no escaping)."""

    rendered: str

    def __repr__(self) -> str:
        return f"SafeString({self.rendered!r})"


@dataclass(frozen=True, slots=True)
class Attrs:
    """Trusted/pre-rendered opening-tag attributes, including leading spaces."""

    rendered: str

    def __repr__(self) -> str:
        return f"Attrs({self.rendered!r})"


class Element(tuple[str, "str | list[Any] | None", "list[Any] | None", str]):
    """One built element: `(tag_start, attrs, children, tail)`.

    Only `Tag.__call__` makes these. The render walk and the `@baked`
    flattener dispatch on this exact type, so a plain tuple in child position
    (a database row, say) is never mistaken for markup and never emitted
    unescaped.

    - `tag_start`: `"<name"` without `>`, or `"<name>"` when `attrs` is None
    - `attrs`: None, a joined str, or `list[str | AttrSlot]` at bake time
    - `children`: list of child nodes, or None when `tail` closes an empty element
    - `tail`: `"</name>"` with children, else `"/>"` or `"></name>"`
    """

    __slots__ = ()


# Attribute values
type AttributeValue = str | SafeString | bool | int | float | None

# Omitted helper values (styles/class conditionals).
type Omitted = Literal[False] | None

# Inline style declarations — str/int/float only (no SafeString; escape always).
type StyleValue = str | int | float
type StyleDeclarations = Mapping[str, StyleValue | Omitted]

# Class tokens are wire names (escaped); conditionals use truthy mapping values.
type ClassInput = str | Omitted | Mapping[str, Any]

# Tag call arguments
type TagAttributes = Mapping[str, AttributeValue] | Attrs

# Render tree node built by a Tag call (kept under its older alias too).
type HtmlElementTuple = Element
type HtmlElement = str | int | float | SafeString | list[HtmlElement] | Element | None


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
