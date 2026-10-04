"""Per-clip analysis cache + candidate thumbnails.

Analysis is the slow phase (full decode of every clip at ~3fps). Its result
depends only on the clip's bytes and the analysis parameters, so candidates are
cached keyed by a fast content fingerprint (first 1MB + size — mtime excluded
so copies still hit). Candidate mid-frame thumbnails are written at analysis
time into the cache dir; the storyboard UI serves them from there.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
from pathlib import Path

import numpy as np

from .analysis import Candidate, adaptive_fps, analyze_clip
from .probe import video_info

SCHEMA_VERSION = 2          # bump when analyze_clip's signals/params change
CACHE_ROOT = Path("work/analysis_cache")
MAX_ENTRIES = 60            # LRU prune cap (per-clip entries)
_PRUNE_LOCK = threading.Lock()


def clip_fingerprint(path: str, params_key: str) -> str:
    p = Path(path)
    h = hashlib.sha1()
    with p.open("rb") as f:
        h.update(f.read(1 << 20))
    h.update(f"|{p.stat().st_size}|v{SCHEMA_VERSION}|{params_key}".encode())
    return h.hexdigest()[:20]


def _cand_to_dict(c: Candidate) -> dict:
    return {
        "start": c.start, "end": c.end, "sharp": c.sharp, "motion": c.motion,
        "exposure": c.exposure, "colorful": c.colorful, "faces": c.faces,
        "audio_raw": c.audio_raw, "audio_n": c.audio_n,
        "thumb": [round(float(x), 5) for x in c.thumb],
        "hist": [round(float(x), 6) for x in c.hist],
        "thumb_jpg": c.thumb_jpg,
    }


def _cand_from_dict(d: dict, src: str) -> Candidate:
    return Candidate(
        src=src, start=d["start"], end=d["end"], sharp=d["sharp"],
        motion=d["motion"], exposure=d["exposure"], colorful=d["colorful"],
        faces=d["faces"], audio_raw=d["audio_raw"], audio_n=d["audio_n"],
        thumb=np.asarray(d["thumb"], np.float32),
        hist=np.asarray(d["hist"], np.float32),
        thumb_jpg=d.get("thumb_jpg", ""),
    )


def _prune() -> None:
    with _PRUNE_LOCK:
        if not CACHE_ROOT.exists():
            return
        entries = sorted(CACHE_ROOT.glob("*.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for meta in entries[MAX_ENTRIES:]:
            shutil.rmtree(meta.with_suffix(""), ignore_errors=True)
            meta.unlink(missing_ok=True)


def cached_analyze(path: str, use_faces: bool = True) -> list[Candidate]:
    """analyze_clip with a transparent disk cache (candidates + thumbnails)."""
    # sample rate adapts to clip length so long footage is cheap to analyze;
    # the fps is part of the cache key + schema version
    fps = 3.0
    try:
        fps = adaptive_fps(video_info(path).duration)
    except Exception:               # noqa: BLE001 — unprobeable -> full rate
        fps = 3.0
    params_key = f"faces={use_faces};fps={fps}"
    try:
        fp = clip_fingerprint(path, params_key)
    except OSError:
        return analyze_clip(path, use_faces=use_faces, target_fps=fps)

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    meta = CACHE_ROOT / f"{fp}.json"
    thumb_dir = CACHE_ROOT / fp

    if meta.exists():
        try:
            data = json.loads(meta.read_text())
            meta.touch()          # LRU bump
            return [_cand_from_dict(d, path) for d in data["candidates"]]
        except Exception:         # corrupt entry -> reanalyze
            meta.unlink(missing_ok=True)

    cands = analyze_clip(path, use_faces=use_faces, target_fps=fps,
                         thumb_dir=str(thumb_dir))
    try:
        meta.write_text(json.dumps(
            {"src": path, "candidates": [_cand_to_dict(c) for c in cands]}))
        _prune()
    except OSError:
        pass
    return cands
