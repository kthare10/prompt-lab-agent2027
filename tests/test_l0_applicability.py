"""L0 must not score a task on work it never asked for.

On t10 — an elicitation task, `depth: none` — the Spearman rho between a trial's
`write_calls` and its L0 was +0.663 (p = 0.007), while the same writes against
the judge's score gave +0.057. L0 there was very nearly a measurement of whether
the model created files, which is exactly what t10's own `must_not` forbids:
`kimi` scored 0.100 for asking correctly and 1.000 for scaffolding early on the
same task. t07 was worse — its fixture seeds a `workflow_generator.py`, so
sixteen models with zero write calls were each credited with compiling and
running a generator they had not written.

    python3 -m tests.test_l0_applicability
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.graders.mechanical import (asks_for_artifact,       # noqa: E402
                                        l0_score, mask_inapplicable)

# What the grader had recorded for a t07 cell: a clean protocol, no writes, and
# a generator that compiles and runs — the fixture's, not the model's.
SEEDED = {
    "tool_calls": 15, "protocol_clean": True, "wrote_files": False,
    "discoverable": False, "path_compliance": False, "deliverables_ok": None,
    "generator_compiles": True, "generator_runs": True, "vocabulary_score": 0.86,
}
# A build task's cell, for the guard that none of this touches those.
BUILT = {
    "tool_calls": 40, "protocol_clean": True, "wrote_files": True,
    "discoverable": True, "path_compliance": True, "deliverables_ok": True,
    "generator_compiles": True, "generator_runs": True, "vocabulary_score": 1.0,
}

REVIEW = {"depth": "none", "expect": {"must": ["…"]}}
BUILD = {"depth": "plan", "expect": {"files": ["workflow_generator.py"]}}
# A `depth: none` task that does ask for files would keep its artifact checks;
# nothing in the suite is shaped that way today, and the predicate is written so
# that one would not silently lose its grading.
HYBRID = {"depth": "none", "expect": {"files": ["notes.md"]}}


def main() -> None:
    fails = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))

    check("a review task asks for no artifact", not asks_for_artifact(REVIEW))
    check("a build task does", asks_for_artifact(BUILD))
    check("depth none plus expected files still does", asks_for_artifact(HYBRID))

    c = mask_inapplicable(REVIEW, dict(SEEDED))
    check("a seeded generator is not credited to the model",
          c["generator_compiles"] is None and c["generator_runs"] is None)
    check("writing nothing is not a failure where nothing was asked for",
          c["wrote_files"] is None and c["path_compliance"] is None)
    check("and the score says so rather than guessing",
          l0_score(c) is None, str(l0_score(c)))

    c = mask_inapplicable(BUILD, dict(BUILT))
    check("a build task keeps every check", all(
        c[k] is not None for k in ("wrote_files", "path_compliance", "generator_compiles")))
    check("and keeps its score", l0_score(c) == 1.0, str(l0_score(c)))

    # Zero tool calls: nothing was called, so there is no protocol to judge. On a
    # build task this changes no score — every artifact check still fails — and
    # on a conversation it is the difference between saying nothing in the right
    # format and never having been asked for a format at all.
    silent_build = dict(BUILT, tool_calls=0, protocol_clean=False, wrote_files=False,
                        discoverable=False, path_compliance=False, deliverables_ok=False,
                        generator_compiles=False, generator_runs=False)
    c = mask_inapplicable(BUILD, dict(silent_build))
    check("no tool calls means no protocol verdict", c["protocol_clean"] is None)
    check("and a silent build trial still scores at the floor",
          l0_score(c) == round(0.1 * 1.0, 3), str(l0_score(c)))

    print(f"\n{9 - fails}/9 passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
