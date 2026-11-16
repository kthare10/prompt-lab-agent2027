"""Before/after comparison of two runs — the only way a knowledge edit earns its place.

Reports the checks that are near-binary (discoverability, generation) separately
from the composite score, because with the within-cell spreads these models show,
a score delta at n=2 is not separable from noise while a 0%→100% flip is.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict

from harness.store import DEFAULT_PATH, Store

BINARY = [
    ("protocol_clean", "clean tool protocol"),
    ("turn_collapsed", "turn collapsed (lower is better)"),
    ("discoverable", "discoverable"),
    ("project_name_matched", "named as asked"),
    ("wrote_files", "files on disk"),
    ("generate_ok", "generate stage"),
    ("deliverables_ok", "deliverables"),
]


def load(store: Store, run_id: str) -> dict[tuple[str, str], list[dict]]:
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in store.trials(run_id):
        r = dict(row)
        r["checks"] = json.loads(r["checks"] or "{}")
        cells[(r["model"], r["task"])].append(r)
    return cells


def rate(trials: list[dict], key: str) -> float | None:
    seen = [bool(t["checks"].get(key)) for t in trials if t["checks"].get(key) is not None]
    return sum(seen) / len(seen) if seen else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--db", default=DEFAULT_PATH)
    args = ap.parse_args()

    store = Store(args.db)
    before, after = load(store, args.before), load(store, args.after)
    store.close()

    models = sorted({m for m, _ in before} | {m for m, _ in after})
    print(f"\n{args.before} → {args.after}\n")

    for label_key, label in BINARY:
        print(f"{label}")
        for model in models:
            b = [t for (m, _), ts in before.items() if m == model for t in ts]
            a = [t for (m, _), ts in after.items() if m == model for t in ts]
            rb, ra = rate(b, label_key), rate(a, label_key)
            fmt = lambda v: "  — " if v is None else f"{100 * v:3.0f}%"
            arrow = ""
            if rb is not None and ra is not None and ra != rb:
                arrow = "  ▲" if ra > rb else "  ▼"
            print(f"   {model:<22}{fmt(rb)} → {fmt(ra)}{arrow}")
        print()

    print("composite L0 (n is small and within-cell spread is large — read as indicative)\n")
    for model in models:
        b = [t["l0_score"] or 0 for (m, _), ts in before.items() if m == model for t in ts]
        a = [t["l0_score"] or 0 for (m, _), ts in after.items() if m == model for t in ts]
        if not (b and a):
            continue
        spread = max(a) - min(a)
        print(f"   {model:<22}{statistics.fmean(b):.2f} → {statistics.fmean(a):.2f}"
              f"   (after: min {min(a):.2f}, max {max(a):.2f}, spread {spread:.2f})")
    print()


if __name__ == "__main__":
    main()
