"""Do the grader and the regrader agree about the tool protocol?

They have disagreed twice. The first time `regrade.py` carried its own copy of
the path checks; they drifted, and the disagreement floored five t01 cells. The
second time it carried its own `protocol_clean`, which never learned about
unknown tool names, so regrading handed `protocol_clean: True` to trials that
had called tools the server does not have — raising their L0 for work that
never happened.

Both now call `protocol_checks`. This pins the behaviour that copy kept getting
wrong, and fails if a second implementation reappears.

    python3 -m tests.test_protocol_agreement
"""

from __future__ import annotations

import inspect

from harness import regrade
from harness.graders import mechanical
from harness.graders.mechanical import protocol_checks

CALL = lambda name, **args: {"name": name, "arguments": args}

CASES: list[tuple[str, list[dict], bool, int, list[str]]] = [
    # (label, calls, protocol_clean, unknown_tool_calls, unknown_tools)
    ("no calls at all", [], False, 0, []),
    ("all known and complete",
     [CALL("write_file", path="a.py", content="x"), CALL("list_directory", path=".")],
     True, 0, []),
    # The case the drifted copy scored perfect: a harmony marker leaking into
    # the tool name. Studio cannot dispatch it, so the work never happens.
    ("harmony marker in the name",
     [CALL("write_file", path="a.py", content="x"),
      CALL("write_file<|channel|>commentary", path="b.py", content="y")],
     False, 1, ["write_file<|channel|>commentary"]),
    ("unknown tool entirely",
     [CALL("list_workflow_projects")], False, 1, ["list_workflow_projects"]),
    ("known tool, missing argument",
     [CALL("write_file", path="a.py")], False, 0, []),
    ("unnamed call",
     [{"arguments": {}}], False, 1, ["(unnamed)"]),
]


def main() -> None:
    fails = 0
    for label, calls, clean, n_unknown, unknown in CASES:
        got = protocol_checks(calls, len(calls) > 0)
        ok = (got["protocol_clean"] == clean
              and got["unknown_tool_calls"] == n_unknown
              and got["unknown_tools"] == unknown)
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"        got  clean={got['protocol_clean']} "
                  f"unknown={got['unknown_tools']} n={got['unknown_tool_calls']}")
            print(f"        want clean={clean} unknown={unknown} n={n_unknown}")

    # An unknown name must cost the protocol point even when everything else is
    # immaculate — that is the exact inversion the drifted copy allowed.
    good = protocol_checks([CALL("write_file", path="a", content="b")], True)
    bad = protocol_checks([CALL("write_file", path="a", content="b"),
                           CALL("no_such_tool")], True)
    ok = good["protocol_clean"] and not bad["protocol_clean"]
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  an unknown name costs the protocol point")

    # No second implementation: neither module may recompute these inline.
    src = inspect.getsource(regrade)
    ok = "protocol_checks(" in src and "not malformed" not in src
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  regrade has no private copy")

    # Assignments only: `c.get("protocol_clean")` reads it, and the comments
    # quote it. Exactly one line may set it.
    assigns = [line.strip() for line in inspect.getsource(mechanical).splitlines()
               if '"protocol_clean":' in line]
    ok = len(assigns) == 1
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  protocol_clean assigned in exactly one place"
          + ("" if ok else f" — {len(assigns)}: {assigns}"))

    total = len(CASES) + 3
    print(f"\n{total - fails}/{total} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
