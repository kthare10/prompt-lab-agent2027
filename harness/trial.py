"""One trial, end to end.

    python3 -m harness.trial --task t00-hello-pegasus \
        --provider custom-mtd4weae --model qwen3.5-122b --depth plan

Runs inside the Studio container (D3). The host orchestrator launches it with
`docker exec`; because $HOME is the bind mount, the results it writes under
`runs/` are visible to the host immediately, with no copy step.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any

import yaml

from harness.graders.mechanical import (
    authored_files,
    dag_checks,
    generator_files,
    grade,
    l0_components,
    l0_score,
    project_names,
    resolve_project,
)
from harness.reasoning import has_reasoning_leak, strip_reasoning
from harness.store import RUNS_DIR, Store
from harness.opencode import OpenCode
from harness.studio import Studio, StudioError, WORKSPACE, TURN_TIMEOUT_S
from harness.workspace import archive_stale, workflows_dir, workspace_root

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS = os.path.join(ROOT, "tasks")
# Results live outside the code tree by default when PROMPT_LAB_RUNS says so.
#
# They used to be `<repo>/runs`, and the repo used to be inside the container's
# $HOME — where a model under test, whose `run_command` is an unrestricted shell
# as this uid, deleted the whole thing mid-sweep on 2026-09-05. Mounting the code
# read-only is not enough on its own: `runs/` has to stay writable, so a
# recursive delete aimed at the code tree still empties it. Keeping results on a
# separate path means one `rm -rf` cannot take both.
RUNS = RUNS_DIR

MAX_TURNS = 6
DEPTHS = ("auto", "none", "generate", "build", "plan")
# "auto" takes the depth the task declares, which is the right default:
# a container task graded without a build never learns whether it builds.
DEPTH_ORDER = ("none", "generate", "build", "plan")


# ── task + workspace ──────────────────────────────────────────────────────

def load_task(task_id: str) -> dict:
    path = os.path.join(TASKS, f"{task_id}.yaml")
    if not os.path.isfile(path):
        raise SystemExit(f"No such task: {path}")
    with open(path) as fh:
        return yaml.safe_load(fh)


def first_message(task: dict) -> str:
    """The prompt, prefixed with the slash command when the task uses a skill.

    A skill only loads when the *first* user message starts with its command —
    the model cannot reach for one later — so this is the only place skill
    exposure can be set.
    """
    prompt = task["prompt"].rstrip()
    skill = task.get("skill")
    return f"/{skill} {prompt}" if skill else prompt


def _inside_workspace(path: str) -> str:
    """Return `path` only if it is strictly inside the workflows directory.

    The harness deletes and overwrites paths built by joining names from a task
    spec onto WORKSPACE. Nothing validated the result, so a task with
    `project: ""` would have made `reset_project` call `rmtree(WORKSPACE)` —
    every project, not one — and a `..` in a project id or a `seed_files` target
    would have escaped the workspace altogether, into the home directory the
    container mounts.

    That is not hypothetical harm: on 2026-09-05 something running as this uid
    emptied that home directory mid-sweep, and the harness itself had to be
    ruled out by hand because nothing in the code made it impossible.

    Strictly inside: WORKSPACE itself is refused, not just paths above it.
    """
    root = os.path.realpath(WORKSPACE)
    resolved = os.path.realpath(path)
    if resolved == root or not resolved.startswith(root + os.sep):
        raise SystemExit(
            f"refusing to touch {resolved!r}: outside the workflows directory "
            f"({root!r}). This is a bug in a task spec or in path handling, not "
            "something to work around."
        )
    return resolved


def seed_project(task: dict, project_id: str) -> list[str]:
    """Put the task's input files where its prompt says they already are.

    t03 tells the model the source tarball "sits in the project directory". If
    it does not, every `%post` that untars and compiles fails, identically, for
    every model — a floor score that is a statement about the fixture and not
    about the model. Seeded into *both* candidate directory names, because which
    one the model uses is its own choice (see `resolve_project`).
    """
    seeded: list[str] = []
    for entry in task.get("seed_files") or []:
        # Either a bare filename, or {from: <fixture>, to: <path in the project>}
        # — a whole failed run directory has to land where Pegasus would have
        # written it, not at the project root.
        name = entry["from"] if isinstance(entry, dict) else entry
        rel = entry.get("to", name) if isinstance(entry, dict) else name
        src = os.path.join(ROOT, "fixtures", name)
        if not os.path.exists(src):
            raise SystemExit(f"task {task['id']} needs fixtures/{name}, which does not exist")
        for project in project_names(project_id):
            dest = _inside_workspace(os.path.join(WORKSPACE, project, rel))
            os.makedirs(os.path.dirname(dest) or dest, exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
            seeded.append(os.path.join(project, rel))
    return seeded


def reset_project(project_id: str, archive_to: str | None) -> None:
    """Clear the project directory, archiving whatever was there.

    The prompt names the project verbatim, so trials reuse the name rather than
    embedding a trial id in it — mutating the prompt would change the thing
    under test. Exclusive leases (D5) make that safe, and archiving keeps C8's
    braindump.yml glob from growing without bound.
    """
    os.makedirs(WORKSPACE, exist_ok=True)
    # Every name that counts as this project, not just the one the task asked
    # for. Clearing only `<project>` left `<project>-workflow` to accumulate
    # four models' files across a whole sweep, so each trial was graded partly
    # on its predecessors' work.
    for i, name in enumerate(project_names(project_id)):
        path = _inside_workspace(os.path.join(WORKSPACE, name))
        if not os.path.exists(path):
            continue
        if archive_to:
            dest = archive_to if i == 0 else f"{archive_to}-{name}"
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # `shutil.move` onto an existing directory moves *into* it rather
            # than replacing it, so re-running a cell under its own trial id —
            # what `remeasure_task.sh` and `rerun_censored.sh` both do — first
            # nested the archive silently as `prior_project/<name>`, and then
            # failed outright the next time round with "Destination path already
            # exists". A whole t07 cell was lost to it.
            #
            # The stale archive belongs to the run being replaced, so dropping it
            # is the right call, and it is inside this trial's own results
            # directory. The assertion is the point: an `archive_to` that ever
            # pointed somewhere else would make this an rmtree of the caller's
            # choosing, which is how this project lost 13 hours of results once.
            if os.path.exists(dest):
                assert os.path.basename(os.path.dirname(dest)).startswith(
                    ("t0", "t1", "t2")) or "prior_project" in dest, dest
                shutil.rmtree(dest)
            shutil.move(path, dest)
        else:
            shutil.rmtree(path)


# ── the examples ablation (PLAN.md §8 item 0) ─────────────────────────────
# The skills tell models to read `assets/examples/workflow_generator_tnseq.py`
# and five siblings. `read_file` resolves relative to ~/work and refuses
# anything outside it, so across Stage 1 those instructions produced 291 denied
# reads — while `run_command`, which `_safe_path` does not guard at all,
# succeeded 941 times on the same files. The material was never dead weight; it
# was reachable only by the route nothing documents.
#
# So the `on` arm copies the plugin's `assets/` to ~/work/assets, which makes
# the instruction true **verbatim** — the same relative path the skills already
# print now resolves. Nothing in the knowledge base changes, which is what keeps
# this an ablation of reachability rather than of wording.
#
# It has to be undone as well as done: a trial run with `off` after one run with
# `on` would inherit the copy and silently join the other arm.
EXAMPLES_SRC = os.path.join(
    os.path.expanduser("~"), ".local", "share", "pegasus-ai", "marketplace",
    "plugins", "pegasus-ai", "assets")
EXAMPLES_DEST = os.path.join(workspace_root(), "assets")


def workspace_residue() -> list[str]:
    """What previous trials left in ~/work, which the next one can read.

    `reset_project` clears ~/work/workflows/<project> and nothing else, so
    anything a model stages beside it survives into every later trial. That is
    not hypothetical: ~/work held `.pegasus_ref.md` (48 KB of the reference),
    `.gen_template.py`, `.temp_assets/` and an `assets/templates/` a model had
    copied there — which is why 26 Stage 1 cells got a *successful* `read_file`
    on a path that is denied from a clean workspace. Whether a model could read
    the examples was partly decided by what ran before it.
    """
    root = workspace_root()
    if not os.path.isdir(root):
        return []
    # Dotfiles and the two directories Studio owns are the furniture, not
    # residue. Since the root became $HOME (#402) the unfiltered listing is
    # mostly shell config, which buries the thing this is for: what a previous
    # trial left where the next one can read it.
    return sorted(n for n in os.listdir(root)
                  if not n.startswith(".") and n not in ("workflows", "data"))[:40]


# Every copy of the examples a model can reach from inside the container. The
# marketplace checkout is the one the skills point at; the image ships a second,
# root-owned, world-readable checkout of the same sha under /opt/pegasus-ai; and
# the `on` arm's workspace copies were archived, readable, under the results
# mount. Ablation 1's block arm (s2b) covered only the first, and 13 of 60 cells
# read the examples anyway — 9 by `chmod u+rx` on a directory the model's own
# uid owned, 2 from /opt/pegasus-ai, 2 from the archive.
EXAMPLES_COPIES = (
    EXAMPLES_SRC,
    "/opt/pegasus-ai/marketplace/plugins/pegasus-ai/assets",
)
ARCHIVE_ROOT = os.path.join(RUNS_DIR, "workspace-archive")


def _archived_example_copies() -> list[str]:
    """`<archive>/<stamp>/root/assets` — what the `on` arm left behind."""
    if not os.path.isdir(ARCHIVE_ROOT):
        return []
    out = []
    for stamp in os.listdir(ARCHIVE_ROOT):
        d = os.path.join(ARCHIVE_ROOT, stamp, "root", "assets")
        if os.path.isdir(d):
            out.append(d)
    return out


def _readable_example_copies() -> list[str]:
    """Every copy a model could read right now — empty means blocked."""
    return [d for d in (*EXAMPLES_COPIES, *_archived_example_copies())
            if os.path.isdir(d) and os.access(d, os.R_OK | os.X_OK)]


def _marketplace_assets_readable(readable: bool) -> None:
    """Restore the shipped state of the marketplace copy, when it is ours to set.

    The block itself is no longer applied here. `chmod 000` by the harness was
    `chmod 000` by the model's own uid, and 9 block cells undid it with
    `chmod u+rx` — a blocked directory the subject can unblock is not a
    treatment. `scripts/examples_block.sh` (host side, as root in the container)
    now chowns every copy to root, which this uid can neither read nor undo;
    all this function can do is put back a block it applied itself.
    """
    if not os.path.isdir(EXAMPLES_SRC):
        return
    try:
        os.chmod(EXAMPLES_SRC, 0o755 if readable else 0o000)
    except PermissionError:
        # Root holds it: the host-side block is on. Verified, not toggled, below.
        pass


def set_examples(mode: str) -> str:
    """Where the shipped examples are reachable from, for this trial.

      off    as the product ships: no workspace copy, and every copy readable —
             which on this engine means the examples ARE available.
      on     plus a copy at <workspace>/assets, so the skills' own relative path
             resolves. Largely redundant on OpenCode; kept because it is what
             Stage 1's arm meant and the two must stay comparable.
      block  every copy unreadable, held by root. This is the arm that actually
             tests whether the examples matter, now that `off` has been shown not
             to withhold them. The harness cannot apply it — it runs as the uid
             the model runs as — so it **verifies** that `scripts/examples_block.sh
             on` was run, and refuses to measure if any copy is readable.

    Both directions are checked, because a block left in place is a treatment
    applied to an arm that did not ask for it — the overlay durability failure
    in a new costume, and that one cost a week of results read under the wrong
    condition.
    """
    # Restore first, for anything a killed trial of an older harness left at
    # chmod 000 under this uid. A root-held block is untouched by this.
    _marketplace_assets_readable(True)

    dest = os.path.realpath(EXAMPLES_DEST)
    # Narrower than `_inside_workspace`, which guards ~/work/workflows. This
    # path is its sibling, and an rmtree needs a guard of its own rather than
    # an exemption from that one.
    if os.path.basename(dest) != "assets" or os.path.dirname(dest) != os.path.realpath(
            workspace_root()):
        raise SystemExit(f"refusing to manage {dest}: not <workspace>/assets")
    if os.path.exists(dest):
        shutil.rmtree(dest)

    def _some(paths: list[str]) -> str:
        more = len(paths) - 3
        return ", ".join(paths[:3]) + (f" and {more} more" if more > 0 else "")

    readable = _readable_example_copies()
    if mode == "block":
        if readable:
            raise SystemExit(
                "--examples block, but the examples are readable at: "
                + _some(readable)
                + " — run `scripts/examples_block.sh on` on the host first")
        # Held by root, not by this uid: a block this uid applied is one the
        # model can undo, and that is exactly the failure being closed.
        if os.path.isdir(EXAMPLES_SRC) and os.stat(EXAMPLES_SRC).st_uid == os.getuid():
            raise SystemExit(
                f"--examples block, but {EXAMPLES_SRC} is owned by this uid — the "
                "model could chmod it back. Run `scripts/examples_block.sh on`.")
        return "block"

    blocked = [d for d in (*EXAMPLES_COPIES, *_archived_example_copies())
               if os.path.isdir(d) and not os.access(d, os.R_OK | os.X_OK)]
    if blocked:
        raise SystemExit(
            f"--examples {mode}, but a block is still in place at: "
            + _some(blocked)
            + " — run `scripts/examples_block.sh off` on the host first")
    if mode == "on":
        if not os.path.isdir(EXAMPLES_SRC):
            raise SystemExit(f"--examples on, but no assets at {EXAMPLES_SRC}")
        shutil.copytree(EXAMPLES_SRC, dest)
        return "on"
    return "off"


# ── the Generate form, filled the way a user would ────────────────────────

# Files that are the workflow itself rather than its input, used to tell a data
# directory from a code directory.
_CODE_SUFFIXES = (".py", ".sh", ".def", ".md", ".yml", ".yaml", ".cfg", ".txt")

# Directory names that hold *products*, not inputs. Without this the search
# happily picks a model's own `test_output/summaries` — it contains data files
# and its name matches "test" — and hands the generator its own output as input.
_PRODUCT_NAMES = (
    "output", "outputs", "out", "result", "results", "summaries", "summary",
    "submit", "scratch", "work", "logs", "log", "cache", "sif",
)

# Names that mean "the example inputs", which is what the task asked for.
_INPUT_NAMES = ("data", "input", "inputs", "example", "examples", "sample", "samples", "test")

# Pegasus's own artefacts, plus the `?` directory a model creates when it passes
# `~/work/...` to a tool that does not expand `~` — the silent-failure case
# STUDIO_ENV.md warns about. Neither is input, and both contain data files.
_NOT_INPUT = re.compile(r"^(\?|run\d+|\..*|.*\.sif)$")


def _example_dir(project_dir: str) -> str | None:
    """The directory holding the example inputs the task asked the model for.

    Returned relative to the project, because the generator runs with the
    project as its cwd. Preference, in order: a directory whose path names it as
    input, then the shallowest, then lexicographic so the choice is stable
    across runs. Directories that look like products are skipped outright.
    """
    candidates: list[tuple[int, int, str]] = []
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in _PRODUCT_NAMES
            and not _NOT_INPUT.match(d)
            and d != "__pycache__"
        ]
        if not [f for f in filenames if not f.lower().endswith(_CODE_SUFFIXES)]:
            continue
        rel = os.path.relpath(dirpath, project_dir)
        if rel == ".":
            continue
        parts = [p.lower() for p in rel.split(os.sep)]
        if any(p in _PRODUCT_NAMES for p in parts):
            continue
        # An exact name beats a compound one. A model that ships both `data/test`
        # (the "couple of small example CSVs" the prompt asks for) and
        # `data/scale_test` (40 stations) must be measured on the same input as a
        # model that shipped only the former, or their job counts are not
        # comparable.
        if parts[-1] in _INPUT_NAMES:
            rank = 0
        elif any(p in _INPUT_NAMES for p in parts):
            rank = 1
        else:
            rank = 2
        candidates.append((rank, len(parts), rel))
    return min(candidates)[2] if candidates else None


def generator_args(schema: dict, project_dir: str, task: dict) -> tuple[list[str], list[str]]:
    """Fill the generator's required arguments, as Studio's Generate form does.

    Studio introspects the generator's argparse and renders a form; the user
    supplies the input directory and presses Generate. The harness was calling
    generate with *no* args, so every generator that declared a required
    argument — which the shipped template teaches — failed with an argparse
    usage error and scored `generate_ok: false` for doing the expected thing.

    Deterministic and recorded: a value comes from the task's own
    `generate_args` (keyed by dest) if it declares one, then from the
    introspected default, then from the shape of the parameter. Anything left
    unresolved is returned rather than guessed, so the trial reports why.
    """
    declared = (task.get("generate_args") or {})
    args: list[str] = []
    unresolved: list[str] = []
    supplied: set[str] = set()
    example = _example_dir(project_dir)

    for param in schema.get("params") or []:
        dest, flag = param.get("dest"), param.get("flag")
        if not flag or not param.get("required"):
            continue                      # optional: argparse applies its default
        if dest in declared:
            value = declared[dest]
        elif param.get("default") is not None:
            value = param["default"]
        elif param.get("choices"):
            value = param["choices"][0]
        elif param.get("type") in ("directory", "file", "path"):
            value = example
        else:
            value = None
        if param.get("is_flag") or param.get("type") == "boolean":
            continue                      # a required store_true is a contradiction
        if value is None:
            unresolved.append(f"{flag} ({param.get('type')})")
            continue
        args += [flag, str(value)]
        supplied.add(dest)

    # Mutually exclusive required groups. argparse marks each member
    # `required=False` — the requirement lives on the group — so a filler that
    # only looks at `required` skips every member and reports nothing missing.
    # That is how a generator whose `--test` / `--samples` group was never
    # satisfied failed with "Either --test or --samples must be provided" while
    # the trial recorded `unresolved_args: []`. A silent gap, not a loud one.
    by_dest = {p.get("dest"): p for p in schema.get("params") or []}
    for group in schema.get("mutex_required") or []:
        if any(d in supplied for d in group):
            continue                       # the required loop already covered it
        for dest in _mutex_order(group, by_dest, declared):
            param = by_dest.get(dest) or {}
            flag = param.get("flag")
            if not flag:
                continue
            if param.get("is_flag") or param.get("type") == "boolean":
                args.append(flag)          # a switch needs no value
                break
            value = (declared.get(dest) or param.get("default")
                     or (param.get("choices") or [None])[0]
                     or (example if param.get("type") in ("directory", "file", "path") else None))
            if value is not None:
                args += [flag, str(value)]
                break
        else:
            unresolved.append("one of " + ", ".join(
                (by_dest.get(d) or {}).get("flag") or d for d in group))
    return args, unresolved


def _mutex_order(group: list[str], by_dest: dict, declared: dict) -> list[str]:
    """Which member of a mutually exclusive group to satisfy.

    A value the task itself declared comes first — it is the closest thing to
    the answer a user would type. Then a switch, since `--test` is the "use the
    bundled sample data" affordance and needs no value invented for it. Then
    anything pointing at a path, then whatever is left, alphabetically so the
    choice is reproducible across runs.
    """
    def rank(dest: str) -> tuple[int, str]:
        param = by_dest.get(dest) or {}
        if dest in declared:
            return (0, dest)
        if param.get("is_flag") or param.get("type") == "boolean":
            return (1, dest)
        if param.get("type") in ("directory", "file", "path"):
            return (2, dest)
        return (3, dest)
    return [d for d in sorted(group, key=rank)]


def run_flow(
    studio: Studio,
    task: dict,
    project_name: str,
    project_dir: str,
    wanted: tuple[str, ...] | list[str],
    site: str = "condorpool",
    output_site: str = "local",
) -> dict[str, Any]:
    """Execution to the requested depth, server-side (D3).

    Shared by `trial.py` and `reflow.py` deliberately: `regrade.py` once carried
    its own second copy of the grader's path checks, the two drifted, and the
    disagreement floored five t01 cells. One implementation, two callers.
    """
    flow: dict[str, Any] = {}
    if "generate" not in wanted or not studio.project(project_name):
        if wanted and tuple(wanted) != ("none",):
            flow["error"] = f"project {project_name!r} not discoverable — nothing to run"
        return flow

    # Studio's `/generate` runs exactly one entry point, resolved by
    # `workflow_scanner.py`. A phased design has none: generation is split
    # across scripts, and phase 2 consumes output that only exists once phase
    # 1's workflow has run. Attempting it returns a 400 that reads like a
    # harness fault and kills the trial — on t13, `kimi` lost a complete and
    # arguably correct answer that way. Recorded like `unfillable`: attempted
    # honestly, not measurable, excluded from L0.
    gen_paths, gen_style = generator_files(project_dir)
    if gen_style == "phased":
        flow["generate"] = {
            "status": "phased",
            "generators": [os.path.basename(g) for g in gen_paths],
            "output": "",
        }
        return flow
    if not gen_paths:
        # No entry point Studio could run, under any of the names it accepts.
        # Asking anyway returns the same 400 and the exception ends the trial as
        # `harness_error` — which buries a real model failure under a label that
        # says the harness broke. `Kimi-K2.6` wrote no generator at all on t13
        # and was recorded that way. This is a failed stage, and says so.
        flow["generate"] = {
            "status": "no_generator", "args": [], "unresolved_args": [], "output": "",
        }
        return flow

    schema = (studio.params(project_name) or {}).get("generator") or {}
    gen_args, unresolved = generator_args(schema, project_dir, task)
    if unresolved:
        # Studio would render a form and a real user would type a value here.
        # The harness could not, so the stage was never fairly attempted —
        # recorded as `unfillable` and excluded from L0 rather than scored as a
        # model failure the harness caused.
        flow["generate"] = {"status": "unfillable", "args": gen_args,
                            "unresolved_args": unresolved, "output": ""}
        return flow
    gen = studio.generate(project_name, gen_args)
    flow["generate"] = {
        "status": gen.get("status"),
        "args": gen_args,                  # what the Generate form was filled with
        "unresolved_args": unresolved,     # required, and not fillable
        "output": gen.get("output", "")[-3000:],
    }
    if gen.get("status") != "generated":
        return flow

    if "build" in wanted and any(
        f.endswith(".def") for f in authored_files(project_dir)
    ):
        b = studio.build(project_name)
        flow["build"] = {"status": b["status"], "error": b.get("error"),
                         "output": b.get("output", "")[-3000:]}
    if "plan" in wanted and flow.get("build", {}).get("status") != "error":
        plan = studio.plan(project_name, site=site, output_site=output_site)
        flow["plan"] = {
            "status": plan.get("status"),
            "run_dir": plan.get("run_dir", ""),
            "output": plan.get("output", "")[-3000:],
        }
    return flow


# Stage outcomes that mean "the harness could not fairly attempt this", as
# opposed to "the model's workflow did not work". Both are excluded from L0.
UNMEASURED = ("unfillable", "phased")


def stage_checks(flow: dict, wanted: tuple[str, ...] | list[str]) -> dict[str, bool | None]:
    """Three states, not two, and the middle one matters:

        not requested by the task's depth        -> None  (excluded from L0)
        requested but never reached              -> False (the workflow did not work)
        requested and attempted                  -> the actual result

    Collapsing the middle case into None inverts the ranking: a model that
    writes no generator at all is scored on fewer checks than one that writes a
    complete workflow with a single bad import, and outscores it.
    """
    def stage(name: str) -> bool | None:
        if name not in wanted:
            return None
        entry = flow.get(name)
        if not isinstance(entry, dict):
            # Never reached *because an earlier stage was unfillable* is not a
            # failure either — the whole chain went unmeasured.
            gen = flow.get("generate")
            if isinstance(gen, dict) and gen.get("status") in UNMEASURED:
                return None
            return False
        if entry.get("status") in UNMEASURED:
            return None
        return entry.get("status") in ("generated", "built", "planned")

    return {n + "_ok": stage(n) for n in ("generate", "build", "plan")}


# ── user simulator ────────────────────────────────────────────────────────

# Ways a model signals it is waiting on the user without ending in a question
# mark. Kept explicit rather than clever: each one was observed in a transcript.
_ASKING = re.compile(
    r"(let me know|once you (confirm|tell|answer)|please (confirm|clarify|specify|provide)"
    r"|could you (confirm|clarify|tell|let)"
    # "I need your actual pipeline files", "I need that detail", "Two things I
    # need from you before writing anything" — qwen-3.8-27b, s1-jetstream t05a
    # and t14. Both were build tasks and both scored at the floor for asking.
    r"|i need (a few|some|the following|your|that|these|those|from you|to know)"
    r"|need from you"
    # "Drop the script in and I'll come back with a plan before writing anything."
    # A bare "tell me the…" was tried here and dropped: it also matches a model
    # that has finished and is offering follow-up ("if the run fails, tell me the
    # run and I'll dig into the logs" — GLM-5.2-FP8 on t00, 31 tool calls and six
    # files written). Both real cases match on other alternatives anyway.
    r"|drop (the|your) \w+ in"
    # `write` and `create` were missing from a list that already had `scaffold`.
    r"|before i (start|begin|generate|build|scaffold|write|create|proceed)"
    r"|before writing anything|waiting (for|on) (your|you)"
    r"|answers? to (the|these)|questions? (above|below)|then i(’|')?ll (start|begin|write|generate)"
    r"|let(’|')?s start by (filling|confirming)|fill(ing)? in (those|these|the) details"
    r"|once (i have|you have) (that|those|these)"
    # Patched for the AGENT 2027 reference run, never committed: "To start, I need:" and
    # "Once I have the Snakefile" (claude-opus-5-5, s1-claude t05a) and "just share that
    # directory path" (kimi, s1-nrp t05a). Neither matched, so both trials ended after one turn.
    r"|to start, i need|i need:|once i have (the|your) \w+|just share (that|the|your) \w+"
    # Added 2026-11-15 for the repeat runs, never committed: "Please share your Snakefile path"
    # (Kimi-K2.6, s1-anchor-azure t05b), "Once these details are provided" (gpt-oss, s1-nrp t11),
    # 'or say "proceed"' (qwen3, s1-nrp t13), "To build it, I need" (GLM-5.2-FP8, s1-jetstream
    # t13), and "I need to clarify one thing" (minimax-m2.7, s1-fabric t11).
    r"|please (share|paste|send) |once (these|those|the) \w+ (are|is) provided"
    r"|or say .proceed|to build (it|this), i need|i need to clarify)",
    re.I,
)


def looks_like_question(text: str) -> bool:
    """Is the model waiting on the user?

    This used to be `"?" in text[-400:]`, which cost real results. gpt-oss asks
    its questions in a numbered list and then closes with a statement —
    "Let me know the answers, and I'll start writing the files." — so the tail
    holds no question mark, the harness treated the turn as final, and the model
    scored 0.100 for doing exactly what `pegasus-scaffold` tells it to do:
    ask before creating anything.

    So: a question mark anywhere in the last two paragraphs, or a phrase that
    hands the turn back. Both are checked over the tail rather than the whole
    message, because a model that asks a rhetorical question early and then
    gets on with the work has not stopped.
    """
    tail = text.strip()[-1200:]
    return "?" in tail or bool(_ASKING.search(tail))


# Returned as the key when every scripted answer, including `default`, has been
# used. The caller ends the conversation rather than repeating itself.
EXHAUSTED = "exhausted"


def pick_answer(
    question: str, bank: dict[str, str], used: set[str] | None = None
) -> tuple[str, str]:
    """Choose a scripted reply, returning (key, text).

    Keys are matched on token overlap with the question. Which key matched is
    recorded per turn: the matching is a confound in its own right — a model
    whose questions are phrased oddly falls through to `default`, which is a
    thinner prompt, and that must not be mistaken for the model being worse.

    `used` carries the keys already answered this trial. Without it the matcher
    is stateless and re-picks its single best key every turn, so a model that
    asks about several things in sequence is handed the same sentence over and
    over. That is not hypothetical: on t02, `gpt-oss` was told "Between 500 and
    2,000 depending on the survey" twice, asked again, and gave up after three
    turns having written nothing — scored as a model failure.
    """
    if not bank:
        return "none", "Please go ahead."
    used = used or set()
    words = set(re.findall(r"[a-z]+", question.lower()))

    def stem(w: str) -> str:
        # Crude but sufficient: the keys are short snake_case nouns, and the only
        # mismatch that mattered in practice was plural. "How many tiles?" scored
        # zero against `tile_count` and fell through to the thin default.
        return w[:-1] if len(w) > 3 and w.endswith("s") else w

    stems = {stem(w) for w in words}
    best, score = None, 0
    for key in bank:
        if key == "default" or key in used:
            continue
        key_stems = {stem(w) for w in re.findall(r"[a-z]+", key.lower().replace("_", " "))}
        overlap = len(stems & key_stems)
        if overlap > score:
            best, score = key, overlap

    if best is None or score == 0:
        # Nothing matched, or everything that matched is already answered. Offer
        # an unused scripted answer before falling back to the thin default, so a
        # model that keeps asking still learns something new each turn.
        remaining = [k for k in bank if k != "default" and k not in used]
        if remaining and score == 0 and used:
            best = remaining[0]
        elif "default" in used:
            # The bank is spent, default included. Handing over the same generic
            # sentence again is not a conversation: on t13, `kimi` was told
            # "Tell me what you'd recommend and why." twice in a row, spent a
            # round restating its previous answer, and tripped
            # `rounds_exhausted` — a harness artefact scored as model behaviour.
            return EXHAUSTED, ""
        else:
            best = "default" if "default" in bank else next(iter(bank))
    return best, bank[best]


def _turn_timeout(args: argparse.Namespace) -> float | None:
    """Resolve the per-turn ceiling: -1 = module default, 0 = no ceiling.

    Sweep builds its Namespace by hand, so a missing attribute must mean the
    default rather than an AttributeError mid-sweep.
    """
    raw = getattr(args, "turn_timeout", -1.0)
    if raw is None or raw < 0:
        return TURN_TIMEOUT_S
    return None if raw == 0 else float(raw)


# ── the trial ─────────────────────────────────────────────────────────────

def run_trial(args: argparse.Namespace) -> dict:
    task = load_task(args.task)
    project_id = task["project"]
    trial_id = args.trial_id or f"{args.task}--{args.model or 'default'}--{uuid.uuid4().hex[:6]}"
    trial_id = re.sub(r"[^A-Za-z0-9._-]+", "-", trial_id)
    trial_dir = os.path.join(RUNS, args.run_id, trial_id)
    os.makedirs(trial_dir, exist_ok=True)

    # Which engine the conversation runs on. `studio-webapp` main moved the
    # chat panel onto `opencode serve` behind a byte proxy, so the builtin loop
    # is still there and is no longer what a user drives. Both stay runnable and
    # the engine is recorded on every trial, because a score from one is not a
    # score from the other: different system prompt, different tool set,
    # different permission model.
    studio = (OpenCode(args.studio_url) if args.backend == "opencode"
              else Studio(args.studio_url))
    record: dict[str, Any] = {
        "trial_id": trial_id,
        "run_id": args.run_id,
        "task": args.task,
        "exposure": task.get("exposure"),
        "skill": task.get("skill"),
        "agent": task.get("agent", "general"),
        "provider": args.provider,
        "model": args.model,
        "depth": args.depth,
        "project_id": project_id,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "running",
        "repeat": args.repeat,
    }

    def save() -> None:
        with open(os.path.join(trial_dir, "trial.json"), "w") as fh:
            json.dump(record, fh, indent=2)

    save()
    log = open(os.path.join(trial_dir, "transcript.jsonl"), "w")

    def emit(kind: str, payload: Any) -> None:
        log.write(json.dumps({"t": time.time(), "kind": kind, "payload": payload}) + "\n")
        log.flush()

    try:
        # Preconditions. Each is a constraint that silently ruins a sweep.
        studio.health()
        record["llm_config"] = studio.ensure_llm_config()          # C1
        studio.assert_container_free()                              # C2
        record["approval_mode"] = (
            "gate_off" if args.approval == "off" else "auto_approve"
        )
        # The ceiling is a property of the measurement, not of the model: a
        # `turn_timeout` score is a lower bound, and what it is a lower bound of
        # depends entirely on this number. It was not recorded anywhere, so a run
        # measured under two different budgets — which is exactly what raising it
        # for one slow model produces — was indistinguishable from a uniform one.
        # NULL on a row predating this field means the 2400s default.
        record["turn_timeout_s"] = _turn_timeout(args)
        studio.set_command_approval(args.approval != "off")         # C3
        provider_name = studio.provider_name(args.provider or "")
        record["knowledge_sha"] = knowledge_sha()
        record["overlay_sha"] = overlay_sha()

        conversation_id = f"trial-{trial_id}"
        reset_project(project_id, os.path.join(trial_dir, "prior_project"))
        # `reset_project` clears this trial's project and nothing else, so
        # everything a previous trial staged beside it is still readable. Over
        # 391 trials that reached 2.9 GB, and it is not inert: 26 Stage 1 cells
        # got a *successful* `read_file` on a path denied from a clean workspace.
        # An `--examples off` arm is meaningless while a previous model's
        # finished generator sits next door, so the ablation needs this per
        # trial, not once before the sweep.
        #
        # Off by default: every trial measured so far ran dirty, and flipping the
        # default would make new rows incomparable with them for a reason nothing
        # in the record would show. The arm is recorded, like `examples` and
        # `approval_mode`, so the two are never pooled by accident.
        if args.clean_workspace == "on":
            dest, moved = archive_stale(
                os.path.join(RUNS_DIR, "workspace-archive"), stamp=trial_id)
            if moved:
                print(f"[workspace] archived {moved} stale entries to {dest}")
        examples_mode = set_examples(args.examples)
        residue = workspace_residue()
        # Where this engine resolves a relative path. OpenCode joins onto the
        # directory its session was created in — $HOME unless one is passed —
        # while the grader's default is ~/work. Read before the turn, because
        # every write in it is interpreted against this.
        engine_dir = ""
        if args.backend == "opencode":
            try:
                engine_dir = studio.directory()
            except Exception as e:  # noqa: BLE001
                engine_dir = f"unknown: {e}"
        record["seeded"] = seed_project(task, project_id)

        messages = [{"role": "user", "content": first_message(task)}]
        emit("user", messages[0]["content"])

        turns: list[dict] = []
        bank = task.get("sim_answers") or {}
        asked: set[str] = set()          # scripted answers already given (see pick_answer)
        for turn_no in range(1, MAX_TURNS + 1):
            result = studio.chat(
                messages,
                provider=args.provider,
                model=args.model,
                agent=task.get("agent", "general"),
                conversation_id=conversation_id,
                auto_approve=args.approval != "off",
                timeout_s=_turn_timeout(args),
                **({"session_dir": args.session_dir}
                   if args.backend == "opencode" else {}),
            )
            turn = {
                "n": turn_no,
                "text": strip_reasoning(result.text),
                "reasoning_leak": has_reasoning_leak(result.text),
                "tool_calls": result.tool_calls,
                "tool_results": [
                    {"name": r.get("name"), "result": str(r.get("result", ""))[:2000]}
                    for r in result.tool_results
                ],
                "permissions": result.permissions,
                "meta": result.meta,
                "error": result.error,
                "ttft_s": result.ttft_s,
                "duration_s": result.duration_s,
                "timed_out": result.timed_out,
            }
            turns.append(turn)
            emit("assistant", turn)

            if result.error:
                record["status"] = "model_error"
                record["error"] = result.error
                break

            if result.timed_out:
                # Not a model_error: the model did not misbehave, it ran past a
                # budget the harness set. Kept separate so the leaderboard can
                # report "too slow to screen" without pooling it into failures.
                record["status"] = "turn_timeout"
                record["error"] = (
                    f"turn {turn_no} exceeded the per-turn ceiling of "
                    f"{_turn_timeout(args)}s"
                )
                break

            # The cleaned text is what goes back to the model and what drives
            # question detection: a stray `</think>` in the history is noise the
            # model did not write and should not have to read.
            messages.append({"role": "assistant", "content": turn["text"]})
            if not looks_like_question(turn["text"]) or turn_no == MAX_TURNS:
                break
            key, answer = pick_answer(turn["text"], bank, asked)
            if key == EXHAUSTED:
                # Nothing left to tell it. Ending here keeps the trial honest:
                # the model asked a question the script cannot answer, which is
                # a fact about the task, not a failure to grade.
                record["sim_exhausted"] = True
                break
            asked.add(key)
            turn["sim_key"] = key           # logged: matching is itself a confound
            messages.append({"role": "user", "content": answer})
            emit("user", {"sim_key": key, "text": answer})

        studio.assert_turn_was_ours(conversation_id)                # C2, post-flight

        # The directory the model actually used, which is not always the name
        # the task asked for — the scaffold skill instructs a `-workflow`
        # suffix. Resolved once, then used for the snapshot, grading and every
        # server-side stage, so all three agree on what "the project" is.
        # Where a relative write resolves: the session's directory on OpenCode,
        # and the workspace root on the builtin, whose `_safe_path` joins onto
        # it. Both are the same place now that the root is $HOME and the engine
        # starts there — but they are different questions, so they stay
        # different variables.
        ws_root = workspace_root()
        write_root_for = (args.session_dir or engine_dir or None
                          if args.backend == "opencode" else ws_root)
        project_name = resolve_project(project_id, turns, workspace=workflows_dir(ws_root),
                                       backend=args.backend, write_root=write_root_for)
        project_dir = os.path.join(WORKSPACE, project_name)
        record["project_dir"] = project_name
        if os.path.isdir(project_dir):
            # Seeded fixtures are excluded: t08 ships a 6.8 MB failed run
            # directory, and copying it into all 14 trial directories would add
            # ~95 MB per sweep of bytes that are already in `fixtures/`.
            # `reflow.py` re-seeds from the fixture, so nothing is lost.
            seeded_here = {
                rel.split(os.sep, 1)[1]
                for rel in record.get("seeded", [])
                if rel.split(os.sep, 1)[0] == project_name and os.sep in rel
            }
            # `symlinks=True` because the tree is the model's, not ours. A model
            # that installs a toolchain into its project leaves symlinks in it,
            # and dereferencing them both inflates the record and *fails* on a
            # dangling one: qwen-3.8-27b's t05a cell died on
            # `tools/share/doc/libncurses6`, after the model had done the work,
            # and the trial was lost to a snapshot rather than to anything it did.
            shutil.copytree(
                project_dir, os.path.join(trial_dir, "workspace"), dirs_exist_ok=True,
                symlinks=True,
                ignore=lambda d, names: [
                    n for n in names
                    if os.path.relpath(os.path.join(d, n), project_dir) in seeded_here
                    # Rebuildable and enormous: 0.9 GB of `.sif` and a 21 MB
                    # Pegasus worker tarball per run had accumulated in `runs/`,
                    # which is 3.2 GB of which the actual record is 7.9 MB. The
                    # definition file is what a grader or judge reads; the image
                    # it produces is not evidence.
                    or n.endswith(".sif")
                    or n.startswith("pegasus-worker-")
                ],
            )

        record["turns"] = len(turns)
        # Both engines report usage as `meta["tokens"] = {"input", "output"}`;
        # the flat `input_tokens` this once read is written by nobody, so every
        # trial recorded 0/0. That is not a cosmetic loss: `cohort.py` reads a
        # zero input count as "this cell never reached a model" (finding 4), so
        # a whole sweep would have been dropped as not-measured. Accept the flat
        # spelling too rather than assuming only the nested one ever appears.
        def _tokens(turn: dict, which: str) -> int:
            meta = turn["meta"] or {}
            nested = meta.get("tokens") or {}
            return nested.get(which) or meta.get(f"{which}_tokens") or 0

        record["tokens"] = {
            "input": sum(_tokens(t, "input") for t in turns),
            "output": sum(_tokens(t, "output") for t in turns),
            # Totals are summed over turns as well as steps; peak is the largest
            # single context anywhere in the trial. Absent on a backend that
            # reports no per-step usage, which is the honest answer rather than
            # a zero that reads as "it was asked nothing".
            "input_total": sum(_tokens(t, "input_total") for t in turns) or None,
            "output_total": sum(_tokens(t, "output_total") for t in turns) or None,
            "input_peak": max((_tokens(t, "input_peak") for t in turns), default=0) or None,
            "steps": sum(_tokens(t, "steps") for t in turns) or None,
        }
        record["wall_s"] = round(sum(t["duration_s"] for t in turns), 1)
        record["ttft_s"] = turns[0]["ttft_s"] if turns else None
        # What a relative path was joined onto. OpenCode uses the session's
        # directory; the builtin uses ~/work, which is `grade`'s default.
        record["checks"] = grade(task, project_dir, turns, args.backend, write_root_for,
                                 scan_root=workflows_dir(ws_root))
        # After grade(), which replaces the dict wholesale. The arm is a
        # condition of the trial, not a check, but `checks` is the column that
        # takes a new field without a schema migration — the reason it is JSON.
        record["checks"]["examples"] = examples_mode
        record["checks"]["clean_workspace"] = args.clean_workspace
        record["checks"]["backend"] = args.backend
        record["checks"]["engine_directory"] = engine_dir
        record["checks"]["workspace_root"] = ws_root
        record["checks"]["provider_name"] = provider_name
        record["checks"]["session_dir"] = args.session_dir or ""
        record["checks"]["workspace_residue"] = residue
        # How often the engine stopped to ask. On the builtin engine "gate off"
        # meant it never asked; OpenCode asks on its own, so the posture shows
        # up here rather than in whether anything was approved.
        record["checks"]["permission_prompts"] = sum(
            len(t.get("permissions") or []) for t in turns)

        # Execution, to the requested depth. Both stages run server-side, which
        # is to say in this container, with the real Pegasus toolchain.
        depth = task.get("depth", "plan") if args.depth == "auto" else args.depth
        if depth not in DEPTH_ORDER:
            depth = "plan"
        record["depth"] = depth
        wanted = DEPTH_ORDER[: DEPTH_ORDER.index(depth) + 1]

        flow = run_flow(
            studio, task, project_name, project_dir, wanted,
            site=args.site, output_site=args.output_site,
        )
        record["flow"] = flow
        record["checks"].update(stage_checks(flow, wanted))
        record["checks"].update(dag_checks(task, project_dir))

        if record["status"] == "running":
            record["status"] = "done"

    except StudioError as e:
        record["status"] = "quarantined" if "Turn ownership" in str(e) else "harness_error"
        record["error"] = str(e)
    except Exception as e:  # noqa: BLE001 — a trial must never take the sweep down
        record["status"] = "harness_error"
        record["error"] = f"{type(e).__name__}: {e}"
    finally:
        record["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        if record.get("checks"):
            record["checks"].update(l0_components(record["checks"]))
            record["l0_score"] = l0_score(record["checks"])
        log.close()
        save()
        try:
            store = Store(args.db)
            store.save(record)
            store.close()
        except Exception as e:  # noqa: BLE001 — a store failure must not lose the trial
            record["store_error"] = f"{type(e).__name__}: {e}"
            save()
    return record


OVERLAY = "/opt/pegasus-ai/overlay/STUDIO_ENV.md"


def overlay_sha() -> str:
    """Hash of the Studio environment overlay the model was actually given.

    `knowledge_sha` covers the *marketplace* checkout only. The overlay ships in
    the studio-webapp **image**, so a change to it — studio-webapp #322 renamed
    the workflow directory convention in exactly this file — moves the knowledge
    every model sees while leaving no trace in the trial row. That invisibility
    is not hypothetical: the overlay silently reverting on a container recreate
    is ceiling #2, and it cost a day of results that read as a model regression.
    """
    try:
        with open(OVERLAY, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return "absent"


def knowledge_sha() -> str:
    """Which knowledge base produced this result — without it, 'did that skill
    edit help?' is unanswerable."""
    repo = os.path.expanduser("~/.local/share/pegasus-ai/marketplace")
    try:
        return subprocess.run(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", required=True)
    ap.add_argument("--provider", default=None, help="provider id, e.g. custom-mtd4weae")
    ap.add_argument("--model", default=None)
    ap.add_argument("--depth", default="auto", choices=DEPTHS,
                    help="auto = whatever the task declares")
    ap.add_argument("--backend", default="builtin", choices=("builtin", "opencode"),
                    help="which chat engine drives the conversation")
    ap.add_argument("--session-dir", default=None,
                    help="opencode only: root the session here. The chat panel "
                         "passes the workflow's folder for a workflow-scoped "
                         "conversation and nothing for an unscoped one; the "
                         "tasks ask for a workflow that does not exist yet, so "
                         "the default is nothing, like a user starting fresh")
    ap.add_argument("--clean-workspace", default="off", choices=("off", "on"),
                    help="archive what previous trials left in the workspace "
                         "before this one runs; required for an honest "
                         "--examples off arm")
    ap.add_argument("--examples", default="off", choices=("off", "on", "block"),
                    help="copy the plugin's assets/ into ~/work so the skills' own "
                         "relative paths resolve (PLAN.md §8 item 0)")
    ap.add_argument("--approval", default="off", choices=("off", "auto"),
                    help="off = gate disabled (D2, stages 0-1); auto = gate on, harness approves")
    ap.add_argument("--site", default="condorpool")
    ap.add_argument("--output-site", default="local")
    ap.add_argument("--run-id", default=dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--trial-id", default=None)
    ap.add_argument("--repeat", type=int, default=0)
    ap.add_argument("--turn-timeout", type=float, default=-1.0,
                    help="wall-clock ceiling per assistant turn in seconds; "
                         "-1 uses STUDIO_TURN_TIMEOUT (default 2400), 0 disables")
    ap.add_argument("--db", default=None)
    ap.add_argument("--studio-url", default=os.environ.get("STUDIO_URL", "http://localhost:8888"))
    args = ap.parse_args()
    if args.db is None:
        from harness.store import DEFAULT_PATH
        args.db = DEFAULT_PATH

    record = run_trial(args)
    c = record.get("checks", {})
    print(json.dumps({
        "trial_id": record["trial_id"],
        "status": record["status"],
        "error": record.get("error"),
        "turns": record.get("turns"),
        "tool_calls": c.get("tool_calls"),
        "protocol_ok": c.get("protocol_ok"),
        "wrote_files": c.get("wrote_files"),
        "discoverable": c.get("discoverable"),
        "named_as_asked": c.get("project_name_matched"),
        "deliverables_ok": c.get("deliverables_ok"),
        "files_missing": c.get("files_missing"),
        "generator_compiles": c.get("generator_compiles"),
        "generator_runs": c.get("generator_runs"),
        "generate_ok": c.get("generate_ok"),
        "plan_ok": c.get("plan_ok"),
        "l0_score": record.get("l0_score"),
        "wall_s": record.get("wall_s"),
        "tokens": record.get("tokens"),
    }, indent=2))
    sys.exit(0 if record["status"] == "done" else 1)


if __name__ == "__main__":
    main()
