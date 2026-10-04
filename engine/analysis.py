"""Per-clip analysis for highlight selection.

For event/long footage, "interesting" is driven mostly by **source-audio
energy** (cheers, laughter, goals spike the loudness) plus visual quality
gates, colorfulness, and a small face/people bonus — NOT by sharpness+motion,
which barely discriminate across a single-location gathering (and vanish on
120fps slow-mo).

Frames are decoded via an ffmpeg pipe at a low fps/resolution (far faster than
per-frame OpenCV grab on long 4K/120fps clips). Each ~1s window becomes a
scored Candidate carrying a perceptual thumbnail for de-duplication.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import ffmpeg
from .probe import VideoInfo, video_info

# Haar cascades (frontal + profile) — loaded once per thread (CascadeClassifier
# instances are not safe to share across concurrent detectMultiScale calls).
_TLS = threading.local()


def _cascades():
    cs = getattr(_TLS, "cascades", None)
    if cs is None:
        import cv2
        base = cv2.data.haarcascades
        cs = [
            cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml"),
            cv2.CascadeClassifier(base + "haarcascade_profileface.xml"),
        ]
        _TLS.cascades = cs
    return cs


@dataclass
class Candidate:
    src: str
    start: float
    end: float
    score: float = 0.0
    # raw signals (normalized later, across the whole pool)
    sharp: float = 0.0
    motion: float = 0.0
    exposure: float = 1.0
    colorful: float = 0.0
    faces: float = 0.0
    audio_raw: float = 0.0
    audio_n: float = 0.0        # per-clip normalized audio energy (0..1)
    excitement: float = 0.0     # combined "how exciting" for energy-matching
    thumb: np.ndarray = field(default_factory=lambda: np.zeros(1, np.float32))
    hist: np.ndarray = field(default_factory=lambda: np.zeros(1, np.float32))
    thumb_jpg: str = ""         # mid-frame jpg on disk (storyboard UI)

    @property
    def length(self) -> float:
        return self.end - self.start


# ---- fast frame decode via ffmpeg pipe --------------------------------------

def _decode_frames(info: VideoInfo, target_fps: float, width: int):
    """Yield (t_seconds, bgr ndarray) sampled at target_fps, scaled to `width`."""
    h = max(2, int(round(info.height * width / max(1, info.width))))
    h -= h % 2
    frame_bytes = width * h * 3
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", info.path,
           "-vf", f"fps={target_fps},scale={width}:{h}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=frame_bytes * 4)
    idx = 0
    assert proc.stdout is not None
    with ffmpeg.active_proc(proc):
        try:
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                arr = np.frombuffer(buf, np.uint8).reshape(h, width, 3)
                yield idx / target_fps, arr
                idx += 1
        finally:
            proc.stdout.close()
            proc.wait()


# ---- audio energy envelope ---------------------------------------------------

def _audio_envelope(path: str, sr: int = 8000, hop: float = 0.5):
    """Return (times, rms) of the source audio, or None if silent/absent."""
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", path,
         "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with ffmpeg.active_proc(proc):
        raw, _err = proc.communicate()
    if not raw:
        return None
    y = np.frombuffer(raw, np.float32)
    w = max(1, int(sr * hop))
    n = len(y) // w
    if n < 2:
        return None
    rms = np.array([np.sqrt(np.mean(y[i * w:(i + 1) * w] ** 2) + 1e-9)
                    for i in range(n)])
    times = np.arange(n) * hop
    return times, rms


# ---- per-frame metrics -------------------------------------------------------

def _colorfulness(bgr: np.ndarray) -> float:
    b, g, r = (bgr[:, :, 0].astype(np.float32),
               bgr[:, :, 1].astype(np.float32),
               bgr[:, :, 2].astype(np.float32))
    rg = r - g
    yb = 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2)
                 + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def _count_faces(gray: np.ndarray) -> int:
    total = 0
    for c in _cascades():
        if c.empty():
            continue
        found = c.detectMultiScale(gray, 1.15, 4, minSize=(20, 20))
        total += len(found)
    return total


def _thumb(gray: np.ndarray) -> np.ndarray:
    import cv2
    t = cv2.resize(gray, (12, 12)).astype(np.float32).ravel()
    t -= t.mean()
    n = np.linalg.norm(t)
    return t / n if n > 1e-6 else t


def adaptive_fps(duration: float) -> float:
    """Sample fewer frames on very long clips so a 30-min event video doesn't
    take minutes to analyze. Signals are ~1s windows, so 1-2fps still resolves
    them; short clips stay at the full 3fps."""
    if duration > 900:   # >15 min
        return 1.0
    if duration > 300:   # >5 min
        return 2.0
    return 3.0


def analyze_clip(path: str, use_faces: bool = True,
                 window: float = 1.0, target_fps: float = 3.0,
                 width: int = 360, thumb_dir: str | None = None) -> list[Candidate]:
    import cv2

    info = video_info(path)
    env = _audio_envelope(path)
    if thumb_dir:
        Path(thumb_dir).mkdir(parents=True, exist_ok=True)

    def audio_at(t0: float, t1: float) -> float:
        if env is None:
            return 0.0
        times, rms = env
        m = (times >= t0) & (times < t1 + 0.5)
        return float(rms[m].max()) if m.any() else 0.0

    frames = list(_decode_frames(info, target_fps, width))
    if len(frames) < 2:
        return []
    step = max(1, int(round(window * target_fps)))

    cands: list[Candidate] = []
    for i in range(0, len(frames) - 1, step):
        chunk = frames[i:i + step + 1]
        if len(chunk) < 2:
            break
        t0 = chunk[0][0]
        t1 = t0 + window
        grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for _, f in chunk]
        sharp = max(float(cv2.Laplacian(g, cv2.CV_64F).var()) for g in grays)
        motions = [float(np.mean(cv2.absdiff(grays[k], grays[k - 1])))
                   for k in range(1, len(grays))]
        motion = float(np.mean(motions)) if motions else 0.0
        mid = chunk[len(chunk) // 2][1]
        mid_gray = grays[len(grays) // 2]
        lum = mid_gray.astype(np.float32) / 255.0
        mean_l = float(lum.mean())
        clipped = float(np.mean(lum < 0.03) + np.mean(lum > 0.97))
        exposure = max(0.0, 1.0 - abs(mean_l - 0.5) * 1.6) * (1.0 - clipped)
        if mean_l < 0.05 or mean_l > 0.96:
            exposure *= 0.1
        colorful = _colorfulness(mid)
        faces = float(_count_faces(mid_gray)) if use_faces else 0.0
        hsv = cv2.cvtColor(mid, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0], None, [24], [0, 180]).flatten()
        hist = hist / (hist.sum() + 1e-6)
        thumb_jpg = ""
        if thumb_dir:
            thumb_jpg = str(Path(thumb_dir) / f"c{len(cands):04d}.jpg")
            cv2.imwrite(thumb_jpg, mid, [cv2.IMWRITE_JPEG_QUALITY, 72])
        cands.append(Candidate(
            src=path, start=round(t0, 3), end=round(t1, 3),
            sharp=sharp, motion=motion, exposure=exposure, colorful=colorful,
            faces=faces, audio_raw=audio_at(t0, t1),
            thumb=_thumb(mid_gray), hist=hist, thumb_jpg=thumb_jpg,
        ))

    # per-clip audio normalization (each clip's own loud/quiet range)
    araw = np.array([c.audio_raw for c in cands])
    if araw.max() > araw.min():
        an = (araw - araw.min()) / (araw.max() - araw.min())
        for c, v in zip(cands, an):
            c.audio_n = float(v)
    return cands


def rescore(cands: list[Candidate], use_source_audio: bool = True) -> None:
    """Normalize signals across the whole pool and combine into a score.

    Audio energy is the primary driver (event footage); sharpness/exposure act
    as quality gates; colorfulness/motion/faces are minor boosts.
    """
    if not cands:
        return

    def norm(vals):
        a = np.asarray(vals, np.float32)
        lo, hi = np.percentile(a, 5), np.percentile(a, 95)
        return np.clip((a - lo) / (hi - lo + 1e-6), 0, 1)

    sh_n = norm([c.sharp for c in cands])
    mo_n = norm([c.motion for c in cands])
    col_n = norm([c.colorful for c in cands])
    # global audio normalization so a loud cheer anywhere beats a quiet clip
    aud_g = norm([c.audio_raw for c in cands])
    faces = np.array([c.faces for c in cands])
    face_n = np.clip(faces / 3.0, 0, 1)

    for i, c in enumerate(cands):
        audio = max(float(aud_g[i]), 0.6 * c.audio_n) if use_source_audio else 0.0
        motion_life = min(1.0, float(mo_n[i]) * 1.3)   # reward motion, no penalty
        interest = (0.42 * audio
                    + 0.16 * float(col_n[i])
                    + 0.16 * motion_life
                    + 0.14 * float(sh_n[i])
                    + 0.12 * float(face_n[i]))
        if not use_source_audio:
            interest = (0.34 * float(col_n[i]) + 0.30 * motion_life
                        + 0.24 * float(sh_n[i]) + 0.12 * float(face_n[i]))
        gate = c.exposure * (0.5 + 0.5 * min(1.0, float(sh_n[i]) / 0.15 + 0.3))
        c.score = interest * gate
        c.excitement = 0.6 * audio + 0.25 * motion_life + 0.15 * float(face_n[i])
