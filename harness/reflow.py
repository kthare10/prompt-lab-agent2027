"""Re-run the server-side stages for trials already in the store.

`regrade.py` recomputes what the *transcript* implies; this recomputes what the
*workspace* implies, by restoring each trial's snapshot into the live workspace
and driving generate/build/plan against it again. No model is called, so a fixed
execution stage does not cost a re-run — and unlike a re-run, the model output is
byte-identical, so the only thing that changed is the stage.

    python3 -m harness.reflow --run-id t01b-nrp

Runs inside the Studio container (D3). Mutates the shared workspace, so it must
not run while a trial is in flight (C2/D5).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil

import yaml

from harness.graders.mechanical import dag_checks, grade, l0_components, l0_score, resolve_project
from harness.store import DEFAULT_PATH, RUNS_DIR, Store
from harness.studio import Studio, WORKSPACE
from harness.trial import (
    DEPTH_ORDER,
    reset_project,
    run_flow,
    seed_project,
    stage_checks,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--trial-id", action="append", default=None,
                    help="restrict to these trials (repeatable)")
    ap.add_argument("--studio-url", default="http://localhost:8888")
    ap.add_argument("--site", default="condorpool")
    ap.add_argument("--output-site", default="local")
    ap.add_argument("--db", default=DEFAULT_PATH)
    args = ap.parse_args()

    studio = Studio(args.studio_url)
    if studio.current_turn().get("active"):
        raise SystemExit("a chat turn is in flight — refusing to touch the workspace (C2/D5)")

    store = Store(args.db)
    done = 0
    for row in store.trials(args.run_id):
        rec = dict(row)
        if args.trial_id and rec["trial_id"] not in args.trial_id:
            continue
        trial_dir = os.path.join(RUNS_DIR, args.run_id, rec["trial_id"])
        snapshot = os.path.join(trial_dir, "workspace")
        if not os.path.isdir(snapshot) or not os.listdir(snapshot):
            print(f"skip {rec['trial_id']}: no workspace snapshot")
            continue

        task = yaml.safe_load(open(os.path.join(ROOT, "tasks", f"{rec['task']}.yaml")))
        turns = [
            json.loads(line)["payload"]
            for line in open(os.path.join(trial_dir, "transcript.jsonl"))
            if json.loads(line)["kind"] == "assistant"
        ]
        # The name recorded at trial time if we have it; otherwise resolve it the
        # same way the grader does.
        project_name = rec.get("project_dir") or resolve_project(task["project"], turns)
        project_dir = os.path.join(WORKSPACE, project_name)

        reset_project(project_name, None)
        # symlinks=True: the snapshot preserves the model's own symlinks, so
        # restoring it must not try to dereference them.
        shutil.copytree(snapshot, project_dir, dirs_exist_ok=True, symlinks=True)
        # The snapshot deliberately omits seeded fixtures, so put them back —
        # otherwise a re-grade sees a project the model never actually worked in.
        seed_project(task, task["project"])

        depth = rec.get("depth") or task.get("depth", "plan")
        depth = depth if depth in DEPTH_ORDER else "plan"
        wanted = DEPTH_ORDER[: DEPTH_ORDER.index(depth) + 1]

        flow = run_flow(studio, task, project_name, project_dir, wanted,
                        site=args.site, output_site=args.output_site)

        # The workspace-derived checks too: the stage writes workflow.yml and the
        # catalogs, and grading the restored tree is what makes them consistent
        # with the flow beside them.
        checks = grade(task, project_dir, turns,
                       (json.loads(rec.get("checks") or "{}") or {}).get("backend") or "builtin")
        checks.update(stage_checks(flow, wanted))
        checks.update(dag_checks(task, project_dir))

        before = rec.get("l0_score")
        rec["flow"] = flow
        rec["checks"] = checks
        rec["tokens"] = {"input": rec.pop("input_tokens", None),
                         "output": rec.pop("output_tokens", None)}
        checks.update(l0_components(checks))
        rec["l0_score"] = l0_score(checks)

        # A `harness_error` recorded here came from these very stages — the
        # conversation had already finished. Re-running them is the test of
        # whether that error still holds, so if the flow now completes, the
        # status is stale and must not keep the trial off the board: it renders
        # as "not measured", which is the opposite of what a 0.971 means.
        cleared = ""
        if rec.get("status") == "harness_error" and not flow.get("error"):
            rec["status"] = "done"
            rec["error"] = None
            cleared = "  (harness_error cleared)"

        store.save(rec)
        done += 1
        gen = (flow.get("generate") or {}).get("status")
        print(f"{rec['trial_id']}: {before} → {rec['l0_score']}  generate={gen} "
              f"args={(flow.get('generate') or {}).get('args')}{cleared}")

    store.close()
    print(f"reflowed {done} trials in run {args.run_id}")


if __name__ == "__main__":
    main()
