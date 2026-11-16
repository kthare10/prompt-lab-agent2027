"""Recompute the examples case study from data/case_study_pairs.csv and data/trials.csv.

Each row of case_study_pairs.csv is one (task, model, repeat) cell with its L0 score under the
shipped gateway (off), the first block that agents could undo (leaked_block), and the block
enforced as root (held_block). `leaked` marks the cells in which the agent read a blocked file.

    python3 analysis/case_study.py
"""
import csv, os, random, statistics as st
HERE = os.path.dirname(os.path.abspath(__file__))
data = lambda name: list(csv.DictReader(open(os.path.join(HERE, "..", "data", name))))
rows = data("case_study_pairs.csv")
for r in rows:
    for k in ("off", "leaked_block", "held_block"): r[k] = float(r[k])

def signflip(d, n=20000):
    random.seed(0); obs = abs(st.fmean(d))
    return sum(abs(st.fmean([x if random.random() < .5 else -x for x in d])) >= obs for _ in range(n)) / n
def interval(d, n=20000):
    random.seed(1); b = sorted(st.fmean(random.choices(d, k=len(d))) for _ in range(n))
    return b[int(.025 * n)], b[int(.975 * n)]
def report(label, d):
    lo, hi = interval(d)
    print(f"{label:34s} n={len(d):2d}  mean paired difference {st.fmean(d):+.3f}  p {signflip(d):.3f}  95% interval [{lo:+.3f}, {hi:+.3f}]")

print(f"cells: {len(rows)}   cells in which a blocked file was read: {sum(r['leaked'] == '1' for r in rows)}")
report("first block minus shipped", [r["leaked_block"] - r["off"] for r in rows])
report("enforced block minus shipped", [r["held_block"] - r["off"] for r in rows])
for arm in ("off", "leaked_block", "held_block"):
    print(f"trials below 0.3 under {arm:13s}: {sum(r[arm] < 0.3 for r in rows)}")
keep = [r for r in rows if r["off"] >= 0.3 and r["leaked_block"] >= 0.3]
report("first block, neither below 0.3", [r["leaked_block"] - r["off"] for r in keep])

# The shipped gateway against a repeat of itself (runs s2d-examples-off and s2e-examples-off), same
# cells. The second run was resumed after harness failures, so each cell is scored by its first
# measured attempt, which data/trials.csv does not hold for four cells.
rep = data("case_study_repeat.csv")
report("shipped gateway repeated", [float(r["shipped_run2_first_attempt"]) - float(r["shipped_run1"]) for r in rep])
