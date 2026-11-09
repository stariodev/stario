"""
Compile repeated HTML layouts with the `@baked` decorator.

Without `@baked`, each call builds a fresh tree and `render` walks it again.
With `@baked`, capture runs once at decoration, flattening to a segment plan;
each call splices parameters and returns a rendered `SafeString`.
See the module docstring on `baked()` and tests in `test_markup.py`.
"""

import inspect
from collections.abc import Callable
from functools import update_wrapper
from typing import Any, cast, overload

from stario.exceptions import StarioError
from stario_cython.markup import (
    check_raw_text,
    element_parts,
    shared_safe_string,
    shared_str,
    splice_attribute,
    splice_child,
)

from .escape import escape_text
from .render import render_nodes_to_string
from .slots import AttrSlot, BakeSlot, RawSlot, slot_name
from .tag import Tag
from .types import Element, HtmlElement, SafeString

type BakedNode = HtmlElement | BakeSlot | Tag
type PlanSegment = SafeString | BakeSlot | AttrSlot | RawSlot


def _reject_unsupported_parameter_kinds(sig: inspect.Signature) -> None:
    for param_name, param in sig.parameters.items():
        match param.kind:
            case inspect.Parameter.POSITIONAL_ONLY:
                detail = "positional-only parameters are not allowed (no `/`)"
            case inspect.Parameter.VAR_POSITIONAL:
                detail = "*args is not allowed"
            case inspect.Parameter.VAR_KEYWORD:
                detail = "**kwargs is not allowed"
            case _:
                continue
        raise StarioError(
            f"@baked: {detail}",
            context={"parameter": param_name},
            help_text="Use a fixed set of normal or keyword-only parameters.",
        )


def _reject_unused_parameters(
    sig: inspect.Signature,
    plan: tuple[PlanSegment, ...],
    *,
    builder_qualname: str,
) -> None:
    used: set[str] = set()
    for segment in plan:
        segment_type = type(segment)
        if segment_type is BakeSlot:
            used.add(slot_name(cast(BakeSlot, segment)))
        elif segment_type is AttrSlot:
            used.add(cast(AttrSlot, segment).name)
        elif segment_type is RawSlot:
            for part in cast(RawSlot, segment).parts:
                if type(part) is BakeSlot:
                    used.add(slot_name(part))

    for name in sig.parameters:
        if name in used:
            continue
        raise StarioError(
            f"@baked {builder_qualname}: parameter {name!r} is never used in the template",
            context={"function": builder_qualname, "parameter": name},
            help_text=(
                "Every parameter must appear as a child or whole attribute value "
                "in the builder. Remove unused parameters or reference them in "
                "the returned markup."
            ),
            example=(
                "@baked\n"
                "def row(label, price):\n"
                '    return h.Li({"data-price": price}, label)'
            ),
        )


def _flatten_node_into_segments(
    node: BakedNode | None,
    pending: list[str],
    segments: list[PlanSegment],
    *,
    builder_qualname: str,
) -> None:
    if node is None:
        return

    match node:
        case BakeSlot() | RawSlot():
            if pending:
                segments.append(SafeString("".join(pending)))
                pending.clear()
            segments.append(node)

        case SafeString():
            pending.append(node.rendered)
        case str():
            pending.append(escape_text(node))
        case bool():
            raise _static_boolean_error(builder_qualname, str(node))
        case int() | float():
            pending.append(str(node))

        case Tag():
            raise _uncalled_tag_error(builder_qualname, repr(node))

        case Element() as element:
            # Element walk must stay in sync with the render walk in
            # stario_cython.markup (element_parts gives the same pieces).
            tag_start, attrs, children, tail = element_parts(element)
            pending.append(tag_start)
            if attrs is not None:
                if type(attrs) is str:
                    pending.append(attrs)
                else:
                    for part in attrs:
                        if type(part) is str:
                            pending.append(part)
                        else:
                            if pending:
                                segments.append(SafeString("".join(pending)))
                                pending.clear()
                            segments.append(cast(AttrSlot, part))
            if children is None:
                pending.append(tail)
            else:
                if attrs is not None:
                    pending.append(">")
                for child in cast(list[BakedNode], children):
                    _flatten_node_into_segments(
                        child,
                        pending,
                        segments,
                        builder_qualname=builder_qualname,
                    )
                pending.append(tail)

        case list() as child_list:
            for child in cast(list[BakedNode], child_list):
                _flatten_node_into_segments(
                    child,
                    pending,
                    segments,
                    builder_qualname=builder_qualname,
                )

        case _:
            raise _unsupported_child_error(
                builder_qualname, type(node).__name__, str(node)[:100]
            )


def _static_boolean_error(builder_qualname: str, value: str) -> StarioError:
    return StarioError(
        f"@baked {builder_qualname}: boolean literal is not valid HTML child content",
        context={"function": builder_qualname, "value": value},
        help_text=(
            "The builder runs once at decoration with placeholder parameters. "
            "A literal True or False in the tree is fixed forever. Pass a "
            "parameter and branch outside the builder, or use str(...) for text."
        ),
        example=(
            "@baked\n"
            "def row(label, active):\n"
            '    return h.Li(label, "!" if active else "")'
        ),
    )


def _uncalled_tag_error(builder_qualname: str, tag: str) -> StarioError:
    return StarioError(
        f"@baked {builder_qualname}: uncalled Tag factory in child position",
        context={"function": builder_qualname, "tag": tag},
        help_text=(
            "Call the tag inside the builder so it returns an element, "
            "e.g. h.Div(body) not h.Div."
        ),
        example=("@baked\ndef card(body):\n    return h.Div(body)"),
    )


def _capture_child_error(builder_qualname: str, error: StarioError) -> StarioError:
    """A tag call rejected a static child while the builder ran: say it as @baked.

    The tag call sets ``stario_child_kind`` on the error (an attribute, so it
    stays out of the context users see).
    """
    context = error.context
    match getattr(error, "stario_child_kind", None):
        case "bool":
            return _static_boolean_error(
                builder_qualname, str(context.get("node_value"))
            )
        case "tag":
            return _uncalled_tag_error(builder_qualname, str(context.get("tag")))
        case "tuple" | "other":
            return _unsupported_child_error(
                builder_qualname,
                str(context.get("node_type")),
                str(context.get("node_value"))[:100],
            )
        case _:
            return error


def _unsupported_child_error(
    builder_qualname: str, node_type: str, node_value: str
) -> StarioError:
    return StarioError(
        f"@baked {builder_qualname}: unsupported static child type {node_type}",
        context={
            "function": builder_qualname,
            "node_type": node_type,
            "node_value": node_value,
        },
        help_text=(
            "Only str, int, float, SafeString, lists, elements built by "
            "h.*, and parameters may appear in a @baked builder."
        ),
        example=("@baked\ndef row(label):\n    return h.Li(label)"),
    )


def _frozen_segment_plan(
    root: BakedNode,
    *,
    builder_qualname: str,
) -> tuple[PlanSegment, ...]:
    pending: list[str] = []
    segments: list[PlanSegment] = []

    if type(root) is list:
        for node in cast(list[BakedNode], root):
            _flatten_node_into_segments(
                node,
                pending,
                segments,
                builder_qualname=builder_qualname,
            )
    else:
        _flatten_node_into_segments(
            root,
            pending,
            segments,
            builder_qualname=builder_qualname,
        )

    if pending:
        segments.append(SafeString("".join(pending)))
    return tuple(segments)


def _raw_text(value: Any) -> str:
    """One raw-text piece: `str` verbatim, otherwise the `render` ladder."""
    value_type = type(value)
    if value_type is str:
        return cast(str, value)
    if value is None:
        return ""
    if value_type is SafeString:
        return cast(SafeString, value).rendered
    if value_type is int or value_type is float:
        return str(value)
    if value_type is list:
        return "".join(_raw_text(item) for item in cast(list[Any], value))
    return render_nodes_to_string((value,), root_count=1)


def _splice_raw(tag: str, parts: tuple[Any, ...]) -> str:
    """Splice one raw-text run (`h.Script(code)`): verbatim, checked once.

    `parts` is the run with the call's values in place of the slots. The
    breakout check runs over the joined text, as `Tag.__call__` does for
    literal children, so `"</scr"` in the template and `"ipt>"` in a value
    fail together the way `"</script>"` alone does.
    """
    text = "".join([_raw_text(part) for part in parts])
    check_raw_text(text, tag)
    return text


def _generate_splice_function(
    builder: Callable[..., Any],
    sig: inspect.Signature,
    frozen_plan: tuple[PlanSegment, ...],
    static_result: SafeString | None,
) -> Callable[..., Any]:
    """Compile per-call splicing with the builder's exact signature.

    Static text goes into the generated function's globals through
    ``shared_str``: on free-threaded builds an immortal (interned) copy, so
    threads loading it write no refcount. Such a template is never freed
    there, so bake at import, not per call.
    """
    namespace: dict[str, Any] = {
        "__stario_SafeString": SafeString,
        "__stario_attr": splice_attribute,
        "__stario_child": splice_child,
        "__stario_raw": _splice_raw,
    }

    parameters_src: list[str] = []
    star_emitted = False
    for index, (name, param) in enumerate(sig.parameters.items()):
        if name.startswith("__stario"):
            raise StarioError(
                f"@baked: parameter name {name!r} collides with generated internals",
                context={"parameter": name},
                help_text="Rename the parameter; the '__stario' prefix is reserved.",
            )
        if param.kind is inspect.Parameter.KEYWORD_ONLY and not star_emitted:
            parameters_src.append("*")
            star_emitted = True
        if param.default is inspect.Parameter.empty:
            parameters_src.append(name)
        else:
            default_name = f"__stario_default_{index}"
            namespace[default_name] = param.default
            parameters_src.append(f"{name}={default_name}")

    body: list[str] = []
    if static_result is not None:
        namespace["__stario_static"] = shared_safe_string(static_result.rendered)
        body.append("    return __stario_static")
    else:
        expr_parts: list[str] = []
        for plan_index, segment in enumerate(frozen_plan):
            segment_type = type(segment)
            if segment_type is SafeString:
                static_name = f"__stario_static_{plan_index}"
                namespace[static_name] = shared_str(cast(SafeString, segment).rendered)
                expr_parts.append(static_name)
            elif segment_type is AttrSlot:
                attr_slot = cast(AttrSlot, segment)
                expr_parts.append(f"__stario_attr({attr_slot.key!r}, {attr_slot.name})")
            elif segment_type is RawSlot:
                raw_slot = cast(RawSlot, segment)
                parts_src: list[str] = []
                for part_index, part in enumerate(raw_slot.parts):
                    if type(part) is BakeSlot:
                        parts_src.append(slot_name(part))
                    else:
                        literal_name = f"__stario_raw_{plan_index}_{part_index}"
                        namespace[literal_name] = shared_str(part)
                        parts_src.append(literal_name)
                expr_parts.append(
                    f"__stario_raw({raw_slot.tag!r}, ({', '.join(parts_src)},))"
                )
            else:
                slot = cast(BakeSlot, segment)
                expr_parts.append(f"__stario_child({slot_name(slot)})")

        if not expr_parts:
            body.append('    return __stario_SafeString("")')
        elif len(expr_parts) == 1:
            body.append(f"    return __stario_SafeString({expr_parts[0]})")
        else:
            # An f-string builds the result in one BUILD_STRING step: no
            # tuple, no join call. Every part is already a str.
            fields = "".join("{" + part + "}" for part in expr_parts)
            body.append(f'    return __stario_SafeString(f"{fields}")')

    function_name = (
        builder.__name__ if builder.__name__.isidentifier() else "_baked_splice"
    )
    source = f"def {function_name}({', '.join(parameters_src)}):\n" + "\n".join(body)
    exec(compile(source, f"<stario @baked {builder.__qualname__}>", "exec"), namespace)

    generated = namespace[function_name]
    generated.__code__ = generated.__code__.replace(co_qualname=builder.__qualname__)
    return generated


@overload
def baked[**P](fn: Callable[P, HtmlElement]) -> Callable[P, SafeString]: ...


@overload
def baked[**P](
    fn: None = None,
) -> Callable[[Callable[P, HtmlElement]], Callable[P, SafeString]]: ...


def baked[**P](
    fn: Callable[P, HtmlElement] | None = None,
) -> (
    Callable[[Callable[P, HtmlElement]], Callable[P, SafeString]]
    | Callable[P, SafeString]
):
    """
    Compile `fn` once into a segment plan, then return a fast callable.

    Parameters may appear as element children or whole attribute values. Every
    call returns a `SafeString` with dynamic children and attributes already
    rendered. A parameter inside `h.Script` or `h.Style` is spliced verbatim,
    as a literal `str` child would be, and the element's breakout check runs
    on every call.

    Use this for repeated application views whose structure is stable across
    calls. Keep baked builders declarative: compute conditionals, loops, and
    derived strings outside the builder, then pass the finished child fragment
    or whole attribute value in as a parameter.

    Invalid static markup (boolean literals, uncalled tags, unsupported types)
    and unused parameters are rejected when the decorator runs, not on the
    first render call.

    Equality and truthiness on parameters (`==`, `if` / `and` / `or`) are not
    supported inside the builder — placeholders stand in at decoration time.
    Identity checks (`is`, `is not`) are not guarded: `param is not None` is
    always true while the builder runs.
    """

    def decorate(builder: Callable[P, HtmlElement]) -> Callable[P, SafeString]:
        sig = inspect.signature(builder)
        _reject_unsupported_parameter_kinds(sig)

        placeholder_kwargs = {name: BakeSlot(name) for name in sig.parameters}
        try:
            captured_tree = cast(Callable[..., HtmlElement], builder)(
                **placeholder_kwargs
            )
        except TypeError as error:
            raise StarioError(
                f"@baked: cannot call {builder.__qualname__!r} with placeholders: {error}",
                context={"function": builder.__qualname__},
            ) from error
        except StarioError as error:
            wrapped = _capture_child_error(builder.__qualname__, error)
            if wrapped is error:
                raise
            raise wrapped from error
        except Exception as error:
            raise StarioError(
                f"@baked: building {builder.__qualname__!r} failed: {type(error).__name__}: {error}",
                context={"function": builder.__qualname__},
            ) from error

        if captured_tree is None:
            raise StarioError(
                f"@baked: {builder.__qualname__!r} returned None while building",
                context={"function": builder.__qualname__},
            )

        try:
            frozen_plan = _frozen_segment_plan(
                captured_tree,
                builder_qualname=builder.__qualname__,
            )
        except RecursionError as error:
            raise StarioError(
                f"@baked: {builder.__qualname__!r} built a tree nested too deeply to flatten",
                context={"function": builder.__qualname__},
                help_text=(
                    "The flattener walks elements recursively and hit Python's "
                    "recursion limit (~1000 levels). Check for a cycle or a "
                    "self-referencing node."
                ),
            ) from error

        if sig.parameters:
            _reject_unused_parameters(
                sig, frozen_plan, builder_qualname=builder.__qualname__
            )

        static_result: SafeString | None = None
        if not sig.parameters:
            if len(frozen_plan) == 1:
                static_result = cast(SafeString, frozen_plan[0])
            else:
                static_result = SafeString(
                    "".join(
                        cast(SafeString, segment).rendered for segment in frozen_plan
                    )
                )

        splice = _generate_splice_function(builder, sig, frozen_plan, static_result)
        update_wrapper(splice, builder)
        splice.__signature__ = sig  # type: ignore[attr-defined]
        return cast(Callable[P, SafeString], splice)

    if fn is not None:
        return decorate(fn)
    return decorate
