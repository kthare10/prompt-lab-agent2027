#!/usr/bin/env python3
"""Does reaching the shipped examples help? Observationally, no — and that is why
the ablation has to be run.

    python3 tools/examples_effect.py

PLAN.md §8 item 0 calls unlocking the 13 shipped generators "the cheapest
experiment in the whole plan and plausibly the largest single gain". Two things
in this script bear on that, and neither supports the sentence as written.

**The material is not dead weight, and C9 is not what it says.** Across Stage 1
the models made 1,270 tool calls at example and asset paths. `read_file` was
denied 288 times — `Error: Access denied: path must be inside /home/ubuntu/work`
— while `run_command` succeeded 941 times on the same files, because it is not
guarded by `_safe_path` at all (the same hole that emptied the container's $HOME
on 2026-09-05). The examples are reachable. They are reachable only by the route
nothing documents, so whether a model gets them is a function of whether it
thinks to shell out.

**Which makes the observational comparison useless, in an instructive way.**
Within a task, cells that reached the examples score +0.175 higher — but that
compares different models, and the models that shell out are the stronger ones.
Within a model, they score -0.096 *lower* — but that compares different tasks,
and a model goes looking for an example when it is stuck. Adjusting for both
additively leaves -0.022 (p = 0.28). A textbook reversal, and it means this
question cannot be answered from Stage 1 at any level of cleverness: the models
chose their own treatment. Only randomising who gets the examples answers it.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import statistics
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from harness.cohort import kind, open_ro, stage1, task_specs  # noqa: E402
from harness.store import RUNS_DIR  # noqa: E402

DB = f"{RUNS_DIR}/results.db"
PERMUTATIONS = 20000
SEED = 20260911

ASSET = re.compile(r"assets/|/examples/|workflow_generator_\w+\.py|wrapper_template"
                   r"|marketplace|pegasus-ai/", re.I)
# Reaching an example means getting the code, not listing the directory it is in.
CODE = re.compile(r"from Pegasus\.api|ReplicaCatalog\(|TransformationCatalog\(|Workflow\(")
DENIED = re.compile(r"Access denied|must be inside", re.I)


def turns(cell: dict):
    path = os.path.join(RUNS_DIR, cell["run_id"], cell["trial_id"], "transcript.jsonl")
    if not os.path.isfile(path):
        return
    for line in open(path):
        entry = json.loads(line)
        if entry.get("kind") != "assistant":
            continue
        payload = entry.get("payload") or {}
        if isinstance(payload, dict):
            yield payload.get("tool_calls") or [], payload.get("tool_results") or []


def attempts(cell: dict):
    """(tool, outcome, output) for every call aimed at the shipped material."""
    for calls, results in turns(cell):
        for i, call in enumerate(calls):
            args = call.get("arguments") or {}
            if not ASSET.search(f"{args.get('path') or ''} {args.get('command') or ''}"):
                continue
            out = str(results[i].get("result", "")) if i < len(results) else ""
            tool = (call.get("name") or "").split("<|")[0]
            outcome = ("denied" if DENIED.search(out) else
                       "error" if out[:40].find("Error") >= 0 else
                       "ok" if out else "no result")
            yield tool, outcome, out


def reached(cell: dict) -> bool:
    return any(outcome == "ok" and len(out) > 400 and CODE.search(out)
               for _, outcome, out in attempts(cell))


def two_way(rows: list[tuple[str, str, bool, float]]) -> tuple[float, int, int]:
    """Difference in judged score, adjusted for model and task additively.

    Neither marginal is usable on its own: one compares models, the other
    compares tasks, and the treatment is correlated with both.
    """
    grand = statistics.fmean([r[3] for r in rows])
    by_model = {m: statistics.fmean([r[3] for r in rows if r[1] == m])
                for m in {r[1] for r in rows}}
    by_task = {t: statistics.fmean([r[3] for r in rows if r[0] == t])
               for t in {r[0] for r in rows}}
    resid = [(r[2], r[3] - by_model[r[1]] - by_task[r[0]] + grand) for r in rows]
    hit = [v for f, v in resid if f]
    miss = [v for f, v in resid if not f]
    if not hit or not miss:
        return float("nan"), len(hit), len(miss)
    return statistics.fmean(hit) - statistics.fmean(miss), len(hit), len(miss)


def paired(rows, index: int) -> dict[str, tuple[float, int, int]]:
    groups = collections.defaultdict(lambda: ([], []))
    for row in rows:
        groups[row[index]][0 if row[2] else 1].append(row[3])
    return {k: (statistics.fmean(a) - statistics.fmean(b), len(a), len(b))
            for k, (a, b) in groups.items() if a and b}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    if not os.path.isfile(args.db):
        raise SystemExit(f"no store at {args.db}\n  set PROMPT_LAB_RUNS, or pass --db.")

    cells, _ = stage1(open_ro(args.db))
    specs = task_specs()

    tally = collections.Counter()
    for cell in cells.values():
        for tool, outcome, _ in attempts(cell):
            tally[(tool, outcome)] += 1

    print("REACHING THE SHIPPED EXAMPLES")
    print("=" * 72)
    print(f"{sum(tally.values())} tool calls aimed at example and asset paths:\n")
    for (tool, outcome), n in sorted(tally.items(), key=lambda x: -x[1]):
        print(f"  {tool:20s} {outcome:10s} {n:5d}")
    denied = sum(n for (t, o), n in tally.items() if o == "denied")
    shelled = sum(n for (t, o), n in tally.items() if t == "run_command" and o == "ok")
    print(f"\n  The documented route is closed ({denied} denied) and the undocumented one")
    print(f"  is open ({shelled} successful run_command reads). The material is not")
    print("  unreachable; it is reachable only by shelling out.")

    rows = [(t, m, reached(c), float(c["l1_score"])) for (t, m), c in cells.items()
            if c["l1_score"] is not None and kind(specs[t]) == "build"]
    print(f"\n{len(rows)} judged build cells, {sum(1 for r in rows if r[2])} of which got"
          " real example code.\n")

    print("Difference in judged score for the cells that reached them:")
    print("-" * 72)
    for label, index in (("within task (compares models)", 0),
                         ("within model (compares tasks)", 1)):
        diffs = paired(rows, index)
        mean = statistics.fmean([d for d, _, _ in diffs.values()])
        print(f"  {label:32s} {mean:+.3f}   over {len(diffs)} groups")
    d, n_hit, n_miss = two_way(rows)

    rng = random.Random(SEED)
    flags = [r[2] for r in rows]
    hits = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(flags)
        shuffled = [(rows[i][0], rows[i][1], flags[i], rows[i][3]) for i in range(len(rows))]
        hits += abs(two_way(shuffled)[0]) >= abs(d)
    p = (hits + 1) / (PERMUTATIONS + 1)
    print(f"  {'adjusted for both':32s} {d:+.3f}   p = {p:.3f} "
          f"(n {n_hit} reached, {n_miss} not)")
    print()
    print("  The two marginals point opposite ways and the adjustment lands on zero.")
    print("  Whether a model reached the examples was the model's own choice, made")
    print("  when it was stuck, so no amount of adjustment recovers a causal answer")
    print("  from Stage 1. Randomise access and measure it — that is the ablation.")


if __name__ == "__main__":
    main()
