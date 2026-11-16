"""Rank correlation, without a dependency.

numpy and scipy are absent from both the host and the container image, and
adding one so two correlations can be computed would put a build step between
the store and the finding. These are the three functions that need to exist and
agree everywhere they are used — the analysis in `tools/stage1.py` and the live
board in `tools/serve.py` must not each grow their own.
"""

from __future__ import annotations

import random
import statistics

PERMUTATIONS = 20000
SEED = 20260910          # fixed: a p-value that moves between runs is not one


def ranks(xs: list[float]) -> list[float]:
    """Ranks with ties averaged — the tie rule Spearman assumes."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def pearson(a: list[float], b: list[float]) -> float:
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else float("nan")


def spearman(a: list[float], b: list[float]) -> float:
    return pearson(ranks(a), ranks(b))


def permutation_p(a: list[float], b: list[float],
                  permutations: int = PERMUTATIONS) -> float:
    """Two-sided p for a Spearman rho, by shuffling one side.

    n is 12 to 16 here. A table-based p assumes a null distribution these ranks
    have no reason to follow — with ties, with scores bunched at the floor — so
    the null is generated from the data instead. The board asks for fewer
    permutations than the analysis: it recomputes on every request, and the
    third decimal of a p-value is not what anyone is reading it for.
    """
    obs = abs(spearman(a, b))
    if obs != obs:                                        # NaN: no variance
        return float("nan")
    rng = random.Random(SEED)
    shuffled = list(b)
    hits = 0
    for _ in range(permutations):
        rng.shuffle(shuffled)
        r = abs(spearman(a, shuffled))
        hits += (r == r and r >= obs)
    return (hits + 1) / (permutations + 1)


def mean_pairwise_spearman(vectors: list[list[float | None]]) -> tuple[float, int]:
    """How much a group of measurements agrees with itself.

    Each vector is one task's scores, indexed by model, with None where the cell
    is missing. Returns (mean rho over every pair, number of pairs). This is the
    question that has to be answered before any group of tasks is averaged into
    a single column: a mean over tasks that do not correlate with each other is
    not a measurement of anything.
    """
    rhos = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            xs = [(a, b) for a, b in zip(vectors[i], vectors[j])
                  if a is not None and b is not None]
            if len(xs) < 3:
                continue
            r = spearman([a for a, _ in xs], [b for _, b in xs])
            if r == r:
                rhos.append(r)
    return (statistics.fmean(rhos) if rhos else float("nan"), len(rhos))
