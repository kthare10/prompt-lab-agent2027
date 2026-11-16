"""Which trials make up a result, and what family of work each task is.

Two rules that had no single home, and cost something each time they were
re-derived by hand:

**Pooling.** A sweep spans several runs because `sweep.py` takes one
`--provider`, and a task gets re-measured when the environment moves under it
(t06 in `s1c-*` after ceiling #11, t07 in `s1b-*`). So "the Stage 1 result" is
not a `run_id` — it is the newest trial per (task, model) across a named set of
runs, minus the models that were never actually served. `report.py` takes a
single `--run-id` and cannot express that; the board in `tools/serve.py` grew
its own copy; and the headline finding was computed a third way in a script that
no longer exists. Three definitions, three different numbers from one store.
This is the definition.

**Family.** Tasks are not one kind of work. Twelve ask for an artifact and are
graded on whether it runs; three (`depth: none`) ask the model to review, debug
or ask a question, and have nothing to execute — L0 measures almost nothing
there, which is exactly what `tools/stage1.py` reports. The coarse split is
therefore *derived*, not declared: a task is a judgement task iff it produces no
artifact. The finer `family:` in each task spec (build / port / containerise /
review / debug / elicit) is declared, because nothing in the data implies it.
"""

from __future__ import annotations

import json
import os
import statistics
import sqlite3

import yaml

from harness.stats import mean_pairwise_spearman

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS = os.path.join(ROOT, "tasks")

# The Stage 1 cohort. `s1-*` is the main sweep; `s1b-*` carries t07, whose
# fixture was not ready when the sweep ran; `s1c-*` is the t06 re-run after
# ceiling #11 (the project could not be named anything but the default), which
# is why t06 appears twice in the store and the newest must win. `s1d-*` is the
# re-measurement of cells whose request never reached a model at all.
STAGE1_RUNS = ("s1-%", "s1b-%", "s1c-%", "s1d-%")
# Trials that really ran and are not results: a cell run by hand to confirm the
# ceiling was lifted before paying for the t06 re-run, and the smoke test that
# checked the harness still worked after the store was rebuilt from git. Both
# were newer than the sweep, so "newest per cell" quietly promoted them —
# `smoke-restore` had been standing in as gpt-oss-20b's t00 result on the live
# board, which is how this list gained its second entry. `t15t16-smoke` is the
# one-trial-per-task validation of the two new elicitation tasks: it exists to
# prove the spec runs and the simulator answers, not to say anything about a
# model, and it is named here before it runs rather than after it misleads.
EXCLUDED_RUNS = ("s1c-probe", "smoke-restore", "t15t16-smoke", "s2-probe",
                 "oc-smoke", "builtin-smoke")

# Models that produced no measurement to rank: zero tool calls in every trial,
# for reasons that live in the deployment rather than the model. Ranking them
# prints a serving fact as a capability result. Named with their cause rather
# than dropped quietly, because "this model is absent" is itself a finding.
UNMEASURABLE = {
    "qwen3-4bit":
        "not served — every request returned 404 (no matching route)",
    "gemma-small-e4b":
        "served with a 16,384-token context; the harness prompt does not fit",
}


# A trial can fail without saying anything about the model. Two signatures, both
# measured rather than assumed:
#
#   * The gateway rejected the request with "chat-completions request must have
#     at least one message". Studio built that request, and a request with no
#     messages never described a task to anybody. Re-running does not help: the
#     glm-5 / t14 cell was re-run on 2026-09-11 and failed identically in 207s
#     (`s1d-nrp`), so the cell is not recoverable without a Studio fix.
#   * The trial consumed zero input tokens. Nothing was read, so nothing was
#     asked. Every one of the 240 Stage 1 cells that finished consumed tokens,
#     so this separates cleanly: 13 cells, 9 of them glm-5, including eight that
#     were recorded as `turn_timeout` — the harness sat out the 2400s ceiling on
#     a request that had never reached a model at all.
#
# Sixteen of the twenty unfinished Stage 1 cells are one or both. The remaining
# four are nemotron-nano-30b, which engaged, worked for two to five turns and
# then hit the ceiling — a real result about a slow model, and a censored score.
NO_MESSAGES = "chat-completions request must have at least one message"


def never_reached_the_model(trial: dict) -> bool:
    """Did this trial fail before a model was asked anything?"""
    if trial.get("status") == "done":
        return False
    if NO_MESSAGES in (trial.get("error") or ""):
        return True
    return not (trial.get("input_tokens") or 0)


def open_ro(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


# ── what kind of work a task is ───────────────────────────────────────────

def task_specs() -> dict[str, dict]:
    specs = {}
    for name in sorted(os.listdir(TASKS)):
        if name.endswith(".yaml"):
            with open(os.path.join(TASKS, name)) as fh:
                spec = yaml.safe_load(fh) or {}
            specs[spec.get("id") or name[:-5]] = spec
    return specs


def kind(spec: dict) -> str:
    """`build` or `judgement` — derived from whether there is an artifact.

    `depth: none` means the trial stops at the transcript: nothing is generated,
    nothing is planned, nothing can be executed. Every mechanical check below
    `wrote_files` is inapplicable, so an L0 score on such a task is mostly a
    statement about whether the model happened to write a file it was not asked
    for. Kept derived so a new task cannot be misfiled: declare its depth
    honestly and the split follows.
    """
    return "judgement" if (spec.get("depth") or "none") == "none" else "build"


def family(spec: dict) -> str:
    return spec.get("family") or "unclassified"


# ── which trials count ────────────────────────────────────────────────────

def latest_by_cell(
    db: sqlite3.Connection,
    runs: tuple[str, ...] | None = None,
) -> dict[tuple[str, str], dict]:
    """The most recent trial per (task, model), with its judgement attached.

    A cell can be run more than once — a retry after an endpoint outage, a
    re-measurement after an environment fix. The newest is the answer; the older
    ones stay in the store and on disk. `runs` restricts to matching `run_id`
    LIKE patterns; the default is the whole store, which is what the live board
    wants and what an analysis of one stage does not.
    """
    judged: dict[tuple[str, str], dict] = {}
    try:
        for j in db.execute("SELECT * FROM judgements"):
            judged[(j["run_id"], j["trial_id"])] = dict(j)
    except sqlite3.OperationalError:
        pass                      # a store written before L1 existed

    where, params = "", []
    if runs:
        where = " WHERE (" + " OR ".join("run_id LIKE ?" for _ in runs) + ")"
        params = list(runs)
    if EXCLUDED_RUNS:
        where += (" AND " if where else " WHERE ") + \
            " AND ".join("run_id != ?" for _ in EXCLUDED_RUNS)
        params += list(EXCLUDED_RUNS)

    cells: dict[tuple[str, str], dict] = {}
    for r in db.execute(f"SELECT * FROM trials{where} ORDER BY started_at", params):
        d = dict(r)
        d["checks"] = json.loads(d["checks"] or "{}")
        # L1 rides along on the trial rather than in its own table: the question
        # a reader has is "how did this cell do", not "what did the judge say
        # about some trial id".
        j = judged.get((d["run_id"], d["trial_id"]))
        d["l1_score"] = j["l1_score"] if j else None
        d["l1_summary"] = (j or {}).get("summary") or ""
        # `or {}` *after* the parse as well as before it: a failed judgement
        # stores criteria as the literal `null`, which is truthy as a string and
        # parses to None, so the pre-parse guard alone let it through and the
        # whole board raised AttributeError on the next line.
        crit = (json.loads((j or {}).get("criteria") or "{}") if j else {}) or {}
        d["must_unmet"] = sum(1 for c in crit.get("must", []) if not c.get("met"))
        d["must_not_violated"] = sum(1 for c in crit.get("must_not", []) if c.get("violated"))
        d["never_reached"] = never_reached_the_model(d)
        # A score on a trial that never reached a model is a number about
        # nothing. Null here rather than in the store, so the record of what the
        # grader computed stays intact and the rule stays in one place.
        if d["never_reached"]:
            d["l0_score"] = None
            # The judge scored several of these too, reading a transcript in
            # which the model never said anything. Ten of glm-5's fifteen cells
            # are such non-events; left in, they were most of its L1 average.
            d["l1_score"] = None
        if d["task"] and d["model"]:
            cells[(d["task"], d["model"])] = d
    return cells


def drop_unmeasurable(
    cells: dict[tuple[str, str], dict],
) -> tuple[dict[tuple[str, str], dict], list[tuple[str, str, int]]]:
    """Split the cell set into what can be ranked and what cannot.

    Returns (kept, dropped) where dropped is (model, reason, trial count) for
    each excluded model actually present, so the caller can say what is missing.
    """
    seen: dict[str, int] = {}
    for (_, model) in cells:
        if model in UNMEASURABLE:
            seen[model] = seen.get(model, 0) + 1
    kept = {k: v for k, v in cells.items() if k[1] not in UNMEASURABLE}
    dropped = [(m, UNMEASURABLE[m], n) for m, n in sorted(seen.items())]
    return kept, dropped


# A group of tasks earns a single averaged column only by agreeing with itself.
# The twelve build tasks correlate at mean rho +0.46 across models: whatever they
# measure, they measure the same thing, and a mean over them means something. The
# three `depth: none` tasks correlate at -0.17, with review against debug at
# -0.535 — further below zero than any of the 66 build pairs reaches. Averaging
# those into a "judgement" score produced a column that ranked models by nothing,
# and it was reported that way until the pairs were looked at.
#
# The floor is a judgement call; the gap it sits in is not. Anything at or below
# zero is unpoolable by definition, and no observed group lands near 0.2 from
# either side, so the exact value changes no current result.
POOL_MIN_RHO = 0.2


def poolable(scores: dict[tuple[str, str], float], tasks: list[str],
             models: list[str]) -> tuple[bool, float, int]:
    """May these tasks be averaged into one column? (verdict, mean rho, pairs)

    A single task is trivially its own column and needs no agreement to report.
    """
    if len(tasks) < 2:
        return True, float("nan"), 0
    rho, pairs = mean_pairwise_spearman(
        [[scores.get((t, m)) for m in models] for t in tasks])
    if rho != rho:
        return False, rho, pairs
    return rho >= POOL_MIN_RHO, rho, pairs


def columns_for(l1: dict[tuple[str, str], float], specs: dict,
                tasks: list[str], models: list[str],
                axis=kind) -> list[dict]:
    """The columns a leaderboard may honestly carry.

    One per group of tasks that agrees with itself, and one per task in every
    group that does not. Shared by the analysis and the live board so the two
    cannot disagree about what may be averaged — which is the whole reason this
    module exists.

    `axis` selects the grouping: `kind` for the coarse build/judgement split that
    the leaderboard ranks by, `family` for the finer cut that answers "which model
    for which kind of work". The pooling test is the same either way, which is the
    point of passing the axis rather than writing the loop twice.
    """
    cols: list[dict] = []
    for name in sorted({axis(specs.get(t, {})) for t in tasks}):
        ts = [t for t in tasks if axis(specs.get(t, {})) == name]
        ok, rho, pairs = poolable(l1, ts, models)
        if ok:
            cols.append({"key": name, "label": name, "tasks": ts, "pooled": True,
                         "rho": rho, "pairs": pairs,
                         "scores": {m: _mean([l1[(t, m)] for t in ts if (t, m) in l1])
                                    for m in models}})
        else:
            for t in ts:
                cols.append({"key": t,
                             "label": f"{t.split('-', 1)[0]} {family(specs.get(t, {}))}",
                             "tasks": [t], "pooled": False, "rho": float("nan"),
                             "pairs": 0, "scores": {m: l1.get((t, m)) for m in models}})
    return cols


def columns_by_family(l1: dict[tuple[str, str], float], specs: dict,
                      tasks: list[str], models: list[str]) -> list[dict]:
    """Columns on the declared `family:` axis — build / port / containerise /
    review / debug / elicit. Same pooling rule, finer grouping.

    `containerise` does not currently survive it: t03 and t06 correlate at +0.114,
    so they come back as two columns and are never summed.
    """
    return columns_for(l1, specs, tasks, models, axis=family)


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def stage1(db: sqlite3.Connection) -> tuple[dict[tuple[str, str], dict],
                                            list[tuple[str, str, int]]]:
    """The Stage 1 cohort: pooled, newest-per-cell, rankable models only."""
    return drop_unmeasurable(latest_by_cell(db, STAGE1_RUNS))
