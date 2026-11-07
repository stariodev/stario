"""Page build + render timings for two stario builds, side by side.

    uv run benchmarks/html/page_versions.py --base ../stario-main [--rounds 3]

Each case runs under both builds, alternating rounds; each keeps its best.
Cases: the users table page from render_page.py at 10 / 100 / 500 rows, and
a Datastar todo list built with stario.datastar attributes. `--worker`
runs one build's cases and prints JSON (the driver calls this).
"""

import argparse
import gc
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
THIS = HERE.parents[2]


def best(fn, n, repeat=9):
    for _ in range(max(3, n // 10)):
        fn()
    b = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            fn()
        b = min(b, (time.perf_counter_ns() - t0) / n)
    return b / 1000


def worker() -> None:
    from stario import classes, h
    from stario.datastar import data
    from stario.markup import render

    try:
        from stario.markup.render import render_bytes
    except ImportError:

        def render_bytes(*nodes):
            return render(*nodes).encode("utf-8")

    def row(i):
        return h.Tr(
            h.Td(classes("px-3", "py-2", "font-mono"), f"user-{i}"),
            h.Td(classes("px-3", "py-2"), f"Name {i} & co <b>"),
            h.Td(classes("px-3", "py-2", "text-right"), i * 7),
            h.Td(h.A({"href": f"/users/{i}"}, classes("link"), "open")),
        )

    def page(n):
        return h.Html(
            h.Head(
                h.Title("Users"),
                h.Link({"rel": "stylesheet", "href": "/static/app.css"}),
            ),
            h.Body(
                classes("min-h-screen", "bg-base-100"),
                h.Nav(
                    classes("navbar"),
                    h.A({"href": "/"}, "Home"),
                    h.A({"href": "/users"}, "Users"),
                ),
                h.Main(
                    classes("container", "mx-auto", "p-6"),
                    h.H1(classes("text-2xl", "font-semibold"), "Users"),
                    h.Table(
                        classes("table"),
                        h.Thead(h.Tr(*[h.Th(c) for c in ("id", "name", "score", "")])),
                        h.Tbody(*[row(i) for i in range(n)]),
                    ),
                ),
                h.Footer(classes("footer"), h.P("Made with Stario")),
            ),
        )

    items = [(f"Buy milk & eggs {i}", i % 3 == 0) for i in range(20)]

    def todos():
        return h.Section(
            {"id": "todos"},
            data.signals({"filter": "all", "draft": ""}),
            h.Form(
                data.on("submit", "@post('/todos')", prevent=True),
                h.Input(
                    {"type": "text", "name": "draft", "placeholder": "What next?"},
                    data.bind("draft"),
                ),
                h.Button({"type": "submit"}, classes("btn", "btn-primary"), "Add"),
            ),
            h.Ul(
                classes("list"),
                [
                    h.Li(
                        {"id": f"todo-{i}"},
                        classes("todo", {"done": done}),
                        data.show(f"$filter == 'all' || {str(done).lower()}"),
                        h.Input(
                            {"type": "checkbox", "checked": done},
                            data.on("change", f"@patch('/todos/{i}')"),
                        ),
                        h.Span(text),
                        h.Button(
                            {"type": "button"},
                            classes("btn", "btn-ghost"),
                            data.on("click", f"@delete('/todos/{i}')"),
                            "×",
                        ),
                    )
                    for i, (text, done) in enumerate(items)
                ],
            ),
        )

    cases = {}
    for rows, n in ((10, 3000), (100, 400), (500, 80)):
        tree = page(rows)
        cases[f"page {rows} rows: build"] = best(lambda rows=rows: page(rows), n)
        cases[f"page {rows} rows: render -> bytes"] = best(
            lambda tree=tree: render_bytes(tree), n
        )
        cases[f"page {rows} rows: build + render -> bytes"] = best(
            lambda rows=rows: render_bytes(page(rows)), n
        )
    gc.disable()
    cases["page 500 rows: build + render, gc off"] = best(
        lambda: render_bytes(page(500)), 80
    )
    gc.enable()
    tree = todos()
    cases["datastar todos (20): build"] = best(todos, 3000)
    cases["datastar todos (20): render -> bytes"] = best(
        lambda: render_bytes(tree), 3000
    )
    cases["datastar todos (20): build + render -> bytes"] = best(
        lambda: render_bytes(todos()), 3000
    )
    leaf = h.Td("x")
    cases["render one small element -> bytes"] = best(
        lambda: render_bytes(leaf), 100_000
    )
    digest = hashlib.sha256(render_bytes(page(100)) + render_bytes(todos())).hexdigest()
    print(json.dumps({"digest": digest, "cases": cases}))


def main() -> None:
    if "--worker" in sys.argv:
        worker()
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--base-label", default="today")
    parser.add_argument("--label", default="typed")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    builds = {args.base_label: args.base.resolve(), args.label: THIS}
    best_of: dict[str, dict[str, float]] = {label: {} for label in builds}
    digests: dict[str, str] = {}
    for round_ in range(args.rounds):
        for label, checkout in builds.items():
            print(f"round {round_ + 1}/{args.rounds}: {label}", file=sys.stderr)
            out = subprocess.run(
                ["uv", "run", "-q", "python", str(HERE), "--worker"],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            result = json.loads(out.strip().splitlines()[-1])
            digests[label] = result["digest"]
            for name, t in result["cases"].items():
                best_of[label][name] = min(best_of[label].get(name, float("inf")), t)
    if len(set(digests.values())) != 1:
        raise SystemExit(f"the builds render different markup: {digests}")
    a, b = args.base_label, args.label
    print(
        f"\n{'us (best of ' + str(args.rounds) + ' rounds)':<46} {a:>9} {b:>9}   speedup"
    )
    for name in best_of[b]:
        x, y = best_of[a][name], best_of[b][name]
        print(f"  {name:<44} {x:9.2f} {y:9.2f}   {x / y:5.2f}x")


if __name__ == "__main__":
    main()
