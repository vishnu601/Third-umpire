"""Ground-truth simulation: does bias correction recover the true ranking?

We generate events whose true project quality we know, score them through the
same judge-project design as a real event, and ask each method to recover the
truth. Scores are generated from the model the two-way fit assumes
(quality + leniency + noise), rounded to whole 1-5 marks per criterion and
averaged, so rounding and the 1-5 ceiling are included. Spreads are calibrated
from the real event's own fit.

Metrics per method, averaged over runs:
- Kendall tau between the estimated and the true ranking (1 = identical).
- How often the estimated winner is the true winner.
- Overlap of the estimated and true top 3.
Band coverage checks the bootstrap: how often a project's true rank falls
inside its reported 80% rank band (it should be close to 80%).
"""

import math
import random
import statistics
from collections import defaultdict

from . import scoring


def calibrate(points):
    """Spreads of quality, leniency and noise, from an unshrunk two-way fit of real data."""
    m = scoring.fit(points, prior_weight=0)
    return {
        "mu": m.mu,
        "sd_quality": statistics.pstdev(m.quality.values()) if len(m.quality) > 1 else 0.0,
        "sd_leniency": statistics.pstdev(m.leniency.values()) if len(m.leniency) > 1 else 0.0,
        "sd_noise": m.residual_sd,
    }


def _generate(edges, rng, mu, sd_quality, sd_leniency, sd_noise, n_criteria, quality_offsets):
    projects = sorted({p for _, p in edges})
    judges = sorted({j for j, _ in edges})
    quality = {p: rng.gauss(0, sd_quality) + quality_offsets.get(p, 0.0) for p in projects}
    leniency = {j: rng.gauss(0, sd_leniency) for j in judges}
    per_criterion_sd = sd_noise * math.sqrt(n_criteria)
    points = []
    for j, p in edges:
        marks = [
            min(5, max(1, round(mu + quality[p] + leniency[j] + rng.gauss(0, per_criterion_sd))))
            for _ in range(n_criteria)
        ]
        points.append(scoring.ReviewPoint(judge=j, project=p, raw=sum(marks) / n_criteria))
    return quality, points


def _order(scores):
    return sorted(scores, key=lambda p: (-scores[p], str(p)))


def _summary(xs):
    return {"mean": statistics.fmean(xs), "sd": statistics.pstdev(xs) if len(xs) > 1 else 0.0}


METHODS = {
    "plain mean": lambda pts: scoring.mean_scores(pts),
    "per-judge z-score": lambda pts: scoring.zscore_scores(pts),
    "two-way model": lambda pts: {p.project: p.score for p in scoring.analyse(pts, draws=0).projects.values()},
}


def simulate(
    edges,
    runs=300,
    seed=1,
    mu=3.5,
    sd_quality=0.5,
    sd_leniency=0.4,
    sd_noise=0.45,
    n_criteria=3,
    quality_offsets=None,
    band_runs=0,
    band_draws=100,
):
    edges = sorted(set(edges))
    quality_offsets = quality_offsets or {}
    rng = random.Random(seed)
    metrics = {m: defaultdict(list) for m in METHODS}
    inside = total = 0
    for run in range(runs):
        quality, points = _generate(edges, rng, mu, sd_quality, sd_leniency, sd_noise, n_criteria, quality_offsets)
        truth = _order(quality)
        true_top3 = set(truth[:3])
        for name, method in METHODS.items():
            est = _order(method(points))
            metrics[name]["kendall_tau"].append(scoring.kendall_tau(est, truth))
            metrics[name]["winner"].append(1.0 if est[0] == truth[0] else 0.0)
            metrics[name]["top3_overlap"].append(len(true_top3 & set(est[:3])) / 3)
        if run < band_runs:
            analysis = scoring.analyse(points, draws=band_draws, seed=seed + run)
            true_rank = {p: i for i, p in enumerate(truth, 1)}
            for ps in analysis.projects.values():
                total += 1
                inside += ps.rank_lo <= true_rank[ps.project] <= ps.rank_hi
    return {
        "runs": runs,
        "settings": {"mu": mu, "sd_quality": sd_quality, "sd_leniency": sd_leniency, "sd_noise": sd_noise,
                     "n_criteria": n_criteria},
        "methods": {name: {k: _summary(v) for k, v in m.items()} for name, m in metrics.items()},
        "band_runs": band_runs,
        "band_coverage": inside / total if total else None,
    }
