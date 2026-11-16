"""L1 — rubric grading of a finished trial by an LLM judge (PLAN.md §5, §7).

    python3 -m harness.judge --run-id t01b-nrp

Runs on the **host**, not in the container: `codex` is host-only, while L0 must
run inside (D3). It needs nothing from Studio — only the trial directory and the
task spec — so it can grade a completed sweep while the next one is running.

Three things make the scores worth having, and each is enforced here rather than
requested politely:

* the judge sees the **artifacts, not the model's account of them**. It is given
  a read-only bundle and told to open the files; the assistant's prose is
  supplied separately and labelled as a claim to be checked, not as evidence.
* every score must **cite a verbatim quote and its source**, enforced by a JSON
  schema that makes `evidence` and `source` required.
* the judge model is **pinned and recorded per row**. A silent judge upgrade
  would invalidate every comparison against earlier runs, so `--judge-model`
  is written into the judgement and re-grading with a different one adds a row
  rather than replacing one (which is also what D4's double-grading needs).

Where L0 and L1 disagree, **L0 wins** — L1 never touches `l0_score`.
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
import tempfile
from typing import Any

import yaml

from harness.graders.mechanical import PRODUCT_DIRS
from harness.reasoning import strip_reasoning
from harness.store import DEFAULT_PATH, RUNS_DIR, Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS = os.path.join(ROOT, "tasks")
RUNS = RUNS_DIR

DEFAULT_JUDGE = "gpt-5.6-sol"

# The dimensions from §5. Every trial is scored on all of them; the judge marks
# the ones the task does not exercise `applicable: false` rather than inventing a
# number, and those are excluded from the aggregate. Scoring an unexercised
# dimension as 3 would drag every score toward the middle.
DIMENSIONS = [
    ("dag_correctness", "Is the job graph the shape the request implies — fan-out, fan-in, merge tree — with dependencies that come from real File objects rather than a directory scan?"),
    ("catalog_correctness", "Are executables in the TransformationCatalog, data in the ReplicaCatalog, sites in the SiteCatalog? Executables registered as replicas is a defect."),
    ("wrapper_quality", "Do wrapper scripts take explicit file paths as arguments, handle their own errors, and avoid scanning directories to discover their inputs?"),
    ("container_definition", "Is the base image right for this host (ubuntu:24.04 or matching glibc), are build dependencies installed in %post, and is --fakeroot avoided?"),
    ("data_staging", "Are inputs and outputs declared so Pegasus can stage them, rather than assumed to be on a shared filesystem?"),
    ("diagnosis_quality", "When something failed, does the response name a cause *and* an action, and is the cause the real one?"),
    ("communication_quality", "Would a domain scientist who does not know Pegasus be able to act on this? Jargon used without a plain-language gloss counts against it."),
]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dimensions", "must", "must_not", "summary"],
    "properties": {
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "applicable", "score", "evidence", "source", "reason"],
                "properties": {
                    "name": {"type": "string", "enum": [n for n, _ in DIMENSIONS]},
                    "applicable": {"type": "boolean"},
                    "score": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
                    "evidence": {"type": "string"},
                    "source": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "must": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["criterion", "met", "evidence", "source"],
                "properties": {
                    "criterion": {"type": "string"},
                    "met": {"type": "boolean"},
                    "evidence": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
        },
        "must_not": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["criterion", "violated", "evidence", "source"],
                "properties": {
                    "criterion": {"type": "string"},
                    "violated": {"type": "boolean"},
                    "evidence": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}

PROMPT = """\
You are grading one attempt by a language model to build a Pegasus WMS workflow
through a chat interface. You are not helping it; you are assessing what it
actually produced.

Everything you need is in the current directory:

  TASK.md        what the user asked for, and the criteria this task is graded on
  ARTIFACTS/     the files the model wrote — this is the evidence
  TRANSCRIPT.md  what the model *said* it did. A claim, not evidence.
  MECHANICAL.md  deterministic checks already run, and the result of actually
                 executing the workflow. These are facts; do not contradict them.

**Read the files in ARTIFACTS/ before scoring.** A model's description of its own
work is frequently wrong in ways the files reveal — a merge job described as
hierarchical that declares every input on one job, a container "built from
ubuntu:24.04" whose definition says 22.04. Where TRANSCRIPT.md and ARTIFACTS/
disagree, the files are right.

Score each dimension 1-5:

  1  absent, or actively wrong in a way that cannot work
  2  attempted, but broken
  3  works for the example, would not survive the real input
  4  correct, with a rough edge
  5  correct and idiomatic

Rules:

* Every dimension needs `evidence`: a **verbatim** quote from a file or from the
  transcript, and `source`: the path it came from. Do not paraphrase into the
  quote field. If you cannot cite it, you cannot score it.
* If the task does not exercise a dimension, set `applicable` to false and
  `score` to null. Do not award a middling score to a dimension the task never
  asked about — say so instead.
* Judge what is there, not what is missing from your idea of a good workflow.
  The criteria in TASK.md are the standard.
* Adjudicate every `must` and `must_not` in TASK.md, in the order given, with
  its own quote.

Return only the JSON object the schema describes.
"""


def _read(path: str, cap: int = 60_000) -> str:
    try:
        text = open(path, errors="replace").read()
    except OSError as e:
        return f"({type(e).__name__}: {e})"
    return text if len(text) <= cap else text[:cap] + f"\n... [truncated at {cap} bytes]"


def task_md(task: dict) -> str:
    expect = task.get("expect") or {}
    out = [f"# {task.get('title') or task['id']}", "", "## What the user asked for", "",
           "```", task["prompt"].rstrip(), "```", ""]
    if expect.get("files"):
        out += ["## Deliverables the task expects", ""]
        out += [f"- `{f}`" for f in expect["files"]] + [""]
    for key, heading in (("must", "Must (each is a criterion to adjudicate)"),
                         ("must_not", "Must not (each is a criterion to adjudicate)")):
        if expect.get(key):
            out += [f"## {heading}", ""] + [f"- {c}" for c in expect[key]] + [""]
    if task.get("rubric"):
        out += ["## Questions this task is really asking", ""]
        out += [f"- {q}" for q in task["rubric"]] + [""]
    return "\n".join(out)


def transcript_md(trial_dir: str) -> tuple[str, list[dict]]:
    path = os.path.join(trial_dir, "transcript.jsonl")
    turns: list[dict] = []
    if not os.path.isfile(path):
        return "(no transcript)", turns
    parts = ["# What the model said", "",
             "*This is the model's own account. Check it against ARTIFACTS/.*", ""]
    for line in open(path):
        entry = json.loads(line)
        payload = entry["payload"]
        if entry["kind"] == "user":
            text = payload if isinstance(payload, str) else payload.get("text", "")
            parts += ["## User", "", str(text).strip(), ""]
        else:
            # Same repair as regrade: the judge reads this prose directly, so
            # a stray `</think>` would otherwise sit in the evidence it scores.
            payload["text"] = strip_reasoning(payload.get("text") or "")
            turns.append(payload)
            parts += ["## Assistant", "", payload["text"].strip(), ""]
            calls = payload.get("tool_calls") or []
            if calls:
                names = ", ".join(f"{c.get('name')}" for c in calls[:40])
                parts += [f"*{len(calls)} tool calls: {names}*", ""]
    return "\n".join(parts), turns


def mechanical_md(rec: dict) -> str:
    checks = Store._json_field(rec.get("checks")) or {}
    flow = Store._json_field(rec.get("flow")) or {}
    out = ["# Deterministic results", "",
           "These were measured, not inferred. Do not contradict them.", "",
           f"- status: `{rec.get('status')}`",
           f"- L0 score: `{rec.get('l0_score')}`", ""]
    out += ["## Checks", "", "```json", json.dumps(checks, indent=2, sort_keys=True)[:12_000], "```", ""]
    if flow:
        out += ["## Executing the workflow", ""]
        for stage in ("generate", "build", "plan"):
            entry = flow.get(stage)
            if isinstance(entry, dict):
                out += [f"### {stage}: {entry.get('status')}", ""]
                if entry.get("args") is not None:
                    out += [f"invoked with: `{entry.get('args')}`", ""]
                tail = (entry.get("output") or entry.get("error") or "")[-2500:]
                if tail:
                    out += ["```", tail, "```", ""]
        if flow.get("error"):
            out += [f"- {flow['error']}", ""]
    return "\n".join(out)


def bundle(rec: dict, task: dict, trial_dir: str, dest: str) -> str:
    """Assemble the read-only evidence directory and return its content hash."""
    os.makedirs(dest, exist_ok=True)
    tmd, _ = transcript_md(trial_dir)
    open(os.path.join(dest, "TASK.md"), "w").write(task_md(task))
    open(os.path.join(dest, "TRANSCRIPT.md"), "w").write(tmd)
    open(os.path.join(dest, "MECHANICAL.md"), "w").write(mechanical_md(rec))

    artifacts = os.path.join(dest, "ARTIFACTS")
    snapshot = os.path.join(trial_dir, "workspace")
    if os.path.isdir(snapshot):
        # Only what the model authored. Pegasus submit and run directories are
        # products, and they are not inert: one t01 snapshot carried a t00 run's
        # `replicas.yml` registering `hello-pegasus/bin/count_numbers.py`, and
        # the judge duly reported "executables registered in the replica
        # catalog" against a model that had done no such thing.
        # A .sif is also dropped — a hundred megabytes of squashfs that tells the
        # judge nothing its definition file does not.
        # symlinks=True for the same reason the snapshot keeps them: this copies
        # a tree the model made, and a dangling link in it must not cost a grade.
        shutil.copytree(
            snapshot, artifacts, dirs_exist_ok=True, symlinks=True,
            ignore=lambda d, names: [
                n for n in names
                if PRODUCT_DIRS.match(n)
                or n.endswith((".sif", ".tar.gz", ".tgz", ".zip"))
            ],
        )
    else:
        os.makedirs(artifacts, exist_ok=True)
        open(os.path.join(artifacts, "NOTHING-WAS-WRITTEN"), "w").write(
            "The model wrote no files to the project directory.\n")

    h = hashlib.sha256()
    for name in ("TASK.md", "TRANSCRIPT.md", "MECHANICAL.md"):
        h.update(open(os.path.join(dest, name), "rb").read())
    for dirpath, dirnames, filenames in os.walk(artifacts):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            h.update(name.encode())
            # The tree is the model's, and since the snapshot started preserving
            # symlinks it can hold a dangling one — a model that installs a
            # toolchain leaves them behind. `open` on that raises, and this loop
            # runs before the judge is called, so it took the whole batch down:
            # 56 trials went unjudged because of one .bam link in one cell.
            # Hash what the link says rather than what it points at; the link is
            # what the model created, and it is stable across machines.
            if os.path.islink(path):
                h.update(os.readlink(path).encode())
                continue
            try:
                h.update(open(path, "rb").read())
            except OSError as e:
                h.update(f"<unreadable: {type(e).__name__}>".encode())
    return h.hexdigest()[:16]


def aggregate(dimensions: list[dict]) -> float | None:
    """Mean of the applicable dimensions, on 0-1.

    `must` / `must_not` are reported beside it rather than folded in: a single
    number that mixes "how good is the DAG" with "did it violate a hard rule"
    hides which of the two went wrong, and §5 asks for the components.
    """
    scores = [d["score"] for d in dimensions
              if d.get("applicable") and isinstance(d.get("score"), int)]
    return round(sum(scores) / len(scores) / 5.0, 3) if scores else None


def judge_one(rec: dict, task: dict, trial_dir: str, model: str,
              timeout_s: float, skip_if_hash: str | None = None) -> dict:
    work = tempfile.mkdtemp(prefix="l1-")
    try:
        bundle_dir = os.path.join(work, "bundle")
        input_hash = bundle(rec, task, trial_dir, bundle_dir)
        # Hash first, judge second. The bundle is cheap and the judge call is
        # not, so an unchanged trial must cost nothing to re-run over.
        if skip_if_hash is not None and skip_if_hash == input_hash:
            return {"input_hash": input_hash, "skipped": True}
        schema_path = os.path.join(work, "schema.json")
        out_path = os.path.join(work, "out.json")
        json.dump(SCHEMA, open(schema_path, "w"))

        cmd = [
            "codex", "exec",
            "--model", model,
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C", bundle_dir,
            "--output-schema", schema_path,
            "-o", out_path,
            PROMPT,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        if not os.path.isfile(out_path):
            return {"input_hash": input_hash,
                    "error": f"codex exit {proc.returncode}: "
                             f"{(proc.stderr or proc.stdout)[-600:]}"}
        raw = open(out_path).read().strip()
        try:
            verdict = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"input_hash": input_hash, "error": f"unparseable verdict: {e}: {raw[:400]}"}

        dims = verdict.get("dimensions") or []
        return {
            "input_hash": input_hash,
            "l1_score": aggregate(dims),
            "dimensions": dims,
            "criteria": {"must": verdict.get("must") or [],
                         "must_not": verdict.get("must_not") or []},
            "summary": verdict.get("summary") or "",
            "error": None,
        }
    except subprocess.TimeoutExpired:
        return {"input_hash": None, "error": f"judge timed out after {timeout_s}s"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def judgement_path(trial_dir: str, judge_model: str) -> str:
    """Where a judgement is written on disk, one file per judge.

    The judge model is part of the store's primary key, so it belongs in the
    filename too: judging the same trial with a second model must add a record,
    not overwrite the first.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", judge_model)
    return os.path.join(trial_dir, f"judgement--{safe}.json")


def write_judgement(trial_dir: str, rec: dict) -> None:
    """Persist a judgement beside its trial, before the store is touched.

    `ingest.py` puts it plainly: the filesystem is the record of truth and the
    database is an index over it. Judgements were the exception — they lived
    only in `results.db`, which is gitignored, so the 2026-09-05 wipe took all
    46 of them while every trial record survived in git. This closes that hole:
    `sync_runs.sh` copies these files into the repo and `ingest.py` reads them
    back, so a judging pass is recoverable the same way a sweep already is.
    """
    with open(judgement_path(trial_dir, rec["judge_model"]), "w") as fh:
        json.dump(rec, fh, indent=2, sort_keys=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--trial-id", action="append", default=None,
                    help="restrict to these trials (repeatable)")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE,
                    help="pinned and recorded per judgement (§5)")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--force", action="store_true",
                    help="re-judge even when the bundle hash is unchanged")
    ap.add_argument("--db", default=DEFAULT_PATH)
    args = ap.parse_args()

    store = Store(args.db)
    # Only *successful* judgements suppress a re-judge. A failed one still
    # records the bundle hash it was attempted with, so including it here made
    # the skip permanent: three trials lost the judge to a transient
    # `ERROR: Reconnecting... 5/5` and would have been skipped on every retry
    # as "already judged, bundle unchanged". A failure is exactly the thing that
    # should be retried.
    seen = {(j["trial_id"], j["judge_model"]): j["input_hash"]
            for j in store.judgements(args.run_id) if not j["error"]}

    judged = skipped = failed = 0
    for row in store.trials(args.run_id):
        rec = dict(row)
        if args.trial_id and rec["trial_id"] not in args.trial_id:
            continue
        trial_dir = os.path.join(RUNS, args.run_id, rec["trial_id"])
        spec_path = os.path.join(TASKS, f"{rec['task']}.yaml")
        if not os.path.isdir(trial_dir) or not os.path.isfile(spec_path):
            print(f"skip {rec['trial_id']}: no trial directory or task spec")
            continue
        task = yaml.safe_load(open(spec_path))

        # One trial must never end the batch. Judging a run is hours of work
        # and every verdict is written as it is produced, so an exception that
        # escapes here throws away every trial after it — which is exactly what
        # a single unreadable file did, silently, after 29 of 85.
        try:
            verdict = judge_one(
                rec, task, trial_dir, args.judge_model, args.timeout,
                skip_if_hash=None if args.force else seen.get((rec["trial_id"], args.judge_model)),
            )
        except Exception as e:  # noqa: BLE001 - recorded, not swallowed
            verdict = {"input_hash": None, "error": f"{type(e).__name__}: {e}"}
        if verdict.pop("skipped", False):
            skipped += 1
            print(f"skip {rec['trial_id']}: already judged, bundle unchanged")
            continue

        judgement = {
            "run_id": args.run_id,
            "trial_id": rec["trial_id"],
            "judge_model": args.judge_model,
            "judged_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            **verdict,
        }
        write_judgement(trial_dir, judgement)   # disk first, then the index
        store.save_judgement(judgement)
        if verdict.get("error"):
            failed += 1
            print(f"{rec['trial_id']}: FAILED — {verdict['error']}")
        else:
            judged += 1
            violated = sum(1 for c in verdict["criteria"]["must_not"] if c.get("violated"))
            unmet = sum(1 for c in verdict["criteria"]["must"] if not c.get("met"))
            print(f"{rec['trial_id']}: L0={rec.get('l0_score')} L1={verdict['l1_score']} "
                  f"({unmet} must unmet, {violated} must_not violated)")

    store.close()
    print(f"judged {judged}, skipped {skipped}, failed {failed} in run {args.run_id}")


if __name__ == "__main__":
    main()
