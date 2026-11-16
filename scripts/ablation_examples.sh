#!/usr/bin/env bash
# Stage 2, ablation 1 — does unlocking the shipped examples change anything?
#
# PLAN §8 item 0. Every skill tells the model to read
# `assets/examples/workflow_generator_tnseq.py`, and `_safe_path` refuses anything
# outside the workspace (C9), so step 2 of four skills fails for every model. The
# `on` arm copies the plugin's `assets/` into the workspace, which makes the
# skills' own instruction true **verbatim** — same relative path, now resolvable.
# No knowledge text changes, which is what keeps this an ablation of reachability
# rather than of wording.
#
# Stage 1 could not answer this. Across it, cells that reached the examples scored
# +0.175 higher within a task and -0.096 lower within a model, and -0.022 (p 0.28)
# adjusted for both — a textbook reversal, because the models chose their own
# treatment. Randomising access is the only way to get a causal answer.
#
# **Both arms run `--clean-workspace on`.** An `off` arm is not example-free while
# a previous model's finished generator sits next door, and 391 trials had left
# 2.9 GB of exactly that. This is the blocker PLAN recorded against this ablation.
#
# Tasks, chosen for what they test rather than for coverage:
#   t02, t12  the `pointed` pair — a skill names the exact exemplar by filename
#             and `read_file` cannot open it. PLAN calls these "the most
#             diagnostic thing in the suite": if their scores jump, C9 is costing
#             real quality; if not, the pointer was never worth anything.
#   t05a      `exemplified` — a shipped generator implements this exact pipeline,
#             so it is the memorisation control.
#   t01       `documented` — the ordinary case, as a baseline.
set -uo pipefail

C=pegasus-studio
TASKS=${TASKS:-t01-sensor-summary,t02-tile-reduce,t05a-snakemake-tnseq,t12-federated-rounds}
N=${N:-3}
# glm-5 needed 5,875s on a single t05a turn and was censored seven times at 2400.
# Both arms get the same ceiling, and it is recorded per trial, so a timeout is
# not silently a score in one arm and a lower bound in the other.
BUDGET=${BUDGET:-7200}

# provider : models
NRP='deepseek-v4-flash,glm-5,qwen3-small'
AZURE='Kimi-K2.6'
JET='llama-4-scout'

# Arms are parameters: ablation 1 ran off-vs-on and showed only that C9 does not
# hold here, so the real comparison is off-vs-block. Defaults are the live one.
ARM_A=${ARM_A:-off}; RUN_A=${RUN_A:-s2-examples-off}
ARM_B=${ARM_B:-block}; RUN_B=${RUN_B:-s2-examples-block}

HERE=$(cd "$(dirname "$0")" && pwd)
# The block is applied from the host, as root in the container, for the length
# of a block arm and lifted after it: a block the harness applied under its own
# uid was undone by the model in 9 of 60 cells (s2b). `trial.py` refuses to run
# a block cell while any copy is readable, and an off cell while one is not.
trap '"$HERE/examples_block.sh" off >/dev/null 2>&1 || true' EXIT

arm () {  # $1 = run id, $2 = examples mode, $3 = provider, $4 = models
  echo "══ $1  examples=$2  $3  $(date -u +%H:%M) ══"
  if [ "$2" = block ]; then "$HERE/examples_block.sh" on; else "$HERE/examples_block.sh" off; fi
  docker exec $C bash -lc "cd /opt/prompt-lab && python3 -m harness.sweep \
    --run-id $1 --provider $3 --models $4 --tasks $TASKS --repeats $N \
    --depth auto --backend opencode --clean-workspace on --examples $2 \
    --turn-timeout $BUDGET"
  [ "$2" = block ] && "$HERE/examples_block.sh" off
}

M="${STUDIO_HOME:-$HOME/studio-home}/.local/share/pegasus-ai/marketplace"
if [ -n "$(git -C "$M" status --porcelain 2>/dev/null)" ]; then
  echo "!! marketplace is dirty — the assets being copied would not match"
  echo "!! knowledge_sha, and the arms would differ by more than the toggle."
  exit 1
fi
echo "knowledge_sha $(git -C "$M" rev-parse --short HEAD), budget ${BUDGET}s, $N repeats"

# Both arms of a provider run together rather than all-off then all-on. These
# endpoints move under a long run — Jetstream dropped GLM-5.2-FP8 mid-cohort and
# a vLLM host was down for a whole sweep — and an ablation whose arms are a day
# apart cannot tell a treatment effect from the provider having changed.
for spec in "custom-nrp|$NRP" "custom-azure|$AZURE" "custom-jetstream|$JET"; do
  prov=${spec%%|*}; models=${spec##*|}
  arm "$RUN_A" "$ARM_A" "$prov" "$models"
  arm "$RUN_B" "$ARM_B" "$prov" "$models"
done
echo "=== ABLATION 1 COMPLETE ==="
echo "compare with: python3 -m harness.compare --before $RUN_A --after $RUN_B"
