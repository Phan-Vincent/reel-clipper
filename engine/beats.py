"""Beat detection -> a cut grid the montage snaps to.

Primary path: librosa.beat.beat_track. If librosa/numba is unavailable we fall
back to a simple RMS-energy onset picker over ffmpeg-decoded audio so the tool
still runs (lower musicality, but never a hard failure).
"""
from __future__ import annotations

import math
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import ffmpeg


@dataclass
class BeatGrid:
    tempo: float
    beats: list[float]                 # beat timestamps (s)
    onset_strength: list[float] = field(default_factory=list)  # per-beat accent 0..1

    def cut_points(self, cuts_per_beat: float, min_seg: float, max_seg: float,
                   total: float) -> list[float]:
        """Turn beats into an ordered list of cut timestamps.

        cuts_per_beat > 1 subdivides (adds midpoints); < 1 groups (drops beats).
        Result is clamped so no segment is shorter than min_seg or longer than
        max_seg, and it spans [0, total].
        """
        # Defensive: `while p - cleaned[-1] > max_seg` below would spin forever
        # for a <=0 or non-finite max_seg (cleaned[-1] + max_seg never advances).
        # Config validation rejects these up front, but this function is also
        # reachable via `reel --from-job` (no validation), so normalize here.
        if not math.isfinite(min_seg) or min_seg <= 0:
            min_seg = 0.3
        if not math.isfinite(max_seg) or max_seg <= 0:
            max_seg = 1.6
        if max_seg <= min_seg:
            max_seg = min_seg + 0.1

        beats = [b for b in self.beats if 0 <= b <= total]
        if len(beats) < 2:
            # even grid fallback at ~2 cuts/sec
            step = max(min_seg, min(max_seg, 0.5))
            beats = list(np.arange(0, total, step))

        if cuts_per_beat >= 1:
            n = int(round(cuts_per_beat))
            pts = []
            for i in range(len(beats) - 1):
                a, b = beats[i], beats[i + 1]
                for k in range(n):
                    pts.append(a + (b - a) * k / n)
            pts.append(beats[-1])
        else:
            keep = max(1, int(round(1 / cuts_per_beat)))
            pts = beats[::keep]

        pts = sorted(set([0.0] + [p for p in pts if 0 < p < total] + [total]))
        # enforce min/max segment length
        cleaned = [pts[0]]
        for p in pts[1:]:
            if p - cleaned[-1] < min_seg:
                continue
            while p - cleaned[-1] > max_seg:
                cleaned.append(cleaned[-1] + max_seg)
            cleaned.append(p)
        return cleaned


def _load_audio_ffmpeg(path: str, sr: int = 22050) -> tuple[np.ndarray, int]:
    """Decode any audio/video to mono float32 via ffmpeg (no librosa needed)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path,
           "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with ffmpeg.active_proc(proc):
        out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio decode failed: {err.decode()[-800:]}")
    y = np.frombuffer(out, dtype=np.float32).copy()
    return y, sr


def _fallback_beats(path: str, sr: int = 22050) -> BeatGrid:
    y, sr = _load_audio_ffmpeg(path, sr)
    hop = 512
    n = len(y) // hop
    rms = np.array([np.sqrt(np.mean(y[i * hop:(i + 1) * hop] ** 2) + 1e-9) for i in range(n)])
    # onset = positive first difference of RMS
    diff = np.maximum(0, np.diff(rms, prepend=rms[0]))
    thr = diff.mean() + diff.std()
    times, strengths = [], []
    last = -1.0
    for i, d in enumerate(diff):
        t = i * hop / sr
        if d > thr and t - last > 0.28:  # ~min 210 BPM ceiling
            times.append(round(t, 3))
            strengths.append(float(d))
            last = t
    if strengths:
        m = max(strengths)
        strengths = [s / m for s in strengths]
    # rough tempo from median inter-onset interval
    if len(times) > 2:
        iois = np.diff(times)
        tempo = 60.0 / float(np.median(iois))
    else:
        tempo = 120.0
    return BeatGrid(tempo=tempo, beats=times, onset_strength=strengths)


def detect_beats(path: str) -> BeatGrid:
    """Detect beats + per-beat onset strength. Uses librosa if available."""
    try:
        import librosa  # noqa: WPS433 (optional heavy dep)

        y, sr = librosa.load(path, sr=None, mono=True)
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
        tempo = float(np.atleast_1d(tempo)[0])
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        frames = librosa.time_to_frames(beats, sr=sr)
        frames = np.clip(frames, 0, len(onset_env) - 1)
        strengths = onset_env[frames]
        if strengths.size and strengths.max() > 0:
            strengths = strengths / strengths.max()
        return BeatGrid(tempo=tempo, beats=[float(b) for b in beats],
                        onset_strength=[float(s) for s in strengths])
    except Exception:
        return _fallback_beats(path)


def strong_onset_times(grid: BeatGrid, cut_points: list[float],
                       percentile: float = 75.0) -> set[float]:
    """Cut times that land on a strong musical accent -> good for flash/glitch."""
    if not grid.onset_strength:
        return set()
    thr = float(np.percentile(grid.onset_strength, percentile))
    strong_beats = [b for b, s in zip(grid.beats, grid.onset_strength) if s >= thr]
    hits = set()
    for cp in cut_points:
        if any(abs(cp - sb) < 0.08 for sb in strong_beats):
            hits.add(round(cp, 3))
    return hits
