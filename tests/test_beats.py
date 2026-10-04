"""BeatGrid.cut_points + strong_onset_times — the cut-grid math every render
relies on. Pure logic, no ffmpeg/librosa needed."""
import numpy as np
import pytest

from engine.beats import BeatGrid, strong_onset_times


def make_grid(beats, strengths=None):
    return BeatGrid(tempo=120.0, beats=list(beats),
                    onset_strength=list(strengths) if strengths else [])


def test_spans_zero_to_total():
    cp = make_grid([0.5, 1.0, 1.5, 2.0]).cut_points(1.0, 0.3, 1.6, 2.5)
    assert cp[0] == 0.0
    assert cp[-1] == 2.5
    assert cp == sorted(set(cp))


def test_no_segment_shorter_than_min():
    cp = make_grid([0.1, 0.5, 0.9, 1.3, 1.7]).cut_points(1.0, 0.5, 1.6, 2.0)
    segs = [b - a for a, b in zip(cp, cp[1:])]
    assert all(s >= 0.5 - 1e-9 for s in segs)


def test_no_segment_longer_than_max():
    # sparse beats -> min/max clamps insert intermediate cuts
    cp = make_grid([0.5, 5.0]).cut_points(1.0, 0.3, 1.6, 5.0)
    segs = [b - a for a, b in zip(cp, cp[1:])]
    assert all(s <= 1.6 + 1e-9 for s in segs)
    assert cp[-1] == 5.0


def test_subdivision_inserts_midpoints():
    # beats 1.0s apart so the 0.5s midpoints survive the min_seg clamp
    cp = make_grid([0.5, 1.5, 2.5]).cut_points(2.0, 0.3, 1.6, 3.0)
    assert 1.0 in cp and 2.0 in cp


def test_grouping_keeps_every_other_beat():
    cp = make_grid([0.5, 1.0, 1.5, 2.0, 2.5]).cut_points(0.5, 0.3, 1.6, 3.0)
    # halves the number of internal cut points vs 1.0
    cp1 = make_grid([0.5, 1.0, 1.5, 2.0, 2.5]).cut_points(1.0, 0.3, 1.6, 3.0)
    assert len(cp) <= len(cp1)


def test_min_seg_clamp_drops_subdivided_midpoints():
    # beats 0.5s apart subdivided -> 0.25s gaps are below min_seg and dropped
    cp = make_grid([0.5, 1.0, 1.5]).cut_points(2.0, 0.3, 1.6, 2.0)
    assert cp == [0.0, 0.5, 1.0, 1.5, 2.0]


def test_too_few_beats_falls_back_to_grid():
    cp = make_grid([0.3]).cut_points(1.0, 0.3, 1.6, 2.0)
    assert cp[0] == 0.0 and cp[-1] == 2.0
    segs = [b - a for a, b in zip(cp, cp[1:])]
    assert all(0.3 - 1e-9 <= s <= 1.6 + 1e-9 for s in segs)


def test_beats_outside_total_are_dropped():
    cp = make_grid([0.2, 0.5, 10.0]).cut_points(1.0, 0.3, 1.6, 1.0)
    assert cp[-1] == 1.0
    assert all(0 <= p <= 1.0 for p in cp)


def test_strong_onset_times_matches_percentile():
    grid = make_grid([0.5, 1.0, 1.5, 2.0], strengths=[0.1, 0.9, 0.4, 0.8])
    # np.percentile p75 (linear interp) of [0.1,0.4,0.8,0.9] is 0.825
    hits = strong_onset_times(grid, [0.0, 0.5, 1.0, 1.5, 2.0])
    assert 1.0 in hits                # 0.9 >= 0.825
    assert 2.0 not in hits            # 0.8 < 0.825
    assert 0.5 not in hits            # 0.1 < 0.825


def test_strong_onset_times_all_equal_qualify_all():
    grid = make_grid([0.5, 1.0], strengths=[0.5, 0.5])
    hits = strong_onset_times(grid, [0.5, 1.0])
    assert hits == {0.5, 1.0}


def test_strong_onset_times_empty_strengths():
    assert strong_onset_times(make_grid([0.5]), [0.0, 0.5]) == set()


def test_cut_points_bad_min_max_segment_terminates():
    # A <=0 or non-finite max_segment used to spin the clamp loop forever;
    # it must be normalized instead so the function always returns promptly.
    import math
    beats = [i * 0.5 for i in range(40)]
    for min_seg, max_seg in [(0.3, 0.0), (0.3, -1.0), (0.3, -math.inf),
                             (0.3, math.nan), (0.0, 1.6), (-1.0, 1.6),
                             (math.nan, 1.6), (2.0, 1.0)]:
        cp = BeatGrid(tempo=120.0, beats=beats).cut_points(
            1.0, min_seg, max_seg, 18.0)
        assert cp[0] == 0.0 and cp[-1] == 18.0, (min_seg, max_seg)
        assert cp == sorted(set(cp))
        segs = [b - a for a, b in zip(cp, cp[1:])]
        assert all(s > 0 for s in segs)
