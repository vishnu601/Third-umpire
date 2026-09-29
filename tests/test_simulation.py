"""The ground-truth simulation behind the normalization proof."""

import pytest

from portal.simulation import calibrate, simulate
from portal.scoring import ReviewPoint


def track_design(tracks=4, per_track=6, judges_per_track=2):
    """Judges confined to one track each, plus bridge judges spanning two tracks: the fixture's shape."""
    edges, offsets = [], {}
    for t in range(tracks):
        projects = [f"t{t}p{i}" for i in range(per_track)]
        for p in projects:
            offsets[p] = (t - (tracks - 1) / 2) * 0.6  # tracks differ in true quality
        for j in range(judges_per_track):
            edges += [(f"t{t}j{j}", p) for p in projects]
        nxt = [f"t{(t + 1) % tracks}p{i}" for i in range(per_track)]
        edges += [(f"bridge{t}", p) for p in projects[:3] + nxt[:3]]
    return edges, offsets


def test_simulation_is_deterministic():
    edges, offsets = track_design()
    a = simulate(edges, runs=10, seed=3, quality_offsets=offsets)
    b = simulate(edges, runs=10, seed=3, quality_offsets=offsets)
    assert a == b


def test_model_beats_mean_and_zscore_when_tracks_differ_in_quality():
    edges, offsets = track_design()
    r = simulate(edges, runs=40, seed=1, sd_quality=0.4, sd_leniency=0.5, sd_noise=0.4, quality_offsets=offsets)
    tau = {m: r["methods"][m]["kendall_tau"]["mean"] for m in r["methods"]}
    assert tau["two-way model"] > tau["plain mean"]
    assert tau["two-way model"] > tau["per-judge z-score"]


def test_bands_are_reported_when_asked():
    edges, offsets = track_design(tracks=2, per_track=4)
    r = simulate(edges, runs=6, seed=2, band_runs=3, band_draws=30, quality_offsets=offsets)
    cov = r["band_coverage"]
    assert 0 <= cov <= 1 and r["band_runs"] == 3


def test_calibration_reads_the_spreads_off_a_fit():
    rows = [("J", "A", 4), ("J", "B", 2), ("K", "A", 5), ("K", "B", 3), ("L", "A", 3), ("L", "B", 1)]
    c = calibrate([ReviewPoint(j, p, float(x)) for j, p, x in rows])
    assert c["sd_leniency"] == pytest.approx(0.8165, abs=1e-3)  # judges at -0, +1, -1 around the mean
    assert c["sd_quality"] == pytest.approx(1.0, abs=1e-6)
    assert c["sd_noise"] == pytest.approx(0.0, abs=1e-6)
