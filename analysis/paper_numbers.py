"""Recompute the headline numbers of the paper from data/trials.csv.

    python3 analysis/paper_numbers.py
"""
import collections, csv, os, random, statistics as st
HERE = os.path.dirname(os.path.abspath(__file__))
rows = list(csv.DictReader(open(os.path.join(HERE, "..", "data", "trials.csv"))))
f = lambda r, k: float(r[k]) if r[k] not in ("", None) else None
BASE = {"s1-azure", "s1-nrp", "s1-fabric", "s1-jetstream", "s1-claude"}
base = [r for r in rows if r["run_id"] in BASE and r["paper_cohort"] == "1"]
build = lambda r: r["depth"] != "none"

print("== Table I: baseline, one trial per task (time is the median over trials that finished or timed out)")
tab = []
for m in sorted({r["model"] for r in base}):
    mine = [r for r in base if r["model"] == m]
    b = [f(r, "l1_score") for r in mine if build(r) and r["l1_score"] != ""]
    j = [f(r, "l1_score") for r in mine if not build(r) and r["l1_score"] != ""]
    tab.append((st.fmean(b), m, st.fmean(j), st.median(f(r, "wall_s") for r in mine if r["status"] in ("done", "turn_timeout")), len(b), len(j)))
for b, m, j, t, nb, nj in sorted(tab, reverse=True):
    print(f"{m:20s} build {b:.3f} (n={nb})  judgment {j:.3f} (n={nj})  median time {t:6.0f} s")

print("\n== Pooled build scores (trials that ended on an unanswered request left out)")
POOL = {"claude-opus-5-5": ["s1-claude", "s1rep-claude"],
        "deepseek-v4-flash": ["s1-nrp", "s1-anchor-nrp", "s1rep-nrp-deepseek"],
        "Kimi-K2.6": ["s1-azure", "s1-anchor-azure", "s1rep-azure"],
        "qwen-3.8-27b": ["s1-jetstream", "s1rep-jetstream"]}
per = {}
for m, runs in POOL.items():
    bt = collections.defaultdict(list); wall = []; nofile = 0
    for r in rows:
        if r["model"] == m and r["run_id"] in runs and build(r) and r["ended_on_unanswered_request"] == "0":
            bt[r["task"]].append(f(r, "l1_score")); wall.append(f(r, "wall_s")); nofile += r["wrote_files"] == "False"
    per[m] = {t: st.fmean(v) for t, v in bt.items()}
    print(f"{m:20s} pooled {st.fmean(per[m].values()):.3f}  trials {sum(map(len, bt.values()))}  "
          f"median build-trial time {st.median(wall):.0f} s  trials with no file written {nofile}")
def compare(a, b, n=20000):
    d = [per[a][t] - per[b][t] for t in per[a]]
    random.seed(0)
    boot = sorted(st.fmean(random.choices(d, k=len(d))) for _ in range(n))
    obs = abs(st.fmean(d))
    p = sum(abs(st.fmean([x if random.random() < .5 else -x for x in d])) >= obs for _ in range(n)) / n
    print(f"{a} - {b}: {st.fmean(d):+.3f}  95% interval [{boot[int(.025*n)]:+.3f}, {boot[int(.975*n)]:+.3f}]  sign-flip p {p:.3f}")
compare("claude-opus-5-5", "deepseek-v4-flash"); compare("claude-opus-5-5", "Kimi-K2.6")
compare("deepseek-v4-flash", "Kimi-K2.6"); compare("deepseek-v4-flash", "qwen-3.8-27b")

print("\n== Stage outcomes on baseline build trials")
TOP = {"qwen3-small", "glm-5", "Kimi-K2.6", "deepseek-v4-flash", "kimi"}
for name, keep in (("top five", lambda r: r["model"] in TOP), ("Claude", lambda r: r["model"] == "claude-opus-5-5"),
                   ("21 open-weight", lambda r: r["model"] != "claude-opus-5-5")):
    g = [r for r in base if build(r) and keep(r)]
    c = lambda k, v: sum(r[k] == v for r in g)
    nplan = sum(r["depth"] == "plan" for r in g)
    print(f"{name:15s} n={len(g):3d}  generated at trial time {c('stage_generate', 'generated')}  "
          f"builds at trial time {c('stage_build', 'built')}/{sum(r['stage_build'] != '' for r in g)}  "
          f"builds in replay {c('replay_build', 'built')}/{sum(r['replay_build'] != '' for r in g)}  "
          f"planned in replay {c('replay_plan', 'planned')} of {nplan} plan-depth trials")
