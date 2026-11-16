#!/usr/bin/env bash
# What must be true before a sweep is worth starting. Every check here is one
# that has silently invalidated a run: a missing provider config fails every
# turn (C1), a busy chat turn makes the C2/D5 guard refuse every trial, and
# grading anything requires Pegasus, which lives in the container and not on
# the host (D3).
set -u
NAME="${NAME:-pegasus-studio}"
PORT="${PORT:-8888}"
api() { curl -s "http://localhost:$PORT$1"; }
ok=0; bad=0
check() { if [ "$1" = 0 ]; then echo "  ok    $2"; ok=$((ok+1)); else echo "  FAIL  $2"; bad=$((bad+1)); fi; }

echo "preflight for $NAME:"
[ "$(api /api/health | grep -c '"status":"ok"')" = 1 ]; check $? "studio-api answers"
if [ "$bad" != 0 ]; then
    # Everything below asks the API or the container something. With it down
    # they all fail, and the nine extra lines say nothing the first one did not
    # — one of them by dropping a Python traceback into the report.
    echo
    echo "  studio-api is not answering, so the rest would only repeat that."
    docker inspect "$NAME" >/dev/null 2>&1 || { echo "  no container named $NAME."; exit 1; }
    state=$(docker inspect "$NAME" --format '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}')
    echo "  container: $state"
    echo "  start it with:  docker start $NAME    (or scripts/studio_up.sh to recreate)"
    exit 1
fi
[ "$(api /api/opencode/_studio/status | grep -c '"running":true')" = 1 ]; check $? "the chat engine is running"
# C1 is that the activated provider actually answers — not that a provider
# string exists (the image ships a `gateway` placeholder that satisfies that
# and fails every turn) and not that it holds a key (Jetstream's endpoint is
# unauthenticated, and demanding one failed a working configuration). Asking
# the provider for its model list is the only version of this question that is
# true for both, and it is what a first trial would find out the slow way.
active=$(api /api/llm/provider-configs | python3 -c '
import json, sys
try:
    cfgs = json.load(sys.stdin).get("configs") or []
except ValueError:
    cfgs = []                      # not JSON: the API is down or returned HTML
live = [c["provider_id"] for c in cfgs if c.get("is_active")]
print(live[0] if live else "")')
if [ -z "$active" ]; then
    false
else
    api "/api/llm/provider-configs/$active/models" | grep -q '"valid":true'
fi; check $? "the activated provider answers (C1)${active:+ — $active}"
[ "$(api /api/opencode/session/status)" = "{}" ]; check $? "no chat turn is active (C2/D5)"
api /api/opencode/path | grep -q '"directory":"/home/ubuntu"'; check $? "the engine roots at the workspace"

docker exec "$NAME" bash -lc 'command -v pegasus-plan >/dev/null'; check $? "pegasus-plan is present (D3)"
docker exec "$NAME" bash -lc 'command -v apptainer >/dev/null'; check $? "apptainer is present"
docker exec "$NAME" bash -lc 'python3 -c "from Pegasus.api import *"' 2>/dev/null; check $? "Pegasus.api imports"
docker exec "$NAME" bash -lc 'cd /opt/prompt-lab && python3 -c "import harness.trial"' 2>/dev/null; check $? "the harness imports in the container"
docker exec "$NAME" bash -lc 'test -w /opt/prompt-lab-runs'; check $? "results are writable"
docker exec "$NAME" bash -lc 'test ! -w /opt/prompt-lab'; check $? "the code is NOT writable (it is the thing under test's neighbour)"
# `--examples block` chmods the marketplace assets unreadable and restores them on
# exit, but a SIGKILL skips that. A leftover block is an uncontrolled treatment
# applied to every later trial — the overlay durability failure in a new costume,
# and that one cost a week of results read under the wrong condition. The next
# trial self-heals, but a sweep that starts blocked and nobody notices is exactly
# what this file exists to prevent.
docker exec "$NAME" bash -lc 'test -r /home/ubuntu/.local/share/pegasus-ai/marketplace/plugins/pegasus-ai/assets/templates/workflow_generator_template.py'
check $? "the shipped examples are readable (no stale --examples block)"

echo "$ok passed, $bad failed"
[ "$bad" = 0 ]
