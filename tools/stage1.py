#!/usr/bin/env python3
"""The Stage 1 result, derived from the store rather than remembered.

    python3 tools/stage1.py                # the whole analysis
    python3 tools/stage1.py --json         # the same numbers, machine-readable
    python3 tools/stage1.py --markdown     # a block to paste into PLAN.md

Why this exists. The Stage 1 headline — mechanical grading tracks the judge on
build tasks and not on judgement tasks — was computed in an ad-hoc script that
was never committed and is now gone. `report.py` takes a single `--run-id`, and
Stage 1 spans six (three providers, plus `s1b-*` for t07 and `s1c-*` for the t06
re-run), so nothing checked in could even assemble the rows it was computed
from. A finding that cannot be re-derived is a claim, not a result.

Re-deriving it also showed why the rule had to be written down. The prose said
build 0.756 / judgement -0.044. Pooling the same store with the two unserved
models dropped gives 0.763 / -0.114; keeping them gives 0.797 / -0.041. Neither
reproduces the pair, so the original used a third rule nobody recorded. The
numbers below are what `harness/cohort.py` defines, and they move only when that
definition moves.

Everything here reads the store read-only and touches no workflow, so it runs on
the host — unlike anything that plans or executes (D3).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from harness.cohort import (STAGE1_RUNS, columns_for, family,  # noqa: E402
                            kind, open_ro, poolable, stage1, task_specs)  # noqa: E402
from harness.stats import permutation_p, spearman  # noqa: E402
from harness.store import RUNS_DIR  # noqa: E402

DB = f"{RUNS_DIR}/results.db"


# ── the analysis ──────────────────────────────────────────────────────────

def analyse(db_path: str) -> dict:
    db = open_ro(db_path)
    cells, dropped = stage1(db)
    db.close()
    if not cells:
        raise SystemExit(f"no Stage 1 trials in {db_path}")

    specs = task_specs()
    tasks = sorted({t for t, _ in cells})
    models = sorted({m for _, m in cells})
    by_task: dict[str, list[dict]] = defaultdict(list)
    for (t, _), c in cells.items():
        by_task[t].append(c)

    # 1. Does L0 agree with L1, per task?
    agreement = []
    for t in tasks:
        rs = [c for c in by_task[t] if c["l0_score"] is not None and c["l1_score"] is not None]
        l0 = [float(c["l0_score"]) for c in rs]
        l1 = [float(c["l1_score"]) for c in rs]
        agreement.append({
            "task": t,
            "l0_applicable": any(c["l0_score"] is not None for c in by_task[t]),
            "kind": kind(specs.get(t, {})),
            "family": family(specs.get(t, {})),
            "n": len(rs),
            "unjudged": len(by_task[t]) - len(rs),
            "rho": spearman(l0, l1) if len(rs) > 2 else float("nan"),
            "p": permutation_p(l0, l1) if len(rs) > 2 else float("nan"),
            "mean_l0": statistics.fmean(l0) if l0 else float("nan"),
            # From every judged cell, not only the ones with an L0 to pair it
            # with: on a task with no mechanical score that would be none of them.
            "mean_l1": statistics.fmean(
                [float(c["l1_score"]) for c in by_task[t] if c["l1_score"] is not None]
                or [float("nan")]),
        })
    by_kind = {
        k: [a["rho"] for a in agreement if a["kind"] == k and a["rho"] == a["rho"]]
        for k in ("build", "judgement")
    }

    # 2. May a group of tasks be averaged at all? Only if it agrees with itself.
    l1 = {(t, m): float(c["l1_score"]) for (t, m), c in cells.items()
          if c["l1_score"] is not None}

    def group_of(axis, name: str) -> list[str]:
        return [t for t in tasks if axis(specs.get(t, {})) == name]

    def mean_over(ts: list[str], m: str) -> float | None:
        got = [l1[(t, m)] for t in ts if (t, m) in l1]
        return statistics.fmean(got) if got else None

    groups = []
    for axis_name, axis in (("kind", kind), ("family", family)):
        for name in sorted({axis(specs.get(t, {})) for t in tasks}):
            ts = group_of(axis, name)
            ok, rho, pairs = poolable(l1, ts, models)
            groups.append({"axis": axis_name, "name": name, "tasks": ts,
                           "poolable": ok, "rho": rho, "pairs": pairs,
                           "pairwise": [
                               {"a": x, "b": y,
                                "rho": spearman(*_paired(l1, x, y, models)),
                                "p": permutation_p(*_paired(l1, x, y, models))}
                               for i, x in enumerate(ts) for y in ts[i + 1:]
                               if len(_paired(l1, x, y, models)[0]) > 2] if len(ts) > 1 else []})

    # 3. The leaderboard. A group that may be pooled is one column; a group
    #    that may not becomes one column per task. Nothing is averaged across a
    #    boundary the data does not support — that mistake is what produced a
    #    "judgement" column ranking models by nothing.
    columns = columns_for(l1, specs, tasks, models)

    # 4. Does the first column predict the others? Asked per column, because
    #    there is no second column to ask it of once the pooling stops.
    primary = max(columns, key=lambda c: len(c["tasks"]))
    against = []
    for col in columns:
        if col is primary:
            continue
        pair = [(primary["scores"][m], col["scores"][m]) for m in models
                if primary["scores"][m] is not None and col["scores"][m] is not None]
        x, y = [u for u, _ in pair], [v for _, v in pair]
        rank_p = {m: i for i, m in enumerate(
            sorted((m for m in models if primary["scores"][m] is not None),
                   key=lambda m: -primary["scores"][m]), 1)}
        rank_c = {m: i for i, m in enumerate(
            sorted((m for m in models if col["scores"][m] is not None),
                   key=lambda m: -col["scores"][m]), 1)}
        shifts = sorted(({"model": m, "from": rank_p[m], "to": rank_c[m],
                          "shift": rank_p[m] - rank_c[m]}
                         for m in models if m in rank_p and m in rank_c),
                        key=lambda r: -abs(r["shift"]))
        against.append({"column": col["key"], "n_models": len(pair),
                        "rho": spearman(x, y) if len(pair) > 2 else float("nan"),
                        "p": permutation_p(x, y) if len(pair) > 2 else float("nan"),
                        "shifts": shifts})

    # How much of each model's row is a measurement at all. A score averaged
    # over the cells that reached a model is the only score worth printing, and
    # it is not comparable across models until the reader can see how many cells
    # are behind it: glm-5's build column moves 0.364 -> 0.680 when its ten
    # non-events come out, and what is left is three tasks.
    coverage = {m: (sum(1 for (t, mm), c in cells.items()
                        if mm == m and not c["never_reached"]),
                    sum(1 for (t, mm) in cells if mm == m))
                for m in models}

    task_count = {g["name"]: len(g["tasks"]) for g in groups if g["axis"] == "family"}
    return {
        "db": db_path,
        "cells": len(cells),
        "tasks": tasks,
        "models": models,
        "excluded_models": [{"model": m, "reason": r, "cells": n} for m, r, n in dropped],
        "status": {s: sum(1 for c in cells.values() if c["status"] == s)
                   for s in sorted({c["status"] for c in cells.values()})},
        "unjudged": sum(1 for c in cells.values() if c["l1_score"] is None),
        "agreement": agreement,
        "mean_rho": {k: (statistics.fmean(v) if v else float("nan")) for k, v in by_kind.items()},
        "groups": groups,
        "task_count": task_count,
        "columns": columns,
        "coverage": coverage,
        "never_reached": sorted(
            ({"task": t, "model": m, "status": c["status"],
              "reason": ("the request had no messages"
                         if "at least one message" in (c["error"] or "")
                         else "nothing was read — zero input tokens")}
             for (t, m), c in cells.items() if c["never_reached"]),
            key=lambda r: (r["model"], r["task"])),
        "primary": primary["key"],
        "against_primary": against,
    }


def _paired(l1: dict, x: str, y: str, models: list[str]) -> tuple[list[float], list[float]]:
    both = [(l1[(x, m)], l1[(y, m)]) for m in models if (x, m) in l1 and (y, m) in l1]
    return [a for a, _ in both], [b for _, b in both]


# ── rendering ─────────────────────────────────────────────────────────────

def num(x: float | None, w: int = 6, dp: int = 3) -> str:
    return f"{'—':>{w}}" if x is None or x != x else f"{x:{w}.{dp}f}"


def render(a: dict) -> str:
    out: list[str] = []
    w = out.append

    w("STAGE 1 — pooled result")
    w("=" * 72)
    w(f"{a['cells']} cells · {len(a['models'])} models × {len(a['tasks'])} tasks · "
      f"{a['unjudged']} unjudged")
    w("  status: " + ", ".join(f"{k} {v}" for k, v in a["status"].items()))
    for e in a["excluded_models"]:
        w(f"  excluded: {e['model']} ({e['cells']} cells) — {e['reason']}")

    w("")
    w("1. Does the mechanical score agree with the judge?")
    w("-" * 72)
    w("Spearman rho between L0 and L1 across models, one task per row.")
    w("")
    w(f"  {'task':26s} {'kind':10s} {'n':>2s} {'rho':>7s} {'p':>7s} {'mean L0':>8s} {'mean L1':>8s}")
    for r in sorted(a["agreement"], key=lambda r: (r["kind"], r["task"])):
        if not r["l0_applicable"]:
            w(f"  {r['task']:26s} {r['kind']:10s} {len(a['models']):2d} "
              f"{'no mechanical score — see below':>32s} {num(r['mean_l1'], 8)}")
            continue
        flag = "" if r["p"] == r["p"] and r["p"] < 0.05 else "   ns"
        w(f"  {r['task']:26s} {r['kind']:10s} {r['n']:2d} {num(r['rho'], 7)} "
          f"{num(r['p'], 7)} {num(r['mean_l0'], 8)} {num(r['mean_l1'], 8)}{flag}")
    w("")
    n_build = sum(1 for r in a["agreement"] if r["kind"] == "build")
    w(f"  mean rho over {n_build:2d} build tasks: {num(a['mean_rho']['build'], 6)}")
    w("")
    w("  There is no row for the judgement tasks because L0 is no longer computed")
    w("  on a task that asks for no artifact. That is this finding, applied: when")
    w("  it was computed there it came to mean rho -0.114 against +0.763 on the")
    w("  build tasks, and on t10 it correlated with a trial's `write_calls` at")
    w("  +0.663 (p 0.007) while the judge's score correlated with those writes at")
    w("  +0.057 — L0 was measuring whether files appeared, which t10's own")
    w("  `must_not` forbids. Those two numbers are history, not re-derivable: the")
    w("  fix removed the measurand. See PLAN.md, finding 1.")

    w("")
    w("2. May these tasks be averaged together?")
    w("-" * 72)
    w("A group earns one column only by agreeing with itself: mean Spearman rho")
    w("between every pair of tasks in it, across models.")
    w("")
    w(f"  {'group':14s} {'axis':8s} {'tasks':>5s} {'pairs':>5s} {'rho':>7s}  verdict")
    for g in a_groups(a):
        verdict = ("one task — its own column" if len(g["tasks"]) < 2
                   else "pooled" if g["poolable"] else "NOT pooled — reported per task")
        w(f"  {g['name']:14s} {g['axis']:8s} {len(g['tasks']):5d} {g['pairs']:5d} "
          f"{num(g['rho'], 7)}  {verdict}")
    for g in a_groups(a):
        if len(g["tasks"]) > 1 and not g["poolable"]:
            w("")
            w(f"  {g['name']} does not hold together:")
            for pw in g["pairwise"]:
                flag = "" if pw["p"] == pw["p"] and pw["p"] < 0.05 else "   ns"
                w(f"    {pw['a']:22s} vs {pw['b']:22s} rho {num(pw['rho'], 7)} "
                  f"p {num(pw['p'], 6)}{flag}")
    w("")
    w("  Averaging tasks that do not correlate with each other produces a column")
    w("  that ranks models by nothing. Where that happens the tasks are reported")
    w("  side by side below and never summed.")

    w("")
    w("3. Leaderboard  (mean L1)")
    w("-" * 72)
    cols = a["columns"]
    prim = next(c for c in cols if c["key"] == a["primary"])
    w(f"Ranked by {prim['label']} ({len(prim['tasks'])} tasks). L1 throughout: on a task with")
    w("no artifact there is nothing for L0 to measure, so the judge is the only")
    w("score every column shares.")
    w("")
    w(f"  {'model':22s} {'reached':>8s}" + "".join(f"{c['label'][:15]:>16s}" for c in cols))
    w("  " + f"{'(tasks)':22s} {'':8s}" + "".join(f"{len(c['tasks']):>16d}" for c in cols))
    for m in sorted(a["models"], key=lambda m: -(prim["scores"][m] or 0)):
        ok, tot = a["coverage"][m]
        cov = f"{ok}/{tot}"
        w(f"  {m:22s} {cov:>8s}" + "".join(num(c["scores"][m], 16) for c in cols))
    w("")
    thin = [m for m in a["models"] if a["coverage"][m][0] < a["coverage"][m][1]]
    if thin:
        w("  `reached` is the cells where a model was actually asked something. The")
        w("  rest are not results and are not averaged into anything:")
        for m in thin:
            ok, tot = a["coverage"][m]
            w(f"    {m:22s} {ok:2d}/{tot:<2d}  " + ", ".join(
                sorted(r["task"] for r in a["never_reached"] if r["model"] == m))[:46])

    w("")
    w("4. Does the first column predict the others?")
    w("-" * 72)
    for r in a["against_primary"]:
        flag = "" if r["p"] == r["p"] and r["p"] < 0.05 else "  ns"
        w(f"  {prim['label']:14s} vs {r['column']:22s} rho {num(r['rho'], 7)} "
          f"p {num(r['p'], 6)}  n={r['n_models']}{flag}")
        big = [x for x in r["shifts"] if abs(x["shift"]) >= 5][:3]
        for x in big:
            w(f"      {x['model']:20s} #{x['from']:<2d} → #{x['to']:<2d} ({x['shift']:+d})")
    w("")
    w("  Each row is a separate question, because there is no single second")
    w("  column left to ask it of. A model's place on a one-task column rests on")
    w("  one measurement; widening those is what would make them rankings.")
    return "\n".join(out)


def a_groups(a: dict) -> list[dict]:
    """Groups worth printing: every kind, and the families that add something."""
    kinds = [g for g in a["groups"] if g["axis"] == "kind"]
    fams = [g for g in a["groups"] if g["axis"] == "family"
            and not any(k["tasks"] == g["tasks"] for k in kinds)]
    return kinds + fams


def _shift(row: dict | None) -> str:
    return f"{row['shift']:+d}" if row and row["shift"] else "·"


def render_markdown(a: dict) -> str:
    cols = a["columns"]
    prim = next(c for c in cols if c["key"] == a["primary"])
    rows = "\n".join(
        f"| {m} | " + " | ".join(num(c["scores"][m], 1) for c in cols) + " |"
        for m in sorted(a["models"], key=lambda m: -(prim["scores"][m] or 0)))
    head = "| model | " + " | ".join(
        f"{c['label']} ({len(c['tasks'])})" for c in cols) + " |"
    rule = "|---" * (len(cols) + 1) + "|"
    coh = "\n".join(
        f"| {g['name']} | {len(g['tasks'])} | {g['pairs']} | {num(g['rho'], 1)} | "
        + ("pooled" if g["poolable"] else "**not pooled**") + " |"
        for g in a_groups(a) if len(g["tasks"]) > 1)
    pred = "\n".join(
        f"| {prim['label']} vs {r['column']} | {num(r['rho'], 1)} | {num(r['p'], 1)} |"
        for r in a["against_primary"])
    n_build = sum(1 for r in a["agreement"] if r["kind"] == "build")
    n_judge = sum(1 for r in a["agreement"] if r["kind"] == "judgement")
    runs = ", ".join(x.rstrip("%") + "*" for x in STAGE1_RUNS)
    return f"""### Stage 1 — pooled result

Cohort: `harness/cohort.py` — the newest trial per (task, model) over {runs}, minus the
models that were never served. {a["cells"]} cells, {len(a["models"])} models x
{len(a["tasks"])} tasks. Re-derive with `python3 tools/stage1.py`.

**L0 tracks the judge on build tasks and does not on judgement tasks.** Mean Spearman rho
between L0 and L1 across models: **{num(a["mean_rho"]["build"], 1)}** over {n_build} build
tasks, **{num(a["mean_rho"]["judgement"], 1)}** over {n_judge} judgement tasks. A judgement
task (`depth: none`) produces no artifact, so every mechanical check below `wrote_files` is
inapplicable and L0 grades the transcript against them anyway.

**A group of tasks is averaged only where it agrees with itself.**

| group | tasks | pairs | mean inter-task rho | |
|---|---|---|---|---|
{coh}

**So the board carries one build column and the rest task by task.** Asking whether the build
column predicts each of them:

| | rho | p |
|---|---|---|
{pred}

| model | {" | ".join(f"{c['label']} ({len(c['tasks'])})" for c in cols)} |
{rule}
{rows}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()

    # Same failure serve.py hit: started without PROMPT_LAB_RUNS, `DB` points at
    # the repo's `runs/` — which exists, holds synced trial records, and has no
    # store in it. Say which knob is wrong rather than raising sqlite3's
    # "unable to open database file".
    if not os.path.isfile(args.db):
        raise SystemExit(
            f"no store at {args.db}\n"
            "  set PROMPT_LAB_RUNS to the results directory, or pass --db.\n"
            "  results live outside the repo, conventionally "
            f"{os.path.join(os.path.expanduser('~'), 'prompt-lab-runs')}")

    a = analyse(args.db)
    if args.json:
        print(json.dumps(a, indent=2, default=lambda x: None if x != x else x))
    elif args.markdown:
        print(render_markdown(a))
    else:
        print(render(a))


if __name__ == "__main__":
    main()
