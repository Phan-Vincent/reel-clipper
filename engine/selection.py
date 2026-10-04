"""Pick the best moments and order them onto the beat grid.

Greedy by score with three diversity constraints that matter for long
single-location footage:
  1. perceptual de-dup (thumbnail correlation) -> kills repeated shots
     (e.g. the same TV-of-the-match framing over and over),
  2. temporal spread -> no two picks within `min_gap` s of the same source,
  3. a per-source cap.
Then order so the most *exciting* shots land on the strongest musical beats.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .analysis import Candidate
from .config import RenderConfig


@dataclass
class Pick:
    src: str
    start: float
    length: float
    trans: str = "none"   # segment-start transition (INTRO_KINDS or WHIP_IN_KINDS)
    outro: str = "none"   # segment-end whip-out (transitions.WHIP_OUT_KINDS)
    ramp: str = "none"    # speed ramp (see transitions.RAMP_KINDS)
    excitement: float = 0.0  # carried from the candidate, for hero-ramp choice
    src_fps: float = 30.0    # source fps (slow-mo ramps need >=48)
    thumb_jpg: str = ""      # candidate mid-frame jpg (storyboard UI)
    day: int = 0             # auto-chapter day index (0 = single-day reel)
    caption: str = ""        # burned-in caption (auto-captions or per-slot override)


def _thumb_corr_max(t: np.ndarray, chosen: list[Candidate]) -> float:
    if not chosen:
        return 0.0
    M = np.stack([c.thumb for c in chosen])
    if M.shape[1] != t.shape[0]:
        return 0.0
    return float(np.max(M @ t))


def _select(pool: list[Candidate], n: int, cfg: RenderConfig) -> list[Candidate]:
    ordered = sorted(pool, key=lambda c: c.score, reverse=True)
    n_src = len(set(c.src for c in pool)) or 1
    auto_cap = max(2, math.ceil(n / n_src) + 2)
    cap = cfg.max_per_clip if cfg.max_per_clip > 0 else auto_cap

    chosen: list[Candidate] = []
    per_src: dict[str, int] = {}
    starts: dict[str, list[float]] = {}

    def try_add(c, cap_, gap_, dedup_) -> bool:
        if per_src.get(c.src, 0) >= cap_:
            return False
        if any(abs(c.start - s) < gap_ for s in starts.get(c.src, [])):
            return False
        if _thumb_corr_max(c.thumb, chosen) > dedup_:
            return False
        chosen.append(c)
        per_src[c.src] = per_src.get(c.src, 0) + 1
        starts.setdefault(c.src, []).append(c.start)
        return True

    # pass 1 — full constraints
    for c in ordered:
        if len(chosen) >= n:
            break
        try_add(c, cap, cfg.min_gap, cfg.dedup)

    # progressive relaxation to fill remaining slots
    for cap_, gap_, dd in [(cap + 2, cfg.min_gap / 2, cfg.dedup + 0.06),
                           (cap + 5, 1.0, 0.95),
                           (9999, 0.0, 1.01)]:
        if len(chosen) >= n:
            break
        chosen_set = set(id(c) for c in chosen)
        for c in ordered:
            if len(chosen) >= n:
                break
            if id(c) not in chosen_set:
                if try_add(c, cap_, gap_, dd):
                    chosen_set.add(id(c))
    return chosen


def _order(chosen: list[Candidate], slot_onsets: list[float],
           energy_match: bool) -> list[Candidate]:
    n = len(slot_onsets)
    if not chosen:
        return []
    if len(chosen) < n:  # cycle to fill (footage thinner than the grid)
        chosen = (chosen * (n // len(chosen) + 1))[:n]

    if energy_match and any(slot_onsets):
        by_excite = sorted(chosen, key=lambda c: c.excitement, reverse=True)
        slot_rank = sorted(range(n), key=lambda i: slot_onsets[i], reverse=True)
        placed: list[Candidate] = [None] * n  # type: ignore
        for k, slot in enumerate(slot_rank):
            placed[slot] = by_excite[k]
    else:
        placed = sorted(chosen, key=lambda c: c.score, reverse=True)[:n]

    # variety repair: break up adjacent same-source or same-scene shots (e.g.
    # repeated TV-screen framings coming from *different* source clips, which
    # the same-src check alone misses). A swap is accepted only if it fixes
    # position i without creating a new clash at either end of position j.
    def _clash(a: Candidate, b: Candidate) -> bool:
        return a.src == b.src or float(a.thumb @ b.thumb) > 0.78

    for i in range(1, n):
        if not _clash(placed[i - 1], placed[i]):
            continue
        for j in range(i + 1, min(n, i + 10)):
            placed[i], placed[j] = placed[j], placed[i]
            ok_i = (not _clash(placed[i - 1], placed[i])
                    and (i + 1 >= n or not _clash(placed[i], placed[i + 1])))
            ok_j = (not _clash(placed[j - 1], placed[j])
                    and (j + 1 >= n or not _clash(placed[j], placed[j + 1])))
            if ok_i and ok_j:
                break
            placed[i], placed[j] = placed[j], placed[i]   # revert
    return placed


def build_picks(pool: list[Candidate], segment_lengths: list[float],
                durations: dict[str, float], cfg: RenderConfig,
                slot_onsets: list[float]) -> list[Pick]:
    n = len(segment_lengths)
    if not pool or n == 0:
        return []
    chosen = _select(pool, n, cfg)
    placed = _order(chosen, slot_onsets, cfg.energy_match)

    picks: list[Pick] = []
    for i in range(n):
        c = placed[i % len(placed)]
        L = segment_lengths[i]
        dur = durations.get(c.src, c.end + L)
        center = (c.start + c.end) / 2
        start = max(0.0, min(center - L / 2, max(0.0, dur - L)))
        picks.append(Pick(src=c.src, start=round(start, 3), length=round(L, 3),
                          excitement=c.excitement, thumb_jpg=c.thumb_jpg))
    return picks
