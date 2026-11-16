"""Does the grader recognise a generator the way Studio does?

t13 asks for a design whose correct answer splits generation across phases —
its `must` rewards "a sub-workflow planned after demultiplexing, or an
equivalent deferred-planning approach" — while its `expect.files` lists
`workflow_generator.py`. Read literally, the task marks its own intended answer
as a missing deliverable. `kimi` wrote `workflow_generator_phase1.py` and
`_phase2.py`, scored `deliverables_ok=false`, and the run then died on a Studio
400 because nothing matched the one filename `/generate` knows.

    python3 -m tests.test_generator_resolve
"""

from __future__ import annotations

import os
import tempfile

from harness.graders.mechanical import generator_files, l0_score
from harness.store import RUNS_DIR

# The trial that motivated this. Resolved through RUNS_DIR because the results
# directory sits at a different path on the host and in the container; the
# checks against it are skipped when it is not reachable, since `sync_runs.sh`
# backs up records but not workspaces.
REAL_PHASED = os.path.join(RUNS_DIR, "s1-nrp", "t13-barcode-split--kimi--r0", "workspace")


def build(d: str, *names: str, declared: str | None = None) -> None:
    for n in names:
        open(os.path.join(d, n), "w").write("import sys\n")
    if declared:
        open(os.path.join(d, ".pegasushub.yml"), "w").write(
            f"scripts:\n  generator: {declared}\n")


def main() -> None:
    fails = 0

    def check(label: str, got, want) -> None:
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))

    with tempfile.TemporaryDirectory() as d:
        check("nothing there", generator_files(d), ([], None))

    with tempfile.TemporaryDirectory() as d:
        build(d, "workflow_generator.py")
        check("canonical", generator_files(d)[1], "canonical")

    with tempfile.TemporaryDirectory() as d:
        # quickstart-workflow ships the hyphenated name; Studio accepts it.
        build(d, "workflow-generator.py")
        check("hyphenated", generator_files(d)[1], "hyphenated")

    with tempfile.TemporaryDirectory() as d:
        build(d, "make_dag.py", declared="make_dag.py")
        check("declared in .pegasushub.yml", generator_files(d)[1], "declared")

    with tempfile.TemporaryDirectory() as d:
        # Traversal outside the project is refused, matching Studio.
        build(d, "make_dag.py", declared="../../etc/passwd")
        check("declared traversal refused", generator_files(d)[1], None)

    with tempfile.TemporaryDirectory() as d:
        build(d, "workflow_generator_phase1.py", "workflow_generator_phase2.py")
        check("phased", generator_files(d)[1], "phased")

    with tempfile.TemporaryDirectory() as d:
        # A single suffixed file is not a phased design — it is one oddly named
        # script that Studio would not run, and must not count.
        build(d, "workflow_generator_old.py")
        check("single suffixed file is not phased", generator_files(d)[1], None)

    with tempfile.TemporaryDirectory() as d:
        build(d, "workflow_generator.py", "workflow_generator_phase1.py",
              "workflow_generator_phase2.py")
        check("canonical beats phased", generator_files(d)[1], "canonical")

    if os.path.isdir(REAL_PHASED):
        paths, style = generator_files(REAL_PHASED)
        check("real kimi t13 trial", style, "phased")
        check("both phases found", len(paths), 2)

    # A phased generator is unmeasurable standalone, not failing: phase 2 reads a
    # manifest that only exists once phase 1's workflow has run. `l0_score` drops
    # None terms, so the model is neither credited nor punished for it.
    base = {"protocol_clean": True, "wrote_files": True, "path_compliance": True,
            "deliverables_ok": True, "generator_compiles": True,
            "vocabulary_score": 0.7}
    unmeasured = l0_score({**base, "generator_runs": None})
    failed = l0_score({**base, "generator_runs": False})
    ok = unmeasured > failed
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  unmeasured ({unmeasured}) scores above "
          f"failed ({failed})")

    # The two ways `/generate` cannot run, kept distinct. A phased design is
    # unmeasured; no generator at all is a plain failure. Both used to surface
    # as the same Studio 400 and end the trial as `harness_error`, which reads
    # as "the harness broke" for what is, in the second case, the model not
    # having written the thing it was asked for.
    from harness.trial import stage_checks
    want = ("generate", "build", "plan")
    check("phased stages unmeasured",
          stage_checks({"generate": {"status": "phased"}}, want),
          {"generate_ok": None, "build_ok": None, "plan_ok": None})
    check("no_generator stages failed",
          stage_checks({"generate": {"status": "no_generator"}}, want),
          {"generate_ok": False, "build_ok": False, "plan_ok": False})

    total = 13 if os.path.isdir(REAL_PHASED) else 11
    print(f"\n{total - fails}/{total} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
