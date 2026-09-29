"""Pure scoring maths: weighted rubric, two-way bias model, bootstrap uncertainty."""

import random
from decimal import Decimal

import pytest

from portal.scoring import ReviewPoint, analyse, components, fit, flat_judges, kendall_tau, weighted_score


def pts(rows):
    return [ReviewPoint(judge=j, project=p, raw=float(x)) for j, p, x in rows]


# --- weighted rubric ---------------------------------------------------------------


def test_weighted_score_uses_relative_weights():
    assert weighted_score({"a": 4, "b": 2}, {"a": Decimal(3), "b": Decimal(1)}) == pytest.approx(3.5)


def test_weighted_score_ignores_zero_weight_and_unknown_criteria():
    assert weighted_score({"a": 4, "b": 1, "zzz": 5}, {"a": Decimal(1), "b": Decimal(0)}) == pytest.approx(4)


def test_weighted_score_is_none_when_nothing_counts():
    assert weighted_score({"a": 4}, {"a": Decimal(0)}) is None
    assert weighted_score({}, {"a": Decimal(1)}) is None


# --- the two-way model -----------------------------------------------------------------


def test_recovers_an_exact_additive_design():
    quality = {"A": 1.0, "B": 0.0, "C": -1.0}
    leniency = {"H": -0.5, "G": 0.5, "M": 0.0}
    rows = [(j, p, 3 + quality[p] + leniency[j]) for j in leniency for p in quality]
    m = fit(pts(rows), prior_weight=0)
    for p, q in quality.items():
        assert m.quality[p] == pytest.approx(q, abs=1e-6)
    for j, b in leniency.items():
        assert m.leniency[j] == pytest.approx(b, abs=1e-6)


def test_harsh_judge_on_a_strong_batch_is_not_mistaken_for_a_weak_batch():
    # H is 1 point harsh and saw A, B; G saw B, C. B links them.
    rows = [("H", "A", 3), ("H", "B", 2), ("G", "B", 3), ("G", "C", 2)]
    r = analyse(pts(rows), prior_weight=0, draws=0)
    s = {p: x.score for p, x in r.projects.items()}
    assert s["A"] > s["B"] > s["C"]
    assert s["A"] - s["B"] == pytest.approx(1)
    # The plain average understates the gap, because B's reviews mix a harsh and a lenient judge.
    assert r.projects["A"].raw_mean - r.projects["B"].raw_mean == pytest.approx(0.5)


def test_shrinkage_pulls_low_evidence_judges_towards_average():
    # S scored C and D two points above J. Without shrinkage that is all leniency;
    # with it, two reviews are not enough to be sure, so the gap shrinks.
    rows = [("J", "A", 4), ("J", "B", 2), ("J", "C", 3), ("J", "D", 3), ("K", "A", 4), ("K", "B", 2),
            ("S", "C", 5), ("S", "D", 5)]
    free = fit(pts(rows), prior_weight=0)
    shrunk = fit(pts(rows), prior_weight=2)
    assert free.leniency["S"] - free.leniency["J"] == pytest.approx(2, abs=1e-6)
    assert 0 < shrunk.leniency["S"] - shrunk.leniency["J"] < 2


def test_flat_judge_is_excluded_from_the_fit_but_counted():
    rows = [("F", "A", 4), ("F", "B", 4), ("F", "C", 4), ("J", "A", 5), ("J", "B", 2), ("J", "C", 3)]
    r = analyse(pts(rows), draws=0)
    assert r.judges["F"].flat and r.judges["F"].excluded
    assert r.projects["A"].reviews == 2 and r.projects["A"].used_reviews == 1
    assert [x.project for x in r.ranking()] == ["A", "C", "B"]
    assert r.reviews[("F", "A")] is None


def test_project_seen_only_by_a_flat_judge_still_gets_a_score():
    rows = [("F", "A", 4), ("F", "Z", 4), ("F", "Y", 4), ("J", "B", 5), ("J", "C", 1)]
    r = analyse(pts(rows), draws=0)
    # Falls back to its raw mean (better than no estimate) and says so.
    assert r.projects["A"].fallback and r.projects["A"].used_reviews == 1
    assert r.projects["A"].score == pytest.approx(4)
    assert r.projects["A"].low_confidence


def test_two_equal_reviews_are_not_enough_to_call_a_judge_flat():
    rows = [("T", "A", 4), ("T", "B", 4), ("J", "A", 5), ("J", "B", 2)]
    assert not analyse(pts(rows), draws=0).judges["T"].flat


def test_same_mean_from_different_criterion_scores_is_not_flat():
    vectors = [(4, 3, 5), (5, 4, 3), (3, 5, 4)]
    rows = [ReviewPoint(judge="D", project=p, raw=4.0, vector=v) for p, v in zip("ABC", vectors)]
    rows += pts([("J", "A", 5), ("J", "B", 3), ("J", "C", 2)])
    assert flat_judges(rows) == set()
    same = [ReviewPoint(judge="F", project=p, raw=4.0, vector=(4, 4, 4)) for p in "ABC"]
    assert flat_judges(same + rows) == {"F"}


def test_everyone_identical_does_not_break():
    r = analyse(pts([("J", "A", 3), ("K", "B", 3)]), draws=0)
    assert r.projects["A"].score == pytest.approx(3)


def test_empty_input():
    r = analyse([], draws=0)
    assert r.projects == {} and r.judges == {}


def test_fit_converges_to_the_ridge_solution():
    rng = random.Random(3)
    rows = [(f"J{j}", f"P{p:02d}", rng.randint(1, 5)) for j in range(8) for p in range(15) if rng.random() < 0.4]
    points = pts(rows)
    m = fit(points, prior_weight=2)
    assert m.iterations < 1000
    resid = {pt: pt.raw - m.mu - m.quality[pt.project] - m.leniency[pt.judge] for pt in points}
    by_p, by_j = {}, {}
    for pt, r in resid.items():
        by_p[pt.project] = by_p.get(pt.project, 0) + r
        by_j[pt.judge] = by_j.get(pt.judge, 0) + r
    # Normal equations: project residuals sum to zero; judge residuals equal the
    # ridge pull on (leniency + one shared centring constant).
    assert all(abs(v) < 1e-6 for v in by_p.values())
    offsets = [by_j[j] / 2 - m.leniency[j] for j in by_j]
    assert max(offsets) - min(offsets) < 1e-6
    # And leniency is centred, review-weighted, so mu stays the average review.
    n = {j: sum(1 for pt in points if pt.judge == j) for j in by_j}
    assert abs(sum(m.leniency[j] * n[j] for j in by_j)) < 1e-6


def test_shift_of_one_judge_does_not_change_the_ranking():
    base = [("J1", "A", 4), ("J1", "B", 2), ("J1", "C", 3), ("J2", "A", 5), ("J2", "B", 3), ("J2", "C", 4),
            ("J3", "A", 3), ("J3", "C", 3), ("J3", "B", 1)]
    shifted = [(j, p, x - 1 if j == "J2" else x) for j, p, x in base]
    order = lambda r: [x.project for x in r.ranking()]
    assert order(analyse(pts(base), prior_weight=0, draws=0)) == order(analyse(pts(shifted), prior_weight=0, draws=0))


# --- connectivity --------------------------------------------------------------------------


def test_components_split_when_no_judge_links_two_groups():
    comps = components([("J", "A"), ("J", "B"), ("K", "C")])
    assert sorted((len(j), len(p)) for j, p in comps) == [(1, 1), (1, 2)]


def test_a_flat_judge_does_not_count_as_a_bridge():
    rows = [("J", "A", 5), ("J", "B", 3), ("K", "C", 4), ("K", "D", 2), ("F", "A", 4), ("F", "C", 4), ("F", "E", 4)]
    assert analyse(pts(rows), draws=0).components == 2


def test_disconnected_design_is_reported():
    r = analyse(pts([("J", "A", 5), ("J", "B", 3), ("K", "C", 4), ("K", "D", 2)]), draws=0)
    assert r.components == 2


# --- uncertainty -----------------------------------------------------------------------------


def test_bootstrap_is_deterministic_and_bands_contain_the_point_rank():
    rng = random.Random(7)
    rows = [(f"J{j}", f"P{p:02d}", rng.randint(1, 5)) for j in range(6) for p in range(12) if rng.random() < 0.5]
    a = analyse(pts(rows), draws=120, seed=11)
    b = analyse(pts(rows), draws=120, seed=11)
    assert [(x.project, x.p_first) for x in a.ranking()] == [(x.project, x.p_first) for x in b.ranking()]
    for x in a.ranking():
        assert x.rank_lo <= x.rank_hi
        assert 0 <= x.p_first <= x.p_top <= 1
    assert sum(x.p_first for x in a.ranking()) == pytest.approx(1)


def test_a_clear_winner_is_not_contested():
    rows = []
    for j in ("J1", "J2", "J3", "J4"):
        rows += [(j, "Star", 5), (j, "Mid", 3), (j, "Low", 1), (j, "Low2", 1)]
    r = analyse(pts(rows), prize_slots=1, draws=200)
    assert r.projects["Star"].p_first == pytest.approx(1)
    assert not r.projects["Star"].contested


def test_near_tie_is_flagged_as_too_close_to_call():
    rows = [("J1", "A", 5), ("J2", "A", 4), ("J3", "B", 5), ("J4", "B", 4), ("J1", "C", 2), ("J3", "C", 2),
            ("J2", "D", 1), ("J4", "D", 1)]
    r = analyse(pts(rows), prize_slots=1, draws=300)
    assert r.projects["A"].contested and r.projects["B"].contested
    assert r.contested_boundary


def test_kendall_tau():
    assert kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == pytest.approx(1)
    assert kendall_tau(["a", "b", "c"], ["c", "b", "a"]) == pytest.approx(-1)
