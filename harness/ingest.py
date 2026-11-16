"""Rebuild store rows from the per-trial files on disk.

Every trial writes its full record to `runs/<run_id>/<trial_id>/trial.json`
before the store is touched, which is what made the composite-key bug
recoverable rather than fatal. Keep it that way: the filesystem is the record
of truth, the database is an index over it.

Judgements are restored the same way, from the `judgement--<judge>.json` files
`judge.py` writes beside each trial. They used to live only in `results.db`,
which is gitignored, so the 2026-09-05 wipe took all 46 of them while every
trial record survived in git.
"""

from __future__ import annotations

import argparse
import json
import os

import yaml

from harness.graders.mechanical import l0_score, mask_inapplicable
from harness.store import DEFAULT_PATH, RUNS_DIR, Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--db", default=DEFAULT_PATH)
    args = ap.parse_args()

    base = os.path.join(RUNS_DIR, args.run_id)
    if not os.path.isdir(base):
        raise SystemExit(f"no such run directory: {base}")

    store = Store(args.db)
    n = j = 0
    for trial_id in sorted(os.listdir(base)):
        trial_dir = os.path.join(base, trial_id)
        path = os.path.join(trial_dir, "trial.json")
        if not os.path.isfile(path):
            continue
        rec = json.load(open(path))
        rec.setdefault("run_id", args.run_id)
        # The file on disk is the observation; the store holds the graded result.
        # Which checks a task exercises is a property of the task, so the rule is
        # applied here too rather than trusting a score written by whatever
        # version of the grader happened to run at the time. This is the third
        # place that rule is needed, and the third to call the same function —
        # the grader and the regrader have drifted apart twice by not doing that.
        spec_path = os.path.join(ROOT, "tasks", f"{rec.get('task')}.yaml")
        if rec.get("checks") and os.path.isfile(spec_path):
            mask_inapplicable(yaml.safe_load(open(spec_path)) or {}, rec["checks"])
            rec["l0_score"] = l0_score(rec["checks"])
        elif rec.get("checks") and rec.get("l0_score") is None:
            rec["l0_score"] = l0_score(rec["checks"])
        store.save(rec)
        n += 1
        for name in sorted(os.listdir(trial_dir)):
            if not (name.startswith("judgement--") and name.endswith(".json")):
                continue
            verdict = json.load(open(os.path.join(trial_dir, name)))
            verdict.setdefault("run_id", args.run_id)
            verdict.setdefault("trial_id", trial_id)
            store.save_judgement(verdict)
            j += 1
    store.close()
    print(f"ingested {n} trials and {j} judgements into run {args.run_id}")


if __name__ == "__main__":
    main()
