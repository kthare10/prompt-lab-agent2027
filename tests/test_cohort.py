"""Does the pooling rule pool what it says it pools?

The Stage 1 headline was computed three times by three pieces of code — an
ad-hoc script, the board, and prose — and produced three different numbers from
one store, because each one decided differently which trials counted. The rule
now lives in `harness.cohort`, so it can be wrong in only one place, and these
are the four ways it was silently wrong before:

  * t06 exists twice, in `s1-*` and in the `s1c-*` re-run. The newest must win,
    or the ceiling that forced the re-run is still in the number.
  * `s1c-probe` is one hand-run cell that confirmed the ceiling was lifted, and
    `smoke-restore` is the one that checked the harness after the store was
    rebuilt. Both are real trials with real scores and neither is a result —
    and both are newer than the sweep, so "newest wins" promotes them if the
    rule does not name them.
  * `t01-nrp` and friends predate Stage 1. Pooling by "everything in the store"
    silently mixes them in.
  * Two models were never served. Ranking them prints a serving fact as a
    capability result.

    python3 -m tests.test_cohort
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.cohort import (columns_for, family, kind, open_ro,      # noqa: E402
                            poolable, stage1, task_specs)
from harness.store import SCHEMA                                       # noqa: E402

ROWS = [
    # run_id,      trial_id,        task,                model,        started_at, l0
    ("s1-nrp",    "t06--m--r0",    "t06-wrapper-only",  "m",          "2026-09-05", 0.10),
    ("s1c-nrp",   "t06--m--r0",    "t06-wrapper-only",  "m",          "2026-09-08", 0.90),
    ("s1c-probe", "t06--m--r0",    "t06-wrapper-only",  "m",          "2026-09-09", 0.50),
    ("s1-nrp",    "t00--m--r0",    "t00-hello-pegasus", "m",          "2026-09-05", 0.70),
    ("s1b-nrp",   "t07--m--r0",    "t07-review-broken", "m",          "2026-09-06", 0.60),
    ("t01-nrp",   "t01--m--r0",    "t01-sensor-summary", "m",         "2026-09-01", 0.20),
    ("s1-nrp",    "t00--q--r0",    "t00-hello-pegasus", "qwen3-4bit", "2026-09-05", 0.00),
    ("smoke-restore", "t00--m--r0",  "t00-hello-pegasus", "m",          "2026-09-09", 0.01),
]


def main() -> None:
    fails = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        con = sqlite3.connect(path)
        con.executescript(SCHEMA)
        for run, tid, task, model, started, l0 in ROWS:
            con.execute(
                "INSERT INTO trials (run_id,trial_id,task,model,provider,status,l0_score,"
                "checks,started_at) VALUES (?,?,?,?,'p','done',?,'{}',?)",
                (run, tid, task, model, l0, started))
        con.commit(); con.close()

        cells, dropped = stage1(open_ro(path))
        got = {t: c for (t, _), c in cells.items()}

        check("the re-run wins over the run it replaced",
              got.get("t06-wrapper-only", {}).get("l0_score") == 0.90,
              str(got.get("t06-wrapper-only", {}).get("l0_score")))
        check("the probe cell is not a result",
              got.get("t06-wrapper-only", {}).get("run_id") == "s1c-nrp",
              str(got.get("t06-wrapper-only", {}).get("run_id")))
        check("t07 comes in from its own run", "t07-review-broken" in got)
        check("a smoke test is not a result",
              got.get("t00-hello-pegasus", {}).get("l0_score") == 0.70,
              str(got.get("t00-hello-pegasus", {}).get("l0_score")))
        check("runs outside the cohort stay out", "t01-sensor-summary" not in got)
        check("an unserved model is dropped",
              all(m != "qwen3-4bit" for _, m in cells))
        check("and is named with its cause",
              any(m == "qwen3-4bit" and reason and n == 1 for m, reason, n in dropped),
              str(dropped))

    # The build/judgement split is derived from each spec's declared depth, so a
    # new task cannot be misfiled into the family whose finding it would change.
    specs = task_specs()
    judgement = {t for t, s in specs.items() if kind(s) == "judgement"}
    check("judgement tasks are exactly the ones with no artifact",
          judgement == {t for t, s in specs.items() if (s.get("depth") or "none") == "none"},
          str(sorted(judgement)))
    check("every task declares a family",
          all(family(s) != "unclassified" for s in specs.values()),
          str(sorted(t for t, s in specs.items() if family(s) == "unclassified")))

    # Whether a group of tasks may be averaged into one column.
    models = [f"m{i}" for i in range(8)]
    specs = {f"t{i}": {"depth": "plan", "family": "build"} for i in range(2)}
    specs["j0"] = {"depth": "none", "family": "review"}
    specs["j1"] = {"depth": "none", "family": "debug"}
    agree = {("t0", m): i / 8 for i, m in enumerate(models)}
    agree |= {("t1", m): i / 8 for i, m in enumerate(models)}
    # The judgement pair is the shape actually observed: one ranks models the
    # reverse of the other, so their mean is flat and ranks nobody.
    disagree = {("j0", m): i / 8 for i, m in enumerate(models)}
    disagree |= {("j1", m): 1 - i / 8 for i, m in enumerate(models)}
    l1 = agree | disagree

    ok, rho, _ = poolable(l1, ["t0", "t1"], models)
    check("tasks that agree may be pooled", ok and rho > 0.9, f"rho {rho:.3f}")
    ok, rho, _ = poolable(l1, ["j0", "j1"], models)
    check("tasks that disagree may not", not ok and rho < -0.9, f"rho {rho:.3f}")
    check("one task needs no agreement", poolable(l1, ["t0"], models)[0])

    cols = columns_for(l1, specs, ["t0", "t1", "j0", "j1"], models)
    keys = [c["key"] for c in cols]
    check("the coherent group is one column, the other is not summed",
          keys == ["build", "j0", "j1"], str(keys))
    check("and the pooled column is the mean of its tasks",
          cols[0]["scores"]["m7"] == 7 / 8, str(cols[0]["scores"]["m7"]))

    # A trial that never reached a model is not a result. Sixteen of Stage 1's
    # twenty unfinished cells are these, ten of them glm-5's — left in, they
    # were most of that model's average, and taking them out moves its build
    # score from 0.364 to 0.680 over three remaining tasks.
    from harness.cohort import never_reached_the_model as nr
    check("an empty request never reached a model",
          nr({"status": "model_error", "input_tokens": 538426,
              "error": "inference gateway: BadRequest - chat-completions request "
                       "must have at least one message"}))
    check("nor did a trial that read nothing",
          nr({"status": "turn_timeout", "input_tokens": 0, "error": "ceiling"}))
    check("a trial that read tokens and then timed out did",
          not nr({"status": "turn_timeout", "input_tokens": 208790, "error": "ceiling"}))
    check("and a finished trial always did",
          not nr({"status": "done", "input_tokens": 0, "error": None}))

    print(f"\n{18 - fails}/18 passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
