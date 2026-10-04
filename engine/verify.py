"""Beat-sync verification: measure how well a finished montage's cuts land on
the musical beat grid.

Two pieces:
- `beat_alignment` is the pure, testable core — how far each cut time sits
  from the nearest beat.
- `verify_rendered` scene-detects the actual hard cuts in a rendered montage
  (ffmpeg `select=gt(scene,...)`), then aligns them against the grid, so it
  measures the RENDERED result, not the plan.
"""
from __future__ import annotations

import re
import subprocess

import numpy as np

from . import ffmpeg
from .beats import BeatGrid

ON_BEAT_TOL = 0.06      # ~2 frames @ 30fps


def beat_alignment(beats: list[float], cut_times: list[float]) -> dict:
    """How far each cut lands from the nearest beat.

    `cut_times` are the montage's segment boundaries (scene-detected, so the
    t=0 start and the final frame never appear here). Returns
    {mean_off, max_off, worst_cut, n, pct_on_beat}.
    """
    beats = sorted(b for b in beats if np.isfinite(b))
    cuts = [c for c in cut_times if np.isfinite(c)]
    if not beats:
        return {"mean_off": 0.0, "max_off": 0.0, "worst_cut": None,
                "n": 0, "pct_on_beat": 100.0}
    offs, worst = [], (0.0, None)
    for cp in cuts:
        d = min(abs(cp - b) for b in beats)
        offs.append(d)
        if d > worst[0]:
            worst = (d, round(cp, 3))
    on = sum(1 for d in offs if d <= ON_BEAT_TOL)
    return {
        "mean_off": round(float(np.mean(offs)) if offs else 0.0, 4),
        "max_off": round(worst[0], 4),
        "worst_cut": worst[1],
        "n": len(offs),
        "pct_on_beat": round(100.0 * on / len(offs), 1) if offs else 100.0,
    }


def detect_cut_times(path: str, thr: float = 0.30) -> list[float]:
    """Hard-cut timestamps in a rendered montage via ffmpeg scene detection."""
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", path,
         "-filter:v", f"select='gt(scene,{thr})',showinfo", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with ffmpeg.active_proc(proc):
        try:
            _out, err = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", err)]


def verify_rendered(path: str, grid: BeatGrid) -> dict:
    """Detect the montage's real cuts and align them against the beat grid."""
    cuts = detect_cut_times(path)
    report = beat_alignment(grid.beats, cuts)
    report["n_cuts"] = len(cuts)
    report["tempo"] = round(float(grid.tempo), 1)
    return report


def format_report(report: dict) -> str:
    lines = [
        f"beat-sync: {report.get('n_cuts', 0)} cuts @ {report.get('tempo', 0)} BPM",
        f"  on-beat: {report['pct_on_beat']}%  "
        f"(mean {report['mean_off']}s, max {report['max_off']}s"
        + (f" at {report['worst_cut']}s" if report.get("worst_cut") else "") + ")",
    ]
    return "\n".join(lines)
