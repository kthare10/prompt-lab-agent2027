"""SQLite store for trial results.

One row per trial, with the checks kept as JSON rather than columns: L0 grows
every milestone, and a schema migration per new check is friction that would
discourage adding them. Queries that need a specific check pull it out with
json_extract.

Writes are keyed on trial_id, so a re-run of the same trial replaces its row —
which is what makes a killed sweep resumable rather than duplicating work.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    trial_id      TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    task          TEXT NOT NULL,
    exposure      TEXT,
    skill         TEXT,
    agent         TEXT,
    provider      TEXT,
    model         TEXT,
    depth         TEXT,
    repeat        INTEGER DEFAULT 0,
    status        TEXT NOT NULL,
    error         TEXT,
    knowledge_sha TEXT,
    -- The marketplace checkout is only half the knowledge. STUDIO_ENV.md ships
    -- in the studio-webapp image, and a change to it moves what every model is
    -- told without touching knowledge_sha. Comparing across a rebuild without
    -- this is comparing across an unrecorded variable.
    overlay_sha   TEXT,
    approval_mode TEXT,
    -- The per-turn ceiling this cell ran under. A `turn_timeout` is a censored
    -- result and this is what it was censored at; NULL means the 2400s default,
    -- which is what every row measured before this column held.
    turn_timeout_s REAL,
    turns         INTEGER,
    wall_s        REAL,
    ttft_s        REAL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    -- Totals over every step of every turn, which is what a provider bills.
    -- `input_tokens`/`output_tokens` above are the LAST step's figures and are
    -- kept that way because the rows measured before these columns existed hold
    -- that quantity and cannot be recomputed — per-step usage is not retained.
    -- NULL here means "measured before this was recorded", never zero.
    input_tokens_total  INTEGER,
    output_tokens_total INTEGER,
    input_tokens_peak   INTEGER,
    steps               INTEGER,
    l0_score      REAL,
    output_hash   TEXT,
    checks        TEXT,
    flow          TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    -- Composite, not trial_id alone. A sweep names cells `<task>--<model>--r<n>`,
    -- which repeats across runs, so a bare trial_id key made every new run
    -- silently INSERT OR REPLACE the previous run's rows out of existence.
    PRIMARY KEY (run_id, trial_id)
);
CREATE INDEX IF NOT EXISTS trials_cell ON trials(run_id, model, task);

-- L1, kept in its own table rather than as columns on `trials`: a trial can be
-- graded by more than one judge (D4 double-grades 10% with a judge from a
-- different provider, so inter-rater agreement can be reported), and a judge
-- model is pinned per row because a silent judge upgrade invalidates every
-- comparison against earlier runs.
CREATE TABLE IF NOT EXISTS judgements (
    run_id      TEXT NOT NULL,
    trial_id    TEXT NOT NULL,
    judge_model TEXT NOT NULL,
    judged_at   TEXT,
    input_hash  TEXT,        -- artifacts + transcript; lets a re-judge be skipped
    l1_score    REAL,
    dimensions  TEXT,        -- JSON: per-dimension score, evidence, source
    criteria    TEXT,        -- JSON: the task's must / must_not, adjudicated
    summary     TEXT,
    error       TEXT,
    PRIMARY KEY (run_id, trial_id, judge_model)
);
CREATE INDEX IF NOT EXISTS judgements_trial ON judgements(run_id, trial_id);
"""

# Where results live. Overridable so they can sit outside the code tree: the
# code is mounted read-only in the container, but `runs/` must stay writable, so
# nesting it under the code means one `rm -rf` of the tree still empties it —
# which is how a model under test destroyed a sweep's results on 2026-09-05.
# Defined once here and imported, rather than recomputed in seven modules.
RUNS_DIR = os.environ.get("PROMPT_LAB_RUNS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs"
)

DEFAULT_PATH = os.path.join(RUNS_DIR, "results.db")


class Store:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Move a pre-composite-key table across, keeping whatever survived."""
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(trials)")]
        # Additive columns are added in place: rebuilding the table would be a
        # lot of machinery for a nullable field, and old rows genuinely have no
        # value for it — NULL is the honest answer, not a back-filled guess.
        for name, decl in (("overlay_sha", "TEXT"),
                           ("input_tokens_total", "INTEGER"),
                           ("output_tokens_total", "INTEGER"),
                           ("input_tokens_peak", "INTEGER"),
                           ("steps", "INTEGER"),
                           ("turn_timeout_s", "REAL")):
            if cols and name not in cols:
                self.db.execute(f"ALTER TABLE trials ADD COLUMN {name} {decl}")
                self.db.commit()
                cols.append(name)
        pk = [r[5] for r in self.db.execute("PRAGMA table_info(trials)")]
        if not cols or sum(1 for x in pk if x) > 1:
            return  # already composite
        self.db.executescript(
            "ALTER TABLE trials RENAME TO trials_old;"
            + SCHEMA
            + "INSERT OR IGNORE INTO trials SELECT * FROM trials_old;"
            "DROP TABLE trials_old;"
        )
        self.db.commit()

    @staticmethod
    def _json_field(value: Any) -> Any:
        """Decode a field that may already be a JSON string.

        `save()` has to be idempotent: regrade reads a row, edits it and saves it
        back, so a field that arrives as a string must not be re-encoded. Doing
        that nested `flow` one level deeper on every regrade pass until it was
        unreadable. Decode until it stops being a string, then let the caller
        encode exactly once.
        """
        seen = 0
        while isinstance(value, str) and seen < 12:
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                break
            seen += 1
        return value

    def save(self, record: dict[str, Any]) -> None:
        checks = self._json_field(record.get("checks")) or {}
        tokens = record.get("tokens") or {}
        row = {
            "trial_id": record["trial_id"],
            "run_id": record["run_id"],
            "task": record["task"],
            "exposure": record.get("exposure"),
            "skill": record.get("skill"),
            "agent": record.get("agent"),
            "provider": record.get("provider"),
            "model": record.get("model"),
            "depth": record.get("depth"),
            "repeat": record.get("repeat", 0),
            "status": record.get("status", "unknown"),
            "error": record.get("error"),
            "knowledge_sha": record.get("knowledge_sha"),
            "overlay_sha": record.get("overlay_sha"),
            "approval_mode": record.get("approval_mode"),
            "turn_timeout_s": record.get("turn_timeout_s"),
            "turns": record.get("turns"),
            "wall_s": record.get("wall_s"),
            "ttft_s": record.get("ttft_s"),
            "input_tokens": tokens.get("input"),
            "output_tokens": tokens.get("output"),
            "input_tokens_total": tokens.get("input_total"),
            "output_tokens_total": tokens.get("output_total"),
            "input_tokens_peak": tokens.get("input_peak"),
            "steps": tokens.get("steps"),
            "l0_score": record.get("l0_score"),
            "output_hash": checks.get("output_hash"),
            "checks": json.dumps(checks),
            "flow": json.dumps(self._json_field(record.get("flow")) or {}),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
        }
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        self.db.execute(f"INSERT OR REPLACE INTO trials ({cols}) VALUES ({marks})", row)
        self.db.commit()

    def save_judgement(self, rec: dict[str, Any]) -> None:
        cols = ("run_id", "trial_id", "judge_model", "judged_at", "input_hash",
                "l1_score", "dimensions", "criteria", "summary", "error")
        row = dict(rec)
        for k in ("dimensions", "criteria"):
            row[k] = json.dumps(self._json_field(row.get(k)))
        self.db.execute(
            f"INSERT OR REPLACE INTO judgements ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [row.get(c) for c in cols],
        )
        self.db.commit()

    def judgements(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if run_id:
            return list(self.db.execute(
                "SELECT * FROM judgements WHERE run_id = ?", (run_id,)))
        return list(self.db.execute("SELECT * FROM judgements"))

    def done_cells(self, run_id: str) -> set[tuple[str, str, int]]:
        """(task, model, repeat) already completed — what makes a sweep resumable."""
        rows = self.db.execute(
            "SELECT task, model, repeat FROM trials WHERE run_id = ? AND status = 'done'",
            (run_id,),
        )
        return {(r["task"], r["model"], r["repeat"]) for r in rows}

    def trials(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if run_id:
            return list(self.db.execute("SELECT * FROM trials WHERE run_id = ? ORDER BY trial_id", (run_id,)))
        return list(self.db.execute("SELECT * FROM trials ORDER BY run_id, trial_id"))

    def runs(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT run_id FROM trials ORDER BY run_id")]

    def close(self) -> None:
        self.db.close()
