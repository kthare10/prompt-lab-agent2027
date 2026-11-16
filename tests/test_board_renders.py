"""Does the board survive the records it actually has to render?

A failed judgement stores `criteria` as the literal `null`. The page guarded
that with `json.loads(x or "{}")` — but the guard runs *before* the parse, and
`"null"` is a non-empty string, so it parsed to None and the next line raised
`AttributeError: 'NoneType' object has no attribute 'get'`. Three upstream
stream disconnections were enough to take the whole leaderboard down.

    python3 -m tests.test_board_renders
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import serve                                            # noqa: E402
from harness.store import SCHEMA                        # noqa: E402


def main() -> None:
    fails = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        con = sqlite3.connect(path)
        con.executescript(SCHEMA)
        con.execute(
            "INSERT INTO trials (run_id,trial_id,task,model,provider,status,l0_score,checks,started_at)"
            " VALUES ('r','x','t00-hello-pegasus','m','p','done',0.5,'{}','2026-01-01')")
        # The shapes a judgement can take on disk: a good one, one that failed
        # (null criteria), and one whose criteria are an empty string.
        for tid, crit, score, err in (
                ("x", '{"must":[{"met":true}],"must_not":[{"violated":false}]}', 0.7, None),
                ("y", "null", None, "codex exit 1: stream disconnected"),
                ("z", "", None, "boom")):
            if tid != "x":
                con.execute(
                    "INSERT INTO trials (run_id,trial_id,task,model,provider,status,l0_score,checks,started_at)"
                    " VALUES ('r',?,?,?,'p','done',0.4,'{}','2026-01-02')",
                    (tid, f"t01-sensor-summary", f"m{tid}"))
            con.execute(
                "INSERT INTO judgements (run_id,trial_id,judge_model,judged_at,input_hash,"
                "l1_score,dimensions,criteria,summary,error) VALUES ('r',?,'j','now','h',?,'{}',?,'s',?)",
                (tid, score, crit, err))
        con.commit(); con.close()

        try:
            cells = serve.latest_by_cell(serve.open_ro(path))
            check("latest_by_cell survives a failed judgement", True)
        except Exception as e:
            check("latest_by_cell survives a failed judgement", False, f"{type(e).__name__}: {e}")
            cells = {}

        got = {k[1]: c for k, c in cells.items()}
        check("good judgement still counted",
              got.get("m", {}).get("l1_score") == 0.7, str(got.get("m", {}).get("l1_score")))
        check("failed judgement scores None",
              got.get("my", {}).get("l1_score") is None)
        check("failed judgement counts no criteria",
              got.get("my", {}).get("must_unmet") == 0
              and got.get("my", {}).get("must_not_violated") == 0)

        for name, fn in (("board", serve.board), ("judge page", serve.judge_page)):
            try:
                html = fn(path)
                check(f"{name} renders", len(html) > 0 and html.count("<section") == html.count("</section>"))
            except Exception as e:
                check(f"{name} renders", False, f"{type(e).__name__}: {e}")

    print(f"\n{6 - fails}/6 passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
