"""Serial sweep over (task x model x repeat) against one Studio container.

Serial by necessity, not by choice: `ChatTurnManager` cancels the turn in
flight, so two trials against one container corrupt each other silently (C2).
Parallelism is N containers, which arrives with `fleet.py` in M2.

Resumable: cells already recorded as `done` for this run_id are skipped, so a
sweep killed at trial 30 of 45 continues rather than restarting.

    python3 -m harness.sweep --run-id r1 --models qwen3.5-122b,gpt-oss-20b \
        --tasks t00-hello-pegasus,t01-sensor-summary --repeats 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

from harness.store import DEFAULT_PATH, Store
from harness.trial import run_trial


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", default=dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--provider", default="custom-mtd4weae")
    ap.add_argument("--models", required=True, help="comma-separated model ids")
    ap.add_argument("--tasks", required=True, help="comma-separated task ids")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--turn-timeout", type=float, default=-1.0,
                    help="wall-clock ceiling per assistant turn in seconds; "
                         "-1 uses STUDIO_TURN_TIMEOUT (default 2400), 0 disables. "
                         "A model that blows the ceiling is recorded as "
                         "turn_timeout rather than stalling the sweep (C2).")
    ap.add_argument("--depth", default="auto",
                    choices=("auto", "none", "generate", "build", "plan"))
    ap.add_argument("--approval", default="off", choices=("off", "auto"))
    ap.add_argument("--backend", default="builtin", choices=("builtin", "opencode"),
                    help="which chat engine drives the conversation")
    ap.add_argument("--session-dir", default=None,
                    help="opencode only: root every session here (default: the "
                         "engine's own, which is what an unscoped chat gets)")
    ap.add_argument("--clean-workspace", default="off", choices=("off", "on"),
                    help="archive what previous trials left before each cell; "
                         "an --examples off arm is not clean without it")
    ap.add_argument("--examples", default="off", choices=("off", "on", "block"),
                    help="the PLAN.md §8 item 0 ablation, passed through per trial")
    ap.add_argument("--site", default="condorpool")
    ap.add_argument("--output-site", default="local")
    ap.add_argument("--db", default=DEFAULT_PATH)
    ap.add_argument("--studio-url", default=os.environ.get("STUDIO_URL", "http://localhost:8888"))
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]

    store = Store(args.db)
    done = store.done_cells(args.run_id)
    store.close()

    cells = [
        (task, model, rep)
        for task in tasks
        for model in models
        for rep in range(args.repeats)
        if (task, model, rep) not in done
    ]
    total = len(tasks) * len(models) * args.repeats
    print(f"run {args.run_id}: {total} cells, {total - len(cells)} already done, {len(cells)} to run",
          flush=True)

    started = time.monotonic()
    for i, (task, model, rep) in enumerate(cells, 1):
        trial_args = argparse.Namespace(
            task=task, provider=args.provider, model=model, depth=args.depth,
            approval=args.approval, examples=args.examples,
            clean_workspace=args.clean_workspace,
            backend=args.backend, session_dir=args.session_dir,
            site=args.site, output_site=args.output_site,
            run_id=args.run_id, trial_id=f"{task}--{model}--r{rep}", repeat=rep,
            db=args.db, studio_url=args.studio_url,
            turn_timeout=args.turn_timeout,
        )
        t0 = time.monotonic()
        try:
            rec = run_trial(trial_args)
        except Exception as e:  # noqa: BLE001 — one bad cell must not end the sweep
            rec = {"status": "harness_error", "error": f"{type(e).__name__}: {e}", "l0_score": None}
            # ...but a busy container is not one bad cell. Once a turn is stuck
            # active, the C2/D5 guard refuses every subsequent trial, and the
            # sweep otherwise races through the whole remaining matrix marking
            # each cell `harness_error` in under a second. That is exactly what
            # happened to Stage 1 on 2026-09-04: one `turn_timeout` cost 229 of
            # 252 cells, and the log looked like completion.
            if "chat turn is already active" in str(e) or "still busy" in str(e):
                print(f"[{i}/{len(cells)}] {task} {model} r{rep} → ABORTING: {e}", flush=True)
                print(
                    f"stopped after {i - 1} of {len(cells)} cells — the container is "
                    "not free. Clear the stuck turn, then re-run this same command: "
                    "completed cells are skipped.",
                    flush=True,
                )
                break
        print(
            f"[{i}/{len(cells)}] {task} {model} r{rep} → {rec.get('status')} "
            f"l0={rec.get('l0_score')} ({time.monotonic() - t0:.0f}s)",
            flush=True,
        )
        # A quarantined trial means the container was not exclusively ours (C2).
        # Continuing would keep producing corrupt rows.
        if rec.get("status") == "quarantined":
            print("STOPPING: container ownership was lost — see D5.", file=sys.stderr, flush=True)
            break

    print(f"sweep {args.run_id} finished in {(time.monotonic() - started) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
