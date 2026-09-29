"""Scoring maths. Pure functions, no database. JUDGING.md explains and defends it.

1. Each review's raw score is the weighted mean of its criterion scores.
2. Raw scores follow a two-way model:  raw = mu + quality[project] + leniency[judge] + noise.
   It is fitted by alternating least squares. Leniency gets a ridge penalty of
   `prior_weight` pseudo-reviews at zero, so a judge with one review is assumed
   average until shown otherwise. Leniencies are centred (review-weighted) so
   mu stays the event's average review.
3. A project's score is mu + quality: what it would have averaged if every
   review had come from a judge of average leniency.
4. Flat judges (two or more reviews, all identical) say nothing about which
   project is better, so they are left out of the fit. A project reviewed only
   by flat judges falls back to its raw mean and is marked low confidence.
5. Uncertainty comes from a seeded bootstrap: each project's reviews are
   resampled with replacement and the model refitted. That gives a rank band,
   P(first) and P(in the prize places), and flags projects whose placing is
   too close to call.

Why not per-judge z-scores: they assume every judge saw an equally strong set
of projects. With track-based assignment that is false, and a judge who drew a
strong track is wrongly read as harsh. The two-way model estimates quality and
leniency together, using the projects judges share, so it does not need that
assumption. It does need the judge-project graph to be connected; `components`
reports when it is not.
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

DEFAULT_PRIOR_WEIGHT = 2
DEFAULT_DRAWS = 400
DEFAULT_SEED = 20260929
BAND = (0.1, 0.9)  # 80% rank band
CONTESTED = (0.1, 0.9)  # P(in prize places) strictly between these = too close to call
MIN_CONFIDENT_REVIEWS = 2


def weighted_score(values, weights):
    """values: {criterion_key: int}; weights: {criterion_key: Decimal}. None if nothing counts."""
    total = Decimal(0)
    weight = Decimal(0)
    for key, value in values.items():
        w = weights.get(key, Decimal(0))
        if w > 0:
            total += w * value
            weight += w
    return float(total / weight) if weight else None


@dataclass(frozen=True)
class ReviewPoint:
    judge: str
    project: str
    raw: float


@dataclass
class Fit:
    mu: float
    quality: dict
    leniency: dict
    residual_sd: float
    iterations: int


def fit(points, prior_weight=DEFAULT_PRIOR_WEIGHT, iters=1000, tol=1e-10, start=None):
    """Least-squares fit of raw = mu + quality[p] + leniency[j], ridge on leniency."""
    if not points:
        return Fit(0.0, {}, {}, 0.0, 0)
    mu = sum(pt.raw for pt in points) / len(points)
    by_p, by_j = defaultdict(list), defaultdict(list)
    for pt in points:
        by_p[pt.project].append((pt.judge, pt.raw))
        by_j[pt.judge].append((pt.project, pt.raw))
    if start is not None:
        q = {p: start.quality.get(p, 0.0) for p in by_p}
        b = {j: start.leniency.get(j, 0.0) for j in by_j}
    else:
        q = {p: sum(x for _, x in rows) / len(rows) - mu for p, rows in by_p.items()}
        b = {j: 0.0 for j in by_j}
    n = len(points)
    it = 0
    for it in range(1, iters + 1):
        delta = 0.0
        for j, rows in by_j.items():
            new = sum(x - mu - q[p] for p, x in rows) / (len(rows) + prior_weight)
            delta = max(delta, abs(new - b[j]))
            b[j] = new
        centre = sum(b[j] * len(rows) for j, rows in by_j.items()) / n
        for j in b:
            b[j] -= centre
        for p, rows in by_p.items():
            new = sum(x - mu - b[j] for j, x in rows) / len(rows)
            delta = max(delta, abs(new - q[p]))
            q[p] = new
        if delta < tol:
            break
    resid = [pt.raw - mu - q[pt.project] - b[pt.judge] for pt in points]
    return Fit(mu, q, b, math.sqrt(sum(r * r for r in resid) / n), it)


def components(edges):
    """Connected components of the judge-project graph, as [(judges, projects)].

    Bias correction can only compare projects that are linked through shared
    judges; separate components have no common reference point.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for judge, project in edges:
        a, b = find(("j", judge)), find(("p", project))
        if a != b:
            parent[a] = b
    groups = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    return [
        ({n for kind, n in g if kind == "j"}, {n for kind, n in g if kind == "p"})
        for g in groups.values()
    ]


def kendall_tau(order_a, order_b):
    """Rank agreement of two orderings of the same items: 1 identical, -1 reversed."""
    pos = {x: i for i, x in enumerate(order_b)}
    items = [x for x in order_a if x in pos]
    concordant = discordant = 0
    for i in range(len(items)):
        for k in range(i + 1, len(items)):
            if pos[items[i]] < pos[items[k]]:
                concordant += 1
            else:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else 1.0


@dataclass
class JudgeEffect:
    judge: str
    reviews: int
    raw_mean: float
    leniency: float | None
    flat: bool
    excluded: bool


@dataclass
class ProjectScore:
    project: str
    reviews: int
    used_reviews: int
    raw_mean: float
    score: float
    rank: int = 0
    rank_lo: int = 0
    rank_hi: int = 0
    p_first: float = 0.0
    p_top: float = 0.0
    contested: bool = False
    low_confidence: bool = False
    fallback: bool = False


@dataclass
class Analysis:
    mu: float = 0.0
    residual_sd: float = 0.0
    prior_weight: float = DEFAULT_PRIOR_WEIGHT
    prize_slots: int = 3
    draws: int = 0
    components: int = 0
    judges: dict = field(default_factory=dict)
    projects: dict = field(default_factory=dict)
    reviews: dict = field(default_factory=dict)  # (judge, project) -> raw minus leniency, or None if excluded

    def ranking(self):
        return sorted(self.projects.values(), key=lambda r: r.rank)

    @property
    def contested_boundary(self):
        return any(p.contested for p in self.projects.values())


def _order(scores, raw_means):
    return sorted(scores, key=lambda p: (-scores[p], -raw_means[p], str(p)))


def _quantile(sorted_values, q):
    return sorted_values[min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))]


def analyse(points, prize_slots=3, prior_weight=DEFAULT_PRIOR_WEIGHT, draws=DEFAULT_DRAWS, seed=DEFAULT_SEED):
    points = [p for p in points if p.raw is not None]
    result = Analysis(prior_weight=prior_weight, prize_slots=prize_slots, draws=draws)
    if not points:
        return result

    by_j, by_p = defaultdict(list), defaultdict(list)
    for pt in points:
        by_j[pt.judge].append(pt)
        by_p[pt.project].append(pt)
    flat = {j for j, ps in by_j.items() if len(ps) >= 2 and len({round(p.raw, 9) for p in ps}) == 1}
    used = [pt for pt in points if pt.judge not in flat]
    used_by_p = defaultdict(list)
    for pt in used:
        used_by_p[pt.project].append(pt)
    fallback = {p for p in by_p if not used_by_p[p]}

    m = fit(used, prior_weight)
    result.mu, result.residual_sd = m.mu, m.residual_sd
    result.components = len(components((pt.judge, pt.project) for pt in points))
    raw_means = {p: sum(pt.raw for pt in ps) / len(ps) for p, ps in by_p.items()}

    def scores_from(model, fb_means):
        s = {p: model.mu + model.quality[p] for p in model.quality}
        s.update(fb_means)
        return s

    point_scores = scores_from(m, {p: raw_means[p] for p in fallback})
    order = _order(point_scores, raw_means)
    for j, ps in by_j.items():
        result.judges[j] = JudgeEffect(
            judge=j,
            reviews=len(ps),
            raw_mean=sum(p.raw for p in ps) / len(ps),
            leniency=None if j in flat else m.leniency.get(j, 0.0),
            flat=j in flat,
            excluded=j in flat,
        )
    for pt in points:
        result.reviews[(pt.judge, pt.project)] = None if pt.judge in flat else pt.raw - m.leniency.get(pt.judge, 0.0)
    for rank, p in enumerate(order, 1):
        used_n = len(by_p[p]) if p in fallback else len(used_by_p[p])
        result.projects[p] = ProjectScore(
            project=p,
            reviews=len(by_p[p]),
            used_reviews=used_n,
            raw_mean=raw_means[p],
            score=point_scores[p],
            rank=rank,
            rank_lo=rank,
            rank_hi=rank,
            p_first=1.0 if rank == 1 else 0.0,
            p_top=1.0 if rank <= prize_slots else 0.0,
            low_confidence=p in fallback or used_n < MIN_CONFIDENT_REVIEWS,
            fallback=p in fallback,
        )

    if draws:
        rng = random.Random(seed)
        ranks = defaultdict(list)
        projects = sorted(by_p)
        for _ in range(draws):
            sample = []
            for p in projects:
                rows = used_by_p[p]
                if rows:
                    sample.extend(rng.choices(rows, k=len(rows)))
            fb = {}
            for p in fallback:
                rows = by_p[p]
                picks = rng.choices(rows, k=len(rows))
                fb[p] = sum(x.raw for x in picks) / len(picks)
            bm = fit(sample, prior_weight, iters=300, tol=1e-7, start=m)
            s = scores_from(bm, fb)
            for r, p in enumerate(_order(s, raw_means), 1):
                ranks[p].append(r)
        for p, rs in ranks.items():
            rs.sort()
            ps = result.projects[p]
            ps.rank_lo, ps.rank_hi = _quantile(rs, BAND[0]), _quantile(rs, BAND[1])
            ps.p_first = sum(1 for r in rs if r == 1) / draws
            ps.p_top = sum(1 for r in rs if r <= prize_slots) / draws
            ps.contested = CONTESTED[0] < ps.p_top < CONTESTED[1]
    return result


# --- the older method, kept only for comparison in reports and simulations -----------------


def zscore_scores(points):
    """Per-judge z-scores averaged per project (the common approach JUDGING.md argues against)."""
    by_j = defaultdict(list)
    for pt in points:
        by_j[pt.judge].append(pt.raw)
    stats = {}
    for j, xs in by_j.items():
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))
        stats[j] = (m, sd)
    per_p = defaultdict(list)
    for pt in points:
        m, sd = stats[pt.judge]
        per_p[pt.project].append((pt.raw - m) / sd if sd > 0 else 0.0)
    return {p: sum(v) / len(v) for p, v in per_p.items()}


def mean_scores(points):
    per_p = defaultdict(list)
    for pt in points:
        per_p[pt.project].append(pt.raw)
    return {p: sum(v) / len(v) for p, v in per_p.items()}
