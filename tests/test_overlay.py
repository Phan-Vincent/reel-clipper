"""Overlay caption word-wrapping + segment-style range building (pure)."""
from engine.overlay import _build_ranges, wrap_caption


def test_caption_empty_and_whitespace():
    assert wrap_caption("") == []
    assert wrap_caption("   ") == []


def test_caption_short_stays_one_line():
    assert wrap_caption("hello world") == ["hello world"]


def test_caption_wraps_at_limit():
    lines = wrap_caption("aa " * 30, max_chars=20, max_lines=5)
    assert all(len(l) <= 20 for l in lines)
    assert " ".join(lines).split() == ["aa"] * 30   # no words lost


def test_caption_max_lines_truncates():
    lines = wrap_caption(" ".join(str(i) for i in range(50)),
                         max_chars=10, max_lines=3)
    assert len(lines) == 3


def test_caption_hard_breaks_long_word():
    lines = wrap_caption("x" * 100, max_chars=20, max_lines=3)
    assert lines == ["x" * 20] * 3


def test_caption_long_word_after_short_lines():
    lines = wrap_caption("ok " + "y" * 40, max_chars=20, max_lines=2)
    assert lines == ["ok", "y" * 20]      # second line hard-broken


def test_build_ranges_merges_adjacent_same_style():
    segs = [(30, 1, "a"), (30, 1, "a"), (30, 1, "b"), (30, 2, "b")]
    r = _build_ranges(segs)
    assert r == [[0, 60, (1, "a")], [60, 90, (1, "b")], [90, 120, (2, "b")]]


def test_build_ranges_single_segment():
    assert _build_ranges([(48, 2, "hi")]) == [[0, 48, (2, "hi")]]


def test_build_ranges_empty():
    assert _build_ranges([]) == []
