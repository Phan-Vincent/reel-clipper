"""musicscan pure helpers: normalization, downsampling, section scoring."""
import numpy as np
import pytest

from engine.musicscan import _describe, _downsample, _norm, _score_windows


def test_norm_bounds_and_robustness():
    a = np.array([1.0, 2.0, 3.0, 100.0])
    n = _norm(a)
    assert n.min() >= 0.0 and n.max() <= 1.0
    # constant input must not crash (percentile lo == hi)
    assert np.allclose(_norm(np.ones(10)), 0.0)


def test_downsample_length():
    assert len(_downsample(np.arange(100), 240)) == 100     # shorter -> as-is
    assert len(_downsample(np.arange(1000), 240)) == 240


def test_downsample_values_bounded():
    out = _downsample(np.random.default_rng(0).random(1000), 240)
    assert all(0.0 <= v <= 1.0 for v in out)


def test_describe_labels():
    label, desc = _describe(0.5, 0.5, is_top=True)
    assert label == "★ Recommended" and "drop" in desc
    label, _ = _describe(0.3, 0.0, is_top=False)
    assert label == "Alt"


def test_score_windows_prefers_high_energy():
    # two candidate windows: high-energy at 10-14s, quiet at 0-4s
    dur, target = 20.0, 4.0
    n = 200
    inten = np.full(n, 0.1)
    inten[100:140] = 0.9                      # seconds 10-14
    ftimes = np.arange(n) * dur / n
    beats = np.arange(0, dur, 0.5)
    sections = _score_windows(inten, ftimes, dur, target, beats,
                              np.array([]), max_suggestions=1)
    assert sections and sections[0].score == 1.0
    assert 9.5 <= sections[0].start <= 10.5


def test_score_windows_snaps_to_beat_and_clamps():
    dur, target = 10.0, 3.0
    n = 100
    inten = np.linspace(0.0, 1.0, n)          # ramp up toward the end
    ftimes = np.arange(n) * dur / n
    beats = np.array([3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0])
    sections = _score_windows(inten, ftimes, dur, target, beats,
                              np.array([]), max_suggestions=2)
    for s in sections:
        assert 0.0 <= s.start <= dur - target + 0.01
        assert s.end - s.start == pytest.approx(target, abs=0.5)


def test_score_windows_dedups_overlapping_windows():
    dur, target = 20.0, 4.0
    n = 200
    inten = np.full(n, 0.5)
    ftimes = np.arange(n) * dur / n
    beats = np.arange(0, dur, 0.5)
    sections = _score_windows(inten, ftimes, dur, target, beats,
                              np.array([]), max_suggestions=3)
    starts = [s.start for s in sections]
    for i, a in enumerate(starts):
        for j, b in enumerate(starts):
            if i != j:
                assert abs(a - b) >= target * 0.6 - 0.01
