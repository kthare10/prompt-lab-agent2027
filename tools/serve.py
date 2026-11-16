#!/usr/bin/env python3
"""Serve the lab: a live leaderboard at `/` and the live plan at `/plan`.

    python3 tools/serve.py                  # http://0.0.0.0:8899
    python3 tools/serve.py --port 9000

Both pages read from disk on every request, so neither can go stale the way a
published snapshot does — which is the whole reason they exist. `PLAN.md` is the
design of record and changes often; keeping a copy of it in sync by hand is the
kind of chore that quietly stops happening.

The board is organised by **task**, not by run. A sweep spans several runs when
it covers several providers (`sweep.py` takes one `--provider`), and nobody
reading results cares which of the three wrote a row — they care how each model
did on each task. Runs stay in the store; they are just not the axis.

The database is opened **read-only** (`mode=ro`) and re-opened per request, so a
sweep writing to it is never blocked or locked out by someone watching.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import json
import os
import re
import sqlite3
import statistics
import sys
import urllib.parse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.cohort import (columns_by_family, columns_for,  # noqa: E402
                            drop_unmeasurable,
                            family, kind, latest_by_cell, open_ro, poolable,
                            stage1, task_specs)
from harness.stats import permutation_p, spearman  # noqa: E402
from tools.dashboard import CSS, DB  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = os.path.join(ROOT, "PLAN.md")
REFRESH_S = 20

# Only for rows recorded before trials carried `provider_name`. A new provider
# does not belong here — `label_for` prefers the name Studio itself holds.
PROVIDERS = {"custom-mtd4weae": "FABRIC", "custom-mtd4viar": "NRP", "custom-mtd4ywcs": "Azure"}


def label_for(cell: dict) -> str:
    """What to call this cell's provider: what the trial recorded, else the map,
    else the id itself — never `?`, which tells a reader nothing."""
    name = (cell.get("checks") or {}).get("provider_name")
    if name:
        return str(name)
    pid = cell.get("provider") or ""
    return PROVIDERS.get(pid) or pid or "?"


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── the board ─────────────────────────────────────────────────────────────

def verdict(t: dict) -> tuple[str, str]:
    """(css class, label) — what happened, which the score alone does not say."""
    # A task with no artifact has nothing to be discoverable. Falling through to
    # "no files" labelled a correct review, diagnosis or clarifying question as
    # a failure to write one — the same mistake the L0 mask fixes one layer down.
    # Nothing was asked, so nothing was answered: the request the gateway got
    # had no messages, or the trial read zero tokens. Rendering that in the same
    # red as a model failure reads as "this model broke", which is the opposite
    # of what happened — and ten of glm-5's fifteen cells are these.
    if t.get("never_reached"):
        return "unmeasured", "not measured"
    if t["status"] == "done" and (t.get("depth") or "none") == "none":
        return "ok", "answered"
    if t["status"] == "done" and t["checks"].get("discoverable"):
        return "ok", "ok"
    if t["status"] == "done":
        return "warn", "no files"
    if t["status"] == "turn_timeout":
        return "warn", "too slow"
    if t["status"] == "harness_error":
        # Ours, not the model's: the container was busy, the provider config was
        # missing, the sweep never sent the request. Rendering it in the same red
        # as a model failure reads as "this model broke", which is the opposite
        # of what happened — the model was never asked. These cells are awaiting
        # re-measurement, not results.
        return "unmeasured", "not measured"
    return "err", t["status"].replace("_", " ")


def _l1_cells(c: dict, l0_rank: int | None, l1_rank: int | None) -> str:
    """The L1 score, its disagreement with L0, and the task's hard criteria."""
    if c["l1_score"] is None:
        return '<td class="num muted">not judged</td><td class="muted">·</td>'
    shift = ""
    if l0_rank and l1_rank and l0_rank != l1_rank:
        arrow = "&#9650;" if l1_rank < l0_rank else "&#9660;"
        cls = "ok" if l1_rank < l0_rank else "err"
        shift = (f' <span class="delta {cls}" title="L0 ranked it {l0_rank}">'
                 f'{arrow}{abs(l0_rank - l1_rank)}</span>')
    unmet, violated = c["must_unmet"], c["must_not_violated"]
    if unmet or violated:
        bits = []
        if unmet:
            bits.append(f"{unmet} must unmet")
        if violated:
            bits.append(f"{violated} violated")
        crit = f'<span class="pill err">{esc(", ".join(bits))}</span>'
    else:
        crit = '<span class="pill ok">all met</span>'
    return (f'<td class="num strong">{c["l1_score"]:.3f}{shift}</td>'
            f'<td>{crit}</td>')


# ── what may be averaged, and what may not ────────────────────────────────
# A single ranked column answers "which model is best" by averaging every task,
# which assumes the tasks measure one thing. The twelve build tasks do — they
# agree with each other at mean rho +0.46. The three `depth: none` tasks do not:
# -0.17 between them, with review against debug at -0.535, further below zero
# than any of the 66 build pairs reaches. A mean over those ranked models by
# nothing, and the board reported it that way until the pairs were looked at.
#
# So the columns are built by `harness.cohort.columns_for`, which pools a group
# only where the group holds together, and otherwise reports its tasks side by
# side. L1 throughout: on a task with no artifact there is nothing for L0 to
# measure, so the judge is the only score every column shares.

# Fewer permutations than the analysis: this recomputes on every request, and
# nobody reads a live board for the third decimal of a p-value.
BOARD_PERMUTATIONS = 2000


def _num_cell(x: float | None, best: float | None = None) -> str:
    if x is None:
        return '<td class="num muted">·</td>'
    top = " top" if best is not None and x == best else ""
    return f'<td class="num strong{top}">{x:.3f}</td>'


def work_split(cells: dict, specs: dict, models: list[str], tasks: list[str]) -> str:
    l1 = {(t, m): float(c["l1_score"]) for (t, m), c in cells.items()
          if c["l1_score"] is not None}
    if not l1 or len(tasks) < 2:
        return ""

    groups = []
    for axis_name, axis in (("kind", kind), ("family", family)):
        for name in sorted({axis(specs.get(t, {})) for t in tasks}):
            ts = [t for t in tasks if axis(specs.get(t, {})) == name]
            if len(ts) < 2:
                continue
            ok, rho, pairs = poolable(l1, ts, models)
            if any(g["tasks"] == ts for g in groups):
                continue                       # a family identical to its kind
            groups.append({"axis": axis_name, "name": name, "tasks": ts,
                           "poolable": ok, "rho": rho, "pairs": pairs})
    cols = columns_for(l1, specs, tasks, models)
    if len(cols) < 2:
        return ""

    out = ['<section id="split"><h2>What may be averaged</h2>'
           '<p class="note">A group of tasks earns one column only by agreeing with itself — '
           'mean Spearman rho between every pair of tasks in it, across models. Where a group '
           'does not hold together, averaging it produces a column that ranks models by '
           'nothing, so its tasks are reported side by side below and never summed.</p>'
           '<div class="scroller"><table><thead><tr><th>group</th><th>axis</th><th>tasks</th>'
           '<th>pairs</th><th>mean inter-task rho</th><th></th></tr></thead><tbody>']
    for g in groups:
        cls = "ok" if g["poolable"] else "err"
        label = "pooled" if g["poolable"] else "not pooled"
        rho = f'{g["rho"]:.3f}' if g["rho"] == g["rho"] else "—"
        out.append(f'<tr><td class="k">{esc(g["name"])}</td>'
                   f'<td class="k muted">{g["axis"]}</td>'
                   f'<td class="num">{len(g["tasks"])}</td>'
                   f'<td class="num">{g["pairs"]}</td>'
                   f'<td class="num strong">{rho}</td>'
                   f'<td><span class="pill {cls}">{label}</span></td></tr>')
    out.append("</tbody></table></div></section>")

    # The leaderboard itself, ranked by the widest column it is allowed to have.
    prim = max(cols, key=lambda c: len(c["tasks"]))
    best = {c["key"]: max([v for v in c["scores"].values() if v is not None] or [0])
            for c in cols}
    out.append('<section id="abilities"><h2>Leaderboard</h2>'
               f'<p class="note">Mean <b>L1</b> per column, ranked by <b>{esc(prim["label"])}</b>'
               f' ({len(prim["tasks"])} tasks). A column of one task is one measurement per '
               'model, not an average — read it as a cell, not a ranking. '
               '<b>reached</b> counts the cells where a model was asked anything at '
               'all; the rest are not results and are averaged into nothing.</p>')

    rows = []
    for c in cols:
        if c is prim:
            continue
        pair = [(prim["scores"][m], c["scores"][m]) for m in models
                if prim["scores"][m] is not None and c["scores"][m] is not None]
        if len(pair) > 2:
            rho = spearman([x for x, _ in pair], [y for _, y in pair])
            p = permutation_p([x for x, _ in pair], [y for _, y in pair],
                              BOARD_PERMUTATIONS)
            rows.append(f'<li><b>{esc(prim["label"])}</b> vs <b>{esc(c["label"])}</b>: '
                        f'rho {rho:+.2f} <span class="muted">(p = {p:.2f})</span></li>')
    if rows:
        out.append('<p class="headline">Does the widest column predict the others?</p>'
                   '<ul class="reasons">' + "".join(rows) + "</ul>")

    reached = {m: (sum(1 for (t, mm), c in cells.items()
                       if mm == m and not c.get("never_reached")),
                   sum(1 for (t, mm) in cells if mm == m)) for m in models}
    out.append('<div class="scroller"><table><thead><tr><th>model</th><th>provider</th>'
               '<th>reached</th>'
               + "".join(f'<th>{esc(c["label"])} <span class="muted">({len(c["tasks"])})'
                         "</span></th>" for c in cols)
               + "</tr></thead><tbody>")
    for m in sorted(models, key=lambda m: -(prim["scores"][m] if prim["scores"][m] is not None
                                            else -1)):
        c0 = next(c for (t, mm), c in cells.items() if mm == m)
        ok, tot = reached[m]
        cov = (f'<td class="num">{ok}/{tot}</td>' if ok == tot else
               f'<td class="num warn" title="the rest never reached a model">'
               f"{ok}/{tot}</td>")
        out.append(f'<tr><td class="k">{esc(m)}</td>'
                   f'<td class="k muted">{label_for(c0)}</td>' + cov
                   + "".join(_num_cell(c["scores"][m], best[c["key"]]) for c in cols)
                   + "</tr>")
    out.append("</tbody></table></div></section>")
    return "".join(out)


def board(db_path: str) -> str:
    db = open_ro(db_path)
    cells, unmeasurable = drop_unmeasurable(latest_by_cell(db))
    running = db.execute("SELECT COUNT(*) FROM trials WHERE status = 'running'").fetchone()[0]
    db.close()
    if not cells:
        return ("<section><h2>No results yet</h2><p class='note'>The store is empty; "
                "it fills in as trials finish.</p></section>")

    specs = task_specs()
    tasks = sorted({t for t, _ in cells})
    models = sorted({m for _, m in cells})
    # Cells that never ran carry a null score. They must not define "best", and
    # must not count toward a mean — a trial the harness never sent is not a zero.
    best = {t: max([c["l0_score"] for (tt, _), c in cells.items()
                    if tt == t and c["l0_score"] is not None] or [0])
            for t in tasks}
    done = sum(1 for c in cells.values() if c["status"] == "done")

    out = [
        '<header class="mast"><p class="eyebrow">prompt-lab · open-weight model evaluation</p>',
        "<h1>Leaderboard</h1>",
        f'<p class="sub">{len(cells)} cells · {len(models)} models · {len(tasks)} tasks'
        + (f' · <span class="run">{running} running now</span>' if running else "")
        + "</p>",
        '<div class="tiles">',
        f'<div class="tile"><b>{len(models)}</b><span>models</span></div>',
        f'<div class="tile"><b>{len(tasks)}</b><span>tasks</span></div>',
        f'<div class="tile"><b>{done}</b><span>completed</span></div>',
        f'<div class="tile"><b>{len(cells) - done}</b><span>did not finish</span></div>',
        "</div></header>",
        axis_note("kind", specs, tasks),
    ]
    if unmeasurable:
        out.append(
            '<section class="excluded"><h2>Excluded from ranking</h2>'
            '<p class="note">These models returned an error before doing any work, '
            'in every trial. The cause is how they are served, not what they can do, '
            'so a score would be misleading.</p><ul>'
            + "".join(f"<li><b>{esc(m)}</b> — {esc(reason)} "
                      f'<span class="muted">({n} trials)</span></li>'
                      for m, reason, n in unmeasurable)
            + "</ul></section>")
    split = work_split(cells, specs, models, tasks)
    out += [
        '<nav class="tasknav"><span>jump to</span><a href="#matrix">overview</a>',
    ]
    if split:
        out.append('<a href="#split">what pools</a>'
                   '<a href="#abilities">leaderboard</a>')
    out += [f'<a href="#{esc(t)}">{esc(t.split("-", 1)[0])}</a>' for t in tasks]
    out.append("</nav>")
    out.append(split)

    # Overview: every model against every task, ranked by mean.
    out.append('<section id="matrix"><h2>All tasks at a glance</h2>'
               '<p class="note">One row per model, one column per task. A dot means not run; '
               "the best score in each task is marked. <b>median</b> is the wall clock of a "
               "completed trial — turns, tools and the execution stages — over cells that "
               "finished; a censored or never-started cell records a ceiling or a second, not "
               "a duration, so neither counts.</p>"
               '<div class="scroller"><table><thead><tr><th>model</th><th>provider</th>')
    out += [f"<th>{esc(t.split('-', 1)[0])}</th>" for t in tasks]
    out.append("<th>mean L0</th><th>mean L1</th><th>median</th></tr></thead><tbody>")
    lat = latency(cells)
    def mean_l0(model: str) -> float:
        got = [c["l0_score"] for (tt, mm), c in cells.items()
               if mm == model and c["l0_score"] is not None]
        return statistics.fmean(got) if got else 0.0

    for m in sorted(models, key=lambda m: -mean_l0(m)):
        mine = [c for (tt, mm), c in cells.items() if mm == m]
        out.append(f'<tr><td class="k">{esc(m)}</td>'
                   f'<td class="k muted">{label_for(mine[0])}</td>')
        for t in tasks:
            c = cells.get((t, m))
            if not c:
                out.append('<td class="num muted">·</td>')
                continue
            cls, _ = verdict(c)
            if c["l0_score"] is None:
                out.append(f'<td class="num {cls}">&mdash;</td>')
                continue
            top = " top" if c["l0_score"] == best[t] else ""
            out.append(f'<td class="num {cls}{top}">{c["l0_score"]:.2f}</td>')
        l1s = [c["l1_score"] for c in mine if c["l1_score"] is not None]
        mean_l1 = (f'<td class="num strong">{statistics.fmean(l1s):.2f}</td>'
                   if l1s else '<td class="num muted">·</td>')
        med = fmt_secs(lat.get(m, {}).get("median_s"))
        out.append(f'<td class="num strong">{mean_l0(m):.2f}</td>{mean_l1}'
                   f'<td class="num muted">{med}</td></tr>')
    out.append("</tbody></table></div></section>")

    # One ranked section per task.
    for t in tasks:
        ranked = sorted([(m, c) for (tt, m), c in cells.items() if tt == t],
                        key=lambda x: -(x[1]["l0_score"] if x[1]["l0_score"] is not None else -1))
        finished = sum(1 for _, c in ranked if c["status"] == "done")
        # Rank by L1 where the whole task has been judged, because that is the
        # question the reader is asking; fall back to L0 otherwise. Inside a
        # saturated band L0 order is noise — on t01 every one of the top six
        # moved when L1 arrived — so the shift is shown rather than hidden.
        l0_rank = {m: i for i, (m, _) in enumerate(ranked, 1)}
        judged_all = ranked and all(c["l1_score"] is not None for _, c in ranked)
        if judged_all:
            ranked = sorted(ranked, key=lambda x: -(x[1]["l1_score"] or 0))
        unmeasured = sum(1 for _, c in ranked if c["status"] == "harness_error")
        fam = family(specs.get(t, {}))
        out.append(f'<section id="{esc(t)}"><h2>{esc(t)} '
                   f'<span class="pill fam">{esc(fam)}</span></h2>'
                   f'<p class="note">{len(ranked)} models · {finished} completed'
                   + (f' · <b>{unmeasured} awaiting re-measurement</b> (a harness fault, '
                      'not a model result)' if unmeasured else '')
                   + '. '
                   + ("Ranked by <b>L1</b> (judged correctness); "
                      "<span class='muted'>L0 rank shown beside it where it disagrees</span>."
                      if judged_all else
                      "Ranked by L0; the columns are what produced it.")
                   + " L0 is mechanical — did it run. L1 is a judge on the artifacts, "
                     "citing evidence. Where they disagree, L0 wins on executability.</p>"
                   '<div class="scroller"><table><thead><tr><th>model</th><th>provider</th>'
                   "<th>L0</th><th></th><th>L1</th><th>criteria</th><th>verdict</th>"
                   "<th>wall</th><th>tools</th><th>writes</th></tr></thead><tbody>")
        for m, c in ranked:
            k, (cls, label) = c["checks"], verdict(c)
            score = c["l0_score"] if c["l0_score"] is not None else None
            if score is None:
                out.append(
                    f'<tr><td class="k">{esc(m)}</td>'
                    f'<td class="k muted">{label_for(c)}</td>'
                    f'<td class="num muted">&mdash;</td><td></td>'
                    + _l1_cells(c, None, None)
                    + f'<td><span class="pill {cls}">{label}</span></td>'
                    f'<td class="num muted">&middot;</td><td class="num muted">&middot;</td>'
                    f'<td class="num muted">&middot;</td></tr>')
                continue
            tools = k.get("tool_calls")
            writes = k.get("write_calls")
            out.append(
                f'<tr><td class="k">{esc(m)}</td>'
                f'<td class="k muted">{label_for(c)}</td>'
                f'<td class="num strong">{score:.3f}</td>'
                f'<td class="barcell"><span class="bar-track"><span class="bar-fill {cls}" '
                f'style="width:{max(1.0, score * 100):.1f}%"></span></span></td>'
                + _l1_cells(c, l0_rank.get(m), ranked.index((m, c)) + 1 if judged_all else None)
                + f'<td><span class="pill {cls}">{label}</span></td>'
                f'<td class="num">{(c["wall_s"] or 0):.0f}s</td>'
                f'<td class="num">{tools if tools is not None else "·"}</td>'
                f'<td class="num">{writes if writes is not None else "·"}</td></tr>')
        out.append("</tbody></table></div></section>")
    return "".join(out)


# ── the judge ─────────────────────────────────────────────────────────────

DIMENSION_BLURB = {
    "dag_correctness": "the shape of the job graph",
    "catalog_correctness": "executables vs data vs sites, in the right catalog",
    "wrapper_quality": "explicit paths, real error handling",
    "container_definition": "base image, dependencies, no --fakeroot",
    "data_staging": "declared so Pegasus can stage it",
    "diagnosis_quality": "a cause and an action, and the cause is the real one",
    "communication_quality": "could a scientist act on this",
}


def latency(cells: dict) -> dict:
    """Per model: how long its trials take, and how fast it generates.

    **Not `ttft_s`.** That field reads 0.13s median across 333 cells with a 0.7s
    maximum, because it times the first SSE frame out of OpenCode — which the
    engine emits immediately — not the model's first token. It measures the
    harness's link to the engine and says nothing about the model.

    So `median_s` is wall clock for the whole trial: every turn, every tool the
    model ran, and the generate/build/plan stages. For routing that is arguably
    the honest number anyway — it answers "how long until I have a workflow" —
    but it is not LLM latency and must not be labelled as if it were.

    Only `done` cells count. A `turn_timeout` records the ceiling rather than a
    duration, and a cell that never reached a model records a second or two; both
    would drag a median toward a number no trial actually took.

    `s_per_step` is closer to model speed — wall clock over the LLM round trips
    the turn made — but `steps` only exists on trials measured after 66a9348, so
    it is absent for most of Stage 1 and shown only where it is real.
    """
    out = {}
    for (_t, m), c in cells.items():
        if c.get("status") != "done":
            continue
        out.setdefault(m, {"wall": [], "steps": [], "turns": []})
        if c.get("wall_s") is not None:
            out[m]["wall"].append(c["wall_s"])
            if c.get("steps"):
                out[m]["steps"].append(c["wall_s"] / c["steps"])
        if c.get("turns") is not None:
            out[m]["turns"].append(c["turns"])
    stats = {}
    for m, v in out.items():
        stats[m] = {
            "n": len(v["wall"]),
            "median_s": statistics.median(v["wall"]) if v["wall"] else None,
            "s_per_step": statistics.median(v["steps"]) if v["steps"] else None,
        }
    return stats


def fmt_secs(v) -> str:
    if v is None:
        return "&mdash;"
    if v < 90:
        return f"{v:.0f}s"
    if v < 5400:
        return f"{v / 60:.0f}m"
    return f"{v / 3600:.1f}h"


def axis_note(which: str, specs: dict, tasks: list[str]) -> str:
    """Explain the axis a page groups tasks by — once, for both pages.

    `kind` and `family` are easy to mistake for the same thing and are not. The
    difference that matters is *derived* against *declared*: a task is a judgement
    task iff it produces no artifact, which falls out of its `depth` and cannot be
    mislabelled, while `family:` is written in the spec because nothing in the data
    implies it. The coarse split is the one findings rest on, so it is the one that
    is not a matter of opinion.

    Rendered collapsed. A reader who knows this does not need to scroll past it.
    """
    by_kind, by_family = defaultdict(list), defaultdict(list)
    for t in tasks:
        by_kind[kind(specs.get(t, {}))].append(t)
        by_family[family(specs.get(t, {}))].append(t)

    def short(ts):
        return ", ".join(sorted(x.split("-", 1)[0] for x in ts))

    if which == "kind":
        rows = "".join(
            f"<li><b>{esc(k)}</b> ({len(v)} tasks) &mdash; {esc(short(v))}</li>"
            for k, v in sorted(by_kind.items()))
        body = (
            "<p><b>kind</b> is the coarse split this page ranks by, and it is "
            "<b>derived, not declared</b>: a task is <code>judgement</code> if and only if "
            "it declares <code>depth: none</code>, meaning it asks for no artifact &mdash; "
            "a review, a diagnosis, a question back. Everything else is "
            "<code>build</code>.</p>"
            "<p>It is derived on purpose. L0 cannot score a task with nothing on disk, so "
            "the split decides where the mechanical grade applies at all; leaving it to a "
            "label would let a task be filed into the group whose finding it would change. "
            "Declare the depth honestly and the split follows.</p>"
            f"<ul>{rows}</ul>"
            "<p class='note'>The finer <b>family</b> axis &mdash; build, port, containerise, "
            "review, debug, elicit &mdash; is on the <a href='/routing'>routing</a> page.</p>")
    else:
        rows = "".join(
            f"<li><b>{esc(k)}</b> ({len(v)} task{'s' if len(v) != 1 else ''}) "
            f"&mdash; {esc(short(v))}</li>"
            for k, v in sorted(by_family.items()))
        body = (
            "<p><b>family</b> is the fine-grained kind of work, and unlike <code>kind</code> "
            "it is <b>declared</b> in each task's spec &mdash; because nothing in the data "
            "implies it. It is what you route on: finding 3 is that these are different "
            "abilities, with debugging a failed run tracking workflow-building at +0.74 "
            "while reviewing one does not correlate with it at all.</p>"
            f"<ul>{rows}</ul>"
            "<p>A family is averaged into one column only if its tasks agree with each "
            "other (mean pairwise Spearman &ge; 0.2 across models). Where they do not, the "
            "tasks appear separately and are never summed &mdash; an average over tasks that "
            "disagree ranks models by nothing.</p>"
            "<p class='note'>The coarse <b>kind</b> axis &mdash; build against judgement, "
            "derived from whether a task produces an artifact &mdash; is on the "
            "<a href='/'>leaderboard</a>.</p>")
    return (f"<details class='axis'><summary>What is a &ldquo;{esc(which)}&rdquo;?</summary>"
            f"{body}</details>")


def routing_page(db_path: str) -> str:
    """Which model for which KIND of work.

    The leaderboard ranks by the build column because it is the biggest. That is
    the wrong question for an agent picking a model for a review or a diagnosis:
    finding 3 is that those are different abilities, with debug tracking build at
    +0.74 and review not correlating with it at all.

    Grouping comes from `cohort.columns_by_family`, the same call `tools/routing.py`
    makes, so the page and the tool cannot disagree about what may be averaged.

    The provider is shown beside the model because routing needs both: a model id
    alone does not say which endpoint serves it, and the same name can be served
    by more than one. It comes from `label_for`, so a provider added on a new
    machine labels itself rather than rendering as an opaque id.
    """
    db = open_ro(db_path)
    cells, dropped = stage1(db)
    db.close()
    specs = task_specs()
    models = sorted({m for _, m in cells})
    tasks = sorted({t for t, _ in cells})
    l1 = {k: c["l1_score"] for k, c in cells.items() if c.get("l1_score") is not None}
    cols = columns_by_family(l1, specs, tasks, models)

    # One provider per model, off whichever of its cells we have.
    provider = {}
    for (_t, m), c in cells.items():
        provider.setdefault(m, label_for(c))
    lat = latency(cells)

    if not l1:
        return ("<section><h2>Routing</h2><p class='note'>No judged cells yet — "
                "this view is L1 only, because a <code>depth: none</code> task has "
                "no artifact for L0 to score.</p></section>")

    out = ["<section><h2>Which model for which kind of work</h2>",
           "<p class='note'>L1 (judge) per task <b>family</b>. A family earns one "
           "column only by agreeing with itself (mean pairwise Spearman &ge; 0.2); "
           "where it does not, its tasks are shown separately and never summed. "
           "<b>Click any column to sort by it.</b> Default order is the mean across "
           "columns. <b>median</b> is wall clock for a whole completed trial — turns, "
           "tools and the generate/build/plan stages — not model latency: "
           "<code>ttft_s</code> times the first frame out of the engine, which arrives "
           "in 0.13s median and measures nothing about the model.</p>",
           axis_note("family", specs, tasks)]

    out.append("<div class='tw'><table id='routing'><thead><tr>"
               "<th class='sortable' data-type='text'>model</th>"
               "<th class='sortable' data-type='text'>provider</th>"
               "<th class='sortable' data-type='num' title='median wall clock of a "
               "completed trial: every turn, every tool the model ran, and the "
               "generate/build/plan stages'>median<br><span class='sub'>per trial"
               "</span></th>"
               "<th class='sortable' data-type='num' title='wall clock per LLM round "
               "trip; only recorded for trials measured after 66a9348'>s/step<br>"
               "<span class='sub'>where known</span></th>")
    for c in cols:
        n = len(c["tasks"])
        tag = "" if c["pooled"] else " <span title='this family does not hold "
        tag = tag + "together — one task, not a family score'>&#9888;</span>" if not c["pooled"] else ""
        out.append(f"<th class='sortable' data-type='num'>{esc(c['label'])}{tag}"
                   f"<br><span class='sub'>{n} task{'s' if n != 1 else ''}</span></th>")
    out.append("</tr></thead><tbody>")

    def rank(m):
        vs = [c["scores"].get(m) for c in cols]
        vs = [v for v in vs if v is not None]
        return -(sum(vs) / len(vs)) if vs else 1.0

    for m in sorted(models, key=rank):
        lm = lat.get(m, {})
        sps = lm.get("s_per_step")
        out.append(f"<tr><td class='model'>{esc(m)}</td>"
                   f"<td class='prov'>{esc(provider.get(m, '?'))}</td>"
                   f"<td class='lat'>{fmt_secs(lm.get('median_s'))}</td>"
                   f"<td class='lat'>{'&mdash;' if sps is None else f'{sps:.0f}s'}</td>")
        for c in cols:
            v = c["scores"].get(m)
            best = max((x for x in c["scores"].values() if x is not None), default=None)
            cls = " class='best'" if v is not None and best is not None and v >= best else ""
            out.append(f"<td{cls}>{'&mdash;' if v is None else f'{v:.3f}'}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")

    out.append("<h3>Best per kind</h3><ul>")
    for c in cols:
        ranked = sorted(((v, m) for m, v in c["scores"].items() if v is not None),
                        reverse=True)
        if not ranked:
            continue
        top, best = ranked[0]
        near = [m for v, m in ranked[1:] if top - v <= 0.05]
        caveat = ""
        if not c["pooled"]:
            caveat = (" &mdash; <b>its family does not hold together</b>, so this is "
                      "that one task, not a family score")
        elif len(c["tasks"]) == 1:
            caveat = " &mdash; one task, so one measurement per model"
        who = f"{esc(best)} <span class='sub'>({esc(provider.get(best, '?'))})</span>"
        if near:
            verdict = (f"<b>{who}</b> {top:.3f}, but {esc(', '.join(near[:3]))} "
                       f"within 0.05 &mdash; not separable at this sample size")
        elif len(ranked) > 1:
            verdict = f"<b>{who}</b> {top:.3f}, clear by {top - ranked[1][0]:.3f}"
        else:
            verdict = f"<b>{who}</b> {top:.3f}"
        out.append(f"<li>{esc(c['label'])}: {verdict}{caveat}</li>")
    out.append("</ul>")

    if dropped:
        out.append("<p class='note'>Excluded, never asked anything: "
                   + "; ".join(f"{esc(m)} ({n} cells)" for m, _r, n in dropped)
                   + ".</p>")
    out.append("</section>")

    # Sorting is done here rather than by reloading with a query parameter: the
    # table is small, already in the page, and a round trip would lose the
    # reader's place. `&mdash;` sorts last in both directions — a model that was
    # never asked is not "worst", it is absent.
    out.append("""<script>
(function () {
  var table = document.getElementById('routing');
  if (!table) return;
  var head = table.tHead.rows[0], body = table.tBodies[0];
  [].forEach.call(head.cells, function (th, i) {
    if (th.className.indexOf('sortable') < 0) return;
    th.addEventListener('click', function () {
      var numeric = th.dataset.type === 'num';
      var desc = th.dataset.dir !== 'desc';
      [].forEach.call(head.cells, function (o) {
        o.removeAttribute('data-dir');
        o.classList.remove('sorted');
      });
      th.dataset.dir = desc ? 'desc' : 'asc';
      th.classList.add('sorted');
      var rows = [].slice.call(body.rows);
      rows.sort(function (a, b) {
        var x = a.cells[i].textContent.trim(), y = b.cells[i].textContent.trim();
        var xm = (x === '\u2014'), ym = (y === '\u2014');
        if (xm || ym) return xm && ym ? 0 : (xm ? 1 : -1);
        if (numeric) return desc ? (parseFloat(y) - parseFloat(x))
                                 : (parseFloat(x) - parseFloat(y));
        return desc ? y.localeCompare(x) : x.localeCompare(y);
      });
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });
})();
</script>""")
    return "".join(out)


def judge_page(db_path: str) -> str:
    db = open_ro(db_path)
    cells, _ = drop_unmeasurable(latest_by_cell(db))
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM judgements ORDER BY judged_at DESC")]
    except sqlite3.OperationalError:
        rows = []
    db.close()

    live = {(c["run_id"], c["trial_id"]): c for c in cells.values()}
    judged = [r for r in rows if (r["run_id"], r["trial_id"]) in live and not r["error"]]
    models = {r["judge_model"] for r in rows}

    out = ['<header class="mast"><p class="eyebrow">prompt-lab · L1</p>',
           "<h1>The Codex judge</h1>",
           '<p class="sub">What a rubric grader sees that a mechanical one cannot</p>',
           "</header>"]

    out.append(
        '<section><h2>Why there is a second grader</h2>'
        "<p class='lede'>L0 is deterministic and free: did the files land, does the generator "
        "compile, does it run, does the workflow generate, build and plan. It answers "
        "<i>did this work at all</i> — and once models clear that bar it stops "
        "discriminating. On <code>t01</code> six models finished between 0.886 and 1.000 and "
        "one sat on the ceiling.</p>"
        "<p>L1 is an LLM judge run over the artifacts through "
        "<code>codex exec --sandbox read-only</code>, one process per trial. Three things "
        "make its scores worth reading:</p>"
        "<ul class='reasons'>"
        "<li><b>It must cite evidence.</b> The JSON schema makes <code>evidence</code> and "
        "<code>source</code> required on every score. A number it cannot quote a file for is "
        "not a number it can return.</li>"
        "<li><b>It sees artifacts, not claims.</b> The model's own transcript is supplied "
        "separately and labelled as a claim to check. Where the prose and the files disagree, "
        "the files win.</li>"
        "<li><b>The judge is pinned and recorded.</b> A silent judge upgrade would invalidate "
        "every earlier comparison, so the model is written into each row.</li>"
        "</ul>"
        "<p class='note'>Where L0 and L1 disagree, <b>L0 wins on executability</b> — a workflow "
        "that does not run is not redeemed by elegant code. L1 speaks to correctness, which is "
        "a different question.</p></section>")

    if not judged:
        out.append("<section><h2>Nothing judged yet</h2><p class='note'>Run "
                   "<code>python3 -m harness.judge --run-id &lt;run&gt;</code>.</p></section>")
        return "".join(out)

    # Where the two graders disagree, which is the whole point.
    moved = []
    for task in sorted({live[(r["run_id"], r["trial_id"])]["task"] for r in judged}):
        cand = [(live[(r["run_id"], r["trial_id"])], r) for r in judged
                if live[(r["run_id"], r["trial_id"])]["task"] == task]
        if len(cand) < 2:
            continue
        by_l0 = {c["model"]: i for i, (c, _) in
                 enumerate(sorted(cand, key=lambda x: -(x[0]["l0_score"] or 0)), 1)}
        by_l1 = {c["model"]: i for i, (c, _) in
                 enumerate(sorted(cand, key=lambda x: -(x[1]["l1_score"] or 0)), 1)}
        for c, r in cand:
            if by_l0[c["model"]] != by_l1[c["model"]]:
                moved.append((task, c, r, by_l0[c["model"]], by_l1[c["model"]]))

    out.append(f'<section><h2>Coverage</h2><div class="tiles">'
               f'<div class="tile"><b>{len(judged)}</b><span>trials judged</span></div>'
               f'<div class="tile"><b>{len(moved)}</b><span>rank changes</span></div>'
               f'<div class="tile"><b>{esc(", ".join(sorted(models)) or "—")}</b>'
               f'<span>judge model</span></div></div></section>')

    if moved:
        out.append('<section><h2>Where the two graders disagree</h2>'
                   "<p class='note'>Rank by L0 against rank by L1, within a task. A model that "
                   "moves is one whose mechanical score was not measuring what the task asked "
                   "for.</p><div class='scroller'><table><thead><tr><th>task</th><th>model</th>"
                   "<th>L0</th><th>L1</th><th>rank</th><th>what the judge found</th>"
                   "</tr></thead><tbody>")
        for task, c, r, r0, r1 in sorted(moved, key=lambda x: (x[0], x[4])):
            arrow = "&#9650;" if r1 < r0 else "&#9660;"
            cls = "ok" if r1 < r0 else "err"
            out.append(
                f'<tr><td class="k muted">{esc(task.split("-", 1)[0])}</td>'
                f'<td class="k">{esc(c["model"])}</td>'
                f'<td class="num">{(c["l0_score"] or 0):.3f}</td>'
                f'<td class="num strong">{(r["l1_score"] or 0):.3f}</td>'
                f'<td class="num"><span class="delta {cls}">{r0}&rarr;{r1} {arrow}</span></td>'
                f'<td class="why">{esc((r["summary"] or "")[:260])}</td></tr>')
        out.append("</tbody></table></div></section>")

    # Every judgement, in full, because the evidence is the product.
    out.append('<section><h2>Every judgement</h2>'
               "<p class='note'>Each score with the quote it was based on and the file it came "
               "from. Open one to read what the judge actually saw.</p>")
    for r in sorted(judged, key=lambda r: -(r["l1_score"] or 0)):
        c = live[(r["run_id"], r["trial_id"])]
        dims = json.loads(r["dimensions"] or "[]")
        crit = json.loads(r["criteria"] or "{}")
        unmet = sum(1 for x in crit.get("must", []) if not x.get("met"))
        bad = sum(1 for x in crit.get("must_not", []) if x.get("violated"))
        flag = (f'<span class="pill err">{unmet + bad} criteria failed</span>'
                if (unmet or bad) else '<span class="pill ok">all criteria met</span>')
        out.append(
            f'<details class="judgement"><summary>'
            f'<span class="jm">{esc(c["model"])}</span>'
            f'<span class="jt muted">{esc(c["task"].split("-", 1)[0])}</span>'
            f'<span class="js">L0 {(c["l0_score"] or 0):.3f}</span>'
            f'<span class="js strong">L1 {(r["l1_score"] or 0):.3f}</span>{flag}</summary>')
        out.append(f'<p class="lede">{esc(r["summary"] or "")}</p>')
        out.append('<div class="scroller"><table><thead><tr><th>dimension</th><th>score</th>'
                   "<th>why</th><th>evidence</th></tr></thead><tbody>")
        for d in dims:
            if not d.get("applicable"):
                out.append(f'<tr class="na"><td class="k">{esc(d.get("name", ""))}</td>'
                           f'<td class="num muted">n/a</td>'
                           f'<td class="why muted" colspan="2">'
                           f'{esc(d.get("reason") or "this task does not exercise it")}</td></tr>')
                continue
            score = d.get("score") or 0
            cls = "ok" if score >= 4 else ("warn" if score == 3 else "err")
            out.append(
                f'<tr><td class="k">{esc(d.get("name", ""))}'
                f'<span class="dimhint">{esc(DIMENSION_BLURB.get(d.get("name"), ""))}</span></td>'
                f'<td class="num"><span class="pill {cls}">{score}/5</span></td>'
                f'<td class="why">{esc(d.get("reason") or "")}</td>'
                f'<td class="ev"><code>{esc((d.get("evidence") or "")[:400])}</code>'
                f'<span class="src">{esc(d.get("source") or "")}</span></td></tr>')
        out.append("</tbody></table></div>")
        if crit.get("must") or crit.get("must_not"):
            out.append('<div class="scroller"><table><thead><tr><th>the task&rsquo;s own rules'
                       "</th><th></th><th>evidence</th></tr></thead><tbody>")
            for x in crit.get("must", []):
                ok = x.get("met")
                out.append(f'<tr><td class="why">{esc(x.get("criterion", ""))}</td>'
                           f'<td><span class="pill {"ok" if ok else "err"}">'
                           f'{"met" if ok else "not met"}</span></td>'
                           f'<td class="ev"><code>{esc((x.get("evidence") or "")[:300])}</code>'
                           f'<span class="src">{esc(x.get("source") or "")}</span></td></tr>')
            for x in crit.get("must_not", []):
                bad_ = x.get("violated")
                out.append(f'<tr><td class="why">must not: {esc(x.get("criterion", ""))}</td>'
                           f'<td><span class="pill {"err" if bad_ else "ok"}">'
                           f'{"violated" if bad_ else "clean"}</span></td>'
                           f'<td class="ev"><code>{esc((x.get("evidence") or "")[:300])}</code>'
                           f'<span class="src">{esc(x.get("source") or "")}</span></td></tr>')
            out.append("</tbody></table></div>")
        out.append("</details>")
    out.append("</section>")
    return "".join(out)


# ── the plan ──────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def render_plan() -> str:
    """PLAN.md as HTML with a sidebar, read fresh so the page cannot lag the file."""
    from markdown_it import MarkdownIt

    with open(PLAN) as fh:
        src = fh.read()
    body = MarkdownIt("commonmark", {"linkify": True}) \
        .enable("table").enable("strikethrough").render(src)

    toc: list[tuple[int, str, str]] = []

    def anchor(m: re.Match) -> str:
        level, inner = int(m.group(1)), m.group(2)
        plain = re.sub(r"<[^>]+>", "", inner)
        sid = _slug(plain)
        toc.append((level, sid, plain))
        return f'<h{level} id="{sid}">{inner}</h{level}>'

    body = re.sub(r"<h([23])>(.*?)</h\1>", anchor, body, flags=re.S)
    # Tables scroll in their own box rather than widening the page.
    body = body.replace("<table>", '<div class="tw"><table>').replace("</table>", "</table></div>")

    # `text` came from stripping tags off markdown-it's output, so its entities
    # are already escaped — escaping again would render a literal &quot;.
    links = "".join(
        f'<a class="lv{lv}" href="#{sid}">{text}</a>' for lv, sid, text in toc)
    mtime = dt.datetime.fromtimestamp(os.path.getmtime(PLAN)).strftime("%Y-%m-%d %H:%M")
    return (
        '<div class="doc">'
        f'<aside class="side"><details open><summary>Contents</summary><nav>{links}</nav>'
        f'<p class="stamp">PLAN.md · edited {mtime}</p></details></aside>'
        f'<article class="prose">{body}</article></div>'
        # Highlight the section currently in view. Progressive: without JS the
        # sidebar is still a working list of links.
        "<script>(function(){"
        "var ls=[].slice.call(document.querySelectorAll('.side nav a'));"
        "var hs=ls.map(function(a){return document.getElementById("
        "decodeURIComponent(a.getAttribute('href').slice(1)));});"
        "function mark(){var y=window.scrollY+120,i=0;"
        "for(var j=0;j<hs.length;j++){if(hs[j]&&hs[j].offsetTop<=y){i=j;}}"
        "ls.forEach(function(a,k){a.classList.toggle('on',k===i);});}"
        "addEventListener('scroll',mark,{passive:true});addEventListener('resize',mark);mark();"
        "})();</script>"
    )


# ── chrome ────────────────────────────────────────────────────────────────

BOARD_CSS = """<style>
.pill.unmeasured{color:var(--ink-3);border-style:dashed}
.num.unmeasured,.bar-fill.unmeasured{color:var(--ink-3);background:var(--rule)}
.reasons{margin:14px 0 0;padding-left:0;list-style:none;display:flex;flex-direction:column;gap:9px}
.reasons li{padding-left:15px;border-left:2px solid var(--rule-2);font-size:13.5px;line-height:1.62}
.judgement{border:1px solid var(--rule);border-radius:7px;margin-bottom:9px;background:var(--surface)}
.judgement>summary{cursor:pointer;padding:11px 14px;display:flex;gap:11px;align-items:center;
  flex-wrap:wrap;list-style:none;font-size:13px}
.judgement>summary::-webkit-details-marker{display:none}
.judgement[open]>summary{border-bottom:1px solid var(--rule)}
.judgement>summary:hover{background:var(--surface-2)}
.jm{font-family:"IBM Plex Mono",ui-monospace,monospace;font-weight:600}
.jt{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px}
.js{font-variant-numeric:tabular-nums;font-size:12px;color:var(--ink-2);margin-left:auto}
.js.strong{color:var(--ink);font-weight:600;margin-left:0}
.judgement>p,.judgement>.scroller{margin:12px 14px}
.dimhint{display:block;font-family:"IBM Plex Sans",system-ui,sans-serif;font-size:11px;
  color:var(--ink-3);font-weight:400;margin-top:2px;max-width:20ch}
.why{font-size:12.5px;line-height:1.58;max-width:46ch}
.ev code{display:block;white-space:pre-wrap;word-break:break-word;font-size:11px;line-height:1.5;
  background:var(--surface-2);border:1px solid var(--rule);border-radius:4px;padding:6px 8px;
  max-width:44ch;max-height:8.5em;overflow:auto}
.src{display:block;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px;
  color:var(--ink-3);margin-top:4px;word-break:break-all}
tr.na{opacity:.55}
.delta{font-size:.72em;font-weight:600;letter-spacing:.02em}
.tasknav{position:sticky;top:31px;z-index:8;display:flex;gap:6px;flex-wrap:wrap;align-items:center;
  background:var(--ground);border-bottom:1px solid var(--rule);padding:10px 0;margin-bottom:4px}
.tasknav span{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px;
  letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);margin-right:4px}
.tasknav a{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;color:var(--ink-2);
  text-decoration:none;padding:3px 9px;border:1px solid var(--rule-2);border-radius:2px}
.tasknav a:hover,.tasknav a:focus-visible{color:var(--accent);border-color:var(--accent)}
section[id]{scroll-margin-top:92px}
td.strong{font-weight:600}
td.top{position:relative}
td.top::after{content:"";position:absolute;left:4px;top:50%;transform:translateY(-50%);
  width:4px;height:4px;border-radius:50%;background:var(--accent)}
td.ok{color:var(--good)}td.warn{color:var(--warn)}td.err{color:var(--bad)}
.barcell{width:30%;min-width:110px}
.bar-track{display:block;height:7px;background:var(--surface-2);border-radius:4px;overflow:hidden}
.bar-fill{display:block;height:100%;border-radius:0 4px 4px 0;background:var(--accent)}
.bar-fill.ok{background:var(--good)}.bar-fill.warn{background:var(--warn)}
.bar-fill.err{background:var(--bad)}
.pill{display:inline-block;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px;
  letter-spacing:.04em;padding:1px 8px;border-radius:9px;border:1px solid currentColor;white-space:nowrap}
.pill.ok{color:var(--good)}.pill.warn{color:var(--warn)}.pill.err{color:var(--bad)}
.sub .run{color:var(--accent);font-weight:600}
.pill.fam{color:var(--ink-3);vertical-align:middle;margin-left:6px;font-weight:400}
.headline{font-size:13.5px;line-height:1.62;max-width:66ch;margin:0 0 18px;
  padding-left:13px;border-left:2px solid var(--accent)}
/* Routing: sortable headers. The affordance has to be visible without hover,
   because a column you can sort and cannot tell is sortable is not a feature. */
th.sortable{cursor:pointer;user-select:none;position:relative}
th.sortable:hover{color:var(--accent)}
th.sortable::after{content:'\2195';opacity:.28;margin-left:5px;font-size:10px}
th.sortable.sorted::after{opacity:1;color:var(--accent)}
th.sortable.sorted[data-dir=desc]::after{content:'\2193'}
th.sortable.sorted[data-dir=asc]::after{content:'\2191'}
td.prov{color:var(--ink-3);font-size:12px;white-space:nowrap}
td.lat{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink-2);font-size:12px}
/* The axis explainer, collapsed: a reader who already knows what a `kind` is
   should not have to scroll past the definition to reach the table. */
details.axis{margin:0 0 18px;border:1px solid var(--rule);border-radius:6px;
  padding:9px 13px;background:var(--bg-2)}
details.axis summary{cursor:pointer;font-size:12.5px;font-weight:600;
  color:var(--ink-2);user-select:none}
details.axis[open] summary{margin-bottom:9px;color:var(--accent)}
details.axis p{font-size:12.5px;line-height:1.6;max-width:74ch;margin:0 0 9px}
details.axis ul{font-size:12.5px;line-height:1.6;margin:0 0 9px;padding-left:19px}
details.axis code{font-size:11.5px}
</style>"""

PROSE_CSS = """<style>
.doc{display:grid;grid-template-columns:236px minmax(0,1fr);gap:38px;align-items:start}
@media (max-width:900px){.doc{grid-template-columns:minmax(0,1fr);gap:8px}}
.side{position:sticky;top:52px;max-height:calc(100vh - 72px);overflow-y:auto;padding:14px 0 20px}
@media (max-width:900px){.side{position:static;max-height:none}}
.side summary{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px;
  letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);cursor:pointer;margin-bottom:10px}
.side nav{display:flex;flex-direction:column;gap:1px;border-left:1px solid var(--rule)}
.side nav a{font-size:12.5px;color:var(--ink-3);text-decoration:none;padding:3px 0 3px 12px;
  border-left:2px solid transparent;margin-left:-1px;line-height:1.35}
.side nav a.lv3{padding-left:24px;font-size:12px}
.side nav a:hover{color:var(--accent)}
.side nav a.on{color:var(--accent);border-left-color:var(--accent);font-weight:500}
.side .stamp{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px;
  color:var(--ink-3);margin:14px 0 0;padding-left:12px}
.prose h1{font-family:"IBM Plex Serif",Georgia,serif;font-size:clamp(26px,3.4vw,34px);
  font-weight:600;letter-spacing:-.02em;margin:22px 0 10px;text-wrap:balance}
.prose h2{font-family:"IBM Plex Serif",Georgia,serif;font-size:22px;font-weight:600;
  margin:46px 0 8px;padding-top:16px;border-top:1px solid var(--rule);letter-spacing:-.01em}
.prose h3{font-size:16px;font-weight:600;margin:28px 0 6px}
.prose h4{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12.5px;
  letter-spacing:.04em;color:var(--ink-2);margin:22px 0 6px}
.prose :is(h1,h2,h3)[id]{scroll-margin-top:70px}
.prose p,.prose li{max-width:72ch}
.prose p{margin:0 0 13px}
.prose ul,.prose ol{margin:0 0 14px;padding-left:22px}
.prose li{margin:0 0 5px}
.prose blockquote{margin:18px 0;padding:12px 18px;background:var(--surface-2);
  border-left:3px solid var(--bad);color:var(--ink)}
.prose blockquote p:last-child{margin-bottom:0}
.prose pre{background:var(--surface-2);border:1px solid var(--rule);padding:13px 15px;
  overflow-x:auto;font-size:12.5px;line-height:1.55;margin:0 0 16px}
.prose pre code{background:none;padding:0;font-size:inherit}
.prose .tw{overflow-x:auto;border:1px solid var(--rule);background:var(--surface);margin:0 0 18px}
.prose .tw table{border:none;margin:0;min-width:0}
.prose a{color:var(--accent)}
.prose hr{border:none;border-top:1px solid var(--rule);margin:34px 0}
</style>"""


# Each stylesheet constant must be a *complete* `<style>` element. Rules added
# just after the opening `"""` land outside the tag, and a browser hoists that
# stray text into the body — it renders as a wall of CSS at the top of every
# page while the rules themselves never apply. That happened twice; this is the
# check that would have caught it at import.
for _name, _sheet in (("CSS", CSS), ("BOARD_CSS", BOARD_CSS), ("PROSE_CSS", PROSE_CSS)):
    _s = _sheet.strip()
    assert _s.startswith("<style>") and _s.endswith("</style>") \
        and _s.count("<style>") == _s.count("</style>"), (
        f"{_name} is not a complete <style> element — rules would leak into the page"
    )


def shell(body: str, *, here: str = "board", refresh: bool = True,
          title: str = "Prompt Lab — live") -> str:
    """Wrap a body in the shared chrome.

    `refresh` is off for the plan: a document is read, and a page that reloads
    under you mid-paragraph is worse than one you refresh yourself.
    """
    tabs = "".join(
        f'<a class="tab{" on" if key == here else ""}" href="{href}">{label}</a>'
        for key, href, label in (("board", "/", "leaderboard"),
                                 ("routing", "/routing", "routing"),
                                 ("judge", "/judge", "codex judge"),
                                 ("plan", "/plan", "plan")))
    status = (f"<span>re-reads the store every {REFRESH_S}s</span>"
              f"<span>updated {dt.datetime.now().strftime('%H:%M:%S')}</span>"
              if refresh else "<span>reload to pick up edits</span>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        + (f"<meta http-equiv='refresh' content='{REFRESH_S}'>" if refresh else "")
        + f"<title>{title}</title>"
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?"
        "family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600"
        "&family=IBM+Plex+Serif:wght@600&display=swap'>"
        + CSS + BOARD_CSS + PROSE_CSS +
        "<style>"
        ".live{position:sticky;top:0;z-index:9;background:var(--surface-2);"
        "border-bottom:1px solid var(--rule-2);padding:7px 26px;"
        "font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11.5px;"
        "color:var(--ink-3);display:flex;gap:16px;flex-wrap:wrap;align-items:center}"
        ".tab{color:var(--ink-3);text-decoration:none;padding:2px 0;border-bottom:2px solid transparent}"
        ".tab.on{color:var(--accent);font-weight:600;border-bottom-color:var(--accent)}"
        ".tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}"
        "</style></head><body>"
        f"<div class='live'>{tabs}{status}</div>"
        f"<div class='wrap'>{body}</div></body></html>"
    )


class Handler(http.server.BaseHTTPRequestHandler):
    db_path = DB
    # A browser opens speculative connections that never send a request line.
    # Reading one blocks the handler until the client gives up; single-threaded
    # that wedges the whole server behind an empty socket, which is exactly how
    # the first version of this failed.
    timeout = 15
    protocol_version = "HTTP/1.0"   # close each response; no keep-alive to strand

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        path = urllib.parse.urlparse(self.path).path
        if path in ("/judge", "/judge/"):
            render, kw = (lambda: judge_page(self.db_path)), {
                "here": "judge", "refresh": False, "title": "Prompt Lab — the judge"}
        elif path in ("/routing", "/routing/"):
            render, kw = (lambda: routing_page(self.db_path)), {
                "here": "routing", "refresh": False,
                "title": "Prompt Lab — routing"}
        elif path in ("/plan", "/plan/"):
            render, kw = render_plan, {"here": "plan", "refresh": False,
                                       "title": "Prompt Lab — plan"}
        elif path in ("/", "/index.html"):
            render, kw = (lambda: board(self.db_path)), {}
        else:
            self.send_error(404)
            return
        try:
            out = shell(render(), **kw)
        except Exception as exc:  # noqa: BLE001 — a watcher must not crash the server
            out = shell(f"<section><h2>Could not render this page</h2>"
                        f"<p class='note'>{esc(f'{type(exc).__name__}: {exc}')}</p></section>",
                        **{**kw, "refresh": False})
        raw = out.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_a) -> None:
        """Silence per-request logging — this polls, and the noise buries errors."""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    # Fail here rather than serving "Could not render this page" on every tab.
    # The store moved out of the code tree, so `DB` is only right when
    # PROMPT_LAB_RUNS is set; started without it, the server came up healthy on
    # a path that does not exist and every page was an error card. A 200 that
    # says nothing is worse than not starting.
    if not os.path.isfile(args.db):
        raise SystemExit(
            f"no store at {args.db}\n"
            "  set PROMPT_LAB_RUNS to the results directory, or pass --db.\n"
            "  results live outside the repo, conventionally "
            f"{os.path.join(os.path.expanduser('~'), 'prompt-lab-runs')}")

    Handler.db_path = args.db
    # Threaded: one slow render, or one silent preconnect socket, must not block
    # every other request behind it.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    http.server.ThreadingHTTPServer.daemon_threads = True
    with http.server.ThreadingHTTPServer((args.host, args.port), Handler) as srv:
        print(f"serving on http://{args.host}:{args.port}  (db {args.db})", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
