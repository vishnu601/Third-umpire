# Judging: assignment, scoring and normalization

This is the part participants came for, so every number here is reproducible:

```bash
docker compose exec portal python manage.py normalization_report      # the fixture, raw vs corrected
docker compose exec portal python manage.py normalization_simulation  # ground-truth recovery
```

The saved outputs are in [`docs/proof/`](docs/proof/). The code is `src/portal/scoring.py` (pure functions, no
database) and `src/portal/simulation.py`, tested in `tests/test_scoring.py` and `tests/test_simulation.py`.

**In one paragraph:** judges score 1 to 5 on a weighted rubric. We treat every review as
*project quality + judge leniency + noise* and estimate quality and leniency together from the projects judges
share, shrinking the leniency of judges with little evidence toward zero. A project's score is what it would have
averaged with judges of average leniency. We then resample the model's noise 400 times to get a rank band and a
chance of placing for every project, and we say *too close to call* when that chance is between 10% and 90%. On the
fixture, the method picks the true winner more often than a plain average or z-scores in simulation, and it shows that
no project on the fixture wins more than 22% of the time.

---

## 1. Assignment

**Target.** Each event sets *reviews per project* (default 3; DOGFOOD's own panel uses 3).

**Automatic assignment** (`services.auto_assign`, organizer button on *Judges*):

1. Submitted projects are ordered by how many judges they already have, fewest first.
2. For each, candidates are every judge of the event who is **not already assigned** and **not on the project's
   team** (conflict of interest, enforced again when scoring).
3. Judges who cover the project's track come first; judges with no tracks are generalists and also count as
   on-track. Off-track judges are used only when on-track ones run out.
4. Within that, the **least-loaded** judge wins, ties broken by judge id, so the result is deterministic.
5. Existing assignments are kept, so running it again only **tops up** gaps. That is how the fixture's
   unfinished batches are fixed: one click assigns a third judge to the 8 projects stuck at 2 reviews.

**Batch assignment** (organizer picks one judge and ticks projects) exists for everything the algorithm can't know.
It refuses conflicts (409) and non-submitted projects. Unassigning is refused once a review exists.

**Tie-break assignment** (results page, section 5) adds one judge to each contested project, preferring judges who
already reviewed *another* contested project.

**Why connectivity matters.** Bias correction compares projects through the judges they share. If judges and
projects split into groups with no judge in common, scores across groups cannot be compared, by any method. The
dashboard counts these groups (*Can every project be compared?*) and the results page warns if there is more
than one. The fixture is one connected group: 29 judges × 41 projects (the flat judge jdg_07 doesn't count as a link,
since its reviews are left out of the fit). Judges who cover two tracks are what hold it together.

## 2. Scoring

Each criterion has a relative weight (organizer-editable; 0 switches a criterion off) and three **anchors** (what a
1, 3 and 5 look like). The scoring form shows them next to every score. Shared anchors reduce disagreement at the
source, which is cheaper than correcting it afterwards.

A review's raw score is the weighted mean of its criterion scores:

```
raw = Σ_c w_c · s_c / Σ_c w_c        (criteria with w_c = 0 are ignored)
```

Weights can change after scoring starts: results recompute, and every change is in the audit log. New criteria are
locked once reviews exist, because old reviews would be missing them.

## 3. Why not a plain average, and why not z-scores

A **plain average** lets luck of the draw decide: a project reviewed by lenient judges beats an equal project reviewed
by harsh ones. On the fixture, judge leniency ranges from **−0.48 to +0.37 points**, while the top five projects sit
within **0.23 points** of each other.

**Per-judge z-scores** (subtract each judge's mean, divide by their spread) are the usual fix, and they are wrong
here. They assume every judge saw an equally strong set of projects. In the fixture **every judge scores only their
own track** (0 of 126 reviews are off-track). A judge who drew a strong track looks harsh, and their best projects get
pulled down. Z-scores also divide by a spread estimated from 1 to 11 reviews, and they are undefined for a judge whose
spread is zero.

In simulation on the fixture's own design (section 6), z-scores find the true winner **less often than a plain
average** (26% vs 34%).

## 4. The model

For review *i* of project *p* by judge *j*:

```
raw_i = μ + q_p + b_j + ε_i
```

- `μ`: the event's average review.
- `q_p`: project quality.
- `b_j`: judge leniency.
- `ε_i`: noise.

We minimise

```
Σ_i (raw_i − μ − q_p − b_j)²  +  k · Σ_j b_j²        subject to  Σ_j n_j b_j = 0
```

by alternating least squares. `μ` is the mean review. Each pass updates leniency, then quality:

```
b_j ← Σ_{i∈j} (raw_i − μ − q_p) / (n_j + k)
q_p ← mean_{i∈p} (raw_i − μ − b_j)
```

It stops when no estimate moves by more than 1e-10 (65 passes on the fixture). Only then are leniencies centred
(weighted by review count, so `μ` stays the average review), and every quality moves by the same constant the other
way. That is a reparametrisation: fitted values, residuals and ranks don't change. An earlier version centred inside
every pass, which fought the ridge step and never converged (it always ran the full 1,000 passes). Fixing it moved
the twelve top scores in the report below by at most 0.01 and cut a full analysis from 2.8 s to 0.4 s.

- **Score** = `μ + q_p`: what the project would have averaged with judges of average leniency, on the same 1–5 scale
  as the raw scores.
- **Shrinkage `k = 2`** pseudo-reviews at zero leniency. A judge with one review is assumed roughly average until
  shown otherwise; a judge with ten reviews is mostly themselves. Without it, a judge with a single review has their
  leniency and their one project's quality perfectly confounded. We picked 2 as a small prior (less than the median
  3–4 reviews per judge); the unit tests show its effect (`test_shrinkage_pulls_low_evidence_judges_towards_average`).
- **Flat judges** (three or more reviews, every criterion score identical on every sheet) are left out of the fit.
  A constant ballot carries no information about which project is better, and including it would pull the judge's
  projects toward each other. They are still counted, shown and flagged, and they don't count as a bridge when we
  check whether every project can be compared. Two identical sheets can be honest agreement, so it takes three.
  Sheets are compared criterion by criterion, so different sheets with the same weighted mean are not mistaken for
  flat. The fixture has one: **jdg_07**, 3 reviews, all 4/4/4.
- **Withdrawn projects** (an organizer's call, for example the duplicate "Dry Harbour" prj_07 / prj_41) are left
  out of the fit entirely. Their reviews are kept for the record and the exports.
- **Fallback:** a project reviewed only by flat judges keeps its raw mean and is marked low confidence (there is
  nothing to compare it with).
- **Low confidence** = fewer than 2 reviews used.
- **Invariance:** adding a constant to every score from one judge changes nothing but that judge's leniency (test:
  `test_shift_of_one_judge_does_not_change_the_ranking`). An exact additive design is recovered exactly (test:
  `test_recovers_an_exact_additive_design`).

**Category awards** (e.g. *Best Innovation*) run the same model on one criterion at a time.

## 5. Uncertainty: when is a result a result?

With 2 to 5 reviews per project, the order at the top is fragile, and a platform that prints one winner is
overstating what it knows. We measure it with a **residual bootstrap**:

1. Fit the model; keep each review's fitted value and residual.
2. Scale residuals by `sqrt(N / (N − P − J + 1))`. A fit with one parameter per project and per judge leaves
   residuals smaller than the true noise; for the fixture, N=123 used reviews, P=41, J=29, a factor of 1.51.
3. 400 times (fixed seed, so pages are reproducible): add a randomly drawn residual to every fitted value, refit,
   rank.
4. Report per project: the **80% rank band** (10th to 90th percentile rank), **P(first)**, and **P(top k)**, where k is
   the number of overall prizes (3 if none are set).
5. **Too close to call** = P(top k) strictly between 10% and 90%.

Refitting each draw carries the uncertainty in every judge's leniency into the ranking. We first resampled each
project's own reviews instead, and the simulation (section 6) showed that made the bands too narrow (64% coverage):
it ignores leniency uncertainty, and resampling 2 reviews barely varies.

**Tie-break suggestions** use this directly. For each contested project we suggest the judge who (a) already
reviewed another contested project, so the two get a direct comparison, then (b) covers the track, then (c) has the
lightest load. Never someone from the project's team, never a flat judge. "Already reviewed" means a submitted
review, not just an assignment, because only a score links two projects in the model. A project whose tie-break
judge hasn't scored yet gets no second suggestion, so pressing the button twice adds nothing. One click assigns them
all (`source=tiebreak`, audited). Scores are frozen while results are published; unpublish to change them.

## 6. Proof on the fixture

### 6.1 The fixture, raw vs corrected ([`docs/proof/normalization-report.txt`](docs/proof/normalization-report.txt))

```
Event evt_01: 126 reviews, 30 judges, 41 projects, 1 connected component(s)
Two-way model: mu=3.556, residual sd=0.464, leniency prior=2 pseudo-reviews, 400 bootstrap draws, prize places=3
Flat judges excluded from the fit: jdg_07
Spread of judges' average given score: raw sd=0.413, after removing leniency sd=0.265
Harshest judges:  jdg_01 -0.48 (n=1), jdg_10 -0.42 (n=3), jdg_25 -0.28 (n=5), jdg_14 -0.25 (n=3)
Most lenient:     jdg_15 +0.37 (n=6), jdg_02 +0.35 (n=6), jdg_13 +0.24 (n=3), jdg_26 +0.22 (n=10)

model  raw    z  project  score   raw   n    band    P1  Ptop
    1    1    3  prj_11    4.33  4.33   4   1-11    14%   42%  too close
    2    2    1  prj_34    4.28  4.33   3   1-14    22%   47%  too close
    3    4    6  prj_25    4.22  4.11   3   1-17    12%   34%  too close
    4    6    5  prj_16    4.10  4.00   3   2-23     5%   21%  too close
    5    5    4  prj_37    4.10  4.08   4   2-17     6%   22%  too close
    6    3    7  prj_10    3.98  4.17   2   1-23    11%   28%  too close
    7    7    2  prj_33    3.96  4.00   3   2-24     7%   19%  too close
    8    8   12  prj_21    3.91  3.89   3   4-27     4%   10%
    9   11   22  prj_38    3.89  3.78   3   3-26     4%   14%  too close
   10   10   15  prj_08    3.85  3.80   5   5-26     0%    2%
   11    9    9  prj_41    3.81  3.83   4   5-26     1%    3%
   12   12   13  prj_04    3.77  3.78   3   4-28     1%    8%

Largest moves, raw mean -> model: prj_19 14->31, prj_12 27->17, prj_14 29->20, prj_24 21->14, prj_01 26->19
Kendall tau vs model: raw mean 0.80, z-score 0.72
Too close to call for the top 3: prj_11, prj_34, prj_25, prj_16, prj_37, prj_10, prj_33, prj_38, prj_15
```

How to read it:
- Correcting leniency shrinks the spread of judges' average scores from 0.41 to 0.265. What remains is real: some
  judges were given stronger tracks.
- **The point estimate and the likeliest winner are different projects.** prj_11 has the highest score (4.33), but
  prj_34 wins more resamples (22% against 14%). prj_34 rests on 3 reviews to prj_11's 4, so its score is less
  certain (rank band 1–14 against 1–11) and lands at both extremes more often, first included. A platform that
  printed one number would crown prj_11 and hide that the call is a coin toss between several projects.
- Z-scores would have crowned prj_34 and dropped prj_38 from 9th to 22nd. The largest correction moves prj_19 down
  17 places. Its raw average (3.67) was propped up by the flat judge jdg_07's constant 4.00, so once that review
  is set aside it rests on its one informative review (3.33), and it is flagged as low confidence.
- The honest headline: **no project wins in more than 22% of resamples, and nine projects are too close to call for
  the top three.** With 2 to 5 reviews per project, this fixture cannot name a winner with confidence. The tie-break
  panel says which extra reviews would.

### 6.2 Ground truth: does the method recover the right answer? ([`docs/proof/normalization-simulation.txt`](docs/proof/normalization-simulation.txt))

We can't know the fixture's true ranking, so we simulate events where we do. Each simulated event keeps **the
fixture's exact judge-project design** (126 reviews, same judges on the same projects) and uses spreads **calibrated
from the fixture's own unshrunk fit**: quality sd 0.62, leniency sd 0.68, noise sd 0.43. Every review is 3 criteria,
each rounded to a whole mark 1–5 and averaged, so rounding and the ceiling are included. 300 events, fixed seed.

```
method                    Kendall tau  true winner found  true top-3 found
plain mean               0.601 ± 0.075               34%               50%
per-judge z-score        0.622 ± 0.061               26%               48%
two-way model            0.655 ± 0.064               41%               56%

80% rank bands contained the true rank 73% of the time (80 simulated events x 41 projects).
```

- The two-way model is best on every measure, and z-scores find the winner *less* often than a plain average.
- The gains are real but modest. With ~3 noisy reviews per project, no method recovers the true order well, which
  is the argument for showing uncertainty rather than hiding it.
- **Caveat, stated plainly:** the "80%" bands cover the truth 73% of the time, so they run slightly narrow. Likely
  causes: shrinkage bias, and marks capped at 1–5, which the bootstrap doesn't reproduce. Read them as roughly 75%
  bands. The "too close to call" flag is conservative in the same direction.
- **Caveat 2:** the simulation generates data from the same additive model we fit, which favours it. The z-score
  and mean comparisons are fair on that data, but a different real-world bias shape (for example judges who
  compress their scale) is not modelled.

## 7. Who can see what (enforced in the backend)

| Data | Judge | Organizer | Participant | Public |
|---|---|---|---|---|
| Own reviews (`/api/judge/scores`, scoring form) | ✅ own only | — | — | — |
| Another judge's reviews (`?judge=` anyone else, even a nonexistent id) | **403** | per-project review page, export | 403 | 401 |
| Assigned projects queue | own | per judge (no scores) | — | — |
| All scores CSV, results CSV, progress API | 403 | ✅ | 403 | 401 |
| Results page | 403 until published | ✅ always | 403 until published | 403 until published |
| Per-judge leniency, tie-break suggestions | never | ✅ | never | never |

A judge can't be assigned or score a project from their own team, and people on a team can't accept a judge invite
for that event. Every review change is audited with before and after values (`review.changed`).

## 8. Choices worth revisiting

- `k = 2` and the 10%–90% "too close" thresholds are judgement calls. They're constants at the top of
  `scoring.py`.
- A multiplicative scale term per judge (judges who use only 3–5) is not modelled. It needs more reviews per judge
  than the fixture has.
- Pairwise (Bradley–Terry) judging would sidestep calibration entirely. It is not built.
