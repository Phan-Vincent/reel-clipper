"""Chapter slot allocation + day grouping. allocate_slots has a hard
invariant ("sum is ALWAYS exactly n_slots") — this is where it's pinned."""
from engine.chapters import Chapters, allocate_slots, detect_chapters


def _alloc(n, share):
    return allocate_slots(n, {int(k): float(v) for k, v in share.items()})


def test_sum_is_always_exactly_n():
    for n in (1, 5, 17, 40):
        a = _alloc(n, {1: 10, 2: 5, 3: 1})
        assert sum(a.values()) == n


def test_every_allocated_day_gets_at_least_one():
    a = _alloc(10, {1: 10, 2: 5, 3: 1})
    assert all(v >= 1 for v in a.values())


def test_more_days_than_slots_keeps_biggest_days():
    a = _alloc(2, {1: 100, 2: 5, 3: 4, 4: 3})
    assert sum(a.values()) == 2
    assert set(a) == {1, 2}          # biggest-footage days win, one slot each


def test_footage_proportional():
    a = _alloc(12, {1: 3, 2: 1})
    # day1 has 75% of footage -> ~9 slots, day2 ~3
    assert a[1] > a[2] and 7 <= a[1] <= 11


def test_zero_or_negative_slots():
    assert _alloc(0, {1: 1}) == {}
    assert _alloc(-3, {1: 1}) == {}


def test_empty_share():
    assert _alloc(5, {}) == {}


def test_single_day():
    assert _alloc(8, {1: 42}) == {1: 8}


def test_detect_chapters_groups_by_calendar_day(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr("engine.chapters.creation_dt", lambda c: {
        "a.mp4": datetime(2024, 7, 21, 10, 0),
        "b.mp4": datetime(2024, 7, 22, 9, 0),
        "c.mp4": None,                                   # undated -> day 1
    }.get(c))
    ch = detect_chapters(["a.mp4", "b.mp4", "c.mp4"])
    assert ch is not None and ch.n_days == 2
    assert ch.day_of["a.mp4"] == 1 and ch.day_of["b.mp4"] == 2
    assert ch.day_of["c.mp4"] == 1                       # undated joins day 1
    assert ch.dates == {1: "JUL 21", 2: "JUL 22"}


def test_detect_chapters_none_for_single_day(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr("engine.chapters.creation_dt",
                        lambda c: datetime(2024, 7, 21, 12, 0))
    assert detect_chapters(["a.mp4", "b.mp4"]) is None


def test_detect_chapters_none_without_dates(monkeypatch):
    monkeypatch.setattr("engine.chapters.creation_dt", lambda c: None)
    assert detect_chapters(["a.mp4"]) is None
