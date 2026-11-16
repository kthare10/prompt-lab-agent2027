# prompt-lab

Evaluation harness, task suite, and per-trial records for the paper
*Evaluating Frontier-Model Agent Skills with Open-Weight Models in a Science Gateway*
(AGENT 2027 workshop submission).

The harness drives PegasusAI Studio through its chat API as a user would, lets a model build a
Pegasus workflow project with the PegasusAI plugin's skills, and grades the result twice: a
mechanical score (L0) and a rubric score from a judge model (L1).

## Contents

| Path | What it holds |
|---|---|
| `harness/` | Trial driver, sweep runner, simulated user, mechanical grader, judge, results store, replay (`reflow.py`) |
| `tasks/` | The 17 tasks (12 build, 5 judgment) with scripted user replies and grading criteria |
| `fixtures/` | Inputs the tasks seed into the workspace |
| `tests/` | Unit tests for the grader and the simulated user |
| `scripts/` | The baseline sweep launchers and the examples block used in the case study |
| `tools/` | Reporting and workspace tools the harness and tests use |
| `data/trials.csv` | One row per trial for every run cited in the paper |
| `data/judge_dimensions.csv` | The judge's score on each rubric dimension, per trial |
| `data/case_study_pairs.csv`, `data/case_study_repeat.csv` | The 48 paired L0 scores of the examples case study and of the repeated shipped gateway |
| `analysis/paper_numbers.py` | Recomputes Table I, the pooled scores, the paired comparisons, and the stage counts |
| `analysis/case_study.py` | Recomputes the case-study differences |

## Reproducing the numbers

    python3 analysis/paper_numbers.py
    python3 analysis/case_study.py

The scripts need only the Python standard library and read the files in `data/`.

## Columns of `data/trials.csv`

- `run_id`, `trial_id`, `task`, `model`, `provider`, `repeat`: what was run. `depth` is `none` for
  judgment tasks and `generate`, `build`, or `plan` for build tasks, naming the last stage graded.
- `status`, `turns`, `wall_s`, token and step counts: how the trial went.
- `l0_score`, `l1_score`, `judge_model`: the two scores.
- `wrote_files` through `vocabulary_score`: the mechanical checks behind L0.
- `stage_generate`, `stage_build`, `stage_plan`: stage outcomes at trial time. Every container build
  failed at trial time because the evaluation host lacked the user namespace Apptainer needs.
- `replay_generate`, `replay_build`, `replay_plan`: outcomes when the same stored projects were
  replayed later with user namespaces enabled and without calling a model. L0 and L1 predate the replay.
- `paper_cohort`: 1 for the 21 open-weight models and Claude reported in the paper.
- `ended_on_unanswered_request`: 1 for the two trials left out of the pooled scores.

## Runs

| Run ids | Role in the paper |
|---|---|
| `s1-azure`, `s1-nrp`, `s1-fabric`, `s1-jetstream` | Baseline, one trial per task |
| `s1-claude` | Claude Opus 5.5 reference |
| `s1-anchor-*`, `s1rep-*` | Repeated build trials (`s1rep-nrp-kimi` stopped when its endpoint stopped responding) |
| `s1-claude-t05a-fix`, `s1-kimi-t05a-fix` | Single re-runs after the question detector was fixed |
| `s2b-*`, `s2c-*`, `s2d-*`, `s2e-*` | Arms of the examples case study |

## Tests

Each module runs on its own, for example `python3 -m tests.test_dag_shape`. `test_sim_user` and one case
of `test_reasoning` read stored transcripts and do not run from this release.

## What is not here

Transcripts, the files the agents wrote, and the judge's free-text evidence are not included in
this release. The harness expects a running PegasusAI Studio instance and model endpoints of your own.

## License

Apache License 2.0. See `LICENSE`.
