"""Where the user's files live — the harness's mirror of Studio's own rule.

Studio moved the workspace root from `$HOME/work` to `$HOME` in
pegasus-isi/studio-webapp#402: a workflow project is `$HOME/workflows/<name>/`
and uploads land in `$HOME/data/`. The `work` level came from JupyterHub images
where only that subdirectory is persistent; Studio has no such split, and the
extra level disagreed with everything else that had already moved up to `$HOME`.

For the harness this is not a constant to update — it is a **property of the
trial**. Stage 1's 240 cells ran under `$HOME/work`, and their transcripts hold
relative paths that only mean the right thing when resolved against that root.
Flipping a module-level constant would regrade every one of them against a
directory their models never wrote to, and each would come back "wrote nothing"
— the same failure mode as grading an OpenCode transcript with the builtin's
tool names, one level up. So the root is recorded per trial and read back, and
rows that predate the field keep the root they actually ran under.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil

# What the workspace root was before studio-webapp#402. Rows without a recorded
# root ran under it, by definition: the field was added with the move.
LEGACY_ROOT = os.path.join(os.path.expanduser("~"), "work")

WORKFLOWS_SUBDIR = "workflows"


def workspace_root() -> str:
    """Studio's rule, copied rather than approximated: `STUDIO_WORKSPACE_DIR`
    wins, else `$HOME`."""
    return (os.environ.get("STUDIO_WORKSPACE_DIR")
            or os.environ.get("HOME")
            or os.path.expanduser("~"))


def workflows_dir(root: str | None = None) -> str:
    return os.path.join(root or workspace_root(), WORKFLOWS_SUBDIR)


def recorded_root(checks: dict | None) -> str:
    """The root a recorded trial ran under, for regrading it years later."""
    return ((checks or {}).get("workspace_root") or LEGACY_ROOT)


# ── clearing what previous trials left ────────────────────────────────────

# What legitimately lives in the workspace. `studio` and `data` are the product's,
# `AGENTS.md` is rewritten by the knowledge layer on every start, `workflows` is
# the projects directory whose *contents* are cleared separately, and `work` is
# the pre-#402 root, kept because it is read and never written.
#
# An allowlist, not a denylist, because residue is unbounded and inventive: 391
# trials left a directory named `$HOME`, one named `"`, one named
# `pecus-pecus-pecus-pecus`, and a ` workflows` with a leading space sitting
# beside the real one. No denylist would have predicted any of them.
KEEP = {"AGENTS.md", "data", "workflows", "studio", "work"}


def stale_entries(root: str | None = None) -> tuple[list[str], list[str]]:
    """(stray entries at the workspace root, project dirs under workflows/).

    Dotfiles are never listed. They hold the product's and the tooling's state —
    `.config/opencode/opencode.json` is the chat engine's provider list — and the
    cost of moving the wrong one dwarfs the cost of leaving a model's
    `.build-scratch` behind, which is not example material anyway.
    """
    ws = os.path.realpath(root or workspace_root())
    roots = sorted(n for n in os.listdir(ws)
                   if not n.startswith(".") and n not in KEEP)
    wf = os.path.join(ws, WORKFLOWS_SUBDIR)
    projects = sorted(os.listdir(wf)) if os.path.isdir(wf) else []
    return roots, projects


def archive_stale(archive_root: str, root: str | None = None,
                  stamp: str | None = None) -> tuple[str, int]:
    """Move everything `stale_entries` names into a timestamped archive.

    Archives, never deletes: the residue is evidence about what models do when
    nobody is grading them, and this project has already lost thirteen hours of
    results to an rmtree run as uid 1000.

    Returns (archive directory, entries moved).
    """
    ws = os.path.realpath(root or workspace_root())
    archive_root = os.path.realpath(archive_root)
    if not os.path.isdir(ws):
        raise SystemExit(f"no workspace at {ws}")
    # The archive must not live inside what it is emptying, or the next trial
    # finds it and it is residue again — the same nesting that once let one
    # `rm -rf` of the code tree take the results with it.
    if archive_root == ws or archive_root.startswith(ws + os.sep):
        raise SystemExit(f"refusing: archive {archive_root} is inside the workspace {ws}")

    roots, projects = stale_entries(ws)
    dest = os.path.join(archive_root, stamp or dt.datetime.now().strftime("%Y%m%dT%H%M%S"))
    # The stamp callers pass is the trial id, and a trial id repeats across runs:
    # s2-off, s2-on, s2b-off and s2b-block all archived `t02--deepseek--r0` here.
    # `shutil.move` onto an existing directory moves *into* it, so the second
    # sweep silently nested the archive as `<project>/<project>`, and the third
    # and fourth failed outright with "Destination path already exists" — 12
    # cells of ablation 1 recorded as `harness_error` before a turn was sent.
    # Archives are never deleted (above), so an occupied stamp gets a timestamp
    # suffix rather than being merged into or replaced.
    if os.path.exists(dest):
        dest = f"{dest}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}"
    moved = 0
    for sub, names, base in (("root", roots, ws),
                             (WORKFLOWS_SUBDIR, projects, os.path.join(ws, WORKFLOWS_SUBDIR))):
        for name in names:
            src = os.path.join(base, name)
            # Re-checked here rather than trusted from the listing: a symlink
            # swapped in between would otherwise move something outside the
            # workspace.
            if os.path.realpath(os.path.dirname(os.path.realpath(src))) != os.path.realpath(base):
                continue
            os.makedirs(os.path.join(dest, sub), exist_ok=True)
            shutil.move(src, os.path.join(dest, sub, name))
            moved += 1
    return dest, moved
