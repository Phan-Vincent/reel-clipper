"""Selection: diversity constraints, relaxation, ordering, start clamping."""
from collections import Counter

import numpy as np

from engine.analysis import Candidate
from engine.config import RenderConfig
from engine.selection import _order, _select, build_picks

# All-zero thumbs: dedup correlation is exactly 0.0, so tests exercise the
# source-cap / min-gap / relaxation logic deterministically (the dedup branch
# is covered by test_build_bench + the variety repair below).


def _cand(src, start, score=0.5, end=None):
    end = end if end is not None else start + 1.0
    c = Candidate(src=src, start=start, end=end, score=score,
                  thumb=np.zeros(24, np.float32), excitement=score)
    return c


def _cfg(**kw):
    d = dict(target_duration=6.0, cuts_per_beat=1.0, min_gap=5.0, dedup=0.86,
             max_per_clip=0, energy_match=True)
    d.update(kw)
    return RenderConfig(**d)


def test_select_respects_per_source_cap():
    # 4 sources x 3 candidates, starts 6s apart within a source -> pass 1
    # alone fills all 8 slots, so the max_per_clip cap is never relaxed
    pool = []
    for s in "abcd":
        pool += [_cand(s, 0 + 6 * i, score=1.0 - i * 0.01) for i in range(3)]
    cfg = _cfg(max_per_clip=3)
    chosen = _select(pool, 8, cfg)
    counts = Counter(c.src for c in chosen)
    assert len(chosen) == 8
    assert all(counts[s] <= 3 for s in "abcd")


def test_select_enforces_min_gap_while_possible():
    # pass 1 picks the best spread candidates (starts 0 and 5, 5s apart);
    # later relaxation may add closer ones, but never drops these
    pool = [_cand("a", i * 1.0, score=1.0 - i * 0.01) for i in range(8)]
    chosen = _select(pool, 4, _cfg(min_gap=5.0))
    starts = sorted(c.start for c in chosen)
    assert 0.0 in starts and 5.0 in starts


def test_select_relaxation_fills_slots():
    # all shots from one source with tiny gaps: pass 1 blocks them, but the
    # relaxed passes must still fill the requested n
    pool = [_cand("a", i * 0.5, score=1.0) for i in range(30)]
    chosen = _select(pool, 10, _cfg())
    assert len(chosen) == 10


def test_build_picks_clamps_start_inside_source():
    dur = {"a.mp4": 5.0}
    pool = [_cand("a.mp4", 4.5, score=1.0)]
    picks = build_picks(pool, [2.0], dur, _cfg(), [0.0])
    assert picks[0].start >= 0.0
    assert picks[0].start + picks[0].length <= dur["a.mp4"] + 1e-9


def test_build_picks_uses_segment_lengths():
    pool = [_cand("a.mp4", 0.5, score=1.0)]
    picks = build_picks(pool, [1.2, 0.8, 1.0], {"a.mp4": 10.0}, _cfg(),
                        [0.0] * 3)
    assert [p.length for p in picks] == [1.2, 0.8, 1.0]


def test_order_cycles_when_fewer_candidates_than_slots():
    cands = [_cand("a", 0, score=1.0), _cand("b", 0, score=0.8)]
    placed = _order(cands, [0.0, 0.0, 0.0, 0.0], energy_match=False)
    assert len(placed) == 4


def test_order_energy_match_places_most_exciting_on_strongest_beat():
    cands = [_cand("a", 0, score=0.5), _cand("b", 1, score=1.0)]
    for c, e in zip(cands, (0.2, 0.9)):
        c.excitement = e
    placed = _order(cands, [0.1, 1.0], energy_match=True)
    assert placed[1] == cands[1]     # strongest slot gets the most exciting shot


def test_order_variety_repair_breaks_adjacent_same_src():
    # all from the same source -> every adjacency clashes; the repair must
    # still produce a valid full-length placement without dropping shots
    cands = [_cand("a", i, score=1.0) for i in range(4)]
    placed = _order(cands, [0.0] * 4, energy_match=False)
    assert len(placed) == 4
    assert {id(c) for c in placed} == {id(c) for c in cands}   # no drops


def test_build_picks_empty_pool():
    assert build_picks([], [1.0], {"a": 5.0}, _cfg(), [0.0]) == []
