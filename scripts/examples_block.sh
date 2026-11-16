#!/usr/bin/env bash
# Make every copy of the shipped examples unreadable to the model — or readable
# again — from the host, as root inside the container.
#
#   scripts/examples_block.sh on | off | status
#
# Why from the host: the harness runs as the uid the model runs as, so a block
# it applies is a block the model can undo. In ablation 1's block arm (s2b) the
# harness did `chmod 000` on the marketplace assets and 9 of 60 cells ran
# `chmod u+rx` and read them; 2 more read the image's own root-owned checkout
# under /opt/pegasus-ai, and 2 read the `on` arm's workspace copies archived
# under the results mount. Three copies, three routes, 13 leaked cells.
#
# `chown root` of the *directory* is enough: without x on it nothing beneath can
# be opened or listed, and the model's uid can neither chmod a root-owned
# directory nor remove it. File modes and content are untouched, so the
# marketplace checkout stays clean and `knowledge_sha` is unaffected.
#
# `off` returns ownership to the container's uid, which is what the trials, the
# `on` arm's copytree and the clean-workspace archiving need. `trial.py`
# verifies the state in both directions and refuses to measure under the wrong
# one; preflight's twelfth check catches a block left on after a kill.
set -euo pipefail
NAME="${NAME:-pegasus-studio}"
MK=/home/ubuntu/.local/share/pegasus-ai/marketplace/plugins/pegasus-ai/assets
IMG=/opt/pegasus-ai/marketplace/plugins/pegasus-ai/assets
ARCHIVE=/opt/prompt-lab-runs/workspace-archive

# Everything that currently exists: the two checkouts and every archived
# workspace copy. Evaluated inside the container, as root, so a copy no one
# knew about does not get missed because the host cannot see it.
LIST='for d in '"$MK $IMG"' $(find '"$ARCHIVE"' -mindepth 3 -maxdepth 3 -type d -path "*/root/assets" 2>/dev/null); do [ -d "$d" ] && echo "$d"; done'

uid=$(docker inspect "$NAME" --format '{{.Config.User}}')
[ -n "$uid" ] || { echo "cannot tell which uid $NAME runs as" >&2; exit 1; }

case "${1:-}" in
  on)
    docker exec -u 0 "$NAME" bash -c "$LIST | while read -r d; do chown root:root \"\$d\" && chmod 000 \"\$d\"; done"
    ;;
  off)
    docker exec -u 0 "$NAME" bash -c "$LIST | while read -r d; do chown $uid \"\$d\" && chmod 755 \"\$d\"; done"
    ;;
  status) ;;
  *) echo "usage: $0 on|off|status" >&2; exit 2 ;;
esac
# Reported as the model's uid sees it, which is the only view that matters.
docker exec "$NAME" bash -c "$LIST | while read -r d; do if [ -r \"\$d\" ] && [ -x \"\$d\" ]; then echo \"readable  \$d\"; else echo \"blocked   \$d\"; fi; done" \
  | awk '{if (c[$1]++ < 3) print "  " $0} END {printf "  %d blocked, %d readable\n", c["blocked"], c["readable"]}'
