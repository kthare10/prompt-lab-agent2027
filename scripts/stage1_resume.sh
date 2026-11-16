# Re-run Stage 1. done_cells counts only status='done', so the 22 completed
# cells are skipped and the 229 that died behind a stuck turn are retried.
set -u
cd /opt/prompt-lab
bash scripts/stage1.sh
