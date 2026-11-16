"""Leaderboards and per-check breakdowns from the results store.

Two rules the plan insists on and this enforces:

* Never report a single composite alone — a model that writes perfect code in
  the wrong directory scores the same as one that fails everywhere, and only
  the per-check table shows which.
* Never report variance the provider manufactured. Repeats whose assistant text
  hashes identically are the endpoint's cache (C10), not independent samples,
  so the cell is marked `cached` rather than given a variance of zero.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict

from harness.store import DEFAULT_PATH, Store

CHECKS = [
    ("protocol_clean", "clean protocol"),
    ("wrote_files", "files on disk"),
    ("discoverable", "discoverable"),
    ("project_name_matched", "named as asked"),
    ("deliverables_ok", "complete deliverables"),
    ("generator_compiles", "generator parses"),
    ("generator_runs", "generator runs"),
    ("generate_ok", "generate stage"),
    ("build_ok", "image build"),
    ("plan_ok", "plan stage"),
]


def pct(values: list[bool | None]) -> str:
    seen = [bool(v) for v in values if v is not None]
    return f"{100 * sum(seen) / len(seen):3.0f}%" if seen else "  — "


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--db", default=DEFAULT_PATH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    store = Store(args.db)
    run_id = args.run_id or (store.runs()[-1] if store.runs() else None)
    if not run_id:
        raise SystemExit("no runs in the store")
    rows = [dict(r) for r in store.trials(run_id)]
    store.close()
    for r in rows:
        r["checks"] = json.loads(r["checks"] or "{}")

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"] or "?"].append(r)

    if args.json:
        print(json.dumps({"run_id": run_id, "trials": rows}, indent=2, default=str))
        return

    print(f"\nrun {run_id} — {len(rows)} trials, {len({r['task'] for r in rows})} tasks, "
          f"{len(by_model)} models\n")

    # ── leaderboard ───────────────────────────────────────────────────────
    head = f"{'model':<22}{'n':>4}{'L0':>7}{'ok':>6}  " + "".join(f"{lbl[:11]:>13}" for _, lbl in CHECKS)
    print(head)
    print("-" * len(head))
    ranked = sorted(
        by_model.items(),
        key=lambda kv: -statistics.fmean([t["l0_score"] or 0 for t in kv[1]]),
    )
    for model, trials in ranked:
        scores = [t["l0_score"] or 0 for t in trials]
        ok = sum(1 for t in trials if t["status"] == "done")
        line = (f"{model:<22}{len(trials):>4}{statistics.fmean(scores):>7.2f}"
                f"{ok}/{len(trials):>4}  ")
        line += "".join(f"{pct([t['checks'].get(k) for t in trials]):>13}" for k, _ in CHECKS)
        print(line)

    # ── per task ──────────────────────────────────────────────────────────
    print("\nby task (mean L0)\n")
    tasks = sorted({r["task"] for r in rows})
    print(f"{'model':<22}" + "".join(f"{t.split('-')[0]:>9}" for t in tasks))
    for model, trials in ranked:
        cells = []
        for task in tasks:
            got = [t["l0_score"] or 0 for t in trials if t["task"] == task]
            cells.append(f"{statistics.fmean(got):>9.2f}" if got else f"{'—':>9}")
        print(f"{model:<22}" + "".join(cells))

    # ── variance, honestly ────────────────────────────────────────────────
    print("\nrepeat independence (C10 — the provider caches identical prompts)\n")
    for model, trials in ranked:
        for task in tasks:
            cell = [t for t in trials if t["task"] == task]
            if len(cell) < 2:
                continue
            hashes = {t["output_hash"] for t in cell if t["output_hash"]}
            scores = [t["l0_score"] or 0 for t in cell]
            if len(hashes) == 1:
                verdict = "cached — variance unmeasurable"
            else:
                verdict = f"stdev {statistics.pstdev(scores):.3f} over {len(hashes)} distinct outputs"
            print(f"  {model:<22}{task:<26}{verdict}")

    # ── things that need a human ──────────────────────────────────────────
    flags = [
        (r, k) for r in rows for k in
        ("tool_call_leaked_into_chat", "turn_collapsed", "narrated_not_written",
         "wrote_elsewhere", "wrote_partly_elsewhere", "uses_bad_fakeroot",
         "rounds_exhausted", "history_trimmed")
        if r["checks"].get(k)
    ]
    bad = [r for r in rows if r["status"] not in ("done",)]
    if flags or bad:
        print("\nflags\n")
        for r, k in flags:
            where = ""
            if k.startswith("wrote_"):
                where = "  → " + ", ".join(r["checks"].get("stray_targets", [])[:2])
            print(f"  {k:<28}{r['trial_id']}{where}")
        for r in bad:
            print(f"  {r['status']:<28}{r['trial_id']}  {(r['error'] or '')[:70]}")
    print()


if __name__ == "__main__":
    main()
