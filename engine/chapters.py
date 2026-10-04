"""Auto-chaptering: group clips into DAY 01 / DAY 02 / ... by shooting date.

A trip's footage spans calendar days; the reel should play chronologically with
the camcorder DAY counter advancing at each boundary. Dates come from embedded
metadata (survives upload copies); clips with no readable date join the first
day so they stay usable.
"""
from __future__ import annotations

from dataclasses import dataclass

from .probe import creation_dt


@dataclass
class Chapters:
    day_of: dict[str, int]        # clip path -> 1-based day index
    dates: dict[int, str]         # day index -> "JUL 21" style label
    n_days: int


def detect_chapters(clips: list[str]) -> Chapters | None:
    """Returns None unless the footage genuinely spans >= 2 calendar days."""
    dts = {c: creation_dt(c) for c in clips}
    dated = {c: d for c, d in dts.items() if d is not None}
    if not dated:
        return None
    days = sorted({d.date() for d in dated.values()})
    if len(days) < 2:
        return None
    idx = {day: i + 1 for i, day in enumerate(days)}
    day_of = {c: idx[d.date()] for c, d in dated.items()}
    for c in clips:                       # undated clips join day 1
        day_of.setdefault(c, 1)
    dates = {i + 1: day.strftime("%b %d").upper() for i, day in enumerate(days)}
    return Chapters(day_of=day_of, dates=dates, n_days=len(days))


def allocate_slots(n_slots: int, share: dict[int, float]) -> dict[int, int]:
    """Split n_slots across days proportional to footage share.

    Sum is ALWAYS exactly n_slots. Every allocated day gets >= 1 slot; when
    there are more days than slots, the biggest-footage days are kept (one
    slot each) and the rest are dropped rather than over-allocating.
    Deficit slots go to the day with the largest unfilled entitlement
    (raw - alloc), so a min-1-bumped day can't outbid a footage-heavy one.
    """
    days = sorted(share, key=lambda d: share[d], reverse=True)
    if not days or n_slots <= 0:
        return {}
    if n_slots <= len(days):
        return {d: 1 for d in days[:n_slots]}
    total = sum(share.values()) or 1.0
    raw = {d: n_slots * share[d] / total for d in days}
    alloc = {d: max(1, int(raw[d])) for d in days}
    while sum(alloc.values()) < n_slots:
        under = [d for d in days if alloc[d] < raw[d]] or days
        d = max(under, key=lambda k: raw[k] - alloc[k])
        alloc[d] += 1
    while sum(alloc.values()) > n_slots:
        over = [d for d in days if alloc[d] > max(1.0, raw[d])] \
            or [d for d in days if alloc[d] > 1]
        d = max(over, key=lambda k: alloc[k] - raw[k])
        alloc[d] -= 1
    return alloc
