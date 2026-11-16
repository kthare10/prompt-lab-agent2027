"""Can the grader find the project when the model names it something else?

Ceiling #5: a project has two names, `x` and `x-workflow`. t06 showed it can
have any name. Its project id is `wrapper-only` — which reads as a description
of the task, not a name — and not one of sixteen models used it. They named it
for the science: `qmap-workflow`, `qmap-analysis-workflow`, `qmap-analysis`.
`resolve_project` fell through to `wrapper-only`, which does not exist, so no
workspace snapshot was taken, all eighteen trials reached the judge with an
empty ARTIFACTS/, and every model was scored as having written nothing. L1 came
back with two distinct values across sixteen models.

    python3 -m tests.test_project_drift
"""

from __future__ import annotations

import os

from harness.graders.mechanical import SERVER_WORKSPACE, resolve_project

# Derived, not hardcoded: `_resolve` interprets a model's relative path against
# the server's workspace, so a root that disagrees with it makes every write
# look like it landed outside and the test passes only by falling back.
WS = os.path.join(SERVER_WORKSPACE, "workflows")


def turn(*paths: str) -> list[dict]:
    return [{"tool_calls": [{"name": "write_file",
                             "arguments": {"path": p, "content": "x"}} for p in paths]}]


def main() -> None:
    fails = 0

    def check(label: str, got, want) -> None:
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))

    # Unchanged: the expected names still win outright.
    check("exact name wins",
          resolve_project("sensor-summary", turn("workflows/sensor-summary/x.py"), WS),
          "sensor-summary")
    check("suffixed name wins",
          resolve_project("sensor-summary", turn("workflows/sensor-summary-workflow/x.py"), WS),
          "sensor-summary-workflow")
    check("expected name beats an unexpected one",
          resolve_project("sensor-summary",
                          turn("workflows/scratch/a.py", "workflows/sensor-summary/x.py"), WS),
          "sensor-summary")

    # The t06 case.
    check("drifted name is found",
          resolve_project("wrapper-only", turn("workflows/qmap-workflow/bin/run_qmap.py"), WS),
          "qmap-workflow")
    check("busiest drifted directory wins",
          resolve_project("wrapper-only",
                          turn("workflows/qmap-analysis/a.py",
                               "workflows/qmap-workflow/b.py",
                               "workflows/qmap-workflow/c.py"), WS),
          "qmap-workflow")

    # Shipped directories are not the project, even when written into.
    check("assets/ is not the project",
          resolve_project("wrapper-only",
                          turn("workflows/assets/a.py", "workflows/assets/b.py",
                               "workflows/qmap-workflow/c.py"), WS),
          "qmap-workflow")
    check("only shared dirs falls back",
          resolve_project("wrapper-only", turn("workflows/references/a.md"), WS),
          "wrapper-only")
    check("hidden scratch dir is not the project",
          resolve_project("wrapper-only",
                          turn("workflows/.scratch/a", "workflows/qmap-workflow/b.py"), WS),
          "qmap-workflow")

    # Wrote nothing: unchanged, must not adopt another trial's leftovers.
    check("wrote nothing falls back to the asked-for name",
          resolve_project("wrapper-only", [], WS), "wrapper-only")
    check("writes outside the workspace are ignored",
          resolve_project("wrapper-only", turn("/home/ubuntu/work/x.py"), WS), "wrapper-only")

    print(f"\n{10 - fails}/10 passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
