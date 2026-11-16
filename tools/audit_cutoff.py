"""Which recorded trials did the harness hang up on?

Ceiling #10: `looks_like_question` was `"?" in text[-400:]`, so a model that
finished a turn waiting on the user — without a question mark in its last few
sentences — had its trial ended after that turn. It then scored as though it had
chosen to stop.

This finds trials where the *fixed* rule says the model was waiting but the old
rule did not, which is exactly the population that was cut short.

    python3 -m tools.audit_cutoff
"""

from __future__ import annotations

import json
import os

from harness.store import RUNS_DIR
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.trial import MAX_TURNS, looks_like_question  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    db = sqlite3.connect(f"file:{os.path.join(ROOT, 'runs', 'results.db')}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM trials")]

    suspect, clean, skipped = [], 0, 0
    for r in rows:
        if r["run_id"].startswith("gptoss-"):        # already re-run under the fix
            skipped += 1
            continue
        path = os.path.join(RUNS_DIR, r["run_id"], r["trial_id"], "transcript.jsonl")
        if not os.path.isfile(path):
            skipped += 1
            continue
        turns = [json.loads(l)["payload"] for l in open(path)
                 if json.loads(l)["kind"] == "assistant"]
        if not turns:
            skipped += 1
            continue
        text = (turns[-1].get("text") or "").strip()
        old_rule = "?" in text[-400:]
        # Cut short only if the model was waiting, the old rule missed it, and the
        # trial had turns left to spend.
        if looks_like_question(text) and not old_rule and len(turns) < MAX_TURNS:
            tools = sum(len(t.get("tool_calls") or []) for t in turns)
            suspect.append((r, len(turns), tools))
        else:
            clean += 1

    print(f"examined {len(rows)}  |  unaffected {clean}  |  cut short {len(suspect)}  "
          f"|  skipped {skipped}")
    print()
    hdr = ("run", "task", "model", "turns", "tools", "l0")
    print("%-16s %-6s %-18s %5s %5s %6s" % hdr)
    for r, t, tc in sorted(suspect, key=lambda x: (x[0]["task"], x[0]["l0_score"] or 0)):
        print("%-16s %-6s %-18s %5d %5d %6.3f"
              % (r["run_id"], r["task"][:5], r["model"], t, tc, r["l0_score"] or 0))

    by_task: dict[str, int] = {}
    for r, _, _ in suspect:
        by_task[r["task"]] = by_task.get(r["task"], 0) + 1
    print()
    for task, n in sorted(by_task.items()):
        print(f"  {task:28s} {n} cut short")


if __name__ == "__main__":
    main()
