"""
Microbenchmarks for stario.markup hot paths.

Run from the project root:

    uv run benchmarks/html/micro.py
    uv run benchmarks/html/micro.py --attrs   # per-token vs batch-at-end

Use these to catch regressions when touching `tag.py`, `attributes.py`,
`baked.py`, or `render.py`. Times are best-of-N microseconds per call.
"""

import sys
import time
from collections.abc import Mapping
from typing import Any, cast

from stario.markup import baked, classes, data, render, styles
from stario import h
from stario.markup.escape import escape_attribute_value, escape_text
from stario.markup.types import Attrs


def bench(name: str, fn, n: int = 20000, repeat: int = 7) -> float:
    for _ in range(500):
        fn()
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            fn()
        best = min(best, (time.perf_counter_ns() - t0) / n)
    us = best / 1000
    print(f"  {name:<52} {us:8.3f} us")
    return us


# --- batch-at-end alternatives (benchmark only; SafeString not supported) ----

def _classes_batch(*tokens: Any) -> Attrs:
    raw: list[str] = []
    for token in tokens:
        if token is None or token is False:
            continue
        if type(token) is dict or isinstance(token, Mapping):
            for name, include in cast(Mapping[Any, Any], token).items():
                if include:
                    raw.append(str(name))
            continue
        raw.append(str(token))
    return Attrs(f' class="{escape_attribute_value(" ".join(raw))}"')


def _styles_batch(declarations: Mapping[str, Any]) -> Attrs:
    raw: list[str] = []
    for key in declarations:
        value = declarations[key]
        if value is None or value is False:
            continue
        raw.append(f"{key}:{value};")
    return Attrs(f' style="{escape_attribute_value("".join(raw))}"')


def _attrs_main() -> None:
    """Compare per-token escape vs join-then-escape-once (plain str inputs only)."""
    tailwind = ("btn", "btn-primary", "rounded", "shadow", "px-4", "py-2", "text-sm")
    unique = tuple(f"token-{i}" for i in range(8))
    style_common = {"color": "red", "margin-top": "4px", "padding": "8px", "display": "flex"}
    style_unique = {f"prop-{i}": f"val-{i}" for i in range(8)}
    data_common = {"user-id": "123", "role": "admin", "state": "open"}
    data_flat = {"data-user-id": "123", "data-role": "admin", "data-state": "open"}
    cond = {"btn": True, "active": False, "primary": True, "shadow": True, "hidden": False}

    print("\nclasses() — repeated tokens (Tailwind-like, LRU hits):")
    bench("per-token (current)", lambda: classes(*tailwind), n=50000)
    bench("batch at end", lambda: _classes_batch(*tailwind), n=50000)

    print("\nclasses() — unique tokens each call (LRU cold):")
    bench("per-token (current)", lambda: classes(*unique), n=50000)
    bench("batch at end", lambda: _classes_batch(*unique), n=50000)

    print("\nclasses() — conditional dict (3 of 5 true):")
    bench("per-token (current)", lambda: classes(cond), n=50000)
    bench("batch at end", lambda: _classes_batch(cond), n=50000)

    print("\nstyles() — common property names (LRU hits on keys):")
    bench("per-token (current)", lambda: styles(style_common), n=50000)
    bench("batch at end", lambda: _styles_batch(style_common), n=50000)

    print("\nstyles() — unique keys/values each call (LRU cold):")
    bench("per-token (current)", lambda: styles(style_unique), n=50000)
    bench("batch at end", lambda: _styles_batch(style_unique), n=50000)

    print("\ndata() — per-attribute only (no single value to batch):")
    bench("data() helper", lambda: data(data_common), n=50000)
    bench("flat Tag dict (same attrs)", lambda: h.Div(data_flat), n=50000)
    print()


def main() -> None:
    print("\nTag construction:")
    bench("Div() cached empty", lambda: h.Div())
    bench("Div('text') single child", lambda: h.Div("text"))
    bench("Div({'class': 'card'}) str attribute", lambda: h.Div({"class": "card"}))
    bench(
        "Div(classes(5 tokens))",
        lambda: h.Div(classes("btn", "btn-primary", "rounded", "shadow", "px-4")),
    )
    bench("Div(data(2 keys))", lambda: h.Div(data({"id": 1, "k": "x"})))
    bench(
        "Div(styles(2 props))",
        lambda: h.Div(styles({"color": "red", "margin-top": "4px"})),
    )
    bench(
        "Div(classes(3 conditional tokens))",
        lambda: h.Div(classes({"btn": True, "active": False, "primary": True})),
    )

    print("\n@baked splice (3 slots: child + attrs):")
    bench("positional call", lambda: _component("Item", 5, "card on"))
    bench("keyword call", lambda: _component(name="Item", price=5, state="card on"))
    bench("call + render", lambda: render(_component("Item", 5, "card on")))

    print("\nattribute_parts join (tag build, no Tag):")
    _P1 = [' class="card"']
    _P2 = [' class="card"', ' data-id="1"']
    _P5 = [f' k{i}="v{i}"' for i in range(5)]

    def join_always(parts: list[str]) -> str:
        return "".join(parts)

    def join_len1(parts: list[str]) -> str:
        if len(parts) == 1:
            return parts[0]
        return "".join(parts)

    bench("join always (1 part)", lambda: join_always(_P1), n=500000)
    bench("len==1 branch (1 part)", lambda: join_len1(_P1), n=500000)
    bench("join always (2 parts)", lambda: join_always(_P2), n=500000)
    bench("a+b manual (2 parts)", lambda: _P2[0] + _P2[1], n=500000)

    print("\nrender walk:")
    bench("small prebuilt tree", lambda: render(_STATIC_TREE))

    print("\nescape_text (text node escaping, 60 chars):")
    _ASCII = "Lorem ipsum dolor sit amet, consectetur adipiscing elit sed"
    _ASCII_DIRTY = "Lorem <b>ipsum</b> & dolor sit amet, consectetur elit sed"
    _LATIN1 = "Lörem ipsum dolör sit ämet, consectetür adipiscing élit sed"
    _CJK = "ロレム イプサム ドロール シット アメット コンセクテトゥール アディピシング"
    _EMOJI = "Lorem ipsum 🎉 dolor sit amet, consectetur 🚀 adipiscing elit"
    _CJK_DIRTY = "ロレム <b>イプサム</b> & ドロール シット アメット コンセクテトゥール"
    bench("ascii clean", lambda: escape_text(_ASCII), n=200000)
    bench("ascii with & < >", lambda: escape_text(_ASCII_DIRTY), n=200000)
    bench("latin-1 clean", lambda: escape_text(_LATIN1), n=200000)
    bench("cjk (2-byte kind) clean", lambda: escape_text(_CJK), n=200000)
    bench("emoji (4-byte kind) clean", lambda: escape_text(_EMOJI), n=200000)
    bench("cjk with & < >", lambda: escape_text(_CJK_DIRTY), n=200000)

    print("\ntuple allocation (pure construction, no Tag):")
    _CHILDREN = ["hello"]
    _ATTRS = ' class="card" data-id="1"'
    bench(
        "4-tuple (tag_start, attrs str, children, tail) [current shape]",
        lambda: ("<div", _ATTRS, _CHILDREN, "</div>"),
        n=500000,
    )
    bench(
        "4-tuple (open fused, None attrs, children, tail)",
        lambda: (f"<div{_ATTRS}>", None, _CHILDREN, "</div>"),
        n=500000,
    )
    bench(
        "4-tuple (tag_start_no_attrs, None, children, tail) [no-attrs path]",
        lambda: ("<div>", None, _CHILDREN, "</div>"),
        n=500000,
    )
    print()


@baked
def _component(name, price, state):
    return h.Li(
        {"class": state, "data-price": price},
        h.Span({"class": "name"}, name),
    )


_STATIC_TREE = h.Div(
    classes("wrap", "p-4"),
    h.Span({"class": "name"}, "Item"),
    h.Span({"class": "price"}, "9.99"),
)


if __name__ == "__main__":
    if "--attrs" in sys.argv:
        _attrs_main()
    else:
        main()
