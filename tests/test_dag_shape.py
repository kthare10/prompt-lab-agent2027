"""Does the DAG-shape check actually discriminate?

`expect.jobs` and `expect.dag` sat in the task specs unread, so a flat fan-in
over 2,000 tiles scored the same as a proper merge tree — which is the whole
content of t02. Synthetic workflows are used rather than recorded ones because
the case that matters (2,000 tiles into one job) is the case no model has
produced yet.

    python3 -m tests.test_dag_shape
"""

from __future__ import annotations

import os
import tempfile

import yaml

from harness.graders.mechanical import dag_checks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def workflow(n_tiles: int, chunk: int | None = None) -> tuple[list, list]:
    """`chunk=None` builds one merge over every tile; `chunk=k` a staged tree."""
    jobs = [{"id": f"t{i}"} for i in range(n_tiles)]
    deps: list[dict] = []
    if chunk is None:
        jobs.append({"id": "merge"})
        deps += [{"id": f"t{i}", "children": ["merge"]} for i in range(n_tiles)]
        return jobs, deps
    level, r = [f"t{i}" for i in range(n_tiles)], 0
    while len(level) > 1:
        nxt = []
        for g in range(0, len(level), chunk):
            m = f"m{r}_{g}"
            jobs.append({"id": m})
            nxt.append(m)
            deps += [{"id": p, "children": [m]} for p in level[g:g + chunk]]
        level, r = nxt, r + 1
    return jobs, deps


def graded(task_id: str, jobs: list, deps: list) -> dict:
    task = yaml.safe_load(open(os.path.join(ROOT, "tasks", f"{task_id}.yaml")))
    d = tempfile.mkdtemp()
    yaml.safe_dump({"jobs": jobs, "jobDependencies": deps},
                   open(os.path.join(d, "workflow.yml"), "w"))
    return dag_checks(task, d)


def main() -> None:
    cases = [
        # (label, task, workflow, expected jobs_ok, expected dag_ok)
        ("flat fan-in, 2000 tiles", "t02-tile-reduce", workflow(2000), True, False),
        ("staged tree, chunk 25", "t02-tile-reduce", workflow(2000, 25), True, True),
        ("flat fan-in, 3 tiles", "t02-tile-reduce", workflow(3), False, False),
        ("two-stage tree, chunk 3", "t02-tile-reduce", workflow(12, 3), True, True),
        ("fan-in is a fan-in", "t01-sensor-summary", workflow(4), True, True),
        ("a tree is not a fan-in", "t01-sensor-summary", workflow(12, 3), True, False),
        ("one job is single", "t00-hello-pegasus", ([{"id": "a"}], []), True, True),
    ]
    failures = 0
    for label, task_id, (jobs, deps), want_jobs, want_dag in cases:
        c = graded(task_id, jobs, deps)
        ok = c["jobs_ok"] is want_jobs and c["dag_ok"] is want_dag
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label:26s} {task_id:20s} "
              f"jobs={c['dag_jobs']:5d} depth={c['dag_depth']:2d} "
              f"max_in={c['dag_max_in_degree']:5d} merges={c['dag_merge_jobs']:4d} "
              f"-> jobs_ok={c['jobs_ok']} dag_ok={c['dag_ok']}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
