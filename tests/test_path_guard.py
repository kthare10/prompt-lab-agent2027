"""The harness must not be able to delete outside the workflows directory.

`reset_project` deletes paths built by joining a task spec's `project` onto
WORKSPACE, and `seed_project` writes to paths built from a `seed_files` target.
Neither validated the result. `project: ""` would have meant `rmtree(WORKSPACE)`
— every project at once — and a `..` anywhere would have escaped into the home
directory the container mounts.

On 2026-09-05 that home directory was emptied mid-sweep by something running as
this uid, and the harness had to be cleared by hand because nothing in the code
made it impossible. This makes it impossible.

    python3 -m tests.test_path_guard
"""

from __future__ import annotations

import os

from harness.trial import WORKSPACE, _inside_workspace

CASES = [
    # (path fragment joined onto WORKSPACE, should it be allowed?)
    ("sensor-summary", True),
    ("sensor-summary-workflow", True),
    ("nextgen/ubuntu/pegasus/nextgen/run0003", True),
    ("", False),                      # WORKSPACE itself — rmtree of everything
    (".", False),                     # same, spelled differently
    ("..", False),                    # ~/work
    ("../..", False),                 # the mounted home directory
    ("../../../etc", False),
    ("ok/../..", False),              # escapes after looking innocent
    ("/etc", False),                  # absolute paths win os.path.join
    ("/home/ubuntu", False),
]


def main() -> None:
    fails = 0
    for frag, allowed in CASES:
        path = os.path.join(WORKSPACE, frag)
        try:
            _inside_workspace(path)
            got = True
        except SystemExit:
            got = False
        ok = got == allowed
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {'allow' if got else 'refuse':6s} "
              f"(want {'allow' if allowed else 'refuse'})  {frag!r}")

    print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
