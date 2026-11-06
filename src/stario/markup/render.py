"""Render HTML trees."""

from collections.abc import Iterable
from typing import Any

from stario.exceptions import StarioError

from .escape import escape_text
from .slots import AttrSlot, BakeSlot, slot_name
from .tag import Tag
from .types import Element, HtmlElement, SafeString


def _render(nodes: Iterable[Any], results: list[str]) -> None:
    """Depth-first render."""
    for node in nodes:
        if node is None:
            continue

        node_obj: object = node
        node_type = type(node_obj)

        if node_type is str:
            results.append(escape_text(node))
            continue

        if node_type is Element:
            # Element walk must stay in sync with baked._flatten_node_into_segments.
            tag_start, attrs, children, tail = node
            results.append(tag_start)
            if attrs is not None:
                if type(attrs) is str:
                    results.append(attrs)
                else:
                    for part in attrs:
                        if type(part) is str:
                            results.append(part)
                        elif type(part) is AttrSlot:
                            raise StarioError(
                                "Unfilled @baked attribute slot in render tree",
                                context={"attribute": part.key, "slot_name": part.name},
                                help_text="Elements with parameter-held attributes only exist inside @baked builders. Call the baked function to substitute slots, then render the returned fragment.",
                                example=(
                                    "@baked\n"
                                    "def link(href, label):\n"
                                    "    return h.A({'href': href}, label)\n\n"
                                    "render(link('/docs', 'Docs'))"
                                ),
                            )
                        else:
                            raise StarioError(
                                f"Invalid attribute part type: {type(part).__name__}",
                                context={"part_type": type(part).__name__},
                                help_text="Attribute parts must be pre-rendered strings.",
                            )
            if children is not None:
                if attrs is not None:
                    results.append(">")
                _render(children, results)
            results.append(tail)
            continue

        if node_type is SafeString:
            results.append(node.rendered)
            continue

        if node_type is list:
            _render(node, results)
            continue

        if node_type is int or node_type is float:
            results.append(str(node))
            continue

        if node_type is BakeSlot:
            raise StarioError(
                "Unfilled @baked slot in render tree",
                context={"slot_name": slot_name(node)},
                help_text="Call the baked function to substitute slots, then render the returned fragment.",
                example=(
                    "@baked\n"
                    "def layout(title, body):\n"
                    "    return h.Div(h.Title(title), body)\n\n"
                    "render(layout('Hi', h.P('x')))"
                ),
            )

        if node_type is Tag:
            raise StarioError(
                "Cannot render a Tag object directly",
                context={"tag": repr(node)},
                help_text="Call the tag first to create an element, e.g. use h.Div() instead of Div.",
                example='render(h.Div("Hello"))',
            )

        if node_type is bool:
            raise StarioError(
                "Boolean values are not valid HTML child content",
                context={"node_value": str(node)},
                help_text="Use a conditional to include or omit content, or convert the value to text explicitly with str(...).",
                example='h.P("enabled" if is_enabled else "disabled")',
            )

        if node_type is tuple:
            raise StarioError(
                "Cannot render a tuple as HTML content",
                context={"node_value": str(node)[:100]},
                help_text=(
                    "A tuple is not markup. Unpack it into children (h.Li(*row)) "
                    "or convert each item to text; only elements built by h.* "
                    "render as tags."
                ),
                example="h.Ul(*[h.Li(str(cell)) for cell in row])",
            )

        raise StarioError(
            f"Cannot render element of type {type(node).__name__}",
            context={
                "node_type": type(node).__name__,
                "node_value": str(node)[:100],
            },
            help_text="Only str, int, float, SafeString, lists, and elements built by h.* can be rendered.",
            example="h.Div(str(my_object))",
        )


def render_nodes_to_string(
    nodes: Iterable[Any],
    *,
    root_count: int | None = None,
) -> str:
    """Render an iterable of nodes with the same error contract as `render`."""
    try:
        results: list[str] = []
        _render(nodes, results)
        count = len(results)
        if count == 0:
            return ""
        if count == 1:
            return results[0]
        return "".join(results)

    except StarioError:
        raise
    except RecursionError as e:
        raise StarioError(
            "HTML tree is nested too deeply to render",
            context={"root_count": root_count if root_count is not None else "unknown"},
            help_text=(
                "The renderer walks elements recursively and hit Python's "
                "recursion limit (~1000 levels of nesting). Real markup never "
                "nests this deep; check for a cycle or a self-referencing node."
            ),
        ) from e
    except Exception as e:
        raise StarioError(
            f"Unexpected error while rendering HTML: {type(e).__name__}: {e}",
            context={
                "error_type": type(e).__name__,
                "error_message": str(e),
                "node_count": root_count if root_count is not None else "unknown",
            },
            help_text="Check that all HTML elements are valid types and properly structured.",
            example='render(h.Div({"class": "container"}, h.P("Hello")))',
        ) from e


def render(*nodes: HtmlElement) -> str:
    """Walk HTML fragments depth-first and return one UTF-8 string.

    Accepts any number of root nodes (variadic). Plain `str` in child position
    is HTML text-escaped; attribute escaping happens inside `Tag` when the
    opening tag is built. `SafeString` is emitted unchanged (trusted).

    Fragments from `baked` are `SafeString` values. Pass them as one root, e.g.
    `render(layout(a, b))`. You can also nest `layout(...)` wherever an element
    child is accepted, same as `h.Div(...)`.
    """
    return render_nodes_to_string(nodes, root_count=len(nodes))
