"""Suggest which part of an uploaded song to use for a short reel.

A full track is 2-4 min; a reel is ~40s. The best 40s is almost never the
intro — it's the drop / chorus / hook. This scans the track's intensity
(RMS + onset strength + low-band "bass" energy), finds high-energy windows
that contain a build-into-drop, snaps their start to a beat / structural
boundary, and returns the top few as suggestions plus a downsampled envelope
for the UI to draw.

librosa is the primary path; a pure-ffmpeg RMS fallback keeps it working if
librosa/numba is unavailable (lower musicality, no beat snap).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .beats import _load_audio_ffmpeg

ENVELOPE_POINTS = 240


@dataclass
class Section:
    start: float
    end: float
    score: float       # 0..1, relative to the best window
    label: str         # e.g. "★ Recommended", "Alt"
    descriptor: str    # e.g. "build into the drop", "peak energy"


def _norm(a: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(a, 5), np.percentile(a, 95)
    return np.clip((a - lo) / (hi - lo + 1e-9), 0.0, 1.0)


def _smooth(a: np.ndarray, w: int) -> np.ndarray:
    if w <= 1:
        return a
    k = np.ones(w) / w
    return np.convolve(a, k, mode="same")


def _downsample(a: np.ndarray, n: int) -> list[float]:
    if len(a) <= n:
        return [round(float(x), 3) for x in a]
    idx = np.linspace(0, len(a), n + 1).astype(int)
    return [round(float(a[idx[i]:idx[i + 1]].mean()), 3) for i in range(n)]


def _rolling_drop(inten: np.ndarray, pre: int) -> np.ndarray:
    """Per-frame positive jump vs the quietest point in the preceding `pre`
    frames — large where a build resolves into a drop."""
    drop = np.zeros_like(inten)
    for i in range(len(inten)):
        lo = inten[max(0, i - pre):i + 1].min()
        drop[i] = max(0.0, inten[i] - lo)
    return drop


def _whole_track(dur: float, tempo: float, env: list[float]) -> dict:
    return {
        "duration": round(dur, 2), "tempo": round(tempo, 1),
        "envelope": env,
        "sections": [asdict(Section(0.0, round(dur, 2), 1.0,
                                     "★ Whole track", "track fits the reel"))],
    }


def _unanalyzable() -> dict:
    """Valid, never-throws result for undecodable/empty audio. Empty sections
    signal the caller (web endpoint / CLI) that no usable analysis was possible
    without breaking the 'analyze_track never raises' contract."""
    return {"duration": 0.0, "tempo": 0.0, "envelope": [], "sections": []}


def _describe(energy: float, drop: float, is_top: bool) -> tuple[str, str]:
    label = "★ Recommended" if is_top else "Alt"
    if drop >= 0.38:
        return label, "build into the drop"
    if energy >= 0.55:
        return label, "peak energy"
    return label, "high-energy section"


def _score_windows(inten: np.ndarray, ftimes: np.ndarray, dur: float,
                   target: float, beats: np.ndarray, bounds: np.ndarray,
                   max_suggestions: int) -> list[Section]:
    fps = (len(inten) - 1) / dur if dur > 0 else 1.0
    maxstart = dur - target
    drop = _rolling_drop(inten, max(1, int(fps * 4)))

    def fidx(t: float) -> int:
        return int(np.clip(round(t * fps), 0, len(inten) - 1))

    def snap(t: float) -> float:
        if beats.size:
            near = beats[np.argmin(np.abs(beats - t))]
            if abs(near - t) < 0.4:
                t = float(near)
        # snapping can land just past the last legal start — re-clamp so
        # start+target never runs off the end of the track.
        return float(max(0.0, min(t, maxstart)))

    def near(t: float, arr: np.ndarray, tol: float) -> bool:
        return bool(arr.size and np.min(np.abs(arr - t)) < tol)

    starts = set(np.arange(0, maxstart + 0.01, 1.5).round(2).tolist())
    starts.update(float(b) for b in bounds if 0 <= b <= maxstart)
    peak_t = float(ftimes[int(np.argmax(inten))])
    starts.update(s for s in (peak_t, peak_t - 2, peak_t - 4) if 0 <= s <= maxstart)

    pad = int(fps * 2)   # credit a drop that lands just at the window edge
    scored = []
    for s in starts:
        s = max(0.0, min(s, maxstart))
        f0, f1 = fidx(s), fidx(s + target)
        seg = inten[f0:f1 + 1]
        if len(seg) < 2:
            continue
        energy = float(seg.mean())
        peak = float(seg.max())
        seg_drop = float(drop[f0:f1 + 1].max())
        start_drop = float(drop[max(0, f0 - pad):f0 + pad + 1].max())  # opens on a drop?
        # reward a drop that lands early in the window (build then hit)
        drop_pos = int(np.argmax(drop[f0:f1 + 1])) / max(1, len(seg))
        early = 1.0 - min(1.0, abs(drop_pos - 0.25) * 2)
        bnd = 1.0 if near(s, bounds, 1.0) else 0.0
        score = (0.38 * energy + 0.16 * peak + 0.20 * seg_drop
                 + 0.08 * early * seg_drop + 0.12 * start_drop + 0.06 * bnd)
        scored.append((score, snap(s), energy, max(seg_drop, start_drop)))

    scored.sort(reverse=True)
    picks, best = [], (scored[0][0] if scored else 1.0)
    for score, s, energy, sd in scored:
        if any(abs(s - p[1]) < target * 0.6 for p in picks):
            continue
        picks.append((score, s, energy, sd))
        if len(picks) >= max_suggestions:
            break

    out = []
    for i, (score, s, energy, sd) in enumerate(picks):
        label, desc = _describe(energy, sd, i == 0)
        out.append(Section(round(float(s), 2), round(float(min(s + target, dur)), 2),
                           round(float(score / (best + 1e-9)), 3), label, desc))
    return out


def analyze_track(path: str, target: float, max_suggestions: int = 3,
                  sr: int = 22050) -> dict:
    """Return {duration, tempo, envelope[0..1], sections[]} for the track."""
    target = max(3.0, float(target))
    try:
        import librosa

        y, sr = librosa.load(path, sr=sr, mono=True)
        dur = len(y) / sr
        hop = 512
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
        S = np.abs(librosa.stft(y, n_fft=1024, hop_length=hop))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=1024)
        low = S[freqs < 250].mean(axis=0)
        L = min(len(rms), len(onset), len(low))
        rms, onset, low = rms[:L], onset[:L], low[:L]
        ftimes = librosa.frames_to_time(np.arange(L), sr=sr, hop_length=hop)
        inten = 0.5 * _norm(rms) + 0.25 * _norm(onset) + 0.25 * _norm(low)
        inten = _smooth(inten, max(1, int(round(sr / hop * 0.6))))

        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
        tempo = float(np.atleast_1d(tempo)[0])
        if not np.isfinite(tempo):     # silent/degenerate track -> keep JSON valid
            tempo = 0.0
        beats = np.asarray(beats, dtype=float)

        bounds = np.array([])
        try:
            mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop, n_mfcc=13)
            bframes = librosa.time_to_frames(beats, sr=sr, hop_length=hop)
            bframes = bframes[(bframes >= 0) & (bframes < mfcc.shape[1])]
            if len(bframes) > 6:
                msync = librosa.util.sync(mfcc, bframes, aggregate=np.mean)
                k = min(8, msync.shape[1] - 1)
                bidx = librosa.segment.agglomerative(msync, k)
                bounds = beats[np.clip(bidx, 0, len(beats) - 1)]
        except Exception:
            bounds = beats[::4] if beats.size else np.array([])

        env = _downsample(inten, ENVELOPE_POINTS)
        if dur <= target + 1.5:
            return _whole_track(dur, tempo, env)
        sections = _score_windows(inten, ftimes, dur, target, beats, bounds,
                                  max_suggestions)
        return {"duration": round(dur, 2), "tempo": round(tempo, 1),
                "envelope": env, "sections": [asdict(s) for s in sections]}
    except Exception:
        return _analyze_fallback(path, target, max_suggestions, sr)


def _analyze_fallback(path: str, target: float, max_suggestions: int,
                      sr: int) -> dict:
    # This is the last line of defense — it must NEVER raise. Undecodable audio
    # (ffmpeg error) or a sub-hop clip (n < 2 -> empty rms -> percentile crash)
    # both return a valid, empty-section result instead of throwing.
    try:
        y, sr = _load_audio_ffmpeg(path, sr)
    except Exception:
        return _unanalyzable()
    dur = len(y) / sr
    hop = 1024
    n = len(y) // hop
    if n < 2:
        return _whole_track(dur, 0.0, []) if dur > 0 else _unanalyzable()
    rms = np.array([np.sqrt(np.mean(y[i * hop:(i + 1) * hop] ** 2) + 1e-9)
                    for i in range(n)])
    inten = _smooth(_norm(rms), max(1, int(round(sr / hop * 0.6))))
    ftimes = np.arange(n) * hop / sr
    env = _downsample(inten, ENVELOPE_POINTS)
    if dur <= target + 1.5:
        return _whole_track(dur, 0.0, env)
    sections = _score_windows(inten, ftimes, dur, target,
                              np.array([]), np.array([]), max_suggestions)
    return {"duration": round(dur, 2), "tempo": 0.0,
            "envelope": env, "sections": [asdict(s) for s in sections]}
