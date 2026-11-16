#!/usr/bin/env bash
# Bring up the Studio container this harness measures, on a clean machine.
#
# This existed only as an invocation typed from `docker inspect` output, which
# is how a container gets recreated without the bind mount that makes the host
# able to read what the models wrote — or without PROMPT_LAB_RUNS, which sends
# every result to a path inside the image.
#
# The layout it assumes (studio-webapp#402):
#   $HOME in the container is $STUDIO_HOME on the host, so the host reads every
#   file the chat writes and never needs `docker exec` to look at one;
#   the workspace root *is* $HOME, and a project is $HOME/workflows/<name>/;
#   the code is read-only at /opt/prompt-lab and the results are writable at
#   /opt/prompt-lab-runs, deliberately not nested under the code — on
#   2026-09-05 something running as uid 1000 emptied the container's home and
#   took the harness, the tasks and 13 hours of results with it.
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/pegasus-isi/studio-webapp:main}"
NAME="${NAME:-pegasus-studio}"
PORT="${PORT:-8888}"
STUDIO_HOME="${STUDIO_HOME:-$HOME/studio-home}"
LAB="${LAB:-$HOME/prompt-lab}"
RUNS="${RUNS:-$HOME/prompt-lab-runs}"
CONDOR_TOKENS="${CONDOR_TOKENS:-$HOME/condor-tokens.d}"
# Empty on a machine with no pool of its own: the sweep stops at `plan`, which
# is local, and only `--depth run` needs a schedd.
CONDOR_HOST="${CONDOR_HOST:-}"
# The uid the container runs as. The entrypoint supports any uid ("runs as
# whatever UID KubeSpawner injects"), and every bind mount below is owned by
# whoever runs this script, so the two must agree or the engine cannot write
# its own home: on the Jetstream VM that user was `ubuntu` (1000), on the
# Exosphere VM it is `exouser` (1001), and a hard-coded 1000 made every
# write under $STUDIO_HOME fail there before a single trial ran.
STUDIO_UID="${STUDIO_UID:-$(id -u):$(id -g)}"

mkdir -p "$STUDIO_HOME" "$RUNS" "$CONDOR_TOKENS"
[ -d "$LAB" ] || { echo "no harness at $LAB (git clone it first)" >&2; exit 1; }

if docker inspect "$NAME" >/dev/null 2>&1; then
    echo "renaming the running container rather than replacing it — a recreate"
    echo "loses nothing under \$HOME (bind mount) but does lose the engine's"
    echo "in-process state, and keeping it makes a rollback one command."
    docker rename "$NAME" "$NAME-$(date +%Y%m%d-%H%M%S)"
    docker stop "$NAME-"* >/dev/null 2>&1 || true
fi

args=(
    -d --name "$NAME" --user "$STUDIO_UID" -p "$PORT:8888"
    # Comes back after a daemon restart or a reboot. Without it the container
    # stays stopped and the next thing to notice is a preflight reporting
    # eleven failures, or a sweep that cannot start.
    --restart unless-stopped
    -v "$STUDIO_HOME:/home/ubuntu"
    -v "$CONDOR_TOKENS:/home/ubuntu/.condor/tokens.d:ro"
    -v "$LAB:/opt/prompt-lab:ro"
    -v "$RUNS:/opt/prompt-lab-runs:rw"
    -e HOME=/home/ubuntu -e USER=ubuntu
    -e PROMPT_LAB_RUNS=/opt/prompt-lab-runs
    -e PEGASUS_PYTHON=/usr/bin/python3
    -e NPM_CONFIG_PREFIX=/opt/npm-global
    -e KNOWLEDGE_REPO_URL=https://github.com/pegasus-isi/claude-plugin-marketplace.git
)
if [ -n "$CONDOR_HOST" ]; then
    args+=(-e "_CONDOR_CONDOR_HOST=$CONDOR_HOST"
           -e "_CONDOR_SCHEDD_HOST=$CONDOR_HOST"
           -e _CONDOR_SEC_TOKEN_DIRECTORY=/home/ubuntu/.condor/tokens.d)
fi

docker run "${args[@]}" "$IMAGE" >/dev/null
echo "started $NAME from $IMAGE"

for _ in $(seq 1 60); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/api/health")" = 200 ]; then
        echo "studio-api healthy"
        break
    fi
    sleep 5
done

exec "$(dirname "$0")/preflight.sh"
