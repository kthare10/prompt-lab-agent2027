#!/usr/bin/env bash
# Stage 1 on the three providers Jetstream did not cover, serially (D5: never two
# trials against one container).
#
# Smallest provider first, deliberately: Azure is one model and FABRIC four, so
# both finish and become judgeable while NRP's twelve-model tail is still
# running. A problem found in hour 12 of NRP costs less if two providers are
# already banked.
#
# Each provider is smoked on t00 through the full stack before its sweep. The
# raw-endpoint probe this replaces is not enough — `llama-4-scout` returned
# native tool calls to a nine-tool probe and none at all through Studio with the
# real 39k-token prompt, and `gemma-4-31b-it` spent seventeen cells on a vLLM
# host that was simply down. Fifteen minutes here is cheap against twenty hours.
set -uo pipefail

C=pegasus-studio
SMOKE_RUN=${SMOKE_RUN:-smoke-rest}     # never s1-*: `cohort.py` pools the newest
                                       # trial per (task, model) across s1-%, so
                                       # a smoke cell under an s1 id would silently
                                       # become the measured one.
TASKS=$(cd "$(dirname "$0")/.." && python3 -c "
import yaml, glob
print(','.join(sorted(yaml.safe_load(open(f))['id'] for f in glob.glob('tasks/t*.yaml'))))")

# provider-id : smoke model : full model list
AZURE_MODELS='Kimi-K2.6'
FABRIC_MODELS='gpt-oss-20b,minimax-m2.7,nemotron-nano-30b,qwen3.5-122b'
# Everything the endpoint serves except `qwen3-embedding`, which is not a chat
# model. `gemma-small-e4b` is kept although it is served with a 16k context the
# harness prompt does not fit: Stage 1 named it excluded-from-ranking rather than
# dropping it, and a gap in the store records nothing.
NRP_MODELS='deepseek-v4-flash,gemma,gemma-small,gemma-small-e4b,gemma4-12b,gemma4-small,glm-5,gpt-oss,kimi,minimax-m2,qwen3,qwen3-small'

run () { docker exec $C bash -lc "cd /opt/prompt-lab && $1"; }

smoke () {  # $1 = provider id, $2 = a model that should work
  echo "── smoke $1 ($2)"
  local out
  out=$(run "python3 -m harness.trial --task t00-hello-pegasus --provider $1 \
        --model $2 --depth auto --backend opencode --run-id $SMOKE_RUN" 2>&1 | tail -40)
  # The bar is "did this reach a model", not "did it finish the task". A
  # `turn_timeout` means the model engaged and outran a harness budget, which
  # PLAN keeps distinct from `model_error` precisely because it is not
  # misbehaviour — and gating on `done` alone threw away FABRIC's entire
  # 68-cell sweep after minimax-m2.7 spent 126 tool calls and 101,741 tokens on
  # t00 before hitting the ceiling. The provider was never in doubt.
  #
  # `input_tokens > 0` is the same test `cohort.py` uses to decide a cell never
  # reached a model, so the gate and the analysis agree on what "reached" means.
  local line verdict
  line=$(printf '%s' "$out" | python3 -c "
import sys, json
s = sys.stdin.read(); end = s.rfind('}') + 1
d = None
for i, ch in enumerate(s):
    if ch != '{':
        continue
    try:
        d = json.loads(s[i:end]); break
    except Exception:
        continue
try:
    if d is None:
        raise ValueError('no JSON record in output')
except Exception:
    print('dead|UNPARSEABLE trial output'); raise SystemExit
tok = (d.get('tokens') or {}).get('input') or 0
st = d.get('status')
alive = st == 'done' or tok > 0
print(f\"{'alive' if alive else 'dead'}|{st} l0={d.get('l0_score')} \"
      f\"tools={d.get('tool_calls')} tokens={tok}\")")
  verdict=${line%%|*}
  echo "   -> ${line#*|}"
  if [ "$verdict" = alive ]; then return 0; fi
  echo "   !! $1 never reached a model — skipping its sweep"
  return 1
}

sweep () {  # $1 = run id, $2 = provider id, $3 = models
  echo "══ sweep $1 ($2) ══  $(date -u +%H:%M) UTC"
  run "python3 -m harness.sweep --run-id $1 --provider $2 --models $3 \
       --tasks $TASKS --repeats 1 --depth auto --backend opencode"
  echo "══ $1 finished $(date -u +%H:%M) UTC"
}

# knowledge_sha must not move under a multi-day sweep: the marketplace lives in
# the container's $HOME, so the models under test can write to it.
M="${STUDIO_HOME:-$HOME/studio-home}/.local/share/pegasus-ai/marketplace"
if [ -n "$(git -C "$M" status --porcelain 2>/dev/null)" ]; then
  echo "!! marketplace is dirty at $(git -C "$M" rev-parse --short HEAD) — a sweep"
  echo "!! started now would straddle two knowledge bases. Clean it first."
  exit 1
fi
echo "knowledge_sha $(git -C "$M" rev-parse --short HEAD), tasks: $(echo "$TASKS" | tr ',' '\n' | wc -l)"

smoke custom-azure  Kimi-K2.6   && AZ=1  || AZ=0
smoke custom-fabric minimax-m2.7 && FA=1 || FA=0
smoke custom-nrp    kimi         && NR=1 || NR=0

[ "$AZ" = 1 ] && sweep s1-azure  custom-azure  "$AZURE_MODELS"
[ "$FA" = 1 ] && sweep s1-fabric custom-fabric "$FABRIC_MODELS"
[ "$NR" = 1 ] && sweep s1-nrp    custom-nrp    "$NRP_MODELS"
echo "=== STAGE 1 (remaining providers) COMPLETE ==="
