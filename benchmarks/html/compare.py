"""
Compare stario HTML generation against jinja2, htpy, dominate, and tdom.

Every contender renders the same product page (autoescaping on everywhere).
Run from the project root:

    uv run --with dominate --with htpy --with jinja2 --with tdom benchmarks/html/compare.py

Times are best-of-N microseconds per full page; lower is better. The
`bytes` column adds the UTF-8 encode a response needs (stario: one
`render_bytes` call). `--json` prints machine-readable results, which
`compare_versions.py` uses to put two stario builds side by side.
"""

import hashlib
import json
import re
import sys
import time
from string.templatelib import Template

import htpy as hp
import jinja2
from dominate import tags as dt
from tdom import html

from stario.markup import baked, classes, data, render

try:
    from stario.markup.render import render_bytes
except ImportError:  # stario before the typed core

    def render_bytes(*nodes):
        return render(*nodes).encode("utf-8")


from stario import h

ROW_COUNT = int(sys.argv[sys.argv.index("--rows") + 1]) if "--rows" in sys.argv else 50
ROWS = [
    {"id": i, "name": f"Item {i}", "active": i % 3 == 0, "price": f"{i}.99"}
    for i in range(ROW_COUNT)
]


def bench(fn, n: int = 2000, repeat: int = 7) -> float:
    for _ in range(200):
        fn()
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            fn()
        best = min(best, (time.perf_counter_ns() - t0) / n)
    return best / 1000  # us


# --- stario: naive (tree built per call) ---------------------------------------


def _naive_tree():
    return h.Html(
        {"lang": "en"},
        h.Body(
            classes("bg", "min-h-screen"),
            h.Main(
                classes("mx-auto", "px-4"),
                h.H1({"class": "title"}, "Products"),
                h.Ul(
                    {"class": "product-list"},
                    *[
                        h.Li(
                            classes("card", "active" if r["active"] else None),
                            data({"id": r["id"]}),
                            h.Span({"class": "name"}, r["name"]),
                            h.Span({"class": "price"}, r["price"]),
                        )
                        for r in ROWS
                    ],
                ),
            ),
        ),
    )


def stario_naive() -> str:
    return render(_naive_tree())


def stario_naive_bytes() -> bytes:
    return render_bytes(_naive_tree())


# --- stario: @baked row + layout ------------------------------------------------


@baked
def _row(cls, rid, name, price):
    return h.Li(
        {"class": cls, "data-id": rid},
        h.Span({"class": "name"}, name),
        h.Span({"class": "price"}, price),
    )


@baked
def _page(rows):
    return h.Html(
        {"lang": "en"},
        h.Body(
            classes("bg", "min-h-screen"),
            h.Main(
                classes("mx-auto", "px-4"),
                h.H1({"class": "title"}, "Products"),
                h.Ul({"class": "product-list"}, rows),
            ),
        ),
    )


def _baked_page():
    return _page(
        [
            _row(
                "card active" if r["active"] else "card",
                r["id"],
                r["name"],
                r["price"],
            )
            for r in ROWS
        ]
    )


def stario_baked() -> str:
    return render(_baked_page())


def stario_baked_bytes() -> bytes:
    return render_bytes(_baked_page())


# --- htpy -----------------------------------------------------------------------


def htpy_page() -> str:
    return str(
        hp.html(lang="en")[
            hp.body(class_=["bg", "min-h-screen"])[
                hp.main(class_=["mx-auto", "px-4"])[
                    hp.h1(class_="title")["Products"],
                    hp.ul(class_="product-list")[
                        (
                            hp.li(
                                class_=["card", "active" if r["active"] else None],
                                data_id=r["id"],
                            )[
                                hp.span(class_="name")[r["name"]],
                                hp.span(class_="price")[r["price"]],
                            ]
                            for r in ROWS
                        )
                    ],
                ]
            ]
        ]
    )


# --- dominate ---------------------------------------------------------------------


def dominate_page() -> str:
    doc = dt.html(lang="en")
    with doc:
        with dt.body(cls="bg min-h-screen"):
            with dt.main(cls="mx-auto px-4"):
                dt.h1("Products", cls="title")
                with dt.ul(cls="product-list"):
                    for r in ROWS:
                        cls = "card active" if r["active"] else "card"
                        with dt.li(cls=cls, data_id=r["id"]):
                            dt.span(r["name"], cls="name")
                            dt.span(r["price"], cls="price")
    return doc.render(pretty=False)


# --- jinja2 ------------------------------------------------------------------------

_env = jinja2.Environment(autoescape=True)
_tmpl = _env.from_string(
    '<html lang="en"><body class="bg min-h-screen"><main class="mx-auto px-4">'
    '<h1 class="title">Products</h1><ul class="product-list">{% for r in rows %}'
    '<li class="card{% if r.active %} active{% endif %}" data-id="{{ r.id }}">'
    '<span class="name">{{ r.name }}</span><span class="price">{{ r.price }}</span>'
    "</li>{% endfor %}</ul></main></body></html>"
)


def jinja_page() -> str:
    return _tmpl.render(rows=ROWS)


# --- tdom (Python 3.14 t-strings) ------------------------------------------------


def _tdom_row(*, cls: str, rid: int, name: str, price: str) -> Template:
    return t'<li class={cls} data-id={rid}><span class="name">{name}</span><span class="price">{price}</span></li>'


def _tdom_page(children: Template) -> Template:
    return t'<html lang="en"><body class={["bg", "min-h-screen"]}><main class={["mx-auto", "px-4"]}><h1 class="title">Products</h1><ul class="product-list">{children}</ul></main></body></html>'


def tdom_naive() -> str:
    """Inline t-string with a list comprehension for rows."""
    return html(t"""
<html lang="en">
<body class={["bg", "min-h-screen"]}>
<main class={["mx-auto", "px-4"]}>
<h1 class="title">Products</h1>
<ul class="product-list">
{
        [
            t'<li class={["card", "active" if r["active"] else None]} data-id={r["id"]}><span class="name">{r["name"]}</span><span class="price">{r["price"]}</span></li>'
            for r in ROWS
        ]
    }
</ul>
</main>
</body>
</html>
""")


def tdom_components() -> str:
    """Reusable Row + Page component functions (fastest tdom shape for this page)."""
    rows = [
        _tdom_row(
            cls="card active" if r["active"] else "card",
            rid=r["id"],
            name=r["name"],
            price=r["price"],
        )
        for r in ROWS
    ]
    return html(_tdom_page(rows))


# --- run ---------------------------------------------------------------------------


def _normalized(page: object) -> str:
    """The page without a doctype or whitespace between tags (htpy adds a
    doctype; the naive t-string keeps its newlines)."""
    text = re.sub(r"(?i)^<!doctype html>", "", str(page).strip())
    return re.sub(r">\s+<", "><", text)


def main() -> None:
    contenders = [
        ("stario @baked", stario_baked, stario_baked_bytes),
        ("stario naive (tree per call)", stario_naive, stario_naive_bytes),
        ("jinja2 (compiled, autoescape)", jinja_page, None),
        ("htpy", htpy_page, None),
        ("dominate", dominate_page, None),
        ("tdom (components)", tdom_components, None),
        ("tdom (naive t-string)", tdom_naive, None),
    ]
    expected = stario_naive()
    results = {}
    for name, fn, fn_bytes in contenders:
        if fn_bytes is None:
            fn_bytes = lambda fn=fn: fn().encode("utf-8")  # noqa: E731
        if fn is stario_naive or fn is stario_baked:
            assert fn() == expected, name
            assert fn_bytes() == expected.encode(), name
        # Every contender must build the same page, or the times mean nothing.
        assert _normalized(fn()) == _normalized(expected), name
        results[name] = {"str": bench(fn), "bytes": bench(fn_bytes)}

    if "--json" in sys.argv:
        digest = hashlib.sha256(expected.encode()).hexdigest()
        print(json.dumps({"rows": ROW_COUNT, "digest": digest, "results": results}))
        return

    fastest = min(r["str"] for r in results.values())
    print(f"\n{ROW_COUNT}-row product page, per page (best of 7 x 2000):\n")
    print(f"  {'':<34} {'str':>9}    {'bytes':>9}")
    for name, r in sorted(results.items(), key=lambda item: item[1]["str"]):
        print(
            f"  {name:<34} {r['str']:9.1f} us {r['bytes']:9.1f} us   {r['str'] / fastest:5.1f}x"
        )
    print()


if __name__ == "__main__":
    main()
