"""Original clip audio -> a single track aligned to the montage timeline.

The montage video is silent (segments render with -an). When the user wants
the clips' original sound mixed under the music, each pick's source-audio
window is extracted and concatenated into one wav whose length equals the
montage duration; the mux then sidechain-ducks the music against it.

All windows are extracted in ONE ffmpeg command (per-input `-ss` + `-vn`, so
video is never decoded). Speed-ramped segments still contribute silence: the
setpts ramp is piecewise-constant speed that would need a matching piecewise
`atempo` chain (see transitions.atempo_chain for the per-speed factorization),
which the single-command concat path doesn't model yet — and ramped shots are
stylized moments where natural audio matters least.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import ffmpeg
from .probe import video_info

logger = logging.getLogger("reel.sourceaudio")

SR = 44100


def source_windows(picks: list[dict], has_audio) -> list[dict]:
    """Per-pick audio windows aligned to the montage timeline.

    Each entry: {"src": path, "start", "length"} or {"src": None, "length"}
    for silence (ramped segment, or a clip with no audio stream). `has_audio`
    is a callable(src) -> bool, injected so the window math stays pure.
    """
    return [
        {"src": pk["src"], "start": pk["start"], "length": pk["length"]}
        if pk.get("ramp", "none") == "none" and has_audio(pk.get("src", ""))
        else {"src": None, "length": pk["length"]}
        for pk in picks
    ]


def _input_args(windows: list[dict]) -> list[str]:
    """ffmpeg input args for a set of windows: silence via anullsrc, clips via
    fast-seek + length cap + no video decode (pure, for tests)."""
    inputs: list[str] = []
    for w in windows:
        if w["src"] is None:
            inputs += ["-f", "lavfi", "-t", f"{w['length']:.3f}",
                       "-i", "anullsrc=r=44100:cl=stereo"]
        else:
            # -ss before -i = fast seek, -t caps the window to the pick's
            # length, -vn skips video decode entirely. Without -t the concat
            # would swallow the rest of each clip (a 3s clip x 16 picks
            # produced a 29s track for an 8s montage in testing).
            inputs += ["-ss", f"{w['start']:.3f}", "-t", f"{w['length']:.3f}",
                       "-vn", "-i", w["src"]]
    return inputs


def build_source_track(picks: list[dict], work_dir: str | Path) -> str | None:
    """Extract + concat every pick's source-audio window into one 44.1k
    stereo wav. Returns its path, or None if no pick has audio at all."""
    work = Path(work_dir)
    audio_cache: dict[str, bool] = {}

    def has_audio(src: str) -> bool:
        if src not in audio_cache:
            try:
                audio_cache[src] = video_info(src).has_audio
            except Exception:               # noqa: BLE001 — treat as silent
                audio_cache[src] = False
        return audio_cache[src]

    windows = source_windows(picks, has_audio)
    if not any(w["src"] is not None for w in windows):
        return None

    inputs = _input_args(windows)
    pre: list[str] = []
    for i, w in enumerate(windows):
        # concat needs uniform format across inputs
        pre.append(f"[{i}:a]aresample={SR},"
                   f"aformat=sample_fmts=fltp:channel_layouts=stereo[f{i}]")
    filt = (";".join(pre) + ";" + "".join(f"[f{i}]" for i in range(len(windows)))
            + f"concat=n={len(windows)}:v=0:a=1[a]")
    out = work / "source_audio.wav"
    try:
        ffmpeg.run([*inputs, "-filter_complex", filt, "-map", "[a]",
                    "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le",
                    str(out)], desc="source audio")
    except ffmpeg.FFmpegError:
        # e.g. a clip whose audio stream fails to decode: fall back to the
        # music-only mux instead of failing the whole render
        out.unlink(missing_ok=True)
        logger.warning("source-audio extraction failed — falling back to music-only")
        return None
    return str(out)
