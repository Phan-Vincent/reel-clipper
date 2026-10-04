"""Beat-sync verification: pure alignment + report formatting."""
from engine.verify import beat_alignment, format_report


def test_beat_alignment_perfectly_on_beat():
    beats = [0.5, 1.0, 1.5, 2.0]
    cuts = [0.5, 1.0, 1.5]              # segment boundaries land exactly on beats
    r = beat_alignment(beats, cuts)
    assert r["mean_off"] == 0.0 and r["max_off"] == 0.0
    assert r["pct_on_beat"] == 100.0 and r["n"] == 3


def test_beat_alignment_measures_offset():
    beats = [1.0, 2.0, 3.0]
    cuts = [1.4, 2.5]                   # off by 0.4 and 0.5
    r = beat_alignment(beats, cuts)
    assert r["n"] == 2
    assert abs(r["max_off"] - 0.5) < 1e-9
    assert r["pct_on_beat"] == 0.0
    assert r["worst_cut"] == 2.5


def test_beat_alignment_empty_beats_is_safe():
    r = beat_alignment([], [0.0, 1.0])
    assert r["n"] == 0 and r["pct_on_beat"] == 100.0


def test_beat_alignment_ignores_nonfinite():
    r = beat_alignment([1.0, 2.0], [float("nan"), 1.0, 2.0])
    assert r["n"] == 2 and r["mean_off"] == 0.0


def test_format_report_has_headline():
    rep = {"n_cuts": 12, "tempo": 128.0, "pct_on_beat": 95.0,
           "mean_off": 0.02, "max_off": 0.1, "worst_cut": 3.2}
    out = format_report(rep)
    assert "12 cuts" in out and "95.0%" in out and "3.2s" in out
