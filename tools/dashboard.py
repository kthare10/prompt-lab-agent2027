#!/usr/bin/env python3
"""Render the results store as a single self-contained HTML dashboard.

    python3 tools/dashboard.py                 # newest run
    python3 tools/dashboard.py --run-id m1     # a specific run
    python3 tools/dashboard.py --all           # every run, summary + newest detail

Static by design: the page is generated from `runs/results.db` and republished.
A live page would have to reach the API or the database, and an artifact can do
neither — so the honest thing is a snapshot that says when it was taken.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import sqlite3
import statistics
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Results moved off the code tree (see harness/store.py RUNS_DIR): the code is
# mounted read-only in the container and results must stay writable, so nesting
# them meant one `rm -rf` of the tree took both.
sys.path.insert(0, ROOT)
from harness.store import RUNS_DIR  # noqa: E402

DB = os.path.join(RUNS_DIR, "results.db")
OUT = os.path.join(RUNS_DIR, "dashboard.html")

# Categorical slots 1-3 of the reference palette, validated for both surfaces
# with scripts/validate_palette.js. Assigned to models in fixed order and never
# cycled: colour follows the model, not its rank, so filtering cannot repaint it.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70"]

CHECKS = [
    ("protocol_clean", "clean tool protocol"),
    ("discoverable", "discoverable"),
    ("project_name_matched", "named as asked"),
    ("wrote_files", "files on disk"),
    ("deliverables_ok", "deliverables complete"),
    ("generator_compiles", "generator parses"),
    ("generator_runs", "generator runs"),
    ("generate_ok", "generate stage"),
    ("build_ok", "image build"),
    ("plan_ok", "plan stage"),
]
FLAGS = [
    ("turn_collapsed", "turn collapsed"),
    ("silent_turn", "silent turn"),
    ("wrote_elsewhere", "wrote somewhere invisible"),
    ("narrated_not_written", "narrated, wrote nothing"),
    ("tool_call_leaked_into_chat", "tool call leaked into chat"),
    ("uses_bad_fakeroot", "used --fakeroot"),
    ("rounds_exhausted", "tool rounds exhausted"),
]


def rows(db: sqlite3.Connection, run_id: str | None = None) -> list[dict]:
    q = "SELECT * FROM trials" + (" WHERE run_id = ?" if run_id else "")
    out = []
    for r in db.execute(q, (run_id,) if run_id else ()):
        d = dict(r)
        d["checks"] = json.loads(d["checks"] or "{}")
        out.append(d)
    return out


def pct(vals: list) -> float | None:
    seen = [bool(v) for v in vals if v is not None]
    return 100 * sum(seen) / len(seen) if seen else None


def bar(value: float | None, color: str, label: str, maximum: float = 100.0) -> str:
    """A thin bar with a rounded data-end and a visible label.

    The label is not decoration: the light-surface green sits below 3:1 against
    the page, and the palette validator's contrast WARN obliges either visible
    labels or a table view. This page has both.
    """
    if value is None:
        return '<div class="bar-row"><span class="bar-val muted">—</span></div>'
    w = max(1.5, 100 * value / maximum)
    return (
        f'<div class="bar-row"><span class="bar-track">'
        f'<span class="bar-fill" style="width:{w:.1f}%;background:{color}"></span></span>'
        f'<span class="bar-val">{label}</span></div>'
    )


def build(db: sqlite3.Connection, run_id: str, all_runs: list[str], in_progress: bool = False) -> str:
    data = rows(db, run_id)
    models = sorted({r["model"] for r in data if r["model"]})
    tasks = sorted({r["task"] for r in data if r["task"]})
    colour = {m: SERIES[i % len(SERIES)] for i, m in enumerate(models)}
    dark = {m: SERIES_DARK[i % len(SERIES_DARK)] for i, m in enumerate(models)}
    by_model = defaultdict(list)
    for r in data:
        by_model[r["model"]].append(r)

    ranked = sorted(models, key=lambda m: -statistics.fmean(
        [t["l0_score"] or 0 for t in by_model[m]] or [0]))

    disc = pct([r["checks"].get("discoverable") for r in data])
    gen = pct([r["checks"].get("generate_ok") for r in data])
    ksha = next((r["knowledge_sha"] for r in data if r["knowledge_sha"]), "—")
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    P = []
    A = P.append
    if in_progress:
        # A partial sweep must never read as a finished result. Cells complete in
        # task-then-model order, so an early snapshot is not just smaller — it is
        # skewed toward whichever models have been reached.
        A('<div class="banner"><b>Run in progress.</b> These are partial results: '
          'cells complete in order, so the mix of models and tasks below is not yet '
          'balanced. Treat rates as provisional until the run finishes.</div>')

    # ── hero ──────────────────────────────────────────────────────────────
    A(f'''<header class="mast">
  <p class="eyebrow">prompt-lab · open-weight model evaluation</p>
  <h1>Run {html.escape(run_id)}</h1>
  <p class="sub">{len(data)} trials · {len(models)} models · {len(tasks)} tasks ·
     knowledge <code>{html.escape(str(ksha))}</code> · snapshot {generated}</p>
  <div class="tiles">
    <div class="tile"><b>{len(data)}</b><span>trials</span></div>
    <div class="tile"><b>{'—' if disc is None else f'{disc:.0f}%'}</b><span>discoverable</span></div>
    <div class="tile"><b>{'—' if gen is None else f'{gen:.0f}%'}</b><span>generate stage passed</span></div>
    <div class="tile"><b>{sum(1 for r in data if r['status'] != 'done')}</b><span>non-clean trials</span></div>
  </div>
</header>''')

    # ── legend ────────────────────────────────────────────────────────────
    A('<div class="legend">' + "".join(
        f'<span class="key"><i style="--c:{colour[m]};--cd:{dark[m]}"></i>{html.escape(m)}</span>'
        for m in models) + '</div>')

    # ── leaderboard ───────────────────────────────────────────────────────
    A('<section><h2>L0 score by model</h2>'
      '<p class="note">Deterministic checks only — no judge. Executability weighted 0.9, '
      'communication 0.1. Bars are means; the spread underneath is what actually matters.</p>')
    for m in ranked:
        scores = [t["l0_score"] or 0 for t in by_model[m]]
        mean = statistics.fmean(scores)
        lo, hi = min(scores), max(scores)
        A(f'<div class="lb"><span class="lb-name">{html.escape(m)}</span>'
          f'<span class="lb-bar">{bar(mean * 100, colour[m], f"{mean:.2f}", 100)}</span>'
          f'<span class="lb-meta">n={len(scores)} · range {lo:.2f}–{hi:.2f}</span></div>')
    A('</section>')

    # ── check matrix (the table view the contrast WARN obliges) ───────────
    A('<section><h2>Check pass rates</h2>'
      '<p class="note">Every cell is a percentage of that model\'s trials in this run. '
      'A dash means the check could not be evaluated — usually because an earlier step '
      'produced nothing to check.</p><div class="scroller"><table><thead><tr><th>check</th>'
      + "".join(f'<th>{html.escape(m)}</th>' for m in ranked) + '</tr></thead><tbody>')
    for key, label in CHECKS:
        A(f'<tr><td class="k">{label}</td>')
        for m in ranked:
            v = pct([t["checks"].get(key) for t in by_model[m]])
            cls = "" if v is None else (" good" if v >= 75 else " bad" if v <= 25 else "")
            A(f'<td class="num{cls}">{"—" if v is None else f"{v:.0f}%"}</td>')
        A('</tr>')
    A('</tbody></table></div></section>')

    # ── per task ──────────────────────────────────────────────────────────
    A('<section><h2>L0 by task</h2><div class="grid">')
    for task in tasks:
        A(f'<div class="card"><h3>{html.escape(task)}</h3>')
        for m in ranked:
            cell = [t["l0_score"] or 0 for t in by_model[m] if t["task"] == task]
            if not cell:
                continue
            A(f'<div class="mini"><span class="mini-name">{html.escape(m.split("-")[0])}</span>'
              + bar(statistics.fmean(cell) * 100, colour[m], f"{statistics.fmean(cell):.2f}") + '</div>')
        A('</div>')
    A('</div></section>')

    # ── repeat independence ───────────────────────────────────────────────
    A('<section><h2>Repeat independence</h2>'
      '<p class="note">Repeats whose assistant text hashes identically came from the provider\'s '
      'cache, not from independent sampling — reporting a variance of zero for those would be '
      'reporting the endpoint, not the model.</p><div class="scroller"><table><thead><tr>'
      '<th>model</th><th>task</th><th>n</th><th>distinct outputs</th><th>L0 range</th><th>verdict</th>'
      '</tr></thead><tbody>')
    for m in ranked:
        for task in tasks:
            cell = [t for t in by_model[m] if t["task"] == task]
            if len(cell) < 2:
                continue
            hashes = {t["output_hash"] for t in cell if t["output_hash"]}
            sc = [t["l0_score"] or 0 for t in cell]
            cached = len(hashes) <= 1
            A(f'<tr><td class="k">{html.escape(m)}</td><td class="k">{html.escape(task)}</td>'
              f'<td class="num">{len(cell)}</td><td class="num">{len(hashes)}</td>'
              f'<td class="num">{min(sc):.2f}–{max(sc):.2f}</td>'
              f'<td class="{"bad" if cached else ""}">'
              f'{"cached — variance unmeasurable" if cached else f"stdev {statistics.pstdev(sc):.3f}"}</td></tr>')
    A('</tbody></table></div></section>')

    # ── latency ───────────────────────────────────────────────────────────
    A('<section><h2>Wall clock per trial</h2>'
      '<p class="note">A model that scores the same but takes fifteen times longer is not '
      'equivalent in a chat interface where someone is watching.</p>')
    worst = max([t["wall_s"] or 0 for t in data] or [1])
    for m in ranked:
        w = [t["wall_s"] or 0 for t in by_model[m]]
        A(f'<div class="lb"><span class="lb-name">{html.escape(m)}</span>'
          f'<span class="lb-bar">{bar(statistics.median(w), colour[m], f"{statistics.median(w):.0f}s", worst)}</span>'
          f'<span class="lb-meta">median · range {min(w):.0f}–{max(w):.0f}s</span></div>')
    A('</section>')

    # ── flags ─────────────────────────────────────────────────────────────
    hits = [(r, k, lbl) for r in data for k, lbl in FLAGS if r["checks"].get(k)]
    bad = [r for r in data if r["status"] != "done"]
    A('<section><h2>Flags</h2>')
    if not hits and not bad:
        A('<p class="note">Nothing flagged in this run.</p>')
    else:
        A('<div class="scroller"><table><thead><tr><th>flag</th><th>trial</th><th>detail</th>'
          '</tr></thead><tbody>')
        for r, key, lbl in hits:
            extra = ", ".join(r["checks"].get("stray_targets", [])[:2]) if key.startswith("wrote_") else ""
            if key == "turn_collapsed":
                extra = f'{r["checks"].get("malformed_tool_calls", 0)} malformed calls'
            A(f'<tr><td class="k">{lbl}</td><td class="k">{html.escape(r["trial_id"])}</td>'
              f'<td class="muted">{html.escape(extra)}</td></tr>')
        for r in bad:
            A(f'<tr><td class="k bad">{html.escape(r["status"])}</td>'
              f'<td class="k">{html.escape(r["trial_id"])}</td>'
              f'<td class="muted">{html.escape((r["error"] or "")[:90])}</td></tr>')
        A('</tbody></table></div>')
    A('</section>')

    # ── all runs ──────────────────────────────────────────────────────────
    A('<section><h2>All runs in this store</h2><div class="scroller"><table><thead><tr>'
      '<th>run</th><th>trials</th><th>models</th><th>mean L0</th><th>discoverable</th>'
      '<th>knowledge</th></tr></thead><tbody>')
    for rid in all_runs:
        rr = rows(db, rid)
        if not rr:
            continue
        d = pct([x["checks"].get("discoverable") for x in rr])
        A(f'<tr><td class="k{" cur" if rid == run_id else ""}">{html.escape(rid)}</td>'
          f'<td class="num">{len(rr)}</td>'
          f'<td class="num">{len({x["model"] for x in rr})}</td>'
          f'<td class="num">{statistics.fmean([x["l0_score"] or 0 for x in rr]):.2f}</td>'
          f'<td class="num">{"—" if d is None else f"{d:.0f}%"}</td>'
          f'<td class="k muted">{html.escape(str(next((x["knowledge_sha"] for x in rr if x["knowledge_sha"]), "—")))}</td></tr>')
    A('</tbody></table></div></section>')

    A(f'<footer>generated by <code>tools/dashboard.py</code> · {generated} · '
      f'static snapshot of <code>runs/results.db</code></footer>')
    return "\n".join(P)


CSS = """
<style>
:root{
  --ground:#FCFCFB; --surface:#FFFFFF; --surface-2:#F1F2F0;
  --ink:#141A19; --ink-2:#4A5654; --ink-3:#7B8785; --rule:#E0E3E0; --rule-2:#C9CFCC;
  --accent:#0C6360; --good:#1E7A4F; --bad:#A33329;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --ground:#141514; --surface:#1C1E1D; --surface-2:#232726;
  --ink:#E9EDEB; --ink-2:#AEBAB7; --ink-3:#7F8B89; --rule:#2A2F2E; --rule-2:#3A413F;
  --accent:#55C4B9; --good:#5CB183; --bad:#E0776A;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
}}
:root[data-theme="dark"]{
  --ground:#141514; --surface:#1C1E1D; --surface-2:#232726;
  --ink:#E9EDEB; --ink-2:#AEBAB7; --ink-3:#7F8B89; --rule:#2A2F2E; --rule-2:#3A413F;
  --accent:#55C4B9; --good:#5CB183; --bad:#E0776A;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font-family:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif;font-size:15px;line-height:1.6}
.wrap{max-width:1060px;margin:0 auto;padding:0 26px 80px}
code{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.87em;
  background:var(--surface-2);padding:1px 5px;border-radius:2px}
.mast{padding:48px 0 30px;border-bottom:1px solid var(--rule)}
.eyebrow{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;letter-spacing:.15em;
  text-transform:uppercase;color:var(--accent);margin:0 0 14px;font-weight:500}
h1{font-family:"IBM Plex Serif",Georgia,serif;font-weight:600;font-size:clamp(28px,4vw,40px);
  margin:0 0 10px;letter-spacing:-.02em}
.sub{color:var(--ink-2);margin:0;font-size:14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);margin-top:26px}
.tile{background:var(--surface);padding:14px 16px}
.tile b{display:block;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:24px;
  font-weight:600;font-variant-numeric:tabular-nums;line-height:1.15}
.tile span{display:block;margin-top:4px;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--ink-3);font-family:"IBM Plex Mono",ui-monospace,monospace}
.banner{background:var(--surface-2);border:1px solid var(--rule-2);
  border-left:3px solid var(--accent);padding:12px 16px;margin:22px 0 0;font-size:13.5px;color:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:18px;padding:18px 0 0;font-size:13px}
.key{display:flex;align-items:center;gap:7px;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px}
.key i{width:11px;height:11px;border-radius:2px;background:var(--c);display:inline-block;flex:none}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]) .key i{background:var(--cd)}}
:root[data-theme="dark"] .key i{background:var(--cd)}
section{margin:44px 0 0}
h2{font-family:"IBM Plex Serif",Georgia,serif;font-size:21px;font-weight:600;margin:0 0 6px;letter-spacing:-.01em}
h3{font-size:13px;font-weight:600;margin:0 0 10px;font-family:"IBM Plex Mono",ui-monospace,monospace;
  color:var(--ink-2);letter-spacing:.03em}
.note{color:var(--ink-2);font-size:13.5px;max-width:66ch;margin:0 0 18px}
.lb{display:grid;grid-template-columns:180px 1fr 190px;gap:14px;align-items:center;
  padding:9px 0;border-bottom:1px solid var(--rule)}
.lb:last-child{border-bottom:none}
.lb-name{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12.5px}
.lb-meta{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;color:var(--ink-3);
  font-variant-numeric:tabular-nums;text-align:right}
.bar-row{display:flex;align-items:center;gap:10px}
.bar-track{flex:1;height:8px;background:var(--surface-2);border-radius:4px;overflow:hidden;display:block}
.bar-fill{display:block;height:100%;border-radius:0 4px 4px 0}
.bar-val{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;
  font-variant-numeric:tabular-nums;min-width:44px;text-align:right;color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.card{background:var(--surface);border:1px solid var(--rule);padding:15px 16px}
.mini{display:grid;grid-template-columns:74px 1fr;gap:10px;align-items:center;margin-bottom:7px}
.mini-name{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;color:var(--ink-2)}
.scroller{overflow-x:auto;border:1px solid var(--rule);background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:520px}
th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--rule)}
thead th{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ink-3);font-weight:500;background:var(--surface-2);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
td.k{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px}
td.num{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums;text-align:right}
td.good{color:var(--good);font-weight:600}
td.bad,.bad{color:var(--bad)}
.cur{color:var(--accent);font-weight:600}
.muted{color:var(--ink-3)}
footer{margin-top:52px;padding-top:20px;border-top:1px solid var(--rule);
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;color:var(--ink-3)}
</style>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--in-progress", action="store_true",
                    help="mark the run as still being written")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    all_runs = [r[0] for r in db.execute("SELECT DISTINCT run_id FROM trials ORDER BY run_id")]
    if not all_runs:
        raise SystemExit("no runs in the store")
    run_id = args.run_id or all_runs[-1]

    body = build(db, run_id, all_runs, args.in_progress)
    db.close()
    page = (
        "<title>Prompt Lab Results</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600'
        '&family=IBM+Plex+Serif:wght@600&display=swap">\n'
        + CSS + f'<div class="wrap">{body}</div>\n'
    )
    with open(args.out, "w") as fh:
        fh.write(page)
    print(f"wrote {args.out} ({len(page)} bytes, run {run_id})")


if __name__ == "__main__":
    main()
