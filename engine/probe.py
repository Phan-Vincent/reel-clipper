"""Media metadata helpers built on ffprobe."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import ffmpeg


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool


def _parse_fps(rate: str) -> float:
    try:
        if "/" in rate:
            n, d = rate.split("/")
            return float(n) / float(d) if float(d) else 0.0
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


@lru_cache(maxsize=256)
def video_info(path: str | Path) -> VideoInfo:
    """ffprobe metadata for a clip. Cached: the same path is probed repeatedly
    across prepare() and storyboard apply_edits() — job dirs are unique per
    upload, so the cached result can't go stale within a run."""
    info = ffmpeg.probe(path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        raise ffmpeg.FFmpegError(f"No video stream in {path}")
    fps = _parse_fps(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0")
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0.0)
    return VideoInfo(
        path=str(path),
        width=int(v.get("width", 0)),
        height=int(v.get("height", 0)),
        fps=fps or 30.0,
        duration=dur,
        has_audio=a is not None,
    )


def creation_dt(path: str | Path) -> datetime | None:
    """Best-effort shooting time, in LOCAL time for calendar-day grouping.

    Priority: com.apple.quicktime.creationdate (has the shoot's UTC offset —
    what iPhones write) -> format/stream creation_time (UTC) -> file mtime.
    Embedded tags survive uploads/copies; mtime is the last resort.
    """
    try:
        info = ffmpeg.probe(path)
    except Exception:
        return None
    tags = dict(info.get("format", {}).get("tags") or {})
    for s in info.get("streams", []):
        for k, v in (s.get("tags") or {}).items():
            tags.setdefault(k, v)

    def parse(v: str) -> datetime | None:
        v = v.strip().replace("Z", "+00:00")
        for fmt in (None, "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.fromisoformat(v) if fmt is None \
                    else datetime.strptime(v, fmt)
            except ValueError:
                continue
        return None

    qt = tags.get("com.apple.quicktime.creationdate")
    if qt:
        dt = parse(qt)
        if dt:                       # already carries the local shoot offset
            return dt.replace(tzinfo=None) if dt.tzinfo is None \
                else datetime(*dt.timetuple()[:6])
    ct = tags.get("creation_time")
    if ct:
        dt = parse(ct)
        if dt:                       # UTC -> convert to this machine's local
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return datetime(*dt.astimezone().timetuple()[:6])
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime)
    except OSError:
        return None


def is_video(path: str | Path) -> bool:
    try:
        video_info(path)
        return True
    except Exception:
        return False
