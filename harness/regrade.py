"""Recompute the transcript-derived L0 checks for trials already in the store.

Checks that read the workspace (deliverables, generator, flow) are left as
recorded — the directory is gone once the next trial resets it. Only what can
be recomputed from the transcript is touched, so a grader fix does not require
re-spending money on the models.
"""

from __future__ import annotations

import argparse
import json
import os

from harness.graders.mechanical import (
    WRITE_TOOLS,
    write_paths,
    _resolve,
    l0_components,
    l0_score,
    mask_inapplicable,
    protocol_checks,
    resolve_project,
)
import yaml
from harness.reasoning import strip_reasoning
from harness.workspace import recorded_root, workflows_dir
from harness.store import DEFAULT_PATH, RUNS_DIR, Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--db", default=DEFAULT_PATH)
    args = ap.parse_args()

    store = Store(args.db)
    changed = 0
    for row in store.trials(args.run_id):
        rec = dict(row)
        path = os.path.join(RUNS_DIR, args.run_id, rec["trial_id"], "transcript.jsonl")
        if not os.path.isfile(path):
            continue
        turns, prose = [], ""
        for line in open(path):
            entry = json.loads(line)
            if entry["kind"] == "assistant":
                # Repaired on read, so trials captured before the stripper
                # landed are regraded against clean prose without re-running.
                payload = entry["payload"]
                payload["text"] = strip_reasoning(payload.get("text", ""))
                turns.append(payload)
                prose += payload["text"]
        calls = [c for t in turns for c in t.get("tool_calls", [])]
        results = [r for t in turns for r in t.get("tool_results", [])]
        # The engine that produced this transcript decides which calls are
        # writes and what a relative path was joined onto. Both come off the
        # stored row rather than being assumed.
        _stored = json.loads(rec["checks"] or "{}")
        _backend = _stored.get("backend") or "builtin"
        _root = ((_stored.get("session_dir") or _stored.get("engine_directory")
                  if _backend == "opencode" else recorded_root(_stored)) or None)
        writes = write_paths(calls, _backend)
        # The project name comes from the task spec, not the trial id — and the
        # directory the model used is resolved the same way the grader resolves
        # it. This file used to carry its own copy of these path checks, which
        # is how it and the grader came to disagree about `-workflow`.
        spec = yaml.safe_load(open(os.path.join(ROOT, "tasks", f"{rec['task']}.yaml")))
        # The root this trial ran under, not the one in force today: Studio
        # moved the workspace to $HOME (#402), and a Stage 1 row regraded
        # against the new root reports that every model wrote nothing.
        _ws = recorded_root(_stored)
        scanned = os.path.realpath(workflows_dir(_ws))
        project_name = resolve_project(spec["project"], turns, workspace=scanned,
                                       backend=_backend, write_root=_root)
        project_dir = os.path.realpath(os.path.join(scanned, project_name))
        # The engine that wrote these decides what a relative path meant.
        resolved = [(w, _resolve(w, _root)) for w in writes]
        invisible = [w for w, r in resolved if not r.startswith(scanned + os.sep)]
        named = [w for w, r in resolved
                 if r == project_dir or r.startswith(project_dir + os.sep)]
        drift = sorted({
            r[len(scanned) + 1:].split(os.sep)[0]
            for w, r in resolved if r.startswith(scanned + os.sep)
        } - {project_name})

        checks = json.loads(rec["checks"] or "{}")
        flow = Store._json_field(rec.get("flow")) or {}
        if not isinstance(flow, dict):
            flow = {}
        order = ("none", "generate", "build", "plan")
        want = spec.get("depth", "plan")
        want = want if want in order else "plan"
        wanted = order[: order.index(want) + 1]

        def stage(name):
            if name not in wanted:
                return None
            entry = flow.get(name)
            if not isinstance(entry, dict):
                return False
            return entry.get("status") in ("generated", "built", "planned")

        checks["generate_ok"] = stage("generate")
        checks["build_ok"] = stage("build")
        checks["plan_ok"] = stage("plan")
        if not checks.get("wrote_files") or checks.get("generator_compiles") is None:
            expected = any("workflow_generator" in f
                           for f in (spec.get("expect") or {}).get("files", []))
            if expected and checks.get("generator_compiles") is None:
                checks["generator_compiles"] = False
                checks["generator_runs"] = False
        checks.update({
            "write_calls": len(writes),
            "stray_writes": invisible,
            "stray_targets": sorted({r for w, r in resolved if w in invisible})[:6],
            "discoverable": bool(writes) and not invisible,
            "path_compliance": bool(writes) and not invisible,
            "project_name_matched": bool(named),
            "project_name_drift": drift,
            "project_name_suffixed": (project_name != spec["project"]) if writes else None,
            # protocol_clean, malformed and unknown all come from the grader's
            # own implementation via the update below — this file used to
            # recompute them and drifted.
            "tool_errors": sum(1 for r in results if str(r.get("result", "")).startswith("Error executing")),
            "silent_turn": bool(calls) and not prose.strip(),
            "turn_collapsed": bool(calls) and not prose.strip() and (
                any(str(r.get("result", "")).startswith("Error executing") for r in results)
                or not checks.get("wrote_files")
            ),
            "narrated_not_written": not writes and len(prose) > 800,
            "wrote_elsewhere": bool(invisible) and not checks.get("wrote_files"),
            "wrote_partly_elsewhere": bool(invisible) and bool(checks.get("wrote_files")),
        })
        # The engine that produced this transcript decides which tool names
        # are real. It is recorded on the row; regrading an OpenCode trial
        # against the builtin vocabulary would call five known tools unknown.
        backend = checks.get("backend") or "builtin"
        checks.update(protocol_checks(calls, len(calls) > 0, backend))
        # The one place the two graders are allowed to agree from: a rule kept
        # in two copies is how this file and the grader drifted apart twice.
        mask_inapplicable(spec, checks)
        rec["checks"] = checks
        rec["tokens"] = {"input": rec.pop("input_tokens", None), "output": rec.pop("output_tokens", None)}
        checks.update(l0_components(checks))
        # A trial that never got an assistant turn — the container was busy, the
        # request never went out — has nothing to score. Scoring it 0.0 puts a
        # cell that did not happen on the board next to models that tried and
        # failed, and drags every mean down with it.
        rec["l0_score"] = l0_score(checks) if turns else None
        store.save(rec)
        changed += 1
    store.close()
    print(f"regraded {changed} trials in run {args.run_id}")


if __name__ == "__main__":
    main()
