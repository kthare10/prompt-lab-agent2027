"""L0 — deterministic checks over a finished trial.

Free, and authoritative: where L0 and a judge disagree, L0 wins. Nothing here
asks a model anything, so it can run on every trial without a budget.

Must run inside the Studio container. `generator_runs` exercises the real
`from Pegasus.api import *` import path, which does not exist on the host — a
host-side run would fail every trial for a reason unrelated to the model (D3).
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
import re
import subprocess
import sys
from typing import Any

# Server-side caps in the **builtin** loop (studio-api/routers/chat.py). A trial
# that hits one there is reporting a harness-visible truncation, not a model
# verdict. OpenCode has neither cap, so the checks derived from these are None on
# that engine rather than False — see where they are set.
MAX_TOOL_ROUNDS = 50
MAX_HISTORY_MESSAGES = 40

# The say/rather-than table from the Studio environment overlay. The product
# goal is prose a domain scientist can act on, so jargon used *without* its
# plain-language gloss is a defect worth counting — cheaply, here, rather than
# spending a judge call on it.
VOCABULARY = {
    "job": "step",
    "DAG": "pipeline diagram",
    "held": "stuck",
    "idle": "waiting",
    "submitted": "starting",
    "condorpool": "where it runs",
    "slots": "computing capacity",
}

WRITE_TOOLS = {"write_file", "create_directory"}

# The chat engine decides the tool vocabulary, and the vocabulary decides what
# "the model wrote a file" means. Studio's builtin loop exposes five tools of
# its own; OpenCode brings its own set, confirmed against the binary the image
# ships (1.18.29). Grading an OpenCode transcript against the builtin names
# scores every model as having written nothing and called five unknown tools —
# a protocol failure that is entirely the grader's.
ENGINE_TOOLS: dict[str, dict[str, Any]] = {
    "builtin": {
        "write": {"write_file", "create_directory"},
        "known": {"write_file", "read_file", "create_directory",
                  "run_command", "list_directory"},
        # `_safe_path` takes the destination as `path`.
        "path_args": ("path",),
    },
    "opencode": {
        "write": {"write", "edit", "patch"},
        "known": {"bash", "edit", "write", "read", "grep", "glob", "list",
                  "patch", "webfetch", "todowrite", "todoread", "task", "skill"},
        # Tolerant rather than guessed: the argument name is read off the first
        # real transcript, and until one exists every plausible spelling is
        # accepted. A wrong single guess here would report "wrote nothing".
        "path_args": ("filePath", "path", "file_path", "filename"),
        # Built-ins only. The real set is open — `skill`, and a tool per MCP
        # server (Studio configures three) — so a call is judged unknown only
        # when the engine never dispatched it. `harness/opencode.py` marks the
        # ones that reached a completed state.
        "open_set": True,
    },
}


def engine_tools(backend: str) -> dict[str, Any]:
    return ENGINE_TOOLS.get(backend or "builtin", ENGINE_TOOLS["builtin"])


def write_paths(tool_calls: list[dict], backend: str = "builtin") -> list[str]:
    """The destinations this transcript's write-shaped calls named."""
    spec = engine_tools(backend)
    out = []
    for call in tool_calls:
        if (call.get("name") or "") not in spec["write"]:
            continue
        args = call.get("arguments") or {}
        for key in spec["path_args"]:
            if args.get(key):
                out.append(str(args[key]))
                break
        else:
            out.append("")
    return out

# Every tool Studio actually exposes (TOOL_DEFINITIONS in studio-api's
# routers/chat.py). A call naming anything else cannot be dispatched, so it is a
# protocol failure rather than a tool the model "used".
KNOWN_TOOLS = {"write_file", "read_file", "create_directory", "run_command", "list_directory"}

# Directories that hold things the *toolchain* produced, not things the model
# authored: Pegasus submit and run directories, staged output, and the `?`
# directory a model creates when it passes `~/work/...` to a tool that does not
# expand `~`. They are excluded from every walk over a project.
#
# They are not inert. A t01 project snapshot was found to contain a `replicas.yml`
# from a *t00* run — `createdOn: 09-01-26`, registering `hello-pegasus/bin/
# count_numbers.py` — so a walk that includes them grades one trial partly on
# another task's leftovers. `has_container_def` and `uses_bad_fakeroot` both walk
# the project, and a stale `.def` under a submit directory is enough to flip them.
PRODUCT_DIRS = re.compile(
    r"^(\?|submit|submit-run|scratch|output|outputs|work|\.pegasus|run\d+|__pycache__|\.git)$"
)


def authored_files(project_dir: str):
    """Every file the model wrote, skipping toolchain products."""
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [d for d in dirnames if not PRODUCT_DIRS.match(d)]
        for name in filenames:
            yield os.path.join(dirpath, name)

# Required arguments, copied from TOOL_DEFINITIONS in studio-api/routers/chat.py.
# A call that omits one is a protocol failure the server reports as
# "Error executing <tool>: '<arg>'" — it is not a tool the model *used*, and
# counting it as one is how a collapsed turn scored as a clean protocol.
REQUIRED_ARGS = {
    "write_file": ("path", "content"),
    "read_file": ("path",),
    "create_directory": ("path",),
    "run_command": ("command",),
}


# Mirror of studio-api's _safe_path: a relative path is joined onto the
# workspace root, and expanduser runs *after* the join. Getting this wrong in
# either direction produces false path-compliance results, so it is copied
# rather than approximated.
SERVER_WORKSPACE = os.environ.get(
    "STUDIO_WORKSPACE_DIR", os.path.join(os.path.expanduser("~"), "work")
)


def _resolve(path: str, root: str | None = None) -> str:
    """Where the *engine* actually wrote, given what the model asked for.

    `root` is what a relative path is joined onto, and it is not a constant any
    more. Studio's builtin `_safe_path` joins onto ~/work; OpenCode joins onto
    the directory its **session** was created in, which is $HOME unless the
    caller passed `?directory=`. Resolving one engine's writes with the other's
    root is not a small error: on the first OpenCode trial it reported
    `discoverable: true` and no stray writes, when four of the model's six files
    were sitting in $HOME/workflows and only two were where the grader thought
    they all were.
    """
    base = root or SERVER_WORKSPACE
    if not path:
        return base
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.realpath(os.path.expanduser(path))


# The two pieces of live guidance disagree about what the project directory is
# called. `pegasus-scaffold/SKILL.md` says `{pipeline-name}-workflow/` and every
# reference repo it lists is named that way; the environment overlay says the
# name the user asked for. A model that follows either one has followed its
# instructions, so grading accepts both and records which it took.
#
# Grading a single guessed name is how five t01 cells scored 0.271 for a
# complete deliverable set: the files were in `<project>-workflow`, the grader
# looked in `<project>`, and every on-disk check failed for work that was done.
PROJECT_SUFFIXES = ("", "-workflow")


def project_names(project_id: str) -> list[str]:
    """Directory names that count as *this* project, in preference order."""
    return [project_id + suffix for suffix in PROJECT_SUFFIXES]


# Directories the environment ships or shares, which a model may legitimately
# write into without them being "the project".
SHARED_DIRS = {"assets", "references", "pegasus-ai", "opt", "tmp", "bin", "workflows"}


def resolve_project(
    project_id: str,
    turns: list[dict] | None = None,
    workspace: str | None = None,
    backend: str = "builtin",
    write_root: str | None = None,
) -> str:
    """Which directory name this trial's project actually landed in.

    The transcript is the record of intent, so where the model *wrote* wins over
    what happens to be on disk — a stray directory left by an earlier trial must
    not capture this one. Falls back to the task's own name so a trial that wrote
    nothing still grades against a stable path.
    """
    root = os.path.realpath(workspace or os.path.join(SERVER_WORKSPACE, "workflows"))
    names = project_names(project_id)

    written: Counter[str] = Counter()
    for turn in turns or []:
        # Engine-aware on both counts: which calls are writes, and what a
        # relative one is joined onto. Filtering on the builtin's names saw no
        # writes at all in an OpenCode transcript, so this fell back to the
        # task's own project name and graded an empty directory while the
        # model's work sat beside it under `<name>-workflow`.
        for path in write_paths(turn.get("tool_calls") or [], backend):
            target = _resolve(path, write_root)
            rel = os.path.relpath(target, root)
            if not rel.startswith(".." + os.sep) and rel != "..":
                written[rel.split(os.sep)[0]] += 1

    for name in names:
        if name in written:
            return name
    if written:
        # It wrote, just not under a name we predicted. Ceiling #5 taught that a
        # project has two names; t06 taught that it can have any name. Its
        # project id is `wrapper-only`, which reads as a description of the task
        # rather than a name, and not one of sixteen models used it — they named
        # it for the science (`qmap-workflow`, `qmap-analysis-workflow`). The
        # old code fell through to `project_id`, so nothing was found, no
        # snapshot was taken, and all eighteen trials reached the judge with an
        # empty ARTIFACTS/ and were scored as having written nothing.
        #
        # Only directories the model created are considered, and the busiest
        # wins: `assets/` and `references/` are shipped by the environment and a
        # model reading them must not redirect grading onto them.
        candidates = {
            name: n for name, n in written.items()
            if name not in SHARED_DIRS and not name.startswith(".")
        }
        if candidates:
            return max(candidates, key=lambda n: (candidates[n], n))
    if turns is not None:
        # The trial ran and wrote nothing. Falling through to whatever is on
        # disk would hand it a directory some *other* trial left behind and
        # score it on those files, so a trial that wrote nothing is graded
        # against the name it was asked for, which reset_project has cleared.
        return project_id
    for name in names:
        if os.path.isdir(os.path.join(root, name)):
            return name
    return project_id


def _workflow_yaml(project_dir: str) -> str | None:
    """The abstract workflow the generator produced, wherever it put it.

    `workflow.yml` is the template's default, but a generator is free to name it
    something else via `--output`, so fall back to any authored YAML that has the
    two keys a Pegasus workflow always has.
    """
    default = os.path.join(project_dir, "workflow.yml")
    if os.path.isfile(default):
        return default
    for path in sorted(authored_files(project_dir)):
        if not path.endswith((".yml", ".yaml")):
            continue
        try:
            head = open(path, errors="replace").read(4000)
        except OSError:
            continue
        if "jobDependencies" in head and "\njobs:" in head:
            return path
    return None


def _jobs_expected(spec: Any, count: int) -> bool | None:
    """Adjudicate an `expect.jobs` value: `12`, `">=10"`, `"<=3"`."""
    if spec is None:
        return None
    text = str(spec).strip()
    for op, test in ((">=", lambda a, b: a >= b), ("<=", lambda a, b: a <= b),
                     (">", lambda a, b: a > b), ("<", lambda a, b: a < b)):
        if text.startswith(op):
            try:
                return test(count, int(text[len(op):]))
            except ValueError:
                return None
    try:
        return count == int(text)
    except ValueError:
        return None


# Studio's own rule for locating a project's generator, mirrored so the grader
# and the server agree on what counts as one. `workflow_scanner.py` accepts the
# underscore form, the hyphenated form that quickstart-workflow ships, and any
# bare filename declared in `.pegasushub.yml` under `scripts.generator`.
GENERATOR_NAMES = ("workflow_generator.py", "workflow-generator.py")

# A phased design splits generation across several scripts — typically
# `workflow_generator_phase1.py` / `_phase2.py`, where phase 2 is generated from
# phase 1's output. t13 asks for exactly this (its `must` rewards "a sub-workflow
# planned after demultiplexing, or an equivalent deferred-planning approach"),
# so a model that produces one is answering the question, not missing a file.
_PHASED = re.compile(r"^workflow[_-]generator[_-].+\.py$", re.IGNORECASE)


def _declared_generator(project_dir: str) -> str | None:
    """Generator filename declared in `.pegasushub.yml` (`scripts.generator`)."""
    marker = os.path.join(project_dir, ".pegasushub.yml")
    if not os.path.isfile(marker):
        return None
    try:
        import yaml
        data = yaml.safe_load(open(marker, errors="replace")) or {}
        name = ((data.get("scripts") or {}).get("generator") or "").strip()
    except Exception:
        return None
    # Bare filename only: no traversal outside the project, same as Studio.
    if name and name == os.path.basename(name) and os.path.isfile(os.path.join(project_dir, name)):
        return name
    return None


def generator_files(project_dir: str) -> tuple[list[str], str | None]:
    """Resolve a project's generator scripts.

    Returns (absolute paths, style) where style is one of `canonical`,
    `hyphenated`, `declared`, `phased`, or None when nothing was found.
    A single canonical file always wins; a phased set is only consulted when no
    single entry point exists, so a project with both is judged on the one
    Studio would actually run.
    """
    if not os.path.isdir(project_dir):
        return [], None
    for name, style in zip(GENERATOR_NAMES, ("canonical", "hyphenated")):
        path = os.path.join(project_dir, name)
        if os.path.isfile(path):
            return [path], style
    declared = _declared_generator(project_dir)
    if declared:
        return [os.path.join(project_dir, declared)], "declared"
    phased = sorted(
        os.path.join(project_dir, f)
        for f in os.listdir(project_dir)
        if _PHASED.match(f) and os.path.isfile(os.path.join(project_dir, f))
    )
    return (phased, "phased") if len(phased) >= 2 else ([], None)


def protocol_checks(tool_calls: list[dict], protocol_ok: bool,
                    backend: str = "builtin") -> dict[str, Any]:
    """The tool-protocol verdict, computed once for every caller.

    `regrade.py` used to carry its own copy of these. The two drifted — the copy
    never learned about unknown tool names — so a regrade handed
    `protocol_clean: True` to trials that had called tools the server does not
    have, silently raising their L0. That is the second time a duplicated check
    in that file has skewed a run; this exists so there is no copy to drift.
    """
    malformed = [
        f"{call.get('name')}:{arg}"
        for call in tool_calls
        for arg in REQUIRED_ARGS.get(call.get("name") or "", ())
        if (call.get("arguments") or {}).get(arg) is None
    ]
    # A call naming a tool the server does not have. Studio cannot dispatch it,
    # so the work it describes never happens — and until this check existed, the
    # call was invisible: not in REQUIRED_ARGS, so never checked for arguments;
    # not in WRITE_TOOLS, so never counted as a write. `gpt-oss-20b` emitted
    # `write_file<|channel|>commentary` — a harmony marker leaking into the name
    # — for 2 of its 6 calls on one trial, and L0 recorded
    # `malformed_tool_calls: 0, protocol_clean: True, tool_errors: 0`. A third of
    # its calls were dropped and the protocol scored perfect.
    spec = engine_tools(backend)

    def is_unknown(call: dict) -> bool:
        if (call.get("name") or "") in spec["known"]:
            return False
        # An open vocabulary is only as good as the evidence: a name the engine
        # actually ran is a tool, whatever this file happens to list.
        return not (spec.get("open_set") and call.get("dispatched"))

    unknown = sorted({call.get("name") or "(unnamed)"
                      for call in tool_calls if is_unknown(call)})
    return {
        "malformed_tool_calls": len(malformed),
        "malformed_tools": sorted(set(malformed)),
        "unknown_tool_calls": sum(1 for call in tool_calls if is_unknown(call)),
        "unknown_tools": unknown[:6],
        # Emitting native calls is necessary but not sufficient — the other forms
        # of the open-weight tool-protocol failure are a well-formed call frame
        # whose arguments did not survive whatever produced them, and a call
        # addressed to a tool that does not exist.
        "protocol_clean": protocol_ok and not malformed and not unknown,
    }


def dag_checks(task: dict, project_dir: str) -> dict[str, Any]:
    """Structure of the generated workflow, measured rather than described.

    `expect.jobs` and `expect.dag` have been in the task specs since the suite
    was written and were read by *nothing*: a flat fan-in over 2,000 tiles scored
    identically to a proper merge tree, which is the entire content of t02.

    What is returned is a set of *measurements* — job count, in/out degree, depth
    — plus `dag_ok` only for the shapes that can be stated crisply. A shape this
    cannot decide returns `None` and is excluded from scoring rather than
    guessed at; the measurements are still recorded, so a judge or a human can
    settle it. Naming a shape the code cannot actually test would be the same
    mistake as the path check that asserted its own success.
    """
    expect = task.get("expect") or {}
    want_shape = expect.get("dag")
    c: dict[str, Any] = {"dag_shape_expected": want_shape}
    if expect.get("jobs") is None and want_shape is None:
        return {}

    path = _workflow_yaml(project_dir)
    if not path:
        c["dag_parsed"] = False
        # Nothing generated is already counted by `generate_ok`; scoring it twice
        # would punish the same failure in two places.
        c["jobs_ok"] = None if expect.get("jobs") is None else False
        c["dag_ok"] = None if want_shape is None else False
        return c

    try:
        import yaml
        doc = yaml.safe_load(open(path, errors="replace")) or {}
    except Exception as e:  # noqa: BLE001
        c["dag_parsed"] = False
        c["dag_parse_error"] = f"{type(e).__name__}: {e}"
        return c

    jobs = doc.get("jobs") or []
    ids = [j.get("id") for j in jobs if isinstance(j, dict)]
    parents: dict[str, set] = {i: set() for i in ids}
    children: dict[str, set] = {i: set() for i in ids}
    for entry in doc.get("jobDependencies") or []:
        pid = entry.get("id")
        for kid in entry.get("children") or []:
            children.setdefault(pid, set()).add(kid)
            parents.setdefault(kid, set()).add(pid)

    c["dag_parsed"] = True
    c["dag_jobs"] = len(jobs)
    c["dag_max_in_degree"] = max((len(v) for v in parents.values()), default=0)
    c["dag_max_out_degree"] = max((len(v) for v in children.values()), default=0)
    sinks = [i for i in ids if not children.get(i)]
    sources = [i for i in ids if not parents.get(i)]
    c["dag_sinks"] = len(sinks)
    c["dag_sources"] = len(sources)
    c["dag_merge_jobs"] = sum(1 for i in ids if len(parents.get(i, ())) > 1)

    # Longest path in levels. The graph is a DAG by construction, but a
    # generator can emit a cycle; bound the walk so a bad workflow cannot hang
    # the grader.
    depth: dict[str, int] = {}

    def level(node: str, seen: frozenset = frozenset()) -> int:
        if node in depth:
            return depth[node]
        if node in seen or len(seen) > len(ids):
            return 0                       # cycle: stop rather than recurse
        d = 1 + max((level(p, seen | {node}) for p in parents.get(node, ())), default=0)
        depth[node] = d
        return d

    c["dag_depth"] = max((level(i) for i in ids), default=0)
    c["jobs_ok"] = _jobs_expected(expect.get("jobs"), len(jobs))

    producers = len(jobs) - c["dag_merge_jobs"]
    if want_shape == "single":
        c["dag_ok"] = len(jobs) == 1
    elif want_shape == "fan_in":
        # One terminal job that consumes every producer, and nothing between.
        c["dag_ok"] = (c["dag_sinks"] == 1 and c["dag_merge_jobs"] == 1
                       and c["dag_max_in_degree"] == producers and c["dag_depth"] == 2)
    elif want_shape == "merge_tree":
        # Staged: more than one merge, deeper than a flat fan-in, and no single
        # job swallowing every producer — which is exactly what t02 forbids.
        c["dag_ok"] = (c["dag_merge_jobs"] > 1 and c["dag_depth"] >= 3
                       and c["dag_max_in_degree"] < producers)
    else:
        c["dag_ok"] = None                 # not decidable here; left to L1
    return c


def grade(task: dict, project_dir: str, turns: list[dict],
          backend: str = "builtin", write_root: str | None = None,
          scan_root: str | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {}
    expect = task.get("expect") or {}
    prose = "\n".join(t.get("text", "") for t in turns)
    tool_calls = [call for t in turns for call in t.get("tool_calls", [])]

    # ── protocol ──────────────────────────────────────────────────────────
    c["tool_calls"] = len(tool_calls)
    c["turns"] = len(turns)
    # The dominant open-weight failure on OpenAI-compatible endpoints: tool
    # calls emitted as prose instead of as native calls.
    c["protocol_ok"] = len(tool_calls) > 0

    c.update(protocol_checks(tool_calls, bool(c["protocol_ok"]), backend))

    results = [r for t in turns for r in t.get("tool_results", [])]
    c["tool_errors"] = sum(
        1 for r in results if str(r.get("result", "")).startswith("Error executing")
    )
    # Silence is not failure. Some models do the work and say nothing — an
    # earlier version of this check called that a collapse and flagged three
    # successful trials out of three. A collapse is silence *plus* evidence the
    # work did not land: a tool error, or nothing written.
    c["silent_turn"] = bool(tool_calls) and not prose.strip()
    # A tool-call object emitted as a chat message — a real observed failure the
    # environment overlay warns about, which is why t10 exists.
    c["tool_call_leaked_into_chat"] = bool(
        re.search(r"\[use=\w+\]|\{\s*\"questions\"\s*:", prose)
    )
    # Both caps belong to the **builtin** loop: `_MAX_TOOL_ROUNDS` and
    # `_MAX_HISTORY_MESSAGES` are constants in `routers/chat.py`, which is no
    # longer the code path a user drives. OpenCode has neither, so comparing a
    # tool-call count against 50 there does not measure a ceiling the harness or
    # the engine imposed — it measures a model that worked hard. It fired on 95
    # of 511 OpenCode trials and is reported beside real failure signals.
    #
    # None, not False: the check did not pass, it did not apply. Same reasoning
    # as `mask_inapplicable`, and the same reason C9 had to be withdrawn — a
    # constraint read out of the builtin engine does not automatically hold on
    # the one that replaced it.
    capped = (backend or "builtin") == "builtin"
    c["rounds_exhausted"] = len(tool_calls) >= MAX_TOOL_ROUNDS if capped else None
    c["history_trimmed"] = len(turns) * 2 > MAX_HISTORY_MESSAGES if capped else None

    # ── files on disk, in the one directory that counts (C6) ──────────────
    writes = write_paths(tool_calls, backend)
    want = os.path.realpath(project_dir)
    # What the Workflows tab lists. Studio moved this from $HOME/work/workflows
    # to $HOME/workflows (#402), so it is a property of the trial, not of this
    # module: a Stage 1 row regraded against the new root would report that
    # every model wrote nothing.
    scanned = os.path.realpath(scan_root or os.path.join(SERVER_WORKSPACE, "workflows"))
    resolved = [(w, _resolve(w, write_root)) for w in writes]

    # Two different failures, with very different consequences. Studio's
    # Workflows tab lists the immediate children of ~/work/workflows and
    # nothing else, so a write outside that root is *invisible* — no Generate,
    # Plan or Run button, however good the code is. A write inside it under a
    # different name than the user asked for is merely a naming deviation: the
    # project still works.
    invisible = [w for w, r in resolved if not r.startswith(scanned + os.sep)]
    # "Did *the model's* work land on disk" — not "is this directory non-empty".
    # A task that seeds a fixture (t08 ships a whole failed run directory) makes
    # the project non-empty before the model does anything, and the old test
    # credited a model that made zero tool calls with having written the
    # deliverables. Anchoring on the paths the model actually wrote cannot be
    # fooled by anything the harness put there.
    c["wrote_files"] = any(os.path.exists(r) for _, r in resolved)
    c["write_calls"] = len(writes)
    c["stray_writes"] = invisible
    c["stray_targets"] = sorted({r for w, r in resolved if w in invisible})[:6]
    c["discoverable"] = bool(writes) and not invisible
    c["path_compliance"] = c["discoverable"]

    # `startswith` on a bare prefix is not a path test: it matched
    # `sensor-summary-workflow/...` against a `sensor-summary` project and
    # reported `project_name_matched` on the exact trials whose files the grader
    # had failed to find. The component boundary is what makes it a path.
    named = [w for w, r in resolved if r == want or r.startswith(want + os.sep)]
    c["project_name_matched"] = bool(named)
    # Computed whether or not the project matched: a trial can write the project
    # correctly *and* scatter files into sibling directories, and only reporting
    # drift on total failure hides that.
    c["project_name_drift"] = sorted({
        r[len(scanned) + 1:].split(os.sep)[0]
        for w, r in resolved if r.startswith(scanned + os.sep)
    } - {os.path.basename(want)})
    # Which of the two conventions this trial took. Not a defect — the skill
    # asks for the suffix — but the disagreement stays visible in the record.
    c["project_name_suffixed"] = (
        os.path.basename(want) != (task.get("project") or "") if writes else None
    )

    # Three different failures that a single flag would blur together:
    #   nothing written at all          → the "code in chat, empty directory" case
    #   written, but somewhere useless  → invisible to the Workflows tab (C6)
    #   written to the right place      → fine
    c["narrated_not_written"] = not writes and len(prose) > 800
    c["wrote_elsewhere"] = bool(invisible) and not c["wrote_files"]
    c["wrote_partly_elsewhere"] = bool(invisible) and c["wrote_files"]

    present, missing = [], []
    # A generator deliverable is satisfied by whatever Studio would accept as
    # the generator, not by one hardcoded filename. t13 asks for a design whose
    # correct answer splits generation across phases, then lists
    # `workflow_generator.py` among its expected files; taken literally that
    # marks the intended answer as a missing deliverable.
    _gens, _style = generator_files(project_dir)
    for want in expect.get("files", []):
        target = want.rstrip("/")
        if "workflow_generator" in target and _gens:
            present.append(want)
            continue
        (present if os.path.exists(os.path.join(project_dir, target)) else missing).append(want)
    c["files_present"], c["files_missing"] = present, missing
    # None, not True, when the task asks for no files: t08 is a diagnosis task
    # and expects nothing on disk, so "all deliverables present" is vacuous and
    # was handing every model a free point on a check it never exercised.
    c["deliverables_ok"] = None if not expect.get("files") else not missing

    # ── the generator ─────────────────────────────────────────────────────
    gens, style = _gens, _style
    c["generator_style"] = style
    c["generator_paths"] = [os.path.basename(g) for g in gens]
    src = ""
    if gens:
        srcs = []
        errors = []
        for g in gens:
            text = open(g, errors="replace").read()
            srcs.append(text)
            try:
                compile(text, g, "exec")
            except SyntaxError as e:
                errors.append(f"{os.path.basename(g)}: {e.msg} at line {e.lineno}")
        c["generator_compiles"] = not errors
        if errors:
            c["generator_compile_error"] = "; ".join(errors)
        # Pattern-matching below reads the whole set, so a phased design gets
        # credit for the catalogs it builds wherever it builds them.
        src = "\n".join(srcs)
        if style == "phased":
            # A phased generator cannot be exercised in isolation: phase 2 reads
            # a manifest that only exists once phase 1's *workflow* has run, so
            # invoking it here would fail for a reason that says nothing about
            # the model. Unmeasured, not failed — `l0_score` drops None terms.
            c["generator_runs"] = None
            c["generator_run_error"] = (
                "phased generator: not runnable standalone, so not measured"
            )
        else:
            c["generator_runs"], c["generator_run_error"] = _runs(gens[0], project_dir)
        # Catalogs: which of the five components the generator actually builds.
        for name, pattern in (
            ("replica_catalog", r"ReplicaCatalog\("),
            ("transformation_catalog", r"TransformationCatalog\("),
            ("site_catalog", r"SiteCatalog\("),
            ("properties", r"Properties\("),
        ):
            c[f"has_{name}"] = bool(re.search(pattern, src))
        c["job_count_literal"] = len(re.findall(r"\bJob\(", src))
    else:
        # Absent generator: False when the task asked for one, None when it did not.
        expected = any("workflow_generator" in f for f in expect.get("files", []))
        for k in ("generator_compiles", "generator_runs"):
            c[k] = False if expected else None

    # ── container definition ──────────────────────────────────────────────
    defs = [
        f for f in authored_files(project_dir) if f.endswith(".def")
    ] if os.path.isdir(project_dir) else []
    c["has_container_def"] = bool(defs)
    if defs:
        text = "\n".join(open(d, errors="replace").read() for d in defs)
        base = re.search(r"^From:\s*(\S+)", text, re.M | re.I)
        c["container_base"] = base.group(1) if base else None
        # This host is Ubuntu 24.04 / glibc 2.39; an older base fails before a
        # single %post line runs, with an error pointing nowhere near the cause.
        c["base_image_ok"] = bool(base) and not re.search(r"22\.04|20\.04|18\.04", base.group(1))
    # --fakeroot produces an empty container while reporting success.
    all_text = prose + "\n" + src + "\n".join(
        open(d, errors="replace").read() for d in defs
    )
    c["uses_bad_fakeroot"] = bool(re.search(r"--fakeroot(?!\S)", all_text))

    # ── communication ─────────────────────────────────────────────────────
    hits = [
        jargon for jargon, plain in VOCABULARY.items()
        if re.search(rf"\b{re.escape(jargon)}\b", prose) and plain.lower() not in prose.lower()
    ]
    c["unglossed_jargon"] = hits
    c["vocabulary_score"] = round(1 - len(hits) / len(VOCABULARY), 2)

    c["turn_collapsed"] = bool(c["silent_turn"]) and (
        bool(c.get("tool_errors")) or not c.get("wrote_files")
    )

    # ── C10: is this repeat an independent sample, or the provider's cache? ─
    c["output_hash"] = hashlib.sha256(prose.encode()).hexdigest()[:16]
    return mask_inapplicable(task, c)


def _runs(gen: str, cwd: str) -> tuple[bool, str | None]:
    """Does the generator actually execute? `--help` is enough to exercise the
    imports, which is where weak models fail after writing valid syntax."""
    try:
        p = subprocess.run(
            [sys.executable, gen, "--help"], cwd=cwd,
            capture_output=True, text=True, timeout=120,
        )
        return p.returncode == 0, None if p.returncode == 0 else (p.stderr or p.stdout)[-600:]
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# Weights are the composite from PLAN.md §5, minus the judged components,
# which arrive with L1 in M4. Reported as `l0_score` so it is never mistaken
# for the full Usability figure.
# Checks that only mean something when the task asked for an artifact. On a
# `depth: none` task the model is asked for a review, a diagnosis or a question
# — there is nothing to write, nothing to generate, nothing to plan — and
# scoring it against these asks whether it did work it was told not to do.
#
# Measured, not assumed: on t10 the Spearman rho between a trial's `write_calls`
# and its L0 is +0.663 (p = 0.007), against +0.057 for the same writes and the
# judge's score. L0 there was very nearly a measurement of whether the model
# created files — which is exactly what t10's own `must_not` forbids. `kimi`
# scores 0.100 for asking correctly and 1.000 for scaffolding early on the same
# task. It did not merely miss elicitation; it rewarded the failure.
#
# t07 was worse still. Its fixture seeds a workflow_generator.py, so
# `generator_files` found one, compiled it and ran it, and sixteen models with
# zero write calls were each credited with a generator they had not written.
# `wrote_files` already anchors on the paths the model actually wrote "so it
# cannot be fooled by anything the harness put there"; the generator checks did
# not, and the fixture fooled them.
ARTIFACT_CHECKS = (
    "wrote_files", "discoverable", "path_compliance", "deliverables_ok",
    "generator_compiles", "generator_runs", "generate_ok", "build_ok", "plan_ok",
)


def asks_for_artifact(task: dict) -> bool:
    """Does this task ask the model to leave something on disk?"""
    return ((task.get("depth") or "none") != "none"
            or bool((task.get("expect") or {}).get("files")))


def mask_inapplicable(task: dict, c: dict[str, Any]) -> dict[str, Any]:
    """Set to None the checks this task never exercised.

    `l0_score` drops None terms, so a check that does not apply stops being a
    zero the model is charged for. The precedent is `deliverables_ok`, which was
    already None when a task asks for no files — this is the same argument
    carried to the rest of the artifact chain.

    Lives here, and is called by both `grade` and `regrade`, because the two
    have drifted apart twice before by each keeping its own copy of a rule.
    """
    c["l0_applicable"] = asks_for_artifact(task)
    if not c["l0_applicable"]:
        for key in ARTIFACT_CHECKS:
            if key in c:
                c[key] = None
    # Nothing was called, so there is no protocol to be clean or unclean about.
    # A build task whose model called nothing still fails on every artifact
    # check, so this changes no score there; on a task that asks for a
    # conversation it is the difference between "said nothing in the right
    # format" and "was never asked to say anything in a format at all".
    if not c.get("tool_calls"):
        c["protocol_clean"] = None
    return c


def l0_score(c: dict[str, Any]) -> float | None:
    """Named components, not one flat average over every check.

    Averaging all eleven checks equally made `dag_ok` worth 1/11th of the score
    on t02 — a task whose *entire* content is the shape of the job graph. The
    result: `gemma` topped t02's L0 at 0.807 with a **two-job workflow and no
    merge job at all**, because it did everything else tidily, while `kimi`'s
    genuine 67-job merge tree scored lower. A weighting that lets a model win a
    fan-out task without fanning out is measuring the wrong thing.

    So shape is its own component, and it only exists for the tasks that declare
    one. Tasks that declare no DAG (t03, t08) keep exactly the old formula, so
    their existing numbers stay comparable.

    Within shape, `dag_ok` outweighs `jobs_ok`: job count is weak evidence, and
    a flat fan-in over 2,000 tiles satisfies `jobs >= 10` while being precisely
    the thing t02's `must_not` forbids.
    """
    executability = [
        c.get("protocol_clean"), c.get("wrote_files"), c.get("path_compliance"),
        c.get("deliverables_ok"), c.get("generator_compiles"), c.get("generator_runs"),
        c.get("generate_ok"), c.get("build_ok"), c.get("plan_ok"),
    ]
    scored = [bool(x) for x in executability if x is not None]
    shape_parts = [(c.get("dag_ok"), 0.7), (c.get("jobs_ok"), 0.3)]
    applicable = [(v, w) for v, w in shape_parts if v is not None]
    vocab = float(c.get("vocabulary_score") or 0)

    # Nothing mechanical applied. Returning the vocabulary term alone puts a
    # number on the board that reads as a score and is not one; every consumer
    # already handles a null L0 (the board prints an em dash and keeps it out of
    # every mean).
    #
    # `l0_applicable` is False on a task that asks for no artifact. The one
    # component that survives the mask there is `protocol_clean`, and rolling
    # that into a score is not an improvement on the bug being fixed: it is
    # near-binary, so t07's L0 becomes 0.96 for a clean protocol and 0.09 for a
    # dirty one — spread 0.448 where it was 0.091 — and on the board that reads
    # as "this model reviewed the workflow five times better", when all it says
    # is that its tool calls were well-formed. The fact is kept as a check and
    # shown as one. It is not a score, so it is not reported as one.
    if c.get("l0_applicable") is False or (not scored and not applicable):
        return None
    exec_score = sum(scored) / len(scored) if scored else 0.0

    if not applicable:
        return round(0.9 * exec_score + 0.1 * vocab, 3)

    total_w = sum(w for _, w in applicable)
    shape_score = sum(w for v, w in applicable if v) / total_w
    return round(0.55 * exec_score + 0.35 * shape_score + 0.10 * vocab, 3)


def l0_components(c: dict[str, Any]) -> dict[str, float | None]:
    """The parts behind `l0_score`, so a single number cannot hide which half
    went wrong (PLAN.md §5: "report the components too")."""
    executability = [
        c.get("protocol_clean"), c.get("wrote_files"), c.get("path_compliance"),
        c.get("deliverables_ok"), c.get("generator_compiles"), c.get("generator_runs"),
        c.get("generate_ok"), c.get("build_ok"), c.get("plan_ok"),
    ]
    scored = [bool(x) for x in executability if x is not None]
    shape = [(c.get("dag_ok"), 0.7), (c.get("jobs_ok"), 0.3)]
    applicable = [(v, w) for v, w in shape if v is not None]
    return {
        "l0_executability": round(sum(scored) / len(scored), 3) if scored else None,
        "l0_shape": (round(sum(w for v, w in applicable if v)
                           / sum(w for _, w in applicable), 3) if applicable else None),
        "l0_vocabulary": float(c.get("vocabulary_score") or 0),
    }