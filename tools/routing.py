#!/usr/bin/env python3
"""Which model to send which KIND of work to.

The leaderboard in `tools/stage1.py` ranks by the build column because that is
the biggest one. That is the wrong question for routing: an agent picking a model
for a review, a diagnosis or an under-specified request is not doing build work,
and PLAN's finding 3 is precisely that those are different abilities — debugging
a failed run tracks workflow-building, reviewing one does not.

So this reports one column per task *family*, and the rules it obeys are the
cohort's, not its own:

  * a family earns a column only by agreeing with itself (mean pairwise Spearman
    >= 0.2). `containerise` does not, so t03 and t06 are reported side by side
    and never summed — averaging tasks that disagree produces a column that ranks
    models by nothing. The grouping is `cohort.columns_by_family`, not a copy of
    it here: the board shows the same table, and two copies of a rule is how this
    project produced two of its worst bugs.
  * a family of one task is its own column, and says so: a model's place on it
    rests on a single measurement.
  * cells where a model was never asked anything are not results and are not
    averaged into anything.

L1 throughout, because a `depth: none` task has no artifact for L0 to score and
the judge is the only number every family shares.

    python3 tools/routing.py [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.cohort import (columns_by_family, open_ro,  # noqa: E402
                            stage1, task_specs)
from harness.store import RUNS_DIR  # noqa: E402

# How close is too close to call. The top of several families sits inside a few
# hundredths, which at n=1 per cell is not a ranking — see PLAN §5 on within-cell
# spread. Routing on a difference this small is routing on noise.
TOO_CLOSE = 0.05


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(RUNS_DIR, "results.db"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not os.path.isfile(args.db):
        raise SystemExit(f"no store at {args.db}\n  set PROMPT_LAB_RUNS or pass --db")

    db = open_ro(args.db)
    cells, dropped = stage1(db)
    specs = task_specs()
    models = sorted({m for _, m in cells})
    tasks = sorted({t for t, _ in cells})
    l1 = {k: c["l1_score"] for k, c in cells.items() if c.get("l1_score") is not None}

    cols = columns_by_family(l1, specs, tasks, models)

    if args.json:
        print(json.dumps({
            "columns": [{"key": c["key"], "tasks": c["tasks"], "pooled": c["pooled"],
                         "n_tasks": len(c["tasks"]),
                         "scores": {m: v for m, v in c["scores"].items() if v is not None}}
                        for c in cols]}, indent=2))
        return

    print("ROUTING — which model for which kind of work")
    print("=" * 78)
    print("L1 (judge). Cohort: newest trial per (task, model) over the Stage 1 runs.")
    print()

    head = f"  {'model':20s}" + "".join(f"{c['label'][:13]:>15s}" for c in cols)
    print(head)
    print(f"  {'(tasks)':20s}" + "".join(f"{len(c['tasks']):>15d}" for c in cols))
    order = sorted(models, key=lambda m: -(_mean([c["scores"].get(m) for c in cols]) or -1))
    for m in order:
        row = f"  {m:20s}"
        for c in cols:
            v = c["scores"].get(m)
            row += f"{(f'{v:.3f}' if v is not None else '—'):>15s}"
        print(row)

    print()
    print("BEST PER KIND")
    print("-" * 78)
    for c in cols:
        ranked = sorted(((v, m) for m, v in c["scores"].items() if v is not None),
                        reverse=True)
        if not ranked:
            print(f"  {c['label']:22s} no measured cells"); continue
        top, best = ranked[0]
        tied = [m for v, m in ranked if top - v <= TOO_CLOSE]
        note = ""
        if not c["pooled"]:
            note = "  (its family disagrees with itself — this task only)"
        elif len(c["tasks"]) == 1:
            note = "  (one task — one measurement per model)"
        if len(tied) > 1:
            print(f"  {c['label']:22s} {best:20s} {top:.3f}   "
                  f"within {TOO_CLOSE} of: {', '.join(tied[1:4])}{note}")
        else:
            second = ranked[1][0] if len(ranked) > 1 else 0.0
            print(f"  {c['label']:22s} {best:20s} {top:.3f}   "
                  f"clear by {top - second:.3f}{note}")

    # `drop_unmeasurable` returns (model, reason, cells) per excluded model.
    if dropped:
        print()
        print("excluded — never asked anything, so not averaged into any column:")
        for model, reason, n in dropped:
            print(f"  {model:20s} {n:>2} cells — {reason}")
    missing = sorted(m for m in models
                     if all(c["scores"].get(m) is None for c in cols))
    for m in missing:
        print(f"  {m:20s}    no judged cells")


if __name__ == "__main__":
    main()
