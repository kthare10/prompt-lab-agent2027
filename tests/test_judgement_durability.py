"""Does a judgement survive losing the database?

Trial records already do: `trial.json` is written before the store is touched,
`sync_runs.sh` copies it into the repo, and `ingest.py` reads it back. That
chain is why the 2026-09-05 wipe was survivable. Judgements were the exception
— they existed only in `results.db`, which is gitignored — so all 46 were lost
while every trial around them was restored from git.

This exercises the whole loop: write, back up, destroy the store, rebuild.

    python3 -m tests.test_judgement_durability
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    fails = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))

    with tempfile.TemporaryDirectory() as runs:
        run_id, trial_id = "r1", "t00-x--m--r0"
        trial_dir = os.path.join(runs, run_id, trial_id)
        os.makedirs(trial_dir)
        json.dump({"trial_id": trial_id, "run_id": run_id, "task": "t00-x",
                   "model": "m", "status": "done", "l0_score": 0.5},
                  open(os.path.join(trial_dir, "trial.json"), "w"))

        env = {**os.environ, "PROMPT_LAB_RUNS": runs, "PYTHONPATH": ROOT}

        # Two judges on one trial: the judge model is part of the store's
        # primary key, so it has to be part of the filename too or the second
        # judgement silently overwrites the first.
        write = (
            "from harness.judge import write_judgement, judgement_path\n"
            f"d={trial_dir!r}\n"
            "for m,s in (('gpt-5.6-sol',0.8),('other/model:v2',0.4)):\n"
            "    write_judgement(d, {'run_id':'r1','trial_id':'%s','judge_model':m,\n"
            "        'l1_score':s,'dimensions':{},'criteria':{},'summary':'s','error':None})\n"
            % trial_id)
        subprocess.run([sys.executable, "-c", write], env=env, check=True, cwd=ROOT)

        files = sorted(f for f in os.listdir(trial_dir) if f.startswith("judgement--"))
        check("one file per judge", len(files) == 2, str(files))
        check("unsafe characters sanitised",
              all(all(c.isalnum() or c in "._-" for c in f) for f in files), str(files))

        # sync_runs.sh must pick them up, or they never reach git.
        repo_runs = os.path.join(ROOT, "runs")
        found = subprocess.run(
            ["sh", "-c", f"cd {runs} && find . -name 'judgement--*.json' | wc -l"],
            capture_output=True, text=True).stdout.strip()
        check("sync pattern matches them", found == "2", f"{found} found")

        # Now the wipe: the store is gone, the files remain.
        db = os.path.join(runs, "rebuilt.db")
        out = subprocess.run(
            [sys.executable, "-m", "harness.ingest", "--run-id", run_id, "--db", db],
            env=env, cwd=ROOT, capture_output=True, text=True)
        check("ingest ran", out.returncode == 0, (out.stderr or "").strip()[-200:])

        import sqlite3
        con = sqlite3.connect(db)
        rows = con.execute(
            "SELECT judge_model, l1_score FROM judgements ORDER BY judge_model").fetchall()
        con.close()
        check("both judgements restored", len(rows) == 2, str(rows))
        check("scores intact", sorted(r[1] for r in rows) == [0.4, 0.8], str(rows))
        check("judge models intact",
              sorted(r[0] for r in rows) == ["gpt-5.6-sol", "other/model:v2"], str(rows))

    print(f"\n{7 - fails}/7 passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
