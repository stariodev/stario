"""Run compare.py under two stario builds and print one merged table.

    uv run benchmarks/html/compare_versions.py --base ../stario-main [--rows 50] [--rounds 3]

`--base` is a checkout of the other stario (its own `.venv`). The rounds
alternate between the two builds, and each row keeps its best time, so slow
moments on a busy machine hit both builds alike. The third-party contenders
run in both builds; their best time is shown once.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
THIS = HERE.parents[2]
WITH = ["--with", "dominate", "--with", "htpy", "--with", "jinja2", "--with", "tdom"]


def run(checkout: Path, rows: int) -> dict:
    out = subprocess.run(
        [
            "uv",
            "run",
            "-q",
            *WITH,
            "python",
            str(HERE.parent / "compare.py"),
            "--json",
            "--rows",
            str(rows),
        ],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--base-label", default="today")
    parser.add_argument("--label", default="typed")
    parser.add_argument("--rows", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    builds = {args.base_label: args.base.resolve(), args.label: THIS}
    best: dict[str, dict[str, dict[str, float]]] = {label: {} for label in builds}
    digests: dict[str, str] = {}
    for round_ in range(args.rounds):
        for label, checkout in builds.items():
            print(f"round {round_ + 1}/{args.rounds}: {label}", file=sys.stderr)
            result = run(checkout, args.rows)
            digests[label] = result["digest"]
            for name, r in result["results"].items():
                slot = best[label].setdefault(
                    name, {"str": float("inf"), "bytes": float("inf")}
                )
                slot["str"] = min(slot["str"], r["str"])
                slot["bytes"] = min(slot["bytes"], r["bytes"])

    if len(set(digests.values())) != 1:
        raise SystemExit(f"the builds render different pages: {digests}")

    table: list[tuple[str, float, float]] = []
    for name in best[args.label]:
        if name.startswith("stario"):
            for label in builds:
                r = best[label][name]
                table.append((f"{name} [{label}]", r["str"], r["bytes"]))
        else:
            table.append(
                (
                    name,
                    min(best[label][name]["str"] for label in builds),
                    min(best[label][name]["bytes"] for label in builds),
                )
            )
    fastest = min(t[1] for t in table)
    print(
        f"\n{args.rows}-row product page, best of {args.rounds} rounds x 7 x 2000 (us per page):\n"
    )
    print(f"  {'':<44} {'-> str':>9} {'-> bytes':>10}")
    for name, s, b in sorted(table, key=lambda t: t[1]):
        print(f"  {name:<44} {s:9.1f} {b:10.1f}   {s / fastest:5.1f}x")
    print()


if __name__ == "__main__":
    main()
