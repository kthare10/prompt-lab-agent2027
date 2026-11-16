#!/usr/bin/env python3
"""Archive what previous trials left in the workspace, so the next one starts clean.

`reset_project` clears `<workspace>/workflows/<project>` and its `-workflow`
twin, and nothing else. Everything a model stages beside that survives into every
later trial, and after 391 trials this workspace held 2.9 GB under `workflows/`
and ~35 stray entries at its root — whole projects written to the wrong place,
replica catalogs, `probe.sub`, a directory literally named `$HOME`, one named `"`,
and four case variants of `legacy-tool-workflow` that `reset_project`'s two
spellings never matched.

That is a Stage 1 caveat and a Stage 2 blocker. It is why 26 Stage 1 cells got a
*successful* `read_file` on a path that is denied from a clean workspace: whether
a model could reach the examples was partly decided by what ran before it. An
`--examples off` arm is meaningless while a previous model's finished
`workflow_generator.py` is sitting next door.

**Archives, never deletes.** Everything moves to a timestamped directory under
`$PROMPT_LAB_RUNS`, outside the workspace so the archive does not become residue
itself. The residue is evidence about what models do when unobserved, and this
project has already lost thirteen hours of results to an rmtree run as uid 1000.

**Allowlist, not denylist.** Residue is unbounded and inventive; what belongs is
short and known. Anything not on the list is archived, so a new kind of mess is
caught by default rather than after it has confounded a run.

Dry run unless `--apply`. Run it inside the container, where `workspace_root()`
is the path models actually write to.

    python3 tools/clean_workspace.py            # show what would move
    python3 tools/clean_workspace.py --apply    # move it
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.store import RUNS_DIR  # noqa: E402
from harness.workspace import (KEEP, archive_stale,  # noqa: E402
                               stale_entries, workspace_root)


def human(path: str) -> str:
    total = 0
    if os.path.isdir(path):
        for dirpath, _d, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    else:
        try:
            total = os.path.getsize(path)
        except OSError:
            total = 0
    for unit in ("B", "K", "M", "G"):
        if total < 1024 or unit == "G":
            return f"{total:.0f}{unit}"
        total /= 1024
    return "?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=workspace_root())
    ap.add_argument("--archive-root", default=os.path.join(RUNS_DIR, "workspace-archive"))
    ap.add_argument("--apply", action="store_true", help="move it; otherwise dry run")
    args = ap.parse_args()

    ws = os.path.realpath(args.workspace)
    if not os.path.isdir(ws):
        raise SystemExit(f"no workspace at {ws}")
    roots, projects = stale_entries(ws)

    print(f"workspace : {ws}")
    print(f"archive   : {os.path.realpath(args.archive_root)}/<timestamp>")
    print(f"keeping   : {', '.join(sorted(KEEP))}, and every dotfile")
    print()
    print(f"{len(roots)} stray entries at the workspace root:")
    for n in roots:
        print(f"   {n:32s} {human(os.path.join(ws, n))}")
    print()
    print(f"{len(projects)} project directories under workflows/:")
    for n in projects:
        print(f"   {n:32s} {human(os.path.join(ws, 'workflows', n))}")

    if not args.apply:
        print()
        print("dry run — nothing moved. Re-run with --apply to archive.")
        return
    dest, moved = archive_stale(args.archive_root, ws)
    print()
    print(f"archived {moved} entries to {dest}")


if __name__ == "__main__":
    main()
